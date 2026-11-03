"use strict";

const CONFIG_KEYS = ["USERTOKEN", "ABC", "AUTH_V", "USER_AGENT", "COURSE_ID", "STUDY_GRADE", "LLM_URL", "LLM_KEY", "LLM_MODEL", "MAX_CONCURRENT_TASKS", "VOCABGO_PROXY_MODE", "VOCABGO_PROXY_URL", "LLM_PROXY_MODE", "LLM_PROXY_URL"];
const JOB_STATE_FIELDS = ["running", "done", "loop", "waiting", "active", "stopped", "status", "has_logs", "round", "exit_code", "recovery_required", "recovery_id", "recovery_reason", "test_completed", "test_end_code", "account_current", "queued", "starting", "queue_position"];
const JOB_TASK_METADATA_FIELDS = ["category", "category_label", "category_reason", "category_checked_at", "can_start", "can_loop_start", "repeatable", "eligibility_reason", "deadline", "deadline_ms", "task_kind_label", "score", "progress"];
const OFFICIAL_TASK_STATUS_FIELDS = ["official_status", "official_status_label", "official_status_reason", "official_status_checked_at", "official_status_stale"];
const TASK_OFFICIAL_STATUS_LABELS = {ongoing: "进行中", ended: "已结束", unknown: "状态待确认"};
const TASK_TAB_LABELS = {executable: "可执行", ended: "已结束"};
const TASK_SOURCE_LABELS = {class: "班级任务", study: "自学任务"};
const TASK_EXECUTION_LABELS = {runnable: "程序可运行", full: "已满分", unsupported: "需在官方端完成", blocked: "当前不可启动", recovery: "需核对", unknown: "资格待确认"};
let currentTasks = [];
let selectedTaskStatus = "executable";
let selectedTaskSource = "class";
let sourceViewEpoch = 0;
let remoteTasks = new Map();
let localJobs = new Map();
let latestJobSeq = -1;
let latestFullJobSeq = -1;
let captureActive = false;
let lastCaptureState = null;
let captureMutationPending = false;
let captureEpoch = 0;
let captureFlight = null;
let captureTimer = null;
let taskEpoch = 0;
let taskFlight = null;
let taskTimer = null;
let taskReloadRequested = false;
let remoteRefreshing = false;
let jobsFlight = null;
let jobsTimer = null;
let configEpoch = 0;
let configPending = false;
let lastTaskConfig = null;
let batchPending = false;
const pendingTasks = new Map();
let taskStatus = "加载中…";
let jobsWarning = "";
const sourceActionWarnings = {class: "", study: ""};
let activeLogKey = null;
let activeLogTask = null;
let logEpoch = 0;
let logFlight = null;
let logTimer = null;
let logReloadRequested = false;
let logCursor = null;
let logLines = [];

function taskKey(task) {
  const source = task.source || "class";
  return JSON.stringify([
    source,
    String(source === "study" ? (task.course_id || "CET4_v2") : "release"),
    String(source === "study" ? (task.list_id || task.release_id) : task.release_id),
    ...(task.account_key ? [task.account_key] : []),
  ]);
}

function escapeHtml(value) {
  return String(value == null ? "" : value).replace(/[&<>"']/g, character => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[character]));
}

async function requestJson(url, options = {}) {
  const controller = new AbortController();
  const abort = () => controller.abort();
  const timeout = setTimeout(abort, ["/api/start", "/api/tasks/recovery/ack"].includes(url) ? 90000 : 10000);
  if (options.signal) {
    if (options.signal.aborted) abort();
    else options.signal.addEventListener("abort", abort, {once: true});
  }
  try {
    const response = await fetch(url, {cache: "no-store", ...options, signal: controller.signal});
    let data;
    try {
      data = await response.json();
    } catch (error) {
      if (error.name === "AbortError") throw error;
      throw new Error(`服务器返回无效内容（HTTP ${response.status}）`);
    }
    if (!data || typeof data !== "object" || Array.isArray(data)) throw new Error("服务器返回无效 JSON 内容");
    if (!response.ok && !data.error) data.error = `请求失败（HTTP ${response.status}）`;
    return data;
  } catch (error) {
    if (error.name === "AbortError" && !(options.signal && options.signal.aborted)) {
      throw new Error("请求超时，请稍后重试");
    }
    throw error;
  } finally {
    clearTimeout(timeout);
    if (options.signal) options.signal.removeEventListener("abort", abort);
  }
}

function postJson(url, payload) {
  return requestJson(url, {
    method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload),
  });
}

function renderTaskStatus() {
  document.getElementById("status").textContent = captureActive
    ? "鉴权获取期间已暂停任务刷新和启动"
    : [`${TASK_SOURCE_LABELS[selectedTaskSource]} ${tasksForSource(currentTasks).length} 个任务`, taskStatus,
      jobsWarning, sourceActionWarnings[selectedTaskSource]].filter(Boolean).join(" · ");
}

function setTaskControlsDisabled(disabled) {
  document.querySelectorAll("#task-card button").forEach(button => {
    const role = button.dataset.role || (button.dataset.source ? "source-switch" : button.dataset.filter ? "filter" : button.id === "start-all" ? "batch" : "refresh");
    const pending = button.dataset.key && pendingTasks.has(button.dataset.key);
    button.disabled = !!disabled || button.dataset.permanentDisabled === "true"
      || (pending && role !== "logs")
      || (batchPending && ["start", "loop", "batch"].includes(role))
      || (role === "refresh" && !!taskFlight);
  });
}

function applyJobs(data, singleJob = null) {
  const seq = Number(data.seq);
  const fullSnapshot = Array.isArray(data.jobs);
  if (!Number.isSafeInteger(seq) || seq < latestJobSeq
    || (seq === latestJobSeq && (!fullSnapshot || latestFullJobSeq >= seq))) return false;
  if (fullSnapshot) {
    localJobs = new Map(data.jobs.map(job => [taskKey(job), {...job}]));
    latestFullJobSeq = seq;
  } else if (singleJob) {
    localJobs.set(taskKey(singleJob), {...singleJob});
  }
  latestJobSeq = seq;
  return true;
}

function applyRemoteTask(task, target = remoteTasks, expectedKey = null) {
  if (!task || typeof task !== "object" || Array.isArray(task)) return false;
  const key = taskKey(task);
  if (expectedKey != null && key !== expectedKey) return false;
  const previous = remoteTasks.get(key);
  const checkedAt = Number(task.category_checked_at);
  const previousCheckedAt = previous ? Number(previous.category_checked_at) : NaN;
  // Eligibility and official task status have independent freshness clocks.
  const olderEligibility = previous && Number.isFinite(checkedAt) && Number.isFinite(previousCheckedAt) && checkedAt < previousCheckedAt;
  const updated = olderEligibility ? {...previous} : {...previous, ...task};
  const officialCheckedAt = Number(task.official_status_checked_at);
  const previousOfficialCheckedAt = previous ? Number(previous.official_status_checked_at) : NaN;
  if (previous && Number.isFinite(previousOfficialCheckedAt) && previousOfficialCheckedAt > 0
    && (!Number.isFinite(officialCheckedAt) || officialCheckedAt < previousOfficialCheckedAt)) {
    OFFICIAL_TASK_STATUS_FIELDS.forEach(field => {
      if (field in previous) updated[field] = previous[field];
      else delete updated[field];
    });
  } else if (olderEligibility && Number.isFinite(officialCheckedAt) && officialCheckedAt > 0) {
    OFFICIAL_TASK_STATUS_FIELDS.forEach(field => { if (field in task) updated[field] = task[field]; });
  }
  if (!olderEligibility && previous && ["ongoing", "ended"].includes(previous.official_status)
    && task.official_status === "unknown" && task.official_status_stale === true) {
    OFFICIAL_TASK_STATUS_FIELDS.forEach(field => { if (field in previous) updated[field] = previous[field]; });
    updated.official_status_stale = true;
    const knownReason = previous.official_status_reason || "上次明确的官方分类";
    updated.official_status_reason = knownReason.includes("本次官方分类未确认") ? knownReason : `${knownReason}；本次官方分类未确认，请刷新重试`;
  }
  target.set(key, updated);
  return !olderEligibility;
}

function mergeTasks() {
  const tasks = new Map(remoteTasks);
  localJobs.forEach((job, key) => {
    const remote = tasks.get(key);
    const sameAccount = !remote || !job.account_key || remote.account_key === job.account_key;
    const hasKnownOfficialStatus = ["ongoing", "ended"].includes(job.official_status)
      && Number.isSafeInteger(Number(job.official_status_checked_at)) && Number(job.official_status_checked_at) > 0;
    const task = remote ? {...remote} : {...job, source_label: job.source === "study" ? "自学" : "班级",
      category: "unknown", category_label: "资格待确认", category_reason: "未取得当前任务信息，请刷新后确认是否可继续。",
      official_status: hasKnownOfficialStatus ? job.official_status : "unknown",
      official_status_label: hasKnownOfficialStatus ? TASK_OFFICIAL_STATUS_LABELS[job.official_status] : "状态待确认",
      official_status_reason: hasKnownOfficialStatus ? `${job.official_status_reason || "上次明确的官方分类"}；未取得当前远端任务，保留上次状态，请刷新任务列表。` : "未取得当前官方任务状态，请刷新任务列表。",
      official_status_stale: true,
      can_start: false, can_loop_start: false};
    const checkedAt = job.category_checked_at;
    const remoteCheckedAt = remote ? Number(remote.category_checked_at) : NaN;
    if (remote && sameAccount && Number.isSafeInteger(checkedAt) && checkedAt > 0
      && ["available", "full", "unsupported", "blocked", "unknown"].includes(job.category)
      && (!Number.isSafeInteger(remoteCheckedAt) || checkedAt > remoteCheckedAt)) {
      const updatedRemote = {...remote};
      JOB_TASK_METADATA_FIELDS.forEach(field => {
        if (field in job) { task[field] = job[field]; updatedRemote[field] = job[field]; }
      });
      remoteTasks.set(key, updatedRemote);
    }
    const officialCheckedAt = Number(job.official_status_checked_at);
    const remoteOfficialCheckedAt = remote ? Number(remote.official_status_checked_at) : NaN;
    if (remote && sameAccount && Number.isSafeInteger(officialCheckedAt) && officialCheckedAt > 0
      && ["ongoing", "ended", "unknown"].includes(job.official_status)
      && (!Number.isSafeInteger(remoteOfficialCheckedAt) || officialCheckedAt > remoteOfficialCheckedAt)) {
      const updatedRemote = {...remoteTasks.get(key)};
      OFFICIAL_TASK_STATUS_FIELDS.forEach(field => {
        if (field in job) { task[field] = job[field]; updatedRemote[field] = job[field]; }
      });
      remoteTasks.set(key, updatedRemote);
    }
    JOB_STATE_FIELDS.forEach(field => {
      if (field in job && (sameAccount || (!field.startsWith("recovery_") && !field.startsWith("test_")))) task[field] = job[field];
    });
    ["source", "task_id", "release_id", "course_id", "list_id"].forEach(field => {
      // Practice IDs may change after creation; release or course/list stays stable.
      if (remote && field === "task_id") return;
      if (job[field] != null) task[field] = job[field];
    });
    // Legacy jobs without dated classification must not restore eligibility.
    tasks.set(key, task);
  });
  currentTasks = Array.from(tasks.values());
  render(currentTasks);
}

function isActive(task) {
  return !!(task.active || task.running || task.waiting || (task.loop && !task.stopped && !task.done));
}

function taskCategory(task) {
  return ["available", "full", "unsupported", "blocked", "unknown"].includes(task.category) ? task.category : "unknown";
}

function taskCategoryReason(task) {
  if (task.recovery_reason) return task.recovery_reason;
  if (task.account_current === false) return "此运行记录属于其他账号，不能在当前账号启动。";
  if (task.test_completed) return "本机已确认本次测试结束，请在词达人官方端核对结果；程序不会重新打开测试";
  if (taskOfficialStatus(task) === "ended" && taskCategory(task) === "available") {
    return task.official_status_reason || "词达人官方任务已结束，当前不可启动。";
  }
  if ((task.source || "class") === "class" && Number(task.task_type) === 2 && taskCategory(task) === "available" && !taskCanStart(task)) {
    return "测试的官方状态已过期或字段尚未确认，请刷新任务列表后再启动。";
  }
  return task.category_reason || task.eligibility_reason || task.note
    || (taskCategory(task) === "unknown" ? "任务执行资格尚未确认，请刷新任务列表。" : "");
}

function taskCanStart(task) {
  if (task.account_current === false || task.recovery_required || task.test_completed || taskOfficialStatus(task) === "ended"
    || ["full", "unsupported", "blocked"].includes(taskCategory(task)) || !task.can_start) return false;
  if ((task.source || "class") === "class" && Number(task.task_type) === 2) {
    const progress = Number(task.progress);
    const score = Number(task.score);
    const checkedAt = Number(task.official_status_checked_at);
    const age = Date.now() - checkedAt;
    return taskOfficialStatus(task) === "ongoing" && !task.stale && !task.official_status_stale
      && Number.isSafeInteger(checkedAt) && checkedAt > 0 && age >= -1000 && age <= 90000
      && Number(task.over_status) === 2 && Number(task.free) === 1
      && typeof task.progress !== "boolean" && task.progress != null && task.progress !== "" && Number.isFinite(progress) && progress >= 0 && progress < 100
      && typeof task.score !== "boolean" && task.score != null && task.score !== "" && Number.isFinite(score) && score >= 0 && score < 100;
  }
  return true;
}

function taskCanLoop(task) {
  return !((task.source || "class") === "class" && Number(task.task_type) === 2)
    && taskCategory(task) === "available" && taskCanStart(task) && task.can_loop_start === true;
}

function taskOfficialStatus(task) {
  return ["ongoing", "ended", "unknown"].includes(task.official_status) ? task.official_status : "unknown";
}

function taskTab(task) {
  return taskOfficialStatus(task) === "ended" ? "ended" : "executable";
}

function taskExecutionStatus(task) {
  if (task.recovery_required) return "recovery";
  if (task.account_current === false) return "blocked";
  const category = taskCategory(task);
  return category === "available" ? (taskCanStart(task) ? "runnable" : "blocked") : category;
}

function tasksForSource(tasks) {
  return tasks.filter(taskIsInCurrentSource);
}

function taskIsInCurrentSource(task) {
  return !!task && (task.source || "class") === selectedTaskSource;
}

function selectTaskSource(source) {
  if (!Object.prototype.hasOwnProperty.call(TASK_SOURCE_LABELS, source) || source === selectedTaskSource) return;
  selectedTaskSource = source;
  selectedTaskStatus = "executable";
  sourceViewEpoch += 1;
  closeLogs();
  render(currentTasks);
}

function renderTaskSources(tasks) {
  document.querySelectorAll("#task-sources button[data-source]").forEach(button => {
    const selected = button.dataset.source === selectedTaskSource;
    button.classList.toggle("selected", selected);
    button.setAttribute("aria-pressed", String(selected));
  });
  const otherSource = selectedTaskSource === "class" ? "study" : "class";
  const otherActive = tasks.filter(task => (task.source || "class") === otherSource && isActive(task)).length;
  const activity = document.getElementById("other-source-activity");
  if (activity) activity.hidden = otherActive === 0;
  const summary = document.getElementById("other-source-summary");
  if (summary) summary.textContent = `${TASK_SOURCE_LABELS[otherSource]}仍有 ${otherActive} 个正在执行或等待执行，可切换查看停止和日志。`;
  const switchButton = document.getElementById("switch-other-source");
  if (switchButton) {
    switchButton.dataset.source = otherSource;
    switchButton.textContent = `查看${TASK_SOURCE_LABELS[otherSource]}`;
  }
}

function renderTaskFilters(tasks) {
  renderTaskSources(tasks);
  const sourceTasks = tasksForSource(tasks);
  const counts = {executable: 0, ended: 0};
  sourceTasks.forEach(task => { counts[taskTab(task)] += 1; });
  document.querySelectorAll("#task-filters button[data-filter]").forEach(button => {
    const status = button.dataset.filter;
    button.textContent = `${TASK_TAB_LABELS[status] || status}（${counts[status] || 0}）`;
    button.classList.toggle("selected", status === selectedTaskStatus);
    button.setAttribute("aria-pressed", String(status === selectedTaskStatus));
  });
  const visible = sourceTasks.filter(task => taskTab(task) === selectedTaskStatus);
  const activeCount = sourceTasks.filter(isActive).length;
  const visibleKeys = new Set(visible.map(taskKey));
  const hiddenActive = sourceTasks.filter(task => isActive(task) && !visibleKeys.has(taskKey(task))).length;
  const summary = document.getElementById("filter-summary");
  if (summary) summary.textContent = `显示 ${visible.length} / ${sourceTasks.length} 个${TASK_SOURCE_LABELS[selectedTaskSource]}`
    + (activeCount ? ` · 执行及等待中 ${activeCount} 个` : "")
    + (hiddenActive ? `（${hiddenActive} 个在另一分类，可切换“可执行 / 已结束”查看停止和日志）` : "");
  const batchButton = document.getElementById("start-all");
  if (batchButton) {
    const eligibleCount = tasks.filter(task => (task.source || "class") === "class" && Number(task.task_type) === 1 && taskCanLoop(task) && !isActive(task)).length;
    batchButton.hidden = selectedTaskSource !== "class";
    batchButton.dataset.permanentDisabled = String(selectedTaskSource !== "class" || !eligibleCount);
    batchButton.title = eligibleCount ? `将 ${eligibleCount} 个可循环的班级学习任务加入队列，按并发上限依次执行。` : "当前没有已确认可循环的班级学习任务。";
  }
  return visible;
}

function patchTaskRows(body, entries) {
  const existing = new Map(Array.from(body.children).map(row => [row.dataset.taskKey, row]));
  const retained = new Set();
  entries.forEach(({key, html}, index) => {
    let row = existing.get(key);
    if (!row) { row = document.createElement("tr"); row.dataset.taskKey = key; }
    if (row.renderedHtml !== html) { row.innerHTML = html; row.renderedHtml = html; }
    retained.add(row);
    if (body.children[index] !== row) body.insertBefore(row, body.children[index] || null);
  });
  Array.from(body.children).forEach(row => { if (!retained.has(row)) row.remove(); });
}

function render(tasks) {
  const body = document.getElementById("tbody");
  const visible = renderTaskFilters(tasks);
  renderTaskStatus();
  if (!visible.length) {
    body.innerHTML = `<tr><td colspan="6" style="color:#667085;text-align:center;padding:24px;">${tasksForSource(tasks).length ? "当前分类没有任务，可切换可执行 / 已结束查看。" : `暂无${TASK_SOURCE_LABELS[selectedTaskSource]}，或当前账号下还没有此类任务。`}</td></tr>`;
    setTaskControlsDisabled(captureActive);
    return;
  }
  const entries = visible.map((task, index) => {
    const key = taskKey(task);
    const attributeKey = escapeHtml(key);
    const rawProgress = Number(task.progress || 0);
    const progress = Number.isFinite(rawProgress) ? Math.min(100, Math.max(0, rawProgress)) : 0;
    const score = task.score == null ? "-" : escapeHtml(task.score);
    const category = taskCategory(task);
    const reason = taskCategoryReason(task);
    const officialStatus = taskOfficialStatus(task);
    const officialReason = task.official_status_reason || "未取得当前官方任务状态，请刷新任务列表。";
    const execution = taskExecutionStatus(task);
    const kind = task.task_kind_label || (task.source === "study" ? "自学" : ({1: "学习", 2: "测试", 6: "PK"})[Number(task.task_type)] || "类型待确认");
    let badge = `<div class="task-badges"><span class="badge source">${escapeHtml(task.source_label || (task.source === "study" ? "自学" : "班级"))}</span><span class="badge task-kind">${escapeHtml(kind)}</span></div>`;
    badge += `<div class="task-badges"><span class="badge official-status ${officialStatus}" title="${escapeHtml(officialReason)}">${escapeHtml(TASK_OFFICIAL_STATUS_LABELS[officialStatus])}</span><span class="badge execution ${execution}" title="${escapeHtml(reason)}">${escapeHtml(TASK_EXECUTION_LABELS[execution])}</span></div>`;
    const pending = pendingTasks.get(key);
    if (pending) badge += `<span class="badge run">${pending === "stop" ? "正在停止…" : pending === "recover" ? "正在重新同步…" : "正在启动…"}</span>`;
    else if (task.status === "stopping") badge += '<span class="badge run">正在停止…</span>';
    else if (task.queued) badge += `<span class="badge run">排队中${task.queue_position ? ` · 第 ${escapeHtml(task.queue_position)} 位` : ""}</span>`;
    else if (task.starting) badge += '<span class="badge run">正在确认任务资格…</span>';
    else if (task.running) badge += `<span class="badge run">${task.loop ? `🔁 循环中（第 ${escapeHtml(task.round || 1)} 轮）` : "运行中"}</span>`;
    else if (task.waiting || (task.active && task.loop)) badge += '<span class="badge run">等待下一轮重启</span>';
    else if (task.recovery_required) badge += '<span class="badge fail">本机提交状态需核对</span>';
    else if (task.stopped || task.status === "stopped") badge += '<span class="badge">已停止</span>';
    else if (task.status === "failed" || (task.exit_code != null && Number(task.exit_code) !== 0)) badge += `<span class="badge" style="color:#b42318;">失败（退出码 ${escapeHtml(task.exit_code == null ? "未知" : task.exit_code)}）</span>`;
    else if (task.done) badge += `<span class="badge done">${task.exit_code === 0 ? "本机流程已完成" : "本机流程已停止"}</span>`;
    const button = (role, label, className = "", permanent = false, title = "") =>
      `<button data-action="${role}" data-role="${role}" data-key="${attributeKey}" data-permanent-disabled="${permanent}" class="${className}" title="${escapeHtml(title)}"${permanent ? " disabled" : ""}>${label}</button>`;
    let actions;
    if (isActive(task)) actions = button("stop", task.status === "stopping" ? "正在停止…" : "停止", "danger", task.status === "stopping");
    else if (task.recovery_required && task.account_current !== false) actions = button("recover", "核对后重新同步", "", !task.recovery_id,
      task.recovery_id ? "先在词达人官方端确认本次操作的结果，再点击重新同步；不会自动启动" : "运行记录损坏或无法读取，请先核对并处理记录");
    else if (taskCanStart(task)) actions = button("start", category === "unknown" ? "▶ 单次尝试" : "▶ 启动", "primary", false, reason)
      + (((task.source || "class") === "class" && Number(task.task_type) === 2) ? "" : button("loop", "🔁 循环", "loop", !taskCanLoop(task), taskCanLoop(task) ? "刷到 100 分为止" : (task.eligibility_reason || "当前任务不允许自动循环。")));
    else actions = button("display", TASK_EXECUTION_LABELS[execution], "", true, reason || "暂不支持启动");
    if (task.has_logs || task.running || task.waiting || task.done || task.stopped || Number(task.round) > 0) actions += button("logs", "日志");
    return {key, html: `<td>${index + 1}</td><td>${escapeHtml(task.task_name)}<div class="task-reason">任务状态：${escapeHtml(officialReason)}</div>${reason ? `<div class="task-reason">执行资格：${escapeHtml(reason)}</div>` : ""}</td><td><div class="progress-bar"><div style="width:${progress}%"></div></div>${progress}%</td><td class="${Number(task.score) >= 100 ? "score full" : "score"}">${score}</td><td>${badge}</td><td>${actions}</td>`};
  });
  patchTaskRows(body, entries);
  setTaskControlsDisabled(captureActive);
}

function scheduleTasks(delay = 30000) {
  clearTimeout(taskTimer);
  taskTimer = null;
  if (!captureActive) taskTimer = setTimeout(() => { taskTimer = null; loadTasks(); }, delay);
}

function scheduleJobs(delay = 1500) {
  clearTimeout(jobsTimer);
  jobsTimer = null;
  if (!captureActive) jobsTimer = setTimeout(() => { jobsTimer = null; refreshJobs(); }, delay);
}

function invalidateTaskRequests() {
  taskEpoch += 1;
  clearTimeout(taskTimer);
  clearTimeout(jobsTimer);
  taskTimer = jobsTimer = null;
  if (taskFlight) taskFlight.controller.abort();
  if (jobsFlight) jobsFlight.controller.abort();
}

function reloadTasksAfterChange() {
  invalidateTaskRequests();
  taskReloadRequested = true;
  if (!captureActive && !taskFlight) loadTasks();
  if (!captureActive && !jobsFlight) refreshJobs();
}

function loadTasks(force = false) {
  if (captureActive) { renderTaskStatus(); return Promise.resolve(); }
  if (taskFlight) { if (force) taskReloadRequested = true; return taskFlight.promise; }
  clearTimeout(taskTimer);
  taskTimer = null;
  force = force || taskReloadRequested;
  taskReloadRequested = false;
  taskStatus = currentTasks.length ? "正在刷新任务（保留当前运行状态）…" : "正在拉取任务…";
  renderTaskStatus();
  const flight = {epoch: taskEpoch, controller: new AbortController(), promise: null};
  taskFlight = flight;
  setTaskControlsDisabled(captureActive);
  flight.promise = (async () => {
    try {
      const data = await requestJson(force ? "/api/tasks?refresh=1" : "/api/tasks", {signal: flight.controller.signal});
      if (flight.epoch !== taskEpoch || captureActive) return;
      if (!data.ok || !Array.isArray(data.tasks)) throw new Error(data.error || "读取任务失败");
      // Partial upstream failures keep the existing rows and all local jobs visible.
      const next = (data.warnings || []).length ? new Map(remoteTasks) : new Map();
      data.tasks.forEach(task => applyRemoteTask(task, next));
      remoteTasks = next;
      applyJobs(data);
      mergeTasks();
      remoteRefreshing = !!data.refreshing;
      taskStatus = remoteRefreshing ? "后台正在更新任务，当前列表仍可操作…"
        : data.updated_at ? `已更新 ${new Date(data.updated_at).toLocaleTimeString()}` : "尚未取得远端任务";
      if ((data.warnings || []).length) taskStatus += " · " + data.warnings.join(" · ");
    } catch (error) {
      if (flight.epoch === taskEpoch && !captureActive && error.name !== "AbortError") taskStatus = "❌ 任务刷新失败，已保留现有任务：" + error.message;
    } finally {
      if (taskFlight === flight) taskFlight = null;
      setTaskControlsDisabled(captureActive);
      renderTaskStatus();
      if (!captureActive) scheduleTasks(taskReloadRequested ? 0 : remoteRefreshing ? 1500 : 30000);
    }
  })();
  return flight.promise;
}

function refreshJobs() {
  if (captureActive) return Promise.resolve();
  if (jobsFlight) return jobsFlight.promise;
  clearTimeout(jobsTimer);
  jobsTimer = null;
  const flight = {epoch: taskEpoch, controller: new AbortController(), promise: null};
  jobsFlight = flight;
  flight.promise = (async () => {
    try {
      const data = await requestJson("/api/jobs", {signal: flight.controller.signal});
      if (flight.epoch !== taskEpoch || captureActive) return;
      if (!data.ok || !Array.isArray(data.jobs)) throw new Error(data.error || "读取运行状态失败");
      if (applyJobs(data)) mergeTasks();
      else render(currentTasks); // Time-based eligibility can change without a job event.
      jobsWarning = "";
    } catch (error) {
      if (flight.epoch === taskEpoch && !captureActive && error.name !== "AbortError") jobsWarning = "❌ 运行状态读取失败，正在重试：" + error.message;
    } finally {
      if (jobsFlight === flight) jobsFlight = null;
      renderTaskStatus();
      scheduleJobs(flight.epoch === taskEpoch ? 1500 : 0);
    }
  })();
  return flight.promise;
}

function taskPayload(task, loop) {
  return {source: task.source || "class", task_id: task.task_id, release_id: task.release_id,
    course_id: task.course_id, list_id: task.list_id, task_type: task.task_type, grade: task.grade,
    task_name: task.task_name, account_key: task.account_key, loop: !!loop};
}

function resolveTask(key) {
  return currentTasks.find(task => taskKey(task) === key);
}

async function startTask(key, loop) {
  const selected = resolveTask(key);
  if (captureActive || batchPending || pendingTasks.has(key) || !taskIsInCurrentSource(selected) || !taskCanStart(selected) || (loop && !taskCanLoop(selected)) || isActive(selected)) return;
  const task = {...selected};
  const viewEpoch = sourceViewEpoch;
  const source = task.source || "class";
  pendingTasks.set(key, "start");
  sourceActionWarnings[source] = "";
  mergeTasks();
  try {
    const data = await postJson("/api/start", taskPayload(task, loop));
    if (data.task) applyRemoteTask(data.task, remoteTasks, key);
    applyJobs(data, data.job);
    mergeTasks();
    if (!data.ok) throw new Error(data.error || "未知错误");
    if (viewEpoch === sourceViewEpoch && taskIsInCurrentSource(task) && !captureActive) showLogs(task, loop);
  } catch (error) {
    sourceActionWarnings[source] = "❌ 启动失败：" + error.message;
    if (viewEpoch === sourceViewEpoch && taskIsInCurrentSource(task)) alert(sourceActionWarnings[source]);
  } finally {
    pendingTasks.delete(key);
    mergeTasks();
    renderTaskStatus();
    if (!captureActive) refreshJobs();
  }
}

async function stopTask(key) {
  const selected = resolveTask(key);
  if (captureActive || pendingTasks.has(key) || !taskIsInCurrentSource(selected) || !isActive(selected) || selected.status === "stopping") return;
  const task = {...selected};
  const viewEpoch = sourceViewEpoch;
  const source = task.source || "class";
  if (!confirm(`停止${TASK_SOURCE_LABELS[source]}「${task.task_name}」？`) || !taskIsInCurrentSource(task)) return;
  pendingTasks.set(key, "stop");
  sourceActionWarnings[source] = "";
  mergeTasks();
  try {
    const data = await postJson("/api/stop", taskPayload(task, false));
    applyJobs(data, data.job);
    if (!data.ok) throw new Error(data.error || "未知错误");
  } catch (error) {
    sourceActionWarnings[source] = "❌ 停止失败：" + error.message;
    if (viewEpoch === sourceViewEpoch && taskIsInCurrentSource(task)) alert(sourceActionWarnings[source]);
  } finally {
    pendingTasks.delete(key);
    mergeTasks();
    renderTaskStatus();
    if (!captureActive) refreshJobs();
  }
}

async function recoverTask(key) {
  const selected = resolveTask(key);
  if (captureActive || batchPending || pendingTasks.has(key) || !taskIsInCurrentSource(selected) || isActive(selected)
    || selected.account_current === false || !selected.recovery_required || !selected.recovery_id) return;
  const task = {...selected};
  const viewEpoch = sourceViewEpoch;
  const source = task.source || "class";
  if (!confirm(`我已在词达人官方端核对${TASK_SOURCE_LABELS[source]}「${task.task_name}」的提交结果，确认重新同步？\n只读取最新状态，不会重复提交或自动启动。`) || !taskIsInCurrentSource(task)) return;
  pendingTasks.set(key, "recover");
  sourceActionWarnings[source] = "";
  mergeTasks();
  try {
    const data = await postJson("/api/tasks/recovery/ack", {...taskPayload(task, false), recovery_id: task.recovery_id, confirmed: true});
    if (data.task) applyRemoteTask(data.task, remoteTasks, key);
    applyJobs(data);
    if (!data.ok) throw new Error(data.error || "重新同步失败，暂停保留");
    sourceActionWarnings[source] = "已重新同步；如需继续，请手动启动任务。";
    taskReloadRequested = true;
    if (!taskFlight) scheduleTasks(0);
  } catch (error) {
    sourceActionWarnings[source] = "❌ 重新同步失败：" + error.message;
    if (viewEpoch === sourceViewEpoch && taskIsInCurrentSource(task)) alert(sourceActionWarnings[source]);
  } finally {
    pendingTasks.delete(key);
    mergeTasks();
    renderTaskStatus();
    if (!captureActive) refreshJobs();
  }
}

async function startAllClassLoop() {
  if (selectedTaskSource !== "class" || captureActive || batchPending || pendingTasks.size) return;
  const eligibleCount = currentTasks.filter(task => (task.source || "class") === "class" && Number(task.task_type) === 1 && taskCanLoop(task) && !isActive(task)).length;
  if (!eligibleCount) { alert("当前没有已确认可循环的班级学习任务。请先刷新任务并查看分类原因。"); return; }
  if (!confirm(`确定要循环启动 ${eligibleCount} 个允许程序循环的班级学习任务？仅按执行资格筛选，其他任务跳过。`) || selectedTaskSource !== "class") return;
  const viewEpoch = sourceViewEpoch;
  batchPending = true;
  sourceActionWarnings.class = "";
  setTaskControlsDisabled(captureActive);
  try {
    const data = await postJson("/api/start_all", {});
    applyJobs(data);
    mergeTasks();
    if (!data.ok) throw new Error(data.error || "未知错误");
    sourceActionWarnings.class = `已加入 ${data.accepted ?? data.started} 个任务，最多 ${data.max_concurrent_tasks || 4} 个同时执行，其余排队；跳过 ${data.skipped} 个`;
    if (viewEpoch === sourceViewEpoch && selectedTaskSource === "class") alert(sourceActionWarnings.class);
  } catch (error) {
    sourceActionWarnings.class = "❌ 批量启动失败：" + error.message;
    if (viewEpoch === sourceViewEpoch && selectedTaskSource === "class") alert(sourceActionWarnings.class);
  } finally {
    batchPending = false;
    setTaskControlsDisabled(captureActive);
    renderTaskStatus();
    if (!captureActive) refreshJobs();
  }
}

function scheduleLogs(delay = 1500) {
  clearTimeout(logTimer);
  logTimer = null;
  if (activeLogTask && !captureActive) logTimer = setTimeout(() => { logTimer = null; refreshLogs(); }, delay);
}

function showLogs(taskOrKey, loop) {
  const selected = typeof taskOrKey === "string" ? resolveTask(taskOrKey) : taskOrKey;
  if (!taskIsInCurrentSource(selected)) return;
  logEpoch += 1;
  logCursor = null;
  logLines = [];
  clearTimeout(logTimer);
  logTimer = null;
  if (logFlight) logFlight.controller.abort();
  activeLogTask = {...selected};
  activeLogKey = taskKey(selected);
  logReloadRequested = true;
  document.getElementById("modal-title").textContent = `📜 ${TASK_SOURCE_LABELS[selected.source || "class"]} · ${selected.task_name}` + (loop || selected.loop ? " 🔁" : "");
  document.getElementById("modal-logs").textContent = "正在读取日志…";
  const status = document.getElementById("modal-status");
  if (status) status.textContent = "";
  document.getElementById("modal").classList.add("show");
  if (!logFlight) refreshLogs();
}

function closeLogs() {
  logEpoch += 1;
  logCursor = null;
  logLines = [];
  activeLogKey = activeLogTask = null;
  logReloadRequested = false;
  clearTimeout(logTimer);
  logTimer = null;
  if (logFlight) logFlight.controller.abort();
  document.getElementById("modal").classList.remove("show");
}

function refreshLogs() {
  if (!activeLogTask || captureActive) return Promise.resolve();
  if (logFlight) return logFlight.promise;
  const task = {...activeLogTask};
  const flight = {epoch: logEpoch, controller: new AbortController(), promise: null};
  logFlight = flight;
  logReloadRequested = false;
  const params = new URLSearchParams();
  Object.entries(taskPayload(task, false)).forEach(([key, value]) => {
    if (value != null && key !== "loop" && key !== "task_name") params.set(key, String(value));
  });
  if (logCursor) params.set("cursor", logCursor);
  let pollAgain = true;
  flight.promise = (async () => {
    try {
      const data = await requestJson(`/api/logs?${params}`, {signal: flight.controller.signal});
      if (flight.epoch !== logEpoch || !activeLogTask || captureActive) return;
      if (!data.ok || !Array.isArray(data.logs)) throw new Error(data.error || "日志暂不可用");
      // Log cursors are independent of job metadata versions. A new job state
      // must not discard valid log lines received by this single-flight poll.
      const box = document.getElementById("modal-logs");
      const wasBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 30;
      const reset = data.log_reset !== false || !data.log_cursor;
      if (reset) {
        logLines = data.logs.slice(-500);
        box.textContent = logLines.join("\n");
      } else if (data.logs.length) {
        const prefix = logLines.length ? "\n" : "";
        logLines.push(...data.logs);
        box.appendChild(document.createTextNode(prefix + data.logs.join("\n")));
        if (logLines.length > 500) {
          logLines = logLines.slice(-500);
          box.textContent = logLines.join("\n");
        }
      }
      logCursor = data.log_cursor || null;
      const status = document.getElementById("modal-status");
      if (status) status.textContent = data.job && data.job.recovery_required ? "提交状态需核对，请先在官方端核对后重新同步"
        : data.job && data.job.queued ? "排队中，轮到本任务时自动确认资格"
        : data.job && data.job.starting ? "正在确认任务资格，可随时取消"
        : data.waiting ? "等待下一轮重启，日志会继续更新" : "";
      if (wasBottom) box.scrollTop = box.scrollHeight;
      if (data.job && applyJobs(data, data.job)) mergeTasks();
      const local = localJobs.get(activeLogKey);
      const active = data.active === undefined ? (data.running || data.waiting || data.loop || (local && isActive(local))) : data.active;
      pollAgain = !data.done || !!active;
      if (Number.isSafeInteger(Number(data.seq)) && Number(data.seq) < latestJobSeq) {
        pollAgain = !local || !local.done || isActive(local);
      }
    } catch (error) {
      if (flight.epoch === logEpoch && activeLogTask && !captureActive && error.name !== "AbortError") {
        const status = document.getElementById("modal-status");
        if (status) status.textContent = "❌ 日志读取失败，正在重试：" + error.message;
        else document.getElementById("modal-logs").textContent = "❌ 日志读取失败，正在重试：" + error.message;
      }
    } finally {
      if (logFlight === flight) logFlight = null;
      if (activeLogTask && !captureActive) {
        if (logReloadRequested || flight.epoch !== logEpoch) scheduleLogs(0);
        else if (pollAgain) scheduleLogs();
      }
    }
  })();
  return flight.promise;
}

function renderCaptureStatus(capture) {
  const previousActive = captureActive;
  const state = capture.state || "idle";
  captureActive = !!capture.active || captureMutationPending;
  if (captureActive && !previousActive) {
    invalidateTaskRequests();
    logEpoch += 1;
    clearTimeout(logTimer);
    logTimer = null;
    if (logFlight) logFlight.controller.abort();
  }
  const pill = document.getElementById("capture-pill");
  pill.className = "status-box" + (state === "succeeded" ? " ok" : ["failed", "cancelled", "timed_out"].includes(state) ? " warn" : "");
  pill.textContent = ({idle: "等待获取", starting: "正在启动", waiting: "等待微信请求", validating: "正在验证",
    cancelling: "正在取消", succeeded: "获取成功", failed: "获取失败", cancelled: "已取消", timed_out: "已超时"})[state] || state;
  document.getElementById("capture-message").textContent = capture.message || "";
  document.getElementById("capture-start").disabled = captureActive || captureMutationPending;
  document.getElementById("capture-cancel").disabled = !capture.can_cancel || captureMutationPending;
  setTaskControlsDisabled(captureActive);
  renderTaskStatus();
  if (previousActive && !captureActive) {
    taskReloadRequested = true;
    if (!taskFlight) scheduleTasks(0);
    if (!jobsFlight) scheduleJobs(0);
    if (activeLogTask && !logFlight) scheduleLogs(0);
  }
}

function scheduleCapture() {
  clearTimeout(captureTimer);
  captureTimer = setTimeout(() => { captureTimer = null; refreshCaptureStatus(); }, 1000);
}

function refreshCaptureStatus() {
  if (captureFlight) return captureFlight.promise;
  clearTimeout(captureTimer);
  captureTimer = null;
  const flight = {epoch: captureEpoch, controller: new AbortController(), promise: null};
  captureFlight = flight;
  flight.promise = (async () => {
    try {
      const data = await requestJson("/api/auth/capture/status", {signal: flight.controller.signal});
      if (flight.epoch !== captureEpoch) return;
      if (!data.ok) throw new Error(data.error || "读取获取状态失败");
      const previous = lastCaptureState;
      renderCaptureStatus(data.capture);
      lastCaptureState = data.capture.state;
      if (previous && previous !== "succeeded" && data.capture.state === "succeeded") {
        const changed = await loadConfig();
        if (!changed) reloadTasksAfterChange();
      }
    } catch (error) {
      if (flight.epoch === captureEpoch && error.name !== "AbortError") {
        const pill = document.getElementById("capture-pill");
        pill.className = "status-box warn";
        pill.textContent = "状态读取失败";
        document.getElementById("capture-message").textContent = error.message;
      }
    } finally {
      if (captureFlight === flight) captureFlight = null;
      scheduleCapture();
    }
  })();
  return flight.promise;
}

async function mutateCapture(url, action) {
  if (captureMutationPending) return;
  captureEpoch += 1;
  if (captureFlight) captureFlight.controller.abort();
  captureMutationPending = true;
  renderCaptureStatus({active: true, state: action === "启动" ? "starting" : "cancelling"});
  try {
    const data = await postJson(url, {});
    captureMutationPending = false;
    if (data.capture) {
      const previous = lastCaptureState;
      renderCaptureStatus(data.capture);
      lastCaptureState = data.capture.state;
      if (previous !== "succeeded" && data.capture.state === "succeeded") {
        const changed = await loadConfig();
        if (!changed) reloadTasksAfterChange();
      }
    }
    if (!data.ok) throw new Error(data.error || data.message || "未知错误");
  } catch (error) {
    captureMutationPending = false;
    alert(`获取${action}失败：${error.message}`);
  } finally {
    scheduleCapture();
  }
}

function startCapture() {
  if (captureActive) return Promise.resolve();
  return mutateCapture("/api/auth/capture/start", "启动");
}

function cancelCapture() {
  return mutateCapture("/api/auth/capture/cancel", "取消");
}

async function loadConfig() {
  const epoch = ++configEpoch;
  try {
    const data = await requestJson("/api/config");
    if (epoch !== configEpoch) return;
    if (!data.ok) throw new Error(data.error || "读取配置失败");
    CONFIG_KEYS.forEach(key => {
      const element = document.getElementById(key);
      if (element) element.value = data.config[key] || "";
    });
    updateProxyFields();
    document.getElementById("env-path").textContent = `📄 ${data.env_file}`;
    renderConfigStatus(data.missing_auth || []);
    return updateTaskConfig(data.config);
  } catch (error) {
    if (epoch !== configEpoch) return;
    const pill = document.getElementById("config-pill");
    pill.className = "status-box warn";
    pill.textContent = "配置读取失败";
    document.getElementById("env-path").textContent = "❌ " + error.message;
  }
}

function updateTaskConfig(config) {
  const keys = ["USERTOKEN", "ABC", "AUTH_V", "USER_AGENT", "COURSE_ID", "STUDY_GRADE", "VOCABGO_PROXY_MODE", "VOCABGO_PROXY_URL"];
  const next = Object.fromEntries(keys.map(key => [key, String(config[key] || "")]));
  const previous = lastTaskConfig;
  lastTaskConfig = next;
  if (!previous) return false;
  const accountChanged = keys.slice(0, 4).some(key => next[key] !== previous[key]);
  const studyChanged = next.COURSE_ID !== previous.COURSE_ID || next.STUDY_GRADE !== previous.STUDY_GRADE;
  const routeChanged = keys.slice(6).some(key => next[key] !== previous[key]);
  if (!accountChanged && !studyChanged && !routeChanged) return false;
  if (accountChanged) remoteTasks.clear();
  else if (studyChanged) remoteTasks.forEach((task, key) => { if (task.source === "study") remoteTasks.delete(key); });
  mergeTasks();
  reloadTasksAfterChange();
  return true;
}

function renderConfigStatus(missing) {
  const pill = document.getElementById("config-pill");
  pill.className = "status-box" + (missing && missing.length ? " warn" : " ok");
  pill.textContent = missing && missing.length ? `缺少字段：${missing.join(", ")}` : "鉴权配置完整，可直接启动任务";
}

function updateProxyFields() {
  ["VOCABGO", "LLM"].forEach(prefix => {
    document.getElementById(`${prefix}_PROXY_FIELD`).hidden = document.getElementById(`${prefix}_PROXY_MODE`).value !== "custom";
  });
}

async function saveConfig(refreshTasks) {
  if (configPending) return;
  configPending = true;
  configEpoch += 1;
  const payload = {};
  CONFIG_KEYS.forEach(key => { const element = document.getElementById(key); payload[key] = element ? element.value : ""; });
  try {
    const data = await postJson("/api/config", payload);
    if (!data.ok) throw new Error(data.error || "未知错误");
    renderConfigStatus(data.missing_auth || []);
    document.getElementById("env-path").textContent = `✅ 已保存到 ${data.env_file}`;
    // Only task configuration changes invalidate account/course requests; LLM-only saves do not fetch upstream.
    const changed = updateTaskConfig(data.config || payload);
    if (refreshTasks && !changed) reloadTasksAfterChange();
  } catch (error) {
    alert("保存失败：" + error.message);
  } finally {
    configPending = false;
  }
}

document.getElementById("tbody").addEventListener("click", event => {
  const button = event.target.closest("button[data-action]");
  if (!button || button.disabled) return;
  const key = button.dataset.key;
  if (button.dataset.action === "start" || button.dataset.action === "loop") startTask(key, button.dataset.action === "loop");
  else if (button.dataset.action === "stop") stopTask(key);
  else if (button.dataset.action === "recover") recoverTask(key);
  else if (button.dataset.action === "logs") showLogs(key);
});

const taskFilters = document.getElementById("task-filters");
if (taskFilters) taskFilters.addEventListener("click", event => {
  const button = event.target.closest("button[data-filter]");
  if (!button || button.disabled || !(button.dataset.filter in TASK_TAB_LABELS)) return;
  selectedTaskStatus = button.dataset.filter;
  render(currentTasks);
});

const taskSources = document.getElementById("task-sources");
if (taskSources) taskSources.addEventListener("click", event => {
  const button = event.target.closest("button[data-source]");
  if (button && !button.disabled) selectTaskSource(button.dataset.source);
});

const switchOtherSource = document.getElementById("switch-other-source");
if (switchOtherSource) switchOtherSource.addEventListener("click", () => {
  if (!switchOtherSource.disabled) selectTaskSource(switchOtherSource.dataset.source);
});

["VOCABGO_PROXY_MODE", "LLM_PROXY_MODE"].forEach(id => document.getElementById(id).addEventListener("change", updateProxyFields));

loadConfig().then(refreshCaptureStatus).then(() => {
  if (!captureActive) { loadTasks(); refreshJobs(); }
});
