import time as time_module
from datetime import datetime as dtime
from datetime import timedelta as delta
import psutil
import os


import numpy as np

try:
    from mpi4py import MPI
except:
    MPI = None

# from parcels.compiler import GNUCompiler
from parcels.wrapping.code_compiler import GNUCompiler_MS
from parcels.particleset_node import ParticleSet
# from parcels.kernel_vectorized import Kernel
from parcels.kernels.advection import AdvectionRK4
from parcels.particle import JITParticle
from parcels.tools.loggers import logger
from parcels.tools import get_cache_dir, get_package_dir
from parcels.tools import idgen

__all__ = ['ParticleSet_Benchmark']

class ParticleSet_TimingLog():
    _stime = 0
    _etime = 0
    _mtime = 0
    _samples = None
    _timings = None
    _iter = 0
    def __init__(self):
        self._stime = 0
        self._etime = 0
        self._mtime = 0
        self._samples = []
        self._timings = []
        self._iter = 0

    def start_timing(self):
        if MPI:
            mpi_comm = MPI.COMM_WORLD
            mpi_rank = mpi_comm.Get_rank()
            if mpi_rank == 0:
                #self._stime = MPI.Wtime()
                #self._stime = time_module.perf_counter()
                self._stime = time_module.process_time()
        else:
            self._stime = time_module.perf_counter()

    def stop_timing(self):
        if MPI:
            mpi_comm = MPI.COMM_WORLD
            mpi_rank = mpi_comm.Get_rank()
            if mpi_rank == 0:
                #self._etime = MPI.Wtime()
                #self._etime = time_module.perf_counter()
                self._etime = time_module.process_time()
        else:
            self._etime = time_module.perf_counter()

    def accumulate_timing(self):
        if MPI:
            mpi_comm = MPI.COMM_WORLD
            mpi_rank = mpi_comm.Get_rank()
            if mpi_rank == 0:
                self._mtime += (self._etime-self._stime)
            else:
                self._mtime = 0
        else:
            self._mtime += (self._etime-self._stime)

    def advance_iteration(self):
        if MPI:
            mpi_comm = MPI.COMM_WORLD
            mpi_rank = mpi_comm.Get_rank()
            if mpi_rank == 0:
                self._timings.append(self._mtime)
                self._samples.append(self._iter)
                self._iter+=1
            self._mtime = 0
        else:
            self._timings.append(self._mtime)
            self._samples.append(self._iter)
            self._iter+=1
            self._mtime = 0

    def __len__(self):
        return len(self._timings)

    def get_values(self):
        return self._timings

    def get_value(self, index):
        return self._timings[index]

class ParticleSet_ParamLogging():
    _samples = None
    _params = None
    _iter = 0
    def __init__(self):
        self._samples = []
        self._params = []
        self._iter = 0

    def advance_iteration(self, param):
        # logger.info("Pset length at i={}: {}".format(self._iter, param))
        self._params.append(param)
        self._samples.append(self._iter)
        self._iter+=1

    def __len__(self):
        return len(self._params)

    def get_params(self):
        return self._params

    def get_param(self, index):
        return self._params[index]


class ParticleSet_Benchmark(ParticleSet):

    def __init__(self, fieldset, pclass=JITParticle, lon=None, lat=None, depth=None, time=None, repeatdt=None,
                 lonlatdepth_dtype=None, pid_orig=None, **kwargs):
        super(ParticleSet_Benchmark, self).__init__(fieldset, pclass, lon, lat, depth, time, repeatdt, lonlatdepth_dtype, pid_orig, **kwargs)
        self.total_log = ParticleSet_TimingLog()
        self.compute_log = ParticleSet_TimingLog()
        self.io_log = ParticleSet_TimingLog()
        self.plot_log = ParticleSet_TimingLog()
        self.nparticle_log = ParticleSet_ParamLogging()
        self.process = psutil.Process(os.getpid())
        self.mem_log = ParticleSet_ParamLogging()

    #@profile
    def execute(self, pyfunc=AdvectionRK4, endtime=None, runtime=None, dt=1.,
                moviedt=None, recovery=None, output_file=None, movie_background_field=None,
                verbose_progress=None, postIterationCallbacks=None, callbackdt=None):
        """Execute a given kernel function over the particle set for
        multiple timesteps. Optionally also provide sub-timestepping
        for particle output.

        :param pyfunc: Kernel function to execute. This can be the name of a
                       defined Python function or a :class:`parcels.kernel.Kernel` object.
                       Kernels can be concatenated using the + operator
        :param endtime: End time for the timestepping loop.
                        It is either a datetime object or a positive double.
        :param runtime: Length of the timestepping loop. Use instead of endtime.
                        It is either a timedelta object or a positive double.
        :param dt: Timestep interval to be passed to the kernel.
                   It is either a timedelta object or a double.
                   Use a negative value for a backward-in-time simulation.
        :param moviedt:  Interval for inner sub-timestepping (leap), which dictates
                         the update frequency of animation.
                         It is either a timedelta object or a positive double.
                         None value means no animation.
        :param output_file: :mod:`parcels.particlefile.ParticleFile` object for particle output
        :param recovery: Dictionary with additional `:mod:parcels.tools.error`
                         recovery kernels to allow custom recovery behaviour in case of
                         kernel errors.
        :param movie_background_field: field plotted as background in the movie if moviedt is set.
                                       'vector' shows the velocity as a vector field.
        :param verbose_progress: Boolean for providing a progress bar for the kernel execution loop.
        :param postIterationCallbacks: (Optional) Array of functions that are to be called after each iteration (post-process, non-Kernel)
        :param callbackdt: (Optional, in conjecture with 'postIterationCallbacks) timestep inverval to (latestly) interrupt the running kernel and invoke post-iteration callbacks from 'postIterationCallbacks'
        """

        # check if pyfunc has changed since last compile. If so, recompile
        if self._kernel is None or (self._kernel.pyfunc is not pyfunc and self._kernel is not pyfunc):
            # Generate and store Kernel
            if isinstance(pyfunc, self._kclass):
                self._kernel = pyfunc
            else:
                self._kernel = self.Kernel(pyfunc)
            # Prepare JIT kernel execution
            if self._ptype.uses_jit:
                self._kernel.remove_lib()
                cppargs = ['-DDOUBLE_COORD_VARIABLES'] if self.lonlatdepth_dtype == np.float64 else None
                self._kernel.compile(compiler=GNUCompiler_MS(cppargs=cppargs, incdirs=[os.path.join(get_package_dir(), 'include'), os.path.join(get_package_dir(), 'nodes'), "."], tmp_dir=get_cache_dir()))
                self._kernel.load_lib()

        # Convert all time variables to seconds
        if isinstance(endtime, delta):
            raise RuntimeError('endtime must be either a datetime or a double')
        if isinstance(endtime, dtime):
            endtime = np.datetime64(endtime)

        if isinstance(endtime, np.datetime64):
            if self.time_origin.calendar is None:
                raise NotImplementedError('If fieldset.time_origin is not a date, execution endtime must be a double')
            endtime = self.time_origin.reltime(endtime)

        if isinstance(runtime, delta):
            runtime = runtime.total_seconds()
        if isinstance(dt, delta):
            dt = dt.total_seconds()
        outputdt = output_file.outputdt if output_file else np.infty
        if isinstance(outputdt, delta):
            outputdt = outputdt.total_seconds()

        if isinstance(moviedt, delta):
            moviedt = moviedt.total_seconds()
        if isinstance(callbackdt, delta):
            callbackdt = callbackdt.total_seconds()

        assert runtime is None or runtime >= 0, 'runtime must be positive'
        assert outputdt is None or outputdt >= 0, 'outputdt must be positive'
        assert moviedt is None or moviedt >= 0, 'moviedt must be positive'

        # ==== Set particle.time defaults based on sign of dt, if not set at ParticleSet construction => moved below (l. xyz)
        # piter = 0
        # while piter < len(self._nodes):
        #     pdata = self._nodes[piter].data
        # #node = self.begin()
        # #while node is not None:
        # #    pdata = node.data
        #     if np.isnan(pdata.time):
        #         mintime, maxtime = self._fieldset.gridset.dimrange('time_full')
        #         pdata.time = mintime if dt >= 0 else maxtime
        # #    node.set_data(pdata)
        #     self._nodes[piter].set_data(pdata)
        #     piter += 1

        # Derive _starttime and endtime from arguments or fieldset defaults
        if runtime is not None and endtime is not None:
            raise RuntimeError('Only one of (endtime, runtime) can be specified')


        mintime, maxtime = self._fieldset.gridset.dimrange('time_full')
        _starttime = min([n.data.time for n in self._nodes if not np.isnan(n.data.time)] + [mintime, ]) if dt >= 0 else max([n.data.time for n in self._nodes if not np.isnan(n.data.time)] + [maxtime, ])
        if self.repeatdt is not None and self.repeat_starttime is None:
            self.repeat_starttime = _starttime
        if runtime is not None:
            endtime = _starttime + runtime * np.sign(dt)
        elif endtime is None:
            endtime = maxtime if dt >= 0 else mintime

        # print("Fieldset min-max: {} to {}".format(mintime, maxtime))
        # print("starttime={} to endtime={} (runtime={})".format(_starttime, endtime, runtime))

        execute_once = False
        if abs(endtime-_starttime) < 1e-5 or dt == 0 or runtime == 0:
            dt = 0
            runtime = 0
            endtime = _starttime

            logger.warning_once("dt or runtime are zero, or endtime is equal to Particle.time. "
                                "The kernels will be executed once, without incrementing time")
            execute_once = True


        # ==== Initialise particle timestepping
        #for p in self:
        #    p.dt = dt
        piter = 0
        while piter < len(self._nodes):
            pdata = self._nodes[piter].data
            pdata.dt = dt
            if np.isnan(pdata.time):
                pdata.time = _starttime
            self._nodes[piter].set_data(pdata)
            piter += 1

        # First write output_file, because particles could have been added
        if output_file is not None:
            output_file.write(self, _starttime)

        if moviedt:
            self.show(field=movie_background_field, show_time=_starttime, animation=True)
        if moviedt is None:
            moviedt = np.infty
        if callbackdt is None:
            interupt_dts = [np.infty, moviedt, outputdt]
            if self.repeatdt is not None:
                interupt_dts.append(self.repeatdt)
            callbackdt = np.min(np.array(interupt_dts))

        time = _starttime
        if self.repeatdt and self.rparam is not None:
            next_prelease = self.repeat_starttime + (abs(time - self.repeat_starttime) // self.repeatdt + 1) * self.repeatdt * np.sign(dt)
        else:
            next_prelease = np.infty if dt > 0 else - np.infty
        next_output = time + outputdt if dt > 0 else time - outputdt

        next_movie = time + moviedt if dt > 0 else time - moviedt
        next_callback = time + callbackdt if dt > 0 else time - callbackdt

        next_input = self._fieldset.computeTimeChunk(time, np.sign(dt))

        tol = 1e-12

        if verbose_progress is None:
            walltime_start = time_module.time()
        if verbose_progress:
            pbar = self._create_progressbar_(_starttime, endtime)

        while (time < endtime and dt > 0) or (time > endtime and dt < 0) or dt == 0:
            self.total_log.start_timing()

            if verbose_progress is None and time_module.time() - walltime_start > 10:
                # Showing progressbar if runtime > 10 seconds
                if output_file:
                    logger.info('Temporary output files are stored in %s.' % output_file.tempwritedir_base)
                    logger.info('You can use "parcels_convert_npydir_to_netcdf %s" to convert these '
                                'to a NetCDF file during the run.' % output_file.tempwritedir_base)
                pbar = self._create_progressbar_(_starttime, endtime)
                verbose_progress = True

            if dt > 0:
                time = min(next_prelease, next_input, next_output, next_movie, next_callback, endtime)
            else:
                time = max(next_prelease, next_input, next_output, next_movie, next_callback, endtime)
            # ==== compute ==== #
            self.compute_log.start_timing()
            self._kernel.execute(self, endtime=time, dt=dt, recovery=recovery, output_file=output_file, execute_once=execute_once)
            if abs(time-next_prelease) < tol:
                add_iter = 0
                while add_iter < self.rparam.num_pts:
                    gen_id = self.rparam.get_particle_id(add_iter)
                    lon = self.rparam.get_longitude(add_iter)
                    lat = self.rparam.get_latitude(add_iter)
                    pdepth = self.rparam.get_depth_value(add_iter)
                    ptime = time
                    pindex = idgen.total_length
                    pid = idgen.nextID(lon, lat, pdepth, ptime) if gen_id is None else gen_id
                    pdata = JITParticle(lon=lon, lat=lat, pid=pid, fieldset=self._fieldset, depth=pdepth, time=ptime, index=pindex)
                    pdata.dt = dt
                    self.add(self._nclass(id=pid, data=pdata))
                    add_iter += 1
                next_prelease += self.repeatdt * np.sign(dt)
            self.compute_log.stop_timing()
            self.compute_log.accumulate_timing()
            # logger.info("Pset length: {}".format(len(self)))
            self.nparticle_log.advance_iteration(len(self))
            # ==== end compute ==== #
            if abs(time-next_output) < tol:  # ==== IO ==== #
                if output_file is not None:
                    self.io_log.start_timing()
                    output_file.write(self, time)
                    self.io_log.stop_timing()
                    self.io_log.accumulate_timing()
                next_output += outputdt * np.sign(dt)
            if abs(time-next_movie) < tol:  # ==== Plotting ==== #
                self.plot_log.start_timing()
                self.show(field=movie_background_field, show_time=time, animation=True)
                self.plot_log.stop_timing()
                self.plot_log.accumulate_timing()
                next_movie += moviedt * np.sign(dt)
            # ==== insert post-process here to also allow for memory clean-up via external func ==== #
            if abs(time-next_callback) < tol:
                if postIterationCallbacks is not None:
                    for extFunc in postIterationCallbacks:
                        extFunc()
                next_callback += callbackdt * np.sign(dt)
            if time != endtime:  # ==== IO ==== #
                self.io_log.start_timing()
                next_input = self.fieldset.computeTimeChunk(time, dt)
                self.io_log.stop_timing()
                self.io_log.accumulate_timing()
            if dt == 0:
                break
            if verbose_progress:  # ==== Plotting ==== #
                self.plot_log.start_timing()
                pbar.update(abs(time - _starttime))
                self.plot_log.stop_timing()
                self.plot_log.accumulate_timing()
            self.total_log.stop_timing()
            self.total_log.accumulate_timing()
            mem_B_used_total = 0
            if MPI:
                mpi_comm = MPI.COMM_WORLD
                mem_B_used = self.process.memory_info().rss
                mem_B_used_total = mpi_comm.reduce(mem_B_used, op=MPI.SUM, root=0)
            else:
                mem_B_used_total = self.process.memory_info().rss
            self.mem_log.advance_iteration(mem_B_used_total)

            self.compute_log.advance_iteration()
            self.io_log.advance_iteration()
            self.plot_log.advance_iteration()
            self.total_log.advance_iteration()

        if output_file is not None:
            self.io_log.start_timing()
            output_file.write(self, time)
            self.io_log.stop_timing()
            self.io_log.accumulate_timing()
        if verbose_progress:
            self.plot_log.start_timing()
            pbar.finish()
            self.plot_log.stop_timing()
            self.plot_log.accumulate_timing()