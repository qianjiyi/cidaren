"""Local task leases and durable pauses for requests whose result is uncertain.

This state is independent of the lexicon and contains no credentials, answers or
raw topic codes.  A pending write survives crashes; it is never replayed here.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import uuid


SCHEMA_VERSION = 1
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_MAX_STATE_BYTES = 4 * 1024 * 1024
_MAX_ALIASES = 100_000


class SafetyError(RuntimeError):
    """An invalid or unavailable local safety record must stop execution."""

    code = 3

    def __init__(self, reason):
        self.reason = str(reason)
        super().__init__(self.reason)


class SafetyPaused(SafetyError):
    """An operation may already have changed the remote task."""

    def __init__(self, reason, *, recovery_id=None, state=None):
        super().__init__(reason)
        self.recovery_id = recovery_id
        self.state = state or {}


PausedError = SafetyPaused


def _identifier(value, label):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise SafetyError(f"{label}无效，任务未启动")
    text = str(value).strip()
    if not text or len(text) > 256:
        raise SafetyError(f"{label}无效，任务未启动")
    if re.fullmatch(r"[+-]?\d+", text):
        number = int(text)
        if number <= 0:
            raise SafetyError(f"{label}无效，任务未启动")
        text = str(number)
    return text


def account_key(user_info):
    """Hash the stable server identity; changing a Token keeps the same key."""
    if not isinstance(user_info, dict):
        raise SafetyError("服务器未提供稳定账号编号，任务未启动")
    value = user_info.get("id")
    if (isinstance(value, bool) or not isinstance(value, (int, str))
            or (isinstance(value, str) and (len(value) > 256 or not re.fullmatch(r"[0-9]+", value.strip())))):
        raise SafetyError("服务器未提供稳定账号编号，任务未启动")
    identity = str(int(value))
    if int(identity) <= 0:
        raise SafetyError("服务器未提供稳定账号编号，任务未启动")
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _stamp():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _uuid(value):
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _topic_hash(code):
    if isinstance(code, bool) or not isinstance(code, (str, int)) or not str(code).strip():
        raise SafetyError("题码格式异常，任务已停止")
    return hashlib.sha256(str(code).encode("utf-8")).hexdigest()


@contextmanager
def _file_lock(path):
    """Hold one byte for the entire round; the OS releases it on process death."""
    handle = None
    locked = False
    try:
        handle = path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        locked = True
    except OSError:
        if handle is not None:
            handle.close()
        raise SafetyError("任务正在另一进程运行，或运行记录无法锁定；未发送请求") from None
    try:
        yield
    finally:
        if locked:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()


class SafetyStore:
    """One atomically replaced JSON record per account and stable task key."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.path = self.root / "data" / "task_runtime"

    @staticmethod
    def account_key(user_info):
        return account_key(user_info)

    def _key(self, account, source, release_id=None, course_id=None, list_id=None):
        if not isinstance(account, str) or not _HASH.fullmatch(account):
            raise SafetyError("账号标识无效，任务未启动")
        if source == "class":
            return {"account_key": account, "source": source,
                    "release_id": _identifier(release_id, "班级任务编号")}
        if source == "study":
            return {"account_key": account, "source": source,
                    "course_id": _identifier(course_id, "课程编号"),
                    "list_id": _identifier(list_id if list_id is not None else release_id, "词表编号")}
        raise SafetyError("任务来源未确认，任务未启动")

    def _paths(self, key):
        identity = hashlib.sha256(json.dumps(key, sort_keys=True, ensure_ascii=False,
                                             separators=(",", ":")).encode("utf-8")).hexdigest()
        return self.path / f"{identity}.json", self.path / f"{identity}.lock"

    @staticmethod
    def _empty(key):
        return {"schema_version": SCHEMA_VERSION, "task": key, "round": 0,
                "round_id": None, "aliases": [], "status": "clean", "recovery": None,
                "updated_at": None}

    def _load(self, path, key):
        try:
            with path.open("rb") as handle:
                raw = handle.read(_MAX_STATE_BYTES + 1)
        except FileNotFoundError:
            return self._empty(key)
        except OSError:
            raise SafetyError("无法读取运行安全记录，任务未启动") from None
        try:
            if len(raw) > _MAX_STATE_BYTES:
                raise ValueError
            state = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
            if (not isinstance(state, dict) or type(state.get("schema_version")) is not int
                    or state["schema_version"] != SCHEMA_VERSION or state.get("task") != key
                    or type(state.get("round")) is not int or state["round"] < 0
                    or (state.get("round_id") is not None and not _uuid(state["round_id"]))
                    or not isinstance(state.get("updated_at"), str)
                    or state.get("status") not in {"clean", "pending", "paused"}):
                raise ValueError
            aliases = state.get("aliases")
            if (not isinstance(aliases, list) or len(aliases) > _MAX_ALIASES
                    or any(not isinstance(item, str) or not _HASH.fullmatch(item) for item in aliases)
                    or len(set(aliases)) != len(aliases)):
                raise ValueError
            recovery = state.get("recovery")
            if state["status"] == "clean":
                if recovery is not None:
                    raise ValueError
            elif (not isinstance(recovery, dict) or not _uuid(recovery.get("recovery_id"))
                  or not isinstance(recovery.get("reason"), str) or not recovery["reason"]
                  or not isinstance(recovery.get("action"), str)
                  or not isinstance(recovery.get("started_at"), str)
                  or (recovery.get("topic_hash") is not None
                      and (not isinstance(recovery["topic_hash"], str)
                           or not _HASH.fullmatch(recovery["topic_hash"])) )):
                raise ValueError
            return state
        except (ValueError, TypeError, UnicodeError, KeyError):
            raise SafetyError("运行安全记录损坏或版本不兼容，任务已停止；请先核对并处理记录") from None

    def _save(self, path, state):
        temporary = None
        try:
            self.path.mkdir(parents=True, exist_ok=True)
            raw = json.dumps(state, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode("utf-8")
            if len(raw) > _MAX_STATE_BYTES:
                raise SafetyError("运行安全记录超出大小限制，任务已停止")
            with tempfile.NamedTemporaryFile(dir=self.path, prefix=".state-", suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            temporary = None
            if os.name != "nt":
                descriptor = os.open(self.path, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        except OSError:
            raise SafetyError("无法保存运行安全记录，任务已停止；未确认的请求不会重发") from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    @staticmethod
    def _public(state):
        recovery = state.get("recovery") or {}
        return {"paused": state["status"] != "clean", "pending": state["status"] == "pending",
                "recovery_id": recovery.get("recovery_id"), "reason": recovery.get("reason"),
                "action": recovery.get("action"), "updated_at": state.get("updated_at")}

    @staticmethod
    def _check_clean(state):
        if state["status"] != "clean":
            public = SafetyStore._public(state)
            raise SafetyPaused(public["reason"], recovery_id=public["recovery_id"], state=public)

    def inspect(self, account_key, source, release_id=None, course_id=None, list_id=None):
        key = self._key(account_key, source, release_id, course_id, list_id)
        path, _ = self._paths(key)
        return self._public(self._load(path, key))

    @contextmanager
    def scope(self, account_key, source, release_id=None, course_id=None, list_id=None):
        key = self._key(account_key, source, release_id, course_id, list_id)
        path, lock_path = self._paths(key)
        try:
            self.path.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise SafetyError("无法建立运行安全记录目录，任务未启动") from None
        with _file_lock(lock_path):
            state = self._load(path, key)
            self._check_clean(state)
            context = TaskContext(self, path, key, state)
            try:
                yield context
            finally:
                context._active = False

    def ack(self, account_key, source, release_id=None, course_id=None, list_id=None,
            *, expected_recovery_id, validate=None):
        """Clear only the pause already inspected and explicitly acknowledged.

        User confirmation is supplied by the caller. The optional validate callback
        runs under the same OS lock, allowing a read-only remote refresh to check
        account and task identity without a race. No write is replayed here.
        """
        if not _uuid(expected_recovery_id):
            raise SafetyError("恢复标识无效，请重新刷新任务状态")
        key = self._key(account_key, source, release_id, course_id, list_id)
        path, lock_path = self._paths(key)
        try:
            self.path.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise SafetyError("无法建立运行安全记录目录，恢复已取消") from None
        with _file_lock(lock_path):
            state = self._load(path, key)
            recovery = state.get("recovery") or {}
            if state["status"] == "clean" or recovery.get("recovery_id") != expected_recovery_id:
                raise SafetyError("暂停状态已经变化，请刷新后重新确认；没有清除新状态")
            if validate is not None:
                if validate() is False:
                    raise SafetyError("只读核对未通过，暂停记录已保留")
                state = self._load(path, key)
                recovery = state.get("recovery") or {}
                if state["status"] == "clean" or recovery.get("recovery_id") != expected_recovery_id:
                    raise SafetyError("核对期间暂停状态已经变化，没有清除新状态")
            # Recovery resumes the same round. Only a newly confirmed selection
            # may start a new round and clear its already processed topic aliases.
            state.update(status="clean", recovery=None, updated_at=_stamp())
            self._save(path, state)
            return self._public(state)


class TaskContext:
    """A task scope held under an OS lock; never construct it directly."""

    def __init__(self, store, path, key, state):
        self.store = store
        self.path = path
        self.key = key.copy()
        self.account_key = key["account_key"]
        self.source = key["source"]
        self._state = state
        self._active = True
        self._inflight_id = None

    def _check_active(self):
        if not self._active:
            raise SafetyError("任务上下文已关闭，未发送请求")

    def _refresh(self):
        self._check_active()
        self._state = self.store._load(self.path, self.key)
        return self._state

    def _commit(self):
        self._state["updated_at"] = _stamp()
        self.store._save(self.path, self._state)

    @property
    def round_id(self):
        return self._refresh()["round_id"]

    @property
    def round_number(self):
        return self._refresh()["round"]

    def begin_round(self):
        state = self._refresh()
        self.store._check_clean(state)
        state.update(round=state["round"] + 1, round_id=str(uuid.uuid4()), aliases=[])
        self._commit()
        return state["round_id"]

    def is_seen(self, code):
        if code is None:
            return False
        return _topic_hash(code) in self._refresh()["aliases"]

    def register_aliases(self, *codes):
        state = self._refresh()
        self.store._check_clean(state)
        if len(codes) == 1 and isinstance(codes[0], (list, tuple, set)):
            codes = tuple(codes[0])
        aliases = set(state["aliases"])
        aliases.update(_topic_hash(code) for code in codes if code is not None)
        if len(aliases) > _MAX_ALIASES:
            self.pause("本轮题码数量异常，任务已暂停，未继续提交")
        state["aliases"] = sorted(aliases)
        self._commit()

    def before_write(self, action, topic_code=None):
        state = self._refresh()
        self.store._check_clean(state)
        if not isinstance(action, str) or not action.strip() or len(action) > 160:
            raise SafetyError("请求操作名称无效，未发送请求")
        action = " ".join(action.split())
        topic_hash = _topic_hash(topic_code) if topic_code is not None else None
        if topic_hash is not None:
            # A Verify/Save request may be accepted before its response is lost.
            # Persist its attempted credential together with pending; merely
            # receiving the next topic never adds it to this processed set.
            hashes = set(state["aliases"])
            hashes.add(topic_hash)
            if len(hashes) > _MAX_ALIASES:
                self.pause("本轮题码数量异常，任务已暂停，未继续提交")
            state["aliases"] = sorted(hashes)
        recovery_id = str(uuid.uuid4())
        state.update(status="pending", recovery={"recovery_id": recovery_id, "action": action,
                     "topic_hash": topic_hash,
                     "reason": f"{action}结果待确认；请在词达人核对后重新同步", "started_at": _stamp()})
        self._commit()
        self._inflight_id = recovery_id
        return recovery_id

    def confirm_write(self, expected_recovery_id=None, *, aliases=None, begin_round=False):
        state = self._refresh()
        expected = expected_recovery_id or self._inflight_id
        recovery = state.get("recovery") or {}
        if (state["status"] != "pending" or not _uuid(expected)
                or recovery.get("recovery_id") != expected or expected != self._inflight_id):
            raise SafetyError("请求确认状态已经变化，未清除暂停记录")
        if type(begin_round) is not bool:
            raise SafetyError("新轮确认状态无效，未清除暂停记录")
        if begin_round:
            state.update(round=state["round"] + 1, round_id=str(uuid.uuid4()), aliases=[])
        codes = [] if aliases is None else aliases
        if isinstance(codes, (str, int)):
            codes = [codes]
        if not isinstance(codes, (list, tuple, set)):
            raise SafetyError("确认题码格式异常，未清除暂停记录")
        hashes = set(state["aliases"])
        hashes.update(_topic_hash(code) for code in codes if code is not None)
        if len(hashes) > _MAX_ALIASES:
            self.pause("本轮题码数量异常，任务已暂停，未继续提交")
        state["aliases"] = sorted(hashes)
        state.update(status="clean", recovery=None)
        self._commit()
        self._inflight_id = None

    def pause(self, reason):
        state = self._refresh()
        reason = " ".join(str(reason).split())[:500] or "任务状态异常，已暂停"
        # Each newly discovered protocol problem has a fresh recovery identity;
        # a confirmation for the preceding pending request must not clear it.
        recovery = state.get("recovery") or {"recovery_id": str(uuid.uuid4()), "action": "protocol",
                                            "topic_hash": None, "started_at": _stamp()}
        recovery["recovery_id"] = str(uuid.uuid4())
        recovery["reason"] = reason
        state.update(status="paused", recovery=recovery)
        self._commit()
        public = self.store._public(state)
        raise SafetyPaused(reason, recovery_id=public["recovery_id"], state=public)

    def execute(self, action, payload, callback, validator, *, confirm_aliases=None, begin_round=False):
        """Make exactly one state-changing attempt and acknowledge its response."""
        topic_code = payload.get("topic_code") if isinstance(payload, dict) else None
        recovery_id = self.before_write(action, topic_code)
        try:
            response = callback()
        except SafetyPaused:
            raise
        except Exception:
            public = self.store._public(self._refresh())
            raise SafetyPaused(public["reason"], recovery_id=public["recovery_id"], state=public) from None
        try:
            if validator(response) is False:
                self.pause(f"{action}返回无法确认；请在词达人核对后重新同步")
            aliases = confirm_aliases(response) if callable(confirm_aliases) else confirm_aliases
            new_round = begin_round(response) if callable(begin_round) else begin_round
        except SafetyPaused:
            raise
        except Exception as exc:
            if getattr(exc, "definitive_response", False) is True:
                self.confirm_write(recovery_id)
                raise
            public = self.store._public(self._refresh())
            raise SafetyPaused(public["reason"], recovery_id=public["recovery_id"], state=public) from None
        # Commit the response and its topic aliases/round identity together. A
        # crash can leave a pause, never a clean record missing accepted aliases.
        self.confirm_write(recovery_id, aliases=aliases, begin_round=new_round)
        return response
