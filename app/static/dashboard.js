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
      $("reportStatus").textContent = `short report ${copied ? "copied" : "generated"} - ${Math.round(performance.now() - started)} ms - ${(text.length / 1024).toFixed(1)} KB`;
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

    bindSafeNavigation();
  }

  document.addEventListener("DOMContentLoaded", () => {
    bindActions();
    localTimes();
    setInterval(localTimes, 1000);
    renderStatusFallback();
    loadStatus();
  });
}());
