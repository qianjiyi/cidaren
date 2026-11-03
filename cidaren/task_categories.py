"""Task eligibility from the server's task metadata, independent of process state."""

from __future__ import annotations

import math
import time
from datetime import datetime, timedelta, timezone


OFFICIAL_STATUS_FIELDS = (
    "official_status", "official_status_label", "official_status_reason", "official_status_checked_at", "official_status_stale",
)
CATEGORY_FIELDS = (
    "category", "category_label", "category_reason", "category_checked_at",
    "can_start", "can_loop_start", "repeatable", "eligibility_reason", "deadline",
    "deadline_ms", "task_kind_label", *OFFICIAL_STATUS_FIELDS,
)
LABELS = {"available": "程序可运行", "full": "已满分", "unsupported": "需在官方端完成",
          "blocked": "当前不可启动", "unknown": "资格待确认"}
CHINA_TIME = timezone(timedelta(hours=8))
OFFICIAL_LABELS = {"ongoing": "进行中", "ended": "已结束", "unknown": "状态待确认"}


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _integer(value):
    result = _number(value)
    return int(result) if result is not None and result.is_integer() else None


def _percent(value):
    result = _number(value)
    return result if result is not None and 0 <= result <= 100 else None


def is_unstarted_class_test(task):
    """Recognize only the official sentinel for a never-started class test.

    This identifies the initial task ID; callers must still check eligibility,
    account ownership, persisted safety state and the official lifecycle.
    """
    if not isinstance(task, dict):
        return False
    task_id = task.get("task_id")
    sentinel = type(task_id) is int and task_id == -1 or task_id == "-1"
    return ((task.get("source") or "class") == "class"
            and _integer(task.get("task_type")) == 2 and sentinel
            and _percent(task.get("score")) == 0
            and _percent(task.get("progress")) == 0)


def official_task_status(task):
    """Only the official list filters establish lifecycle; score/deadline do not."""
    status = task.get("official_status")
    if status not in OFFICIAL_LABELS:
        status = "unknown"
    checked_at = _integer(task.get("official_status_checked_at"))
    reason = task.get("official_status_reason")
    if not isinstance(reason, str) or not reason:
        reason = ({"ongoing": "词达人官方进行中列表中的任务",
                   "ended": "词达人官方已结束列表中的任务"}.get(status)
                  or ("自学列表未提供官方进行中 / 已结束分类" if task.get("source") == "study"
                      else "尚未取得词达人官方进行中 / 已结束分类"))
    if task.get("stale") and "上次成功" not in reason:
        reason += "；远端刷新失败，显示上次成功读取的状态"
    return dict(official_status=status, official_status_label=OFFICIAL_LABELS[status],
                official_status_reason=reason, official_status_checked_at=max(0, checked_at or 0),
                official_status_stale=bool(task.get("official_status_stale") or task.get("stale")))


def classify_task(task, *, now_ms=None):
    """Do not infer attempts or task type from names, process exit codes or progress alone."""
    result = dict(task)
    result.update(official_task_status(task))
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    source = task.get("source") or "class"
    task_type = _integer(task.get("task_type"))
    score, progress = _percent(task.get("score")), _percent(task.get("progress"))
    over_status = _integer(task.get("over_status"))
    start, duration = _number(task.get("start_time")), _number(task.get("over_time"))
    deadline_ms = None
    if source == "class" and start is not None and start >= 100_000_000_000 and duration is not None and duration >= 0:
        value = start + duration
        if value <= 253_402_185_599_000:  # representable datetime, not arbitrary corrupt input
            deadline_ms = int(value)
    deadline = datetime.fromtimestamp(deadline_ms / 1000, CHINA_TIME).strftime("%Y-%m-%d %H:%M:%S") if deadline_ms is not None else None
    kind = "自学" if source == "study" else {1: "学习", 2: "测试", 6: "PK"}.get(task_type, "未知")
    result.update(category_checked_at=now_ms, deadline=deadline, deadline_ms=deadline_ms,
                  task_kind_label=kind, repeatable=False, can_start=False, can_loop_start=False)

    def finish(category, reason, *, single=False, loop=False):
        result.update(category=category, category_label=LABELS[category], category_reason=reason,
                      eligibility_reason=reason, can_start=single, can_loop_start=loop)
        return result

    if score is not None and score >= 100 and not (source == "class" and task_type == 2):
        return finish("full", "分数已达到 100，无需继续运行" + ("；任务也已截止" if over_status == 3 else ""))
    if source not in {"class", "study"}:
        return finish("unknown", "未知任务来源，无法确认执行资格")
    task_id = _integer(task.get("task_id"))
    release_id = (task.get("list_id") or task.get("release_id")) if source == "study" else task.get("release_id")
    if release_id in (None, "") or (source == "class" and (task_id is None or task_id <= 0)
                                    and not is_unstarted_class_test(task)):
        return finish("unknown", "任务编号不完整，请刷新任务列表")
    if source == "study":
        if task_id is None or task_id < -1:
            return finish("unknown", "自学任务编号格式异常，请刷新任务列表")
        if task_id == -1:
            result["task_id"] = 0  # -1 means no practice created yet, not an unavailable task
    if source == "class":
        if task_type == 2 and task.get("test_completed"):
            return finish("blocked", "本机已确认本次测试结束，请在词达人官方端核对结果；程序不会重新打开测试")
        if result["official_status"] == "ended":
            return finish("blocked", "词达人官方已结束列表中的任务，不能启动")
        if over_status == 3 or (deadline_ms is not None and now_ms >= deadline_ms):
            return finish("blocked", "任务已截止" + (f"（{deadline}）" if deadline else ""))
        if over_status == 1:
            return finish("blocked", "任务尚未开放，到开放时间后刷新列表")
        if task_type == 2:
            raw_progress = _number(task.get("progress"))
            if raw_progress is not None and raw_progress >= 100:
                return finish("blocked", "测试进度已达到 100% 或超过正常范围，请在词达人官方端核对结果；程序不会重新打开测试")
            checked_at = result["official_status_checked_at"]
            if (result["official_status"] != "ongoing" or result["official_status_stale"]
                    or checked_at <= 0 or now_ms - checked_at > 90_000 or checked_at - now_ms > 1_000):
                return finish("unknown", "测试的官方进行中状态尚未新鲜确认，请刷新任务列表；旧状态或分类读取失败时不会启动")
            if over_status != 2:
                return finish("unknown", "测试开放状态尚未确认，请在词达人官方端核对")
            if _integer(task.get("free")) != 1:
                return finish("unknown", "测试课程权限尚未确认，请在词达人官方端核对")
            if score is None or progress is None:
                return finish("unknown", "测试分数或进度无效，请在词达人官方端核对；程序不会重新打开测试")
            if score >= 100:
                return finish("full", "测试分数已达到 100，请在词达人官方端核对结果")
            return finish("available", "测试可单次执行；完成后不会自动重新打开或循环", single=True)
        if task_type == 6:
            return finish("unsupported", "当前程序未实现 PK 流程，请在词达人学生端完成")
        if task_type != 1:
            return finish("unknown", "任务类型未确认，不能套用学习答题流程")
    elif task_type != 3:
        return finish("unknown", "自学任务类型未确认，请刷新任务列表")
    if _integer(task.get("free")) != 1:
        return finish("unknown", "课程权限待确认，不能仅凭任务列表判断是否可运行")
    result["repeatable"] = True
    if score is None or progress is None:
        return finish("unknown", "分数或进度字段缺失，允许手动单次尝试，暂停自动循环", single=True)
    if source == "class" and over_status != 2:
        return finish("unknown", "开放状态未确认，允许手动单次尝试，暂停自动循环", single=True)
    reason = "已完成一轮但未满分，可继续练习" if progress >= 100 else "尚未满分，可继续完成任务"
    if progress == 0:
        reason = "尚未完成，可开始练习"
    if deadline:
        reason += f"；截止 {deadline}"
    if task.get("stale"):
        reason += "；远端刷新失败，当前为上次成功列表"
    return finish("available", reason, single=True, loop=True)
