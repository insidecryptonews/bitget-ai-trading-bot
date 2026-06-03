(function () {
  "use strict";

  const params = new URLSearchParams(window.location.search);
  const token = params.get("token") || "";
  const state = {
    status: null,
    lastFullReportText: "",
    lastShortReportText: "",
    lastReportGeneratedAt: "",
    trainingSummary: null,
  };

  const MAIN_ANALYSIS_STEPS = [
    { name: "Training Summary", url: "/api/training/summary?hours=6", target: "edgePolicyOutput", handler: handleSummary },
    { name: "Time Death Autopsy", url: "/api/training/time-death-autopsy?hours=24", target: "timeDeathOutput", handler: handleTimeDeath },
    { name: "Candidate Ranking", url: "/api/training/candidate-ranking?hours=24", target: "edgePolicyOutput", append: true, handler: handleCandidateRanking },
    { name: "Score Calibration", url: "/api/training/score-calibration?hours=24", target: "scoreIncubatorOutput", handler: handleScoreCalibration },
    { name: "Candidate Incubator", url: "/api/training/candidate-incubator?hours=24", target: "scoreIncubatorOutput", append: true, handler: handleCandidateIncubator },
    { name: "Training Data Integrity", url: "/api/training/training-data-integrity?hours=24", target: "scoreIncubatorOutput", append: true },
    { name: "Core Corrections", url: "/api/training/core-corrections?hours=24", target: "pipelineCostOutput", handler: handleCoreCorrections },
    { name: "Execution Safety", url: "/api/training/execution-safety-audit", target: "executionSafetyOutput", handler: handleExecutionSafety },
    { name: "Operational Intelligence", url: "/api/training/operational-intelligence-audit?hours=24", target: "operationalIntelligenceOutput", handler: handleOperationalIntelligence },
    { name: "Data Pipeline Diagnosis", url: "/api/training/data-pipeline-diagnosis?hours=24", target: "pipelineCostOutput", handler: handleDataPipelineDiagnosis },
    { name: "Label Quality V2", url: "/api/training/label-quality-v2?hours=24", target: "pipelineCostOutput", append: true, handler: handleLabelQualityV2 },
    { name: "Bitget Cost Model", url: "/api/training/bitget-cost-model-audit?hours=24", target: "pipelineCostOutput", append: true, handler: handleBitgetCostModel },
    { name: "Margin Mode Audit", url: "/api/training/margin-mode-audit", target: "pipelineCostOutput", append: true, handler: handleMarginMode },
    { name: "Worker Health Audit", url: "/api/training/worker-health-audit", target: "runtimeOutput", append: true },
    { name: "Dashboard Data Binding", url: "/api/training/dashboard-data-binding-audit", target: "runtimeOutput", append: true },
    { name: "Edge Guard", url: "/api/training/edge-guard?hours=24", target: "edgePolicyOutput", append: true, handler: handleEdgeGuard },
    { name: "Orchestrator", url: "/api/training/paper-policy-orchestrator?hours=24", target: "edgePolicyOutput", append: true, handler: handleOrchestrator },
    { name: "Pre-Move", url: "/api/training/pre-move-event-labeler?hours=24", target: "preMoveOutput", handler: handlePreMove },
    { name: "Latency", url: "/api/training/latency-audit?hours=24", target: "runtimeOutput", handler: handleLatency },
    { name: "Data Vault Status", url: "/api/training/data-vault-status", target: "dataVaultOutput", handler: handleVault },
    { name: "Data Vault Audit", url: "/api/training/data-vault-audit", target: "dataVaultOutput", append: true },
    { name: "Exit Calibration V2", url: "/api/training/exit-label-calibration-v2?hours=24", target: "exitCalibrationOutput", handler: handleExitCalibration },
  ];

  const $ = (id) => document.getElementById(id);
  const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
  const num = (value, fallback = 0) => Number.isFinite(Number(value)) ? Number(value) : fallback;
  const pct = (ratio) => `${(num(ratio) * 100).toFixed(1)}%`;
  const pendingText = (label = "pendiente") => label;
  const fmt = (value, digits = 2) => num(value).toFixed(digits);
  const safeText = (value, fallback = "n/a") => {
    if (value === null || value === undefined || value === "") return fallback;
    return String(value);
  };
  const escapeHtml = (value) => safeText(value, "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "\"": "&quot;",
    "'": "&#039;",
  }[char]));

  function apiUrl(path) {
    if (!token) return path;
    return `${path}${path.includes("?") ? "&" : "?"}token=${encodeURIComponent(token)}`;
  }

  async function fetchJson(path) {
    const response = await fetch(apiUrl(path), { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
  }

  async function fetchText(path) {
    const response = await fetch(apiUrl(path), { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.text();
  }

  function setText(id, text) {
    const node = $(id);
    if (node) node.textContent = safeText(text, "");
  }

  function setHtml(id, html) {
    const node = $(id);
    if (node) node.innerHTML = html;
  }

  function setButtonLoading(button, loading, label) {
    if (!button) return;
    if (!button.dataset.label) button.dataset.label = button.textContent;
    button.disabled = loading;
    button.classList.toggle("loading", loading);
    button.textContent = loading ? (label || "Cargando...") : button.dataset.label;
  }

  function localTimes() {
    const now = new Date();
    setText("utcTimeTag", `UTC: ${new Intl.DateTimeFormat("es-ES", {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      timeZone: "UTC",
    }).format(now)}`);
    setText("madridTimeTag", `Madrid: ${new Intl.DateTimeFormat("es-ES", {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      timeZone: "Europe/Madrid",
    }).format(now)}`);
  }

  function renderKpiCard(item) {
    const stateClass = item.state === "bad" ? "danger-text" : item.state === "watch" ? "warning-text" : item.state === "ok" ? "safe-text" : "info-text";
    return `
      <article class="kpi-card">
        <div class="kpi-label">${escapeHtml(item.label)}</div>
        <div class="kpi-value ${stateClass}">${escapeHtml(item.value)}</div>
        <div class="kpi-note">${escapeHtml(item.note || "")}</div>
      </article>`;
  }

  function renderStatusPill(label, state) {
    const badge = state === "ok" ? "badge-safe" : state === "bad" ? "badge-danger" : state === "watch" ? "badge-warning" : "badge-muted";
    return `<span class="badge ${badge}">${escapeHtml(label)}</span>`;
  }

  function renderReadinessGrid(items) {
    return items.map((item) => `
      <article class="readiness-card state-${escapeHtml(item.state)}">
        <span class="panel-label">${escapeHtml(item.label)}</span>
        <strong>${escapeHtml(item.value)}</strong>
        <small>${escapeHtml(item.note || "")}</small>
      </article>`).join("");
  }

  function renderStackedBar(parts) {
    const total = parts.reduce((sum, item) => sum + Math.max(0, num(item.value)), 0);
    if (total <= 0) return renderEmptyState("Sin datos recientes");
    const segments = parts.map((item) => {
      const percent = (Math.max(0, num(item.value)) / total) * 100;
      return `<div class="stack-segment ${escapeHtml(item.className || "")}" style="width:${percent}%">${escapeHtml(item.label)} ${percent.toFixed(1)}%</div>`;
    }).join("");
    return `<div class="stacked-bar">${segments}</div>`;
  }

  function renderHorizontalBarChart(rows) {
    const max = Math.max(1, ...rows.map((row) => Math.abs(num(row.value))));
    if (!rows.length || rows.every((row) => num(row.value) === 0)) return renderEmptyState("Sin datos recientes");
    return rows.map((row) => {
      const width = clamp((Math.abs(num(row.value)) / max) * 100, 2, 100);
      return `
        <div class="bar-row">
          <div class="bar-label">${escapeHtml(row.label)}</div>
          <div class="bar-track"><div class="bar-fill ${escapeHtml(row.color || "")}" style="width:${width}%"></div></div>
          <div class="bar-value">${escapeHtml(row.display ?? row.value)}</div>
        </div>`;
    }).join("");
  }

  function renderMiniBars(rows) {
    return renderHorizontalBarChart(rows);
  }

  function renderEmptyState(text) {
    return `<div class="empty-state">${escapeHtml(text || "Sin datos")}</div>`;
  }

  function metricRow(label, value) {
    return `<div class="metric-row"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`;
  }

  function listItems(rows, emptyText) {
    if (!rows || !rows.length) return renderEmptyState(emptyText || "Sin datos recientes");
    return rows.slice(0, 8).map((row) => `
      <div class="list-item">
        <strong>${escapeHtml(row.title || row.symbol || row.reason || row.group || "item")}</strong>
        <span>${escapeHtml(row.subtitle || row.side || row.count || row.decision || row.reason || "")}</span>
      </div>`).join("");
  }

  function renderStatus(data) {
    state.status = data;
    const safety = data.safety || {};
    const health = data.health || {};
    const labels = data.labels || {};
    const signals = data.signals || {};
    const paper = data.paper || {};
    const mfe = data.mfe_mae || {};
    const finalRecommendation = safeText(data.final_recommendation, "NO LIVE");
    const online = true;
    $("onlineBadge").textContent = online ? "online" : "offline";
    $("onlineBadge").className = `badge ${online ? "badge-safe" : "badge-danger"}`;
    setText("gitVersionTag", `git: ${safeText(data.git_version, "unknown")}`);
    setText("lastRefreshTag", `refresh: ${new Date().toLocaleTimeString("es-ES")}`);
    setText("workerStatusTag", `worker: ${num(health.cycles_error) > 0 ? "warning" : "ok"}`);
    setText("finalRecommendationHero", finalRecommendation);

    const labelsTotal = num(labels.total, NaN);
    const labelsReady = Number.isFinite(labelsTotal) && labelsTotal > 0;
    const summaryReady = Boolean(state.trainingSummary);
    const timeRatio = summaryReady ? state.trainingSummary.time / 100 : labelsReady ? num(labels.time_ratio) : NaN;
    const tpRatio = summaryReady ? state.trainingSummary.tp / 100 : labelsReady ? num(labels.tp_ratio) : NaN;
    const slRatio = summaryReady ? state.trainingSummary.sl / 100 : labelsReady ? num(labels.sl_ratio) : NaN;
    const labelNote = summaryReady ? "Training Summary manual" : labelsReady ? `${num(labels.time)} TIME / ${num(labels.total)} labels` : "pendiente: usa Training Summary manual";
    const timeDisplay = Number.isFinite(timeRatio) ? pct(timeRatio) : pendingText();
    const tpDisplay = Number.isFinite(tpRatio) ? pct(tpRatio) : pendingText();
    const slDisplay = Number.isFinite(slRatio) ? pct(slRatio) : pendingText();
    const pfDisplay = summaryReady ? fmt(state.trainingSummary.pf, 2) : pendingText("research");
    const candidateState = inferCandidateState();
    const mainProblem = Number.isFinite(timeRatio) && timeRatio > 0.8 ? "TIME death alto / no valid candidates" : candidateState === "NO_VALID_CANDIDATES" ? "No valid candidates" : "Keep research";
    setText("mainProblemHero", mainProblem);
    setText("heroMessage", `${finalRecommendation}. No activar live ni filtros paper. Estado rapido: ${mainProblem}.`);

    const kpis = [
      { label: "PF 6h", value: pfDisplay, note: summaryReady ? "Training Summary cargado" : "usa Training Summary manual", state: summaryReady && state.trainingSummary.pf < 1 ? "bad" : "watch" },
      { label: "TIME ratio", value: timeDisplay, note: labelNote, state: Number.isFinite(timeRatio) ? timeRatio > 0.8 ? "bad" : timeRatio > 0.5 ? "watch" : "ok" : "watch" },
      { label: "TP ratio", value: tpDisplay, note: "TP1 + TP2", state: Number.isFinite(tpRatio) ? tpRatio <= 0.05 ? "bad" : "ok" : "watch" },
      { label: "SL ratio", value: slDisplay, note: "stop-loss outcomes", state: Number.isFinite(slRatio) ? slRatio > 0.25 ? "bad" : "watch" : "watch" },
      { label: "Candidate status", value: candidateState, note: "ranking/orchestrator manual", state: candidateState === "NO_VALID_CANDIDATES" ? "bad" : "watch" },
      { label: "Net EV est.", value: "not loaded", note: "Net Edge Lab manual", state: "watch" },
      { label: "Open paper", value: safeText(paper.open_positions, "0"), note: "simulated positions", state: num(paper.open_positions) > 0 ? "watch" : "ok" },
      { label: "Worker health", value: num(health.cycles_error) > 0 ? "WARNING" : "OK", note: `429=${num(health.api_429_count)} errors=${num(health.api_error_count)}`, state: num(health.cycles_error) > 0 || num(health.api_error_count) > 0 ? "watch" : "ok" },
    ];
    setHtml("overviewKpis", kpis.map(renderKpiCard).join(""));

    const readiness = [
      { label: "Safety", value: safety.live_trading ? "DANGER" : "OK", state: safety.live_trading ? "bad" : "ok", note: "LIVE must stay false" },
      { label: "Worker", value: num(health.cycles_error) > 0 ? "WARNING" : "OK", state: num(health.cycles_error) > 0 ? "watch" : "ok", note: `${safeText(health.uptime_min, 0)} min uptime` },
      { label: "Backups", value: "CHECK", state: "watch", note: "Data Vault manual" },
      { label: "Data quality", value: labelsReady || summaryReady ? "OBSERVING" : "PENDIENTE", state: labelsReady || summaryReady ? "watch" : "watch", note: labelsReady ? "labels window" : "manual refresh required" },
      { label: "TIME", value: Number.isFinite(timeRatio) ? timeRatio > 0.8 ? "BAD" : "WATCH" : "PENDIENTE", state: Number.isFinite(timeRatio) && timeRatio > 0.8 ? "bad" : "watch", note: Number.isFinite(timeRatio) ? pct(timeRatio) : "not loaded" },
      { label: "Net Edge", value: "WATCH", state: "watch", note: "requires net labs" },
      { label: "Candidates", value: candidateState, state: candidateState === "NO_VALID_CANDIDATES" ? "bad" : "watch", note: "manual refresh" },
      { label: "Live readiness", value: "NO LIVE", state: "bad", note: "paper/research only" },
    ];
    setHtml("readinessGrid", renderReadinessGrid(readiness));

    setHtml("outcomeStackedChart", summaryReady ? renderStackedBar([
      { label: "TIME", value: state.trainingSummary.time, className: "time" },
      { label: "SL", value: state.trainingSummary.sl, className: "sl" },
      { label: "TP", value: state.trainingSummary.tp, className: "tp" },
    ]) : labelsReady ? renderStackedBar([
      { label: "TIME", value: num(labels.time), className: "time" },
      { label: "SL", value: num(labels.sl), className: "sl" },
      { label: "TP", value: num(labels.tp1) + num(labels.tp2), className: "tp" },
    ]) : renderEmptyState("Pendiente: ejecuta Training Summary o analisis principal"));
    $("timeRiskBadge").textContent = Number.isFinite(timeRatio) ? timeRatio > 0.8 ? "TIME BAD" : "watch" : "pending";
    $("timeRiskBadge").className = `badge ${Number.isFinite(timeRatio) && timeRatio > 0.8 ? "badge-danger" : "badge-warning"}`;
    setHtml("signalBarChart", renderHorizontalBarChart([
      { label: "LONG", value: num(signals.long), color: "green", display: num(signals.long) },
      { label: "SHORT", value: num(signals.short), color: "red", display: num(signals.short) },
      { label: "NO_TRADE", value: num(signals.no_trade), color: "orange", display: num(signals.no_trade) },
    ]));
    setHtml("candidateStatusChart", renderHorizontalBarChart([
      { label: "NO_VALID_CANDIDATES", value: candidateState === "NO_VALID_CANDIDATES" ? 1 : 0, color: "red", display: candidateState },
      { label: "WATCH_ONLY", value: 1, color: "orange", display: "research" },
      { label: "BLOCKED", value: 1, color: "red", display: "no live" },
    ]));
    setHtml("timeDeathMiniChart", renderMiniBars([
      { label: "TIME", value: Number.isFinite(timeRatio) ? timeRatio * 100 : 0, color: Number.isFinite(timeRatio) && timeRatio > 0.8 ? "red" : "orange", display: Number.isFinite(timeRatio) ? pct(timeRatio) : "pending" },
      { label: "TP", value: Number.isFinite(tpRatio) ? tpRatio * 100 : 0, color: "green", display: Number.isFinite(tpRatio) ? pct(tpRatio) : "pending" },
      { label: "SL", value: Number.isFinite(slRatio) ? slRatio * 100 : 0, color: "red", display: Number.isFinite(slRatio) ? pct(slRatio) : "pending" },
    ].filter((row) => Number.isFinite(row.value) && row.value > 0)));
    setHtml("timeDeathChart", $("outcomeStackedChart").innerHTML);

    setHtml("paperSummaryCards", [
      ["open", paper.open_positions],
      ["opened", paper.open_success],
      ["failed", paper.open_fail],
      ["reconcile", paper.reconcile_runs],
      ["closed label", paper.reconcile_closed_label],
      ["closed time", paper.reconcile_closed_time],
    ].map(([label, value]) => `<div class="mini-kpi"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value ?? 0)}</strong></div>`).join(""));
    setHtml("paperOutcomeChart", renderHorizontalBarChart([
      { label: "Open", value: num(paper.open_positions), color: "blue", display: num(paper.open_positions) },
      { label: "Open success", value: num(paper.open_success), color: "green", display: num(paper.open_success) },
      { label: "Open fail", value: num(paper.open_fail), color: "red", display: num(paper.open_fail) },
    ]));
    renderOpenPaperPosition(data.open_paper_positions_detail || []);
    setHtml("topSignalsList", listItems((data.top_signals || []).map((row) => ({
      title: `${row.symbol || "NA"} ${row.side || ""} score=${row.score || row.confidence_score || "NA"}`,
      subtitle: row.reason || row.strategy_type || "",
    })), "Sin top signals recientes"));
    setHtml("topBlocksList", listItems((data.top_blocks || []).map((row) => ({
      title: row.reason || "block",
      subtitle: `count=${row.count || 0}`,
    })), "Sin blocks recientes"));
    setHtml("runtimeMetrics", [
      metricRow("uptime", `${safeText(health.uptime_min, 0)} min`),
      metricRow("cycles ok/error", `${num(health.cycles_ok)} / ${num(health.cycles_error)}`),
      metricRow("API 429/errors", `${num(health.api_429_count)} / ${num(health.api_error_count)}`),
      metricRow("memory", `${fmt(health.memory_mb_last)} MB last / ${fmt(health.memory_mb_max)} MB max`),
      metricRow("worker lock", safeText(data.worker_lock && data.worker_lock.status, "unknown")),
    ].join(""));
    setHtml("latencyChart", renderHorizontalBarChart([
      { label: "cycle p50", value: 0, color: "blue", display: "run audit" },
      { label: "market p95", value: 0, color: "orange", display: "manual" },
      { label: "decision p99", value: 0, color: "purple", display: "manual" },
    ]));
    renderMfeMae(mfe);
    renderVaultCards(data);
  }

  function renderMfeMae(mfe) {
    const items = [
      { label: "MFE active", value: safeText(mfe.active, 0), note: "active paths", state: "info" },
      { label: "MFE matured", value: safeText(mfe.matured, 0), note: "matured paths", state: num(mfe.matured) > 0 ? "ok" : "watch" },
      { label: "Coverage", value: pct(mfe.coverage_pct), note: "path coverage", state: num(mfe.coverage_pct) > 0.6 ? "ok" : "watch" },
    ];
    const existing = $("overviewKpis");
    if (existing && !existing.dataset.mfeMerged) {
      existing.dataset.mfeMerged = "1";
    }
  }

  function renderVaultCards(data) {
    const vault = data.vps_migration || {};
    const lock = data.worker_lock || {};
    setHtml("vaultCards", [
      { label: "R2 configured", value: vault.r2_configured === false ? "NO" : "CHECK", note: "data vault status manual", state: "watch" },
      { label: "Upload verified", value: vault.r2_last_upload_verified === true ? "YES" : "CHECK", note: "no secrets shown", state: vault.r2_last_upload_verified ? "ok" : "watch" },
      { label: "Worker lock", value: safeText(lock.status, "unknown"), note: "single worker guard", state: "watch" },
    ].map(renderKpiCard).join(""));
  }

  function renderOpenPaperPosition(rows) {
    const first = rows && rows[0];
    $("paperOpenBadge").textContent = first ? "open" : "none";
    $("paperOpenBadge").className = `badge ${first ? "badge-warning" : "badge-muted"}`;
    if (!first) {
      setHtml("paperPositionCard", "Sin posicion paper abierta.");
      $("paperPositionCard").className = "position-card empty-state";
      return;
    }
    $("paperPositionCard").className = "position-card";
    const fields = [
      ["symbol", first.symbol],
      ["side", first.side],
      ["entry", first.entry_price],
      ["SL", first.stop_loss],
      ["TP1", first.take_profit_1],
      ["TP2", first.take_profit_2],
      ["score", first.score],
      ["strategy", first.strategy],
      ["opened", first.opened_at],
      ["PnL", first.unrealized_pnl],
    ];
    setHtml("paperPositionCard", fields.map(([label, value]) => `
      <div class="position-item"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value ?? "n/a")}</strong></div>`).join(""));
  }

  function inferCandidateState() {
    return "NO_VALID_CANDIDATES";
  }

  async function loadStatus() {
    try {
      renderStatusFallback();
      const data = await fetchJson("/api/training/status");
      renderStatus(data);
    } catch (error) {
      $("onlineBadge").textContent = "offline";
      $("onlineBadge").className = "badge badge-danger";
      setText("heroMessage", `No se pudo cargar estado rapido: ${error.message}. UI V3 sigue sin ejecutar acciones peligrosas.`);
    }
  }

  function renderStatusFallback() {
    if ($("overviewKpis").children.length) return;
    setHtml("overviewKpis", [
      { label: "PF 6h", value: "loading", note: "status rapido", state: "watch" },
      { label: "TIME ratio", value: "loading", note: "labels", state: "watch" },
      { label: "TP ratio", value: "loading", note: "labels", state: "watch" },
      { label: "Candidate status", value: "NO_VALID_CANDIDATES", note: "default safe", state: "bad" },
    ].map(renderKpiCard).join(""));
    setHtml("readinessGrid", renderReadinessGrid([
      { label: "Safety", value: "OK", state: "ok", note: "paper only" },
      { label: "Worker", value: "loading", state: "watch", note: "status endpoint" },
      { label: "TIME", value: "WATCH", state: "watch", note: "no data yet" },
      { label: "Live readiness", value: "NO LIVE", state: "bad", note: "always off" },
    ]));
    setHtml("outcomeStackedChart", renderEmptyState("Cargando TP/SL/TIME"));
    setHtml("signalBarChart", renderEmptyState("Cargando senales"));
    setHtml("candidateStatusChart", renderHorizontalBarChart([{ label: "NO_VALID_CANDIDATES", value: 1, color: "red", display: "safe default" }]));
    setHtml("timeDeathMiniChart", renderEmptyState("Cargando TIME"));
    setHtml("preMoveChart", renderHorizontalBarChart([
      { label: "LONG events", value: 0, color: "green", display: "manual" },
      { label: "SHORT events", value: 0, color: "red", display: "manual" },
    ]));
    setHtml("exitCalibrationChart", renderEmptyState("Exit calibration manual"));
    setHtml("opsPromotionChart", renderEmptyState("Operational intelligence manual"));
    setHtml("opsWalkForwardChart", renderEmptyState("Walk-forward manual"));
    setHtml("opsSuddenChart", renderEmptyState("Sudden move manual"));
    setHtml("opsSimulatorChart", renderEmptyState("Shadow simulator manual"));
  }

  async function runLab(buttonId, url, outputId, handler, append = false) {
    const button = $(buttonId);
    setButtonLoading(button, true);
    const started = performance.now();
    try {
      const payload = await fetchJson(url);
      const text = payload.text || JSON.stringify(payload, null, 2);
      const output = $(outputId);
      if (output) {
        output.textContent = append ? `${output.textContent}\n\n${text}` : text;
      }
      if (handler) handler(payload, text);
      return { ok: true, durationMs: Math.round(performance.now() - started), text };
    } catch (error) {
      const output = $(outputId);
      if (output) output.textContent = `ERROR_SANITIZED: ${error.message}`;
      return { ok: false, durationMs: Math.round(performance.now() - started), error };
    } finally {
      setButtonLoading(button, false);
    }
  }

  async function runMainAnalysis() {
    const button = $("refreshMainAnalysisBtn");
    setButtonLoading(button, true, "Analizando...");
    const status = $("reportStatus");
    const total = MAIN_ANALYSIS_STEPS.length;
    for (let index = 0; index < total; index += 1) {
      const step = MAIN_ANALYSIS_STEPS[index];
      status.textContent = `running ${index + 1}/${total}: ${step.name}`;
      const result = await runLab(null, step.url, step.target, step.handler, step.append);
      status.textContent = `${step.name}: ${result.ok ? "ok" : "error"} (${result.durationMs} ms)`;
    }
    status.textContent = `analisis principal OK ${new Date().toLocaleTimeString("es-ES")} - no backup/restore/live`;
    setButtonLoading(button, false);
  }

  function extractPercent(text, label) {
    const regex = new RegExp(`${label.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\s*=?\\s*([0-9]+(?:\\.[0-9]+)?)%?`, "i");
    const match = text.match(regex);
    return match ? num(match[1]) : 0;
  }

  function handleSummary(payload, text) {
    const pfRaw = (text.match(/PF=([0-9.]+)/i) || text.match(/PF:\s*([0-9.]+)/i) || [])[1] || "0";
    const time = extractPercent(text, "TIME%");
    const tp = extractPercent(text, "TP%");
    const sl = extractPercent(text, "SL%");
    state.trainingSummary = {
      pf: num(pfRaw),
      time,
      tp,
      sl,
      loadedAt: new Date().toLocaleTimeString("es-ES"),
    };
    setHtml("outcomeStackedChart", renderStackedBar([
      { label: "TIME", value: time, className: "time" },
      { label: "SL", value: sl, className: "sl" },
      { label: "TP", value: tp, className: "tp" },
    ]));
    $("finalRecommendationHero").textContent = "NO LIVE";
    $("mainProblemHero").textContent = `PF ${fmt(pfRaw, 2)} / TIME ${time.toFixed(1)}%`;
    if (state.status) {
      renderStatus(state.status);
    }
  }

  function handleCandidateRanking(payload, text) {
    const noValid = /NO_VALID_CANDIDATES/i.test(text);
    $("candidateRankingState").textContent = noValid ? "NO_VALID_CANDIDATES" : "WATCH_ONLY";
    setHtml("candidateStatusChart", renderHorizontalBarChart([
      { label: noValid ? "NO_VALID_CANDIDATES" : "WATCH_ONLY", value: 1, color: noValid ? "red" : "orange", display: noValid ? "blocked" : "watch" },
      { label: "ALLOW", value: /PAPER_CANDIDATE/i.test(text) ? 1 : 0, color: "green", display: /PAPER_CANDIDATE/i.test(text) ? "found" : "none" },
    ]));
  }

  function handleEdgeGuard(payload, text) {
    const allowCount = Array.isArray(payload.allow_paper_candidates) ? payload.allow_paper_candidates.length : 0;
    $("edgeGuardState").textContent = allowCount > 0 ? `${allowCount} allow candidates` : "No allow candidates";
  }

  function handleOrchestrator(payload, text) {
    const noAction = payload.no_actionable_candidates === true || /no_actionable_candidates:\s*true/i.test(text);
    $("orchestratorState").textContent = noAction ? "PAPER_ONLY / no actionable" : "PAPER_ONLY / watch";
  }

  function handleScoreCalibration(payload, text) {
    const quality = safeText(payload.overall_score_quality || (text.match(/overall_score_quality:\s*(\S+)/i) || [])[1], "MIXED");
    const problem = safeText(payload.biggest_problem || (text.match(/biggest_problem:\s*(\S+)/i) || [])[1], "score_not_monotonic");
    setText("scoreQualityState", quality);
    setText("scoreProblemText", `biggest_problem: ${problem}. Research only; no penalty applied.`);
    const rows = payload.by_score_bucket || [];
    setHtml("scoreMonotonicityChart", rows.length ? renderHorizontalBarChart(rows.slice(0, 9).map((row) => ({
      label: row.group_value || row.score_bucket || "bucket",
      value: Math.max(0, num(row.net_EV_est) + 1),
      color: num(row.net_EV_est) > 0 ? "green" : "red",
      display: `${fmt(row.net_EV_est, 4)} EV / ${pct(row.tp_ratio)} TP`,
    }))) : renderEmptyState("Score calibration no cargado."));
  }

  function handleCandidateIncubator(payload, text) {
    const counts = payload.candidate_status_counts || {};
    const total = Object.values(counts).reduce((sum, value) => sum + num(value), 0);
    setText("incubatorState", total ? `${total} setups reviewed` : "no setups loaded");
    setText("incubatorProblemText", "RESEARCH ONLY. PAPER_CANDIDATE_DISABLED no activa nada.");
    const chartRows = Object.entries(counts).map(([label, value]) => ({
      label,
      value,
      color: label === "REJECT" ? "red" : label === "WATCH_ONLY" || label === "NEED_MORE_DATA" ? "orange" : "blue",
      display: value,
    }));
    setHtml("incubatorStatusChart", chartRows.length ? renderHorizontalBarChart(chartRows) : renderEmptyState("Incubadora no cargada."));
    const rows = payload.candidates || payload.top_shadow_only || [];
    if (!rows.length) {
      setHtml("incubatorRows", `<tr><td colspan="7">Sin candidatos incubados. NO LIVE.</td></tr>`);
      return;
    }
    setHtml("incubatorRows", rows.slice(0, 12).map((row) => `
      <tr>
        <td>${escapeHtml(row.candidate_id || `${row.symbol || "NA"}_${row.side || "NA"}_${row.market_regime || "NA"}_${row.score_bucket || "NA"}`)}</td>
        <td>${escapeHtml(row.samples || 0)}</td>
        <td>${escapeHtml(pct(row.TP))}</td>
        <td>${escapeHtml(pct(row.SL))}</td>
        <td>${escapeHtml(pct(row.TIME))}</td>
        <td>${escapeHtml(fmt(row.net_EV_est, 4))}</td>
        <td>${escapeHtml(`${row.candidate_status || "WATCH_ONLY"} ${row.candidate_category || ""}`)}</td>
      </tr>`).join(""));
  }

  function handleCoreCorrections(payload, text) {
    const funding = safeText(payload.funding_model_status || (text.match(/funding_model_status:\s*(\S+)/i) || [])[1], "UNKNOWN");
    const doubleCounting = safeText(payload.double_counting_risk || (text.match(/double_counting_risk:\s*(\S+)/i) || [])[1], "UNKNOWN");
    const labelGuard = safeText(payload.labeler_guard_status || (text.match(/labeler_guard_status:\s*(\S+)/i) || [])[1], "UNKNOWN");
    const duplicateGuard = safeText(payload.duplicate_guard_status || (text.match(/duplicate_guard_status:\s*(\S+)/i) || [])[1], "UNKNOWN");
    const actionability = safeText(payload.candidate_actionability_logic || (text.match(/candidate_actionability_logic:\s*(.+)/i) || [])[1], "market_probe_not_actionable");
    setText("coreCorrectionsState", payload.cost_model_fixed === true || /cost_model_fixed:\s*true/i.test(text) ? "FIXED_READ_ONLY" : "CHECK");
    setText("coreCorrectionsText", `double_counting=${doubleCounting}; no live/no writes.`);
    setText("fundingModelState", funding);
    setText("labelerGuardState", labelGuard);
    setText("duplicateGuardState", duplicateGuard);
    setText("candidateActionabilityState", actionability);
  }

  function handleDataPipelineDiagnosis(payload, text) {
    const status = safeText(payload.dangerous_duplicate_status || (text.match(/dangerous_duplicate_status:\s*(\S+)/i) || [])[1], "UNKNOWN");
    const falsePositive = safeText(payload.audit_false_positive_status || (text.match(/audit_false_positive_status:\s*(\S+)/i) || [])[1], "UNKNOWN");
    setText("pipelineStatusState", status);
    setText("pipelineProblemText", `false_positive_audit=${falsePositive}. No DB writes.`);
    setHtml("pipelineDuplicateChart", renderHorizontalBarChart([
      { label: "Exact duplicates", value: num(payload.exact_duplicate_count), color: status === "BAD" ? "red" : "orange", display: safeText(payload.exact_duplicate_count, 0) },
      { label: "Conflicting labels", value: num(payload.conflicting_labels), color: "red", display: safeText(payload.conflicting_labels, 0) },
      { label: "Benign density", value: num(payload.benign_minute_bucket_density), color: "blue", display: fmt(payload.benign_minute_bucket_density, 1) },
    ]));
  }

  function handleRelationRepair(payload, text) {
    const status = safeText(payload.relation_health_status || (text.match(/relation_health_status:\s*(\S+)/i) || [])[1], "UNKNOWN");
    setText("pipelineStatusState", status);
    setText("pipelineProblemText", `relation_health_status=${status}. unsafe actions not taken.`);
  }

  function handleLabelQualityV2(payload, text) {
    const status = safeText(payload.label_quality_status || (text.match(/label_quality_status:\s*(\S+)/i) || [])[1], "UNKNOWN");
    setHtml("labelQualityChart", renderHorizontalBarChart([
      { label: "missed TP", value: num(payload.missed_tp_labels), color: "orange", display: safeText(payload.missed_tp_labels, 0) },
      { label: "missed SL", value: num(payload.missed_sl_labels), color: "red", display: safeText(payload.missed_sl_labels, 0) },
      { label: "TIME mismatch", value: num(payload.inconsistent_time_labels), color: "red", display: safeText(payload.inconsistent_time_labels, 0) },
      { label: "path mismatch", value: num(payload.path_metric_label_mismatch), color: "orange", display: safeText(payload.path_metric_label_mismatch, 0) },
    ]));
    if (status !== "OK") {
      setText("pipelineStatusState", status);
    }
  }

  function handleBitgetCostModel(payload, text) {
    const status = safeText(payload.cost_model_status || (text.match(/cost_model_status:\s*(\S+)/i) || [])[1], "UNKNOWN");
    const funding = safeText(payload.funding_model_status || (text.match(/funding_model_status:\s*(\S+)/i) || [])[1], "UNKNOWN");
    setText("costModelState", status);
    setText("costModelProblemText", `funding=${funding}; USDT-M maker/taker, no spot fees.`);
    const summary = payload.cost_sensitivity_summary || {};
    setHtml("costSensitivityChart", renderHorizontalBarChart([
      { label: "Gross edge", value: num(summary.gross_edge_groups), color: "blue", display: safeText(summary.gross_edge_groups, 0) },
      { label: "Net negative", value: num(summary.gross_edge_net_negative), color: "red", display: safeText(summary.gross_edge_net_negative, 0) },
      { label: "Changed under low cost", value: num(summary.changed_positive_under_maker_maker_or_zero_slippage), color: "orange", display: safeText(summary.changed_positive_under_maker_maker_or_zero_slippage, 0) },
    ]));
  }

  function handleCostInventory(payload, text) {
    const maker = safeText(payload.maker_fee_assumption, "2");
    const taker = safeText(payload.taker_fee_assumption, "6");
    setText("costModelState", "INVENTORIED");
    setText("costModelProblemText", `bot maker=${maker}bps taker=${taker}bps; compare USDT-M VIP0.`);
  }

  function handleMarginMode(payload, text) {
    const status = safeText(payload.margin_mode_status || (text.match(/margin_mode_status:\s*(\S+)/i) || [])[1], "UNKNOWN");
    setText("marginModeState", status);
  }

  function handleExecutionSafety(payload, text) {
    const net = safeText((text.match(/net_rr_adjusted:\s*(\S+)/i) || [])[1], "OK");
    const dynamic = safeText((text.match(/dynamic_exit_policy:\s*(\S+)/i) || [])[1], "SHADOW_READY");
    const stop = safeText((text.match(/stop_quality:\s*(\S+)/i) || [])[1], "OK");
    const balance = safeText((text.match(/fresh_balance_before_risk:\s*(\S+)/i) || [])[1], "NOT_LIVE_ONLY");
    const idempotency = safeText((text.match(/idempotency:\s*(\S+)/i) || [])[1], "OK");
    const emergency = safeText((text.match(/emergency_stop_failsafe:\s*(\S+)/i) || [])[1], "OK");
    const circuit = safeText((text.match(/circuit_breaker_magnitude:\s*(\S+)/i) || [])[1], "OK");
    const clock = safeText((text.match(/clock_drift:\s*(\S+)/i) || [])[1], "UNKNOWN");
    const hardening = safeText((text.match(/config_hardening:\s*(\S+)/i) || [])[1], "OK");
    setText("netRrState", net);
    setText("dynamicExitState", dynamic);
    setText("stopQualityState", stop);
    setText("freshBalanceState", balance);
    setText("idempotencyState", idempotency);
    setText("emergencyFailsafeState", emergency);
    setText("circuitMagnitudeState", circuit);
    setText("clockDriftState", clock);
    setText("configHardeningState", hardening);
    setHtml("executionSafetyChart", renderHorizontalBarChart([
      { label: "Net RR", value: net === "OK" ? 1 : 0.4, color: net === "OK" ? "green" : "orange", display: net },
      { label: "Idempotency", value: idempotency === "OK" ? 1 : 0.5, color: idempotency === "OK" ? "green" : "orange", display: idempotency },
      { label: "Failsafe", value: emergency === "OK" ? 1 : 0.5, color: emergency === "OK" ? "green" : "orange", display: emergency },
      { label: "Clock", value: clock === "BAD" ? 1 : 0.4, color: clock === "BAD" ? "red" : "orange", display: clock },
    ]));
  }

  function handleNetRrAudit(payload, text) {
    const gross = num((text.match(/gross_rr:\s*([0-9.]+)/i) || [])[1]);
    const net = num((text.match(/net_rr:\s*([0-9.]+)/i) || [])[1]);
    const warning = safeText((text.match(/rr_warning:\s*(\S+)/i) || [])[1], "UNKNOWN");
    setText("netRrState", warning === "OK" ? "OK" : "WARNING");
    setHtml("netRrChart", renderHorizontalBarChart([
      { label: "Gross RR", value: gross, color: "blue", display: fmt(gross, 2) },
      { label: "Net RR", value: net, color: net >= 1.4 ? "green" : "orange", display: fmt(net, 2) },
    ]));
  }

  function handleDynamicExitAudit(payload, text) {
    const trendTp1 = safeText((text.match(/trend_tp1_r:\s*([0-9.]+)/i) || [])[1], "2.0");
    setText("dynamicExitState", /DISABLED_SHADOW_ONLY/i.test(text) ? "SHADOW_READY" : "CHECK");
    setHtml("executionSafetyChart", renderHorizontalBarChart([
      { label: "Trend TP1 R", value: num(trendTp1), color: "blue", display: `${trendTp1}R` },
      { label: "Research only", value: 1, color: "green", display: "disabled" },
    ]));
  }

  function handleStructuralStopAudit(payload, text) {
    const quality = safeText((text.match(/stop_quality:\s*(\S+)/i) || [])[1], "UNKNOWN");
    const whipsaw = num((text.match(/whipsaw_risk:\s*([0-9.]+)/i) || [])[1]);
    setText("stopQualityState", quality);
    setHtml("executionSafetyChart", renderHorizontalBarChart([
      { label: "Stop quality", value: quality === "STRUCTURAL_VALID" || quality === "ATR_FALLBACK" ? 1 : 0.5, color: quality.includes("REJECT") ? "red" : "green", display: quality },
      { label: "Whipsaw", value: whipsaw, color: whipsaw > 0.75 ? "orange" : "blue", display: fmt(whipsaw, 2) },
    ]));
  }

  function handleOperationalIntelligence(payload, text) {
    const counts = payload.candidate_promotion_state_counts || {};
    const best = payload.shadow_simulator_best_policy || {};
    const strategy = payload.strategy_research_summary || {};
    setText("opsExitPolicyState", safeText(payload.exit_policy_v3_status || (text.match(/exit_policy_v3_status:\s*(\S+)/i) || [])[1], "NEED_DATA"));
    setText("opsTrailingCount", safeText(payload.trailing_profit_lock_candidate_count || (text.match(/trailing_profit_lock_candidate_count:\s*(\d+)/i) || [])[1], 0));
    setText("opsSuddenState", `${safeText(payload.sudden_move_patterns_found || 0)} patterns`);
    setText("opsWalkForwardState", `${safeText(payload.walk_forward_stable_candidates || 0)} stable / ${safeText(payload.walk_forward_overfit_rejected || 0)} rejected`);
    setText("opsAntiOverfitState", safeText(payload.anti_overfit_status || "UNKNOWN"));
    setText("opsPromotionState", Object.keys(counts).length ? `${Object.keys(counts).length} states` : "not loaded");
    setText("opsSimulatorState", best.strategy_id ? safeText(best.recommendation || "research") : "not loaded");
    setText("opsStrategyState", `${safeText(strategy.tested_hypotheses || 0)} hypotheses`);
    setText("opsStatValidityState", safeText(payload.statistical_validity_status || "INVALID_METRICS_BLOCKED"));
    setHtml("opsPromotionChart", Object.keys(counts).length ? renderHorizontalBarChart(Object.entries(counts).map(([label, value]) => ({
      label,
      value,
      color: label.includes("REJECT") ? "red" : label.includes("NEED") ? "orange" : "blue",
      display: value,
    }))) : renderEmptyState("Sin estados cargados"));
    setHtml("opsWalkForwardChart", renderHorizontalBarChart([
      { label: "stable", value: num(payload.walk_forward_stable_candidates), color: "green", display: safeText(payload.walk_forward_stable_candidates, 0) },
      { label: "overfit/reject", value: num(payload.walk_forward_overfit_rejected), color: "red", display: safeText(payload.walk_forward_overfit_rejected, 0) },
      { label: "anti-overfit rejects", value: num(payload.anti_overfit_rejects), color: "orange", display: safeText(payload.anti_overfit_rejects, 0) },
    ]));
    setHtml("opsSuddenChart", renderHorizontalBarChart([
      { label: "patterns", value: num(payload.sudden_move_patterns_found), color: "blue", display: safeText(payload.sudden_move_patterns_found, 0) },
      { label: "false positive risk", value: num(payload.sudden_move_false_positive_risk), color: "orange", display: safeText(payload.sudden_move_false_positive_risk, 0) },
      { label: "pre-move patterns", value: num(payload.pre_move_patterns_found), color: "purple", display: safeText(payload.pre_move_patterns_found, 0) },
    ]));
    setHtml("opsSimulatorChart", renderHorizontalBarChart([
      { label: "best net EV", value: Math.max(0, num(best.net_ev)), color: num(best.net_ev) > 0 ? "green" : "red", display: fmt(best.net_ev, 4) },
      { label: "best net PF", value: Math.max(0, num(best.net_pf)), color: num(best.net_pf) > 1 ? "green" : "orange", display: fmt(best.net_pf, 2) },
    ]));
  }

  function handleCandidatePromotionV2(payload, text) {
    const counts = payload.candidate_promotion_state_counts || {};
    handleOperationalIntelligence({ candidate_promotion_state_counts: counts, shadow_simulator_best_policy: {}, strategy_research_summary: {} }, text);
    setText("opsPromotionState", Object.keys(counts).length ? `${Object.keys(counts).length} states` : "no candidates");
  }

  function handleShadowSimulator(payload, text) {
    const best = payload.best_simulated_policy || {};
    setText("opsSimulatorState", best.strategy_id ? safeText(best.recommendation || "research") : "not loaded");
    setHtml("opsSimulatorChart", renderHorizontalBarChart([
      { label: "best net EV", value: Math.max(0, num(best.net_ev)), color: num(best.net_ev) > 0 ? "green" : "red", display: fmt(best.net_ev, 4) },
      { label: "best net PF", value: Math.max(0, num(best.net_pf)), color: num(best.net_pf) > 1 ? "green" : "orange", display: fmt(best.net_pf, 2) },
    ]));
  }

  function handleSuddenMoveDetector(payload, text) {
    setText("opsSuddenState", `${safeText(payload.patterns_found || 0)} patterns`);
    setHtml("opsSuddenChart", renderHorizontalBarChart([
      { label: "patterns", value: num(payload.patterns_found), color: "blue", display: safeText(payload.patterns_found, 0) },
      { label: "false positive risk", value: num(payload.false_positive_risk_count), color: "orange", display: safeText(payload.false_positive_risk_count, 0) },
    ]));
  }

  function handleTimeDeath(payload, text) {
    const time = extractPercent(text, "TIME%");
    const tp = extractPercent(text, "TP%");
    const sl = extractPercent(text, "SL%");
    setHtml("timeDeathChart", renderStackedBar([
      { label: "TIME", value: time || 80, className: "time" },
      { label: "SL", value: sl || 10, className: "sl" },
      { label: "TP", value: tp || 10, className: "tp" },
    ]));
    $("timeDeathStatusBadge").textContent = time > 80 ? "BAD" : "WATCH";
    $("timeDeathStatusBadge").className = `badge ${time > 80 ? "badge-danger" : "badge-warning"}`;
  }

  function handleExitCalibration(payload, text) {
    const rows = payload.best_trade_signal_shadow_exits || payload.recommended_shadow_tests || [];
    if (!rows.length) {
      setHtml("exitCalibrationChart", renderEmptyState("NEED_MORE_DATA. No aplicar exits."));
      setHtml("exitCalibrationRows", `<tr><td colspan="7">Sin candidatos accionables. Research only.</td></tr>`);
    } else {
      setHtml("exitCalibrationChart", renderHorizontalBarChart(rows.slice(0, 6).map((row) => ({
        label: row.group || row.group_key || row.source || "candidate",
        value: Math.max(0, num(row.best_shadow_net_ev ?? row.net_EV ?? row.net_ev)),
        color: num(row.best_shadow_net_ev ?? row.net_EV ?? row.net_ev) > 0 ? "green" : "red",
        display: fmt(row.best_shadow_net_ev ?? row.net_EV ?? row.net_ev, 5),
      }))));
      setHtml("exitCalibrationRows", rows.slice(0, 8).map((row) => `
        <tr>
          <td>${escapeHtml(row.group || row.group_key || row.symbol || "group")}</td>
          <td>${escapeHtml(row.source || "trade_signal")}</td>
          <td>${escapeHtml(row.samples || row.sample_count || 0)}</td>
          <td>${escapeHtml(row.best_shadow_exit || row.exit || "shadow")}</td>
          <td>${escapeHtml(fmt(row.best_shadow_net_ev ?? row.net_EV ?? row.net_ev, 5))}</td>
          <td>${escapeHtml(fmt(row.best_shadow_net_pf ?? row.net_PF ?? row.net_pf, 2))}</td>
          <td>${escapeHtml(row.decision || "WATCH_ONLY")}</td>
        </tr>`).join(""));
    }
    setHtml("exitCalibrationDiagnosis", listItems([
      { title: /market_probe/i.test(text) ? "market_probe: RESEARCH ONLY" : "source separation", subtitle: "NOT ACTIONABLE unless trade_signal confirms" },
      { title: /NEED_MORE_DATA/i.test(text) ? "NEED_MORE_DATA" : "Shadow exits", subtitle: "No exits applied" },
    ]));
  }

  function handlePreMove(payload, text) {
    const longEvents = num(payload.long_events || (text.match(/long_events:\s*(\d+)/i) || [])[1]);
    const shortEvents = num(payload.short_events || (text.match(/short_events:\s*(\d+)/i) || [])[1]);
    setHtml("preMoveChart", renderHorizontalBarChart([
      { label: "LONG events", value: longEvents, color: "green", display: longEvents || "manual" },
      { label: "SHORT events", value: shortEvents, color: "red", display: shortEvents || "manual" },
    ]));
    const noPatterns = /NO_VALID_PATTERNS|top_long_patterns:\s*none|top_short_patterns:\s*none/i.test(text);
    setHtml("preMoveSetups", noPatterns ? renderEmptyState("NO_VALID_PATTERNS. Research only.") : listItems([{ title: "Patterns found", subtitle: "Review technical output before trusting." }]));
  }

  function handleLatency(payload, text) {
    const p50 = num((text.match(/p50[^0-9]*([0-9.]+)/i) || [])[1]);
    const p95 = num((text.match(/p95[^0-9]*([0-9.]+)/i) || [])[1]);
    const p99 = num((text.match(/p99[^0-9]*([0-9.]+)/i) || [])[1]);
    setHtml("latencyChart", renderHorizontalBarChart([
      { label: "p50", value: p50, color: "green", display: p50 ? `${p50} ms` : "n/a" },
      { label: "p95", value: p95, color: "orange", display: p95 ? `${p95} ms` : "n/a" },
      { label: "p99", value: p99, color: "red", display: p99 ? `${p99} ms` : "n/a" },
    ]));
  }

  function handleVault(payload, text) {
    setHtml("vaultCards", [
      { label: "Remote backups", value: payload.remote_backup_count ?? "check", note: payload.latest_remote_backup || "vault status", state: num(payload.remote_backup_count) > 0 ? "ok" : "watch" },
      { label: "Upload verified", value: payload.last_upload_verified === true ? "YES" : "CHECK", note: payload.last_upload_status || "unknown", state: payload.last_upload_verified ? "ok" : "watch" },
      { label: "Secrets excluded", value: payload.secrets_excluded === false ? "NO" : "YES", note: "no tokens in panel", state: payload.secrets_excluded === false ? "bad" : "ok" },
    ].map(renderKpiCard).join(""));
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (_) {
      return false;
    }
  }

  async function generateFullReport() {
    const button = $("copyChatGptReportBtn");
    setButtonLoading(button, true, "Generando 1-2 min...");
    const started = performance.now();
    try {
      const text = await fetchText("/api/training/full-report?hours=24&format=text");
      state.lastFullReportText = text;
      state.lastReportGeneratedAt = new Date().toLocaleString("es-ES");
      $("fullReportPreview").textContent = text;
      const copied = await copyText(text);
      $("reportStatus").textContent = `full report ${copied ? "copied" : "generated"} - ${Math.round(performance.now() - started)} ms - ${(text.length / 1024).toFixed(1)} KB`;
    } catch (error) {
      $("reportStatus").textContent = `full report error: ${error.message}`;
    } finally {
      setButtonLoading(button, false);
    }
  }

  async function generateShortReport() {
    const button = $("copyShortReportBtn");
    setButtonLoading(button, true);
    const started = performance.now();
    try {
      const text = await fetchText("/api/training/short-report?hours=24&format=text");
      state.lastShortReportText = text;
      state.lastReportGeneratedAt = new Date().toLocaleString("es-ES");
      $("fullReportPreview").textContent = text;
      const copied = await copyText(text);
      const match = text.match(/report_status:\s*([A-Z_]+)/i);
      const reportStatus = match ? ` ${match[1].toUpperCase()}` : "";
      $("reportStatus").textContent = `short report${reportStatus} ${copied ? "copied" : "generated"} - ${Math.round(performance.now() - started)} ms - ${(text.length / 1024).toFixed(1)} KB`;
    } catch (error) {
      $("reportStatus").textContent = `short report error: ${error.message}`;
    } finally {
      setButtonLoading(button, false);
    }
  }

  function copyLastReport() {
    const text = state.lastFullReportText || state.lastShortReportText || "";
    if (!text) {
      $("reportStatus").textContent = "No hay reporte generado para copiar.";
      return;
    }
    copyText(text).then((ok) => {
      $("reportStatus").textContent = ok ? `ultimo reporte copiado (${state.lastReportGeneratedAt})` : "No se pudo copiar automaticamente.";
    });
  }

  function download(path) {
    window.location.href = apiUrl(path);
  }

  function bindSafeNavigation() {
    document.querySelectorAll("[data-target]").forEach((item) => {
      item.addEventListener("click", (event) => {
        event.preventDefault();
        const targetId = item.getAttribute("data-target") || "";
        const target = document.getElementById(targetId);
        if (!target) return;
        document.querySelectorAll("[data-target]").forEach((navItem) => {
          navItem.classList.toggle("active", navItem.getAttribute("data-target") === targetId);
        });
        target.scrollIntoView({ behavior: "smooth", block: "start" });
        if (window.history?.replaceState) {
          window.history.replaceState(null, "", `#${targetId}`);
        }
      });
    });
  }

  function bindActions() {
    $("quickRefreshBtn")?.addEventListener("click", loadStatus);
    $("refreshMainAnalysisBtn")?.addEventListener("click", runMainAnalysis);
    $("copyChatGptReportBtn")?.addEventListener("click", generateFullReport);
    $("copyShortReportBtn")?.addEventListener("click", generateShortReport);
    $("copyLastReportBtn")?.addEventListener("click", copyLastReport);
    $("downloadFullTxtBtn")?.addEventListener("click", () => download("/api/training/export/full.txt?hours=24"));
    $("downloadFullJsonBtn")?.addEventListener("click", () => download("/api/training/export/full.json?hours=24"));
    $("downloadSignalsCsvBtn")?.addEventListener("click", () => download("/api/training/export/signals.csv?hours=24"));
    $("downloadPaperCsvBtn")?.addEventListener("click", () => download("/api/training/export/paper-trades.csv?hours=168"));
    $("downloadLabelsCsvBtn")?.addEventListener("click", () => download("/api/training/export/labels.csv?hours=24"));
    $("downloadLatencyCsvBtn")?.addEventListener("click", () => download("/api/training/export/latency.csv?hours=24"));
    $("downloadPreMoveCsvBtn")?.addEventListener("click", () => download("/api/training/export/pre-move-events.csv?hours=24"));
    $("downloadCandidatesCsvBtn")?.addEventListener("click", () => download("/api/training/export/candidates.csv?hours=24"));

    const labBindings = [
      ["candidateRankingBtn", "/api/training/candidate-ranking?hours=24", "edgePolicyOutput", handleCandidateRanking, true],
      ["scoreCalibrationBtn", "/api/training/score-calibration?hours=24", "scoreIncubatorOutput", handleScoreCalibration],
      ["candidateIncubatorBtn", "/api/training/candidate-incubator?hours=24", "scoreIncubatorOutput", handleCandidateIncubator, true],
      ["trainingDataIntegrityBtn", "/api/training/training-data-integrity?hours=24", "scoreIncubatorOutput", null, true],
      ["coreCorrectionsBtn", "/api/training/core-corrections?hours=24", "pipelineCostOutput", handleCoreCorrections],
      ["dataPipelineDiagnosisBtn", "/api/training/data-pipeline-diagnosis?hours=24", "pipelineCostOutput", handleDataPipelineDiagnosis],
      ["relationRepairAuditBtn", "/api/training/relation-repair-audit?hours=24", "pipelineCostOutput", handleRelationRepair, true],
      ["labelQualityV2Btn", "/api/training/label-quality-v2?hours=24", "pipelineCostOutput", handleLabelQualityV2, true],
      ["costModelInventoryBtn", "/api/training/cost-model-inventory", "pipelineCostOutput", handleCostInventory, true],
      ["bitgetCostModelAuditBtn", "/api/training/bitget-cost-model-audit?hours=24", "pipelineCostOutput", handleBitgetCostModel, true],
      ["marginModeAuditBtn", "/api/training/margin-mode-audit", "pipelineCostOutput", handleMarginMode, true],
      ["executionSafetyAuditBtn", "/api/training/execution-safety-audit", "executionSafetyOutput", handleExecutionSafety],
      ["netRrAuditBtn", "/api/training/net-rr-audit?hours=24", "executionSafetyOutput", handleNetRrAudit, true],
      ["dynamicExitPolicyAuditBtn", "/api/training/dynamic-exit-policy-audit?hours=24", "executionSafetyOutput", handleDynamicExitAudit, true],
      ["structuralStopAuditBtn", "/api/training/structural-stop-audit?hours=24", "executionSafetyOutput", handleStructuralStopAudit, true],
      ["operationalIntelligenceBtn", "/api/training/operational-intelligence-audit?hours=24", "operationalIntelligenceOutput", handleOperationalIntelligence],
      ["exitPolicyV3BacktestBtn", "/api/training/exit-policy-v3-backtest?hours=24", "operationalIntelligenceOutput", null, true],
      ["suddenMoveDetectorBtn", "/api/training/sudden-move-detector?hours=24", "operationalIntelligenceOutput", handleSuddenMoveDetector, true],
      ["preMoveV2Btn", "/api/training/pre-move-v2?hours=24", "operationalIntelligenceOutput", null, true],
      ["walkForwardValidatorBtn", "/api/training/walk-forward-validator?hours=72", "operationalIntelligenceOutput", null, true],
      ["antiOverfitV2Btn", "/api/training/anti-overfit-v2?hours=72", "operationalIntelligenceOutput", null, true],
      ["candidatePromotionV2Btn", "/api/training/candidate-promotion-v2?hours=24", "operationalIntelligenceOutput", handleCandidatePromotionV2, true],
      ["shadowStrategySimulatorBtn", "/api/training/shadow-strategy-simulator?hours=72", "operationalIntelligenceOutput", handleShadowSimulator, true],
      ["strategyResearchLibraryBtn", "/api/training/strategy-research-library?hours=72", "operationalIntelligenceOutput", null, true],
      ["realStrategyBacktesterBtn", "/api/training/real-strategy-backtest?hours=72", "operationalIntelligenceOutput", null, true],
      ["duplicateModuleAuditBtn", "/api/training/duplicate-module-audit", "operationalIntelligenceOutput", null, true],
      ["edgeGuardBtn", "/api/training/edge-guard?hours=24", "edgePolicyOutput", handleEdgeGuard, true],
      ["paperPolicyOrchestratorBtn", "/api/training/paper-policy-orchestrator?hours=24", "edgePolicyOutput", handleOrchestrator, true],
      ["netEdgeLabBtn", "/api/training/net-edge-lab?hours=24", "edgePolicyOutput", null, true],
      ["evSlippageGateBtn", "/api/training/ev-slippage-calibration-gate?hours=24", "edgePolicyOutput", null, true],
      ["antiOverfitGateBtn", "/api/training/anti-overfit-gate?hours=24", "edgePolicyOutput", null, true],
      ["policyStabilityMatrixBtn", "/api/training/policy-stability-matrix?hours=24", "edgePolicyOutput", null, true],
      ["timeDeathAutopsyBtn", "/api/training/time-death-autopsy?hours=24", "timeDeathOutput", handleTimeDeath, true],
      ["timeDeathFilterProposalBtn", "/api/training/time-death-filter-proposal?hours=24", "timeDeathOutput", null, true],
      ["exitCauseBacktestBtn", "/api/training/exit-cause-backtest?hours=24", "timeDeathOutput", null, true],
      ["exitLabelCalibrationV2Btn", "/api/training/exit-label-calibration-v2?hours=24", "exitCalibrationOutput", handleExitCalibration],
      ["exitSimulationBtn", "/api/training/exit-simulation?hours=24", "exitCalibrationOutput", null, true],
      ["exitPolicyBacktestBtn", "/api/training/exit-policy-backtest?hours=24", "exitCalibrationOutput", null, true],
      ["adaptiveExitBacktestBtn", "/api/training/adaptive-exit-backtest?hours=24", "exitCalibrationOutput", null, true],
      ["preMoveEventLabelerBtn", "/api/training/pre-move-event-labeler?hours=24", "preMoveOutput", handlePreMove, true],
      ["preMoveFeatureSnapshotBtn", "/api/training/pre-move-feature-snapshot?hours=24", "preMoveOutput", null, true],
      ["preMovePatternMinerBtn", "/api/training/pre-move-pattern-miner?hours=24", "preMoveOutput", handlePreMove, true],
      ["preMoveSimilarityScannerBtn", "/api/training/pre-move-similarity-scanner?hours=6", "preMoveOutput", null, true],
      ["latencyAuditBtn", "/api/training/latency-audit?hours=24", "runtimeOutput", handleLatency],
      ["vpsRuntimeHealthBtn", "/api/training/vps-runtime-health", "runtimeOutput", null, true],
      ["workerHealthAuditBtn", "/api/training/worker-health-audit", "runtimeOutput", null, true],
      ["dashboardDataBindingAuditBtn", "/api/training/dashboard-data-binding-audit", "runtimeOutput", null, true],
      ["fastRuntimeReadinessBtn", "/api/training/fast-runtime-readiness?hours=24", "runtimeOutput", null, true],
      ["websocketMigrationPlanBtn", "/api/training/websocket-migration-plan?hours=24", "runtimeOutput", null, true],
      ["dataVaultStatusBtn", "/api/training/data-vault-status", "dataVaultOutput", handleVault],
      ["dataVaultAuditBtn", "/api/training/data-vault-audit", "dataVaultOutput", null, true],
      ["dataExportBtn", "/api/training/data-export?hours=168&upload=1", "dataVaultOutput", handleVault],
      ["dataExport720Btn", "/api/training/data-export?hours=720&upload=1", "dataVaultOutput", handleVault],
      ["dataUploadLatestBtn", "/api/training/data-upload-latest", "dataVaultOutput", handleVault],
      ["dataVaultPruneBtn", "/api/training/data-vault-prune", "dataVaultOutput", null],
    ];
    labBindings.forEach(([buttonId, url, target, handler, append]) => {
      $(buttonId)?.addEventListener("click", () => runLab(buttonId, url, target, handler, append));
    });

    // --- Phase 7B: Research Cockpit + Trade Replay + Cost Stress + Exit Labs ---
    $("cockpitRefreshBtn")?.addEventListener("click", loadResearchCockpit);
    $("cockpitCopyJsonBtn")?.addEventListener("click", copyResearchCockpitJson);
    $("replayRefreshBtn")?.addEventListener("click", loadTradeReplay);
    $("costStressRefreshBtn")?.addEventListener("click", loadCostStress);
    $("profitLockLabBtn")?.addEventListener("click", () => loadExitLab("profit-lock-lab"));
    $("fastExitLabBtn")?.addEventListener("click", () => loadExitLab("fast-exit-lab"));
    $("timeDeathReducerLabBtn")?.addEventListener("click", () => loadExitLab("time-death-reducer-lab"));
    $("phase8QuickBtn")?.addEventListener("click", () => loadPhase8Labs({ hours: 72, symbols: "BTCUSDT,ETHUSDT" }));
    $("phase8FullBtn")?.addEventListener("click", () => loadPhase8Labs({ hours: 720, symbols: "BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,BNBUSDT,LINKUSDT,AVAXUSDT,ADAUSDT,DOTUSDT" }));
    $("phase9QuickBtn")?.addEventListener("click", () => loadPhase9Labs({ hours: 72, symbols: "DOTUSDT" }));
    $("phase9FullBtn")?.addEventListener("click", () => loadPhase9Labs({ hours: 720, symbols: "DOTUSDT" }));
    $("researchPackBtn")?.addEventListener("click", loadResearchPack);

    // --- ResearchOps V5 ---
    $("v5FreshnessRefreshBtn")?.addEventListener("click", loadV5FreshnessMatrix);
    $("v5FreshnessPrepareCliBtn")?.addEventListener("click", prepareV5FreshnessCli);
    $("v5CleanRefreshBtn")?.addEventListener("click", loadV5TrainingCleanView);
    $("v5ShadowRefreshBtn")?.addEventListener("click", loadV5ShadowMultiTrade);
    $("v5CapitalRefreshBtn")?.addEventListener("click", loadV5CapitalLeverage);
    $("v5FeeAwareRefreshBtn")?.addEventListener("click", loadV5FeeAwareExit);
    $("v5PackBtn")?.addEventListener("click", loadV5Pack);
    $("v5PackCopyBtn")?.addEventListener("click", copyV5PackJson);
    $("v5PackDownloadBtn")?.addEventListener("click", downloadV5PackText);
    // ResearchOps V5.1 — Strategy Research Enhancer + shadow filters
    $("v51EnhancerRefreshBtn")?.addEventListener("click", loadV51StrategyEnhancer);
    $("v51ShadowCopyReportBtn")?.addEventListener("click", copyV51ShadowReport);
    for (const id of ["v51ShadowFilterSymbol","v51ShadowFilterSide","v51ShadowFilterStatus","v51ShadowFilterPnl","v51ShadowSort","v51ShadowLimit"]) {
      $(id)?.addEventListener("input", renderV51ShadowFiltered);
      $(id)?.addEventListener("change", renderV51ShadowFiltered);
    }

    bindSafeNavigation();
  }

  // ResearchOps V5 helpers ---------------------------------------------------
  let lastV5PackJson = "";
  let lastV5PackText = "";

  function _v5BadgeForStatus(status) {
    switch (String(status || "").toUpperCase()) {
      case "OK": return "badge badge-safe";
      case "WARNING": return "badge badge-info";
      case "BAD": case "FAIL": case "STALE": case "NEED_DATA": case "GAP": return "badge badge-danger";
      default: return "badge badge-muted";
    }
  }

  async function loadV5FreshnessMatrix() {
    const button = $("v5FreshnessRefreshBtn");
    setButtonLoading(button, true);
    try {
      const symbols = ($("v5FreshnessSymbols")?.value || "").trim();
      const timeframes = ($("v5FreshnessTimeframes")?.value || "5m,15m,1h").trim();
      const url = `/api/research/ohlcv-freshness-status?symbols=${encodeURIComponent(symbols)}&timeframes=${encodeURIComponent(timeframes)}`;
      const payload = await fetchJson(url);
      const overall = payload.overall_actionable ? "OK" : (payload.stale_count + payload.need_data_count + payload.gap_count > 0 ? "STALE" : "UNKNOWN");
      const badge = $("v5FreshnessOverallBadge");
      if (badge) { badge.textContent = overall; badge.className = _v5BadgeForStatus(overall); }
      const rows = Array.isArray(payload.rows) ? payload.rows : [];
      const target = $("v5FreshnessRows");
      if (target) {
        target.innerHTML = rows.length
          ? rows.map((r) => `<tr>
              <td>${escapeHtml(r.symbol)}</td>
              <td>${escapeHtml(r.timeframe)}</td>
              <td><span class="${_v5BadgeForStatus(r.status)}">${escapeHtml(r.status)}</span></td>
              <td>${escapeHtml(fmt(r.age_minutes, 1))}</td>
              <td>${escapeHtml(String(r.staleness_budget_minutes ?? "-"))}</td>
              <td>${escapeHtml(String(r.row_count ?? 0))}</td>
              <td>${escapeHtml(r.newest_timestamp || "-")}</td>
              <td><code>${escapeHtml(r.suggested_command || "-")}</code></td>
            </tr>`).join("")
          : "<tr><td colspan=\"8\">No data.</td></tr>";
      }
    } catch (error) {
      const target = $("v5FreshnessRows");
      if (target) target.innerHTML = `<tr><td colspan="8">freshness error: ${escapeHtml(error.message)}</td></tr>`;
    } finally {
      setButtonLoading(button, false);
    }
  }

  function prepareV5FreshnessCli() {
    const symbols = ($("v5FreshnessSymbols")?.value || "").trim();
    const timeframes = ($("v5FreshnessTimeframes")?.value || "5m,15m,1h").trim();
    const cli = `python -m app.research_lab ohlcv-freshness-refresh --symbols ${symbols} --timeframes ${timeframes} --hours 120 --dry-run`;
    copyText(cli).then(() => {});
  }

  async function loadV5TrainingCleanView() {
    const button = $("v5CleanRefreshBtn");
    setButtonLoading(button, true);
    try {
      const hours = num($("v5CleanHours")?.value, 24);
      const payload = await fetchJson(`/api/research/training-clean-view-audit?hours=${hours}`);
      const badge = $("v5CleanStatusBadge");
      if (badge) { badge.textContent = payload.overall_status || "UNKNOWN"; badge.className = _v5BadgeForStatus(payload.overall_status); }
      const tables = Array.isArray(payload.tables) ? payload.tables : [];
      const target = $("v5CleanRows");
      if (target) {
        target.innerHTML = tables.length
          ? tables.map((row) => `<tr>
              <td>${escapeHtml(row.table)}</td>
              <td>${escapeHtml(String(row.raw_count))}</td>
              <td>${escapeHtml(String(row.clean_count))}</td>
              <td>${escapeHtml(String(row.duplicates))}</td>
              <td>${escapeHtml(fmt(row.duplicate_rate, 4))}</td>
              <td>${escapeHtml(fmt(row.dedupe_ratio, 4))}</td>
              <td>${escapeHtml((row.notes || []).join(','))}</td>
            </tr>`).join("")
          : "<tr><td colspan=\"7\">No data.</td></tr>";
      }
      setText("v5CleanOutput", payload.text || JSON.stringify(payload, null, 2).slice(0, 4000));
    } catch (error) {
      setText("v5CleanOutput", `clean view error: ${error.message}`);
    } finally {
      setButtonLoading(button, false);
    }
  }

  async function loadV5ShadowMultiTrade() {
    const button = $("v5ShadowRefreshBtn");
    setButtonLoading(button, true);
    try {
      const hours = num($("v5ShadowHours")?.value, 24);
      const payload = await fetchJson(`/api/research/shadow-multi-trade-status?hours=${hours}`);
      if (payload.status === "SKIPPED_HEAVY") {
        setText("v5ShadowOutput", `SKIPPED_HEAVY\nCLI: ${payload.cli_command}`);
        const target = $("v5ShadowRows");
        if (target) target.innerHTML = `<tr><td colspan="10">SKIPPED_HEAVY — use CLI: ${escapeHtml(payload.cli_command || '-')}</td></tr>`;
        return;
      }
      const trades = Array.isArray(payload.trades) ? payload.trades : [];
      const target = $("v5ShadowRows");
      if (target) {
        target.innerHTML = trades.length
          ? trades.slice(0, 60).map((t) => `<tr>
              <td><code>${escapeHtml(t.shadow_id)}</code></td>
              <td>${escapeHtml(t.symbol)}</td>
              <td>${escapeHtml(t.side)}</td>
              <td>${escapeHtml(t.setup_id)}</td>
              <td>${escapeHtml(t.status)}</td>
              <td>${escapeHtml(fmt(t.gross_pnl_pct, 4))}</td>
              <td>${escapeHtml(fmt(t.net_pnl_pct, 4))}</td>
              <td>${escapeHtml(fmt(t.gross_pnl_usdt, 4))}</td>
              <td>${escapeHtml(fmt(t.net_pnl_usdt, 4))}</td>
              <td>${escapeHtml(String(t.bars_open))}</td>
            </tr>`).join("")
          : "<tr><td colspan=\"10\">No shadow trades.</td></tr>";
      }
      setText("v5ShadowOutput", payload.text || JSON.stringify(payload, null, 2).slice(0, 4000));
    } catch (error) {
      setText("v5ShadowOutput", `shadow error: ${error.message}`);
    } finally {
      setButtonLoading(button, false);
    }
  }

  async function loadV5CapitalLeverage() {
    const button = $("v5CapitalRefreshBtn");
    setButtonLoading(button, true);
    try {
      const symbol = ($("v5CapitalSymbol")?.value || "DOTUSDT").toUpperCase();
      const hours = num($("v5CapitalHours")?.value, 168);
      const capital = num($("v5CapitalTotal")?.value, 40);
      const payload = await fetchJson(`/api/research/capital-leverage-sim?symbols=${encodeURIComponent(symbol)}&hours=${hours}&capital=${capital}`);
      if (payload.status === "SKIPPED_HEAVY") {
        const target = $("v5CapitalRows");
        if (target) target.innerHTML = `<tr><td colspan="10">SKIPPED_HEAVY — use CLI: ${escapeHtml(payload.cli_command || '-')}</td></tr>`;
        return;
      }
      const scenarios = Array.isArray(payload.scenarios) ? payload.scenarios : [];
      const target = $("v5CapitalRows");
      if (target) {
        target.innerHTML = scenarios.length
          ? scenarios.map((s) => `<tr>
              <td>${escapeHtml(fmt(s.margin_per_trade_usdt, 2))}</td>
              <td>${escapeHtml(String(s.leverage))}</td>
              <td>${escapeHtml(fmt(s.notional_usdt, 2))}</td>
              <td>${escapeHtml(String(s.trades))}</td>
              <td>${escapeHtml(fmt(s.avg_price_move_pct, 4))}</td>
              <td>${escapeHtml(fmt(s.net_pnl_usdt, 4))}</td>
              <td>${escapeHtml(fmt(s.roe_pct, 4))}</td>
              <td>${escapeHtml(fmt(s.min_price_move_to_break_even_pct, 4))}</td>
              <td>${escapeHtml(fmt(s.liquidation_distance_estimate_pct, 4))}</td>
              <td>${escapeHtml(String(s.promotion_eligible))}</td>
            </tr>`).join("")
          : "<tr><td colspan=\"10\">No data.</td></tr>";
      }
    } catch (error) {
      const target = $("v5CapitalRows");
      if (target) target.innerHTML = `<tr><td colspan="10">capital sim error: ${escapeHtml(error.message)}</td></tr>`;
    } finally {
      setButtonLoading(button, false);
    }
  }

  async function loadV5FeeAwareExit() {
    const button = $("v5FeeAwareRefreshBtn");
    setButtonLoading(button, true);
    try {
      const symbols = ($("v5FeeAwareSymbols")?.value || "DOTUSDT").trim();
      const hours = num($("v5FeeAwareHours")?.value, 168);
      const payload = await fetchJson(`/api/research/fee-aware-exit-trainer?symbols=${encodeURIComponent(symbols)}&hours=${hours}`);
      if (payload.status === "SKIPPED_HEAVY") {
        const target = $("v5FeeAwareRows");
        if (target) target.innerHTML = `<tr><td colspan="7">SKIPPED_HEAVY — use CLI: ${escapeHtml(payload.cli_command || '-')}</td></tr>`;
        return;
      }
      const results = Array.isArray(payload.results) ? payload.results : [];
      const target = $("v5FeeAwareRows");
      if (target) {
        target.innerHTML = results.length
          ? results.map((r) => `<tr>
              <td>${escapeHtml(r.symbol)}</td>
              <td>${escapeHtml(fmt(r.net_profit_lock_pct, 2))}</td>
              <td>${escapeHtml(r.decision)}</td>
              <td>${escapeHtml(String(r.promotion_eligible))}</td>
              <td>${escapeHtml(r.best_scenario_name)}</td>
              <td>${escapeHtml(fmt(r.best_net_ev, 6))}</td>
              <td>${escapeHtml(String(r.gross_green_net_negative))}</td>
            </tr>`).join("")
          : "<tr><td colspan=\"7\">No data.</td></tr>";
      }
      const promotable = results.filter((r) => r.promotion_eligible);
      const badge = $("v5FeeAwareBestBadge");
      if (badge) {
        if (promotable.length > 0) { badge.textContent = `${promotable.length} promotable`; badge.className = "badge badge-safe"; }
        else { badge.textContent = "no_promotion"; badge.className = "badge badge-muted"; }
      }
    } catch (error) {
      const target = $("v5FeeAwareRows");
      if (target) target.innerHTML = `<tr><td colspan="7">fee aware error: ${escapeHtml(error.message)}</td></tr>`;
    } finally {
      setButtonLoading(button, false);
    }
  }

  async function loadV5Pack() {
    const button = $("v5PackBtn");
    setButtonLoading(button, true);
    try {
      const payload = await fetchJson(`/api/research-pack-v5?hours=24`);
      lastV5PackJson = JSON.stringify(payload, null, 2);
      lastV5PackText = payload.text || lastV5PackJson;
      setText("v5PackOutput", lastV5PackText.slice(0, 8000));
    } catch (error) {
      setText("v5PackOutput", `pack v5 error: ${error.message}`);
    } finally {
      setButtonLoading(button, false);
    }
  }

  function copyV5PackJson() {
    if (!lastV5PackJson) return;
    copyText(lastV5PackJson).then(() => {});
  }

  function downloadV5PackText() {
    if (!lastV5PackText) return;
    const blob = new Blob([lastV5PackText], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = "research_pack_v5.txt"; a.click();
    URL.revokeObjectURL(url);
  }

  // Phase 7B helpers ---------------------------------------------------------
  let lastCockpitJson = "";

  async function loadResearchCockpit() {
    const button = $("cockpitRefreshBtn");
    setButtonLoading(button, true);
    try {
      const payload = await fetchJson("/api/training/research-cockpit");
      lastCockpitJson = payload.json || JSON.stringify(payload, null, 2);
      setText("cockpitMode", payload.mode || "paper");
      setText("cockpitGit", payload.git_commit_short || "unknown");
      setText("cockpitHealth", payload.health || "UNKNOWN");
      setText("cockpitOpenPositions", String(payload.open_positions ?? 0));
      setText("cockpitOhlcvStatus", payload.ohlcv_status || "UNKNOWN");
      setText("cockpitOhlcvRows", String(payload.ohlcv_total_rows ?? 0));
      setText("cockpitBacktest", payload.latest_backtest_decision || "UNKNOWN");
      setText("cockpitPolicy", payload.latest_policy_decision || "UNKNOWN");
      setText("cockpitPolicyReady", String(Boolean(payload.policy_ready_for_paper)));
      setText("cockpitFinal", payload.final_recommendation || "NO LIVE");
      const rowsTarget = $("cockpitSafetyFlagsRows");
      if (rowsTarget) {
        const flags = payload.safety_flags || {};
        const keys = Object.keys(flags);
        rowsTarget.innerHTML = keys.length
          ? keys.map((k) => `<tr><td>${escapeHtml(k)}</td><td>${escapeHtml(flags[k])}</td></tr>`).join("")
          : "<tr><td colspan=\"2\">No safety flags available.</td></tr>";
      }
      setText("cockpitOutput", payload.text || lastCockpitJson);
    } catch (error) {
      setText("cockpitOutput", `cockpit error: ${error.message}`);
    } finally {
      setButtonLoading(button, false);
    }
  }

  function copyResearchCockpitJson() {
    if (!lastCockpitJson) return;
    copyText(lastCockpitJson).then(() => {});
  }

  async function loadTradeReplay() {
    const button = $("replayRefreshBtn");
    setButtonLoading(button, true);
    try {
      const symbol = ($("replaySymbol")?.value || "BTCUSDT").toUpperCase();
      const hours = num($("replayHours")?.value, 72);
      const timeframe = $("replayTimeframe")?.value || "5m";
      const maxTrades = num($("replayMaxTrades")?.value, 50);
      const url = `/api/training/trade-replay?symbol=${encodeURIComponent(symbol)}&hours=${hours}&timeframe=${encodeURIComponent(timeframe)}&max_trades=${maxTrades}`;
      const payload = await fetchJson(url);
      setText("replaySymbolDisplay", payload.symbol || symbol);
      const candles = Array.isArray(payload.candles) ? payload.candles : [];
      const trades = Array.isArray(payload.trades) ? payload.trades : [];
      setText("replayCandleCount", String(candles.length));
      setText("replayTradeCount", String(trades.length));
      const wins = trades.filter((t) => num(t.net_return_pct) > 0).length;
      const losses = trades.filter((t) => num(t.net_return_pct) < 0).length;
      setText("replayWinCount", String(wins));
      setText("replayLossCount", String(losses));
      setText("replayFirstCandle", candles[0]?.timestamp || "-");
      setText("replayLastCandle", candles[candles.length - 1]?.timestamp || "-");
      renderReplayChart(candles, trades);
      const tableTarget = $("replayTradesRows");
      if (tableTarget) {
        if (!trades.length) {
          tableTarget.innerHTML = "<tr><td colspan=\"12\">No simulated trades for this window.</td></tr>";
        } else {
          tableTarget.innerHTML = trades.slice(0, 200).map((t) => `
            <tr>
              <td>${escapeHtml(t.entry_time)}</td>
              <td>${escapeHtml(t.side)}</td>
              <td>${escapeHtml(fmt(t.entry_price, 4))}</td>
              <td>${escapeHtml(t.exit_time)}</td>
              <td>${escapeHtml(fmt(t.exit_price, 4))}</td>
              <td>${escapeHtml(fmt(t.stop_loss, 4))}</td>
              <td>${escapeHtml(fmt(t.take_profit_1, 4))}</td>
              <td>${escapeHtml(t.exit_reason)}</td>
              <td>${escapeHtml(fmt(t.net_return_pct, 3))}</td>
              <td>${escapeHtml(fmt(t.mfe_pct, 3))}</td>
              <td>${escapeHtml(fmt(t.mae_pct, 3))}</td>
              <td>${escapeHtml(String(t.duration_bars ?? ""))}</td>
            </tr>
          `).join("");
        }
      }
      setText("replayOutput", payload.text || JSON.stringify(payload, null, 2).slice(0, 5000));
    } catch (error) {
      setText("replayOutput", `replay error: ${error.message}`);
    } finally {
      setButtonLoading(button, false);
    }
  }

  function renderReplayChart(candles, trades) {
    const node = $("replayChart");
    if (!node) return;
    if (!candles.length) {
      node.innerHTML = '<div class="replay-empty">No OHLCV candles available for this symbol/timeframe.</div>';
      return;
    }
    const W = Math.max(600, node.clientWidth || 700);
    const H = 220;
    const padding = 24;
    const closes = candles.map((c) => num(c.close));
    const minPrice = Math.min(...closes);
    const maxPrice = Math.max(...closes);
    const range = (maxPrice - minPrice) || 1;
    const stepX = (W - padding * 2) / Math.max(closes.length - 1, 1);
    const points = closes.map((p, i) => {
      const x = padding + i * stepX;
      const y = padding + (1 - (p - minPrice) / range) * (H - padding * 2);
      return `${x.toFixed(2)},${y.toFixed(2)}`;
    }).join(" ");
    const markers = trades.slice(0, 200).map((t) => {
      const eIdx = clamp(num(t.entry_index), 0, closes.length - 1);
      const xIdx = clamp(num(t.exit_index), 0, closes.length - 1);
      const ex = padding + eIdx * stepX;
      const ey = padding + (1 - (num(t.entry_price) - minPrice) / range) * (H - padding * 2);
      const xx = padding + xIdx * stepX;
      const xy = padding + (1 - (num(t.exit_price) - minPrice) / range) * (H - padding * 2);
      const entryColor = String(t.side || "").toUpperCase() === "LONG" ? "#2ee59d" : "#a78bfa";
      const exitColor = num(t.net_return_pct) > 0 ? "#2ee59d" : (num(t.net_return_pct) < 0 ? "#ff4d6d" : "#90a0b7");
      return `
        <line x1="${ex.toFixed(2)}" y1="${ey.toFixed(2)}" x2="${xx.toFixed(2)}" y2="${xy.toFixed(2)}" stroke="${exitColor}" stroke-width="1" stroke-opacity="0.45" />
        <circle cx="${ex.toFixed(2)}" cy="${ey.toFixed(2)}" r="3" fill="${entryColor}" stroke="#0d131d" stroke-width="1"></circle>
        <polygon points="${xx.toFixed(2)},${(xy-3).toFixed(2)} ${(xx-3).toFixed(2)},${(xy+3).toFixed(2)} ${(xx+3).toFixed(2)},${(xy+3).toFixed(2)}" fill="${exitColor}" stroke="#0d131d" stroke-width="0.8"></polygon>
      `;
    }).join("");
    node.innerHTML = `
      <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
        <polyline points="${points}" fill="none" stroke="#5ab7ff" stroke-width="1.4" stroke-linejoin="round" />
        <g>${markers}</g>
      </svg>
    `;
  }

  async function loadCostStress() {
    const button = $("costStressRefreshBtn");
    setButtonLoading(button, true);
    try {
      const hours = num($("costStressHours")?.value, 720);
      const symbols = ($("costStressSymbols")?.value || "").trim();
      const url = `/api/training/cost-stress?hours=${hours}${symbols ? `&symbols=${encodeURIComponent(symbols)}` : ""}`;
      const payload = await fetchJson(url);
      const status = payload.cost_stress_status || "UNKNOWN";
      const badge = $("costStressStatusBadge");
      if (badge) {
        badge.textContent = status;
        badge.className = `badge ${status === "PASS" ? "badge-safe" : status === "WARN" ? "badge-info" : status === "FAIL" ? "badge-danger" : "badge-muted"}`;
      }
      const rows = $("costStressRows");
      const scenarios = Array.isArray(payload.scenarios) ? payload.scenarios : [];
      if (rows) {
        rows.innerHTML = scenarios.length
          ? scenarios.map((s) => `
              <tr>
                <td>${escapeHtml(s.name)}</td>
                <td>${escapeHtml(fmt(s.cost_pct, 4))}%</td>
                <td>${escapeHtml(String(s.trades ?? 0))}</td>
                <td>${escapeHtml(fmt(s.net_ev, 6))}</td>
                <td>${escapeHtml(fmt(s.net_pf, 4))}</td>
                <td>${escapeHtml(pct(s.win_rate))}</td>
              </tr>
            `).join("")
          : "<tr><td colspan=\"6\">No trades available for cost stress.</td></tr>";
      }
      setText("costStressOutput", payload.text || JSON.stringify(payload, null, 2).slice(0, 5000));
    } catch (error) {
      setText("costStressOutput", `cost stress error: ${error.message}`);
    } finally {
      setButtonLoading(button, false);
    }
  }

  // Explicit endpoint constants so the source matches the API surface.
  const EXIT_LAB_ENDPOINTS = {
    "profit-lock-lab": "/api/training/profit-lock-lab",
    "fast-exit-lab": "/api/training/fast-exit-lab",
    "time-death-reducer-lab": "/api/training/time-death-reducer-lab",
  };

  async function loadExitLab(kind) {
    const button = $(kind === "profit-lock-lab" ? "profitLockLabBtn"
      : kind === "fast-exit-lab" ? "fastExitLabBtn" : "timeDeathReducerLabBtn");
    setButtonLoading(button, true);
    try {
      const symbol = ($("exitLabSymbol")?.value || "BTCUSDT").toUpperCase();
      const hours = num($("exitLabHours")?.value, 168);
      const timeframe = $("exitLabTimeframe")?.value || "5m";
      const base = EXIT_LAB_ENDPOINTS[kind] || `/api/training/${kind}`;
      const url = `${base}?symbol=${encodeURIComponent(symbol)}&hours=${hours}&timeframe=${encodeURIComponent(timeframe)}`;
      const payload = await fetchJson(url);
      const comparisons = Array.isArray(payload.comparisons) ? payload.comparisons : [];
      const rows = $("exitLabRows");
      if (rows) {
        rows.innerHTML = comparisons.length
          ? comparisons.map((c) => `
              <tr>
                <td>${escapeHtml(c.policy_name)}</td>
                <td>${escapeHtml(String(c.trades ?? 0))}</td>
                <td>${escapeHtml(fmt(c.net_ev, 6))}</td>
                <td>${escapeHtml(fmt(c.net_pf, 4))}</td>
                <td>${escapeHtml(fmt(c.delta_ev_vs_baseline, 6))}</td>
                <td>${escapeHtml(pct(c.tp_pct))}</td>
                <td>${escapeHtml(pct(c.sl_pct))}</td>
                <td>${escapeHtml(pct(c.time_pct))}</td>
                <td>${escapeHtml(pct(c.profit_lock_pct))}</td>
                <td>${escapeHtml(pct(c.fast_exit_pct))}</td>
                <td>${escapeHtml(fmt(c.avg_duration_bars, 1))}</td>
                <td>${escapeHtml(c.decision)}</td>
              </tr>
            `).join("")
          : "<tr><td colspan=\"12\">No simulated trades for this exit lab.</td></tr>";
      }
      const badge = $("exitLabBestBadge");
      if (badge) {
        const best = payload.best_policy || "no improvement";
        badge.textContent = best ? `best: ${best}` : "no improvement";
        badge.className = `badge ${payload.best_policy ? "badge-safe" : "badge-muted"}`;
      }
      setText("exitLabOutput", payload.text || JSON.stringify(payload, null, 2).slice(0, 5000));
    } catch (error) {
      setText("exitLabOutput", `exit lab error: ${error.message}`);
    } finally {
      setButtonLoading(button, false);
    }
  }

  const PHASE8_ENDPOINTS = [
    { key: "timeExit", label: "Time Exit Autopsy V2", endpoint: "/api/training/time-exit-autopsy-v2" },
    { key: "dynamicHold", label: "Dynamic Hold Lab", endpoint: "/api/training/dynamic-hold-lab" },
    { key: "entryExhaustion", label: "Entry Exhaustion Lab", endpoint: "/api/training/entry-exhaustion-lab" },
    { key: "reversal", label: "Reversal Candidate Lab", endpoint: "/api/training/reversal-candidate-lab" },
    { key: "exitPolicy", label: "Exit Policy Comparator V2", endpoint: "/api/training/exit-policy-v2" },
    { key: "validator", label: "Phase 8 Candidate Validator", endpoint: "/api/training/phase8-candidate-validator" },
  ];

  async function loadPhase8Labs(options = {}) {
    const quickBtn = $("phase8QuickBtn");
    const fullBtn = $("phase8FullBtn");
    setButtonLoading(quickBtn, true, "Analizando...");
    setButtonLoading(fullBtn, true, "Analizando...");
    const hours = options.hours || num($("phase8Hours")?.value, 72);
    const symbols = options.symbols || ($("phase8Symbols")?.value || "BTCUSDT,ETHUSDT");
    const timeframe = $("phase8Timeframe")?.value || "5m";
    setText("phase8Output", `Running Phase 8 research labs hours=${hours} symbols=${symbols}. Heavy dashboard requests are guarded. Research only. NO LIVE.`);
    const rows = [];
    const outputs = [];
    try {
      for (const item of PHASE8_ENDPOINTS) {
        const url = `${item.endpoint}?hours=${hours}&timeframe=${encodeURIComponent(timeframe)}&symbols=${encodeURIComponent(symbols)}`;
        const payload = await fetchJson(url);
        outputs.push(payload.text || JSON.stringify(payload, null, 2));
        rows.push(renderPhase8Row(item.label, payload));
        updatePhase8Cards(item.key, payload);
      }
      setHtml("phase8Rows", rows.join(""));
      setText("phase8Output", outputs.join("\n\n"));
    } catch (error) {
      setText("phase8Output", `phase8 error: ${error.message}`);
    } finally {
      setButtonLoading(quickBtn, false);
      setButtonLoading(fullBtn, false);
    }
  }

  function renderPhase8Row(label, payload) {
    const trades = payload.total_trades ?? payload.baseline_trades ?? payload.trades ?? 0;
    const metric = payload.premature_time_exit_count !== undefined
      ? `premature=${payload.premature_time_exit_count} missed_avg=${fmt(payload.missed_profit_average, 4)}`
      : payload.best_policy
        ? `best=${payload.best_policy} decision=${payload.best_policy_decision || "research"}`
        : payload.late_chase_count !== undefined
          ? `late=${payload.late_chase_count} reversal=${payload.reversal_risk_count}`
            : payload.reversal_opportunities !== undefined
              ? `opps=${payload.reversal_opportunities} false=${payload.false_reversal_traps}`
              : payload.best_candidate_id
                ? `best=${payload.best_candidate_id} decision=${payload.best_decision}`
              : Array.isArray(payload.policies)
                ? `policies=${payload.policies.length}`
                : "research";
    const status = payload.skipped_heavy ? "SKIPPED_HEAVY" : payload.error ? "ERROR" : payload.final_recommendation || "NO LIVE";
    return `<tr>
      <td>${escapeHtml(label)}</td>
      <td>${escapeHtml(String(trades))}</td>
      <td>${escapeHtml(metric)}</td>
      <td>${escapeHtml(status)}</td>
      <td>research_only=${escapeHtml(String(payload.research_only !== false))}; no activation</td>
    </tr>`;
  }

  function updatePhase8Cards(key, payload) {
    if (payload.skipped_heavy) {
      const skippedText = "Heavy 720h run skipped in dashboard. Use CLI command shown below.";
      if (key === "timeExit") {
        setText("phase8TimeExitState", "SKIPPED_HEAVY");
        setText("phase8TimeExitText", skippedText);
      } else if (key === "dynamicHold") {
        setText("phase8HoldState", "SKIPPED_HEAVY");
        setText("phase8HoldText", skippedText);
      } else if (key === "entryExhaustion") {
        setText("phase8ExhaustionState", "SKIPPED_HEAVY");
        setText("phase8ExhaustionText", skippedText);
      } else if (key === "reversal") {
        setText("phase8ReversalState", "SKIPPED_HEAVY");
        setText("phase8ReversalText", skippedText);
      } else if (key === "exitPolicy") {
        setText("phase8ExitPolicyState", "SKIPPED_HEAVY");
        setText("phase8ExitPolicyText", skippedText);
      } else if (key === "validator") {
        setText("phase8ValidatorState", "SKIPPED_HEAVY");
        setText("phase8ValidatorText", skippedText);
      }
      return;
    }
    if (key === "timeExit") {
      setText("phase8TimeExitState", payload.error ? "ERROR" : `premature=${payload.premature_time_exit_count ?? 0}`);
      setText("phase8TimeExitText", `TIME trades=${payload.time_horizon_trades ?? 0}, missed_avg=${fmt(payload.missed_profit_average, 4)}. Counterfactual only.`);
    } else if (key === "dynamicHold") {
      const best = Array.isArray(payload.policies) ? payload.policies.find((p) => p.decision === "IMPROVES_BASELINE_RESEARCH_ONLY") : null;
      setText("phase8HoldState", best ? "WATCH_ONLY" : "NEED_MORE_DATA");
      setText("phase8HoldText", best ? `${best.policy_name} improved in research only.` : "No validated dynamic hold policy loaded or stable yet.");
    } else if (key === "entryExhaustion") {
      setText("phase8ExhaustionState", `late=${payload.late_chase_count ?? 0}`);
      setText("phase8ExhaustionText", `reversal_risk=${payload.reversal_risk_count ?? 0}; no runtime block applied.`);
    } else if (key === "reversal") {
      setText("phase8ReversalState", `opps=${payload.reversal_opportunities ?? 0}`);
      setText("phase8ReversalText", `false traps=${payload.false_reversal_traps ?? 0}; auto_flip=false.`);
    } else if (key === "exitPolicy") {
      setText("phase8ExitPolicyState", payload.best_policy || "none");
      setText("phase8ExitPolicyText", payload.sensitivity_warning || "72h can suggest; 720h validates.");
    } else if (key === "validator") {
      setText("phase8ValidatorState", payload.best_decision || "NEED_MORE_DATA");
      setText("phase8ValidatorText", `best=${payload.best_candidate_id || "none"}; paper_filter=false; manual review only.`);
    }
  }

  const PHASE9_ENDPOINTS = [
    { key: "readiness", label: "Phase 9 Paper Readiness", endpoint: "/api/training/phase9-paper-readiness" },
    { key: "diagnosis", label: "DOT Regime Diagnosis", endpoint: "/api/training/dot-regime-diagnosis" },
    { key: "filter", label: "DOT Regime Filter Lab", endpoint: "/api/training/dot-regime-filter-lab" },
    { key: "netProfit", label: "Net Profit Lock Lab", endpoint: "/api/training/net-profit-lock-lab" },
    { key: "fastSignal", label: "Fast Signal Shadow", endpoint: "/api/training/fast-signal-shadow" },
  ];

  async function loadPhase9Labs(options = {}) {
    const quickBtn = $("phase9QuickBtn");
    const fullBtn = $("phase9FullBtn");
    setButtonLoading(quickBtn, true, "Analizando...");
    setButtonLoading(fullBtn, true, "Preparando...");
    const hours = options.hours || num($("phase9Hours")?.value, 72);
    const symbols = options.symbols || ($("phase9Symbols")?.value || "DOTUSDT");
    const timeframe = $("phase9Timeframe")?.value || "5m";
    const rows = [];
    const outputs = [];
    setText("phase9Output", `Running Phase 9 research labs hours=${hours} symbols=${symbols}. Heavy runs are CLI-first. Research only. NO LIVE.`);
    try {
      for (const item of PHASE9_ENDPOINTS) {
        const url = `${item.endpoint}?hours=${hours}&timeframe=${encodeURIComponent(timeframe)}&symbols=${encodeURIComponent(symbols)}&folds=4&min_trades=250`;
        const payload = await fetchJson(url);
        outputs.push(payload.text || JSON.stringify(payload, null, 2));
        rows.push(renderPhase9Row(item.label, payload));
        updatePhase9Cards(item.key, payload);
      }
      setHtml("phase9Rows", rows.join(""));
      setText("phase9Output", outputs.join("\n\n"));
    } catch (error) {
      setText("phase9Output", `phase9 error: ${error.message}`);
    } finally {
      setButtonLoading(quickBtn, false);
      setButtonLoading(fullBtn, false);
    }
  }

  function renderPhase9Row(label, payload) {
    const status = payload.skipped_heavy ? "SKIPPED_HEAVY" : payload.best_decision || payload.decision || payload.status || (payload.error ? "ERROR" : "RESEARCH_ONLY");
    const metric = payload.best_candidate_id
      ? `best=${payload.best_candidate_id}`
      : payload.trades_total !== undefined
        ? `trades=${payload.trades_total}`
        : payload.contexts_count !== undefined
          ? `contexts=${payload.contexts_count}`
          : payload.summary
            ? JSON.stringify(payload.summary)
            : payload.cli_command || "research";
    const note = payload.skipped_heavy
      ? `run CLI: ${payload.cli_command || "see output"}`
      : `research_only=${String(payload.research_only !== false)}; paper_filter=false; NO LIVE`;
    return `<tr>
      <td>${escapeHtml(label)}</td>
      <td>${escapeHtml(status)}</td>
      <td>${escapeHtml(metric)}</td>
      <td>${escapeHtml(note)}</td>
    </tr>`;
  }

  function updatePhase9Cards(key, payload) {
    if (payload.skipped_heavy) {
      const skippedText = `720h dashboard run skipped. CLI: ${payload.cli_command || "see output"}`;
      if (key === "readiness") {
        setText("phase9ReadinessState", "SKIPPED_HEAVY");
        setText("phase9ReadinessText", skippedText);
      } else if (key === "diagnosis" || key === "filter") {
        setText("phase9FoldState", "SKIPPED_HEAVY");
        setText("phase9FoldText", skippedText);
      } else if (key === "netProfit") {
        setText("phase9NetProfitState", "SKIPPED_HEAVY");
        setText("phase9NetProfitText", skippedText);
      } else if (key === "fastSignal") {
        setText("phase9FastSignalState", "SKIPPED_HEAVY");
        setText("phase9FastSignalText", skippedText);
      }
      return;
    }
    if (key === "readiness") {
      setText("phase9ReadinessState", payload.best_decision || payload.error || "NEED_DATA");
      setText("phase9ReadinessText", `best=${payload.best_candidate_id || "none"}; paper_filter=false; manual review only.`);
      setText("phase9GateState", `wf/cost: ${payload.best_decision || "unknown"}`);
      setText("phase9GateText", "Cost stress, walk-forward, anti-overfit, freshness and sample gates remain mandatory.");
    } else if (key === "diagnosis") {
      setText("phase9FoldState", payload.decision || "NEED_DATA");
      setText("phase9FoldText", `folds=${payload.folds || 0}; trades=${payload.trades_total || 0}; no auto-promotion.`);
    } else if (key === "filter") {
      setText("phase9GateState", payload.decision || "FILTER_RESEARCH_ONLY");
      setText("phase9GateText", `filters=${Array.isArray(payload.results) ? payload.results.length : 0}; paper/demo still disabled.`);
    } else if (key === "netProfit") {
      const best = Array.isArray(payload.scenarios) ? payload.scenarios.find((s) => s.scenario === "stress_0_25") || payload.scenarios[0] : null;
      setText("phase9NetProfitState", best ? fmt(best.net_ev, 6) : "NEED_DATA");
      setText("phase9NetProfitText", best ? `${best.scenario}: pf=${fmt(best.net_pf, 4)} promotion_eligible=${String(best.promotion_eligible)}` : "No net-profit sample loaded.");
    } else if (key === "fastSignal") {
      setText("phase9FastSignalState", payload.summary ? JSON.stringify(payload.summary) : "NEED_DATA");
      setText("phase9FastSignalText", "ENTER_NOW is still shadow-only and blocked if data freshness is stale.");
    }
  }

  async function loadResearchPack() {
    const button = $("researchPackBtn");
    setButtonLoading(button, true, "Generando...");
    try {
      const text = await fetchText("/api/research-pack?hours=24&format=text");
      setText("phase9Output", text);
    } catch (error) {
      setText("phase9Output", `research pack error: ${error.message}`);
    } finally {
      setButtonLoading(button, false);
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    bindActions();
    localTimes();
    setInterval(localTimes, 1000);
    renderStatusFallback();
    loadStatus();
    // Wire the V6 cockpit refresh + pack buttons after DOM is ready.
    $("v6OverviewRefreshBtn")?.addEventListener("click", refreshV6Cockpit);
    $("v6OverviewPackBtn")?.addEventListener("click", refreshV6PackButton);
    // Initial cockpit population — light fetches only.
    refreshV6Cockpit().catch(() => {});
    $("v75RefreshBtn")?.addEventListener("click", refreshV75Strip);
    $("v75PackBtn")?.addEventListener("click", downloadV75Pack);
    refreshV75Strip().catch(() => {});
    $("v8v9RefreshBtn")?.addEventListener("click", refreshV8V9Strip);
    refreshV8V9Strip().catch(() => {});
  });

  // ResearchOps V7.5 helpers --------------------------------------------------
  function _v75SetCard(id, status, value, sub) {
    const card = $(id);
    if (!card) return;
    if (status) card.setAttribute("data-status", status);
    const v = card.querySelector(".v75-value");
    if (v && value !== undefined) v.textContent = value;
    const s = card.querySelector(".v75-sub");
    if (s && sub !== undefined) s.textContent = sub;
  }

  async function refreshV75Strip() {
    const btn = $("v75RefreshBtn");
    setButtonLoading(btn, true);
    try {
      // Duplicate guard hook
      try {
        const p = await fetchJson("/api/research/duplicate-guard-hook-status");
        const tone = p.enabled ? (p.actual_block_count > 0 ? "warn" : "ok") : "info";
        _v75SetCard("v75CardDupHook", tone,
          p.enabled ? (p.mode || "audit") : "disabled",
          `would_block ${p.would_block_count ?? 0} · actual_block ${p.actual_block_count ?? 0}`);
      } catch (e) { /* tolerate */ }
      // Funding model
      try {
        const p = await fetchJson("/api/research/funding-cost-model?hours=720");
        const tone = p.funding_data_status === "OK" ? "ok" : (p.funding_data_status === "NEED_DATA" ? "warn" : "info");
        _v75SetCard("v75CardFunding", tone,
          p.funding_data_status || "UNKNOWN",
          `table_present: ${p.table_present === true}`);
      } catch (e) { /* tolerate */ }
      // Liquidation
      try {
        const p = await fetchJson("/api/research/liquidation-model-bitget?symbol=DOTUSDT&leverage=5&capital=40&margin=5");
        const risk = String(p.liquidation_risk || "UNKNOWN");
        const tone = risk === "LOW" ? "ok" : risk === "MEDIUM" ? "warn" : "bad";
        _v75SetCard("v75CardLiquidation", tone,
          `${(p.liquidation_distance_pct ?? 0).toFixed(2)}% (${risk})`,
          `tier_source: ${p.tier_source || "-"}`);
      } catch (e) { /* tolerate */ }
      // WF V2 (light call, hours=72 to evitar SKIPPED_HEAVY)
      try {
        const p = await fetchJson("/api/research/walk-forward-v2?hours=72");
        if (p.status === "SKIPPED_HEAVY") {
          _v75SetCard("v75CardWfv2", "info", "skipped", "use CLI for full window");
        } else {
          const tone = p.decision === "WF2_PROMISING_LABEL_ONLY" ? "ok"
            : p.decision === "WF2_REJECT" ? "bad" : "info";
          _v75SetCard("v75CardWfv2", tone,
            String(p.decision || "UNKNOWN"),
            `folds ${p.n_folds ?? 0} · positive ${p.positive_folds ?? 0}`);
        }
      } catch (e) { /* tolerate */ }
    } finally {
      setButtonLoading(btn, false);
    }
  }

  // ResearchOps V8/V9 helpers ----------------------------------------------
  async function refreshV8V9Strip() {
    const btn = $("v8v9RefreshBtn");
    setButtonLoading(btn, true);
    try {
      try {
        const p = await fetchJson("/api/research/auto-data-enrichment-status?hours=24");
        const tone = p.symbols_ok && p.symbols_ok.length > 0 ? "ok"
          : (p.symbols_partial && p.symbols_partial.length > 0 ? "warn" : "info");
        const ok = (p.symbols_ok || []).length;
        const partial = (p.symbols_partial || []).length;
        const need = (p.symbols_need_data || []).length;
        _v75SetCard("v8v9CardEnrichment", tone,
          `ok ${ok} · partial ${partial} · need ${need}`,
          `tf ${p.timeframe} · ${p.hours}h`);
      } catch (e) { /* tolerate */ }
      try {
        const p = await fetchJson("/api/research/exit-intelligence-lab?hours=24");
        const tone = p.need_more_data ? "info" : (p.best_delta_pct > 0 ? "ok" : "warn");
        _v75SetCard("v8v9CardExit", tone,
          p.need_more_data ? "NEED_MORE_DATA" : (p.best_policy || "-"),
          `delta ${(p.best_delta_pct ?? 0).toFixed(4)} · n ${p.samples ?? 0}`);
      } catch (e) { /* tolerate */ }
      try {
        const p = await fetchJson("/api/research/strategy-experiment-registry");
        const total = p.total ?? 0;
        _v75SetCard("v8v9CardRegistry", total > 0 ? "ok" : "info",
          `total ${total}`,
          Object.keys(p.by_state || {}).slice(0, 3).map(k => `${k}:${p.by_state[k]}`).join(" · "));
      } catch (e) { /* tolerate */ }
      try {
        const p = await fetchJson("/api/research/shadow-candidate-lifecycle");
        const total = p.total ?? 0;
        _v75SetCard("v8v9CardLifecycle", total > 0 ? "ok" : "info",
          `total ${total}`,
          Object.keys(p.by_proposed_state || {}).slice(0, 3).map(k => `${k}:${p.by_proposed_state[k]}`).join(" · "));
      } catch (e) { /* tolerate */ }
      try {
        const p = await fetchJson("/api/research/validation-gates-v9");
        const overall = p.overall_status || "NEED_MORE_DATA";
        const tone = overall === "PASS" ? "ok" : overall === "FAIL" ? "bad" : "info";
        _v75SetCard("v8v9CardGates", tone, overall,
          `pass ${p.pass_count ?? 0} · fail ${p.fail_count ?? 0} · need ${p.need_data_count ?? 0}`);
      } catch (e) { /* tolerate */ }
    } finally {
      setButtonLoading(btn, false);
    }
  }

  async function downloadV75Pack() {
    const btn = $("v75PackBtn");
    setButtonLoading(btn, true);
    try {
      const payload = await fetchJson("/api/research-pack-v7-5?hours=24");
      const text = payload.text || JSON.stringify(payload, null, 2);
      const ok = await copyText(text);
      if (!ok) {
        const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url; a.download = "research_pack_v7_5.txt"; a.click();
        URL.revokeObjectURL(url);
      }
    } catch (e) { /* tolerate */ }
    finally { setButtonLoading(btn, false); }
  }

  // ResearchOps V6 — Operator Cockpit helpers --------------------------------

  function _v6SetCard(id, status, value, sub, foot) {
    const card = $(id);
    if (!card) return;
    if (status) card.setAttribute("data-status", status);
    const v = card.querySelector(".v6-card-value");
    if (v && value !== undefined) v.textContent = value;
    const s = card.querySelector(".v6-card-sub");
    if (s && sub !== undefined) s.textContent = sub;
    const f = card.querySelector(".v6-card-foot");
    if (f && foot !== undefined) f.textContent = foot;
  }

  function _v6Status(input) {
    const s = String(input || "UNKNOWN").toUpperCase();
    if (s === "OK") return "ok";
    if (s === "WARNING" || s === "WARN") return "warn";
    if (s === "BAD" || s === "FAIL" || s === "STALE" || s === "NEED_DATA" || s === "GAP") return "bad";
    if (s === "SHADOW_ONLY" || s === "SHADOW") return "shadow";
    if (s === "INFO") return "info";
    return "unknown";
  }

  function _v6RenderBlockers(items) {
    const list = $("v6BlockersList");
    const countBadge = $("v6BlockersCount");
    if (!list) return;
    if (!items.length) {
      list.innerHTML = `<li class="v6-blocker-item" data-severity="ok"><span class="v6-blocker-tag">OK</span><span class="v6-blocker-text">Sin bloqueos detectados. Mantener research.</span></li>`;
      if (countBadge) { countBadge.textContent = "0 bloqueos"; countBadge.className = "badge badge-safe"; }
      return;
    }
    list.innerHTML = items.map((b) => `<li class="v6-blocker-item" data-severity="${b.severity}"><span class="v6-blocker-tag">${escapeHtml(b.tag)}</span><span class="v6-blocker-text"><strong>${escapeHtml(b.title)}</strong> — ${escapeHtml(b.detail)}<br><em style="color:var(--muted);">next: ${escapeHtml(b.next_action || "-")}</em></span></li>`).join("");
    if (countBadge) {
      countBadge.textContent = `${items.length} bloqueos`;
      countBadge.className = "badge " + (items.some((b) => b.severity === "bad") ? "badge-danger" : "badge-warning");
    }
  }

  async function refreshV6Cockpit() {
    const btn = $("v6OverviewRefreshBtn");
    setButtonLoading(btn, true);
    const blockers = [];
    try {
      // Worker status + git from /api/training/status (cheap).
      try {
        const statusPayload = await fetchJson("/api/training/status");
        if (statusPayload && statusPayload.git) {
          const gitTxt = String(statusPayload.git).slice(0, 40);
          setText("v6CommandGit", `git: ${gitTxt}`);
        }
        const workerOk = statusPayload?.health?.toLowerCase?.() === "ok" || statusPayload?.worker_lock?.status === "OK";
        const wb = $("v6CommandWorkerBadge");
        if (wb) {
          wb.textContent = `worker: ${workerOk ? "OK" : "unknown"}`;
          wb.className = `badge ${workerOk ? "badge-safe" : "badge-muted"}`;
        }
      } catch (e) { /* tolerate missing endpoint */ }
      setText("v6CommandLastScan", `last scan: ${new Date().toISOString().slice(0, 19).replace("T", " ")} UTC`);

      // Freshness
      try {
        const fr = await fetchJson("/api/research/ohlcv-freshness-status?symbols=BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,BNBUSDT,LINKUSDT,AVAXUSDT,ADAUSDT,DOTUSDT&timeframes=5m,15m,1h");
        const ok = fr.ok_count ?? 0;
        const stale = fr.stale_count ?? 0;
        const need = fr.need_data_count ?? 0;
        const gap = fr.gap_count ?? 0;
        const status = (stale + need + gap === 0) ? "ok" : (need >= 5 ? "bad" : "warn");
        const newest = (fr.rows || []).map((r) => r.newest_timestamp).filter(Boolean).sort().slice(-1)[0] || "-";
        _v6SetCard("v6CardFreshness", status,
          fr.overall_actionable ? "All fresh" : `${stale + need + gap} not fresh`,
          `ok ${ok} · stale ${stale} · need ${need} · gap ${gap}`,
          `last candle: ${newest}`);
        if (stale + need + gap > 0) {
          blockers.push({
            severity: status === "bad" ? "bad" : "warn",
            tag: "OHLCV_STALE",
            title: "OHLCV data not fresh",
            detail: `${stale} stale · ${need} missing · ${gap} gap`,
            next_action: "python -m app.research_lab ohlcv-freshness-refresh --apply --allow-real-writes",
          });
        }
      } catch (e) { /* keep card UNKNOWN */ }

      // Clean research metrics (V6)
      let cleanPayload = null;
      try {
        cleanPayload = await fetchJson("/api/research/clean-research-metrics?hours=24");
        const dq = String(cleanPayload.data_quality_status || "UNKNOWN").toUpperCase();
        const dqStatus = dq === "OK" ? "ok" : dq === "WARNING" ? "warn" : dq === "BAD" ? "bad" : "unknown";
        _v6SetCard("v6CardDataQuality", dqStatus, dq,
          `duplicate rate: ${(cleanPayload.duplicate_rate ?? 0).toFixed(4)}`,
          `raw ${cleanPayload.raw_sample_count ?? 0} · clean ${cleanPayload.clean_sample_count ?? 0}`);
        // Populate raw vs clean panel.
        setText("v6RawSampleValue", String(cleanPayload.raw_sample_count ?? 0));
        setText("v6CleanSampleValue", String(cleanPayload.clean_sample_count ?? 0));
        setText("v6DuplicateRateValue", `${((cleanPayload.duplicate_rate ?? 0) * 100).toFixed(2)}%`);
        setText("v6DuplicateImpactValue", `${((cleanPayload.duplicate_impact_pct ?? 0)).toFixed(4)}%`);
        setText("v6RawEvValue", (cleanPayload.raw_ev_pct ?? 0).toFixed(6));
        setText("v6CleanEvValue", (cleanPayload.clean_ev_pct ?? 0).toFixed(6));
        if (dq === "BAD") {
          blockers.push({
            severity: "bad",
            tag: "DATA_QUALITY",
            title: "Data Quality BAD — duplicate_rate over threshold",
            detail: `duplicate_rate=${((cleanPayload.duplicate_rate ?? 0) * 100).toFixed(2)}% — research rankings on RAW are not trustworthy.`,
            next_action: "python -m app.research_lab training-clean-view-audit --hours 720",
          });
        }
        if ((cleanPayload.clean_sample_count ?? 0) < 15) {
          blockers.push({
            severity: "warn",
            tag: "LOW_SAMPLE",
            title: "Clean sample count too low",
            detail: `clean_sample_count=${cleanPayload.clean_sample_count ?? 0} — readings are not yet reliable.`,
            next_action: "Continue shadow data collection",
          });
        }
        if ((cleanPayload.raw_ev_pct ?? 0) > 0 && (cleanPayload.clean_ev_pct ?? 0) <= 0) {
          blockers.push({
            severity: "bad",
            tag: "RAW_CLEAN_DIFF",
            title: "RAW positive but CLEAN negative",
            detail: "Promoting on RAW would chase duplicates that wipe out the apparent edge.",
            next_action: "Use clean metrics only; never promote raw",
          });
        }
      } catch (e) { /* leave card UNKNOWN */ }

      // Strategy Research Enhancer (CLEAN-aware decision + best leads)
      try {
        const enh = await fetchJson("/api/research/strategy-research-enhancer?hours=24&symbols=BTCUSDT,ETHUSDT,DOTUSDT");
        const dec = String(enh.overall_decision || "UNKNOWN").toUpperCase();
        const tone = dec === "RESEARCH_PROMISING" ? "ok" : dec === "SHADOW_ONLY" ? "shadow" : dec.startsWith("REJECT_") ? "bad" : "warn";
        _v6SetCard("v6CardEdge", tone, dec,
          `clean net EV: ${(enh.clean_metrics?.clean_ev_pct ?? 0).toFixed(6)}`,
          `clean PF: ${(enh.clean_metrics?.clean_pf ?? 0).toFixed(4)} · promotable: ${dec === "RESEARCH_PROMISING" ? "review only" : "false"}`);
        // Best leads table.
        const target = $("v6BestLeadsRows");
        if (target) {
          const top = (enh.rankings_by_symbol || []).slice(0, 10);
          if (top.length === 0) {
            target.innerHTML = "<tr><td colspan=\"10\">Sin pistas todavía. Carga la sección ResearchOps V5 para generar shadow trades.</td></tr>";
          } else {
            target.innerHTML = top.map((r, idx) => {
              const why = r.decision === "RESEARCH_PROMISING" ? "research only" : (r.reasons && r.reasons[0]) || "—";
              return `<tr>
                <td>${idx + 1}</td>
                <td>${escapeHtml(r.key)}</td>
                <td>—</td>
                <td>—</td>
                <td>${escapeHtml(fmt(r.net_ev_pct, 4))}</td>
                <td>${escapeHtml(fmt(r.net_pf, 4))}</td>
                <td>${escapeHtml(String(r.trades))}</td>
                <td>${escapeHtml(r.confidence)}</td>
                <td>${escapeHtml(r.decision)}</td>
                <td>${escapeHtml(why)}</td>
              </tr>`;
            }).join("");
          }
        }
        // Worst side / symbol.
        const sideRanking = enh.rankings_by_side || [];
        const worstSide = sideRanking.filter((r) => r.net_ev_pct < 0).sort((a, b) => a.net_ev_pct - b.net_ev_pct).slice(0, 3);
        const worstSym = (enh.rankings_by_symbol || []).filter((r) => r.net_ev_pct < 0).sort((a, b) => a.net_ev_pct - b.net_ev_pct).slice(0, 5);
        const sideList = $("v6WorstSideList");
        if (sideList) sideList.innerHTML = worstSide.length ? worstSide.map((r) => `<li><strong>${escapeHtml(r.key)}</strong> · clean EV ${fmt(r.net_ev_pct, 4)} · n=${r.trades}</li>`).join("") : "<li>Sin datos suficientes.</li>";
        const symList = $("v6WorstSymbolList");
        if (symList) symList.innerHTML = worstSym.length ? worstSym.map((r) => `<li><strong>${escapeHtml(r.key)}</strong> · clean EV ${fmt(r.net_ev_pct, 4)} · n=${r.trades}</li>`).join("") : "<li>Sin datos suficientes.</li>";
        if (dec === "REJECT_NEGATIVE_NET") {
          blockers.push({
            severity: "bad", tag: "NEGATIVE_NET",
            title: "Clean net EV is negative",
            detail: "Promotion blocked because the de-duplicated dataset gives net EV ≤ 0.",
            next_action: "Iterate filters (SHORT-only, anti-late, time-death reducer)",
          });
        }
        if (dec === "NEED_MORE_DATA") {
          blockers.push({
            severity: "warn", tag: "MORE_DATA",
            title: "Need more clean samples",
            detail: "Shadow trades available are below the promising threshold.",
            next_action: "Run shadow-multi-trade-replay over 720h",
          });
        }
      } catch (e) { /* keep card UNKNOWN */ }

      // Shadow learning quick read.
      try {
        const sh = await fetchJson("/api/research/shadow-multi-trade-status?hours=24");
        if (sh.status !== "SKIPPED_HEAVY") {
          const closed = (sh.trades || []).filter((t) => String(t.status).startsWith("CLOSED_"));
          const netPos = closed.filter((t) => t.net_pnl_pct > 0).length;
          const netNeg = closed.filter((t) => t.net_pnl_pct < 0).length;
          const bySymbol = {};
          for (const t of closed) bySymbol[t.symbol] = (bySymbol[t.symbol] || 0) + t.net_pnl_pct;
          const entries = Object.entries(bySymbol).sort((a, b) => b[1] - a[1]);
          const best = entries[0] ? `${entries[0][0]} ${entries[0][1].toFixed(2)}%` : "-";
          const worst = entries.length ? entries[entries.length - 1] : null;
          const worstStr = worst ? `${worst[0]} ${worst[1].toFixed(2)}%` : "-";
          const tone = netNeg > netPos && closed.length >= 10 ? "warn" : "shadow";
          _v6SetCard("v6CardShadow", tone, `${closed.length} closed`,
            `net+ ${netPos} · net- ${netNeg}`,
            `best: ${best} · worst: ${worstStr}`);
        }
      } catch (e) { /* keep default */ }

      // Always-on blockers based on safety flags.
      blockers.push({
        severity: "muted",
        tag: "LIVE_DISABLED",
        title: "Live trading explicitly disabled",
        detail: "LIVE_TRADING=False · can_send_real_orders=False · ENABLE_PAPER_POLICY_FILTER=False.",
        next_action: "No action required — invariant by design.",
      });
      blockers.push({
        severity: "muted",
        tag: "PAPER_FILTER_OFF",
        title: "Paper filter disabled by design",
        detail: "PAPER_DEMO_READY_MANUAL_REVIEW_ONLY is a label — not an activation.",
        next_action: "Human review only; never auto-activate.",
      });
      // Update readiness card.
      const hasBad = blockers.some((b) => b.severity === "bad");
      const hasWarn = blockers.some((b) => b.severity === "warn");
      _v6SetCard("v6CardReadiness", "danger", "NO PAPER · NO LIVE",
        hasBad ? "hard blockers present" : hasWarn ? "warnings present" : "manual review only",
        `next action: ${hasBad ? "fix data quality + net EV" : hasWarn ? "keep collecting clean samples" : "keep research"}`);
      setText("v6CommandNextAction", `Next action: ${hasBad ? "Resolver bloqueos rojos antes de seguir." : hasWarn ? "Seguir investigando con CLEAN metrics." : "Mantener research, no promocionar."}`);
      _v6RenderBlockers(blockers);
    } finally {
      setButtonLoading(btn, false);
    }
  }

  async function refreshV6PackButton() {
    const btn = $("v6OverviewPackBtn");
    setButtonLoading(btn, true);
    try {
      const payload = await fetchJson("/api/research-pack-v5?hours=24");
      const text = payload.text || JSON.stringify(payload, null, 2);
      const ok = await copyText(text);
      if (!ok) {
        const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url; a.download = "research_pack_v5.txt"; a.click();
        URL.revokeObjectURL(url);
      }
    } catch (e) { /* tolerate */ }
    finally { setButtonLoading(btn, false); }
  }

  // ResearchOps V5.1 helpers -------------------------------------------------
  let v51LastShadowPayload = null;
  let v51LastEnhancerJson = "";

  function _v51SetCockpit(cardId, status, value, sub, foot) {
    const card = $(cardId);
    if (!card) return;
    if (status) card.setAttribute("data-status", status);
    const v = card.querySelector(".v51-cockpit-value");
    if (v && value !== undefined) v.textContent = value;
    const s = card.querySelector(".v51-cockpit-sub");
    if (s && sub !== undefined) s.textContent = sub;
    const f = card.querySelector(".v51-cockpit-foot");
    if (f && foot !== undefined) f.textContent = foot;
  }

  function _v51CockpitFromFreshness(payload) {
    if (!payload) return;
    const ok = payload.ok_count ?? 0;
    const stale = payload.stale_count ?? 0;
    const need = payload.need_data_count ?? 0;
    const gap = payload.gap_count ?? 0;
    let status = "ok";
    if (stale + need + gap > 0) status = stale > 0 ? "warn" : "warn";
    if (need >= ok && ok === 0) status = "bad";
    const newest = (payload.rows || []).map((r) => r.newest_timestamp).filter(Boolean).sort().slice(-1)[0] || "-";
    _v51SetCockpit("v51CockpitFreshness", status,
      payload.overall_actionable ? "All fresh" : `${stale + need + gap} not fresh`,
      `ok ${ok} · stale ${stale} · need ${need} · gap ${gap}`,
      `newest: ${newest}`);
  }

  function _v51CockpitFromCleanView(payload) {
    if (!payload) return;
    const status = (payload.overall_status || "UNKNOWN").toUpperCase();
    const tone = status === "OK" ? "ok" : status === "WARNING" ? "warn" : status === "BAD" ? "bad" : "unknown";
    _v51SetCockpit("v51CockpitDataQuality", tone, status,
      `duplicate rate: ${(payload.duplicate_rate ?? 0).toFixed(4)}`,
      `raw ${payload.raw_sample_count ?? 0} · clean ${payload.clean_sample_count ?? 0}`);
  }

  function _v51CockpitFromShadow(payload) {
    if (!payload) return;
    const closed = (payload.trades || []).filter((t) => String(t.status).startsWith("CLOSED_"));
    const netPos = closed.filter((t) => t.net_pnl_pct > 0).length;
    const netNeg = closed.filter((t) => t.net_pnl_pct < 0).length;
    const ggNetNeg = closed.filter((t) => t.gross_pnl_pct > 0 && t.net_pnl_pct < 0).length;
    const bySymbol = {};
    for (const t of closed) {
      bySymbol[t.symbol] = (bySymbol[t.symbol] || 0) + t.net_pnl_pct;
    }
    const entries = Object.entries(bySymbol).sort((a, b) => b[1] - a[1]);
    const best = entries[0] ? `${entries[0][0]} (${entries[0][1].toFixed(2)}%)` : "-";
    const worst = entries.length ? entries[entries.length - 1] : null;
    const worstStr = worst ? `${worst[0]} (${worst[1].toFixed(2)}%)` : "-";
    let tone = "shadow";
    if (netNeg > netPos && closed.length >= 10) tone = "warn";
    _v51SetCockpit("v51CockpitShadow", tone, `${closed.length} closed`,
      `net+ ${netPos} · net- ${netNeg} · gg-net-neg ${ggNetNeg}`,
      `best: ${best} · worst: ${worstStr}`);
  }

  function _v51CockpitFromEnhancer(payload) {
    if (!payload) return;
    const dec = String(payload.overall_decision || "UNKNOWN").toUpperCase();
    let tone = "info";
    if (dec === "REJECT_DATA_QUALITY" || dec === "REJECT_NEGATIVE_NET" || dec === "REJECT_COSTS" || dec === "REJECT_OVERFIT_RISK") tone = "bad";
    else if (dec === "RESEARCH_PROMISING") tone = "ok";
    else if (dec === "NEED_MORE_DATA") tone = "muted";
    else if (dec === "SHADOW_ONLY") tone = "shadow";
    _v51SetCockpit("v51CockpitReadiness", "danger", "NO LIVE",
      `NO paper/demo · paper filter disabled`,
      `enhancer: ${dec}`);
  }

  function renderV51ShadowFiltered() {
    if (!v51LastShadowPayload) return;
    const trades = Array.isArray(v51LastShadowPayload.trades) ? v51LastShadowPayload.trades : [];
    const symbolFilter = ($("v51ShadowFilterSymbol")?.value || "").trim().toUpperCase();
    const sideFilter = $("v51ShadowFilterSide")?.value || "";
    const statusFilter = $("v51ShadowFilterStatus")?.value || "";
    const pnlFilter = $("v51ShadowFilterPnl")?.value || "";
    const sort = $("v51ShadowSort")?.value || "recent";
    const limit = Math.max(10, Math.min(500, num($("v51ShadowLimit")?.value, 60)));
    let filtered = trades.filter((t) => {
      if (symbolFilter && t.symbol !== symbolFilter) return false;
      if (sideFilter && t.side !== sideFilter) return false;
      if (statusFilter && t.status !== statusFilter) return false;
      if (pnlFilter === "positive" && !(t.net_pnl_pct > 0)) return false;
      if (pnlFilter === "negative" && !(t.net_pnl_pct < 0)) return false;
      return true;
    });
    if (sort === "best_net") filtered.sort((a, b) => b.net_pnl_pct - a.net_pnl_pct);
    else if (sort === "worst_net") filtered.sort((a, b) => a.net_pnl_pct - b.net_pnl_pct);
    filtered = filtered.slice(0, limit);
    const target = $("v5ShadowRows");
    if (target) {
      target.innerHTML = filtered.length
        ? filtered.map((t) => `<tr>
            <td><code>${escapeHtml(t.shadow_id)}</code></td>
            <td>${escapeHtml(t.symbol)}</td>
            <td>${escapeHtml(t.side)}</td>
            <td>${escapeHtml(t.setup_id)}</td>
            <td>${escapeHtml(t.status)}</td>
            <td>${escapeHtml(fmt(t.gross_pnl_pct, 4))}</td>
            <td>${escapeHtml(fmt(t.net_pnl_pct, 4))}</td>
            <td>${escapeHtml(fmt(t.gross_pnl_usdt, 4))}</td>
            <td>${escapeHtml(fmt(t.net_pnl_usdt, 4))}</td>
            <td>${escapeHtml(String(t.bars_open))}</td>
          </tr>`).join("")
        : "<tr><td colspan=\"10\">No trades match the filters.</td></tr>";
    }
    // Update inline summary chips.
    const closed = trades.filter((t) => String(t.status).startsWith("CLOSED_"));
    const netPos = closed.filter((t) => t.net_pnl_pct > 0).length;
    const netNeg = closed.filter((t) => t.net_pnl_pct < 0).length;
    const ggNetNeg = closed.filter((t) => t.gross_pnl_pct > 0 && t.net_pnl_pct < 0).length;
    const bySymbol = {};
    for (const t of closed) bySymbol[t.symbol] = (bySymbol[t.symbol] || 0) + t.net_pnl_pct;
    const sorted = Object.entries(bySymbol).sort((a, b) => b[1] - a[1]);
    const best = sorted[0] ? `${sorted[0][0]} ${sorted[0][1].toFixed(2)}%` : "-";
    const worst = sorted.length ? sorted[sorted.length - 1] : null;
    const worstStr = worst ? `${worst[0]} ${worst[1].toFixed(2)}%` : "-";
    const row = $("v51ShadowSummaryRow");
    if (row) {
      row.innerHTML = `
        <span class="v51-chip" data-tone="info">total: ${trades.length}</span>
        <span class="v51-chip" data-tone="ok">net+: ${netPos}</span>
        <span class="v51-chip" data-tone="bad">net-: ${netNeg}</span>
        <span class="v51-chip" data-tone="warn">gg-net-neg: ${ggNetNeg}</span>
        <span class="v51-chip" data-tone="muted">best: ${escapeHtml(best)}</span>
        <span class="v51-chip" data-tone="muted">worst: ${escapeHtml(worstStr)}</span>
        <span class="v51-chip" data-tone="shadow">SHADOW_ONLY</span>
      `;
    }
  }

  function copyV51ShadowReport() {
    if (!v51LastShadowPayload || !v51LastShadowPayload.text) return;
    copyText(v51LastShadowPayload.text).then(() => {});
  }

  async function loadV51StrategyEnhancer() {
    const button = $("v51EnhancerRefreshBtn");
    setButtonLoading(button, true);
    try {
      const hours = num($("v51EnhancerHours")?.value, 24);
      const symbols = ($("v51EnhancerSymbols")?.value || "BTCUSDT,ETHUSDT,DOTUSDT").trim();
      const payload = await fetchJson(`/api/research/strategy-research-enhancer?hours=${hours}&symbols=${encodeURIComponent(symbols)}`);
      if (payload.status === "SKIPPED_HEAVY") {
        setText("v51EnhancerOutput", `SKIPPED_HEAVY\nCLI: ${payload.cli_command || "-"}`);
        return;
      }
      v51LastEnhancerJson = JSON.stringify(payload, null, 2);
      const decBadge = $("v51EnhancerDecisionBadge");
      if (decBadge) {
        const dec = payload.overall_decision || "UNKNOWN";
        decBadge.textContent = dec;
        decBadge.className = "badge " + (
          dec === "RESEARCH_PROMISING" ? "badge-safe" :
          dec === "SHADOW_ONLY" ? "badge-info" :
          dec === "NEED_MORE_DATA" ? "badge-muted" :
          dec.startsWith("REJECT_") ? "badge-danger" : "badge-warning"
        );
      }
      const summary = $("v51EnhancerSummaryRow");
      if (summary) {
        summary.innerHTML = `
          <span class="v51-chip" data-tone="${payload.overall_decision === 'RESEARCH_PROMISING' ? 'ok' : payload.overall_decision === 'SHADOW_ONLY' ? 'shadow' : payload.overall_decision && payload.overall_decision.startsWith('REJECT_') ? 'bad' : 'muted'}">overall: ${escapeHtml(payload.overall_decision || '-')}</span>
          <span class="v51-chip" data-tone="${payload.data_quality_status === 'BAD' ? 'bad' : payload.data_quality_status === 'WARNING' ? 'warn' : payload.data_quality_status === 'OK' ? 'ok' : 'muted'}">data quality: ${escapeHtml(payload.data_quality_status || '-')}</span>
          <span class="v51-chip" data-tone="info">raw ${payload.raw_sample_count ?? 0} · clean ${payload.clean_sample_count ?? 0}</span>
          <span class="v51-chip" data-tone="muted">duplicate rate: ${(payload.duplicate_rate ?? 0).toFixed(4)}</span>
        `;
      }
      const bySymRows = (payload.rankings_by_symbol || []).slice(0, 12).map((r) => {
        const decTone = r.decision === "RESEARCH_PROMISING" ? "ok" : r.decision === "SHADOW_ONLY" ? "shadow" : r.decision === "NEED_MORE_DATA" ? "muted" : "bad";
        return `<tr>
          <td>${escapeHtml(r.key)}</td>
          <td>${escapeHtml(String(r.trades))}</td>
          <td>${escapeHtml(fmt(r.net_ev_pct, 4))}</td>
          <td>${escapeHtml(fmt(r.net_pf, 4))}</td>
          <td>${escapeHtml(fmt(r.win_rate_net, 3))}</td>
          <td>${escapeHtml(fmt(r.gross_green_net_negative_rate, 3))}</td>
          <td>${escapeHtml(r.confidence)}</td>
          <td><span class="v51-chip" data-tone="${decTone}">${escapeHtml(r.decision)}</span></td>
        </tr>`;
      }).join("");
      const target1 = $("v51EnhancerBySymbolRows");
      if (target1) target1.innerHTML = bySymRows || "<tr><td colspan=\"8\">No data.</td></tr>";
      const bySideRows = (payload.rankings_by_side || []).slice(0, 8).map((r) => {
        const decTone = r.decision === "RESEARCH_PROMISING" ? "ok" : r.decision === "SHADOW_ONLY" ? "shadow" : r.decision === "NEED_MORE_DATA" ? "muted" : "bad";
        return `<tr>
          <td>${escapeHtml(r.key)}</td>
          <td>${escapeHtml(String(r.trades))}</td>
          <td>${escapeHtml(fmt(r.net_ev_pct, 4))}</td>
          <td>${escapeHtml(fmt(r.net_pf, 4))}</td>
          <td>${escapeHtml(fmt(r.win_rate_net, 3))}</td>
          <td>${escapeHtml(fmt(r.gross_green_net_negative_rate, 3))}</td>
          <td>${escapeHtml(r.confidence)}</td>
          <td><span class="v51-chip" data-tone="${decTone}">${escapeHtml(r.decision)}</span></td>
        </tr>`;
      }).join("");
      const target2 = $("v51EnhancerBySideRows");
      if (target2) target2.innerHTML = bySideRows || "<tr><td colspan=\"8\">No data.</td></tr>";
      const ideaList = $("v51EnhancerIdeasList");
      if (ideaList) {
        const ideas = Array.isArray(payload.research_ideas) ? payload.research_ideas : [];
        ideaList.innerHTML = ideas.length
          ? ideas.map((i) => `<li><strong>${escapeHtml(i.name)}</strong> — ${escapeHtml(i.description)} <em style="color:var(--muted);">(${escapeHtml(i.why)})</em></li>`).join("")
          : "<li>No ideas yet.</li>";
      }
      setText("v51EnhancerOutput", payload.text || v51LastEnhancerJson.slice(0, 5000));
      _v51CockpitFromEnhancer(payload);
    } catch (error) {
      setText("v51EnhancerOutput", `enhancer error: ${error.message}`);
    } finally {
      setButtonLoading(button, false);
    }
  }

  // Patch existing V5 load functions to also feed the V5.1 cockpit cards.
  const __origLoadV5FreshnessMatrix = typeof loadV5FreshnessMatrix === "function" ? loadV5FreshnessMatrix : null;
  if (__origLoadV5FreshnessMatrix) {
    window.__loadV5FreshnessMatrixHook = async function () {
      await __origLoadV5FreshnessMatrix();
      try {
        const symbols = ($("v5FreshnessSymbols")?.value || "").trim();
        const timeframes = ($("v5FreshnessTimeframes")?.value || "5m,15m,1h").trim();
        const payload = await fetchJson(`/api/research/ohlcv-freshness-status?symbols=${encodeURIComponent(symbols)}&timeframes=${encodeURIComponent(timeframes)}`);
        _v51CockpitFromFreshness(payload);
      } catch (e) { /* ignore */ }
    };
  }

  // Wrap V5 load functions to feed the cockpit when they finish.
  const __wrapWithCockpit = (origName, cockpitFn) => {
    if (typeof window[origName] !== "function") return;
    const orig = window[origName];
    window[origName] = async function () {
      const result = await orig.apply(this, arguments);
      try { cockpitFn(); } catch (e) { /* ignore */ }
      return result;
    };
  };

  // Hook: after V5 functions run, refresh the cockpit. We use polling on the
  // panel's plain-text output because the original V5 functions are closure-
  // scoped. The cleanest cross-functional integration is to call the cockpit
  // helpers directly from each load function — done above for freshness via a
  // dedicated handler, and below by triggering the cockpit on a refresh button
  // click using a small wrapper attached to the dashboard refresh buttons.
  const __wrapRefreshButton = (btnId, cockpitFetcher) => {
    const btn = document.getElementById(btnId);
    if (!btn || !cockpitFetcher) return;
    btn.addEventListener("click", async () => {
      try { await cockpitFetcher(); } catch (e) { /* ignore */ }
    });
  };
  __wrapRefreshButton("v5FreshnessRefreshBtn", async () => {
    const symbols = ($("v5FreshnessSymbols")?.value || "").trim();
    const timeframes = ($("v5FreshnessTimeframes")?.value || "5m,15m,1h").trim();
    const payload = await fetchJson(`/api/research/ohlcv-freshness-status?symbols=${encodeURIComponent(symbols)}&timeframes=${encodeURIComponent(timeframes)}`);
    _v51CockpitFromFreshness(payload);
  });
  __wrapRefreshButton("v5CleanRefreshBtn", async () => {
    const hours = num($("v5CleanHours")?.value, 24);
    const payload = await fetchJson(`/api/research/training-clean-view-audit?hours=${hours}`);
    _v51CockpitFromCleanView(payload);
  });
  __wrapRefreshButton("v5ShadowRefreshBtn", async () => {
    const hours = num($("v5ShadowHours")?.value, 24);
    const payload = await fetchJson(`/api/research/shadow-multi-trade-status?hours=${hours}`);
    if (payload.status === "SKIPPED_HEAVY") return;
    v51LastShadowPayload = payload;
    renderV51ShadowFiltered();
    _v51CockpitFromShadow(payload);
  });
  __wrapRefreshButton("v5FeeAwareRefreshBtn", async () => {
    const symbols = ($("v5FeeAwareSymbols")?.value || "DOTUSDT").trim();
    const hours = num($("v5FeeAwareHours")?.value, 168);
    const payload = await fetchJson(`/api/research/fee-aware-exit-trainer?symbols=${encodeURIComponent(symbols)}&hours=${hours}`);
    if (payload.status === "SKIPPED_HEAVY") return;
    const promotable = (payload.results || []).filter((r) => r.promotion_eligible);
    const best = (payload.results || []).reduce((acc, r) => r.best_net_ev > (acc?.best_net_ev ?? -Infinity) ? r : acc, null);
    const tone = promotable.length > 0 ? "ok" : (best && best.gross_green_net_negative ? "warn" : "muted");
    _v51SetCockpit("v51CockpitFeeAware", tone,
      promotable.length > 0 ? `${promotable.length} promotable` : "no promotion",
      `best net EV: ${best ? best.best_net_ev.toFixed(6) : "-"}`,
      `npl: ${best ? best.net_profit_lock_pct.toFixed(2) + "%" : "-"}`);
  });
}());
