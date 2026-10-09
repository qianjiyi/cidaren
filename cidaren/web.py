"""
词达人 Web 控制台
- 列出所有任务（task_name / progress / score）
- 点击启动按钮在子进程中刷题
- 定时刷新分数和子进程日志
- 支持在前端编辑 token / LLM 配置并同步写入 .env
"""
import os, sys, subprocess, threading, time, signal, socket, webbrowser
import hashlib, json
from collections import deque
from pathlib import Path
from urllib.parse import urlparse
from flask import Flask, jsonify, request, Response

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import a as quiz  # noqa
    from config import (
        build_subprocess_env,
        env_file_path,
        get_missing_auth_fields,
        get_runtime_config,
        save_runtime_config,
    )
    from token_capture import CaptureManager, is_loopback_request
    from bank_store import BankError, prepare_default_store
    from task_categories import CATEGORY_FIELDS, classify_task
    from task_safety import SafetyStore, SafetyError
else:
    from . import a as quiz  # noqa
    from .config import (
        build_subprocess_env,
        env_file_path,
        get_missing_auth_fields,
        get_runtime_config,
        save_runtime_config,
    )
    from .token_capture import CaptureManager, is_loopback_request
    from .bank_store import BankError, prepare_default_store
    from .task_categories import CATEGORY_FIELDS, classify_task
    from .task_safety import SafetyStore, SafetyError

app = Flask(__name__)

# ==== 子进程任务状态 ====
JOBS = {}  # 稳定任务键 -> 进程及运行状态
JOBS_LOCK = threading.RLock()
JOBS_SEQ = 0
TASKS_FETCH_LOCK = threading.Lock()
TASK_CACHE_LOCK = threading.RLock()
TASK_CACHE = {}
ACCOUNT_CACHE = {}
SAFETY = SafetyStore(Path(__file__).resolve().parent.parent)
RECOVERY_FIELDS = ("recovery_required", "recovery_id", "recovery_reason")
LOG_MAX = 500
TASK_METADATA_FIELDS = (
    "source", "source_label", "task_id", "release_id", "course_id", "list_id", "task_type", "grade",
    "task_name", "progress", "score", "free", "over_status", "over_time", "start_time", "release_time", "stale", "account_key",
)
JOB_STATE_FIELDS = (
    "job_key", "running", "done", "loop", "waiting", "active", "stopped", "status", "has_logs", "exit_code", "round",
    *RECOVERY_FIELDS,
    "account_current",
)


def task_metadata(task):
    return {field: task[field] for field in TASK_METADATA_FIELDS if field in task}


def _auth_scope(config):
    return hashlib.sha256(json.dumps([config.get(k, "") for k in ("USERTOKEN", "ABC", "AUTH_V")]).encode()).hexdigest()


def _resolve_account(config, client=None):
    """从服务器确认身份；Token 只作为本机列表缓存键。"""
    key = (client or _client(config)).get_account_key()
    with TASK_CACHE_LOCK:
        ACCOUNT_CACHE[_auth_scope(config)] = key
    with JOBS_LOCK:
        for job in JOBS.values():
            if job.get("loop") and job.get("account_key") != key:
                job["loop"] = False
                job["cancel_event"].set()
                job["logs"].append("[loop] 当前账号已改变，停止旧账号的循环")
                _touch_jobs()
    return key


def _safety_identity(task, account=None):
    return dict(account_key=account or task.get("account_key"), source=task.get("source") or "class",
                release_id=task.get("release_id"), course_id=task.get("course_id"),
                list_id=task.get("list_id") or task.get("release_id"))


def _with_recovery(task, account=None, active=False):
    row = dict(task)
    fields = dict(recovery_required=False, recovery_id=None, recovery_reason=None)
    if (account or row.get("account_key")) and not active:
        try:
            state = SAFETY.inspect(**_safety_identity(row, account))
            fields.update(recovery_required=bool(state.get("paused")), recovery_id=state.get("recovery_id"),
                          recovery_reason=state.get("reason"))
        except SafetyError as exc:
            fields.update(recovery_required=True, recovery_reason=str(exc))
    row.update(fields)
    if row["recovery_required"]:
        row.update(can_start=False, can_loop_start=False, repeatable=False)
    return row


def _remote_task(config, source, release_id, course_id=None, list_id=None, client=None):
    c = client or _client(config)
    account = _resolve_account(config, c)
    records = _list_study_tasks(c, course_id) if source == "study" else _list_class_tasks(c)
    matches = [r for r in records if str(r.get("list_id") if source == "study" else r.get("release_id"))
               == str(list_id or release_id)]
    if len(matches) != 1:
        raise ValueError("服务器未能唯一确认该账号的任务归属，请在官方端核对")
    if source == "study" and matches[0].get("course_id") not in (None, course_id):
        raise ValueError("服务器自学词表所属课程与所选任务不符")
    row = {**task_metadata(matches[0]), "source": source, "source_label": "自学" if source == "study" else "班级",
           "account_key": account, "release_id": release_id, "stale": False}
    if source == "study":
        row.update(course_id=course_id, list_id=list_id or release_id, release_id=list_id or release_id)
    _refresh_cached_record(config, source, row, course_id)
    with JOBS_LOCK:
        for job in JOBS.values():
            if (job.get("account_key") == account and job.get("source") == source
                    and _safety_identity(job) == _safety_identity(row)):
                job["task_meta"] = task_metadata(row)
                job["task_confirmed"] = True
                _touch_jobs()
    return classify_task(row)


def _stop_changed_auth_loops(previous, current):
    if _auth_scope(previous) == _auth_scope(current):
        return
    with JOBS_LOCK:
        for job in JOBS.values():
            if job.get("loop"):
                job["loop"] = False
                job["cancel_event"].set()
                job["logs"].append("[loop] 鉴权已更换，停止后续循环；刷新账号和任务后可手动启动")
                _touch_jobs()


def _cached_task(config, source, task_id, release_id, course_id=None, list_id=None):
    key = _job_key(source, task_id, release_id, course_id, list_id)
    scope = (_auth_scope(config), source, str(course_id or "CET4_v2") if source == "study" else "")
    with TASK_CACHE_LOCK:
        for row in TASK_CACHE.get(scope, []):
            if _job_key(row.get("source"), row.get("task_id"), row.get("release_id"), row.get("course_id"), row.get("list_id")) == key:
                return _with_recovery(classify_task(row))
    return None


def _refresh_cached_record(config, source, record, course_id=None):
    """A loop's existing score request also refreshes eligibility; no extra network request."""
    scope = (_auth_scope(config), source, str(course_id or "CET4_v2") if source == "study" else "")
    release_id = record.get("list_id") if source == "study" else record.get("release_id")
    key = _job_key(source, record.get("task_id"), release_id, course_id, record.get("list_id"))
    with TASK_CACHE_LOCK:
        rows = TASK_CACHE.get(scope, [])
        for index, row in enumerate(rows):
            if _job_key(source, row.get("task_id"), row.get("release_id"), row.get("course_id"), row.get("list_id")) == key:
                rows[index] = classify_task({**task_metadata(row), **task_metadata(record), "source": source, "stale": False})
                break


def _job_key(source, task_id, release_id, course_id=None, list_id=None, account_key=None):
    source = source or "class"
    if source == "study":
        key = (source, str(course_id or "CET4_v2"), str(list_id or release_id))
    else:
        key = (source, str(task_id), str(release_id))
    return (*key, account_key) if account_key else key


def _int_or_default(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _score_full(score):
    try:
        return float(score) >= 100
    except (TypeError, ValueError):
        return False


def _find_job(source, task_id, release_id, course_id=None, list_id=None, account_key=None):
    with JOBS_LOCK:
        key = _job_key(source, task_id, release_id, course_id, list_id, account_key)
        job = JOBS.get(key)
        if job or account_key:
            return job
        matches = [job for identity, job in JOBS.items() if identity[:3] == key]
        return matches[0] if len(matches) == 1 else None


def _touch_jobs():
    """只在 JOBS_LOCK 内调用；浏览器用代次拒绝旧状态。"""
    global JOBS_SEQ
    JOBS_SEQ += 1


def _job_active(job):
    return bool(job and (not job.get("done") or (job.get("loop") and not job.get("stopped"))))


def _job_snapshot(job):
    """只返回公开状态；调用方持有 JOBS_LOCK。"""
    running = not job["done"]
    waiting = bool(job["done"] and job.get("loop") and not job.get("stopped"))
    if running:
        status = "stopping" if job.get("stopped") else "running"
    elif waiting:
        status = "waiting"
    elif job.get("stopped"):
        status = "stopped"
    elif job.get("exit_code") not in (None, 0):
        status = "failed"
    else:
        status = "completed"
    key = _job_key(job["source"], job["task_id"], job["release_id"], job.get("course_id"), job.get("list_id"), job.get("account_key"))
    state = {
        "job_key": json.dumps(key, ensure_ascii=False, separators=(",", ":")),
        "source": job["source"], "source_label": "自学" if job["source"] == "study" else "班级",
        "task_id": job["task_id"], "release_id": job["release_id"],
        "course_id": job.get("course_id"), "list_id": job.get("list_id"),
        "task_type": job.get("task_type"), "grade": job.get("grade"),
        "account_key": job.get("account_key"),
        "account_current": True,
        "task_name": job.get("task_name") or f"任务 {job['task_id']}",
        "running": running, "done": job["done"],
        "loop": bool(job.get("loop")), "waiting": waiting, "active": _job_active(job),
        "stopped": bool(job.get("stopped")), "status": status, "has_logs": True,
        "exit_code": job.get("exit_code"), "round": job.get("round", 1),
    }
    metadata = job.get("task_meta")
    eligibility = classify_task(metadata or state)
    state.update({field: eligibility[field] for field in CATEGORY_FIELDS})
    with TASK_CACHE_LOCK:
        current_account = ACCOUNT_CACHE.get(_auth_scope(get_runtime_config()))
    if current_account and current_account != job.get("account_key"):
        state.update(account_current=False, can_start=False, can_loop_start=False,
                     category="unknown", category_label="待确认", category_reason="旧账号任务，仅保留停止和日志；切回该账号后刷新",
                     eligibility_reason="当前账号与此任务账号不符", source_label="旧账号 · " + state["source_label"])
    if job.get("account_snapshot") != state["account_current"]:
        job["account_snapshot"] = state["account_current"]
        _touch_jobs()
    if job.get("task_confirmed") is False:
        reason = "远端列表中未确认该任务，保留本机控制和日志；请刷新任务列表"
        state.update(category="unknown", category_label="待确认", category_reason=reason,
                     eligibility_reason=reason, can_start=False, can_loop_start=False, repeatable=False)
    if metadata:
        for field in ("score", "progress"):
            if field in metadata:
                state[field] = metadata[field]
    state = _with_recovery(state, job.get("account_key"), active=running)
    recovery = tuple(state[field] for field in RECOVERY_FIELDS)
    if job.get("recovery_snapshot") != recovery:
        job["recovery_snapshot"] = recovery
        _touch_jobs()
    if state["recovery_required"]:
        state["status"] = "recovery_required"
    return state


def _merge_jobs(tasks):
    """远端列表缺失时保留本机任务；状态始终取锁内最新快照。"""
    rows = {}
    for task in tasks:
        key = _job_key(task.get("source"), task.get("task_id"), task.get("release_id"), task.get("course_id"), task.get("list_id"), task.get("account_key"))
        rows[key] = {**_with_recovery(classify_task(task)), "job_key": json.dumps(key, ensure_ascii=False, separators=(",", ":")),
                     "running": False, "done": False, "loop": False, "waiting": False,
                     "active": False, "stopped": False, "status": "idle", "has_logs": False,
                     "exit_code": None, "round": 0, "account_current": True}
        if rows[key]["recovery_required"]:
            rows[key].update(status="recovery_required", has_logs=True)
    with JOBS_LOCK:
        for key, job in JOBS.items():
            state = _job_snapshot(job)
            if key in rows:
                same_account = rows[key].get("account_key") == job.get("account_key")
                rows[key].update({field: state[field] for field in JOB_STATE_FIELDS if same_account or field not in RECOVERY_FIELDS})
                if not same_account:
                    continue
                metadata = task_metadata(rows[key])
                if job.get("task_meta") != metadata or job.get("task_confirmed") is False:
                    job["task_meta"] = metadata
                    job["task_confirmed"] = True
                    _touch_jobs()
            else:
                if job.get("task_confirmed") is not False:
                    job["task_confirmed"] = False
                    _touch_jobs()
                rows[key] = _job_snapshot(job)
        return list(rows.values())


def _client(config=None):
    cfg = config or get_runtime_config()
    missing = get_missing_auth_fields(cfg)
    if missing:
        raise ValueError(f"请先在配置面板填写: {', '.join(missing)}")
    return quiz.Client(
        cfg["USERTOKEN"],
        cfg["ABC"],
        cfg["AUTH_V"],
        ua=cfg.get("USER_AGENT", ""),
    )


def _has_active_jobs():
    """包括正在执行以及循环模式等待重启的任务。"""
    with JOBS_LOCK:
        return any(_job_active(job) for job in JOBS.values())


def _validate_captured_credentials(credentials):
    try:
        c = quiz.Client(
            credentials["USERTOKEN"],
            credentials["ABC"],
            credentials["AUTH_V"],
            ua=credentials.get("USER_AGENT", ""),
        )
        response = c.main_info()
        if response.get("code") != 1:
            return False, response.get("msg") or f"服务端返回 code={response.get('code')}"
        user_info = (response.get("data") or {}).get("user_info") or {}
        identity = user_info.get("student_name") or user_info.get("student_code") or "账号有效"
        return True, str(identity)
    except Exception as exc:
        return False, str(exc)


CAPTURE = CaptureManager(
    Path(__file__).resolve().parent.parent,
    save_runtime_config,
    _validate_captured_credentials,
    _has_active_jobs,
)


def _capture_request_allowed():
    origin = request.headers.get("Origin")
    origin_host = urlparse(origin).hostname if origin else None
    return is_loopback_request(request.remote_addr, origin_host)


def _list_class_tasks(c):
    """拉取班级任务, 多页累加。"""
    out, page = [], 1
    while True:
        resp = c.page_task(page=page, size=50)
        recs = quiz._task_data(resp, "读取班级任务列表", allow_empty=True).get("records") or []
        if not isinstance(recs, list) or any(not isinstance(row, dict) for row in recs):
            raise ValueError("班级任务列表格式错误")
        if not recs:
            break
        out.extend(recs)
        if len(recs) < 50:
            break
        page += 1
        if page > 20:
            break
    return out


def _list_study_tasks(c, course_id):
    """拉取自学任务。"""
    resp = c.study_task_list(course_id=course_id)
    data = quiz._task_data(resp, "读取自学任务列表", allow_empty=True)
    tasks = data.get("task_list") or []
    if not isinstance(tasks, list) or any(not isinstance(row, dict) for row in tasks):
        raise ValueError("自学任务列表格式错误")
    return tasks


def _list_tasks():
    """串行拉取远端列表；单个来源失败时保留上次成功结果。"""
    cfg = get_runtime_config()
    c = _client(cfg)
    account = _resolve_account(cfg, c)
    course_id = (cfg.get("COURSE_ID") or "CET4_v2").strip() or "CET4_v2"
    study_grade = _int_or_default(cfg.get("STUDY_GRADE"), 2)
    auth_scope = _auth_scope(cfg)
    out, warnings = [], []
    with TASKS_FETCH_LOCK:
        for source, label, course in (("class", "班级", ""), ("study", "自学", course_id)):
            cache_key = (auth_scope, source, course)
            try:
                records = _list_class_tasks(c) if source == "class" else _list_study_tasks(c, course)
                rows = []
                for r in records:
                    rid = r.get("release_id") if source == "class" else r.get("list_id")
                    row = {"source": source, "source_label": label, "can_start": rid is not None, "account_key": account,
                           "task_id": r.get("task_id") or 0, "release_id": rid,
                           "task_name": r.get("task_name"), "progress": r.get("progress"), "score": r.get("score")}
                    for field in ("task_type", "free", "over_status", "over_time", "start_time", "release_time"):
                        if field in r:
                            row[field] = r[field]
                    if source == "study":
                        row.update(course_id=r.get("course_id") or course, list_id=rid,
                                   task_type=r.get("task_type"), grade=_int_or_default(r.get("grade"), study_grade))
                    rows.append(classify_task(row))
                with TASK_CACHE_LOCK:
                    TASK_CACHE[cache_key] = rows
                out.extend(_with_recovery(row) for row in rows)
            except Exception as exc:
                warnings.append(f"{label}任务读取失败，已保留上次列表及本机任务: {exc}")
                with TASK_CACHE_LOCK:
                    out.extend(_with_recovery(classify_task({**row, "stale": True})) for row in TASK_CACHE.get(cache_key, []))
    return out, warnings


def _reader_thread(job_id, proc, job=None):
    """绑定本轮对象，旧线程不能修改或重启后来替换的任务。"""
    with JOBS_LOCK:
        job = job or JOBS[job_id]
    for line in iter(proc.stdout.readline, b""):
        try:
            text = line.decode("utf-8", errors="replace").rstrip()
        except Exception:
            text = repr(line)
        with JOBS_LOCK:
            if JOBS.get(job_id) is job:
                job["logs"].append(text)
                if text == quiz.NO_PENDING_WORDS_MESSAGE:
                    job["no_pending_words"] = True
    proc.wait()
    with JOBS_LOCK:
        if JOBS.get(job_id) is not job:
            return
        job["done"] = True
        job["exit_code"] = proc.returncode
        _touch_jobs()
        if proc.returncode != 0:
            job["loop"] = False
            job["logs"].append("[loop] 本轮异常退出，已停止循环；结果不明时须在官方端核对后重新同步")
            _touch_jobs()
            return
        if proc.returncode == 0 and job.get("loop") and job.get("no_pending_words"):
            job["loop"] = False
            job["logs"].append("[loop] 单词已满分，停止循环")
            return
        if not job.get("loop") or job.get("stopped"):
            return

    # 循环模式: 如果未满分则重新启动
    if job.get("loop") and not job.get("stopped"):
        try:
            score = _query_score(job["source"], job["task_id"], job["release_id"], job.get("course_id"), job.get("list_id"), job.get("account_key"))
        except Exception as e:
            with JOBS_LOCK:
                if JOBS.get(job_id) is job:
                    job["logs"].append(f"[loop] 查询分数失败: {e}")
                    job["loop"] = False
                    _touch_jobs()
            return
        with JOBS_LOCK:
            if JOBS.get(job_id) is not job or job.get("stopped") or not job.get("loop"):
                return
            config = get_runtime_config()
            eligibility = _cached_task(config, job["source"], job["task_id"], job["release_id"], job.get("course_id"), job.get("list_id"))
            if eligibility:
                job["task_meta"] = task_metadata(eligibility)
            if score is None:
                job["loop"] = False
                job["logs"].append("[loop] 服务器未返回当前任务分数，停止循环；请刷新后核对")
                _touch_jobs()
                return
            if _score_full(score):
                job["loop"] = False
                job["logs"].append(f"[loop] 已满分 ({score}), 停止循环")
                _touch_jobs()
                return
            if not eligibility or not eligibility["can_loop_start"]:
                job["loop"] = False
                job["logs"].append("[loop] 停止循环: " + (eligibility["category_reason"] if eligibility else "当前鉴权下无法确认任务资格，请刷新列表"))
                _touch_jobs()
                return
            job["logs"].append(f"[loop] 当前分数={score}, 5s 后重新启动...")
        if job["cancel_event"].wait(5):
            return
        with JOBS_LOCK:
            if JOBS.get(job_id) is not job or job.get("stopped") or not job.get("loop"):
                return
            config = get_runtime_config()
            eligibility = _cached_task(config, job["source"], job["task_id"], job["release_id"], job.get("course_id"), job.get("list_id"))
            if not eligibility or not eligibility["can_loop_start"]:
                job["loop"] = False
                job["logs"].append("[loop] 重启前任务资格已变化，停止循环: " + (eligibility["category_reason"] if eligibility else "请刷新任务列表"))
                _touch_jobs()
                return
            try:
                _spawn_job(job["source"], eligibility["task_id"], job["release_id"], loop=True, config=config,
                           course_id=job.get("course_id"), list_id=job.get("list_id"),
                           task_type=job.get("task_type"), grade=job.get("grade"),
                           task_name=job.get("task_name"), task_meta=eligibility, expected_job=job)
            except Exception as exc:
                job["loop"] = False
                job["exit_code"] = 1
                job["logs"].append(f"[loop] 无法重新启动，已停止循环: {exc}")
                _touch_jobs()


def _query_score(source, task_id, release_id, course_id=None, list_id=None, expected_account=None):
    """轻量查询单个任务当前分数"""
    config = get_runtime_config()
    c = _client(config)
    account = _resolve_account(config, c)
    if expected_account and account != expected_account:
        raise ValueError("当前账号已改变，不能继续旧账号的任务")
    if source == "study":
        resp = c.study_task_list(course_id=course_id or "CET4_v2")
        recs = quiz._task_data(resp, "读取自学分数").get("task_list") or []
        for r in recs:
            if str(r.get("list_id")) == str(list_id or release_id):
                _refresh_cached_record(config, source, r, course_id)
                return r.get("score")
        return None

    page = 1
    while page <= 20:
        resp = c.page_task(page=page, size=50)
        recs = quiz._task_data(resp, "读取班级分数").get("records") or []
        if not recs:
            return None
        for r in recs:
            if str(r.get("task_id")) == str(task_id) and str(r.get("release_id")) == str(release_id):
                _refresh_cached_record(config, source, r)
                return r.get("score")
        if len(recs) < 50:
            return None
        page += 1
    return None


def _spawn_job(source, task_id, release_id, loop=False, config=None, course_id=None, list_id=None, task_type=None, grade=None, task_name=None, expected_job=None, task_meta=None):
    with JOBS_LOCK:
        return _spawn_job_locked(source, task_id, release_id, loop, config, course_id, list_id, task_type, grade, task_name, expected_job, task_meta)


def _spawn_job_locked(source, task_id, release_id, loop, config, course_id, list_id, task_type, grade, task_name, expected_job, task_meta):
    """启动子进程跑一个 task, 配置通过环境变量透传给 runner。"""
    course_id = (course_id or "CET4_v2") if source == "study" else None
    list_id = (list_id or release_id) if source == "study" else None
    key = _job_key(source, task_id, release_id, course_id, list_id, (task_meta or {}).get("account_key"))
    old = JOBS.get(key)
    if expected_job is not None:
        if old is not expected_job or old.get("stopped") or not old.get("loop") or not old.get("done"):
            raise ValueError("任务状态已变化，取消旧循环重启")
    elif _job_active(old):
        raise ValueError("任务正在运行或等待循环重启")
    account = (task_meta or {}).get("account_key")
    if not account:
        raise ValueError("账号尚未由服务器确认，请刷新任务列表")
    if expected_job and account != expected_job.get("account_key"):
        raise ValueError("当前账号已改变，取消旧账号的循环")
    checked = _with_recovery(task_meta, account)
    if checked["recovery_required"]:
        raise SafetyError(checked["recovery_reason"] or "提交状态需核对")
    if source == "study":
        args = ["study", str(task_id), str(list_id or release_id), str(course_id or "CET4_v2"), str(task_type or 3), str(grade or 2)]
    else:
        args = ["class", str(task_id), str(release_id)]
    environment = build_subprocess_env(config)
    environment["CIDAREN_TASK_ACCOUNT_KEY"] = account
    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "cidaren._runner", *args],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        bufsize=1,
        env=environment,
    )
    # 保留旧的 logs (循环模式下追加)
    if old and old.get("account_key") != account:
        old = None
    logs = old["logs"] if old else deque(maxlen=LOG_MAX)
    if old:
        logs.append(f"========== 第 {old.get('round', 1) + 1} 轮启动 ==========")
    job = {
        "proc": proc,
        "logs": logs,
        "started": time.time(),
        "done": False,
        "exit_code": None,
        "source": source,
        "release_id": release_id,
        "task_id": task_id,
        "course_id": course_id,
        "list_id": list_id,
        "task_type": task_type,
        "account_key": account,
        "grade": grade,
        "task_name": task_name or (old.get("task_name") if old else None),
        "task_meta": task_metadata(task_meta) if task_meta else (old.get("task_meta") if old else None),
        "task_confirmed": task_meta is not None,
        "loop": loop,
        "stopped": False,
        "no_pending_words": False,
        "cancel_event": threading.Event(),
        "round": (old.get("round", 1) + 1) if old else 1,
    }
    JOBS[key] = job
    _touch_jobs()
    threading.Thread(target=_reader_thread, args=(key, proc, job), daemon=True).start()
    return job


# ==== Routes ====

@app.route("/")
def index():
    return Response(_INDEX_HTML, mimetype="text/html; charset=utf-8", headers={"Cache-Control": "no-cache"})


@app.route("/ui.js")
def ui_script():
    return Response(Path(__file__).with_name("web_ui.js").read_text(encoding="utf-8"),
                    mimetype="application/javascript", headers={"Cache-Control": "no-cache"})


@app.route("/api/config")
def api_config():
    config = get_runtime_config()
    return jsonify({
        "ok": True,
        "config": config,
        "env_file": env_file_path(),
        "missing_auth": get_missing_auth_fields(config),
    })


@app.route("/api/config", methods=["POST"])
def api_save_config():
    body = request.get_json(force=True) or {}
    previous = get_runtime_config()
    saved = save_runtime_config(body)
    _stop_changed_auth_loops(previous, saved)
    return jsonify({
        "ok": True,
        "config": saved,
        "env_file": env_file_path(),
        "missing_auth": get_missing_auth_fields(saved),
    })


@app.route("/api/auth/capture/start", methods=["POST"])
def api_capture_start():
    if not _capture_request_allowed():
        return jsonify({"ok": False, "error": "仅允许从本机控制台启动获取"}), 403
    with JOBS_LOCK:
        ok, message = CAPTURE.start()
    status = CAPTURE.status()
    return jsonify({"ok": ok, "message": message, "capture": status}), (200 if ok else 409)


@app.route("/api/auth/capture/status")
def api_capture_status():
    if not _capture_request_allowed():
        return jsonify({"ok": False, "error": "仅允许从本机控制台读取状态"}), 403
    return jsonify({"ok": True, "capture": CAPTURE.status()})


@app.route("/api/auth/capture/cancel", methods=["POST"])
def api_capture_cancel():
    if not _capture_request_allowed():
        return jsonify({"ok": False, "error": "仅允许从本机控制台取消获取"}), 403
    ok, message = CAPTURE.cancel()
    return jsonify({"ok": ok, "message": message, "capture": CAPTURE.status()}), (200 if ok else 409)


@app.route("/api/tasks")
def api_tasks():
    if CAPTURE.is_active():
        return jsonify({"ok": False, "capturing": True, "error": "正在获取鉴权，任务列表已暂停刷新"}), 409
    try:
        tasks, warnings = _list_tasks()
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    if CAPTURE.is_active():
        return jsonify({"ok": False, "capturing": True, "error": "正在获取鉴权，任务列表已暂停刷新"}), 409
    with JOBS_LOCK:
        return jsonify({"ok": True, "tasks": _merge_jobs(tasks), "warnings": warnings,
                        "jobs": [_job_snapshot(job) for job in JOBS.values()], "seq": JOBS_SEQ})


@app.route("/api/jobs")
def api_jobs():
    """本地状态查询，不访问词达人服务器。"""
    if CAPTURE.is_active():
        return jsonify({"ok": False, "capturing": True, "error": "正在获取鉴权，任务状态刷新已暂停"}), 409
    with JOBS_LOCK:
        return jsonify({"ok": True, "jobs": [_job_snapshot(job) for job in JOBS.values()], "seq": JOBS_SEQ})


def _request_task(body, config=None):
    if not isinstance(body, dict):
        raise ValueError("任务参数必须是 JSON 对象")
    source = body.get("source") or "class"
    if source not in {"class", "study"}:
        raise ValueError("未知任务来源")
    try:
        task_id = int(str(body.get("task_id", 0)))
        release_id = body.get("release_id")
        if source == "class":
            release_id = int(str(release_id))
    except (TypeError, ValueError):
        raise ValueError("任务编号格式错误") from None
    if source == "study" and task_id == -1:
        task_id = 0
    if task_id < 0 or (source == "class" and task_id == 0) or release_id in (None, ""):
        raise ValueError("任务编号不完整")
    cfg = config or get_runtime_config()
    course_id = str(body.get("course_id") or cfg.get("COURSE_ID") or "CET4_v2").strip() if source == "study" else None
    list_id = body.get("list_id") or release_id
    return source, task_id, release_id, course_id, list_id


@app.route("/api/start", methods=["POST"])
def api_start():
    if CAPTURE.is_active():
        return jsonify({"ok": False, "error": "正在获取鉴权，暂不能启动任务"}), 409
    body = request.get_json(silent=True)
    config = get_runtime_config()
    try:
        source, task_id, release_id, course_id, list_id = _request_task(body, config)
        task_type = int(body.get("task_type") or 3)
    except (TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    grade = _int_or_default(body.get("grade") or config.get("STUDY_GRADE"), 2)
    loop = bool(body.get("loop", False))
    missing = get_missing_auth_fields(config)
    if missing:
        return jsonify({"ok": False, "error": f"请先填写配置: {', '.join(missing)}"}), 400
    if not _cached_task(config, source, task_id, release_id, course_id, list_id):
        return jsonify({"ok": False, "error": "当前任务信息尚未确认，请先刷新任务列表"}), 409
    try:
        fresh = _with_recovery(_remote_task(config, source, release_id, course_id, list_id))
        if body.get("account_key") is not None and body["account_key"] != fresh["account_key"]:
            raise ValueError("所选任务属于其他账号，请刷新当前账号的任务列表")
        if source == "class" and str(fresh.get("task_id")) != str(task_id):
            raise ValueError("服务器任务编号已变化，请刷新后重新选择")
    except Exception as exc:
        return jsonify({"ok": False, "error": f"无法确认账号和任务: {exc}"}), 409
    with JOBS_LOCK:
        if CAPTURE.is_active():
            return jsonify({"ok": False, "error": "正在获取鉴权，暂不能启动任务"}), 409
        if _auth_scope(config) != _auth_scope(get_runtime_config()):
            return jsonify({"ok": False, "error": "鉴权已变化，请刷新账号和任务"}), 409
        eligibility = _with_recovery(fresh)
        if eligibility["recovery_required"]:
            return jsonify({"ok": False, "error": eligibility["recovery_reason"] or "提交状态需核对", "task": eligibility}), 409
        if not eligibility["can_start"] or (loop and not eligibility["can_loop_start"]):
            return jsonify({"ok": False, "error": eligibility["category_reason"] + ("；不能自动循环" if loop and eligibility["can_start"] else ""),
                            "task": eligibility}), 409
        task_id = eligibility["task_id"]
        task_type = eligibility.get("task_type") or task_type
        job = _find_job(source, task_id, release_id, course_id, list_id, eligibility.get("account_key"))
        if _job_active(job):
            return jsonify({"ok": False, "error": "任务正在运行或等待循环重启",
                            "job": _job_snapshot(job), "seq": JOBS_SEQ}), 409
        try:
            job = _spawn_job(source, task_id, release_id, loop=loop, config=config, course_id=course_id,
                             list_id=list_id, task_type=task_type, grade=grade,
                             task_name=eligibility.get("task_name"), task_meta=eligibility)
        except Exception as exc:
            return jsonify({"ok": False, "error": f"无法启动任务: {exc}"}), 500
        return jsonify({"ok": True, "job": _job_snapshot(job), "seq": JOBS_SEQ})


@app.route("/api/start_all", methods=["POST"])
def api_start_all():
    """一键启动所有班级任务循环"""
    if CAPTURE.is_active():
        return jsonify({"ok": False, "error": "正在获取鉴权，暂不能启动任务"}), 409
    try:
        config = get_runtime_config()
        missing = get_missing_auth_fields(config)
        if missing:
            return jsonify({"ok": False, "error": f"请先填写配置: {', '.join(missing)}"}), 400
        c = _client(config)
        account = _resolve_account(config, c)
        tasks = _list_class_tasks(c)
        tasks = [_with_recovery(classify_task({**task_metadata(row), "source": "class", "source_label": "班级", "account_key": account})) for row in tasks]
        with TASK_CACHE_LOCK:
            TASK_CACHE[(_auth_scope(config), "class", "")] = tasks
        started = 0
        skipped = 0
        with JOBS_LOCK:
            if CAPTURE.is_active():
                return jsonify({"ok": False, "error": "正在获取鉴权，暂不能启动任务"}), 409
            if _auth_scope(config) != _auth_scope(get_runtime_config()):
                return jsonify({"ok": False, "error": "鉴权已变化，请刷新账号和任务"}), 409
            for r in tasks:
                tid = r.get("task_id")
                rid = r.get("release_id")
                job = _find_job("class", tid, rid, account_key=account)
                if _job_active(job):
                    skipped += 1
                    continue
                r = _with_recovery(r)
                if r["category"] != "available" or not r["can_loop_start"]:
                    skipped += 1
                    continue
                _spawn_job("class", tid, rid, loop=True, config=config, task_name=r.get("task_name"), task_type=r.get("task_type"), task_meta=r)
                started += 1
            return jsonify({"ok": True, "started": started, "skipped": skipped,
                            "jobs": [_job_snapshot(job) for job in JOBS.values()], "seq": JOBS_SEQ})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


def _recovery_request_allowed():
    if not is_loopback_request(request.remote_addr, urlparse(request.host_url).hostname):
        return False
    origin = urlparse(request.headers.get("Origin", ""))
    target = urlparse(request.host_url)
    try:
        return (origin.scheme in {"http", "https"} and not origin.username and not origin.password
                and origin.path in {"", "/"} and not origin.query and not origin.fragment
                and (origin.scheme, origin.hostname, origin.port or (443 if origin.scheme == "https" else 80))
                == (target.scheme, target.hostname, target.port or (443 if target.scheme == "https" else 80))
                and request.headers.get("Sec-Fetch-Site") not in {"cross-site", "same-site"}
                and request.is_json)
    except ValueError:
        return False


@app.route("/api/tasks/recovery/ack", methods=["POST"])
def api_recovery_ack():
    if not _recovery_request_allowed():
        return jsonify({"ok": False, "error": "仅允许本机同源网页确认恢复"}), 403
    if CAPTURE.is_active():
        return jsonify({"ok": False, "error": "正在获取鉴权，暂不能重新同步"}), 409
    body = request.get_json(silent=True)
    config = get_runtime_config()
    try:
        source, task_id, release_id, course_id, list_id = _request_task(body, config)
        if body.get("confirmed") is not True or not isinstance(body.get("recovery_id"), str):
            raise ValueError("请先在官方端核对，并明确确认当前暂停记录")
        account = _resolve_account(config)
        if body.get("account_key") != account:
            raise ValueError("账号身份已变化，保留原账号的暂停记录")
        with JOBS_LOCK:
            job = _find_job(source, task_id, release_id, course_id, list_id, account)
            if _job_active(job):
                raise ValueError("任务仍在运行，请先停止任务并等待退出")
            acknowledged_job = job
        refreshed = None

        def validate():
            nonlocal refreshed
            refreshed = _remote_task(config, source, release_id, course_id, list_id)
            if refreshed["account_key"] != account or _auth_scope(config) != _auth_scope(get_runtime_config()):
                raise ValueError("重新同步期间账号已变化，暂停保留")
            if CAPTURE.is_active():
                raise ValueError("正在获取鉴权，暂停保留")
            return True

        SAFETY.ack(**_safety_identity({"source": source, "release_id": release_id, "course_id": course_id,
                                     "list_id": list_id}, account),
                   expected_recovery_id=body["recovery_id"], validate=validate)
        with JOBS_LOCK:
            job = _find_job(source, task_id, release_id, course_id, list_id, account)
            if job and job is acknowledged_job and job.get("account_key") == account:
                job["task_meta"] = task_metadata(refreshed)
                job["loop"] = False
                job["logs"].append("[recovery] 已人工核对并只读重新同步；未重放请求，需手动启动")
            _touch_jobs()
            return jsonify({"ok": True, "task": _with_recovery(refreshed),
                            "jobs": [_job_snapshot(j) for j in JOBS.values()], "seq": JOBS_SEQ})
    except (ValueError, SafetyError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 409
    except Exception as exc:
        return jsonify({"ok": False, "error": f"重新同步失败，暂停保留: {exc}"}), 500


@app.route("/api/stop", methods=["POST"])
def api_stop():
    body = request.get_json(silent=True)
    try:
        source, task_id, release_id, course_id, list_id = _request_task(body)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    with JOBS_LOCK:
        job = _find_job(source, task_id, release_id, course_id, list_id, body.get("account_key"))
        if not job:
            return jsonify({"ok": False, "error": "任务未运行"}), 404
        job["stopped"] = True
        job["loop"] = False
        job["cancel_event"].set()
        _touch_jobs()
        if not job["done"]:
            try:
                job["proc"].send_signal(signal.SIGTERM)
            except ProcessLookupError:
                pass  # 进程恰好退出，由 reader 收尾
            except Exception as exc:
                return jsonify({"ok": False, "error": str(exc), "job": _job_snapshot(job), "seq": JOBS_SEQ}), 500
        return jsonify({"ok": True, "job": _job_snapshot(job), "seq": JOBS_SEQ})


@app.route("/api/logs")
def api_logs_query():
    source = request.args.get("source") or "class"
    task_id = request.args.get("task_id")
    release_id = request.args.get("release_id")
    course_id = request.args.get("course_id")
    list_id = request.args.get("list_id")
    with JOBS_LOCK:
        job = _find_job(source, task_id, release_id, course_id, list_id, request.args.get("account_key"))
        task = None if job else _cached_task(get_runtime_config(), source, task_id, release_id, course_id, list_id)
        if task and request.args.get("account_key") not in (None, task.get("account_key")):
            task = None
        return _logs_response(job, task)


@app.route("/api/logs/<int:task_id>/<int:release_id>")
def api_logs(task_id, release_id):
    with JOBS_LOCK:
        job = _find_job("class", task_id, release_id)
        task = None if job else _cached_task(get_runtime_config(), "class", task_id, release_id)
        return _logs_response(job, task)


def _logs_response(job, task=None):
    if not job:
        if task and task.get("recovery_required"):
            state = {**task, "running": False, "active": False, "waiting": False, "loop": False,
                     "done": True, "exit_code": 3, "status": "recovery_required", "has_logs": True}
            return jsonify({"ok": True, "logs": ["⏸ " + (task.get("recovery_reason") or "提交状态需要核对"),
                            "本次服务没有此前进程的完整日志；暂停来自本机持久记录，请在官方端核对后重新同步。"],
                            "job": state, "seq": JOBS_SEQ, "done": True, "active": False, "waiting": False,
                            "loop": False, "stopped": False, "exit_code": 3, "status": "recovery_required"})
        return jsonify({"ok": False, "logs": [], "error": "本次服务中还没有该任务的日志"}), 404
    state = _job_snapshot(job)
    return jsonify({"ok": True, "logs": list(job["logs"]), "job": state, "seq": JOBS_SEQ,
                    "done": state["done"], "exit_code": state["exit_code"],
                    "active": state["active"], "waiting": state["waiting"],
                    "loop": state["loop"], "stopped": state["stopped"], "status": state["status"]})


# ==== HTML ====
_INDEX_HTML = '''<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>词达人 控制台</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", sans-serif;
         margin: 0; padding: 24px; background: #f5f7fb; color: #1d1d1f; }
  h1 { font-size: 26px; margin: 0 0 8px; }
  .subtitle { margin: 0 0 20px; color: #667085; font-size: 14px; }
  .layout { display: grid; gap: 20px; }
  .card { background: #fff; border: 1px solid #eaecf0; border-radius: 16px; box-shadow: 0 8px 24px rgba(15,23,42,.04); overflow: hidden; }
  .card-head { padding: 18px 20px; border-bottom: 1px solid #f2f4f7; display: flex; justify-content: space-between; align-items: center; gap: 12px; }
  .card-head h2 { margin: 0; font-size: 18px; }
  .card-head p { margin: 4px 0 0; color: #667085; font-size: 13px; }
  .card-body { padding: 20px; }
  .bar { display: flex; gap: 12px; align-items: center; margin-bottom: 16px; font-size: 13px; color: #666; flex-wrap: wrap; }
  button { font: inherit; padding: 6px 14px; border-radius: 6px; border: 1px solid #d2d2d7;
           background: white; cursor: pointer; }
  button:hover { background: #f0f0f0; }
  button.primary { background: #007aff; color: white; border-color: #007aff; }
  button.primary:hover { background: #0062cc; }
  button.danger { background: #ff3b30; color: white; border-color: #ff3b30; }
  button.loop { background: #5856d6; color: white; border-color: #5856d6; }
  button.loop:hover { background: #4845b0; }
  button:disabled { opacity: .5; cursor: not-allowed; }
  .btn-row { display: flex; gap: 10px; flex-wrap: wrap; }
  .grid { display: grid; grid-template-columns: repeat(2, minmax(280px, 1fr)); gap: 16px; }
  .field { display: flex; flex-direction: column; gap: 8px; }
  .field label { font-size: 13px; font-weight: 600; color: #344054; }
  .field input { width: 100%; border: 1px solid #d0d5dd; border-radius: 10px; padding: 10px 12px; font: inherit; }
  .field small { color: #667085; font-size: 12px; }
  .full { grid-column: 1 / -1; }
  .status-box { display: inline-flex; align-items: center; gap: 8px; padding: 8px 12px; border-radius: 999px; background: #f8fafc; border: 1px solid #e2e8f0; color: #475467; font-size: 12px; }
  .status-box.ok { background: #ecfdf3; color: #027a48; border-color: #d1fadf; }
  .status-box.warn { background: #fff7ed; color: #b54708; border-color: #fed7aa; }
  .capture-help { margin: 0; color: #667085; font-size: 13px; line-height: 1.7; }
  .capture-actions { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-top: 16px; }
  .inline-code { font-family: ui-monospace, Menlo, monospace; background: #f2f4f7; padding: 2px 6px; border-radius: 6px; }
  table { width: 100%; border-collapse: collapse; background: white; border-radius: 8px; overflow: hidden;
          box-shadow: 0 1px 3px rgba(0,0,0,.06); }
  th, td { padding: 10px 12px; text-align: left; border-bottom: 1px solid #f0f0f0; font-size: 14px; }
  th { background: #fafafa; font-weight: 600; color: #666; font-size: 12px; text-transform: uppercase; }
  tr:last-child td { border-bottom: none; }
  .progress-bar { width: 100px; height: 6px; background: #e8e8ed; border-radius: 3px; overflow: hidden; display: inline-block; vertical-align: middle; margin-right: 6px; }
  .progress-bar > div { height: 100%; background: #34c759; transition: width .3s; }
  .score { font-weight: 600; }
  .score.full { color: #34c759; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 500; }
  .badge.run { background: #fff3cd; color: #856404; }
  .badge.done { background: #d4edda; color: #155724; }
  .badge.fail { background: #fee4e2; color: #b42318; }
  .task-filters { display: flex; gap: 8px; flex-wrap: wrap; margin: 0 0 14px; align-items: center; }
  .task-filters button.selected { background: #eaf3ff; border-color: #007aff; color: #0059bf; }
  .task-filters select { padding: 7px 10px; border: 1px solid #d2d2d7; border-radius: 6px; font: inherit; }
  .filter-summary { color: #667085; font-size: 13px; }
  .category { display: inline-block; font-size: 11px; border-radius: 6px; padding: 2px 6px; margin-top: 6px; }
  .category.available { color: #175cd3; background: #eff8ff; }
  .category.full { color: #027a48; background: #ecfdf3; }
  .category.blocked { color: #b42318; background: #fef3f2; }
  .category.unknown { color: #b54708; background: #fffaeb; }
  .task-reason { color: #667085; font-size: 12px; max-width: 420px; line-height: 1.5; margin-top: 4px; }
  .modal { display: none; position: fixed; inset: 0; background: rgba(0,0,0,.5); z-index: 100; }
  .modal.show { display: flex; align-items: center; justify-content: center; }
  .modal-body { background: #1e1e1e; color: #d4d4d4; width: 80vw; height: 75vh; border-radius: 8px;
                padding: 16px; overflow: hidden; display: flex; flex-direction: column; }
  .modal-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; color: white; }
  .modal-logs { flex: 1; overflow-y: auto; font-family: ui-monospace, Menlo, monospace; font-size: 12px;
                white-space: pre-wrap; word-break: break-word; line-height: 1.5; }
  @media (max-width: 900px) {
    body { padding: 16px; }
    .grid { grid-template-columns: 1fr; }
    table { display: block; overflow-x: auto; }
  }
</style>
</head>
<body>
  <h1>📚 cidaren 控制台</h1>
  <p class="subtitle">在浏览器里维护词达人鉴权与 LLM 配置，保存后自动写入 <span class="inline-code">.env</span>，并用于后续任务执行。</p>

  <div class="layout">
    <section class="card">
      <div class="card-head">
        <div>
          <h2>自动获取鉴权</h2>
          <p>从 PC 微信中的词达人学生端读取同一次请求里的完整鉴权字段。</p>
        </div>
        <div id="capture-pill" class="status-box">正在读取状态…</div>
      </div>
      <div class="card-body">
        <p class="capture-help">
          点击开始后，在 PC 微信里打开“词达人 → 学生端”并进入任意页面。成功后会先恢复系统代理、验证账号，
          再自动保存配置和刷新任务。首次使用会在当前用户证书库中信任本项目的独立抓取证书。
        </p>
        <div class="capture-actions">
          <button id="capture-start" class="primary" onclick="startCapture()">🔑 获取 Token</button>
          <button id="capture-cancel" class="danger" onclick="cancelCapture()" disabled>取消获取</button>
          <span id="capture-message">尚未开始获取</span>
        </div>
      </div>
    </section>

    <section class="card">
      <div class="card-head">
        <div>
          <h2>配置中心</h2>
          <p>这里填写 token / LLM 变量，点击保存后会同步落盘到项目根目录的 <span class="inline-code">.env</span>。</p>
        </div>
        <div id="config-pill" class="status-box">读取中...</div>
      </div>
      <div class="card-body">
        <div class="grid">
          <div class="field">
            <label for="USERTOKEN">USERTOKEN</label>
            <input id="USERTOKEN" type="password" autocomplete="off" />
            <small>词达人请求头中的 usertoken。</small>
          </div>
          <div class="field">
            <label for="ABC">ABC</label>
            <input id="ABC" type="password" autocomplete="off" />
            <small>词达人请求头中的 abc。</small>
          </div>
          <div class="field full">
            <label for="AUTH_V">AUTH_V</label>
            <input id="AUTH_V" type="password" autocomplete="off" />
            <small>词达人请求头中的 authorization-v。</small>
          </div>
          <div class="field full">
            <label for="USER_AGENT">USER_AGENT</label>
            <input id="USER_AGENT" type="password" autocomplete="off" />
            <small>自动获取时一并保存，任务请求会沿用微信中的 User-Agent。</small>
          </div>
          <div class="field">
            <label for="COURSE_ID">COURSE_ID</label>
            <input id="COURSE_ID" type="text" placeholder="CET4_v2" />
            <small>自学任务课程 ID。</small>
          </div>
          <div class="field">
            <label for="STUDY_GRADE">STUDY_GRADE</label>
            <input id="STUDY_GRADE" type="number" min="1" max="4" placeholder="2" />
            <small>自学模式: 1 快速 / 2 普通 / 3 完整 / 4 超级困难。</small>
          </div>
          <div class="field">
            <label for="LLM_URL">LLM_URL</label>
            <input id="LLM_URL" type="text" placeholder="https://ai.saurlax.com/" />
            <small>OpenAI 兼容接口地址，留空表示不启用 LLM 兜底。</small>
          </div>
          <div class="field">
            <label for="LLM_MODEL">LLM_MODEL</label>
            <input id="LLM_MODEL" type="text" placeholder="step-3.6" />
            <small>请求使用的模型名。</small>
          </div>
          <div class="field full">
            <label for="LLM_KEY">LLM_KEY</label>
            <input id="LLM_KEY" type="password" autocomplete="off" />
            <small>Bearer token / API Key。</small>
          </div>
        </div>

        <div class="bar" style="margin-top:18px; margin-bottom:0; justify-content:space-between;">
          <div id="env-path" class="status-box">.env 路径加载中...</div>
          <div class="btn-row">
            <button onclick="loadConfig()">重新读取</button>
            <button class="primary" onclick="saveConfig(false)">保存配置</button>
            <button class="primary" onclick="saveConfig(true)">保存并刷新任务</button>
          </div>
        </div>
      </div>
    </section>

    <section class="card" id="task-card">
      <div class="card-head">
        <div>
          <h2>任务面板</h2>
          <p>启动后会在子进程里执行刷题流程，日志可实时查看。</p>
        </div>
        <div class="status-box">运行状态实时更新 · 分数每 30 秒刷新</div>
      </div>
      <div class="card-body">
        <div class="bar">
          <button id="refresh-tasks" onclick="loadTasks()">🔄 刷新任务</button>
          <button id="start-all" class="loop" onclick="startAllClassLoop()">🚀 循环启动可继续的班级任务</button>
          <span id="status">加载中...</span>
        </div>
        <div class="task-filters" id="task-filters" aria-label="任务分类">
          <button data-filter="all" class="selected" aria-pressed="true">全部</button>
          <button data-filter="available" aria-pressed="false">未完成可继续</button>
          <button data-filter="full" aria-pressed="false">已满分</button>
          <button data-filter="blocked" aria-pressed="false">不可执行</button>
          <button data-filter="unknown" aria-pressed="false">待确认</button>
          <label for="source-filter">来源</label>
          <select id="source-filter"><option value="all">全部来源</option><option value="class">班级</option><option value="study">自学</option></select>
          <span class="filter-summary" id="filter-summary" role="status"></span>
        </div>
        <table>
          <thead>
            <tr>
              <th style="width:40px">#</th>
              <th>任务名</th>
              <th style="width:160px">进度</th>
              <th style="width:80px">分数</th>
              <th style="width:80px">状态</th>
              <th style="width:200px">操作</th>
            </tr>
          </thead>
          <tbody id="tbody"></tbody>
        </table>
      </div>
    </section>
  </div>

  <div class="modal" id="modal" onclick="if(event.target===this)closeLogs()">
    <div class="modal-body">
      <div class="modal-head">
        <span id="modal-title">日志</span>
        <span id="modal-status" role="status"></span>
        <button onclick="closeLogs()">✕ 关闭</button>
      </div>
      <div class="modal-logs" id="modal-logs"></div>
    </div>
  </div>

<script src="/ui.js"></script>
</body>
</html>
'''


def main():
    host = "127.0.0.1"
    port = 5001
    CAPTURE.recover_stale_proxy()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, port))
        except OSError as exc:
            raise SystemExit(f"端口 {port} 已被占用。请先关闭占用该端口的程序。详细信息: {exc}")
    try:
        bank = prepare_default_store()
        with bank.runtime():
            counts = bank.status()
            print(f"📚 词库: {bank.path}，历史原文 {counts['legacy']} 条，缓存 {counts['cache']} 条")
            print(f"🌐 http://localhost:{port}")
            print(f"📄 配置文件: {env_file_path()}")
            if os.environ.get("CIDAREN_NO_BROWSER") != "1":
                threading.Timer(0.8, lambda: webbrowser.open(f"http://localhost:{port}")).start()
            app.run(host=host, port=port, debug=False, threaded=True)
    except BankError as exc:
        raise SystemExit(f'词库错误: {exc}')


if __name__ == "__main__":
    main()
