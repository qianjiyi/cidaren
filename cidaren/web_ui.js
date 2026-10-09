"use strict";

const CONFIG_KEYS = ["USERTOKEN", "ABC", "AUTH_V", "USER_AGENT", "COURSE_ID", "STUDY_GRADE", "LLM_URL", "LLM_KEY", "LLM_MODEL"];
const JOB_STATE_FIELDS = ["running", "done", "loop", "waiting", "active", "stopped", "status", "has_logs", "round", "exit_code", "recovery_required", "recovery_id", "recovery_reason", "account_current"];
const JOB_TASK_METADATA_FIELDS = ["category", "category_label", "category_reason", "category_checked_at", "can_start", "can_loop_start", "repeatable", "eligibility_reason", "deadline", "deadline_ms", "task_kind_label", "score", "progress"];
const TASK_CATEGORY_LABELS = {all: "全部", available: "未完成可继续", full: "已满分", blocked: "不可执行", unknown: "待确认"};
let currentTasks = [];
let selectedTaskCategory = "all";
let selectedTaskSource = "all";
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
let jobsFlight = null;
let jobsTimer = null;
let configEpoch = 0;
let configPending = false;
let lastTaskConfig = null;
let batchPending = false;
const pendingTasks = new Map();
let taskStatus = "加载中…";
let jobsWarning = "";
let actionWarning = "";
let activeLogKey = null;
let activeLogTask = null;
let logEpoch = 0;
let logFlight = null;
let logTimer = null;
let logReloadRequested = false;

function taskKey(task) {
  const source = task.source || "class";
  return JSON.stringify([
    source,
    String(source === "study" ? (task.course_id || "CET4_v2") : task.task_id),
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
  const timeout = setTimeout(abort, ["/api/tasks", "/api/start_all", "/api/start", "/api/tasks/recovery/ack"].includes(url) ? 90000 : 10000);
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
    : [taskStatus, jobsWarning, actionWarning].filter(Boolean).join(" · ");
}

function setTaskControlsDisabled(disabled) {
  document.querySelectorAll("#task-card button").forEach(button => {
    const role = button.dataset.role || (button.dataset.filter ? "filter" : button.id === "start-all" ? "batch" : "refresh");
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
  // A task refresh started before a rejected start must not restore its old eligibility.
  if (previous && Number.isFinite(checkedAt) && Number.isFinite(previousCheckedAt) && checkedAt < previousCheckedAt) {
    target.set(key, {...previous});
    return false;
  }
  target.set(key, {...previous, ...task});
  return true;
}

function mergeTasks() {
  const tasks = new Map(remoteTasks);
  localJobs.forEach((job, key) => {
    const remote = tasks.get(key);
    const sameAccount = !remote || !job.account_key || remote.account_key === job.account_key;
    const task = remote ? {...remote} : {...job, source_label: job.source === "study" ? "自学" : "班级",
      category: "unknown", category_label: "待确认", category_reason: "未取得当前任务信息，请刷新后确认是否可继续。",
      can_start: false, can_loop_start: false};
    const checkedAt = job.category_checked_at;
    const remoteCheckedAt = remote ? Number(remote.category_checked_at) : NaN;
    if (remote && sameAccount && Number.isSafeInteger(checkedAt) && checkedAt > 0
      && ["available", "full", "blocked", "unknown"].includes(job.category)
      && (!Number.isSafeInteger(remoteCheckedAt) || checkedAt > remoteCheckedAt)) {
      const updatedRemote = {...remote};
      JOB_TASK_METADATA_FIELDS.forEach(field => {
        if (field in job) { task[field] = job[field]; updatedRemote[field] = job[field]; }
      });
      remoteTasks.set(key, updatedRemote);
    }
    JOB_STATE_FIELDS.forEach(field => {
      if (field in job && (sameAccount || !field.startsWith("recovery_"))) task[field] = job[field];
    });
    ["source", "task_id", "release_id", "course_id", "list_id"].forEach(field => {
      // The study API may return a new task ID for the next attempt; course/list is the stable identity.
      if (remote && task.source === "study" && field === "task_id") return;
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
  return ["available", "full", "blocked", "unknown"].includes(task.category) ? task.category : "unknown";
}

function taskCategoryReason(task) {
  return task.recovery_reason || task.category_reason || task.eligibility_reason || task.note
    || (taskCategory(task) === "unknown" ? "任务状态尚未确认，仅支持手动单次尝试。" : "");
}

function taskCanStart(task) {
  return task.account_current !== false && !task.recovery_required && !["full", "blocked"].includes(taskCategory(task)) && !!task.can_start;
}

function taskCanLoop(task) {
  return taskCategory(task) === "available" && taskCanStart(task) && task.can_loop_start === true;
}

function tasksForSource(tasks) {
  return tasks.filter(task => selectedTaskSource === "all" || (task.source || "class") === selectedTaskSource);
}

function renderTaskFilters(tasks) {
  const sourceTasks = tasksForSource(tasks);
  const counts = {all: sourceTasks.length, available: 0, full: 0, blocked: 0, unknown: 0};
  sourceTasks.forEach(task => { counts[taskCategory(task)] += 1; });
  document.querySelectorAll("#task-filters button[data-filter]").forEach(button => {
    const category = button.dataset.filter;
    button.textContent = `${TASK_CATEGORY_LABELS[category] || category}（${counts[category] || 0}）`;
    button.classList.toggle("selected", category === selectedTaskCategory);
    button.setAttribute("aria-pressed", String(category === selectedTaskCategory));
  });
  const visible = sourceTasks.filter(task => selectedTaskCategory === "all" || taskCategory(task) === selectedTaskCategory);
  const activeCount = tasks.filter(isActive).length;
  const visibleKeys = new Set(visible.map(taskKey));
  const hiddenActive = tasks.filter(task => isActive(task) && !visibleKeys.has(taskKey(task))).length;
  const summary = document.getElementById("filter-summary");
  if (summary) summary.textContent = `显示 ${visible.length} / ${tasks.length} 个任务`
    + (activeCount ? ` · 运行中 ${activeCount} 个` : "")
    + (hiddenActive ? `（${hiddenActive} 个在当前筛选外，选择“全部”及“全部来源”查看停止和日志）` : "");
  const batchButton = document.getElementById("start-all");
  if (batchButton) {
    const eligibleCount = tasks.filter(task => (task.source || "class") === "class" && taskCanLoop(task) && !isActive(task)).length;
    batchButton.dataset.permanentDisabled = String(!eligibleCount);
    batchButton.title = eligibleCount ? `启动 ${eligibleCount} 个未完成可继续的班级学习任务；测试、已满分、不可执行和待确认任务将跳过。` : "当前没有已确认可循环的班级学习任务。";
  }
  return visible;
}

function render(tasks) {
  const body = document.getElementById("tbody");
  const visible = renderTaskFilters(tasks);
  if (!visible.length) {
    body.innerHTML = `<tr><td colspan="6" style="color:#667085;text-align:center;padding:24px;">${tasks.length ? "当前分类没有任务，可切换分类或来源查看。" : "暂无任务，或当前账号下还没有可见任务。"}</td></tr>`;
    setTaskControlsDisabled(captureActive);
    return;
  }
  body.innerHTML = visible.map((task, index) => {
    const key = taskKey(task);
    const attributeKey = escapeHtml(key);
    const rawProgress = Number(task.progress || 0);
    const progress = Number.isFinite(rawProgress) ? Math.min(100, Math.max(0, rawProgress)) : 0;
    const score = task.score == null ? "-" : escapeHtml(task.score);
    const category = taskCategory(task);
    const reason = taskCategoryReason(task);
    let badge = `<span class="badge">${escapeHtml(task.source_label || (task.source === "study" ? "自学" : "班级"))}</span>`;
    badge += `<span class="badge category ${category}" title="${escapeHtml(reason)}">${escapeHtml(task.category_label || TASK_CATEGORY_LABELS[category])}</span>`;
    const pending = pendingTasks.get(key);
    if (pending) badge += `<span class="badge run">${pending === "stop" ? "正在停止…" : pending === "recover" ? "正在重新同步…" : "正在启动…"}</span>`;
    else if (task.status === "stopping") badge += '<span class="badge run">正在停止…</span>';
    else if (task.running) badge += `<span class="badge run">${task.loop ? `🔁 循环中（第 ${escapeHtml(task.round || 1)} 轮）` : "运行中"}</span>`;
    else if (task.waiting || (task.active && task.loop)) badge += '<span class="badge run">等待下一轮重启</span>';
    else if (task.recovery_required) badge += '<span class="badge" style="color:#b42318;">需核对提交状态</span>';
    else if (task.stopped || task.status === "stopped") badge += '<span class="badge">已停止</span>';
    else if (task.status === "failed" || (task.exit_code != null && Number(task.exit_code) !== 0)) badge += `<span class="badge" style="color:#b42318;">失败（退出码 ${escapeHtml(task.exit_code == null ? "未知" : task.exit_code)}）</span>`;
    else if (task.done) badge += `<span class="badge done">${task.exit_code === 0 ? "已完成" : "已结束"}</span>`;
    const button = (role, label, className = "", permanent = false, title = "") =>
      `<button data-action="${role}" data-role="${role}" data-key="${attributeKey}" data-permanent-disabled="${permanent}" class="${className}" title="${escapeHtml(title)}"${permanent ? " disabled" : ""}>${label}</button>`;
    let actions;
    if (isActive(task)) actions = button("stop", task.status === "stopping" ? "正在停止…" : "停止", "danger", task.status === "stopping");
    else if (task.recovery_required && task.account_current !== false) actions = button("recover", "核对后重新同步", "", !task.recovery_id,
      task.recovery_id ? "先在词达人官方端确认本次操作的结果，再点击重新同步；不会自动启动" : "运行记录损坏或无法读取，请先核对并处理记录");
    else if (taskCanStart(task)) actions = button("start", category === "unknown" ? "▶ 单次尝试" : "▶ 启动", "primary", false, reason)
      + button("loop", "🔁 循环", "loop", !taskCanLoop(task), taskCanLoop(task) ? "刷到 100 分为止" : (task.eligibility_reason || "当前任务不允许自动循环。"));
    else actions = button("display", category === "full" ? "已满分" : category === "blocked" ? "不可执行" : "待确认", "", true, reason || "暂不支持启动");
    if (task.has_logs || task.running || task.waiting || task.done || task.stopped || Number(task.round) > 0) actions += button("logs", "日志");
    return `<tr><td>${index + 1}</td><td>${escapeHtml(task.task_name)}${reason ? `<div class="task-reason">${escapeHtml(reason)}</div>` : ""}</td><td><div class="progress-bar"><div style="width:${progress}%"></div></div>${progress}%</td><td class="${Number(task.score) >= 100 ? "score full" : "score"}">${score}</td><td>${badge}</td><td>${actions}</td></tr>`;
  }).join("");
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

function loadTasks() {
  if (captureActive) { renderTaskStatus(); return Promise.resolve(); }
  if (taskFlight) return taskFlight.promise;
  clearTimeout(taskTimer);
  taskTimer = null;
  taskReloadRequested = false;
  taskStatus = currentTasks.length ? "正在刷新任务（保留当前运行状态）…" : "正在拉取任务…";
  renderTaskStatus();
  const flight = {epoch: taskEpoch, controller: new AbortController(), promise: null};
  taskFlight = flight;
  setTaskControlsDisabled(captureActive);
  flight.promise = (async () => {
    try {
      const data = await requestJson("/api/tasks", {signal: flight.controller.signal});
      if (flight.epoch !== taskEpoch || captureActive) return;
      if (!data.ok || !Array.isArray(data.tasks)) throw new Error(data.error || "读取任务失败");
      // Partial upstream failures keep the existing rows and all local jobs visible.
      const next = (data.warnings || []).length ? new Map(remoteTasks) : new Map();
      data.tasks.forEach(task => applyRemoteTask(task, next));
      remoteTasks = next;
      applyJobs(data);
      mergeTasks();
      taskStatus = `共 ${currentTasks.length} 个任务 · 已更新 ${new Date().toLocaleTimeString()}`;
      if ((data.warnings || []).length) taskStatus += " · " + data.warnings.join(" · ");
    } catch (error) {
      if (flight.epoch === taskEpoch && !captureActive && error.name !== "AbortError") taskStatus = "❌ 任务刷新失败，已保留现有任务：" + error.message;
    } finally {
      if (taskFlight === flight) taskFlight = null;
      setTaskControlsDisabled(captureActive);
      renderTaskStatus();
      if (!captureActive) scheduleTasks(taskReloadRequested ? 0 : 30000);
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
  if (captureActive || batchPending || pendingTasks.has(key) || !selected || !taskCanStart(selected) || (loop && !taskCanLoop(selected)) || isActive(selected)) return;
  const task = {...selected};
  pendingTasks.set(key, "start");
  actionWarning = "";
  mergeTasks();
  try {
    const data = await postJson("/api/start", taskPayload(task, loop));
    if (data.task) applyRemoteTask(data.task, remoteTasks, key);
    applyJobs(data, data.job);
    mergeTasks();
    if (!data.ok) throw new Error(data.error || "未知错误");
    showLogs(task, loop);
  } catch (error) {
    actionWarning = "❌ 启动失败：" + error.message;
    alert(actionWarning);
  } finally {
    pendingTasks.delete(key);
    mergeTasks();
    renderTaskStatus();
    if (!captureActive) refreshJobs();
  }
}

async function stopTask(key) {
  const selected = resolveTask(key);
  if (captureActive || pendingTasks.has(key) || !selected || !isActive(selected) || selected.status === "stopping") return;
  const task = {...selected};
  if (!confirm(`停止任务「${task.task_name}」？`)) return;
  pendingTasks.set(key, "stop");
  actionWarning = "";
  mergeTasks();
  try {
    const data = await postJson("/api/stop", taskPayload(task, false));
    applyJobs(data, data.job);
    if (!data.ok) throw new Error(data.error || "未知错误");
  } catch (error) {
    actionWarning = "❌ 停止失败：" + error.message;
    alert(actionWarning);
  } finally {
    pendingTasks.delete(key);
    mergeTasks();
    renderTaskStatus();
    if (!captureActive) refreshJobs();
  }
}

async function recoverTask(key) {
  const selected = resolveTask(key);
  if (captureActive || batchPending || pendingTasks.has(key) || !selected || isActive(selected)
    || selected.account_current === false || !selected.recovery_required || !selected.recovery_id) return;
  const task = {...selected};
  if (!confirm(`我已在词达人官方端核对「${task.task_name}」的提交结果，确认重新同步？\n只读取最新状态，不会重复提交或自动启动。`)) return;
  pendingTasks.set(key, "recover");
  actionWarning = "";
  mergeTasks();
  try {
    const data = await postJson("/api/tasks/recovery/ack", {...taskPayload(task, false), recovery_id: task.recovery_id, confirmed: true});
    if (data.task) applyRemoteTask(data.task, remoteTasks, key);
    applyJobs(data);
    if (!data.ok) throw new Error(data.error || "重新同步失败，暂停保留");
    actionWarning = "已重新同步；如需继续，请手动启动任务。";
    taskReloadRequested = true;
    if (!taskFlight) scheduleTasks(0);
  } catch (error) {
    actionWarning = "❌ 重新同步失败：" + error.message;
    alert(actionWarning);
  } finally {
    pendingTasks.delete(key);
    mergeTasks();
    renderTaskStatus();
    if (!captureActive) refreshJobs();
  }
}

async function startAllClassLoop() {
  if (captureActive || batchPending || pendingTasks.size) return;
  const eligibleCount = currentTasks.filter(task => (task.source || "class") === "class" && taskCanLoop(task) && !isActive(task)).length;
  if (!eligibleCount) { alert("当前没有已确认可循环的班级学习任务。请先刷新任务并查看分类原因。"); return; }
  if (!confirm(`确定要循环启动 ${eligibleCount} 个未完成可继续的班级学习任务？测试、已满分、不可执行和待确认任务将跳过。`)) return;
  batchPending = true;
  actionWarning = "";
  setTaskControlsDisabled(captureActive);
  try {
    const data = await postJson("/api/start_all", {});
    applyJobs(data);
    mergeTasks();
    if (!data.ok) throw new Error(data.error || "未知错误");
    alert(`已启动 ${data.started} 个任务循环，跳过 ${data.skipped} 个（运行中、已满分或不允许循环）`);
  } catch (error) {
    actionWarning = "❌ 批量启动失败：" + error.message;
    alert(actionWarning);
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
  if (!selected) return;
  logEpoch += 1;
  clearTimeout(logTimer);
  logTimer = null;
  if (logFlight) logFlight.controller.abort();
  activeLogTask = {...selected};
  activeLogKey = taskKey(selected);
  logReloadRequested = true;
  document.getElementById("modal-title").textContent = "📜 " + selected.task_name + (loop || selected.loop ? " 🔁" : "");
  document.getElementById("modal-logs").textContent = "正在读取日志…";
  const status = document.getElementById("modal-status");
  if (status) status.textContent = "";
  document.getElementById("modal").classList.add("show");
  if (!logFlight) refreshLogs();
}

function closeLogs() {
  logEpoch += 1;
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
  let pollAgain = true;
  flight.promise = (async () => {
    try {
      const data = await requestJson(`/api/logs?${params}`, {signal: flight.controller.signal});
      if (flight.epoch !== logEpoch || !activeLogTask || captureActive) return;
      if (!data.ok || !Array.isArray(data.logs)) throw new Error(data.error || "日志暂不可用");
      if (Number.isSafeInteger(Number(data.seq)) && Number(data.seq) < latestJobSeq) {
        const latest = localJobs.get(activeLogKey);
        pollAgain = !latest || !latest.done || isActive(latest);
        return;
      }
      const box = document.getElementById("modal-logs");
      const wasBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 30;
      box.textContent = data.logs.join("\n");
      const status = document.getElementById("modal-status");
      if (status) status.textContent = data.job && data.job.recovery_required ? "提交状态需核对，请先在官方端核对后重新同步"
        : data.waiting ? "等待下一轮重启，日志会继续更新" : "";
      if (wasBottom) box.scrollTop = box.scrollHeight;
      if (data.job && applyJobs(data, data.job)) mergeTasks();
      const local = localJobs.get(activeLogKey);
      const active = data.active === undefined ? (data.running || data.waiting || data.loop || (local && isActive(local))) : data.active;
      pollAgain = !data.done || !!active;
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
  const keys = ["USERTOKEN", "ABC", "AUTH_V", "USER_AGENT", "COURSE_ID", "STUDY_GRADE"];
  const next = Object.fromEntries(keys.map(key => [key, String(config[key] || "")]));
  const previous = lastTaskConfig;
  lastTaskConfig = next;
  if (!previous) return false;
  const accountChanged = keys.slice(0, 4).some(key => next[key] !== previous[key]);
  const studyChanged = next.COURSE_ID !== previous.COURSE_ID || next.STUDY_GRADE !== previous.STUDY_GRADE;
  if (!accountChanged && !studyChanged) return false;
  if (accountChanged) remoteTasks.clear();
  else remoteTasks.forEach((task, key) => { if (task.source === "study") remoteTasks.delete(key); });
  mergeTasks();
  reloadTasksAfterChange();
  return true;
}

function renderConfigStatus(missing) {
  const pill = document.getElementById("config-pill");
  pill.className = "status-box" + (missing && missing.length ? " warn" : " ok");
  pill.textContent = missing && missing.length ? `缺少字段：${missing.join(", ")}` : "鉴权配置完整，可直接启动任务";
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
  if (!button || button.disabled || !(button.dataset.filter in TASK_CATEGORY_LABELS)) return;
  selectedTaskCategory = button.dataset.filter;
  render(currentTasks);
});

const sourceFilter = document.getElementById("source-filter");
if (sourceFilter) sourceFilter.addEventListener("change", () => {
  selectedTaskSource = ["all", "class", "study"].includes(sourceFilter.value) ? sourceFilter.value : "all";
  render(currentTasks);
});

loadConfig().then(refreshCaptureStatus).then(() => {
  if (!captureActive) { loadTasks(); refreshJobs(); }
});
