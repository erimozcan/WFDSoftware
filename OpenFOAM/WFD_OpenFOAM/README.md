# WFD OpenFOAM Propeller CFD Scaffold

This project is a working scaffold for testing drone propeller variants with OpenFOAM 13. The long-term workflow is to generate one case per STL variant, mesh it, run a rotating propeller simulation, extract thrust and torque metrics, and write comparison results to CSV.

## Current Milestone

The current milestone generates baseline cases, meshes the propeller with `snappyHexMesh`, runs a first-pass steady MRF solve, and parses raw force and moment output. This is physically structured enough for workflow development, but it is not final validation accuracy.

Baseline geometry:

- STL: `prop_stls/WFD_Prop_2p0_clean_m_centered.stl`
- Diameter: `0.1524 m`
- Rotation axis: z-axis, `[0, 0, 1]`
- Forward-flight batch RPMs: `8000`, `10000`, `12000`
- Forward-flight freestream: `10 m/s`

## Solver Modes

The generator supports two solver modes through `variants.csv`:

- `incompressible_mrf`: existing steady MRF workflow using `templates/prop_MRF_template` and `foamRun -solver incompressibleFluid`.
- `compressible_forward_mrf`: forward-flight MRF workflow using `templates/compressible_forward_mrf` and `rhoPimpleFoam`.

The compressible mode writes `0/U`, `0/p`, `0/T`, `0/k`, `0/omega`, `0/nut`, `0/alphat`, plus `constant/thermophysicalProperties`, `constant/turbulenceProperties`, and `constant/MRFProperties`. It uses perfect-gas air defaults:

```text
Tinf_K=288.15, pinf_Pa=101325, R=287, gamma=1.4, Cp=1004.5, mu=1.7894e-5, Pr=0.71
```

`rpm` is converted to `omega_rad_s = 2*pi*rpm/60`. `Vinf_mps` is applied along the propeller z-axis in the generated `U` initial and inlet fields. Static `Vinf_mps=0` rows are marked `batch_enabled=false` and are intended for debug/sanity checks rather than the default RPM sweep.

Generation and parsing metadata include `solver_mode`, `Vinf_mps`, `rpm`, `omega_rad_s`, `Tinf_K`, `pinf_Pa`, `gamma`, `R`, `Cp`, `mu`, `Pr`, `diameter_m`, and `radius_m`.

Mesh reuse rule: do not rerun meshing when only `Vinf_mps`, `rpm`, pressure, temperature, turbulence initial values, or solver controls change. Rerun meshing only when STL geometry, mesh resolution, domain dimensions, refinement settings, or rotor-zone geometry change.

## Folder Structure

- `variants.csv`: variant input table.
- `prop_stls/`: cleaned source STL files.
- `templates/prop_MRF_template/`: copied OpenFOAM tutorial-style template case.
- `cases/`: generated case directories.
- `scripts/`: Python automation scripts.
- `app.py` and `web/`: local browser dashboard.
- `results/`: batch status and future result CSV files.

## Generate Cases

If a case already exists, replace it explicitly:

```bash
python3 scripts/generate_case.py --overwrite
```

For each row in `variants.csv`, this creates `cases/<variant_id>/`, copies the full template case, copies the selected STL to `constant/triSurface/propeller.stl`, writes `case_info.json`, and updates `constant/MRFProperties` with the variant-specific MRF `omega` computed from `rpm`.

## Main Terminal UI

The easiest entry point is:

```bash
python3 scripts/main.py
```

The interactive menu provides:

```text
Ozcan Propeller CFD
1) Generate cases
2) Run sanity mesh checks
3) Run snappy mesh for one case
4) Solve one case using existing mesh
5) Rebuild mesh + solve one case
6) Parse all results
7) Diagnose one case
8) Run full batch
q) Quit
```

Command-line shortcuts are also available:

```bash
python3 scripts/main.py generate
python3 scripts/main.py sanity
python3 scripts/main.py mesh --case rpm_10000
python3 scripts/main.py solve-existing --case rpm_10000
python3 scripts/main.py solve-one --case rpm_10000
python3 scripts/main.py solve-batch
python3 scripts/main.py parse
python3 scripts/main.py diagnose --case rpm_10000
```

The UI prints the active stage, elapsed time, the full log path, and selected recent log lines for long OpenFOAM commands. If `rich` is installed it uses nicer terminal formatting; otherwise it uses plain text.

## Current Sanity Workflow

Without extra flags, the single-case runner only runs:

- `blockMesh`
- `checkMesh`

Example:

```bash
python3 scripts/run_case.py cases/baseline
```

Logs are written inside the case directory:

- `log.blockMesh`
- `log.checkMesh`

## Snappy Meshing Workflow

The first-pass propeller meshing workflow runs:

- `blockMesh`
- `surfaceFeatures`
- `snappyHexMesh -overwrite`
- `topoSet` to create the `rotatingZone` cellZone
- `checkMesh`

OpenFOAM 13 supersedes `surfaceFeatureExtract` with `surfaceFeatures`; the script uses the v13 utility and writes the log as `log.surfaceFeatureExtract`.

```bash
python3 scripts/run_case.py cases/baseline --snappy
```

Snappy logs are written inside the case directory:

- `log.blockMesh`
- `log.surfaceFeatureExtract`
- `log.snappyHexMesh`
- `log.topoSet`
- `log.checkMesh`

## Browser Dashboard

A simple local browser UI is available as a thin wrapper around the existing scripts. It does not replace the terminal workflow and it does not accept arbitrary shell commands from the browser.

Install the web dependencies if needed:

```bash
pip install fastapi uvicorn
```

Start the server from the project root in WSL:

```bash
python3 app.py
```

Open this URL in your Windows browser:

```text
http://localhost:8000
```

Dashboard actions:

- `Validate Forward-Flight Model`: prepares and runs one controlled `compressible_forward_mrf` sanity case only when explicitly clicked. The default validation case is `100 mph / 10000 rpm`, using `rhoPimpleFoam`, perfect-gas air, and `kOmegaSST`. Preparation writes `validation_config.json`, `run_metadata.json`, `validation_report.json`, and `validation_report.md` without launching OpenFOAM. Running validation executes only that one validation case and updates the report with setup, startup, numerical, force/torque, physics, and sign-convention checks.
- `Batch Forward-Flight Run`: primary workflow for compressible propeller batches. It reads `variants.csv`, applies the UI RPM/freestream/air settings unless a row explicitly overrides `rpm` or `Vinf_mps`, generates `compressible_forward_mrf` cases, reuses existing meshes when geometry is unchanged, runs the case solver, and writes `results/forward_batch_results.csv`.
- Freestream presets are available for `0`, `100`, `300`, and `650 mph`; the default is `300 mph` (`134.112 m/s`). The UI converts mph to m/s with `Vinf_mps = mph * 0.44704`.
- The main results table compares thrust, torque, power, efficiency, advance ratio, freestream Mach, helical tip Mach, and propeller coefficients `CT`, `CQ`, and `CP`.
- The ranking panel reports best by thrust, best by efficiency, and best overall candidate. Failed runs are not ranked above successful runs.

The collapsed `Debug / Advanced` section keeps individual OpenFOAM-stage tools available:

- `incompressible_mrf` solver mode, static debug settings, mesh-only runs, solver-only runs, dry runs, and force re-mesh controls.
- `Run Snappy Mesh for Selected Case`: runs the existing `run_case.py --snappy` path. This remeshes and creates the MRF cellZone, but does not run `foamRun`.
- `Diagnose Selected Case`: runs `python3 scripts/diagnose_cfd_setup.py cases/<case>`. This is inspection only; it does not remesh or solve.
- `Rebuild Mesh + Solve Selected Case`: runs `python3 scripts/run_case.py cases/<case> --solve`. This is the slower full fresh path because it rebuilds the mesh before solving with the case solver.
- `Parse Selected Case Raw Output`: runs `python3 scripts/parse_forces.py cases/<case>` and keeps raw force/moment details expandable in the results table.

## Baseline MRF Solve

The first-pass solve workflow runs the full serial setup:

- `blockMesh`
- `surfaceFeatures`
- `snappyHexMesh -overwrite`
- `topoSet` to create the cylindrical `rotatingZone` cellZone
- `checkMesh`
- `foamRun -solver incompressibleFluid`

Run one baseline solve from the project root:

```bash
python3 scripts/generate_case.py --overwrite
python3 scripts/run_case.py cases/baseline --solve
python3 scripts/parse_forces.py cases/baseline
```

To solve again using an existing mesh without remeshing:

```bash
python3 scripts/run_case.py cases/baseline --solve-only
```

The MRF region is a simple cylinder centered at the origin around the z-axis. The current baseline uses `omega = 1047.197551 rad/s`, `kOmegaSST`, air `nu = 1.5e-5 m^2/s`, and raw `forces` output on the `propeller` patch.

For a steady OpenFOAM MRF case, the generated template uses `MRFnoSlip` on the rotating `propeller` patch, following the installed OpenFOAM 13 rotating templates.

For static testing at `freestream_velocity_mps = 0`, propulsive efficiency is not meaningful because useful power is zero. Use static thrust per watt as the practical static comparison metric.

## Run Batch Scaffold

From the project root:

```bash
python3 scripts/run_batch.py --overwrite --snappy
```

The batch script reads `variants.csv`, generates each case, runs the selected stages, continues after failures, and writes:

```text
results/batch_status.csv
```

The direct scripts remain useful for advanced/debug work:

```bash
python3 scripts/generate_case.py --overwrite
python3 scripts/run_case.py cases/rpm_10000 --snappy
python3 scripts/run_case.py cases/rpm_10000 --solve
python3 scripts/parse_forces.py cases/rpm_10000
python3 scripts/diagnose_cfd_setup.py cases/rpm_10000
```

## Not Implemented Yet

The following CFD setup work is intentionally not part of this milestone:

- Final mesh independence or domain-size validation.
- Final MRF-region tuning.
- Batch solving and batch force aggregation as the default production workflow.
- Validated thrust/torque accuracy against test data.

## Next Milestone

The next planned milestone is improving the first-pass MRF setup for repeatable variant ranking: cleaner result CSV aggregation, sign convention checks for thrust and torque, and better mesh/refinement controls around the blade surfaces.
