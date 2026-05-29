#!/usr/bin/env python3
"""Parse OpenFOAM forces output and compute first-pass propeller metrics."""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
from pathlib import Path


NUMBER_RE = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?")
VECTOR_RE = re.compile(r"\(([^()]+)\)")
AXIS_INDEX = {"x": 0, "y": 1, "z": 2}


class ForceParseError(Exception):
    """Raised when force output cannot be parsed."""


def numeric_time(path: Path) -> float:
    try:
        return float(path.name)
    except ValueError:
        return -math.inf


def find_latest_force_file(case_path: Path) -> Path:
    post_dir = case_path / "postProcessing"
    if not post_dir.is_dir():
        raise ForceParseError(f"No postProcessing directory found: {post_dir}")

    candidates = []
    for path in post_dir.rglob("*"):
        if not path.is_file():
            continue
        name = path.name.lower()
        if name.startswith("forcecoeff"):
            continue
        if name.startswith("forces") or "forces" in str(path.parent).lower():
            candidates.append(path)
    if not candidates:
        raise ForceParseError(f"No force output files found under: {post_dir}")

    def key(path: Path) -> tuple[float, float]:
        time_dirs = [parent for parent in path.parents if parent.parent != parent]
        best_time = max((numeric_time(parent) for parent in time_dirs), default=-math.inf)
        return best_time, path.stat().st_mtime

    return max(candidates, key=key)


def parse_vector(text: str) -> tuple[float, float, float]:
    values = [float(part) for part in NUMBER_RE.findall(text)]
    if len(values) != 3:
        raise ForceParseError(f"Expected 3 vector components, got: {text}")
    return values[0], values[1], values[2]


def add_vectors(vectors: list[tuple[float, float, float]]) -> tuple[float, float, float]:
    return (
        sum(vector[0] for vector in vectors),
        sum(vector[1] for vector in vectors),
        sum(vector[2] for vector in vectors),
    )


def read_force_rows(force_file: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    pending: dict[str, object] = {}

    with force_file.open() as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith("time"):
                if {"time", "pressure_force", "viscous_force", "pressure_moment", "viscous_moment"}.issubset(pending):
                    pressure_force = pending["pressure_force"]  # type: ignore[assignment]
                    viscous_force = pending["viscous_force"]  # type: ignore[assignment]
                    pressure_moment = pending["pressure_moment"]  # type: ignore[assignment]
                    viscous_moment = pending["viscous_moment"]  # type: ignore[assignment]
                    rows.append(
                        {
                            "time": pending["time"],
                            "pressure_force": pressure_force,
                            "viscous_force": viscous_force,
                            "pressure_moment": pressure_moment,
                            "viscous_moment": viscous_moment,
                            "force": add_vectors([pressure_force, viscous_force]),  # type: ignore[list-item]
                            "moment": add_vectors([pressure_moment, viscous_moment]),  # type: ignore[list-item]
                        }
                    )
                values = NUMBER_RE.findall(stripped)
                pending = {"time": float(values[0])} if values else {}
                continue
            label_match = re.match(r"^(pressure force|viscous force|pressure moment|viscous moment)\s*=\s*(\(.*\))", stripped)
            if label_match and pending:
                key = label_match.group(1).replace(" ", "_")
                pending[key] = parse_vector(label_match.group(2))
                continue

            parts = stripped.split(maxsplit=1)
            if len(parts) != 2:
                continue

            try:
                time = float(parts[0])
            except ValueError:
                continue

            vectors = [parse_vector(match) for match in VECTOR_RE.findall(parts[1])]
            if len(vectors) < 2:
                continue

            if len(vectors) >= 6:
                pressure_force = add_vectors(vectors[0:2])
                viscous_force = vectors[2]
                pressure_moment = add_vectors(vectors[3:5])
                viscous_moment = vectors[5]
            elif len(vectors) == 4:
                pressure_force = vectors[0]
                viscous_force = vectors[1]
                pressure_moment = vectors[2]
                viscous_moment = vectors[3]
            else:
                pressure_force = vectors[0]
                viscous_force = (0.0, 0.0, 0.0)
                pressure_moment = vectors[1]
                viscous_moment = (0.0, 0.0, 0.0)

            rows.append(
                {
                    "time": time,
                    "pressure_force": pressure_force,
                    "viscous_force": viscous_force,
                    "pressure_moment": pressure_moment,
                    "viscous_moment": viscous_moment,
                    "force": add_vectors([pressure_force, viscous_force]),
                    "moment": add_vectors([pressure_moment, viscous_moment]),
                }
            )

    if {"time", "pressure_force", "viscous_force", "pressure_moment", "viscous_moment"}.issubset(pending):
        pressure_force = pending["pressure_force"]  # type: ignore[assignment]
        viscous_force = pending["viscous_force"]  # type: ignore[assignment]
        pressure_moment = pending["pressure_moment"]  # type: ignore[assignment]
        viscous_moment = pending["viscous_moment"]  # type: ignore[assignment]
        rows.append(
            {
                "time": pending["time"],
                "pressure_force": pressure_force,
                "viscous_force": viscous_force,
                "pressure_moment": pressure_moment,
                "viscous_moment": viscous_moment,
                "force": add_vectors([pressure_force, viscous_force]),  # type: ignore[list-item]
                "moment": add_vectors([pressure_moment, viscous_moment]),  # type: ignore[list-item]
            }
        )

    if not rows:
        raise ForceParseError(f"No parseable force rows found in: {force_file}")

    return rows


def average_rows(rows: list[dict[str, object]], count: int) -> dict[str, object]:
    selected = rows[-count:]
    force = add_vectors([row["force"] for row in selected])  # type: ignore[list-item]
    moment = add_vectors([row["moment"] for row in selected])  # type: ignore[list-item]
    n = len(selected)
    return {
        "time": selected[-1]["time"],
        "samples": n,
        "force": (force[0] / n, force[1] / n, force[2] / n),
        "moment": (moment[0] / n, moment[1] / n, moment[2] / n),
    }


def load_case_info(case_path: Path) -> dict[str, object]:
    info_path = case_path / "case_info.json"
    if not info_path.is_file():
        raise ForceParseError(f"Missing case metadata: {info_path}")

    with info_path.open() as handle:
        info = json.load(handle)

    return {
        "solver_mode": str(info.get("solver_mode", "incompressible_mrf")),
        "omega_rad_s": float(info["omega_rad_s"]),
        "rpm": float(info.get("rpm", 0)),
        "Vinf_mps": float(info.get("Vinf_mps", info.get("freestream_velocity_mps", 0))),
        "freestream_velocity_mps": float(info.get("Vinf_mps", info.get("freestream_velocity_mps", 0))),
        "Tinf_K": float(info.get("Tinf_K", 288.15)),
        "pinf_Pa": float(info.get("pinf_Pa", 101325)),
        "gamma": float(info.get("gamma", 1.4)),
        "R": float(info.get("R", 287)),
        "Cp": float(info.get("Cp", 1004.5)),
        "mu": float(info.get("mu", 1.7894e-5)),
        "Pr": float(info.get("Pr", 0.71)),
        "diameter_m": float(info.get("diameter_m", 0)),
        "radius_m": float(info.get("radius_m", 0)),
        "thrust_axis": str(info.get("thrust_axis", "z")),
        "thrust_sign": float(info.get("thrust_sign", 1)),
        "torque_axis": str(info.get("torque_axis", "z")),
        "torque_sign": float(info.get("torque_sign", 1)),
        "shaft_torque_sign": float(info.get("shaft_torque_sign", info.get("torque_sign", 1))),
        "torque_sign_convention": str(
            info.get(
                "torque_sign_convention",
                "shaft_torque_Nm = torque_sign * raw_moment_axis_Nm; power_W = shaft_torque_Nm * omega_rad_s",
            )
        ),
    }


RESULT_COLUMNS = [
    "variant_id",
    "solver_mode",
    "Vinf_mps",
    "Vinf_mph",
    "rpm",
    "omega_rad_s",
    "Tinf_K",
    "pinf_Pa",
    "gamma",
    "R",
    "Cp",
    "mu",
    "Pr",
    "diameter_m",
    "radius_m",
    "freestream_Mach",
    "tip_speed_mps",
    "helical_tip_Mach",
    "advance_ratio_J",
    "raw_Fx_mean",
    "raw_Fy_mean",
    "raw_Fz_mean",
    "raw_force_axis_N_mean",
    "thrust_axis",
    "thrust_sign",
    "thrust_convention_N",
    "raw_Mx_mean",
    "raw_My_mean",
    "raw_Mz_mean",
    "raw_moment_axis_Nm_mean",
    "torque_axis",
    "torque_sign",
    "torque_convention_Nm",
    "fluid_torque_Nm",
    "shaft_torque_Nm",
    "torque_sign_convention",
    "power_W",
    "eta",
    "CT",
    "CQ",
    "CP",
    "run_status",
    "warning_flags",
    "static_thrust_per_watt",
]


def update_results_csv(case_path: Path, row: dict[str, object]) -> None:
    root = Path(__file__).resolve().parents[1]
    results_path = root / "results" / "force_results.csv"
    results_path.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, str]] = []
    if results_path.is_file():
        with results_path.open(newline="") as handle:
            rows = list(csv.DictReader(handle))
    case_name = case_path.name
    rendered = {key: str(row.get(key, "")) for key in RESULT_COLUMNS}
    rendered["variant_id"] = case_name
    rows = [existing for existing in rows if existing.get("variant_id") != case_name]
    rows.append(rendered)
    rows.sort(key=lambda item: item.get("variant_id", ""))
    with results_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=RESULT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Parse propeller forces and moments")
    parser.add_argument("case_path", type=Path, help="Path to an OpenFOAM case directory")
    parser.add_argument(
        "--average-last",
        type=int,
        default=1,
        help="Average the last N force samples instead of using only the final sample",
    )
    args = parser.parse_args()

    if args.average_last < 1:
        print("ERROR: --average-last must be at least 1", file=sys.stderr)
        return 1

    case_path = args.case_path.resolve()

    try:
        force_file = find_latest_force_file(case_path)
        rows = read_force_rows(force_file)
        averaged = average_rows(rows, min(args.average_last, len(rows)))
        info = load_case_info(case_path)
    except (ForceParseError, OSError, KeyError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    force = averaged["force"]
    moment = averaged["moment"]
    omega = info["omega_rad_s"]
    freestream = info["freestream_velocity_mps"]
    rpm = float(info["rpm"])
    diameter = float(info["diameter_m"])
    radius = float(info["radius_m"])
    gamma = float(info["gamma"])
    gas_constant = float(info["R"])
    temperature = float(info["Tinf_K"])
    thrust_axis = str(info["thrust_axis"]).lower()
    torque_axis = str(info["torque_axis"]).lower()
    thrust_sign = float(info["thrust_sign"])
    torque_sign = float(info["torque_sign"])
    shaft_torque_sign = float(info["shaft_torque_sign"])
    torque_sign_convention = str(info["torque_sign_convention"])

    if thrust_axis not in AXIS_INDEX:
        print(f"ERROR: unsupported thrust_axis: {thrust_axis}", file=sys.stderr)
        return 1
    if torque_axis not in AXIS_INDEX:
        print(f"ERROR: unsupported torque_axis: {torque_axis}", file=sys.stderr)
        return 1
    if thrust_sign not in (-1.0, 1.0):
        print(f"ERROR: thrust_sign must be +1 or -1, got {thrust_sign}", file=sys.stderr)
        return 1
    if torque_sign not in (-1.0, 1.0):
        print(f"ERROR: torque_sign must be +1 or -1, got {torque_sign}", file=sys.stderr)
        return 1
    if shaft_torque_sign not in (-1.0, 1.0):
        print(f"ERROR: shaft_torque_sign must be +1 or -1, got {shaft_torque_sign}", file=sys.stderr)
        return 1

    force_x = force[0]  # type: ignore[index]
    force_y = force[1]  # type: ignore[index]
    force_z = force[2]  # type: ignore[index]
    moment_x = moment[0]  # type: ignore[index]
    moment_y = moment[1]  # type: ignore[index]
    moment_z = moment[2]  # type: ignore[index]

    force_components = (force_x, force_y, force_z)
    moment_components = (moment_x, moment_y, moment_z)

    thrust_raw = force_components[AXIS_INDEX[thrust_axis]]
    fluid_torque = moment_components[AXIS_INDEX[torque_axis]]
    thrust_convention = thrust_sign * thrust_raw
    shaft_torque = shaft_torque_sign * fluid_torque
    torque_convention = shaft_torque
    shaft_power_convention = shaft_torque * omega
    power = shaft_power_convention
    thrust_abs = abs(thrust_raw)
    torque_abs = abs(fluid_torque)
    shaft_power_abs = abs(fluid_torque * omega)
    useful_power = thrust_convention * freestream

    efficiency = ""
    if freestream > 0 and shaft_power_convention > 0:
        efficiency = useful_power / shaft_power_convention

    static_thrust_per_watt = ""
    if shaft_power_convention > 0:
        static_thrust_per_watt = thrust_convention / shaft_power_convention

    speed_of_sound = math.sqrt(gamma * gas_constant * temperature)
    freestream_mach = freestream / speed_of_sound if speed_of_sound > 0 else 0.0
    tip_speed = float(omega) * radius
    helical_tip_mach = math.hypot(freestream, tip_speed) / speed_of_sound if speed_of_sound > 0 else 0.0
    advance_ratio = ""
    if rpm > 0 and diameter > 0:
        advance_ratio = freestream / ((rpm / 60.0) * diameter)
    eta = ""
    if power > 0 and freestream > 0:
        eta = thrust_convention * freestream / power
    rho_inf = float(info["pinf_Pa"]) / (gas_constant * temperature) if gas_constant > 0 and temperature > 0 else 0.0
    n_rev_s = rpm / 60.0
    ct = cq = cp = ""
    if rho_inf > 0 and n_rev_s > 0 and diameter > 0:
        ct = thrust_convention / (rho_inf * n_rev_s**2 * diameter**4)
        cq = torque_convention / (rho_inf * n_rev_s**2 * diameter**5)
        cp = power / (rho_inf * n_rev_s**3 * diameter**5)

    warning_flags = []
    if str(info["solver_mode"]) == "incompressible_mrf" and freestream_mach > 0.3:
        warning_flags.append("incompressible_Mach_gt_0.3")
    if freestream_mach > 0.8:
        warning_flags.append("freestream_Mach_gt_0.8")
    if helical_tip_mach > 1.1:
        warning_flags.append("helical_tip_Mach_gt_1.1")
    elif helical_tip_mach > 0.95:
        warning_flags.append("helical_tip_Mach_gt_0.95")
    if thrust_convention < 0:
        warning_flags.append("NEGATIVE_THRUST_CHECK_SIGN_OR_ORIENTATION")
    if thrust_raw > 0 and thrust_convention < 0:
        warning_flags.append("THRUST_SIGN_MISMATCH")
    if shaft_torque < 0 or power <= 0:
        warning_flags.append("TORQUE_SIGN_CHECK")
    if eta != "" and (eta < 0 or eta > 1.0):
        warning_flags.append("eta_out_of_range")
    if int(averaged["samples"]) < args.average_last:
        warning_flags.append("averaging_window_not_reached")

    print(f"case_path: {case_path}")
    print(f"solver_mode: {info['solver_mode']}")
    print(f"Vinf_mps: {info['Vinf_mps']:.8g}")
    print(f"Vinf_mph: {float(info['Vinf_mps']) / 0.44704:.8g}")
    print(f"rpm: {rpm:.8g}")
    print(f"omega_rad_s: {float(omega):.8g}")
    print(f"Tinf_K: {temperature:.8g}")
    print(f"pinf_Pa: {float(info['pinf_Pa']):.8g}")
    print(f"gamma: {gamma:.8g}")
    print(f"R: {gas_constant:.8g}")
    print(f"Cp: {float(info['Cp']):.8g}")
    print(f"mu: {float(info['mu']):.8g}")
    print(f"Pr: {float(info['Pr']):.8g}")
    print(f"diameter_m: {diameter:.8g}")
    print(f"radius_m: {radius:.8g}")
    print(f"force_file: {force_file}")
    print(f"samples_averaged: {averaged['samples']}")
    print(f"latest_time: {averaged['time']}")
    print(f"thrust_axis: {thrust_axis}")
    print(f"thrust_sign: {thrust_sign:g}")
    print(f"torque_axis: {torque_axis}")
    print(f"torque_sign: {torque_sign:g}")
    print(f"shaft_torque_sign: {shaft_torque_sign:g}")
    print(f"torque_sign_convention: {torque_sign_convention}")
    print(f"force_N: ({force_x:.8g} {force_y:.8g} {force_z:.8g})")
    print(f"moment_Nm: ({moment_x:.8g} {moment_y:.8g} {moment_z:.8g})")
    last_row = rows[-1]
    print(f"force_samples: {len(rows)}")
    print(f"last_force_sample_time: {last_row['time']}")
    print(f"last_pressure_force_N: {last_row['pressure_force']}")
    print(f"last_viscous_force_N: {last_row['viscous_force']}")
    print(f"last_pressure_moment_Nm: {last_row['pressure_moment']}")
    print(f"last_viscous_moment_Nm: {last_row['viscous_moment']}")
    print(f"last_total_force_N: {last_row['force']}")
    print(f"last_total_moment_Nm: {last_row['moment']}")
    print(f"force_x: {force_x:.8g}")
    print(f"force_y: {force_y:.8g}")
    print(f"force_z: {force_z:.8g}")
    print(f"moment_x: {moment_x:.8g}")
    print(f"moment_y: {moment_y:.8g}")
    print(f"moment_z: {moment_z:.8g}")
    print(f"thrust_raw_N: {thrust_raw:.8g}")
    print(f"raw_force_axis_N: {thrust_raw:.8g}")
    print(f"torque_raw_Nm: {fluid_torque:.8g}")
    print(f"raw_moment_axis_Nm: {fluid_torque:.8g}")
    print(f"fluid_torque_Nm: {fluid_torque:.8g}")
    print(f"shaft_torque_Nm: {shaft_torque:.8g}")
    print(f"thrust_convention_N: {thrust_convention:.8g}")
    print(f"torque_convention_Nm: {torque_convention:.8g}")
    print(f"shaft_power_convention_W: {shaft_power_convention:.8g}")
    print(f"thrust_abs_N: {thrust_abs:.8g}")
    print(f"torque_abs_Nm: {torque_abs:.8g}")
    print(f"shaft_power_abs_W: {shaft_power_abs:.8g}")
    print(f"useful_power_W: {useful_power:.8g}")
    print(f"efficiency: {efficiency}")
    print(f"static_thrust_per_watt: {static_thrust_per_watt}")
    print(f"freestream_Mach: {freestream_mach:.8g}")
    print(f"tip_speed_mps: {tip_speed:.8g}")
    print(f"helical_tip_Mach: {helical_tip_mach:.8g}")
    print(f"advance_ratio_J: {advance_ratio}")
    print(f"power_W: {power:.8g}")
    print(f"eta: {eta}")
    print(f"CT: {ct}")
    print(f"CQ: {cq}")
    print(f"CP: {cp}")
    print(f"run_status: success")
    print(f"warning_flags: {','.join(warning_flags)}")

    update_results_csv(
        case_path,
        {
            "solver_mode": info["solver_mode"],
            "Vinf_mps": info["Vinf_mps"],
            "Vinf_mph": float(info["Vinf_mps"]) / 0.44704,
            "rpm": rpm,
            "omega_rad_s": omega,
            "Tinf_K": temperature,
            "pinf_Pa": info["pinf_Pa"],
            "gamma": gamma,
            "R": gas_constant,
            "Cp": info["Cp"],
            "mu": info["mu"],
            "Pr": info["Pr"],
            "diameter_m": diameter,
            "radius_m": radius,
            "freestream_Mach": freestream_mach,
            "tip_speed_mps": tip_speed,
            "helical_tip_Mach": helical_tip_mach,
            "advance_ratio_J": advance_ratio,
            "thrust_convention_N": thrust_convention,
            "raw_Fx_mean": force_x,
            "raw_Fy_mean": force_y,
            "raw_Fz_mean": force_z,
            "raw_force_axis_N_mean": thrust_raw,
            "thrust_axis": thrust_axis,
            "thrust_sign": thrust_sign,
            "torque_convention_Nm": torque_convention,
            "raw_Mx_mean": moment_x,
            "raw_My_mean": moment_y,
            "raw_Mz_mean": moment_z,
            "raw_moment_axis_Nm_mean": fluid_torque,
            "torque_axis": torque_axis,
            "torque_sign": torque_sign,
            "fluid_torque_Nm": fluid_torque,
            "shaft_torque_Nm": shaft_torque,
            "torque_sign_convention": torque_sign_convention,
            "power_W": power,
            "eta": eta,
            "CT": ct,
            "CQ": cq,
            "CP": cp,
            "run_status": "success",
            "warning_flags": ",".join(warning_flags),
            "static_thrust_per_watt": static_thrust_per_watt,
        },
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
