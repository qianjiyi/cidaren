"""Task eligibility from the server's task metadata, independent of process state."""

from __future__ import annotations

import math
import time
from datetime import datetime, timedelta, timezone


CATEGORY_FIELDS = (
    "category", "category_label", "category_reason", "category_checked_at",
    "can_start", "can_loop_start", "repeatable", "eligibility_reason", "deadline",
    "deadline_ms", "task_kind_label",
)
LABELS = {"available": "未完成可继续", "full": "已满分", "blocked": "不可执行", "unknown": "待确认"}
CHINA_TIME = timezone(timedelta(hours=8))


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


def classify_task(task, *, now_ms=None):
    """Do not infer attempts or task type from names, process exit codes or progress alone."""
    result = dict(task)
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

    if score is not None and score >= 100:
        return finish("full", "分数已达到 100，无需继续运行" + ("；任务也已截止" if over_status == 3 else ""))
    if source not in {"class", "study"}:
        return finish("unknown", "未知任务来源，无法确认执行资格")
    task_id = _integer(task.get("task_id"))
    release_id = (task.get("list_id") or task.get("release_id")) if source == "study" else task.get("release_id")
    if release_id in (None, "") or (source == "class" and (task_id is None or task_id <= 0)):
        return finish("unknown", "任务编号不完整，请刷新任务列表")
    if source == "study":
        if task_id is None or task_id < -1:
            return finish("unknown", "自学任务编号格式异常，请刷新任务列表")
        if task_id == -1:
            result["task_id"] = 0  # -1 means no practice created yet, not an unavailable task
    if source == "class":
        if over_status == 3 or (deadline_ms is not None and now_ms >= deadline_ms):
            return finish("blocked", "任务已截止" + (f"（{deadline}）" if deadline else ""))
        if over_status == 1:
            return finish("blocked", "任务尚未开放，到开放时间后刷新列表")
        if task_type == 2:
            if progress is not None and progress >= 100:
                return finish("blocked", "测试已完成；当前程序不提供重做测试入口")
            return finish("blocked", "当前程序不支持测试专用答题流程，请在词达人学生端完成")
        if task_type == 6:
            return finish("blocked", "当前程序不支持 PK 任务")
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
