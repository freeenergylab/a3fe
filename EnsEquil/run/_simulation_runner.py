"""Abstract base class for simulation runners."""

from abc import ABC
import glob as _glob
from itertools import count as _count
import numpy as _np
import pathlib as _pathlib
import pickle as _pkl
import scipy.stats as _stats
import subprocess as _subprocess
from threading import Thread as _Thread
from time import sleep as _sleep
from typing import Optional as _Optional, Tuple as _Tuple, Dict as _Dict, Any as _Any
import logging as _logging

from ..analyse.plot import plot_convergence as _plot_convergence

class SimulationRunner(ABC):
    """An abstract base class for simulation runners. Note that
    self._sub_sim_runners (a list of SimulationRunner objects controlled
    by the current SimulationRunner) must be set in order to use methods
    such as run()"""

    # Count the number of instances so we can name things uniquely
    # for each instance
    class_count = _count()
    # Create list of files to be deleted by self.clean()
    run_files = []

    def __init__(self,
                 base_dir: _Optional[str] = None,
                 input_dir: _Optional[str] = None,
                 output_dir: _Optional[str] = None,
                 stream_log_level: int = _logging.INFO,
                 dg_multiplier: int = 1,
                 ensemble_size: int = 5) -> None:
        """
        base_dir : str, Optional, default: None
            Path to the base directory for the simulation runner.
            If None, this is set to the current working directory.
        input_dir : str, Optional, default: None
            Path to directory containing input files for the 
            simulation runner. If None, this is set to
            `base_directory/input`.
        output_dir : str, Optional, default: None
            Path to the output directory in which to store the
            output from the simulation. If None, this is set 
            to `base_directory/output`.
        stream_log_level : int, Optional, default: logging.INFO
            Logging level to use for the steam file handlers for the
            calculation object and its child objects.
        dg_multiplier : int, Optional, default: 1
            +1 or -1. Records whether the free energy change should 
            be multiplied by +1 or -1 when being added to the total
            free energy change for the super simulation-runner.
        ensemble_size : int, Optional, default: 5
            Number of repeats to run.
        """
        # Set up the directories (which may be overwritten if the 
        # simulation runner is subsequently loaded from a pickle file)
        if base_dir is None:
            base_dir = str(_pathlib.Path.cwd())
        if not _pathlib.Path(base_dir).is_dir():
            _pathlib.Path(base_dir).mkdir(parents=True)
        if input_dir is None:
            input_dir = str(_pathlib.Path(base_dir, "input"))
        if output_dir is None:
            output_dir = str(_pathlib.Path(base_dir, "output"))

        # Only create the input and output directories if they're called, using properties
        self.base_dir = base_dir
        self._input_dir = input_dir
        self._output_dir = output_dir

        # Check if we are starting from a previous simulation runner
        self.loaded_from_pickle = False
        if _pathlib.Path(f"{base_dir}/{self.__class__.__name__}.pkl").is_file():
            self._load() # May overwrite the above attributes and options

        else:
            # Initialise sub-simulation runners with an empty list
            self._sub_sim_runners = []

            # Add the dg_multiplier
            if dg_multiplier not in [-1, 1]:
                raise ValueError(f"dg_multiplier must be either +1 or -1, not {dg_multiplier}.")
            self.dg_multiplier = dg_multiplier

            # Register the ensemble size
            self.ensemble_size = ensemble_size

            # Set up logging
            self._stream_log_level = stream_log_level
            self._set_up_logging()

            # Save state
            self._dump()

    def _set_up_logging(self) -> None:
        """Set up the logging for the simulation runner."""
        # TODO: Debug - why is debug output no longer working
        # If logger exists, remove it and start again
        if hasattr(self, "_logger"):
            handlers = self._logger.handlers[:]
            for handler in handlers:
                self._logger.removeHandler(handler)
                handler.close()
            del(self._logger)
        # Name each logger individually to avoid clashes
        self._logger = _logging.getLogger(f"{str(self)}_{next(self.__class__.class_count)}")
        self._logger.setLevel(_logging.DEBUG)
        self._logger.propagate = False
        # For the file handler, we want to log everything
        file_handler = _logging.FileHandler(f"{self.base_dir}/{self.__class__.__name__}.log")
        file_handler.setFormatter(_logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
        file_handler.setLevel(_logging.DEBUG)
        # For the stream handler, we want to log at the user-specified level
        stream_handler = _logging.StreamHandler()
        stream_handler.setFormatter(_logging.Formatter("%(name)s - %(levelname)s - %(message)s"))
        stream_handler.setLevel(self._stream_log_level)
        # Add the handlers to the logger
        self._logger.addHandler(file_handler)
        self._logger.addHandler(stream_handler)


    @property
    def input_dir(self) -> str:
        """The input directory for the simulation runner."""
        if not _pathlib.Path(self._input_dir).is_dir():
            _pathlib.Path(self._input_dir).mkdir(parents=True)
        return self._input_dir

    @input_dir.setter
    def input_dir(self, value: str) -> None:
        f"""Set the input directory for the {self.__class__.__name__}."""
        self._input_dir = value

    @property
    def output_dir(self) -> str:
        f"""The output directory for the {self.__class__.__name__}."""
        if not _pathlib.Path(self._output_dir).is_dir():
            _pathlib.Path(self._output_dir).mkdir(parents=True)
        return self._output_dir
    
    @output_dir.setter
    def output_dir(self, value: str) -> None:
        f"""Set the output directory for the {self.__class__.__name__}."""
        self._output_dir = value

    def __str__(self) -> str:
        return self.__class__.__name__

    def run(self, *args, **kwargs) -> None:
        f"""Run the {self.__class__.__name__}"""
        self._logger.info(f"Running {self.__class__.__name__}...")
        for sub_sim_runner in self._sub_sim_runners:
            sub_sim_runner.run(*args, **kwargs)

    def kill(self) -> None:
        f"""Kill the {self.__class__.__name__}"""
        self._logger.info(f"Killing {self.__class__.__name__}...")
        for sub_sim_runner in self._sub_sim_runners:
            sub_sim_runner.kill()

    def analyse(self, subsampling=False) -> _Tuple[_np.ndarray, _np.ndarray]:
        f"""
        Analyse the {self.__class__.__name__} and any
        sub-simulations, and return the overall free energy
        change.

        Parameters
        ----------
        subsampling: bool, optional, default=False
            If True, the free energy will be calculated by subsampling using
            the methods contained within pymbar.

        Returns
        -------
        dg_overall : np.ndarray
            The overall free energy change for each of the 
            ensemble size repeats.
        er_overall : np.ndarray
            The overall error for each of the ensemble size
            repeats.
        """
        self._logger.info(f"Analysing {self.__class__.__name__}...")
        dg_overall = _np.zeros(self.ensemble_size)
        er_overall = _np.zeros(self.ensemble_size)

        # Analyse the sub-simulation runners
        for sub_sim_runner in self._sub_sim_runners:
            dg, er = sub_sim_runner.analyse(subsampling=subsampling)
            # Decide if the component should be added or subtracted
            # according to the dg_multiplier attribute
            dg_overall += dg * sub_sim_runner.dg_multiplier
            er_overall = _np.sqrt(er_overall**2 + er**2)

        # Log the overall free energy changes
        self._logger.info(f"Overall free energy changes: {dg_overall} kcal mol-1")
        self._logger.info(f"Overall errors: {er_overall} kcal mol-1")

        # Calculate the 95 % confidence interval assuming Gaussian errors
        mean_free_energy = _np.mean(dg_overall)
        # Gaussian 95 % C.I.
        conf_int = _stats.t.interval(0.95,
                                    len(dg_overall)-1,
                                    mean_free_energy,
                                    scale=_stats.sem(dg_overall))[1] - mean_free_energy  # 95 % C.I.

        # Write overall MBAR stats to file
        with open(f"{self.output_dir}/overall_stats.dat", "a") as ofile:
            ofile.write("###################################### Free Energies ########################################\n")
            ofile.write(f"Mean free energy: {mean_free_energy: .3f} + /- {conf_int:.3f} kcal/mol\n")
            for i in range(self.ensemble_size):
                ofile.write(f"Free energy from run {i+1}: {dg_overall[i]: .3f} +/- {er_overall[i]:.3f} kcal/mol\n")
            ofile.write("Errors are 95 % C.I.s based on the assumption of a Gaussian distribution of free energies\n")

        return dg_overall, er_overall

    def analyse_convergence(self) -> _Tuple[_np.ndarray, _np.ndarray]:
        f"""
        Get a timeseries of the total free energy change of the
        {self.__class__.__name__} against total simulation time. Also plot this.
        Keep this separate from analyse as it is expensive to run.
        
        Returns
        -------
        fracts : np.ndarray
            The fraction of the total equilibrated simulation time for each value of dg_overall.
        dg_overall : np.ndarray
            The overall free energy change for the {self.__class__.__name__} for
            each value of total equilibrated simtime for each of the ensemble size repeats. 
        """
        self._logger.info(f"Analysing convergence of {self.__class__.__name__}...")
        
        # Get the dg_overall in terms of fraction of the total simulation time
        # Use steps of 5 % of the total simulation time
        fracts = _np.arange(0.05, 1.05, 0.05)
        # Create an array to store the overall free energy change
        dg_overall = _np.zeros((self.ensemble_size, len(fracts)))

        # Now add up the data for each of the sub-simulation runners
        for sub_sim_runner in self._sub_sim_runners:
            _, dgs = sub_sim_runner.analyse_convergence()
            # Decide if the component should be added or subtracted
            # according to the dg_multiplier attribute
            dg_overall += dgs * sub_sim_runner.dg_multiplier

        self._logger.info(f"Overall free energy changes: {dg_overall} kcal mol-1")
        self._logger.info(f"Fractions of equilibrated simulation time: {fracts}")

        # Plot the overall convergence
        _plot_convergence(fracts, dg_overall, self.tot_simtime, self.equil_time, self.output_dir)

        return fracts, dg_overall

    @property
    def running(self) -> bool:
        f"""Check if the {self.__class__.__name__} is running."""
        return any([sub_sim_runner.running for sub_sim_runner in self._sub_sim_runners])
    
    def wait(self) -> None:
        f"""Wait for the {self.__class__.__name__} to finish running."""
        # Give the simulation runner a chance to start
        _sleep(30)
        while self.running:
            _sleep(30) # Check every 30 seconds

    @property
    def tot_simtime(self) -> float:
        f"""The total simulation time in ns for the {self.__class__.__name__} and any sub-simulation runners."""
        return sum([sub_sim_runner.tot_simtime for sub_sim_runner in self._sub_sim_runners]) # ns

    @property
    def equilibrated(self) -> float:
        f"""Whether the {self.__class__.__name__} is equilibrated."""
        return all([sub_sim_runner.equilibrated for sub_sim_runner in self._sub_sim_runners])

    @property
    def equil_time(self) -> float:
        f"""The equilibration time in ns for the {self.__class__.__name__} and any sub-simulation runners."""
        return sum([sub_sim_runner.equil_time for sub_sim_runner in self._sub_sim_runners]) # ns

    def _refresh_logging(self) -> None:
        """Refresh the logging for the simulation runner and all sub-simulation runners."""
        self._set_up_logging()
        for sub_sim_runner in self._sub_sim_runners:
            sub_sim_runner._refresh_logging()

    def update_paths(self, old_sub_path: str, new_sub_path: str) -> None:
        """ 
        Replace the old sub-path with the new sub-path in the base, input, and output directory
        paths.

        Parameters
        ----------
        old_sub_path : str
            The old sub-path to replace.
        new_sub_path : str
            The new sub-path to replace the old sub-path with.
        """
        # Use private attributes to avoid triggering the property setters
        # which might cause issues by creating directories
        for attr in ["base_dir", "_input_dir", "_output_dir"]:
            setattr(self, attr, getattr(self, attr).replace(old_sub_path, new_sub_path))

        # Now update the loggers, which depend on the paths
        self._set_up_logging()

        # Also update the loggers of any virtual queues
        if hasattr(self, "virtual_queue"):
            # Virtual queue may have already been updated
            if new_sub_path not in self.virtual_queue.log_dir: # type: ignore
                self.virtual_queue.log_dir = self.virtual_queue.log_dir.replace(old_sub_path, new_sub_path) # type: ignore
                self.virtual_queue._set_up_logging() # type: ignore

        # Update the paths of any sub-simulation runners
        for sub_sim_runner in self._sub_sim_runners:
            sub_sim_runner.update_paths(old_sub_path, new_sub_path)

    def set_simfile_option(self, option: str, value: str) -> None:
        """Set the value of an option in the simulation configuration file."""
        for sub_sim_runner in self._sub_sim_runners:
            sub_sim_runner.set_simfile_option(option, value)

    @property
    def stream_log_level(self) -> int:
        """The log level for the stream handler."""
        return self._stream_log_level
    
    @stream_log_level.setter
    def stream_log_level(self, value: int) -> None:
        """Set the log level for the stream handler."""
        self._stream_log_level = value
        # Ensure the new setting is applied
        self._set_up_logging()
        if hasattr(self, "_sub_sim_runners"):
            for sub_sim_runner in self._sub_sim_runners:
                sub_sim_runner.stream_log_level = value
                sub_sim_runner._set_up_logging()

    def clean(self, clean_logs=False) -> None:
        f"""
        Clean the {self.__class__.__name__} by deleting all files
        with extensions matching {self.__class__.run_files} in the 
        base and output dirs, and resetting the total runtime to 0.

        Parameters
        ----------
        clean_logs : bool, default=False
            If True, also delete the log files.
        """
        run_files = self.__class__.run_files
        if clean_logs:
            run_files += self.__class__.__name__ + ".log"

        for run_file in run_files:
            # Delete files in base directory
            for file in _pathlib.Path(self.base_dir).glob(run_file):
                self._logger.info(f"Deleting {file}")
                _subprocess.run(["rm", file])

            # Delete files in output directory
            for file in _pathlib.Path(self.output_dir).glob(run_file):
                self._logger.info(f"Deleting {file}")
                _subprocess.run(["rm", file])

        # Clean any sub-simulation runners
        if hasattr(self, "_sub_sim_runners"):
            for sub_sim_runner in self._sub_sim_runners:
                sub_sim_runner.clean(clean_logs=clean_logs)

    def _update_log(self) -> None:
        f""" Update the status log file with the current status of the {self.__class__.__name__}."""
        self._logger.info("##############################################")
        for var in vars(self):
            self._logger.info(f"{var}: {getattr(self, var)}")
        self._logger.info("##############################################")

    @property
    def _pickable_dict(self) -> _Dict[str, _Any]:
        """Return a version of __dict__ that can be pickled, by removing any thread_lock
        objects, which cannot be pickled."""
        new_dict = {}
        for key, val in self.__dict__.items():
            if isinstance(val, _Thread):
                new_dict[key] = None
            # If this is a list of sub-simulation runners, then we need to
            # recursively call _pickable_dict on each of them and their sub-simulations
            elif isinstance(val, SimulationRunner):
                new_dict[key] = val._pickable_dict
            else:
                new_dict[key] = val        

        return new_dict

    def _dump(self) -> None:
        """ Dump the current state of the simulation object to a pickle file, and do
        the same for any sub-simulations."""
        with open(f"{self.base_dir}/{self.__class__.__name__}.pkl", "wb") as ofile:
            _pkl.dump(self._pickable_dict, ofile)
        for sub_sim_runner in self._sub_sim_runners:
            sub_sim_runner._dump()

    def _load(self) -> None:
        """Load the state of the simulation object from a pickle file, and do
        the same for any sub-simulations."""
        # Note that we cannot recursively call _load on the sub-simulations
        # because this results in the creation of different virtual queues for the
        # stages and sub-lam-windows and simulations
        if not _pathlib.Path(f"{self.base_dir}/{self.__class__.__name__}.pkl").is_file():
            raise FileNotFoundError(f"Could not find {self.__class__.__name__}.pkl in {self.base_dir}")

        print(f"Loading previous {self.__class__.__name__}. Any arguments will be overwritten...")
        with open(f"{self.base_dir}/{self.__class__.__name__}.pkl", "rb") as file:
            self.__dict__ = _pkl.load(file)

        # Refresh logging
        print("Setting up logging...")
        self._refresh_logging()

        # Record that the object was loaded from a pickle file
        self.loaded_from_pickle = True
