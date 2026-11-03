"""Strict adapters for the official class-test answer wire format."""
from __future__ import annotations

from copy import deepcopy
import random
import re

try:
    from .task_safety import SafetyError
except ImportError:  # direct script entry point
    from task_safety import SafetyError


TEST_LIMITS = {
    15: 10, 17: 10, 18: 10,
    16: 15, 21: 15, 22: 15,
    11: 20, 32: 20,
    31: 25, 41: 25, 42: 25, 51: 25, 52: 25,
    43: 30, 44: 30,
    13: 35, 53: 35, 54: 35, 73: 35,
}
INPUT_MODES = {51, 52, 53, 54, 73}
_MEDIA_FIELD = re.compile(r"image|audio|img|sound|voice|picture|^ph_(?:us|en)_url$", re.I)


def _integer(value):
    if type(value) is int:
        return value
    if isinstance(value, str) and re.fullmatch(r"\d+", value):
        return int(value)
    return None


def base_mode(value):
    raw = _integer(value)
    if raw is None:
        raise SafetyError("测试题型编号无效，已停止推进")
    mode = int(str(raw)[:2]) if len(str(raw)) > 2 else raw
    if mode not in TEST_LIMITS:
        raise SafetyError("服务器返回未支持的测试题型，已暂停核对")
    return mode


def _media_payload(value):
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return any(_media_payload(item) or (isinstance(key, str) and key.lower().endswith("id")
                                           and type(item) is int and item > 0)
                   for key, item in value.items())
    if isinstance(value, list):
        return any(_media_payload(item) for item in value)
    return False


def has_media(value):
    """Recognize actual media fields recursively, including parent options."""
    if isinstance(value, dict):
        return any((isinstance(key, str) and _MEDIA_FIELD.search(key)
                    and (_media_payload(item)
                         or type(item) is int and item > 0 and key.lower().endswith("id")))
                   or has_media(item) for key, item in value.items())
    if isinstance(value, list):
        return any(has_media(item) for item in value)
    return False


def media_snapshot(value):
    """Keep media identifiers and their paths without unrelated wrapper data.

    Unknown server containers may hold media inside dictionaries or arrays.
    Preserve array positions, and copy only recognized media leaves through
    those containers; arbitrary neighbouring fields must not enter the bank.
    """
    if isinstance(value, dict):
        found = {}
        for key, item in value.items():
            if (isinstance(key, str) and _MEDIA_FIELD.search(key)
                    and has_media({key: item})):
                found[key] = deepcopy(item)
            else:
                nested = media_snapshot(item)
                if nested is not None:
                    found[key] = nested
        return found or None
    if isinstance(value, list):
        found = [media_snapshot(item) for item in value]
        return found if any(item is not None for item in found) else None
    return None


def _tag(value):
    return type(value) in {int, str} and (type(value) is int and value >= 0
                                        or isinstance(value, str) and bool(value.strip()))


def _option(option):
    if not isinstance(option, dict) or not _tag(option.get("answer_tag")):
        raise SafetyError("测试选项缺少文字或真实 answer_tag，已暂停核对")
    content = option.get("content")
    if (content is not None and not isinstance(content, str)
            or (not content or not content.strip()) and not has_media(option)):
        raise SafetyError("测试选项缺少完整文字或可识别媒体，已暂停核对")


def _flatten_options(options):
    flat = []
    for parent in options:
        _option(parent)
        if "sub_options" in parent and parent["sub_options"] is not None:
            children = parent["sub_options"]
            parent_tag = re.fullmatch(r"#?(\d+)#?", str(parent["answer_tag"]))
            if not isinstance(children, list) or not children or parent_tag is None:
                raise SafetyError("测试父子选项结构或父标签无效")
            parent_index = int(parent_tag[1])
            for child in children:
                _option(child)
                if not re.fullmatch(r"\d+", str(child["answer_tag"])):
                    raise SafetyError("测试子选项标签不能映射为官方组合答案")
                option = deepcopy(child)
                option["content"] = option.get("content") or ""
                option["answer_tag"] = f"{parent_index}#{child['answer_tag']}"
                option["parent_content"] = parent.get("content") or ""
                flat.append(option)
        else:
            option = deepcopy(parent)
            option["content"] = option.get("content") or ""
            option.pop("sub_options", None)
            option["answer_tag"] = str(parent["answer_tag"]) + "0"
            flat.append(option)
    return flat


def _slots(stem, lengths):
    # The official sentence formatter splits each {...} by display whitespace.
    groups = re.findall(r"\{([^{}]*)\}", stem)
    if not groups or stem.count("{") != len(groups) or stem.count("}") != len(groups):
        raise SafetyError("测试多空题缺少完整的有序空格模板")
    hints = []
    for group in groups:
        parts = group.replace("*", "").replace("_", "").split()
        if not parts:
            raise SafetyError("测试多空题存在无法解析的空格")
        hints.extend(parts)
    if (not isinstance(lengths, list) or len(lengths) != len(hints)
            or any(type(length) is not int or length <= 0 for length in lengths)):
        raise SafetyError("测试多空题 w_lens 与空格数不一致")
    return len(hints)


def normalize_topic(topic):
    """Return an idempotent copy; never fabricate tags or discard raw modes."""
    if not isinstance(topic, dict) or not _tag(topic.get("topic_code")):
        raise SafetyError("服务器未返回完整测试题目凭据")
    if type(topic["topic_code"]) is int and topic["topic_code"] <= 0:
        raise SafetyError("测试题目凭据无效")
    result = deepcopy(topic)
    if ("test_protocol_version" in result
            and (type(result["test_protocol_version"]) is not int or result["test_protocol_version"] != 1)):
        raise SafetyError("测试题目适配版本无效")
    raw = _integer(result.get("topic_mode_raw", result.get("topic_mode")))
    mode = base_mode(raw)
    if "topic_mode_raw" in result and base_mode(result.get("topic_mode")) != mode:
        raise SafetyError("测试原始题型与基础题型不一致")
    if "task_type" in result and _integer(result["task_type"]) != 2:
        raise SafetyError("题目类型不属于当前测试")
    stem = result.get("stem")
    if not isinstance(stem, dict) or not isinstance(stem.get("content"), str):
        raise SafetyError("测试题目缺少有效题干")
    if not stem["content"].strip() and not has_media(result):
        raise SafetyError("测试题干为空，已暂停核对")
    options = result.get("raw_options", result.get("options"))
    if options is None and mode in INPUT_MODES:
        options = []
    if not isinstance(options, list):
        raise SafetyError("测试选项结构无效")
    if mode in INPUT_MODES:
        if options:
            raise SafetyError("测试输入题返回了意外选项结构")
    else:
        if not options:
            raise SafetyError("测试选择题没有完整选项")
        for option in options:
            _option(option)
    if mode in {43, 44}:
        result["raw_options"] = deepcopy(options)
        options = _flatten_options(options)
    else:
        options = [{**option, "content": option.get("content") or ""} for option in options]
    tags = [str(option["answer_tag"]) for option in options]
    if len(set(tags)) != len(tags):
        raise SafetyError("测试选项答案标识不唯一")
    if mode == 31:
        count = _integer(result.get("answer_num"))
        if count is None or not 1 <= count <= len(options):
            raise SafetyError("测试多选题答案数量无效")
        result["answer_num"] = count
    if mode == 73:
        result["slot_count"] = _slots(stem["content"], result.get("w_lens"))
    for field in ("topic_done_num", "topic_total"):
        if field in result and (_integer(result[field]) is None or _integer(result[field]) < 0):
            raise SafetyError("测试题目计数字段无效")
        if field in result:
            result[field] = _integer(result[field])
    if mode in INPUT_MODES:
        for field in ("w_len",):
            if field in result and (type(result[field]) is not int or result[field] <= 0):
                raise SafetyError("测试输入题长度限制无效")
    result.update(topic_mode=mode, topic_mode_raw=raw, options=options,
                  test_protocol_version=1,
                  answer_kind=("choices" if mode == 31 else "words" if mode == 32
                               else "fill_slots" if mode == 73 else "fill" if mode in INPUT_MODES
                               else "choice"))
    return result


def save_time_ms(mode, elapsed_ms):
    """Official type-2 order: integer, minimum, cap, then odd milliseconds."""
    limit = TEST_LIMITS[base_mode(mode)] * 1000
    spent = int(elapsed_ms)
    if spent < 100:
        spent = 100 + random.randrange(10)
    spent = min(spent, limit)
    return spent + 1 if spent % 2 == 0 else spent
