import os
import math
import numpy
import subprocess
from lazyflow.rtype import Roi, SubRegion
from lazyflow.graph import Operator, InputSlot, OutputSlot
import itertools
import h5py
import time
import threading
from functools import partial
import tempfile
import shutil
import datetime

from lazyflow.operators import OpH5WriterBigDataset, OpSubRegion

import logging
logger = logging.getLogger(__name__)


class Timer(object):
    def __init__(self):
        self.startTime = None
        self.stopTime = None
    
    def __enter__(self):
        self.startTime = datetime.datetime.now()
        return self
    
    def __exit__(self, *args):
        self.stopTime = datetime.datetime.now()
    
    def seconds(self):
        assert self.startTime is not None, "Timer hasn't started yet!"
        if self.stopTime is None:
            return (datetime.datetime.now() - self.startTime).seconds
        else:
            return (self.stopTime - self.startTime).seconds

STATUS_FILE_NAME_FORMAT = "{} status {}.txt"
OUTPUT_FILE_NAME_FORMAT = "{} output {}.h5"

class OpTaskWorker(Operator):
    ScratchDirectory = InputSlot(stype='filestring')
    Input = InputSlot()
    RoiString = InputSlot(stype='string')
    TaskName = InputSlot(stype='string')
    
    ReturnCode = OutputSlot()

    def setupOutputs(self):
        self.ReturnCode.meta.dtype = bool
        self.ReturnCode.meta.shape = (1,)
    
    def execute(self, slot, subindex, roi, result):
        roiString = self.RoiString.value
        roi = Roi.loads(roiString)
        logger.info( "Executing for roi: {}".format(roi) )
        roituple = ( tuple(roi.start), tuple(roi.stop) )
        statusFileName = STATUS_FILE_NAME_FORMAT.format( self.TaskName.value, str(roituple) )
        outputFileName = OUTPUT_FILE_NAME_FORMAT.format( self.TaskName.value, str(roituple) )

        statusFilePath = os.path.join( self.ScratchDirectory.value, statusFileName )
        outputFilePath = os.path.join( self.ScratchDirectory.value, outputFileName )

        # Create a temporary file to generate the output
        tempDir = tempfile.mkdtemp()
        tmpOutputFile = os.path.join(tempDir, roiString + ".h5")
        logger.info("Constructing output in temporary file: {}".format( tmpOutputFile ))
        
        # Create the output file in our local scratch area.
        with h5py.File( tmpOutputFile, 'w' ) as outputFile:
            assert self.Input.ready()
    
            # Extract sub-region
            opSubRegion = OpSubRegion(parent=self, graph=self.graph)
            opSubRegion.Input.connect( self.Input )
            opSubRegion.Start.setValue( tuple(roi.start) )
            opSubRegion.Stop.setValue( tuple(roi.stop) )
    
            assert opSubRegion.Output.ready()
    
            # Set up the write operator
            opH5Writer = OpH5WriterBigDataset(parent=self, graph=self.graph)
            opH5Writer.hdf5File.setValue( outputFile )
            opH5Writer.hdf5Path.setValue( 'node_result' )
            opH5Writer.Image.connect( opSubRegion.Output )
    
            assert opH5Writer.WriteImage.ready()

            with Timer() as computeTimer:
                result[0] = opH5Writer.WriteImage.value
                logger.info( "Finished task in {} seconds".format( computeTimer.seconds() ) )
        
        # Now copy the result file to the scratch area to be picked up by the master process
        with Timer() as copyTimer:
            logger.info( "Copying {} to {}...".format(tmpOutputFile, outputFilePath) )
            shutil.copyfile(tmpOutputFile, outputFilePath)
            logger.info( "Finished copying after {} seconds".format( copyTimer.seconds() ) )
        
        # Now create the status file to show that we're finished.
        statusFile = file(statusFilePath, 'w')
        statusFile.write('Yay!')
        
        return result

    def propagateDirty(self, slot, subindex, roi):
        self.ReturnCode.setDirty( slice(None) )

class OpClusterize(Operator):
    ProjectFilePath = InputSlot(stype='filestring')
    ScratchDirectory = InputSlot(stype='filestring')
    CommandFormat = InputSlot(stype='string') # Format string for spawning a node task.
    WorkflowTypeName = InputSlot(stype='string')
    NumJobs = InputSlot()
    TaskTimeoutSeconds = InputSlot()
    Input = InputSlot()

    OutputFilePath = InputSlot()
    
    ReturnCode = OutputSlot()

    class TaskInfo():
        taskName = None
        command = None
        statusFilePath = None
        outputFilePath = None
        subregion = None
        
    def setupOutputs(self):
        self.ReturnCode.meta.dtype = bool
        self.ReturnCode.meta.shape = (1,)
    
    def execute(self, slot, subindex, roi, result):
        success = True
        
        taskInfos = self._prepareTaskInfos()
        
        # Spawn each task
        for taskInfo in taskInfos.values():
            logger.info("Launching node task: " + taskInfo.command )
            # Use a separate thread to spawn the task.
            # This shouldn't add much overhead, and we won't block if the command that actually spawns the work is blocking.
            th = threading.Thread( target=partial(subprocess.call, taskInfo.command, shell=True  ) )
            th.start()

        timeOut = self.TaskTimeoutSeconds.value
        with Timer() as totalTimer:
            # When each task completes, it creates a status file.
            while len(taskInfos) > 0:
                # TODO: Maybe replace this naive polling system with an asynchronous 
                #         file status via select.epoll or something like that.
                if totalTimer.seconds() >= timeOut:
                    logger.error("Timing out after {} seconds, even though {} tasks haven't finished yet.".format( totalTimer.seconds(), len(taskInfos) ) )
                    success = False
                    break
                time.sleep(15.0)
    
                logger.debug("Time: {} seconds. Checking {} remaining tasks....".format(totalTimer.seconds(), len(taskInfos)))
    
                # Figure out which results have finished already and copy their results into the final output file
                finished_rois = self._copyFinishedResults( taskInfos )
    
                # Remove the finished tasks from the list we're polling for
                for roi in finished_rois:
                    del taskInfos[roi]
                
                # Handle failured tasks
                failed_rois = self._checkForFailures( taskInfos )
                if len(failed_rois) > 0:
                    success = False
    
                # Remove the failed tasks from the list we're polling for
                for roi in failed_rois:
                    logger.error( "Giving up on failed task: {} for roi: {}".format( taskInfos[roi].taskName, roi ) )
                    del taskInfos[roi]

        if success:
            logger.info( "SUCCESS after {} seconds.".format( totalTimer.seconds() ) )
        else:
            logger.info( "FAILED after {} seconds.".format( totalTimer.seconds() ) )

        result[0] = success
        return result
    
    def _getRoiList(self):
        inputShape = self.Input.meta.shape
        # Use a dumb means of computing task shapes for now.
        # Find the dimension of the data in xyz, and block it up that way.
        taggedShape = self.Input.meta.getTaggedShape()

        spaceDims = filter( lambda (key, dim): key in 'xyz' and dim > 1, taggedShape.items() ) 
        numJobs = self.NumJobs.value
        numJobsPerSpaceDim = math.pow(numJobs, 1.0/len(spaceDims))
        numJobsPerSpaceDim = int(round(numJobsPerSpaceDim))

        roiShape = []
        for key, dim in taggedShape.items():
            if key in [key for key, value in spaceDims]:
                roiShape.append(dim / numJobsPerSpaceDim)
            else:
                roiShape.append(dim)

        roiShape = numpy.array(roiShape)
        
        rois = []
        for indices in itertools.product( *[ range(0, stop, step) for stop,step in zip(inputShape, roiShape) ] ):
            start=numpy.asarray(indices)
            stop=numpy.minimum( start+roiShape, inputShape )
            rois.append( (start, stop) )

        return rois

    def _prepareTaskInfos(self):
        # Divide up the workload into large pieces
        rois = self._getRoiList()
        logger.info( "Dividing into {} node jobs.".format( len(rois) ) )
                
        commandFormat = self.CommandFormat.value

        taskInfos = {}
        for roiIndex, roi in enumerate(rois):
            roi = ( tuple(roi[0]), tuple(roi[1]) )
            taskInfo = OpClusterize.TaskInfo()
            taskInfo.subregion = SubRegion( None, start=roi[0], stop=roi[1] )
            
            taskName = "TASK_{}".format(roiIndex)
            statusFileName = STATUS_FILE_NAME_FORMAT.format( taskName, str(roi) )
            outputFileName = OUTPUT_FILE_NAME_FORMAT.format( taskName, str(roi) )

            statusFilePath = os.path.join( self.ScratchDirectory.value, statusFileName )
            outputFilePath = os.path.join( self.ScratchDirectory.value, outputFileName )


            commandArgs = []
            commandArgs.append( "--workflow_type=" + self.WorkflowTypeName.value )
            commandArgs.append( "--project=" + self.ProjectFilePath.value )
            commandArgs.append( "--scratch_directory=" + self.ScratchDirectory.value )
            commandArgs.append( "--_node_work_=\"" + Roi.dumps( taskInfo.subregion ) + "\"" )
            commandArgs.append( "--process_name={}".format(taskName)  )

            # If the user overrode the temp dir to use, override it for the worker processes, too.            
            if tempfile.tempdir is not None:
                commandArgs.append( "--sys_tmp_dir={}".format( tempfile.tempdir ))
            
            # Check the command format string: We need to know where to put our args...
            assert commandFormat.find("{args}") != -1
            
            allArgs = " " + " ".join(commandArgs) + " "
            taskInfo.taskName = taskName
            taskInfo.command = commandFormat.format( args=allArgs, task_name=taskName )
            taskInfo.statusFilePath = statusFilePath
            taskInfo.outputFilePath = outputFilePath
            taskInfos[roi] = taskInfo

            # If files are still hanging around from the last run, delete them.
            if os.path.exists( statusFilePath ):
                os.remove( statusFilePath )
            if os.path.exists( outputFilePath ):
                os.remove( outputFilePath )

        return taskInfos

    def _copyFinishedResults(self, taskInfos):
        finished_rois = []
        destinationFile = None
        for roi, taskInfo in taskInfos.items():
            # Has the task completed yet?
            #logger.debug( "Checking for file: {}".format( taskInfo.statusFilePath ) )
            if not os.path.exists( taskInfo.statusFilePath ):
                continue

            logger.info( "Found status file: {}".format( taskInfo.statusFilePath ) )
            if not os.path.exists( taskInfo.outputFilePath ):
                raise RuntimeError( "Error: Could not locate output file from spawned task: " + taskInfo.outputFilePath )

            # Open the file
            f = h5py.File( taskInfo.outputFilePath, 'r' )

            # Check the result
            assert 'node_result' in f.keys()
            assert numpy.all(f['node_result'].shape == numpy.subtract(roi[1], roi[0]))
            assert f['node_result'].dtype == self.Input.meta.dtype
            assert f['node_result'].attrs['axistags'] == self.Input.meta.axistags.toJSON()

            # Open the destination file if necessary
            if destinationFile is None:
                destinationFile = h5py.File( self.OutputFilePath.value )
                if 'cluster_result' not in destinationFile.keys():
                    destinationFile.create_dataset('cluster_result', shape=self.Input.meta.shape, dtype=self.Input.meta.dtype)

            # Copy the data into our result (which might be an h5py dataset...)
            key = taskInfo.subregion.toSlice()

            with Timer() as copyTimer:
                destinationFile['cluster_result'][key] = f['node_result'][:]

            shape = f['node_result'][:].shape
            dtype = f['node_result'][:].dtype
            if type(dtype) is numpy.dtype:
                # Make sure we're dealing with a type (e.g. numpy.float64),
                #  not a numpy.dtype
                dtype = dtype.type
            
            dtypeBytes = dtype().nbytes
            totalBytes = dtypeBytes * numpy.prod(shape)
            totalMB = totalBytes / 1000

            logger.info( "Copying {} MB took {} seconds".format(totalMB, copyTimer.seconds() ) )
            finished_rois.append(roi)

        # For now, we close the file after every pass in case something goes horribly wrong...
        if destinationFile is not None:
            destinationFile.close()

        return finished_rois

    def _checkForFailures(self, taskInfos):
        return []

    def propagateDirty(self, slot, subindex, roi):
        self.ReturnCode.setDirty( slice(None) )

































