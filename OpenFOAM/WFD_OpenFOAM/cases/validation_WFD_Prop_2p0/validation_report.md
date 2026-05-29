# Forward-Flight Validation Report: FAIL

Quick validation confirms the compressible forward-flight case is wired correctly and producing parseable force/torque data. It does not prove production aerodynamic convergence or final accuracy.

## Case Settings
- source_variant_id: WFD_Prop_2p0
- validation_case: validation_WFD_Prop_2p0
- solver_mode: compressible_forward_mrf
- rpm: 10000.0
- Vinf_mph: 100.0
- Vinf_mps: 44.704
- Tinf_K: 288.15
- pinf_Pa: 101325.0
- gamma: 1.4
- R: 287.0
- air_model: perfectGas
- turbulence_model: kOmegaSST
- solver: rhoPimpleFoam
- omega_rad_s: 1047.1975511965977
- diameter_m: 0.1524
- radius_m: 0.0762
- advance_ratio_J: 1.7600000000000002
- freestream_Mach: 0.13138086178476094
- tip_speed_mps: 79.79645340118076
- helical_tip_Mach: 0.26880826439234445
- freestream_direction: [0, 0, 1]
- positive_thrust_direction: configured by thrust_sign and thrust_axis in case_info.json
- rotation_axis: [0, 0, 1]
- omega_sign: positive about +z
- force_component_used_as_thrust: z
- moment_component_used_as_torque: z
- note: PASS means internally consistent and runnable; it does not prove final aerodynamic accuracy.
- validation_revolutions: 0.1
- validation_endTime_s: 0.0006
- validation_writeInterval_s: 0.00015

## Expected Nondimensional Parameters
- Vinf_mps: 44.704
- Vinf_mph: 100.0
- rpm: 10000.0
- omega_rad_s: 1047.1975511965977
- advance_ratio_J: 1.7600000000000002
- freestream_Mach: 0.13138086178476094
- tip_speed_mps: 79.79645340118076
- helical_tip_Mach: 0.26880826439234445

## Parsed Force And Torque Values
- force_file: /home/erimozcan/OpenFOAM/erim-13/run/WFD_OpenFOAM/cases/validation_WFD_Prop_2p0/postProcessing/propellerForces/0/forces.dat
- force_samples: 644
- last_force_sample_time: 0.0006
- last_pressure_force_N: (-0.290966621, 2.02408859, 0.861436232)
- last_viscous_force_N: (-0.000336069824, 0.00924101567, 0.0172586544)
- last_pressure_moment_Nm: (0.00784541034, -0.0318970764, -0.0223614172)
- last_viscous_moment_Nm: (5.5472507e-06, 0.000487244974, -0.000891025412)
- last_total_force_N: (-0.29130269082399995, 2.0333296056699997, 0.8786948863999999)
- last_total_moment_Nm: (0.0078509575907, -0.031409831426, -0.023252442612)
- force_N: (-0.22082444 2.0613258 0.85561896)
- moment_Nm: (0.0070620637 -0.032173796 -0.02467084)
- thrust_convention_N: -0.85561896
- fluid_torque_Nm: -0.02467084
- shaft_torque_Nm: 0.02467084
- torque_sign_convention: shaft_torque_Nm = -fluid_moment_axis_Nm for positive omega when fluid moment opposes rotation
- power_W: 25.835243
- eta: -1.4805198502317438
- advance_ratio_J: 1.7600000000000002
- freestream_Mach: 0.13138086
- tip_speed_mps: 79.796453
- helical_tip_Mach: 0.26880826

## Setup Checks
- OK: required file 0/U 
- OK: required file 0/p 
- OK: required file 0/T 
- OK: required file 0/k 
- OK: required file 0/omega 
- OK: required file 0/nut 
- OK: required file 0/alphat 
- OK: required file constant/thermophysicalProperties 
- OK: required file constant/turbulenceProperties 
- OK: required file constant/MRFProperties 
- OK: required file system/controlDict 
- OK: required file system/functions 
- OK: required file run_metadata.json 
- OK: required file validation_config.json 
- OK: propeller patch found mesh patches found: ['inlet', 'outlet', 'propeller', 'sideWalls']
- OK: rotorZone rotor cellZone found cellZones found: ['rotorZone']
- OK: MRFProperties references rotorZone 
- OK: force output configured for propeller 
- OK: controlDict uses rhoPimpleFoam 
- OK: U internalField uses axial freestream 
- OK: p dimensions are absolute pressure 
- OK: T initialized 
- OK: perfect gas thermo configured 
- OK: force function uses computed rho 
- OK: kOmegaSST configured 
- OK: fvSolution solver entry p solver entries found: ['FoamFile', 'PIMPLE', 'U', 'UFinal', 'e', 'eFinal', 'equations', 'fields', 'h', 'hFinal', 'k', 'kFinal', 'omega', 'omegaFinal', 'p', 'pFinal', 'relaxationFactors', 'rho', 'rhoFinal', 'solvers']
- OK: fvSolution solver entry pFinal solver entries found: ['FoamFile', 'PIMPLE', 'U', 'UFinal', 'e', 'eFinal', 'equations', 'fields', 'h', 'hFinal', 'k', 'kFinal', 'omega', 'omegaFinal', 'p', 'pFinal', 'relaxationFactors', 'rho', 'rhoFinal', 'solvers']
- OK: fvSolution solver entry rho solver entries found: ['FoamFile', 'PIMPLE', 'U', 'UFinal', 'e', 'eFinal', 'equations', 'fields', 'h', 'hFinal', 'k', 'kFinal', 'omega', 'omegaFinal', 'p', 'pFinal', 'relaxationFactors', 'rho', 'rhoFinal', 'solvers']
- OK: fvSolution solver entry rhoFinal solver entries found: ['FoamFile', 'PIMPLE', 'U', 'UFinal', 'e', 'eFinal', 'equations', 'fields', 'h', 'hFinal', 'k', 'kFinal', 'omega', 'omegaFinal', 'p', 'pFinal', 'relaxationFactors', 'rho', 'rhoFinal', 'solvers']
- OK: fvSolution solver entry U solver entries found: ['FoamFile', 'PIMPLE', 'U', 'UFinal', 'e', 'eFinal', 'equations', 'fields', 'h', 'hFinal', 'k', 'kFinal', 'omega', 'omegaFinal', 'p', 'pFinal', 'relaxationFactors', 'rho', 'rhoFinal', 'solvers']
- OK: fvSolution solver entry UFinal solver entries found: ['FoamFile', 'PIMPLE', 'U', 'UFinal', 'e', 'eFinal', 'equations', 'fields', 'h', 'hFinal', 'k', 'kFinal', 'omega', 'omegaFinal', 'p', 'pFinal', 'relaxationFactors', 'rho', 'rhoFinal', 'solvers']
- OK: fvSolution solver entry h solver entries found: ['FoamFile', 'PIMPLE', 'U', 'UFinal', 'e', 'eFinal', 'equations', 'fields', 'h', 'hFinal', 'k', 'kFinal', 'omega', 'omegaFinal', 'p', 'pFinal', 'relaxationFactors', 'rho', 'rhoFinal', 'solvers']
- OK: fvSolution solver entry hFinal solver entries found: ['FoamFile', 'PIMPLE', 'U', 'UFinal', 'e', 'eFinal', 'equations', 'fields', 'h', 'hFinal', 'k', 'kFinal', 'omega', 'omegaFinal', 'p', 'pFinal', 'relaxationFactors', 'rho', 'rhoFinal', 'solvers']
- OK: fvSolution solver entry e solver entries found: ['FoamFile', 'PIMPLE', 'U', 'UFinal', 'e', 'eFinal', 'equations', 'fields', 'h', 'hFinal', 'k', 'kFinal', 'omega', 'omegaFinal', 'p', 'pFinal', 'relaxationFactors', 'rho', 'rhoFinal', 'solvers']
- OK: fvSolution solver entry eFinal solver entries found: ['FoamFile', 'PIMPLE', 'U', 'UFinal', 'e', 'eFinal', 'equations', 'fields', 'h', 'hFinal', 'k', 'kFinal', 'omega', 'omegaFinal', 'p', 'pFinal', 'relaxationFactors', 'rho', 'rhoFinal', 'solvers']
- OK: fvSolution solver entry k solver entries found: ['FoamFile', 'PIMPLE', 'U', 'UFinal', 'e', 'eFinal', 'equations', 'fields', 'h', 'hFinal', 'k', 'kFinal', 'omega', 'omegaFinal', 'p', 'pFinal', 'relaxationFactors', 'rho', 'rhoFinal', 'solvers']
- OK: fvSolution solver entry kFinal solver entries found: ['FoamFile', 'PIMPLE', 'U', 'UFinal', 'e', 'eFinal', 'equations', 'fields', 'h', 'hFinal', 'k', 'kFinal', 'omega', 'omegaFinal', 'p', 'pFinal', 'relaxationFactors', 'rho', 'rhoFinal', 'solvers']
- OK: fvSolution solver entry omega solver entries found: ['FoamFile', 'PIMPLE', 'U', 'UFinal', 'e', 'eFinal', 'equations', 'fields', 'h', 'hFinal', 'k', 'kFinal', 'omega', 'omegaFinal', 'p', 'pFinal', 'relaxationFactors', 'rho', 'rhoFinal', 'solvers']
- OK: fvSolution solver entry omegaFinal solver entries found: ['FoamFile', 'PIMPLE', 'U', 'UFinal', 'e', 'eFinal', 'equations', 'fields', 'h', 'hFinal', 'k', 'kFinal', 'omega', 'omegaFinal', 'p', 'pFinal', 'relaxationFactors', 'rho', 'rhoFinal', 'solvers']

## Solver Startup Checks
- OK: rhoPimpleFoam starts /home/erimozcan/OpenFOAM/erim-13/run/WFD_OpenFOAM/cases/validation_WFD_Prop_2p0/log.rhoPimpleFoam
- OK: thermophysical model loads 
- OK: turbulence model loads 
- WARN/FAIL: MRF loads expected rotorZone
- OK: no missing boundary condition errors 
- OK: no missing patch errors 
- WARN/FAIL: no missing cellZone errors expected rotorZone
- WARN/FAIL: no floating point exception during startup 

## Numerical Stability Checks
- OK: Courant number history [(0.0015098134, 0.49699853), (0.0015098199, 0.49704667), (0.0015098264, 0.49709294), (0.0015098329, 0.49713717), (0.0015098394, 0.49717922), (0.0015098459, 0.49721895), (0.0015098523, 0.49725623), (0.0015098588, 0.49729092), (0.0015098653, 0.49732286), (0.0015098718, 0.49735193)]
- WARN/FAIL: maximum Courant number 0.50502044
- OK: mean Courant number 0.0015237027
- OK: residual history available [('Ux', 0.00016631721, 1.1527968e-08), ('Uy', 4.4186647e-05, 3.1851086e-09), ('Uz', 1.8929149e-05, 1.0658048e-09), ('h', 4.073097e-05, 1.1424381e-09), ('p', 0.00028137244, 1.9632351e-12), ('rho', 0.0, 0.0), ('p', 0.00023901047, 1.6418681e-12), ('rho', 0.0, 0.0), ('omega', 1.7050391e-07, 1.728714e-11), ('k', 3.9986729e-07, 1.1079092e-10)]
- OK: residuals did not explode 
- WARN/FAIL: no fatal error, NaN, or floating point exception 
- OK: no obvious nonphysical pressure/temperature/velocity 

## Force/Torque Parsing Checks
- WARN/FAIL: parse_forces.py completed returncode=1
- OK: force output file exists /home/erimozcan/OpenFOAM/erim-13/run/WFD_OpenFOAM/cases/validation_WFD_Prop_2p0/postProcessing/propellerForces/0/forces.dat
- OK: several force samples parseable 644
- OK: thrust can be parsed -0.85561896
- OK: fluid torque can be parsed -0.02467084
- OK: shaft torque can be parsed 0.02467084
- OK: legacy torque convention populated 0.02467084
- OK: power_W computed 25.835243
- OK: advance_ratio_J computed 1.7600000000000002
- OK: eta computed safely -1.4805198502317438

## Physics Sanity Checks
- OK: freestream Mach computed 0.13138086
- OK: tip speed computed 79.796453
- OK: helical tip Mach computed 0.26880826
- OK: thrust sign convention checked thrust_N=-0.85561896
- OK: torque sign convention checked fluid_torque_Nm=-0.02467084, shaft_torque_Nm=0.02467084, shaft_torque_Nm = -fluid_moment_axis_Nm for positive omega when fluid moment opposes rotation
- OK: last total force vector (-0.29130269082399995, 2.0333296056699997, 0.8786948863999999)
- OK: last total moment vector (0.0078509575907, -0.031409831426, -0.023252442612)

## Sign-Convention Checks
- OK: freestream direction vector [0, 0, 1]
- OK: positive thrust direction configured by thrust_sign and thrust_axis in case_info.json
- OK: rotation axis [0, 0, 1]
- OK: omega sign positive about +z
- OK: force component used as thrust z
- OK: moment component used as torque z

## Warnings and Recommended Next Action
- Courant max 0.505 exceeded configured maxCo 0.5
- Solver log contains validation warnings; inspect log.rhoPimpleFoam
- Force parsing failed
- Thrust is negative
- Eta is outside [0, 1]

## Next validation steps
- run 100 mph / 10000 rpm
- then 300 mph / 10000 rpm
- then 650 mph / 10000 rpm
- then 650 mph / target rpm
- then longer production-style validation for the selected condition
- compare trends in thrust, torque, power, eta, Mach, and stability
- only then run the full batch
