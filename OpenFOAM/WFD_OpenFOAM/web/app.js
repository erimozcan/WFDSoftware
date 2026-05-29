const caseSelect = document.querySelector("#caseSelect");
const caseMeshState = document.querySelector("#caseMeshState");
const validationVariant = document.querySelector("#validationVariant");
const validationPreset = document.querySelector("#validationPreset");
const validationRpm = document.querySelector("#validationRpm");
const validationSummary = document.querySelector("#validationSummary");
const validationWarnings = document.querySelector("#validationWarnings");
const validationReport = document.querySelector("#validationReport");
const stopValidationButton = document.querySelector("#stopValidation");
const overnightZip = document.querySelector("#overnightZip");
const overnightPath = document.querySelector("#overnightPath");
const overnightRpm = document.querySelector("#overnightRpm");
const overnightSpeed = document.querySelector("#overnightSpeed");
const overnightSpeedUnits = document.querySelector("#overnightSpeedUnits");
const overnightTinf = document.querySelector("#overnightTinf");
const overnightPinf = document.querySelector("#overnightPinf");
const overnightMaxMinutes = document.querySelector("#overnightMaxMinutes");
const overnightAverageWindow = document.querySelector("#overnightAverageWindow");
const overnightConvergencePreset = document.querySelector("#overnightConvergencePreset");
const convergenceMinSamples = document.querySelector("#convergenceMinSamples");
const convergenceWindowSize = document.querySelector("#convergenceWindowSize");
const convergenceThrustChange = document.querySelector("#convergenceThrustChange");
const convergenceTorqueChange = document.querySelector("#convergenceTorqueChange");
const convergenceThrustCv = document.querySelector("#convergenceThrustCv");
const convergenceTorqueCv = document.querySelector("#convergenceTorqueCv");
const overnightMeshSanity = document.querySelector("#overnightMeshSanity");
const overnightContinueFailed = document.querySelector("#overnightContinueFailed");
const overnightStopFatal = document.querySelector("#overnightStopFatal");
const overnightWarningOverride = document.querySelector("#overnightWarningOverride");
const overnightValidationOverride = document.querySelector("#overnightValidationOverride");
const overnightChecklist = document.querySelector("#overnightChecklist");
const overnightWarnings = document.querySelector("#overnightWarnings");
const overnightVariantsBody = document.querySelector("#overnightVariantsBody");
const overnightMeshProfiles = document.querySelector("#overnightMeshProfiles");
const batchDebugLine = document.querySelector("#batchDebugLine");
const resumeBatchButton = document.querySelector("#resumeBatch");
const recoveryCard = document.querySelector("#recoveryCard");
const recoveryList = document.querySelector("#recoveryList");
const forceRemesh = document.querySelector("#forceRemesh");
const statePill = document.querySelector("#statePill");
const currentBatchId = document.querySelector("#currentBatchId");
const currentAction = document.querySelector("#currentAction");
const selectedCase = document.querySelector("#selectedCase");
const caseIndex = document.querySelector("#caseIndex");
const currentStage = document.querySelector("#currentStage");
const currentPid = document.querySelector("#currentPid");
const elapsedTime = document.querySelector("#elapsedTime");
const heartbeatStatus = document.querySelector("#heartbeatStatus");
const lastLogUpdate = document.querySelector("#lastLogUpdate");
const currentCommand = document.querySelector("#currentCommand");
const progressBar = document.querySelector("#progressBar");
const modeNote = document.querySelector("#modeNote");
const logPanel = document.querySelector("#logPanel");
const statusLogTail = document.querySelector("#statusLogTail");
const resultsBody = document.querySelector("#resultsBody");
const diagnosticsList = document.querySelector("#diagnosticsList");
const bestThrust = document.querySelector("#bestThrust");
const bestEta = document.querySelector("#bestEta");
const bestOverall = document.querySelector("#bestOverall");
const solverProgressCard = document.querySelector("#solverProgressCard");
const solverStatusPill = document.querySelector("#solverStatusPill");
const solverProgressNote = document.querySelector("#solverProgressNote");
const solverProgressBar = document.querySelector("#solverProgressBar");
const solverCurrentTime = document.querySelector("#solverCurrentTime");
const solverTargetTime = document.querySelector("#solverTargetTime");
const solverPhysicalPercent = document.querySelector("#solverPhysicalPercent");
const solverTimestepPercent = document.querySelector("#solverTimestepPercent");
const solverDeltaT = document.querySelector("#solverDeltaT");
const solverCoMean = document.querySelector("#solverCoMean");
const solverCoMax = document.querySelector("#solverCoMax");
const solverExecClock = document.querySelector("#solverExecClock");
const solverLogAge = document.querySelector("#solverLogAge");
const solverForceSamples = document.querySelector("#solverForceSamples");
const solverThrust = document.querySelector("#solverThrust");
const solverTorque = document.querySelector("#solverTorque");
const solverRawAxialForce = document.querySelector("#solverRawAxialForce");
const solverSignedThrust = document.querySelector("#solverSignedThrust");
const solverConvergenceStatus = document.querySelector("#solverConvergenceStatus");
const solverConvergenceThresholds = document.querySelector("#solverConvergenceThresholds");
const solverConvergenceBlockedBy = document.querySelector("#solverConvergenceBlockedBy");
const solverConvergenceThrust = document.querySelector("#solverConvergenceThrust");
const solverConvergenceTorque = document.querySelector("#solverConvergenceTorque");
const solverStopReason = document.querySelector("#solverStopReason");
const solverValidationRevs = document.querySelector("#solverValidationRevs");
const solverWarnings = document.querySelector("#solverWarnings");
const solverLogTail = document.querySelector("#solverLogTail");
const solverPlotsCard = document.querySelector("#solverPlotsCard");
const plotNote = document.querySelector("#plotNote");
const plotWindow = document.querySelector("#plotWindow");
const showMovingAverage = document.querySelector("#showMovingAverage");
const averageWindow = document.querySelector("#averageWindow");
const plotGrid = document.querySelector("#plotGrid");
const plotMessages = document.querySelector("#plotMessages");
const stabilityHints = document.querySelector("#stabilityHints");
const buttons = Array.from(document.querySelectorAll("button"));

let caseDetails = new Map();
let variants = [];
let overnightManifest = null;
let overnightSelected = new Set();
let overnightSelectionInitialized = false;
let latestRecoveryBatchId = "";
let activePlotTab = "default";
let lastTimeseries = null;
let lastImportError = "";
let lastStartError = "";
let lastBackendStatus = "idle";
let batchRunDisabledReason = "Import STL geometry before starting Batch Run.";

function selectedCaseName() {
  return caseSelect.value;
}

function validationPayload() {
  return {
    variant_id: validationVariant.value,
    preset: validationPreset.value,
    rpm: Number(validationRpm.value),
    Vinf_mph: validationPreset.value === "650_selected" ? 650 : 100,
    Tinf_K: 288.15,
    pinf_Pa: 101325,
    force_remesh: forceRemesh.checked,
    validation_revolutions: 0.10
  };
}

function overnightVinfMps() {
  const speed = Number(overnightSpeed.value);
  return overnightSpeedUnits.value === "mph" ? speed * 0.44704 : speed;
}

const convergencePresets = {
  fast_screening: {
    minSamples: 2000,
    windowSize: 500,
    thrustChange: 3,
    torqueChange: 5,
    thrustCv: 5,
    torqueCv: 5
  },
  finalist: {
    minSamples: 5000,
    windowSize: 1000,
    thrustChange: 2,
    torqueChange: 2,
    thrustCv: 3,
    torqueCv: 3
  }
};

function activeConvergenceSettings() {
  const preset = overnightConvergencePreset.value || "fast_screening";
  const values = preset === "custom" ? {
    minSamples: Number(convergenceMinSamples.value),
    windowSize: Number(convergenceWindowSize.value),
    thrustChange: Number(convergenceThrustChange.value),
    torqueChange: Number(convergenceTorqueChange.value),
    thrustCv: Number(convergenceThrustCv.value),
    torqueCv: Number(convergenceTorqueCv.value)
  } : (convergencePresets[preset] || convergencePresets.fast_screening);
  return {
    preset,
    minSamples: Number.isFinite(values.minSamples) ? values.minSamples : 2000,
    windowSize: Number.isFinite(values.windowSize) ? values.windowSize : 500,
    thrustChange: Number.isFinite(values.thrustChange) ? values.thrustChange : 3,
    torqueChange: Number.isFinite(values.torqueChange) ? values.torqueChange : 5,
    thrustCv: Number.isFinite(values.thrustCv) ? values.thrustCv : 5,
    torqueCv: Number.isFinite(values.torqueCv) ? values.torqueCv : 5
  };
}

function applyConvergencePresetToInputs() {
  const preset = overnightConvergencePreset.value || "fast_screening";
  if (preset === "custom") return;
  const values = convergencePresets[preset] || convergencePresets.fast_screening;
  convergenceMinSamples.value = values.minSamples;
  convergenceWindowSize.value = values.windowSize;
  convergenceThrustChange.value = values.thrustChange;
  convergenceTorqueChange.value = values.torqueChange;
  convergenceThrustCv.value = values.thrustCv;
  convergenceTorqueCv.value = values.torqueCv;
}

function overnightPayload() {
  const maxMinutes = Number(overnightMaxMinutes.value);
  const speed = Number(overnightSpeed.value);
  const convergence = activeConvergenceSettings();
  return {
    batch_id: overnightManifest ? overnightManifest.batch_id : "",
    selected_variant_ids: Array.from(overnightSelected),
    rpm: Number(overnightRpm.value),
    freestream_speed: Number.isFinite(speed) ? speed : 300,
    speed_units: overnightSpeedUnits.value,
    Tinf_K: Number(overnightTinf.value),
    pinf_Pa: Number(overnightPinf.value),
    max_wall_clock_minutes_per_propeller: Number.isFinite(maxMinutes) ? maxMinutes : 0,
    averaging_window: Number(overnightAverageWindow.value),
    auto_stop_on_convergence: true,
    convergence_preset: convergence.preset,
    convergence_min_force_samples: convergence.minSamples,
    convergence_window_size: convergence.windowSize,
    convergence_thrust_mean_change_pct: convergence.thrustChange,
    convergence_torque_mean_change_pct: convergence.torqueChange,
    convergence_thrust_cv_pct: convergence.thrustCv,
    convergence_torque_cv_pct: convergence.torqueCv,
    run_mesh_sanity: overnightMeshSanity.checked,
    geometry_check_profile: overnightMeshSanity.checked ? "quick_coarse_mesh_check" : "fast_geometry_check",
    batch_mesh_profile: "production_high_resolution",
    continue_after_failed_variant: overnightContinueFailed.checked,
    stop_on_first_fatal_error: overnightStopFatal.checked,
    allow_warning_override: overnightWarningOverride.checked,
    validation_override: true
  };
}

function updateBatchDebugLine() {
  if (!batchDebugLine) return;
  const imported = overnightManifest && Array.isArray(overnightManifest.variants) ? overnightManifest.variants.length : 0;
  const selected = overnightSelected.size;
  const batchId = overnightManifest ? overnightManifest.batch_id : "none";
  batchDebugLine.textContent = [
    `imported ${imported}`,
    `selected ${selected}`,
    `batch ${batchId || "none"}`,
    `import error ${lastImportError || "none"}`,
    `start error ${lastStartError || "none"}`,
    `start ${batchRunDisabledReason || "ready"}`,
    `backend ${lastBackendStatus || "idle"}`
  ].join(" | ");
}

function normalizeBatchManifest(payload) {
  const manifest = payload || {};
  const variants = Array.isArray(manifest.variants)
    ? manifest.variants
    : (Array.isArray(manifest.imported_variants) ? manifest.imported_variants : []);
  return { ...manifest, variants };
}

function setBatchRunButtonState() {
  const runButton = document.querySelector("#overnightRun");
  if (!runButton) return;
  const importedCount = overnightManifest && Array.isArray(overnightManifest.variants) ? overnightManifest.variants.length : 0;
  const rpm = Number(overnightRpm.value);
  const speed = Number(overnightSpeed.value);
  let reason = "";
  if (!importedCount) reason = "Import STL geometry before starting Batch Run.";
  else if (!overnightSelected.size) reason = "Select at least one imported STL before starting Batch Run.";
  else if (!Number.isFinite(rpm) || rpm <= 0) reason = "Set a global rpm greater than zero.";
  else if (!Number.isFinite(speed) || speed < 0) reason = "Set a non-negative global freestream speed.";
  batchRunDisabledReason = reason;
  runButton.disabled = Boolean(reason);
  runButton.title = reason || "Start Batch Run";
  updateBatchDebugLine();
}

function renderBatchStartResponse(response) {
  if (!response || !response.batch_id) return;
  currentBatchId.textContent = response.batch_id;
  currentAction.textContent = response.action || "Batch Run";
  currentStage.textContent = response.status || "queued";
  caseIndex.textContent = `${response.selected_variant_count || 0}/${response.selected_variant_count || 0}`;
  statePill.textContent = response.status || "queued";
  statePill.className = `pill ${response.status || "queued"}`;
  modeNote.textContent = `Batch Run ${response.status || "queued"}: ${response.batch_id}`;
}

async function apiGet(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}

async function apiPost(url, body = null) {
  const options = { method: "POST", headers: { "Content-Type": "application/json" } };
  if (body) options.body = JSON.stringify(body);
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || response.statusText);
  return data;
}

function numberText(value, digits = 4) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return value || "";
  return parsed.toPrecision(digits);
}

function finitePoints(rows, xKey, yKey) {
  return (rows || [])
    .map((row, index) => ({ x: Number(row[xKey] ?? row.time ?? row.timestep ?? row.sample ?? index + 1), y: Number(row[yKey]) }))
    .filter((point) => Number.isFinite(point.x) && Number.isFinite(point.y));
}

function windowed(points) {
  const value = plotWindow.value;
  if (value === "all") return points;
  const count = Number(value);
  return Number.isFinite(count) ? points.slice(-count) : points;
}

function movingAverage(points, windowSize) {
  if (!points.length || windowSize < 2) return [];
  const averaged = [];
  for (let index = 0; index < points.length; index += 1) {
    const start = Math.max(0, index - windowSize + 1);
    const chunk = points.slice(start, index + 1);
    averaged.push({ x: points[index].x, y: chunk.reduce((sum, point) => sum + point.y, 0) / chunk.length });
  }
  return averaged;
}

function drawLine(canvas, points, averagePoints = []) {
  const ctx = canvas.getContext("2d");
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  canvas.width = Math.max(320, Math.floor(width * window.devicePixelRatio));
  canvas.height = Math.max(180, Math.floor(height * window.devicePixelRatio));
  ctx.scale(window.devicePixelRatio, window.devicePixelRatio);
  ctx.clearRect(0, 0, width, height);
  ctx.strokeStyle = "#2f3b46";
  ctx.lineWidth = 1;
  ctx.strokeRect(36, 12, width - 48, height - 42);
  if (points.length < 2) return;
  const xs = points.map((point) => point.x);
  const ys = points.map((point) => point.y);
  const minX = Math.min(...xs);
  const maxX = Math.max(...xs);
  let minY = Math.min(...ys);
  let maxY = Math.max(...ys);
  if (minY === maxY) {
    minY -= 1;
    maxY += 1;
  }
  const mapX = (value) => 36 + ((value - minX) / (maxX - minX || 1)) * (width - 48);
  const mapY = (value) => 12 + (1 - (value - minY) / (maxY - minY || 1)) * (height - 42);
  ctx.strokeStyle = "#66a6ff";
  ctx.lineWidth = 1.6;
  ctx.beginPath();
  points.forEach((point, index) => {
    const x = mapX(point.x);
    const y = mapY(point.y);
    if (index === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();
  if (averagePoints.length > 1) {
    ctx.strokeStyle = "#3ddc84";
    ctx.lineWidth = 1.8;
    ctx.beginPath();
    averagePoints.forEach((point, index) => {
      const x = mapX(point.x);
      const y = mapY(point.y);
      if (index === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
  }
  ctx.fillStyle = "#93a0ad";
  ctx.font = "11px system-ui";
  ctx.fillText(numberText(minY), 6, height - 30);
  ctx.fillText(numberText(maxY), 6, 20);
  ctx.fillText(numberText(minX), 36, height - 10);
  ctx.fillText(numberText(maxX), width - 78, height - 10);
}

function makePlot(title, points, useAverage = false) {
  const article = document.createElement("section");
  article.className = "plot-card";
  const heading = document.createElement("h3");
  heading.textContent = title;
  const detail = document.createElement("p");
  detail.textContent = points.length ? `${points.length} samples` : "data not available yet";
  const canvas = document.createElement("canvas");
  canvas.className = "plot-canvas";
  article.append(heading, detail, canvas);
  plotGrid.appendChild(article);
  const avgWindow = Number(averageWindow.value) || 20;
  const avg = useAverage && showMovingAverage.checked ? movingAverage(points, avgWindow) : [];
  requestAnimationFrame(() => drawLine(canvas, points, avg));
}

function residualSeries(rows) {
  const fields = ["Ux", "Uy", "Uz", "p", "h", "k", "omega", "rho"];
  return fields
    .map((field) => ({ field, rows: (rows || []).filter((row) => row.field === field) }))
    .filter((item) => item.rows.length);
}

function renderStability(data) {
  const stability = data.stability || {};
  stabilityHints.innerHTML = "";
  for (const key of ["thrust_N", "shaft_torque_Nm"]) {
    const item = stability[key];
    if (!item) continue;
    const div = document.createElement("div");
    div.textContent = `${key}: ${item.status || "not enough samples"}; mean ${numberText(item.mean)}, std ${numberText(item.stddev)}, CV ${numberText(item.coefficient_of_variation)}`;
    stabilityHints.appendChild(div);
  }
}

function renderPlots(data, taskRunning = false) {
  if (!data) return;
  lastTimeseries = data;
  const hasAnyData = (data.force_samples || []).length || (data.courant || []).length || (data.residuals || []).length;
  solverPlotsCard.classList.toggle("hidden", !(taskRunning || hasAnyData));
  if (solverPlotsCard.classList.contains("hidden")) return;
  plotGrid.innerHTML = "";
  const messages = [];
  if (!(data.force_samples || []).length) messages.push("force data not available yet");
  if (!(data.residuals || []).length) messages.push("residual data not available yet");
  if (data.note) messages.push(data.note);
  plotMessages.textContent = messages.join(" | ") || "Live solver histories are updating.";
  plotNote.textContent = data.validation_id
    ? "Validation plots show solver health and force data."
    : "Plots show the current case in the active batch run.";

  const forces = data.force_samples || [];
  const courant = data.courant || [];
  const residuals = data.residuals || [];
  const useTime = forces.some((row) => row.time !== undefined);
  const xKey = useTime ? "time" : "sample";
  if (activePlotTab === "default") {
    makePlot("thrust_N vs simulation time", windowed(finitePoints(forces, xKey, "thrust_N")), true);
    makePlot("shaft_torque_Nm vs simulation time", windowed(finitePoints(forces, xKey, "shaft_torque_Nm")), true);
  } else if (activePlotTab === "forces") {
    for (const key of ["Fx", "Fy", "Fz", "thrust_N"]) makePlot(`${key} vs simulation time`, windowed(finitePoints(forces, xKey, key)), key === "thrust_N");
  } else if (activePlotTab === "moments") {
    for (const key of ["Mx", "My", "Mz", "shaft_torque_Nm"]) makePlot(`${key} vs simulation time`, windowed(finitePoints(forces, xKey, key)), key === "shaft_torque_Nm");
  } else if (activePlotTab === "courant") {
    makePlot("Courant max vs simulation time", windowed(finitePoints(courant, "time", "max")));
    makePlot("Courant mean vs simulation time", windowed(finitePoints(courant, "time", "mean")));
  } else if (activePlotTab === "residuals") {
    const series = residualSeries(residuals);
    if (!series.length) makePlot("residuals", []);
    for (const item of series) makePlot(`residual ${item.field}`, windowed(finitePoints(item.rows, "timestep", "initial")));
  } else if (activePlotTab === "performance") {
    for (const key of ["power_W", "eta", "thrust_N", "shaft_torque_Nm"]) makePlot(`${key} vs simulation time`, windowed(finitePoints(forces, xKey, key)), ["thrust_N", "shaft_torque_Nm"].includes(key));
  }
  renderStability(data);
}


function renderList(target, warnings) {
  target.innerHTML = "";
  for (const warning of warnings || []) {
    const li = document.createElement("li");
    li.textContent = warning;
    target.appendChild(li);
  }
}

function renderOvernightManifest(manifest) {
  overnightManifest = normalizeBatchManifest(manifest);
  const rows = overnightManifest.variants || [];
  const rowIds = new Set(rows.map((row) => row.variant_id));
  if (!overnightSelectionInitialized) {
    overnightSelected = new Set(rows.filter((row) => row.selected !== false).map((row) => row.variant_id));
    overnightSelectionInitialized = true;
  } else {
    overnightSelected = new Set(Array.from(overnightSelected).filter((variantId) => rowIds.has(variantId)));
  }
  updateBatchDebugLine();
  const vinfMps = overnightVinfMps();
  const vinfMph = vinfMps / 0.44704;
  const gamma = 1.4;
  const gasR = 287;
  const speedOfSound = Math.sqrt(gamma * gasR * Number(overnightTinf.value));
  const mach = vinfMps / speedOfSound;
  const radius = 0.0762;
  const omega = Number(overnightRpm.value) * 2 * Math.PI / 60;
  const tipSpeed = omega * radius;
  const helicalMach = Math.hypot(vinfMps, tipSpeed) / speedOfSound;
  const advanceRatio = Number(overnightRpm.value) > 0 ? vinfMps / ((Number(overnightRpm.value) / 60) * (2 * radius)) : "";
  const geometryProfile = overnightMeshSanity.checked ? "quick_coarse_mesh_check" : "fast_geometry_check";
  const batchProfile = "production_high_resolution";
  const convergence = activeConvergenceSettings();
  overnightMeshProfiles.textContent = `Geometry check profile: ${geometryProfile}. Batch mesh profile: ${batchProfile}. Surface refinement: production template. Rotor zone refinement: production template. Boundary/prism layers: as configured in template.`;
  const maxWall = Number(overnightMaxMinutes.value);
  overnightChecklist.textContent = [
    `batch_id ${manifest.batch_id}`,
    `${rows.length} STL(s) detected`,
    `${rows.filter((row) => row.json_found).length} JSON metadata file(s)`,
    `${overnightSelected.size} selected`,
    `disk free ${(manifest.pre_run_checks && manifest.pre_run_checks.disk_free_GB) || "?"} GB`,
    `OpenFOAM ${(manifest.pre_run_checks && manifest.pre_run_checks.openfoam_available) ? "available" : "not detected"}`,
    `template ${(manifest.pre_run_checks && manifest.pre_run_checks.compressible_forward_mrf_template_available) ? "available" : "missing"}`,
    `geometry check ${geometryProfile}`,
    `batch mesh ${batchProfile}`,
    `global ${overnightRpm.value} rpm`,
    `global Vinf ${numberText(vinfMph)} mph / ${numberText(vinfMps)} m/s`,
    `M∞ ${numberText(mach)}`,
    `tip ${numberText(tipSpeed)} m/s`,
    `Mhelical ${numberText(helicalMach)}`,
    `J ${numberText(advanceRatio)}`,
    `max wall-clock ${Number.isFinite(maxWall) && maxWall > 0 ? `${maxWall} min / prop` : "No timeout"}`,
    `averaging last ${overnightAverageWindow.value} force samples`,
    `convergence ${convergence.preset}: min ${convergence.minSamples}, N ${convergence.windowSize}, thrust ${convergence.thrustChange}%, torque ${convergence.torqueChange}%, CV ${convergence.thrustCv}/${convergence.torqueCv}%`,
    `continue after failed ${overnightContinueFailed.checked ? "yes" : "no"}`,
    `template compressible_forward_mrf`
  ].join(" | ");
  overnightVariantsBody.innerHTML = "";
  if (!rows.length) {
    overnightVariantsBody.innerHTML = '<tr><td colspan="16">No STL files detected.</td></tr>';
    lastImportError = "No STL files found in imported folder/zip.";
    updateBatchDebugLine();
    setBatchRunButtonState();
    return;
  }
  const warnings = [];
  for (const row of rows) {
    if ((row.warning_flags || "").includes("METADATA_RPM_VINF_IGNORED") || (row.warning_flags || "").includes("METADATA_FLOW_FIELDS_IGNORED")) {
      warnings.push(`${row.variant_id}: Metadata contains rpm/Vinf fields, but global UI settings will be used.`);
    }
    const tr = document.createElement("tr");
    const checkboxCell = document.createElement("td");
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = overnightSelected.has(row.variant_id);
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) overnightSelected.add(row.variant_id);
      else overnightSelected.delete(row.variant_id);
      updateBatchDebugLine();
      renderOvernightManifest(overnightManifest);
    });
    checkboxCell.appendChild(checkbox);
    tr.appendChild(checkboxCell);
    for (const value of [
      row.variant_id,
      row.original_stl_filename,
      row.imported_stl_path || "",
      row.case_triSurface_stl_path || "",
      row.json_found ? "yes" : "no",
      row.pitch || "",
      row.root_chord || "",
      row.peak_chord || "",
      row.tip_chord || "",
      `${numberText(row.diameter_m)} / ${numberText(row.radius_m)}`,
      overnightRpm.value,
      `${numberText(vinfMph)} mph / ${numberText(vinfMps)} m/s`,
      row.geometry_sanity_status || "Not checked",
      row.diameter_estimate_m ? `${numberText(row.diameter_estimate_m)} m, c=(${numberText(row.center_x)},${numberText(row.center_y)},${numberText(row.center_z)})` : "",
      row.warning_flags || ""
    ]) {
      const td = document.createElement("td");
      td.textContent = value;
      tr.appendChild(td);
    }
    overnightVariantsBody.appendChild(tr);
  }
  if (mach > 0.8) warnings.push("freestream_Mach > 0.8");
  if (helicalMach > 1.1) warnings.push("helical_tip_Mach > 1.1");
  else if (helicalMach > 0.95) warnings.push("helical_tip_Mach > 0.95");
  if (batchProfile !== "production_high_resolution") warnings.push("Batch Run is not using the high-resolution production mesh.");
  renderList(overnightWarnings, [...new Set(warnings)]);
  updateBatchDebugLine();
  setBatchRunButtonState();
}

async function importOvernight() {
  try {
    lastImportError = "";
    lastBackendStatus = "importing";
    updateBatchDebugLine();
    let manifest;
    const file = overnightZip.files[0];
    if (file) {
      const response = await fetch(`/api/batch/import-upload?filename=${encodeURIComponent(file.name)}`, {
        method: "POST",
        headers: { "Content-Type": "application/zip" },
        body: await file.arrayBuffer()
      });
      manifest = await response.json();
      if (!response.ok) throw new Error(manifest.detail || response.statusText);
    } else {
      manifest = await apiPost("/api/batch/import", { path: overnightPath.value });
    }
    manifest = normalizeBatchManifest(manifest);
    console.info("Batch import response", manifest);
    if (!(manifest.variants || []).length) {
      throw new Error("No STL files found in imported folder/zip.");
    }
    overnightSelectionInitialized = false;
    overnightSelected = new Set((manifest.variants || []).map((row) => row.variant_id));
    renderOvernightManifest(manifest);
    modeNote.textContent = "Batch geometry imported. Review variants before the fast geometry check.";
    lastBackendStatus = `imported ${manifest.variants.length}`;
    updateBatchDebugLine();
  } catch (error) {
    console.error("Batch import failed", error);
    lastImportError = error.message;
    lastBackendStatus = "import failed";
    modeNote.textContent = `Import failed: ${error.message}`;
    updateBatchDebugLine();
    setBatchRunButtonState();
  }
}

async function loadLatestBatchManifest() {
  if (overnightManifest) return;
  try {
    const manifest = await apiGet("/api/batch-latest-manifest");
    const normalized = normalizeBatchManifest(manifest);
    overnightSelectionInitialized = false;
    overnightSelected = new Set((normalized.variants || []).filter((row) => row.selected !== false).map((row) => row.variant_id));
    renderOvernightManifest(normalized);
    modeNote.textContent = `Loaded imported Batch Run: ${normalized.batch_id}`;
  } catch (_error) {
    // No previous import is a normal first-run state.
  }
}

async function runOvernightSanity() {
  if (!overnightManifest) {
    modeNote.textContent = "Import a Batch Run first.";
    return;
  }
  await startTask("/api/batch-geometry-check", overnightPayload());
}

async function runOvernightBatch() {
  lastStartError = "";
  updateBatchDebugLine();
  if (!overnightManifest) {
    lastStartError = "Import a Batch Run first.";
    modeNote.textContent = lastStartError;
    updateBatchDebugLine();
    return;
  }
  if (!overnightSelected.size) {
    lastStartError = "Select at least one STL variant before starting Batch Run.";
    modeNote.textContent = lastStartError;
    updateBatchDebugLine();
    return;
  }
  if (!Number.isFinite(Number(overnightRpm.value)) || Number(overnightRpm.value) <= 0) {
    lastStartError = "Set a global rpm greater than zero before starting Batch Run.";
    modeNote.textContent = lastStartError;
    updateBatchDebugLine();
    return;
  }
  if (!Number.isFinite(Number(overnightSpeed.value)) || Number(overnightSpeed.value) < 0) {
    lastStartError = "Set a non-negative global freestream speed before starting Batch Run.";
    modeNote.textContent = lastStartError;
    updateBatchDebugLine();
    return;
  }
  const selectedRows = (overnightManifest.variants || []).filter((row) => overnightSelected.has(row.variant_id));
  const hardFailure = selectedRows.find((row) => String(row.geometry_sanity_status || "").endsWith("Fail"));
  if (hardFailure) {
    lastStartError = `${hardFailure.variant_id} has a hard geometry/mesh failure. Fix or deselect it before starting.`;
    modeNote.textContent = lastStartError;
    updateBatchDebugLine();
    return;
  }
  const missingStl = selectedRows.find((row) => !row.imported_stl_path);
  if (missingStl) {
    lastStartError = `${missingStl.variant_id} is missing an imported STL path.`;
    modeNote.textContent = lastStartError;
    updateBatchDebugLine();
    return;
  }
  const ok = window.confirm("Start Batch Run? This will run selected geometries sequentially at one global RPM and freestream condition.");
  if (!ok) {
    lastBackendStatus = "start cancelled";
    updateBatchDebugLine();
    return;
  }
  lastBackendStatus = "starting";
  updateBatchDebugLine();
  console.info("Batch start payload", overnightPayload());
  const response = await startTask("/api/batch/start", overnightPayload());
  if (response) {
    renderBatchStartResponse(response);
  }
}


async function refreshVariants() {
  const data = await apiGet("/api/variants");
  variants = data.variants || [];
  const previous = validationVariant.value;
  validationVariant.innerHTML = "";
  for (const row of variants) {
    const option = document.createElement("option");
    option.value = row.variant_id;
    option.textContent = `${row.variant_id} (${row.stl_file})`;
    validationVariant.appendChild(option);
  }
  if (previous && variants.some((row) => row.variant_id === previous)) validationVariant.value = previous;
}

async function updateValidationSummary() {
  if (!validationVariant.value) {
    validationSummary.textContent = "No variants found in variants.csv.";
    return;
  }
  try {
    const summary = await apiPost("/api/validation-summary", validationPayload());
    validationSummary.textContent = [
      `${summary.validation_case}`,
      `${summary.solver_mode}`,
      `${numberText(summary.Vinf_mph)} mph / ${numberText(summary.Vinf_mps)} m/s`,
      `${numberText(summary.rpm)} rpm`,
      `M∞ ${numberText(summary.freestream_Mach)}`,
      `tip ${numberText(summary.tip_speed_mps)} m/s`,
      `Mhelical ${numberText(summary.helical_tip_Mach)}`,
      `quick endTime ${numberText(summary.validation_endTime_s)} s`,
      `stops at ${summary.validation_force_samples} force samples / ${summary.validation_target_timesteps} steps / ${Math.round(summary.validation_wall_clock_limit_s / 60)} min`,
      summary.mesh_action
    ].join(" | ");
    renderList(validationWarnings, []);
  } catch (error) {
    validationSummary.textContent = error.message;
    renderList(validationWarnings, []);
  }
}

function updateCaseState() {
  const detail = caseDetails.get(selectedCaseName());
  if (!detail) {
    caseMeshState.textContent = "mesh status unknown";
    selectedCase.textContent = selectedCaseName() || "none";
    return;
  }
  const rpm = detail.rpm ? `, ${detail.rpm} rpm` : "";
  const vinf = detail.Vinf_mps ? `, Vinf ${detail.Vinf_mps} m/s` : "";
  const mode = detail.solver_mode ? `, ${detail.solver_mode}` : "";
  caseMeshState.textContent = detail.has_mesh ? `mesh ready${rpm}${vinf}${mode}` : `mesh missing${rpm}${vinf}${mode}`;
  caseMeshState.className = detail.has_mesh ? "case-state ready" : "case-state missing";
  selectedCase.textContent = selectedCaseName() || "none";
}

async function refreshCases() {
  const data = await apiGet("/api/cases");
  const previous = caseSelect.value;
  caseDetails = new Map((data.details || []).map((item) => [item.name, item]));
  caseSelect.innerHTML = "";
  for (const name of data.cases || []) {
    const detail = caseDetails.get(name) || {};
    const option = document.createElement("option");
    option.value = name;
    const rpm = detail.rpm ? `${detail.rpm} rpm` : "rpm unknown";
    const vinf = detail.Vinf_mps ? `${detail.Vinf_mps} m/s` : "Vinf unknown";
    option.textContent = `${name} (${rpm}, ${vinf})`;
    caseSelect.appendChild(option);
  }
  if (previous && (data.cases || []).includes(previous)) caseSelect.value = previous;
  updateCaseState();
}

async function prepareValidation() {
  try {
    const report = await apiPost("/api/validation-prepare", validationPayload());
    validationReport.textContent = JSON.stringify(report, null, 2);
    modeNote.textContent = "Validation case prepared. Solver has not been run.";
    await refreshCases();
  } catch (error) {
    validationReport.textContent = error.message;
    modeNote.textContent = error.message;
  }
}

async function runValidation() {
  modeNote.textContent = "Starting single validation case. This is not a full batch.";
  await startTask("/api/validation-run", validationPayload());
}

async function stopValidation() {
  modeNote.textContent = "Stopping validation solver and preserving available force data.";
  try {
    await apiPost("/api/validation-stop");
    await pollStatus();
  } catch (error) {
    modeNote.textContent = error.message;
  }
}

async function startTask(endpoint, body = null) {
  try {
    const response = await apiPost(endpoint, body);
    console.info("Backend task response", endpoint, response);
    if (response.batch_id) {
      latestRecoveryBatchId = response.batch_id;
      modeNote.textContent = `${response.action || "Batch Run"} started: ${response.batch_id}`;
      renderBatchStartResponse(response);
    }
    lastBackendStatus = response.status || "ok";
    lastStartError = "";
    updateBatchDebugLine();
    await pollStatus();
    return response;
  } catch (error) {
    console.error("Backend task failed", endpoint, error);
    lastStartError = error.message;
    lastBackendStatus = "request failed";
    modeNote.textContent = error.message;
    updateBatchDebugLine();
    return null;
  }
}

function parseNum(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function resultWarnings(row) {
  const warnings = [];
  if (row.warning_flags) warnings.push(row.warning_flags);
  const solver = row.solver_mode || "";
  const mach = parseNum(row.freestream_Mach);
  const hmach = parseNum(row.helical_tip_Mach);
  const thrust = parseNum(row.thrust_N ?? row.thrust_N_mean);
  const torque = parseNum(row.torque_Nm ?? row.shaft_torque_Nm_mean);
  const power = parseNum(row.power_W ?? row.power_W_mean);
  const eta = parseNum(row.eta ?? row.eta_mean);
  if (solver === "incompressible_mrf" && mach !== null && mach > 0.3) warnings.push("incompressible Mach > 0.3");
  if (mach !== null && mach > 0.8) warnings.push("freestream Mach > 0.8");
  if (hmach !== null && hmach > 1.1) warnings.push("helical tip Mach > 1.1");
  else if (hmach !== null && hmach > 0.95) warnings.push("helical tip Mach > 0.95");
  if (thrust !== null && thrust < 0) warnings.push("negative thrust");
  if ((torque !== null && torque < 0) || (power !== null && power <= 0)) warnings.push("invalid power");
  if (eta !== null && (eta < 0 || eta > 1)) warnings.push("eta out of range");
  return [...new Set(warnings.filter(Boolean))].join(", ");
}

function rankingScore(row) {
  if ((row.run_status || "") !== "success") return -Infinity;
  const thrust = parseNum(row.thrust_N ?? row.thrust_N_mean);
  const power = parseNum(row.power_W ?? row.power_W_mean);
  const eta = parseNum(row.eta ?? row.eta_mean);
  const hmach = parseNum(row.helical_tip_Mach);
  if (thrust === null || power === null || eta === null || thrust <= 0 || power <= 0) return -Infinity;
  let score = eta * 100 + (thrust / power) * 10;
  if (hmach !== null && hmach > 1.1) score -= 100;
  else if (hmach !== null && hmach > 0.95) score -= 30;
  return score;
}

function setBest(label, row, metric) {
  label.textContent = row ? `${row.variant_id || row.case} (${metric})` : "none";
}

function updateRanking(rows) {
  const successful = rows.filter((row) => (row.run_status || "") === "success");
  const byThrust = [...successful].sort((a, b) => (parseNum(b.thrust_N) || -Infinity) - (parseNum(a.thrust_N) || -Infinity))[0];
  const byEta = [...successful].sort((a, b) => (parseNum(b.eta) || -Infinity) - (parseNum(a.eta) || -Infinity))[0];
  const byOverall = [...rows].sort((a, b) => rankingScore(b) - rankingScore(a))[0];
  setBest(bestThrust, byThrust, byThrust ? `${numberText(byThrust.thrust_N)} N` : "");
  setBest(bestEta, byEta, byEta ? `eta ${numberText(byEta.eta)}` : "");
  setBest(bestOverall, byOverall && rankingScore(byOverall) > -Infinity ? byOverall : null, byOverall ? `score ${numberText(rankingScore(byOverall))}` : "");
}

function renderResults(rows) {
  if (!rows || rows.length === 0) return;
  const sorted = [...rows].sort((a, b) => {
    if ((a.run_status || "") !== (b.run_status || "")) return (a.run_status || "") === "success" ? -1 : 1;
    return rankingScore(b) - rankingScore(a);
  });
  resultsBody.innerHTML = "";
  for (const row of sorted) {
    const tr = document.createElement("tr");
    const warnings = resultWarnings(row);
    if (warnings) tr.classList.add("warn-row");
    for (const key of [
      "variant_id", "solver_mode", "rpm", "Vinf_mps", "Vinf_mph", "thrust_N", "torque_Nm",
      "power_W", "eta", "advance_ratio_J", "freestream_Mach", "tip_speed_mps",
      "helical_tip_Mach", "CT", "CQ", "CP", "run_status"
    ]) {
      const td = document.createElement("td");
      const fallback = {
        thrust_N: row.thrust_N_mean,
        torque_Nm: row.shaft_torque_Nm_mean,
        power_W: row.power_W_mean,
        eta: row.eta_mean
      };
      td.textContent = numberText(row[key] ?? fallback[key]);
      tr.appendChild(td);
    }
    const warningCell = document.createElement("td");
    warningCell.textContent = warnings;
    tr.appendChild(warningCell);
    for (const label of ["View plots", "View force history", "View solver log"]) {
      const td = document.createElement("td");
      const button = document.createElement("button");
      button.className = "table-action";
      button.textContent = label;
      button.addEventListener("click", async () => {
        caseSelect.value = row.case || row.variant_id;
        if (label === "View plots") {
          renderPlots(await apiGet(`/api/solver-timeseries?case=${encodeURIComponent(caseSelect.value)}`), false);
          solverPlotsCard.scrollIntoView({ behavior: "smooth", block: "start" });
        } else if (label === "View force history") {
          const series = await apiGet(`/api/solver-timeseries?case=${encodeURIComponent(caseSelect.value)}`);
          logPanel.textContent = JSON.stringify(series.force_samples || [], null, 2);
          logPanel.scrollIntoView({ behavior: "smooth", block: "start" });
        } else {
          const progress = await apiGet(`/api/solver-progress?case=${encodeURIComponent(caseSelect.value)}`);
          logPanel.textContent = (progress.log_tail || []).join("\n") || progress.solver_log || "No solver log found.";
          logPanel.scrollIntoView({ behavior: "smooth", block: "start" });
        }
      });
      td.appendChild(button);
      tr.appendChild(td);
    }
    resultsBody.appendChild(tr);
  }
  updateRanking(sorted);
}

function renderDiagnostics(items) {
  if (!items || items.length === 0) return;
  diagnosticsList.innerHTML = "";
  for (const item of items) {
    const li = document.createElement("li");
    li.textContent = item;
    diagnosticsList.appendChild(li);
  }
}

function percentText(value) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return "n/a";
  return `${parsed.toFixed(1)}%`;
}

function renderSolverProgress(progress, taskRunning = false) {
  if (!progress || Object.keys(progress).length === 0) {
    solverProgressCard.classList.add("hidden");
    return;
  }
  const status = progress.solver_status || "queued";
  const show = taskRunning || ["running", "solver_running", "completed", "failed", "stopped"].includes(status) || progress.current_time !== undefined;
  solverProgressCard.classList.toggle("hidden", !show);
  if (!show) return;

  solverStatusPill.textContent = status;
  solverStatusPill.className = `pill ${status === "completed" ? "success" : status}`;
  const physicalPercent = Number(progress.physical_percent);
  const timestepPercent = Number(progress.timestep_percent);
  const barPercent = Number.isFinite(physicalPercent) ? physicalPercent : (Number.isFinite(timestepPercent) ? timestepPercent : 0);
  solverProgressBar.style.width = `${Math.max(0, Math.min(100, barPercent))}%`;

  solverCurrentTime.textContent = numberText(progress.current_time);
  solverTargetTime.textContent = numberText(progress.target_endTime);
  solverPhysicalPercent.textContent = percentText(progress.physical_percent);
  solverTimestepPercent.textContent = progress.target_timesteps ? `${progress.timesteps_completed || 0}/${progress.target_timesteps} (${percentText(progress.timestep_percent)})` : "n/a";
  solverDeltaT.textContent = numberText(progress.latest_deltaT);
  solverCoMean.textContent = numberText(progress.courant_mean);
  solverCoMax.textContent = numberText(progress.courant_max);
  solverExecClock.textContent = `${numberText(progress.execution_time_s)} s / ${numberText(progress.clock_time_s)} s`;
  solverLogAge.textContent = progress.latest_log_update_age_s !== undefined && progress.latest_log_update_age_s !== null ? `${numberText(progress.latest_log_update_age_s)} s ago` : "n/a";
  solverForceSamples.textContent = progress.force_sample_count !== undefined ? `${progress.force_sample_count} (${numberText(progress.latest_force_time)})` : (progress.force_status || "force data not available yet");
  solverThrust.textContent = progress.latest_thrust_N !== undefined ? `${numberText(progress.latest_thrust_N)} N` : "n/a";
  solverTorque.textContent = progress.latest_shaft_torque_Nm !== undefined ? `${numberText(progress.latest_shaft_torque_Nm)} Nm` : "n/a";
  const thrustAxis = progress.thrust_axis || "z";
  const thrustSign = progress.thrust_sign !== undefined ? Number(progress.thrust_sign) : null;
  solverRawAxialForce.textContent = progress.raw_force_axis_N !== undefined
    ? `${numberText(progress.raw_force_axis_N)} N (${thrustAxis})`
    : "n/a";
  solverSignedThrust.textContent = progress.latest_thrust_N !== undefined
    ? `${numberText(progress.latest_thrust_N)} N${thrustSign !== null && Number.isFinite(thrustSign) ? ` (sign ${thrustSign > 0 ? "+1" : "-1"})` : ""}`
    : "n/a";
  const convergence = progress.convergence || {};
  solverConvergenceStatus.textContent = convergence.convergence_status || "warming up";
  solverConvergenceThresholds.textContent = [
    convergence.convergence_preset || "fast_screening",
    `min ${convergence.minimum_force_samples ?? "n/a"}`,
    `N ${convergence.convergence_window_size ?? "n/a"}`,
    `thrust change < ${percentText(convergence.thrust_mean_change_threshold_pct)}`,
    `torque change < ${percentText(convergence.torque_mean_change_threshold_pct)}`,
    `CV < ${percentText(convergence.thrust_cv_threshold_pct)} / ${percentText(convergence.torque_cv_threshold_pct)}`
  ].join(" | ");
  solverConvergenceBlockedBy.textContent = (convergence.blocked_by || []).length ? convergence.blocked_by.join(", ") : "none";
  solverConvergenceThrust.textContent = convergence.thrust_mean_change_pct !== null && convergence.thrust_mean_change_pct !== undefined
    ? `mean ${numberText(convergence.latest_thrust_mean_N)} N, change ${percentText(convergence.thrust_mean_change_pct)}, CV ${percentText(convergence.thrust_cv_pct)}`
    : "warming up";
  solverConvergenceTorque.textContent = convergence.torque_mean_change_pct !== null && convergence.torque_mean_change_pct !== undefined
    ? `mean ${numberText(convergence.latest_shaft_torque_mean_Nm)} Nm, change ${percentText(convergence.torque_mean_change_pct)}, CV ${percentText(convergence.torque_cv_pct)}`
    : "warming up";
  solverStopReason.textContent = progress.stop_reason || convergence.stop_reason || "n/a";
  solverValidationRevs.textContent = progress.validation_revolutions ? `${numberText(progress.revolutions_completed)} / ${numberText(progress.validation_revolutions)} (${percentText(progress.validation_revolutions_percent)})` : "n/a";
  solverProgressNote.textContent = progress.validation_note || (["running", "solver_running"].includes(status) ? "Solver is active." : "Solver telemetry from current case log.");
  renderList(solverWarnings, progress.warnings || []);
  solverLogTail.textContent = progress.fatal_excerpt || (progress.log_tail || []).join("\n") || "No solver log yet.";
}

function renderStatus(status) {
  const heartbeat = status.batch_heartbeat || {};
  const batchState = status.batch_state || {};
  statePill.textContent = status.status;
  statePill.className = `pill ${status.status}`;
  currentBatchId.textContent = status.batch_id || (batchState.batch_id || "none");
  currentAction.textContent = status.action || "idle";
  selectedCase.textContent = status.case || selectedCaseName() || "none";
  caseIndex.textContent = `${status.case_index || 0}/${status.case_total || 0}`;
  currentStage.textContent = heartbeat.current_stage || batchState.stage || status.stage || "idle";
  currentPid.textContent = heartbeat.pid || "n/a";
  elapsedTime.textContent = `${status.elapsed_seconds || 0} s`;
  heartbeatStatus.textContent = heartbeat.updated_at ? `${heartbeat.pid_alive ? "alive" : "not running"}${heartbeat.current_stage ? `, ${heartbeat.current_stage}` : ""}` : "n/a";
  if (status.process_alive) {
    heartbeatStatus.textContent = `alive, ${status.recovered_process ? "recovered process" : "tracked process"}`;
  }
  lastLogUpdate.textContent = heartbeat.latest_log_timestamp ? `${numberText((Date.now() / 1000) - heartbeat.latest_log_timestamp)} s ago` : "n/a";
  currentCommand.textContent = heartbeat.current_command || status.command || "none";
  const stageText = String(currentStage.textContent);
  const indeterminate = Boolean(status.running && ["blockMesh", "surfaceFeatureExtract", "snappyHexMesh"].some((stage) => stageText.includes(stage)));
  progressBar.classList.toggle("indeterminate", indeterminate);
  progressBar.style.width = indeterminate ? "100%" : `${status.progress || 0}%`;
  if (status.error) modeNote.textContent = status.error;
  else if (status.process_alive && status.recovered_process) modeNote.textContent = "Recovered active Batch Run process. Monitoring without launching a duplicate.";
  else if (batchState.stop_requested || batchState.stop_after_current) modeNote.textContent = `Batch stop flags: stop_requested=${Boolean(batchState.stop_requested)}, stop_after_current=${Boolean(batchState.stop_after_current)}`;
  const validationRunning = Boolean(status.running && (status.action || "").includes("validate"));
  const overnightRunning = Boolean(status.running && ((status.action || "").includes("Batch Run") || (status.action || "").includes("overnight")));
  for (const button of buttons) {
    button.disabled = Boolean(status.running);
  }
  if (!status.running) setBatchRunButtonState();
  stopValidationButton.disabled = !validationRunning;
  document.querySelector("#overnightStopCurrent").disabled = !overnightRunning;
  document.querySelector("#overnightStopAll").disabled = !overnightRunning;
  if (status.results) renderResults(status.results);
  if (status.diagnostics) renderDiagnostics(status.diagnostics);
  if (status.summary && status.summary.validation_case) {
    validationReport.textContent = JSON.stringify(status.summary, null, 2);
  }
  if (status.summary && (status.summary.batch_sanity || status.summary.overnight_sanity) && status.summary.variants) {
    overnightManifest = { batch_id: status.summary.batch_id, variants: status.summary.variants };
    renderOvernightManifest(overnightManifest);
  }
  lastBackendStatus = status.status || lastBackendStatus;
  updateBatchDebugLine();
  renderSolverProgress(status.solver_progress, Boolean(status.running));
}

async function refreshRecovery() {
  try {
    const data = await apiGet("/api/batch-recovery");
    const batches = data.batches || [];
    recoveryCard.classList.toggle("hidden", batches.length === 0);
    resumeBatchButton.disabled = batches.length === 0;
    latestRecoveryBatchId = batches[0] ? batches[0].batch_id : "";
    if (!batches.length) {
      recoveryList.textContent = "No incomplete batches detected.";
      return;
    }
    recoveryList.innerHTML = "";
    for (const batch of batches) {
      const div = document.createElement("div");
      div.className = "summary-panel";
      const detail = document.createElement("div");
      detail.textContent = [
        `batch_id ${batch.batch_id}`,
        `${batch.completed_variants}/${batch.total_variants} completed`,
        `${batch.failed_variants} failed`,
        `stage ${batch.active_incomplete_stage || "unknown"}`
      ].join(" | ");
      const actions = document.createElement("div");
      for (const [label, endpoint] of [["Resume Batch", "/api/batch-resume"], ["Mark Batch Abandoned", "/api/batch-mark-abandoned"]]) {
        const button = document.createElement("button");
        button.textContent = label;
        button.addEventListener("click", async () => {
          await apiPost(endpoint, { path: batch.batch_id });
          await pollStatus();
          await refreshRecovery();
        });
        actions.appendChild(button);
      }
      div.append(detail, actions);
      recoveryList.appendChild(div);
    }
  } catch (error) {
    recoveryList.textContent = error.message;
  }
}

async function pollStatus() {
  const status = await apiGet("/api/status");
  renderStatus(status);
  if (status.running || !solverProgressCard.classList.contains("hidden")) {
    try {
      renderSolverProgress(await apiGet("/api/solver-progress"), Boolean(status.running));
      renderPlots(await apiGet("/api/solver-timeseries"), Boolean(status.running));
    } catch (error) {
      renderSolverProgress({ solver_status: "queued", warnings: [error.message] }, Boolean(status.running));
    }
  }
  const log = await apiGet("/api/log");
  logPanel.textContent = log.lines.length ? log.lines.join("\n") : "Waiting for task output...";
  statusLogTail.textContent = log.lines.length ? log.lines.slice(-18).join("\n") : "No batch log yet.";
  logPanel.scrollTop = logPanel.scrollHeight;
}

document.querySelector("#overnightImport").addEventListener("click", importOvernight);
document.querySelector("#overnightSelectAll").addEventListener("click", () => {
  if (!overnightManifest) return;
  overnightSelectionInitialized = true;
  overnightSelected = new Set((overnightManifest.variants || []).map((row) => row.variant_id));
  updateBatchDebugLine();
  renderOvernightManifest(overnightManifest);
});
document.querySelector("#overnightSelectNone").addEventListener("click", () => {
  overnightSelectionInitialized = true;
  overnightSelected = new Set();
  updateBatchDebugLine();
  if (overnightManifest) renderOvernightManifest(overnightManifest);
});
document.querySelector("#overnightSanity").addEventListener("click", runOvernightSanity);
document.querySelector("#overnightRun").addEventListener("click", runOvernightBatch);
document.querySelector("#overnightStopCurrent").addEventListener("click", () => startTask("/api/batch-stop-current"));
document.querySelector("#overnightStopAll").addEventListener("click", () => startTask("/api/batch-stop-all"));
resumeBatchButton.addEventListener("click", async () => {
  if (!latestRecoveryBatchId && overnightManifest) latestRecoveryBatchId = overnightManifest.batch_id;
  if (!latestRecoveryBatchId) return;
  await startTask("/api/batch-resume", { path: latestRecoveryBatchId });
});
document.querySelector("#prepareValidation").addEventListener("click", prepareValidation);
document.querySelector("#runValidation").addEventListener("click", runValidation);
stopValidationButton.addEventListener("click", stopValidation);
document.querySelectorAll(".plot-tab").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll(".plot-tab").forEach((item) => item.classList.remove("active"));
    button.classList.add("active");
    activePlotTab = button.dataset.plot;
    renderPlots(lastTimeseries, !solverPlotsCard.classList.contains("hidden"));
  });
});
for (const input of [plotWindow, showMovingAverage, averageWindow]) {
  input.addEventListener("change", () => renderPlots(lastTimeseries, !solverPlotsCard.classList.contains("hidden")));
  input.addEventListener("input", () => renderPlots(lastTimeseries, !solverPlotsCard.classList.contains("hidden")));
}
document.querySelector("#debugMesh").addEventListener("click", () => startTask("/api/mesh", { case: selectedCaseName() }));
document.querySelector("#debugSolveOnly").addEventListener("click", () => startTask("/api/solve-only", { case: selectedCaseName() }));
document.querySelector("#debugMeshOnlyBatch").addEventListener("click", () => startTask("/api/batch-mesh"));
document.querySelector("#diagnoseCase").addEventListener("click", () => startTask("/api/diagnose", { case: selectedCaseName() }));
document.querySelector("#debugRebuildSolve").addEventListener("click", () => startTask("/api/rebuild-and-solve", { case: selectedCaseName() }));
document.querySelector("#debugParseCase").addEventListener("click", () => startTask("/api/parse", { case: selectedCaseName() }));
async function flipForceSign(payload) {
  try {
    const response = await apiPost("/api/force-signs", { case: selectedCaseName(), ...payload });
    modeNote.textContent = `Updated force sign metadata for ${response.case}. Reparsed ${response.summary?.samples_used || 0} samples.`;
    renderSolverProgress(await apiGet(`/api/solver-progress?case=${encodeURIComponent(selectedCaseName())}`), false);
    renderPlots(await apiGet(`/api/solver-timeseries?case=${encodeURIComponent(selectedCaseName())}`), false);
    await pollStatus();
  } catch (error) {
    modeNote.textContent = error.message;
  }
}
document.querySelector("#debugFlipThrust").addEventListener("click", () => flipForceSign({ flip_thrust: true, flip_torque: false }));
document.querySelector("#debugFlipTorque").addEventListener("click", () => flipForceSign({ flip_thrust: false, flip_torque: true }));
document.querySelector("#refreshCases").addEventListener("click", refreshCases);
overnightConvergencePreset.addEventListener("change", () => {
  applyConvergencePresetToInputs();
  if (overnightManifest) renderOvernightManifest(overnightManifest);
});
for (const input of [
  overnightRpm, overnightSpeed, overnightSpeedUnits, overnightTinf, overnightPinf, overnightMaxMinutes, overnightAverageWindow,
  convergenceMinSamples, convergenceWindowSize, convergenceThrustChange, convergenceTorqueChange, convergenceThrustCv, convergenceTorqueCv
]) {
  input.addEventListener("change", () => {
    if ([convergenceMinSamples, convergenceWindowSize, convergenceThrustChange, convergenceTorqueChange, convergenceThrustCv, convergenceTorqueCv].includes(input)) {
      overnightConvergencePreset.value = "custom";
    }
    if (overnightManifest) renderOvernightManifest(overnightManifest);
    else setBatchRunButtonState();
  });
  input.addEventListener("input", () => {
    if ([convergenceMinSamples, convergenceWindowSize, convergenceThrustChange, convergenceTorqueChange, convergenceThrustCv, convergenceTorqueCv].includes(input)) {
      overnightConvergencePreset.value = "custom";
    }
    if (overnightManifest) renderOvernightManifest(overnightManifest);
    else setBatchRunButtonState();
  });
}
caseSelect.addEventListener("change", updateCaseState);
validationVariant.addEventListener("change", updateValidationSummary);
validationPreset.addEventListener("change", updateValidationSummary);
validationRpm.addEventListener("input", updateValidationSummary);

Promise.all([refreshVariants(), refreshCases(), refreshRecovery(), loadLatestBatchManifest()]).then(updateValidationSummary).catch((error) => {
  modeNote.textContent = error.message;
});
applyConvergencePresetToInputs();
setBatchRunButtonState();
pollStatus().catch(() => {});
setInterval(pollStatus, 1500);
setInterval(refreshRecovery, 15000);
