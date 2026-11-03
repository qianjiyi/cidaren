"""Single-owner class-test runner; workers can only suggest answers."""
from __future__ import annotations

from copy import deepcopy
import queue
import threading
import time

try:
    from .bank_store import BankError, default_store, encode_answer, tag_for_option
    from .task_safety import SafetyError
    from .test_protocol import TEST_LIMITS, has_media, normalize_topic, save_time_ms
    from .network import close_thread_sessions
except ImportError:  # direct script entry point
    from bank_store import BankError, default_store, encode_answer, tag_for_option
    from task_safety import SafetyError
    from test_protocol import TEST_LIMITS, has_media, normalize_topic, save_time_ms
    from network import close_thread_sessions


END_CODES = {20004, 20006}
_SUGGESTIONS = None
_SUGGESTIONS_LOCK = threading.Lock()


class _SuggestionWorker:
    """At most one model request and one queued suggestion per task process."""
    def __init__(self):
        self.jobs = queue.Queue(maxsize=1)
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self._run, name="test-answer-candidate", daemon=True)
        self.thread.start()

    def _run(self):
        try:
            while not self.stopping.is_set():
                try:
                    work = self.jobs.get(timeout=.5)
                except queue.Empty:
                    continue
                try:
                    if not self.stopping.is_set():
                        work()
                finally:
                    self.jobs.task_done()
        finally:
            close_thread_sessions()

    def submit(self, work):
        try:
            self.jobs.put_nowait(work)
            return True
        except queue.Full:
            return False


def _suggest(work):
    global _SUGGESTIONS
    with _SUGGESTIONS_LOCK:
        if _SUGGESTIONS is None or _SUGGESTIONS.stopping.is_set():
            _SUGGESTIONS = _SuggestionWorker()
        return _SUGGESTIONS.submit(work)


def close_suggestion_worker():
    with _SUGGESTIONS_LOCK:
        if _SUGGESTIONS is not None:
            _SUGGESTIONS.stopping.set()


def _quiz_module():
    if __package__ in (None, ""):
        import a
    else:
        from . import a
    return a


def _valid(topic, answer):
    if topic["topic_mode"] == 31 and not isinstance(answer, list):
        return False
    if topic["topic_mode"] != 31 and topic["topic_mode"] != 73 and isinstance(answer, list):
        return False
    try:
        encode_answer(topic, answer)
        return True
    except BankError:
        return False


def _candidate(topic, answer):
    if topic["topic_mode"] == 31 and answer is not None and not isinstance(answer, list):
        answer = [answer]
    return answer if _valid(topic, answer) else None


def _select(bank, topic, deadline, quiz):
    """All bank activity belongs to the owner, never the answer thread."""
    hit = bank.lookup(topic)
    if hit.answer is not None:
        answer = _candidate(topic, hit.answer)
        if answer is not None and (not has_media(topic) or hit.source in {"精确题库", "临时缓存"}):
            return answer, hit.source
    print(f"    [词库未命中] {hit.reason or '已有答案不适用于当前测试题型'}")
    if has_media(topic):
        print("    [测试跳题] 媒体题无已验证精确答案，等待官方时限")
        return None, "媒体题无精确答案"
    if time.monotonic() >= deadline:
        return None, "答题时间已到"
    answer = _candidate(topic, quiz._match_answer(topic, {}))
    if answer is not None and not bank.is_rejected(topic, answer):
        return answer, "规则"
    print("    [LLM回退] 词库未命中，规则未提供可用答案；等待受测试剩余时间限制")
    results = queue.Queue(maxsize=1)
    epoch = object()

    def suggest():
        try:
            answer = quiz._llm_answer(deepcopy(topic), {}, deadline=deadline) if time.monotonic() < deadline else None
        except Exception:
            # Network/model messages may contain private URLs or credentials.
            answer = None
        results.put((epoch, answer))

    if not _suggest(suggest):
        return None, "模型仍在处理上一题，未增加并发请求"
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None, "答题时间已到"
    try:
        result_epoch, answer = results.get(timeout=remaining)
    except queue.Empty:
        return None, "LLM超时"
    if result_epoch is not epoch or time.monotonic() >= deadline:
        return None, "LLM结果迟到"
    answer = _candidate(topic, answer)
    if answer is not None and not bank.is_rejected(topic, answer):
        return answer, "LLM"
    return None, "没有有效答案"


def _corrections(topic, data):
    raw = data.get("answer_corrects")
    if not isinstance(raw, (str, list)) or not raw:
        return []
    mode, options = topic["topic_mode"], topic["options"]
    if mode == 73:
        candidates = [raw] if isinstance(raw, list) else []
    elif mode == 32:
        if isinstance(raw, str):
            candidates = [raw]
        elif all(isinstance(item, str) for item in raw):
            candidates = [",".join(raw)]
        elif all(type(item) is int for item in raw):
            words = []
            for item in raw:
                matches = [option["content"] for index, option in enumerate(options)
                           if str(tag_for_option(option, index)) == str(item)]
                if len(matches) != 1:
                    return []
                words.extend(matches)
            candidates = [",".join(words)]
        else:
            return []
    elif options:
        if not isinstance(raw, list):
            return []
        tags = []
        for item in raw:
            if type(item) not in {int, str}:
                return []
            matches = [tag_for_option(option, index) for index, option in enumerate(options)
                       if str(tag_for_option(option, index)) == str(item)]
            if len(matches) != 1 or any(str(tag) == str(matches[0]) for tag in tags):
                return []
            tags.extend(matches)
        candidates = [tags] if mode == 31 else tags
    else:
        candidates = [raw] if isinstance(raw, str) else raw
    return [answer for answer in candidates if _valid(topic, answer)]


def _wait_until(deadline):
    remaining = deadline - time.monotonic()
    if remaining > 0:
        time.sleep(remaining)


def _question_clock(client, topic):
    limit = TEST_LIMITS[topic["topic_mode"]] * 1000
    clock = client._task_context.test_clock(topic["topic_code"], topic.get("topic_done_num"), limit)
    elapsed = clock["elapsed_ms"]
    started = time.monotonic()
    return limit, elapsed, started, started + max(0, limit - elapsed) / 1000


def _verify(client, bank, topic, answer, source, deadline, quiz, *, clock=None):
    multiple = topic["topic_mode"] == 31
    options = topic["options"]
    selected, verified = [], []
    candidates = list(answer) if multiple else [answer]
    bundle = answer
    budget = 2 * max(1, len(options))
    attempts = 0
    if clock is None:
        _, base_elapsed, started, _ = _question_clock(client, topic)
    else:
        base_elapsed, started = clock
    while candidates and attempts < budget:
        if multiple and time.monotonic() >= deadline:
            return None
        chosen = candidates.pop(0)
        if multiple and any(str(chosen) == str(previous) for previous in selected):
            continue
        attempts += 1
        # Read persistent UTC only at question activation. A running question
        # uses a monotonic anchor, so wall-clock corrections cannot reset it.
        elapsed = base_elapsed + int(max(0, time.monotonic() - started) * 1000)
        # Ordinary countdown/automatic Skip stops at its first Verify, while
        # the official saved elapsed still includes subsequent corrections.
        response = client.verify(client._current_code, chosen)
        data = quiz._strict_verification(quiz._verification_data(response))
        correct = data["answer_result"] == 1
        if correct:
            if not any(str(chosen) == str(item) for item in verified):
                verified.append(chosen)
        elif _valid(topic, bundle):
            bank.reject(topic, bundle, detail={"test": True, "failed_tag": chosen} if multiple else {"test": True})
        selected.append(chosen)
        if data["over_status"] == 1:
            if multiple:
                if (correct and len(verified) == topic["answer_num"]
                        and data.get("clean_status") not in {0, 1}):
                    bank.record(topic, verified, source, "confirmed", complete=True,
                                detail={"fully_verified": True, "test": True})
                else:
                    print("    [词库暂不学习] 多选结束响应尚不能确认完整答案集合")
            elif correct:
                bank.record(topic, chosen, source, "confirmed", detail={"test": True})
            return elapsed, correct
        corrections = _corrections(topic, data)
        if multiple:
            tags = [tag for corrected in corrections for tag in corrected]
            if data.get("clean_status") == 2:
                candidates.extend(tag for tag in tags
                                  if not any(str(tag) == str(item) for item in [*selected, *candidates]))
            else:
                selected, verified = [], []
                candidates = list(tags)
            if corrections:
                bundle = corrections[0]
        else:
            candidates = corrections
            if candidates:
                bundle = candidates[0]
        if not candidates:
            if multiple:
                return None
            client.pause("测试验证尚未结束且没有可映射纠错答案，已暂停核对")
    client.pause("测试本题验证达到有限次数上限，已暂停核对")


def _run(client, task_id, release_id, bank, quiz):
    response = client.test_start_answer(task_id, release_id)
    if quiz._response_code(response) in END_CODES:
        print("🎉 本次测试答题已结束；不会自动重新开始")
        return
    quiz._task_data(response, "开始测试")
    while True:
        topic = client._active_topic
        if topic is None:
            client.pause("测试响应未激活完整题目，已暂停核对")
        # A separate normalized copy verifies idempotence while all operations
        # continue using the exact object guarded by Client.
        if normalize_topic(topic) != topic:
            client.pause("测试题目正规化状态不一致，已暂停核对")
        _, elapsed, started, deadline = _question_clock(client, topic)
        answer, source = _select(bank, topic, deadline, quiz)
        result = None
        if answer is not None and time.monotonic() < deadline:
            result = _verify(client, bank, topic, answer, source, deadline, quiz,
                             clock=(elapsed, started))
        done, total = topic.get("topic_done_num", "?"), topic.get("topic_total", "?")
        if result is None:
            _wait_until(deadline)
            response = client.test_skip(client._current_code, TEST_LIMITS[topic["topic_mode"]] * 1000)
            print(f"  [{done}/{total}] ⏭ 测试题按官方时限跳过 [{source}]")
        else:
            elapsed, correct = result
            response = client.submit(client._current_code, save_time_ms(topic["topic_mode"], elapsed))
            print(f"  [{done}/{total}] {'✅' if correct else '⚠️'} 测试题已保存 [{source}]")
        if quiz._response_code(response) in END_CODES:
            print("🎉 本次测试答题已结束；不会自动重新开始")
            return
        quiz._task_data(response, "推进测试")


def run_test(client, task_id, release_id, bank=None):
    quiz = _quiz_module()
    if not isinstance(client, quiz.Client):
        raise SafetyError("测试执行器需要可核验任务归属的 Client")
    with client.task_scope("class", task_id=task_id, release_id=release_id, task_type=2):
        if client._task_metadata.get("task_type") not in (2, "2"):
            raise SafetyError("当前执行上下文不是测试任务")
        bank = bank or default_store()
        with bank.runtime():
            return _run(client, task_id, release_id, bank, quiz)
