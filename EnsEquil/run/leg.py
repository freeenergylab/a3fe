"""Functionality for managing legs of the calculation."""

import glob as _glob
import BioSimSpace.Sandpit.Exscientia as _BSS
from enum import Enum as _Enum
import logging as _logging
import numpy as _np
import os as _os
import pathlib as _pathlib
import shutil as _shutil
import subprocess as _subprocess
from typing import Dict as _Dict, List as _List, Tuple as _Tuple, Any as _Any, Optional as _Optional

from ..analyse.plot import plot_convergence as _plot_convergence
from .stage import Stage as _Stage, StageType as _StageType
from ..read._process_somd_files import read_simfile_option as _read_simfile_option, write_simfile_option as _write_simfile_option
from .. read._process_bss_systems import rename_lig as _rename_lig
from ._simulation_runner import SimulationRunner as _SimulationRunner
from ._utils import check_has_wat_and_box as _check_has_wat_and_box

class LegType(_Enum):
    """The type of leg in the calculation."""
    BOUND = 1
    FREE = 2

class PreparationStage(_Enum):
    """The stage of preparation of the input files."""
    STRUCTURES_ONLY = 1
    PARAMETERISED = 2
    SOLVATED = 3
    MINIMISED = 4
    PREEQUILIBRATED = 5

    @property
    def file_suffix(self) -> str:
        """Return the suffix to use for the files in this stage."""
        if self == PreparationStage.STRUCTURES_ONLY:
            return ""
        elif self == PreparationStage.PARAMETERISED:
            return "_param"
        elif self == PreparationStage.SOLVATED:
            return "_solv"
        elif self == PreparationStage.MINIMISED:
            return "_min"
        elif self == PreparationStage.PREEQUILIBRATED:
            return "_preequil"
        else:
            raise ValueError(f"Unknown preparation stage: {self}")
        
    def get_simulation_input_files(self, leg_type: LegType) -> _List[str]:
        """Return the input files required for the simulation in this stage."""
        if self == PreparationStage.STRUCTURES_ONLY:
            if leg_type == LegType.BOUND:
                return ["protein.pdb", "ligand.sdf"] # Need sdf for parameterisation of lig
            elif leg_type == LegType.FREE:
                return ["ligand.sdf"]
        else:
            return [f"{leg_type.name.lower()}{self.file_suffix}.{file_type}" for file_type in ["prm7", "rst7"]]

class Leg(_SimulationRunner):
    """
    Class set up and run the stages of a leg of the calculation.
    """
    # Required input files for each leg type and preparation stage.
    required_input_files = {}
    for leg_type in LegType:
        required_input_files[leg_type] = {}
        for prep_stage in PreparationStage:
            required_input_files[leg_type][prep_stage] = ["run_somd.sh", "template_config.cfg"] + prep_stage.get_simulation_input_files(leg_type)

    required_stages = {LegType.BOUND: [_StageType.RESTRAIN, _StageType.DISCHARGE, _StageType.VANISH],
                        LegType.FREE: [_StageType.DISCHARGE, _StageType.VANISH]}

    default_lambda_values = {LegType.BOUND: { _StageType.RESTRAIN: [0.000, 0.125, 0.250, 0.375, 0.500, 1.000],
                                             _StageType.DISCHARGE: [0.000, 0.143, 0.286, 0.429, 0.571, 0.714, 0.857, 1.000],
                                             _StageType.VANISH: [0.000, 0.025, 0.050, 0.075, 0.100, 0.125, 0.150, 0.175, 0.200, 0.225, 0.250, 0.275, 0.300, 0.325, 0.350, 0.375, 0.400, 0.425, 0.450, 0.475, 0.500, 0.525, 0.550, 0.575, 0.600, 0.625, 0.650, 0.675, 0.700, 0.725, 0.750, 0.800, 0.850, 0.900, 0.950, 1.000]},
                            LegType.FREE: { _StageType.DISCHARGE: [0.000, 0.143, 0.286, 0.429, 0.571, 0.714, 0.857, 1.000],
                                            _StageType.VANISH: [0.000, 0.028, 0.056, 0.111, 0.167, 0.222, 0.278, 0.333, 0.389, 0.444, 0.500, 0.556, 0.611, 0.667, 0.722, 0.778, 0.889, 1.000 ]}}

    def __init__(self, 
                 leg_type: LegType,
                 block_size: float = 1,
                 equil_detection: str = "block_gradient",
                 gradient_threshold: _Optional[float] = None,
                 ensemble_size: int = 5,
                 base_dir: _Optional[str] = None,
                 input_dir: _Optional[str] = None,
                 stream_log_level: int = _logging.INFO,
                 update_paths: bool = True) -> None:
        """
        Instantiate a calculation based on files in the input dir. If leg.pkl exists in the
        base directory, the calculation will be loaded from this file and any arguments
        supplied will be overwritten.

        Parameters
        ----------
        block_size : float, Optional, default: 1
            Size of blocks to use for equilibration detection, in ns.
        equil_detection : str, Optional, default: "block_gradient"
            Method to use for equilibration detection. Options are:
            - "block_gradient": Use the gradient of the block averages to detect equilibration.
            - "chodera": Use Chodera's method to detect equilibration.
        gradient_threshold : float, Optional, default: None
            The threshold for the absolute value of the gradient, in kcal mol-1 ns-1,
            below which the simulation is considered equilibrated. If None, no theshold is
            set and the simulation is equilibrated when the gradient passes through 0. A 
            sensible value appears to be 0.5 kcal mol-1 ns-1.
        ensemble_size : int, Optional, default: 5
            Number of simulations to run in the ensemble.
        base_dir : str, Optional, default: None
            Path to the base directory in which to set up the stages. If None,
            this is set to the current working directory.
        input_dir : str, Optional, default: None
            Path to directory containing input files for the simulations. If None, this
            is set to `current_working_directory/input`.
        stream_log_level : int, Optional, default: logging.INFO
            Logging level to use for the steam file handlers for the
            calculation object and its child objects.
        update_paths: bool, optional, default: True
            if true, if the simulation runner is loaded by unpickling, then
            update_paths() is called.

        Returns
        -------
        None
        """
        # Set the leg type, as this is needed in the superclass constructor
        self.leg_type = leg_type

        super().__init__(base_dir=base_dir,
                         input_dir=input_dir,
                         stream_log_level=stream_log_level,
                         ensemble_size=ensemble_size,
                         update_paths=update_paths)

        if not self.loaded_from_pickle:
            self.stage_types = Leg.required_stages[leg_type]
            self.block_size = block_size
            self.equil_detection = equil_detection
            self.gradient_threshold = gradient_threshold
            self._running: bool = False

            # Change the sign of the dg contribution to negative
            # if this is the bound leg
            if self.leg_type == LegType.BOUND:
                self.dg_multiplier = -1

            # Validate the input
            self._validate_input()

            # Save the state and update log
            self._update_log()
            self._dump()

    def __str__(self) -> str:
        return f"Leg (type = {self.leg_type.name})"

    @property
    def stages(self) -> _List[_SimulationRunner]:
        return self._sub_sim_runners

    @stages.setter
    def legs(self, value) -> None:
        self._logger.info("Modifying/ creating stages")
        self._sub_sim_runners = value

    def _validate_input(self) -> None:
        """Check that the required files are provided for the leg type and set the preparation stage
        according to the files present."""
        # Check backwards, as we care about the most advanced preparation stage
        for prep_stage in reversed(PreparationStage):
            files_absent = False
            for file in Leg.required_input_files[self.leg_type][prep_stage]:
                if not _os.path.isfile(f"{self.input_dir}/{file}"):
                    files_absent = True
            # We have the required files for this prep stage, and this is the most 
            # advanced prep stage that files are present for
            if not files_absent:
                self._logger.info(f"Found all required input files for preparation stage {prep_stage.name.lower()}")
                self.prep_stage = prep_stage
                return 
        # We didn't find all required files for any of the prep stages
        raise ValueError(f"Could not find all required input files for leg type {self.leg_type.name} for " \
                          f"any preparation stage. Required files are: {Leg.required_input_files[self.leg_type]}")


    def setup(self, use_same_restraints:bool = False) -> None:
        """
        Set up the leg. This involves:
            - Creating the input directories
            - Parameterising the input structures
            - Solvating the input structures
            - Minimising the input structures
            - Heating the input structures
            - Running pre-equilibration simulations (and extracting the 
              restraints for the bound leg)
            - Creating the Stage objects
        
        Parameters
        ----------
        use_same_restraints: bool, default=False
            If True, the same restraints will be used for all of the bound leg repeats - by default
            , the restraints generated for the first repeat are used. This allows meaningful
            comparison between repeats for the bound leg. If False, the unique restraints are
            generated for each repeat.
        """
        self._logger.info("Setting up leg...")
        # Create input directories, parameterise, solvate, minimise, heat and preequil, all
        # depending on the input files present.
        # First, create the input directories
        self.create_stage_input_dirs()
        # Then load in the input files
        if self.prep_stage == PreparationStage.STRUCTURES_ONLY:
            system = self.parameterise_input()
        else:
            system = _BSS.IO.readMolecules([f"{self.input_dir}/{file}" for file in self.prep_stage.get_simulation_input_files(self.leg_type)])
        # Now, process the input files depending on the preparation stage
        if self.prep_stage == PreparationStage.PARAMETERISED:
            system = self.solvate_input(system) # This also adds ions
        if self.prep_stage == PreparationStage.SOLVATED:
            _check_has_wat_and_box(system)
            system = self.minimise_input(system)
        if self.prep_stage == PreparationStage.MINIMISED:
            _check_has_wat_and_box(system)
            system = self.heat_and_preequil_input(system)
        if self.prep_stage == PreparationStage.PREEQUILIBRATED:
            # Run separate equilibration simulations for each of the repeats and 
            # extract the final structures to give a diverse ensemble of starting
            # conformations. For the bound leg, this also extracts the restraints.
            _check_has_wat_and_box(system)
            self.run_ensemble_equilibration(system)

        # Write input files
        self.write_input_files(system, use_same_restraints=use_same_restraints)

        # Make sure the stored restraints reflect the restraints used. TODO:
        # make this more robust my using the SOMD functionality to extract 
        # results from the simfiles
        if self.leg_type == LegType.BOUND and use_same_restraints:
            # Use the first restraints
            first_restr = self.restraints[0]
            self.restraints = [first_restr for _ in range(self.ensemble_size)]

        # Create the Stage objects, which automatically set themselves up
        self.stages = []
        for stage_type in self.required_stages[self.leg_type]:
            self.stages.append(_Stage(stage_type=stage_type,
                                      block_size=self.block_size,
                                      equil_detection=self.equil_detection,
                                      gradient_threshold=self.gradient_threshold,
                                      ensemble_size=self.ensemble_size,
                                      lambda_values=Leg.default_lambda_values[self.leg_type][stage_type],
                                      base_dir=self.stage_input_dirs[stage_type].replace("/input", ""),
                                      input_dir=self.stage_input_dirs[stage_type],
                                      output_dir=self.stage_input_dirs[stage_type].replace("input", "output"),
                                      stream_log_level=self.stream_log_level))

        self._logger.info("Setup complete.")
        # Save state
        self._dump()


    def get_optimal_lam_vals(self, simtime:_Optional[float] = 0.1, delta_sem: float = 0.1) -> None:
        """
        Determine the optimal lambda windows for each stage of the leg
        by running short simulations at each lambda value and analysing them.

        Parameters
        ----------
        simtime : float, Optional, default: 0.1
            The length of the short simulations to run, in ns. If None is provided,
            it is assumed that the simulations have already been run and the
            optimal lambda values are extracted from the output files.
        delta_sem : float, default: 0.1
            The desired integrated standard error of the mean of the gradients
            between each lambda value, in kcal mol-1.        
        
        Returns
        -------
        None
        """
        # Check that the leg has been set up
        if not hasattr(self, "stages"):
            raise ValueError("The leg has not been set up yet. Please call setup() first.")

        # If simtime is not None, run short simulations
        if simtime is not None:
            self._logger.info(f"Running short simulations for {simtime} ns to determine optimal lambda windows...")
            self.run(adaptive=False, runtime=simtime)
            self.wait()
        else:
            self._logger.info("Simulation time is not 0 - assuming that short simulations have already been run and" 
                              " extracting optimal lambda values from output files...")

        # Now extract the optimal lambda values
        self._logger.info(f"Determining optimal lambda windows for each stage...")
        for stage in self.stages:
            self._logger.info(f"Determining optimal lambda windows for {stage}...")
            optimal_lam_vals = stage.get_optimal_lam_vals(delta_sem=delta_sem)
            # Create new LamWindow objects with the optimal lambda values, then save data
            stage.lam_vals = optimal_lam_vals 
            stage.update(save_name="lam_val_determination") # This deletes all of the old LamWindow objects and creates a new output dir

        # Save state
        self._dump()


    def create_stage_input_dirs(self) -> _Dict[_StageType, str]:
        """
        Create the input directories for each stage.
        
        Returns
        -------
        stage_input_dirs : Dict[StageType, str]
            Dictionary mapping each stage type to the path to its input directory.
        """
        self._logger.info("Creating stage input directories...")
        stage_input_dirs = {}
        for stage_type in self.stage_types:
            input_dir = f"{self.base_dir}/{stage_type.name.lower()}/input"
            _pathlib.Path(input_dir).mkdir(parents=True, exist_ok=True)
            stage_input_dirs[stage_type] = input_dir

        self.stage_input_dirs = stage_input_dirs

        return stage_input_dirs

    def parameterise_input(self) -> _BSS._SireWrappers._system.System: # type: ignore
        """
        Paramaterise the input structure, using Open Force Field v.2.0 'Sage'
        for the ligand, AMBER ff14SB for the protein, and TIP3P for the water.
        The resulting system is saved to the input directory.
        
        Returns
        -------
        parameterised_system : _BSS._SireWrappers._system.System
            Parameterised system.
        """
        FORCEFIELDS = {"ligand": "openff_unconstrained-2.0.0", 
                       "protein": "ff14SB", 
                       "water": "tip3p"}

        self._logger.info("Parameterising input...")
        # Parameterise the ligand
        self._logger.info("Parameterising ligand...")
        lig_sys = _BSS.IO.readMolecules(f"{self.input_dir}/ligand.sdf")
        # Ensure that the ligand is named "LIG"
        _rename_lig(lig_sys, "LIG")
        param_lig = _BSS.Parameters.parameterise(molecule=lig_sys[0], forcefield=FORCEFIELDS["ligand"]).getMolecule()

        # If bound, then parameterise the protein and waters and add to the system
        if self.leg_type == LegType.BOUND:
            # Parameterise the protein
            self._logger.info("Parameterising protein...")
            protein = _BSS.IO.readMolecules(f"{self.input_dir}/protein.pdb")[0]
            param_protein = _BSS.Parameters.parameterise(molecule=protein, 
                                                         forcefield=FORCEFIELDS["protein"]).getMolecule()

            # Parameterise the waters, if they are supplied
            # Check that waters are supplied
            param_waters = []
            if _pathlib.Path(f"{self.input_dir}/waters.pdb").exists():
                self._logger.info("Crystallographic waters detected. Parameterising...")
                waters = _BSS.IO.readMolecules(f"{self.input_dir}/waters.pdb")
                for water in waters:
                    param_waters.append(_BSS.Parameters.parameterise(molecule=water, 
                                                                    water_model=FORCEFIELDS["water"],
                                                                    forcefield=FORCEFIELDS["protein"]).getMolecule())

            # Create the system
            self._logger.info("Assembling parameterised system...")
            parameterised_system = param_lig + param_protein
            for water in param_waters:
                parameterised_system += water

        # This is the free leg, so just turn the ligand into a system
        else:
            parameterised_system = param_lig.toSystem()

        # Set the parameterisation stage
        self.prep_stage = PreparationStage.PARAMETERISED
        # Save the system
        self._logger.info("Saving parameterised system...")
        _BSS.IO.saveMolecules(f"{self.base_dir}/input/{self.leg_type.name.lower()}{self.prep_stage.file_suffix}",
                               parameterised_system, 
                               fileformat=["prm7", "rst7"])

        return parameterised_system

    def solvate_input(self, parameterised_system: _BSS._SireWrappers._system.System) -> _BSS._SireWrappers._system.System: # type: ignore
        """
        Determine an appropriate (rhombic dodecahedron) 
        box size, then solvate the input structure using
        TIP3P water, adding 150 mM NaCl to the system. 
        The resulting system is saved to the input directory.
        
        Parameters
        ----------
        parameterised_system : _BSS._SireWrappers._system.System
            Parameterised system.
        
        Returns
        -------
        solvated_system : _BSS._SireWrappers._system.System
            Solvated system.
        """
        WATER_MODEL = "tip3p"
        ION_CONC = 0.15 # M

        # Determine the box size
        # Taken from https://github.com/michellab/BioSimSpaceTutorials/blob/main/01_introduction/02_molecular_setup.ipynb
        # Get the minimium and maximum coordinates of the bounding box that
        # minimally encloses the protein.
        self._logger.info("Determining optimal rhombic dodecahedral box...")
        # Want to get box size based on complex/ ligand, exlcuding any crystallographic waters
        non_waters = [mol for mol in parameterised_system if mol.nAtoms() != 3]
        dry_system = _BSS._SireWrappers._system.System(non_waters) # type: ignore
        box_min, box_max = dry_system.getAxisAlignedBoundingBox()

        # Work out the box size from the difference in the coordinates.
        box_size = [y - x for x, y in zip(box_min, box_max)]

        # Add 15 A padding to the box size in each dimension.
        padding = 15 * _BSS.Units.Length.angstrom

        # Work out an appropriate box. This will used in each dimension to ensure
        # that the cutoff constraints are satisfied if the molecule rotates.
        box_length = max(box_size) + 2*padding
        box, angles = _BSS.Box.rhombicDodecahedronHexagon(box_length)

        self._logger.info(f"Solvating system with {WATER_MODEL} water and {ION_CONC} M NaCl...")
        solvated_system = _BSS.Solvent.solvate(model=WATER_MODEL,
                                               molecule=parameterised_system,
                                               box=box, 
                                               angles=angles, 
                                               ion_conc=ION_CONC) 

        # Set the preparation stage
        self.prep_stage = PreparationStage.SOLVATED

        # Save the system
        self._logger.info("Saving solvated system")
        _BSS.IO.saveMolecules(f"{self.base_dir}/input/{self.leg_type.name.lower()}{self.prep_stage.file_suffix}",
                               solvated_system, 
                               fileformat=["prm7", "rst7"])

        return solvated_system

    def minimise_input(self, solvated_system: _BSS._SireWrappers._system.System) -> _BSS._SireWrappers._system.System: # type: ignore
        """
        Minimise the input structure with GROMACS. The resulting system is saved to the input directory.
        
        Parameters
        ----------
        solvated_system : _BSS._SireWrappers._system.System
            Solvated system.
        
        Returns
        -------
        minimised_system : _BSS._SireWrappers._system.System
            Minimised system.
        """
        STEPS = 1000 # This is the default for _BSS
        self._logger.info(f"Minimising input structure with {STEPS} steps...")
        protocol = _BSS.Protocol.Minimisation(steps=STEPS)
        minimised_system = self._run_process(solvated_system, protocol, prep_stage=PreparationStage.MINIMISED)
        return minimised_system

    def heat_and_preequil_input(self, minimised_system: _BSS._SireWrappers._system.System) -> _BSS._SireWrappers._system.System: # type: ignore
        """ 
        Heat the input structure from 0 to 298.15 K with GROMACS. The resulting system is saved to the input directory.
        
        Parameters
        ----------
        minimised_system : _BSS._SireWrappers._system.System
            Minimised system.
        
        Returns
        -------
        preequilibrated_system : _BSS._SireWrappers._system.System
            Pre-Equilibrated system.
        """
        RUNTIME_SHORT_NVT = 5 # ps
        RUNTIME_NVT = 50 # ps 
        END_TEMP = 298.15 # K
        RUNTIME_NPT = 400 # ps
        RUNTIME_NPT_UNRESTRAINED = 1000 # ps

        self._logger.info(f"NVT equilibration for {RUNTIME_SHORT_NVT} ps while restraining all non-solvent atoms")
        protocol = _BSS.Protocol.Equilibration(
                                        runtime=RUNTIME_SHORT_NVT*_BSS.Units.Time.picosecond, 
                                        temperature_start=0*_BSS.Units.Temperature.kelvin, 
                                        temperature_end=END_TEMP*_BSS.Units.Temperature.kelvin,
                                        restraint="all"
                                        )
        equil1 = self._run_process(minimised_system, protocol)

        # If this is the bound leg, carry out step with backbone restraints
        if self.leg_type == LegType.BOUND:
            self._logger.info(f"NVT equilibration for {RUNTIME_NVT} ps while restraining all backbone atoms")
            protocol = _BSS.Protocol.Equilibration(
                                            runtime=RUNTIME_NVT*_BSS.Units.Time.picosecond, 
                                            temperature=END_TEMP*_BSS.Units.Temperature.kelvin, 
                                            restraint="backbone"
                                            )
            equil2 = self._run_process(equil1, protocol)

        else: # Free leg - skip the backbone restraint step
            equil2 = equil1

        self._logger.info(f"NVT equilibration for {RUNTIME_NVT} ps without restraints")
        protocol = _BSS.Protocol.Equilibration(
                                        runtime=RUNTIME_NVT*_BSS.Units.Time.picosecond, 
                                        temperature=END_TEMP*_BSS.Units.Temperature.kelvin,
                                        )
        equil3 = self._run_process(equil2, protocol)

        self._logger.info(f"NPT equilibration for {RUNTIME_NPT} ps while restraining non-solvent heavy atoms")
        protocol = _BSS.Protocol.Equilibration(
                                        runtime=RUNTIME_NPT*_BSS.Units.Time.picosecond, 
                                        pressure=1*_BSS.Units.Pressure.atm,
                                        temperature=END_TEMP*_BSS.Units.Temperature.kelvin,
                                        restraint="heavy",
                                        )
        equil4 = self._run_process(equil3, protocol)

        self._logger.info(f"NPT equilibration for {RUNTIME_NPT_UNRESTRAINED} ps without restraints")
        protocol = _BSS.Protocol.Equilibration(
                                        runtime=RUNTIME_NPT_UNRESTRAINED*_BSS.Units.Time.picosecond, 
                                        pressure=1*_BSS.Units.Pressure.atm,
                                        temperature=END_TEMP*_BSS.Units.Temperature.kelvin,
                                        )
        preequilibrated_system = self._run_process(equil4, protocol, prep_stage=PreparationStage.PREEQUILIBRATED)

        return preequilibrated_system

    def _run_process(self, system: _BSS._SireWrappers._system.System,
                     protocol: _BSS.Protocol._protocol.Protocol,
                     prep_stage: _Optional[PreparationStage] = None) -> _BSS._SireWrappers._system.System:
        """
        Run a process with GROMACS.
        
        Parameters
        ----------
        system : _BSS._SireWrappers._system.System
            System to run the process on.
        protocol : _BSS._Protocol._protocol.Protocol
            Protocol to run the process with.
        prep_stage : _Optional[PreparationStage]
            Preparation stage that the leg will be in if the process 
            completes successfully. If this is supplied, the leg's
            preparation stage will be updated and the files saved
            upon completion of the process.
        
        Returns
        -------
        system : _BSS._SireWrappers._system.System
            System after the process has been run.
        """
        process = _BSS.Process.Gromacs(system, protocol)
        process.start()
        process.wait()
        import time
        time.sleep(10)
        if process.isError():
            self._logger.error(process.stdout())
            self._logger.error(process.stderr())
            raise _BSS._Exceptions.ThirdPartyError("The process failed.")
        system = process.getSystem(block=True)
        if system is None:
            self._logger.error(process.stdout())
            self._logger.error(process.stderr())
            raise _BSS._Exceptions.ThirdPartyError("The final system is None.")
        # Save the system if a suffix is supplied
        if prep_stage is not None:
            # Update the leg's preparation stage
            self.prep_stage = prep_stage
            # Save the files
            file_name = f"{self.leg_type.name.lower()}{prep_stage.file_suffix}"
            self._logger.info(f"Saving {file_name} PRM7 and RST7 files to {self.base_dir}/input")
            # Save, renaming the velocity property to foo so avoid saving velocities. Saving the
            # velocities sometimes causes issues with the size of the floats overflowing the RST7
            # format.
            _BSS.IO.saveMolecules(f"{self.base_dir}/input/{file_name}",
                                system, fileformat=["prm7", "rst7"], property_map={"velocity" : "foo"})
        return system

    def run_ensemble_equilibration(self, pre_equilibrated_system: _BSS._SireWrappers._system.System) -> None:
        """
        Run 5 ns simulations with SOMD for each of the ensemble_size runs and extract the final structures
        to use as diverse starting points for the production runs. If this is the bound leg, the restraints
        will also be extracted from the simulations and saved to a file. The simulations will be run in a 
        subdirectory of the stage base directory called ensemble_equilibration, and the restraints and
        final coordinates will be saved here.
        
        Parameters
        ----------
        pre_equilibrated_system : _BSS._SireWrappers._system.System
            Pre-equilibrated system.
        
        Returns
        -------
        None
        """
        ENSEMBLE_EQUILIBRATION_TIME = 5 # ns

        # Mark the ligand to be decoupled in the absolute binding free energy calculation
        lig = _BSS.Align.decouple(pre_equilibrated_system[0], intramol=True)
        # Check that is actually a ligand
        if lig.nAtoms() > 100 or lig.nAtoms() < 5:
            raise ValueError(f"The first molecule in the bound system has {lig.nAtoms()} atoms and is likely not a ligand. " \
                             "Please check that the ligand is the first molecule in the bound system.")
        # Check that the name is correct
        if lig._sire_object.name().value() != "LIG":
            raise ValueError(f"The name of the ligand in the bound system is {lig._sire_object.name().value()} and is not LIG. " \
                             "Please check that the ligand is the first molecule in the bound system or rename the ligand.")
        self._logger.info(f"Selecting ligand {lig} for decoupling")
        pre_equilibrated_system.updateMolecule(0,lig)
        
        # Create the protocol
        protocol = _BSS.Protocol.Production(timestep=2*_BSS.Units.Time.femtosecond, # 2 fs timestep as 4 fs seems to cause instability even with HMR
                                             runtime=ENSEMBLE_EQUILIBRATION_TIME*_BSS.Units.Time.nanosecond)

        self._logger.info(f"Running {self.ensemble_size} SOMD ensemble equilibration simulations for {ENSEMBLE_EQUILIBRATION_TIME} ns")
        # Repeat this for each of the ensemble_size repeats
        for i in range(self.ensemble_size):
            equil_output_dir = f"{self.base_dir}/ensemble_equilibration_{i+1}"
            if self.leg_type == LegType.BOUND:
                self._logger.info(f"Running SOMD restraint search simulation {i+1} of {self.ensemble_size}")
                restraint_search = _BSS.FreeEnergy.RestraintSearch(pre_equilibrated_system, protocol=protocol,
                                                                engine='Gromacs', work_dir=equil_output_dir)
                restraint_search.start()
                # After waiting for the restraint search to finish, extract the final system with new coordinates, and the restraints
                restraint_search.wait()
                
                # Check that the process completed successfully and that the final system is not None
                process = restraint_search._process
                if process.isError():
                    self._logger.error(process.stdout())
                    self._logger.error(process.stderr())
                    raise _BSS._Exceptions.ThirdPartyError("The process failed.")
                final_system = process.getSystem(block=True)
                if final_system is None:
                    self._logger.error(process.stdout())
                    self._logger.error(process.stderr())
                    raise _BSS._Exceptions.ThirdPartyError("The final system is None.")

                restraint = restraint_search.analyse(method='BSS', block=True)

                # Save the final coordinates 
                self._logger.info(f"Saving somd_{i+1}.rst7 and restraint_{i+1}.txt to {equil_output_dir}")
                # Save, renaming the velocity property to foo so avoid saving velocities. Saving the
                # velocities sometimes causes issues with the size of the floats overflowing the RST7
                # format.
                _BSS.IO.saveMolecules(f"{equil_output_dir}/somd_{i+1}", final_system, 
                                      fileformat=["rst7"], property_map={"velocity" : "foo"})

                # Save the restraints to a text file and store within the Leg object
                with open(f"{equil_output_dir}/restraint_{i+1}.txt", "w") as f:
                    f.write(restraint.toString(engine="SOMD"))
                if not hasattr(self, "restraints"):
                    self.restraints = [restraint]
                else:
                    self.restraints.append(restraint)

            elif self.leg_type == LegType.FREE:
                self._logger.info(f"Running SOMD ensemble equilibration simulation {i+1} of {self.ensemble_size}")
                process = _BSS.Process.Gromacs(pre_equilibrated_system, protocol=protocol, work_dir=equil_output_dir)
                process.start()
                process.wait()
                final_system = process.getSystem(block=True)
                if process.isError():
                    self._logger.error(process.stdout())
                    self._logger.error(process.stderr())
                    raise _BSS._Exceptions.ThirdPartyError("The process failed.")
                if final_system is None:
                    self._logger.error(process.stdout())
                    self._logger.error(process.stderr())
                    raise _BSS._Exceptions.ThirdPartyError("The final system is None.")
                # Save the final coordinates 
                self._logger.info(f"Saving somd_{i+1}.rst7 to {equil_output_dir}")
                _BSS.IO.saveMolecules(f"{equil_output_dir}/somd_{i+1}", final_system, fileformat=["rst7"], property_map={"velocity" : "foo"})


    def write_input_files(self, 
                          pre_equilibrated_system: _BSS._SireWrappers._system.System,
                          use_same_restraints: bool = False) -> None:
        """
        Write the required input files to all of the stage input directories.

        Parameters
        ----------
        pre_equilibrated_system: _BSS._SireWrappers._system.System
            The equilibrated system to run further equilinration on. The final coordinates
            are then used as input for each of the individual runs.
        use_same_restraints: bool, default=False
            If True, the same restraints will be used for all of the bound leg repeats - by default
            , the restraints generated for the first repeat are used. This allows meaningful
            comparison between repeats for the bound leg. If False, the unique restraints are
            generated for each repeat.
        """
        # Dummy values get overwritten later
        DUMMY_RUNTIME = 0.001 # ns
        DUMMY_LAM_VALS = [0.0]
        if not hasattr(self, "stage_input_dirs"):
            raise AttributeError("No stage input directories have been set.")

        for stage_type, stage_input_dir in self.stage_input_dirs.items():
            self._logger.info(f"Writing input files for {self.leg_type.name} leg {stage_type.name} stage")
            restraint = self.restraints[0] if self.leg_type == LegType.BOUND else None
            protocol = _BSS.Protocol.FreeEnergy(runtime=DUMMY_RUNTIME*_BSS.Units.Time.nanosecond, 
                                                lam_vals=DUMMY_LAM_VALS, 
                                                perturbation_type=stage_type.bss_perturbation_type)
            self._logger.info(f"Perturbation type: {stage_type.bss_perturbation_type}")
            # Ensure we remove the velocites to avoid RST7 file writing issues, as before
            restrain_fe_calc = _BSS.FreeEnergy.Absolute(pre_equilibrated_system, 
                                                        protocol,
                                                        engine='SOMD', 
                                                        restraint=restraint,
                                                        work_dir=stage_input_dir,
                                                        setup_only=True,
                                                        property_map={"velocity" : "foo"}) # We will run outside of BSS

            # Copy input written by BSS to the stage input directory
            for file in _glob.glob(f"{stage_input_dir}/lambda_0.0000/*"):
                _shutil.copy(file, stage_input_dir)
            for file in _glob.glob(f"{stage_input_dir}/lambda_*"):
                _subprocess.run(["rm", "-rf", file], check=True)

            # Copy the run_somd.sh script to the stage input directory
            _shutil.copy(f"{self.input_dir}/run_somd.sh", stage_input_dir)

            # Copy the final coordinates from the ensemble equilibration stage to the stage input directory
            # and, if this is the bound stage, also copy over the restraints
            for i in range(self.ensemble_size):
                ens_equil_output_dir = f"{self.base_dir}/ensemble_equilibration_{i+1}"
                coordinates_file = f"{ens_equil_output_dir}/somd_{i+1}.rst7"
                _shutil.copy(coordinates_file, f"{stage_input_dir}/somd_{i+1}.rst7")
                if self.leg_type == LegType.BOUND:
                    if use_same_restraints: # Want to use same restraints for all repeats
                        restraint_file = f"{ens_equil_output_dir}/restraint_1.txt"
                    else:
                        restraint_file = f"{ens_equil_output_dir}/restraint_{i+1}.txt"
                    _shutil.copy(restraint_file, f"{stage_input_dir}/restraint_{i+1}.txt")

            # Update the template-config.cfg file with the perturbed residue number generated
            # by BSS, as well as the restraints options
            _shutil.copy(f"{self.input_dir}/template_config.cfg", stage_input_dir)
            
            # Read simfile options
            perturbed_resnum = _read_simfile_option(f"{stage_input_dir}/somd.cfg", "perturbed residue number")
            # Temporary fix for BSS bug - perturbed residue number is wrong, but since we always add the 
            # ligand first to the system, this should always be 1 anyway
            # TODO: Fix this - raise BSS issue
            perturbed_resnum = "1"
            try:
                use_boresch_restraints = _read_simfile_option(f"{stage_input_dir}/somd.cfg", "use boresch restraints")
            except ValueError:
                use_boresch_restraints = False
            try:
                turn_on_receptor_ligand_restraints_mode = _read_simfile_option(f"{stage_input_dir}/somd.cfg", "turn on receptor-ligand restraints mode")
            except ValueError:
                turn_on_receptor_ligand_restraints_mode = False
            
            # Now write simfile options
            _write_simfile_option(f"{stage_input_dir}/template_config.cfg", "perturbed residue number", perturbed_resnum)
            _write_simfile_option(f"{stage_input_dir}/template_config.cfg", "use boresch restraints", str(use_boresch_restraints))
            _write_simfile_option(f"{stage_input_dir}/template_config.cfg", "turn on receptor-ligand restraints mode", str(turn_on_receptor_ligand_restraints_mode))

            # Now overwrite the SOMD generated config file with the updated template
            _subprocess.run(["mv", f"{stage_input_dir}/template_config.cfg", f"{stage_input_dir}/somd.cfg"], check=True)

            # Set the default lambda windows based on the leg and stage types
            lam_vals = Leg.default_lambda_values[self.leg_type][stage_type]
            lam_vals_str = ", ".join([str(lam_val) for lam_val in lam_vals])
            _write_simfile_option(f"{stage_input_dir}/somd.cfg", "lambda array", lam_vals_str)

    def analyse(self, subsampling=False) -> _Tuple[_np.ndarray, _np.ndarray]:
        f"""
        Analyse the leg and any sub-simulations, and 
        return the overall free energy change.

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
        dg_overall, er_overall = super().analyse(subsampling=subsampling)

        if self.leg_type == LegType.BOUND:
            # We need to add on the restraint corrections. There are no errors associated with these.
            rest_corrs = _np.array([self.restraints[i].getCorrection().value() for i in range(self.ensemble_size)])
            self._logger.info(f"Restraint corrections: {rest_corrs}")
            dg_overall += rest_corrs

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

        if self.leg_type == LegType.BOUND:
            # We need to add on the restraint corrections. There are no errors associated with these.
            rest_corrs = _np.array([self.restraints[i].getCorrection().value() for i in range(self.ensemble_size)])
            self._logger.info(f"Correcting convergence plots with restraint corrections: {rest_corrs}")
            # Make sure the shape is correct
            rest_corrs = [rest_corr * _np.ones(len(fracts)) for rest_corr in rest_corrs]
            dg_overall += rest_corrs

        self._logger.info(f"Overall free energy changes: {dg_overall} kcal mol-1")
        self._logger.info(f"Fractions of equilibrated simulation time: {fracts}")

        # Plot the overall convergence
        _plot_convergence(fracts, dg_overall, self.tot_simtime, self.equil_time, self.output_dir, self.ensemble_size)

        return fracts, dg_overall
