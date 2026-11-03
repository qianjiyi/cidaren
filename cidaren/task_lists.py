"""Read official class task lists without depending on the answer runner."""

import re
import time


def _strict_int(value):
    if type(value) is int:
        return value
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value):
        return int(value)
    return None


def _class_task_pages(client, search_type):
    out, page = [], 1
    while True:
        response = client.page_task(page=page, size=50, search_type=search_type)
        if not isinstance(response, dict) or _strict_int(response.get("code")) != 1:
            raise ValueError("读取班级任务列表失败，服务器未确认成功")
        data = response.get("data")
        if not isinstance(data, dict):
            raise ValueError("班级任务列表 data 格式错误")
        records = data.get("records")
        if not isinstance(records, list) or any(not isinstance(row, dict) for row in records):
            raise ValueError("班级任务列表格式错误")
        out.extend(records)
        total = _strict_int(data.get("total"))
        if total is not None and (total < 0 or total < len(out)):
            raise ValueError("班级任务列表总数异常")
        if not records or len(records) < 50:
            if total is not None and len(out) != total:
                raise ValueError("班级任务列表分页不完整，请刷新重试")
            break
        if total is not None and len(out) == total:
            break
        page += 1
        if page > 20:
            raise ValueError("班级任务列表超出分页上限，不能确认官方分类")
    return out


def _class_task_identity(row):
    value = row.get("release_id")
    if value in (None, "") or isinstance(value, bool):
        raise ValueError("班级任务缺少发布编号，不能确认官方分类")
    return str(value)


def read_class_tasks(client):
    """Return all tasks annotated by official 1=ongoing / 2=ended filters."""
    checked_at = int(time.time() * 1000)
    records = _class_task_pages(client, "0")
    identities = [_class_task_identity(row) for row in records]
    memberships, reason = {}, None
    try:
        if len(set(identities)) != len(identities):
            raise ValueError("官方全部列表包含重复发布编号")
        for search_type, status in (("1", "ongoing"), ("2", "ended")):
            filtered_ids = [_class_task_identity(row) for row in _class_task_pages(client, search_type)]
            if len(set(filtered_ids)) != len(filtered_ids):
                raise ValueError("官方分类列表包含重复发布编号")
            for identity in filtered_ids:
                if identity in memberships:
                    raise ValueError("同一任务同时出现在官方进行中与已结束列表，请重新同步")
                memberships[identity] = status
    except Exception:
        memberships.clear()
        reason = "词达人官方分类读取失败或列表变化，请刷新重试；未按分数、期限或程序能力推断状态"
    out = []
    for row, identity in zip(records, identities):
        status = memberships.get(identity, "unknown")
        out.append({**row, "official_status": status, "official_status_checked_at": checked_at,
                    "official_status_stale": status == "unknown",
                    "official_status_reason": reason or {
                        "ongoing": "词达人官方进行中列表中的任务（search_type=1）",
                        "ended": "词达人官方已结束列表中的任务（search_type=2）",
                        "unknown": "未能在词达人官方分类列表中唯一确认此任务，请刷新重试",
                    }[status]})
    return out
