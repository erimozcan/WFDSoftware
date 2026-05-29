#!/usr/bin/env python3
"""Unit tests for live OpenFOAM progress parsing."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from parse_forces import read_force_rows  # noqa: E402
from solver_progress import force_convergence_metrics, parse_force_timeseries, parse_latest_forces, parse_solver_log_text, parse_solver_log_timeseries  # noqa: E402


class SolverProgressParserTests(unittest.TestCase):
    def test_solver_log_progress_lines(self) -> None:
        parsed = parse_solver_log_text(
            """
Time = 0.0002
deltaT = 1e-07
Courant Number mean: 0.12 max: 0.44
ExecutionTime = 12.4 s  ClockTime = 13 s
Time = 0.0003
deltaT = 2e-07
Courant Number mean: 0.13 max: 0.45
ExecutionTime = 15.4 s  ClockTime = 16 s
"""
        )
        self.assertEqual(parsed["current_time"], 0.0003)
        self.assertEqual(parsed["latest_deltaT"], 2e-07)
        self.assertEqual(parsed["courant_mean"], 0.13)
        self.assertEqual(parsed["courant_max"], 0.45)
        self.assertEqual(parsed["execution_time_s"], 15.4)
        self.assertEqual(parsed["clock_time_s"], 16.0)
        self.assertFalse(parsed["fatal_error"])

    def test_solver_log_fatal_io_error(self) -> None:
        parsed = parse_solver_log_text(
            """
Time = 0.0001
FOAM FATAL IO ERROR:
keyword rhoFinal is undefined in dictionary system/fvSolution/solvers
"""
        )
        self.assertTrue(parsed["fatal_error"])
        self.assertIn("FOAM FATAL IO ERROR", parsed["fatal_excerpt"])
        self.assertEqual(parsed["fatal_rule"], "FOAM_FATAL_IO_ERROR")

    def test_sigfpe_startup_line_is_not_fatal(self) -> None:
        parsed = parse_solver_log_text(
            """
sigFpe : Enabling floating point exception trapping (FOAM_SIGFPE).
fileModificationChecking : Monitoring run-time modified files using timeStampMaster
allowSystemOperations : Allowing user-supplied system call operations
Time = 0.0001
"""
        )
        self.assertFalse(parsed["fatal_error"])
        self.assertEqual(len(parsed["ignored_log_warnings"]), 3)

    def test_solver_log_fatal_error(self) -> None:
        parsed = parse_solver_log_text("FOAM FATAL ERROR:\nDivergence detected\n")
        self.assertTrue(parsed["fatal_error"])
        self.assertEqual(parsed["fatal_rule"], "FOAM_FATAL_ERROR")

    def test_solver_log_segmentation_fault(self) -> None:
        parsed = parse_solver_log_text("Segmentation fault (core dumped)\n")
        self.assertTrue(parsed["fatal_error"])
        self.assertEqual(parsed["fatal_rule"], "SEGMENTATION_FAULT")

    def test_solver_log_floating_point_exception_crash(self) -> None:
        parsed = parse_solver_log_text("Floating point exception (core dumped)\n")
        self.assertTrue(parsed["fatal_error"])
        self.assertEqual(parsed["fatal_rule"], "CORE_DUMPED")

    def test_labeled_force_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            force_file = Path(tmp) / "forces.dat"
            force_file.write_text(
                """
time = 0.000822935
pressure force = (-0.111430952, 1.95547669, 0.809938024)
viscous force  = (-0.000255920, 0.009809391, 0.017045173)
pressure moment = (0.00749307236, -0.0309406761, -0.0240479138)
viscous moment  = (0.0000101751412, 0.000497419545, -0.000912413938)
"""
            )
            rows = read_force_rows(force_file)
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["force"][2], 0.826983197)
        self.assertAlmostEqual(rows[0]["moment"][2], -0.024960327738)

    def test_latest_force_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            case_dir = Path(tmp)
            force_dir = case_dir / "postProcessing" / "propellerForces" / "0"
            force_dir.mkdir(parents=True)
            (force_dir / "forces.dat").write_text(
                "0.1 ((1 2 3) (0.1 0.2 0.3)) ((0.01 0.02 -0.5) (0.001 0.002 -0.05))\n"
            )
            parsed = parse_latest_forces(
                case_dir,
                {
                    "thrust_axis": "z",
                    "thrust_sign": -1,
                    "torque_axis": "z",
                    "shaft_torque_sign": -1,
                },
            )
        self.assertEqual(parsed["force_sample_count"], 1)
        self.assertAlmostEqual(parsed["latest_thrust_N"], -3.3)
        self.assertAlmostEqual(parsed["latest_fluid_torque_Nm"], -0.55)
        self.assertAlmostEqual(parsed["latest_shaft_torque_Nm"], 0.55)

    def test_compact_force_timeseries_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            case_dir = Path(tmp)
            force_dir = case_dir / "postProcessing" / "propellerForces" / "0"
            force_dir.mkdir(parents=True)
            (force_dir / "forces.dat").write_text(
                "0.25 ((-0.1 2.0 0.8) (-0.01 0.02 0.03)) ((0.01 -0.02 -0.2) (0.001 0.002 -0.03))\n"
            )
            samples, status = parse_force_timeseries(
                case_dir,
                {
                    "thrust_axis": "z",
                    "thrust_sign": -1,
                    "torque_axis": "z",
                    "shaft_torque_sign": -1,
                    "omega_rad_s": 100.0,
                    "Vinf_mps": 44.704,
                },
            )
        self.assertEqual(status["status"], "available")
        self.assertEqual(len(samples), 1)
        self.assertAlmostEqual(samples[0]["Fz"], 0.83)
        self.assertAlmostEqual(samples[0]["Mz"], -0.23)
        self.assertAlmostEqual(samples[0]["thrust_N"], -0.83)
        self.assertAlmostEqual(samples[0]["shaft_torque_Nm"], 0.23)
        self.assertAlmostEqual(samples[0]["power_W"], 23.0)

    def test_default_positive_z_thrust_sign(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            case_dir = Path(tmp)
            force_dir = case_dir / "postProcessing" / "propellerForces" / "0"
            force_dir.mkdir(parents=True)
            force_dir.joinpath("forces.dat").write_text(
                "0.5 ((0.30364 0.33469 23.8019) (0 0 0)) ((-0.00408616 0.000727746 0.645793) (0 0 0))\n"
            )
            samples, status = parse_force_timeseries(
                case_dir,
                {
                    "thrust_axis": "z",
                    "torque_axis": "z",
                    "omega_rad_s": 100.0,
                },
            )
        self.assertEqual(status["status"], "available")
        self.assertEqual(len(samples), 1)
        self.assertAlmostEqual(samples[0]["raw_force_axis_N"], 23.8019)
        self.assertEqual(samples[0]["thrust_sign"], 1.0)
        self.assertAlmostEqual(samples[0]["thrust_N"], 23.8019)

    def test_residual_timeseries_lines(self) -> None:
        parsed = parse_solver_log_timeseries(
            """
Time = 0.0001
smoothSolver:  Solving for Ux, Initial residual = 0.1, Final residual = 1e-05, No Iterations 2
smoothSolver:  Solving for Uy, Initial residual = 0.2, Final residual = 2e-05, No Iterations 2
GAMG:  Solving for p, Initial residual = 0.3, Final residual = 3e-05, No Iterations 5
Time = 0.0002
smoothSolver:  Solving for omega, Initial residual = 0.4, Final residual = 4e-05, No Iterations 1
"""
        )
        self.assertEqual(len(parsed["residuals"]), 4)
        self.assertEqual(parsed["residuals"][0]["field"], "Ux")
        self.assertEqual(parsed["residuals"][0]["time"], 0.0001)
        self.assertEqual(parsed["residuals"][2]["field"], "p")
        self.assertEqual(parsed["residuals"][3]["timestep"], 2)

    def test_force_convergence_metrics(self) -> None:
        samples = []
        for index in range(500):
            samples.append({"thrust_N": 24.1, "shaft_torque_Nm": -0.654})
        for index in range(500):
            samples.append({"thrust_N": 23.94 + (0.01 if index % 2 else -0.01), "shaft_torque_Nm": -0.650 + (0.0005 if index % 2 else -0.0005)})
        metrics = force_convergence_metrics(samples, window=500, minimum_samples=1000)
        self.assertTrue(metrics["converged"])
        self.assertEqual(metrics["convergence_status"], "converged")

    def test_force_convergence_requires_minimum_samples(self) -> None:
        samples = [{"thrust_N": 1.0, "shaft_torque_Nm": 1.0} for _ in range(999)]
        metrics = force_convergence_metrics(samples, window=500, minimum_samples=1000)
        self.assertFalse(metrics["converged"])
        self.assertEqual(metrics["convergence_status"], "warming up")
        self.assertIn("insufficient samples", metrics["blocked_by"])

    def test_force_convergence_blocks_on_torque_change(self) -> None:
        samples = []
        for _index in range(500):
            samples.append({"thrust_N": 24.0, "shaft_torque_Nm": 0.60})
        for index in range(500):
            samples.append({"thrust_N": 24.1 + (0.01 if index % 2 else -0.01), "shaft_torque_Nm": 0.66 + (0.001 if index % 2 else -0.001)})
        metrics = force_convergence_metrics(
            samples,
            window=500,
            minimum_samples=1000,
            thrust_mean_change_threshold_pct=3.0,
            torque_mean_change_threshold_pct=5.0,
            thrust_cv_threshold_pct=5.0,
            torque_cv_threshold_pct=5.0,
        )
        self.assertFalse(metrics["converged"])
        self.assertIn("torque mean change", metrics["blocked_by"])
        self.assertNotIn("thrust mean change", metrics["blocked_by"])


if __name__ == "__main__":
    unittest.main()
