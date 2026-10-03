"""Portable, transactional question cache and curated vocabulary store.

Only this module interprets saved answers. API answer tags are deliberately
separate from option positions, and rejected applications survive cache cleanup.
"""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import tempfile
import time
import unicodedata
import uuid

ROOT = Path(__file__).resolve().parent.parent
LEGACY_FILE = Path(__file__).with_name("bank.json")
SCHEMA_VERSION = 1
EXPORT_VERSION = 2
TABLES = ("metadata", "legacy", "records", "knowledge", "rejections", "observations")
TABLE_ORDER = {'metadata':'key', 'legacy':'key', 'records':'id', 'knowledge':'id',
               'rejections':'scope_key,answer_json', 'observations':'id'}
KNOWN_MODES = {0, 11, 15, 17, 21, 22, 31, 32, 41, 51, 52}


class BankError(RuntimeError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def norm(text):
    # Keep word boundaries, negation, apostrophes, morphology and part of speech.
    text = unicodedata.normalize("NFC", str(text or ""))
    text = text.translate(str.maketrans({"，": ",", "；": ";", "：": ":", "…": "..."}))
    return re.sub(r"\s+", " ", text).strip().casefold()


def normalized(value):
    if isinstance(value, str):
        return norm(value)
    if isinstance(value, list):
        return [normalized(x) for x in value]
    if isinstance(value, dict):
        return {k: normalized(v) for k, v in sorted(value.items())}
    return value


def is_collocation(topic):
    remark = (topic.get("stem") or {}).get("remark")
    return topic.get("topic_mode") == 31 and isinstance(remark, list) and bool(remark) and isinstance(remark[0], dict)


def snapshot(topic):
    # No session credentials or transient topic/task identifiers are persisted.
    out = {k: topic[k] for k in ("topic_mode", "stem", "options", "answer_num") if k in topic}
    for k, v in topic.items():
        if any(token in k.lower() for token in ("image", "audio", "img", "sound", "voice")):
            out[k] = v
    out.setdefault("stem", {})
    out.setdefault("options", [])
    return json.loads(dump(out))


def canonical_topic(topic, *, reorder=False):
    out = snapshot(topic)
    stem = out['stem']
    for field in ('content', 'remark'):
        if field in stem:
            stem[field] = normalized(stem[field])
    for opt in out['options']:
        if 'content' in opt:
            opt['content'] = norm(opt['content'])
        if reorder:
            opt.pop('answer_tag', None)
    if reorder:
        out['options'].sort(key=dump)
    # Media identifiers and other non-display fields stay case sensitive.
    return out


def exact_key(topic):
    return hashlib.sha256(dump(canonical_topic(topic)).encode()).hexdigest()


def scope_key(topic):
    return hashlib.sha256(dump(canonical_topic(topic, reorder=True)).encode()).hexdigest()


def _has_media(topic):
    return any(re.search(r"image|audio|img|sound|voice", k, re.I) and v for k, v in topic.items()) or any(
        re.search(r"image|audio|img|sound|voice", k, re.I) and v
        for obj in [topic.get("stem") or {}, *(topic.get("options") or [])]
        for k, v in obj.items()
    )


def semantic_key(topic):
    mode = topic.get("topic_mode")
    stem_obj = topic.get("stem") or {}
    stem, remark = stem_obj.get("content", ""), stem_obj.get("remark", "")
    if mode not in KNOWN_MODES or _has_media(topic):
        return None
    if mode in (0, 15, 21, 22) and not remark and re.fullmatch(r"[A-Za-z][A-Za-z' -]*", stem.strip()):
        return dump(["definition", norm(stem)])
    if mode == 17 and not remark:
        return dump(["reverse_definition", norm(stem)])
    if mode == 32:
        # Blank count and literal words remain part of the template.
        if not isinstance(remark, str) or not remark.strip():
            return None
        return dump(["phrase", norm(stem), norm(remark)])
    if mode in (51, 52) and not remark:
        return None
    if mode == 0:
        return None
    return dump([mode, "collocation" if is_collocation(topic) else "context", norm(stem), normalized(remark),
                 topic.get('answer_num') if is_collocation(topic) else None])


def tag_for_option(option, index):
    return option.get("answer_tag", index)


def _same_tag(left, right):
    if type(left) not in (int, str) or type(right) not in (int, str):
        return False
    return type(left) == type(right) and left == right or (
        isinstance(left, (int, str)) and isinstance(right, (int, str)) and str(left) == str(right)
    )


def encode_answer(topic, answer):
    opts = topic.get("options") or []
    if topic.get("topic_mode") == 32:
        if not isinstance(answer, str):
            raise BankError("组词答案必须是有序文字序列")
        words = [x.strip() for x in answer.replace("，", ",").split(",")]
        if not words or not all(words):
            raise BankError("组词答案为空")
        blanks = len(re.findall(r'(?<!\w)_+(?!\w)|\{\}', (topic.get('stem') or {}).get('content','')))
        if blanks and len(words) != blanks:
            raise BankError('组词答案词数与题目空格数不一致')
        available = Counter(norm(o.get("content")) for o in opts)
        if Counter(norm(x) for x in words) - available:
            raise BankError("组词答案不在当前选项中或重复次数不合法")
        return {"kind": "words", "items": words}
    if opts:
        raw = answer if isinstance(answer, list) else [answer]
        texts = []
        for item in raw:
            matches = [o for i, o in enumerate(opts) if _same_tag(item, tag_for_option(o, i))]
            if len(matches) != 1 or not matches[0].get("content"):
                raise BankError("答案无法唯一映射到 answer_tag")
            texts.append(matches[0]["content"])
        if not texts or len(set(norm(x) for x in texts)) != len(texts):
            raise BankError("答案文字为空或有歧义")
        if isinstance(answer, list):
            return {"kind": "choices", "items": sorted(texts, key=norm)}
        return {"kind": "choice", "text": texts[0]}
    if isinstance(answer, str) and answer.strip():
        return {"kind": "fill", "text": answer.strip()}
    raise BankError("填空答案必须是非空文字")


def map_answer(topic, saved):
    opts = topic.get("options") or []
    kind = saved.get("kind")
    if kind == "words":
        if topic.get('topic_mode') != 32:
            return None
        items = saved.get("items") or []
        blanks = len(re.findall(r'(?<!\w)_+(?!\w)|\{\}', (topic.get('stem') or {}).get('content','')))
        if blanks and len(items) != blanks:
            return None
        available = Counter(norm(o.get("content")) for o in opts)
        if not items or Counter(norm(x) for x in items) - available:
            return None
        # Use the new task's original option spelling.
        words = []
        for item in items:
            matches = {o.get("content") for o in opts if norm(o.get("content")) == norm(item)}
            if len(matches) != 1:
                return None
            words.append(matches.pop())
        return ",".join(words)
    if kind == "fill":
        return saved.get("text") if not opts and saved.get("text") else None
    if kind in ("choice", "choices"):
        texts = [saved.get("text")] if kind == "choice" else saved.get("items") or []
        tags = []
        for text in texts:
            matches = [(i, o) for i, o in enumerate(opts) if norm(o.get("content")) == norm(text)]
            if len(matches) != 1:
                return None
            i, opt = matches[0]
            tags.append(tag_for_option(opt, i))
        if len({dump(tag) for tag in tags}) != len(tags):
            return None
        expected = topic.get('answer_num')
        if kind == 'choices' and isinstance(expected, int) and len(tags) != expected:
            return None
        if kind == 'choice' and is_collocation(topic):
            return None
        return tags[0] if kind == "choice" and tags else tags or None
    return None


def _validate_saved(saved):
    if not isinstance(saved, dict):
        raise ValueError('答案不是结构化记录')
    kind = saved.get('kind')
    if kind in ('choice','fill'):
        if not isinstance(saved.get('text'),str) or not saved['text'].strip():
            raise ValueError('答案文字缺失')
    elif kind in ('choices','words'):
        items = saved.get('items')
        if not isinstance(items,list) or not items or any(not isinstance(x,str) or not x.strip() for x in items):
            raise ValueError('答案序列缺失或格式错误')
    else:
        raise ValueError('答案种类不合法')


def legacy_keys(topic):
    mode = topic.get("topic_mode", "?")
    stem = re.sub(r'\s+', ' ', (topic.get("stem") or {}).get("content", "")).strip().lower()
    remark = (topic.get("stem") or {}).get("remark", "") or ""
    if isinstance(remark, list):
        remark = json.dumps(remark, ensure_ascii=False, sort_keys=True)
    options = "|".join((o.get("content") or "")[:20] for o in (topic.get("options") or [])).lower()
    kind = "coll" if is_collocation(topic) else "norm"
    return [f"{mode}::{kind}::{stem}::{remark}::{options}", f"{mode}::{stem}::{remark}::{options}", f"{mode}::{stem}::{options}"]


def parse_legacy(key, value):
    if not isinstance(key, str) or not isinstance(value, dict) or 'ans' not in value:
        raise BankError('旧题库记录格式不合法')
    parts = key.split("::")
    if len(parts) == 5 and parts[1] in ("norm", "coll"):
        mode, kind, stem, remark, blob = parts
    elif len(parts) == 4:
        mode, stem, remark, blob = parts
        kind = "norm"
    elif len(parts) == 3:
        mode, stem, blob = parts
        kind, remark = "norm", ""
    else:
        raise BankError("无法识别的旧键格式")
    try:
        mode = int(mode)
    except ValueError as exc:
        raise BankError("旧题型不是数字") from exc
    if kind == "coll":
        try:
            remark = json.loads(remark)
        except ValueError as exc:
            raise BankError("旧搭配备注无法解析") from exc
    opts = [{"content": s, "answer_tag": i} for i, s in enumerate(blob.split("|"))] if blob else []
    original_stem = value.get('stem', stem)
    if not isinstance(original_stem, str):
        raise BankError('旧题干不是文字')
    topic = {"topic_mode": mode, "stem": {"content": original_stem, "remark": remark}, "options": opts}
    answer = value.get("ans")
    saved = encode_answer(topic, answer)
    if saved["kind"] in ("choice", "choices"):
        texts = [saved["text"]] if saved["kind"] == "choice" else saved["items"]
        if any(len(s) == 20 for s in texts):
            raise BankError("正确选项可能被截断，需重新验证")
    return topic, saved


@contextmanager
def _file_lock(path, *, wait=0):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        # Reading a locked byte on Windows raises PermissionError before we can
        # translate lock contention into the normal maintenance error.
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        deadline = time.monotonic() + wait
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if time.monotonic() < deadline:
                    time.sleep(.05)
                    continue
                raise BankError("词库维护或运行进程仍在使用此项目，请先停止相关进程") from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


@dataclass
class Match:
    answer: object = None
    source: str = ""
    reason: str = "词库没有兼容记录"


class BankStore:
    def __init__(self, path=None, *, backup_backend=None):
        self.path = Path(path or os.environ.get("CIDAREN_BANK_DB") or ROOT / "data" / "lexicon.sqlite3").resolve()
        self._backup_backend = backup_backend

    def _backend(self):
        if self._backup_backend is not None:
            return self._backup_backend
        from .git_backups import GitBackups
        return GitBackups()

    @contextmanager
    def operation(self):
        # Serialize maintenance commands without blocking task writes during
        # uploads. Runtime leases separately protect database replacement.
        with _file_lock(self.path.parent / '.bank-operation.lock'):
            yield

    @contextmanager
    def connection(self, *, create=False):
        if not create and not self.path.is_file():
            raise BankError("词库尚未迁移，请运行词库管理 migrate")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = None
        try:
            con = sqlite3.connect(self.path, timeout=10)
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA foreign_keys=ON")
            con.execute("PRAGMA busy_timeout=10000")
        except sqlite3.Error as exc:
            if con is not None:
                con.close()
            raise BankError(f"无法打开词库: {exc}") from exc
        try:
            if not create and con.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                raise BankError("词库格式版本不兼容")
            yield con
        except sqlite3.Error as exc:
            raise BankError(f"词库读写失败，原数据未被替换: {exc}") from exc
        finally:
            con.close()

    @contextmanager
    def transaction(self):
        with self.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                yield con
                con.commit()
            except BaseException:
                con.rollback()
                raise

    @contextmanager
    def maintenance(self, *, check_old_server=False):
        with _file_lock(self.path.parent / ".maintenance.lock"):
            if check_old_server:
                with socket.socket() as probe:
                    probe.settimeout(.3)
                    if probe.connect_ex(("127.0.0.1", 5001)) == 0:
                        raise BankError("维护前请关闭5001网页服务和任务")
            for path in (self.path.parent / "runtime").glob("*.lock"):
                with _file_lock(path):
                    pass  # Stale files are harmless; live holders make the lock fail.
            yield

    @contextmanager
    def runtime(self):
        path = self.path.parent / "runtime" / f"{os.getpid()}-{uuid.uuid4().hex}.lock"
        with _file_lock(self.path.parent / ".maintenance.lock", wait=5):
            lock = _file_lock(path)
            lock.__enter__()
        try:
            yield
        finally:
            lock.__exit__(None, None, None)
            path.unlink(missing_ok=True)

    def initialize(self):
        with self.connection(create=True) as con:
            version = con.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, SCHEMA_VERSION):
                raise BankError("不支持的词库版本")
            con.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS legacy (key TEXT PRIMARY KEY, value_json TEXT NOT NULL, issue TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, exact_key TEXT NOT NULL, semantic_key TEXT,
                    topic_json TEXT NOT NULL, answer_json TEXT NOT NULL, raw_answer_json TEXT NOT NULL,
                    stage TEXT NOT NULL CHECK(stage IN ('historical','cache','formal')),
                    verification TEXT NOT NULL CHECK(verification IN ('legacy','pending','confirmed','official')),
                    source TEXT NOT NULL, created_at TEXT NOT NULL, verified_at TEXT,
                    UNIQUE(exact_key,answer_json,stage));
                CREATE INDEX IF NOT EXISTS record_exact ON records(exact_key,stage);
                CREATE TABLE IF NOT EXISTS knowledge (
                    id INTEGER PRIMARY KEY, semantic_key TEXT NOT NULL, answer_json TEXT NOT NULL,
                    origin_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    UNIQUE(semantic_key,answer_json,origin_id));
                CREATE INDEX IF NOT EXISTS knowledge_key ON knowledge(semantic_key);
                CREATE TABLE IF NOT EXISTS rejections (
                    scope_key TEXT NOT NULL, answer_json TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY(scope_key,answer_json));
                CREATE TABLE IF NOT EXISTS observations (
                    id INTEGER PRIMARY KEY, record_id INTEGER REFERENCES records(id) ON DELETE SET NULL,
                    exact_key TEXT NOT NULL, outcome TEXT NOT NULL, detail_json TEXT NOT NULL, created_at TEXT NOT NULL);
                PRAGMA user_version=1;
                COMMIT;
            """)

    def _insert(self, con, topic, saved, raw, stage, verification, source):
        key, skey, stamp = exact_key(topic), semantic_key(topic), now()
        con.execute("""INSERT INTO records(exact_key,semantic_key,topic_json,answer_json,raw_answer_json,
            stage,verification,source,created_at,verified_at) VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(exact_key,answer_json,stage) DO UPDATE SET
            verification=CASE WHEN excluded.verification IN ('confirmed','official') THEN excluded.verification ELSE records.verification END,
            verified_at=COALESCE(excluded.verified_at,records.verified_at)
            """, (key, skey, dump(snapshot(topic)), dump(normalized(saved)), dump(raw), stage, verification, source, stamp,
                  stamp if verification in ("confirmed", "official") else None))
        return con.execute("SELECT id FROM records WHERE exact_key=? AND answer_json=? AND stage=?",
                           (key, dump(normalized(saved)), stage)).fetchone()[0]

    def _index(self, con, record_id):
        row = con.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if not row["semantic_key"]:
            return
        pairs = [(row["semantic_key"], row["answer_json"])]
        key, answer = json.loads(row["semantic_key"]), json.loads(row["answer_json"])
        topic = json.loads(row["topic_json"])
        if key[0] == "definition" and answer.get("kind") == "choice":
            pairs.append((dump(["reverse_definition", norm(answer["text"])]), dump({"kind": "choice", "text": norm((topic["stem"]).get("content"))})))
        elif key[0] == "reverse_definition" and answer.get("kind") == "choice":
            pairs.append((dump(["definition", norm(answer["text"])]), dump({"kind": "choice", "text": norm(topic["stem"].get("content"))})))
        con.executemany("INSERT OR IGNORE INTO knowledge(semantic_key,answer_json,origin_id) VALUES(?,?,?)",
                        [(k, a, record_id) for k, a in pairs])

    def migrate(self, source=LEGACY_FILE):
        # A completed migration never reimports a stale JSON file.
        if self.path.exists():
            with self.connection() as con:
                if con.execute("SELECT value FROM metadata WHERE key='migration' ").fetchone():
                    return {"already_migrated": True, **self.status()}
        with self.operation(), self.maintenance(check_old_server=True):
            # Recheck under the maintenance lock, including after a failed import.
            if self.path.exists():
                with self.connection() as con:
                    if con.execute("SELECT 1 FROM metadata WHERE key='migration'").fetchone():
                        return {'already_migrated': True, **self.status()}
            try:
                raw = Path(source).read_bytes()
            except OSError as exc:
                raise BankError(f'无法读取原题库: {exc}') from exc
            try:
                bank = json.loads(raw)
            except (ValueError, UnicodeError) as exc:
                raise BankError("原题库JSON损坏，迁移已取消") from exc
            if not isinstance(bank, dict) or any(not isinstance(v, dict) or 'ans' not in v for v in bank.values()):
                raise BankError("原题库记录格式不合法，迁移已取消")
            digest = hashlib.sha256(raw).hexdigest()
            from .git_backups import LEGACY
            backup = self._backend().publish({LEGACY:raw}, label='before-migration')
            self._import_legacy(raw, bank, digest)
            return {"already_migrated": False, "backup": backup, **self.status()}

    def _import_legacy(self, raw, bank=None, digest=None):
        """Import already backed-up input, also used for a legacy Git restore."""
        if bank is None:
            try:
                bank = json.loads(raw)
            except (ValueError, UnicodeError) as exc:
                raise BankError('原题库JSON损坏') from exc
        if not isinstance(bank, dict) or any(not isinstance(v, dict) or 'ans' not in v for v in bank.values()):
            raise BankError('原题库记录格式不合法')
        digest = digest or hashlib.sha256(raw).hexdigest()
        self.initialize()
        issues = Counter()
        with self.transaction() as con:
            for key, value in bank.items():
                issue = ""
                try:
                    topic, answer = parse_legacy(key, value)
                except BankError as exc:
                    issue = str(exc)
                    issues[issue] += 1
                else:
                    record_id = self._insert(con, topic, answer, value['ans'], 'historical', 'legacy', 'legacy')
                    self._index(con, record_id)
                con.execute("INSERT INTO legacy VALUES(?,?,?)", (key, dump(value), issue))
            con.execute("INSERT INTO metadata VALUES('migration',?)", (dump({"sha256": digest, "count": len(bank), "time": now(), "issues": dict(issues)}),))

    def validate(self):
        with self.connection() as con:
            result = con.execute("PRAGMA quick_check").fetchone()[0]
            if result != "ok" or con.execute("PRAGMA foreign_key_check").fetchone():
                raise BankError(f"词库完整性检查失败: {result}")
            marker = con.execute("SELECT value FROM metadata WHERE key='migration'").fetchone()
            if not marker:
                raise BankError("词库迁移未完成，请重新执行 migrate")
            try:
                metadata = json.loads(marker[0])
                if type(metadata['count']) is not int or metadata['count'] != con.execute('SELECT COUNT(*) FROM legacy').fetchone()[0]:
                    raise ValueError('迁移数量不一致')
                if not re.fullmatch(r'[0-9a-f]{64}',metadata['sha256']):
                    raise ValueError('原题库哈希不合法')
                for row in con.execute('SELECT topic_json,answer_json,raw_answer_json,exact_key FROM records'):
                    topic, answer = json.loads(row[0]), json.loads(row[1])
                    json.loads(row[2])
                    if not isinstance(topic, dict) or not isinstance(topic.get('stem'), dict) or not isinstance(topic.get('options'), list):
                        raise ValueError('题目格式不合法')
                    if not isinstance(topic['stem'].get('content'),str) or any(not isinstance(o,dict) or not isinstance(o.get('content'),str) for o in topic['options']):
                        raise ValueError('题干或选项文字损坏')
                    if row[3] != exact_key(topic):
                        raise ValueError('精确题目索引不一致')
                    _validate_saved(answer)
                for table, field in [('legacy','value_json'),('knowledge','answer_json'),('rejections','answer_json'),('observations','detail_json')]:
                    for row in con.execute(f'SELECT {field} FROM {table}'):
                        parsed = json.loads(row[0])
                        if table in ('knowledge','rejections'):
                            _validate_saved(parsed)
            except (ValueError, TypeError, KeyError) as exc:
                raise BankError(f'词库记录损坏: {exc}') from exc

    def status(self):
        with self.connection() as con:
            stages = dict(con.execute("SELECT stage,COUNT(*) FROM records GROUP BY stage").fetchall())
            return {"database": str(self.path), "legacy": con.execute("SELECT COUNT(*) FROM legacy").fetchone()[0],
                    "legacy_issues": con.execute("SELECT COUNT(*) FROM legacy WHERE issue<>''").fetchone()[0],
                    "legacy_candidates": con.execute("SELECT COUNT(*) FROM legacy WHERE issue=''").fetchone()[0],
                    "formal": stages.get("formal", 0), "historical": stages.get("historical", 0), "cache": stages.get("cache", 0),
                    "pending": con.execute("SELECT COUNT(*) FROM records WHERE stage='cache' AND verification='pending'").fetchone()[0],
                    "knowledge": con.execute("SELECT COUNT(DISTINCT semantic_key) FROM knowledge").fetchone()[0],
                    "formal_knowledge": con.execute("SELECT COUNT(DISTINCT k.semantic_key) FROM knowledge k JOIN records r ON r.id=k.origin_id WHERE r.stage='formal'").fetchone()[0],
                    "conflicts": con.execute("SELECT COUNT(*) FROM (SELECT semantic_key FROM knowledge GROUP BY semantic_key HAVING COUNT(DISTINCT answer_json)>1)").fetchone()[0],
                    "rejections": con.execute("SELECT COUNT(*) FROM rejections").fetchone()[0],
                    "migration": json.loads(con.execute("SELECT value FROM metadata WHERE key='migration'").fetchone()[0])}

    def record(self, topic, answer, source, verification="pending", *, complete=True, detail=None):
        if verification not in ("pending", "confirmed", "official"):
            raise BankError("未知验证状态")
        saved = encode_answer(topic, answer)
        expected = topic.get('answer_num')
        if saved['kind'] == 'choices' and (not complete or (isinstance(expected, int) and len(answer) != expected)):
            verification = 'pending'
        with self.transaction() as con:
            record_id = self._insert(con, topic, saved, answer, 'cache', verification, source)
            con.execute("INSERT INTO observations(record_id,exact_key,outcome,detail_json,created_at) VALUES(?,?,?,?,?)",
                        (record_id, exact_key(topic), verification, dump(detail or {}), now()))
            if verification in ('confirmed', 'official'):
                con.execute("DELETE FROM rejections WHERE scope_key=? AND answer_json=?", (scope_key(topic), dump(normalized(saved))))
            return record_id

    def record_definitions(self, topic):
        ids = []
        with self.transaction() as con:
            for i, opt in enumerate(topic.get('options') or []):
                if not isinstance(opt.get('content'), str) or not opt['content'].strip():
                    continue
                saved = {'kind':'choice','text':opt['content']}
                record_id = self._insert(con, topic, saved, tag_for_option(opt,i), 'cache', 'official', 'official_definitions')
                con.execute('INSERT INTO observations(record_id,exact_key,outcome,detail_json,created_at) VALUES(?,?,?,?,?)',
                            (record_id, exact_key(topic), 'official', dump({'definition_index':i}), now()))
                ids.append(record_id)
        return ids

    def legacy_issues(self):
        with self.connection() as con:
            return [dict(row) for row in con.execute("SELECT key,value_json,issue FROM legacy WHERE issue<>'' ORDER BY key")]

    def reject(self, topic, answer, *, detail=None):
        saved = encode_answer(topic, answer)
        with self.transaction() as con:
            con.execute("INSERT OR REPLACE INTO rejections VALUES(?,?,?)", (scope_key(topic), dump(normalized(saved)), now()))
            con.execute("INSERT INTO observations(exact_key,outcome,detail_json,created_at) VALUES(?,?,?,?)",
                        (exact_key(topic), 'rejected', dump(detail or {}), now()))

    def is_rejected(self, topic, answer):
        saved = encode_answer(topic, answer)
        with self.connection() as con:
            return bool(con.execute('SELECT 1 FROM rejections WHERE scope_key=? AND answer_json=?',
                                    (scope_key(topic), dump(normalized(saved)))).fetchone())

    def lookup(self, topic):
        key, skey, scope = exact_key(topic), semantic_key(topic), scope_key(topic)
        reasons = []
        with self.connection() as con:
            con.execute('BEGIN')
            rejected = {r[0] for r in con.execute("SELECT answer_json FROM rejections WHERE scope_key=?", (scope,))}

            def resolve(rows, source):
                candidates = {}
                for row in rows:
                    saved = json.loads(row['answer_json'])
                    if row['answer_json'] in rejected:
                        reasons.append('对应答案已被服务器否定')
                        continue
                    answer = map_answer(topic, saved)
                    if answer is not None:
                        candidates[dump(answer)] = answer
                    else:
                        reasons.append('答案无法唯一映射到当前选项')
                if len(candidates) == 1:
                    return Match(next(iter(candidates.values())), source, '')
                if len(candidates) > 1:
                    reasons.append('多个答案无法消歧')
                if len(candidates) > 1:
                    return Match(reason='；'.join(dict.fromkeys(reasons)))
                return None

            for stage, label in [('formal', '精确题库'), ('cache', '临时缓存')]:
                rows = con.execute("SELECT answer_json FROM records WHERE exact_key=? AND stage=? AND verification IN ('confirmed','official')", (key,stage)).fetchall()
                hit = resolve(rows, label)
                if hit:
                    return hit
            # Curated knowledge precedes unverified historical candidates.
            if skey:
                for stage, label in [('formal', '正式词库'), ('historical', '历史词库候选')]:
                    history_key = json.loads(skey)
                    if stage == 'historical' and is_collocation(topic):
                        history_key[-1] = None
                    rows = con.execute("SELECT k.answer_json FROM knowledge k JOIN records r ON r.id=k.origin_id WHERE k.semantic_key=? AND r.stage=?", (dump(history_key),stage)).fetchall()
                    hit = resolve(rows, label)
                    if hit:
                        return hit
            legacy_rows = []
            for old_key in legacy_keys(topic):
                row = con.execute("SELECT value_json,issue FROM legacy WHERE key=?", (old_key,)).fetchone()
                if row and not row['issue'] and not _has_media(topic):
                    old_topic, saved = parse_legacy(old_key, json.loads(row['value_json']))
                    # Missing legacy context must never be inferred from a new topic.
                    old_remark = normalized(old_topic['stem'].get('remark') or '')
                    current_remark = normalized((topic.get('stem') or {}).get('remark') or '')
                    if old_remark != current_remark:
                        reasons.append('历史记录缺少兼容上下文')
                        continue
                    if topic.get('topic_mode') not in KNOWN_MODES:
                        old_texts = [norm(o.get('content')) for o in old_topic['options']]
                        if old_texts != [norm(o.get('content')) for o in topic.get('options') or []]:
                            continue
                    legacy_rows.append({'answer_json':dump(normalized(saved))})
            hit = resolve(legacy_rows, '历史精确候选')
            if hit:
                return hit
        return Match(reason='；'.join(dict.fromkeys(reasons)) or '词库没有兼容记录')

    def preview(self, ids=None):
        with self.connection() as con:
            rows = con.execute("SELECT * FROM records WHERE stage='cache' ORDER BY id").fetchall()
            out = []
            for row in rows:
                if ids is not None and row['id'] not in ids:
                    continue
                formal = con.execute("SELECT answer_json FROM records WHERE exact_key=? AND stage='formal'", (row['exact_key'],)).fetchall()
                skey_answers = {r[0] for r in con.execute("SELECT answer_json FROM knowledge WHERE semantic_key=?", (row['semantic_key'],))}
                topic = json.loads(row['topic_json'])
                banned = con.execute("SELECT 1 FROM rejections WHERE scope_key=? AND answer_json=?", (scope_key(topic), row['answer_json'])).fetchone()
                verdict = '待验证' if row['verification']=='pending' or banned else (
                    '重复' if row['answer_json'] in {r[0] for r in formal} else
                    '冲突（保留候选）' if any(x != row['answer_json'] for x in skey_answers) or formal else '新增')
                out.append({'id':row['id'], 'classification':verdict, 'verification':row['verification'],
                            'stem':topic['stem'].get('content',''), 'remark':topic['stem'].get('remark',''),
                            'answer':json.loads(row['answer_json']), 'cross_task':bool(row['semantic_key']), 'source':row['source']})
            return out

    @staticmethod
    def _data(con):
        tables = {name:[dict(row) for row in con.execute(f'SELECT * FROM {name} ORDER BY {TABLE_ORDER[name]}')]
                  for name in TABLES}
        sequences = dict(con.execute('SELECT name,seq FROM sqlite_sequence ORDER BY name'))
        return {'tables':tables, 'sequences':sequences}

    @staticmethod
    def _fingerprint(data):
        return hashlib.sha256(dump(data).encode('utf-8')).hexdigest()

    def _snapshot(self):
        self.validate()
        # SQLite's backup API creates a consistent in-memory copy, even while
        # task processes write. No standalone local backup remains.
        with self.connection() as src:
            dest = sqlite3.connect(':memory:')
            dest.row_factory = sqlite3.Row
            try:
                src.backup(dest)
                return self._data(dest)
            finally:
                dest.close()

    @staticmethod
    def _export_bytes(data):
        return (json.dumps({'format':'cidaren-wordbank', 'version':EXPORT_VERSION,
                            'schema_version':SCHEMA_VERSION, 'exported_at':now(), **data},
                           ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8')

    def backup(self, label='manual'):
        from .git_backups import LEXICON
        data = self._snapshot()
        result = self._backend().publish({LEXICON:self._export_bytes(data)}, label=label)
        return {**result, 'state_sha256':self._fingerprint(data)}

    def _check_backed_up(self, con, backup):
        if self._fingerprint(self._data(con)) != backup['state_sha256']:
            raise BankError('上传期间词库已有新写入，本次维护已取消，原数据保留；请重试')

    def promote(self, ids=None):
        with self.operation():
            return self._promote(ids)

    def _promote(self, ids):
        backup = self.backup('before-promote')
        review = [row for row in self.preview(ids) if row['classification'].startswith('冲突')]
        promoted, skipped = [], []
        with self.transaction() as con:
            self._check_backed_up(con, backup)
            rows = con.execute("SELECT * FROM records WHERE stage='cache' ORDER BY id").fetchall()
            for row in rows:
                if ids is not None and row['id'] not in ids:
                    continue
                topic = json.loads(row['topic_json'])
                banned = con.execute("SELECT 1 FROM rejections WHERE scope_key=? AND answer_json=?", (scope_key(topic),row['answer_json'])).fetchone()
                if row['verification'] not in ('confirmed','official') or banned:
                    skipped.append(row['id'])
                    continue
                formal = con.execute("SELECT id FROM records WHERE exact_key=? AND answer_json=? AND stage='formal'", (row['exact_key'],row['answer_json'])).fetchone()
                if formal:
                    con.execute("UPDATE observations SET record_id=? WHERE record_id=?", (formal['id'],row['id']))
                    con.execute("DELETE FROM records WHERE id=?", (row['id'],))
                    target_id = formal['id']
                else:
                    con.execute("UPDATE records SET stage='formal' WHERE id=?", (row['id'],))
                    target_id = row['id']
                self._index(con,target_id)
                promoted.append(row['id'])
        return {'promoted':promoted,'skipped':skipped,'conflicts':[r['id'] for r in review if r['id'] in promoted], 'backup':backup}

    def clear_cache(self):
        with self.operation():
            backup = self.backup('before-clear')
            with self.transaction() as con:
                self._check_backed_up(con, backup)
                count = con.execute("SELECT COUNT(*) FROM records WHERE stage='cache'").fetchone()[0]
                con.execute("DELETE FROM records WHERE stage='cache'")
            return {'cleared':count,'backup':backup}

    def export(self, target):
        target = Path(target).resolve()
        if target == self.path or target == LEGACY_FILE:
            raise BankError('导出路径不能覆盖词库或原题库')
        _atomic_json(target, json.loads(self._export_bytes(self._snapshot())))
        return {'export':str(target)}

    def restore(self, backup):
        if isinstance(backup, dict):
            backup = backup['reference']
        reference = str(backup)
        with self.operation(), self.maintenance(check_old_server=True):
            with tempfile.TemporaryDirectory(prefix='.restore-', dir=self.path.parent) as folder:
                legacy = False
                if reference.startswith('git:'):
                    downloaded = self._backend().download(reference, folder)
                    source_path, legacy = downloaded['path'], downloaded['kind'] == 'legacy'
                    reference = downloaded['reference']
                else:
                    source_path = Path(backup).resolve()
                    if source_path == self.path:
                        raise BankError('不能用当前数据库恢复自身')
                    if not source_path.is_file():
                        raise BankError('恢复文件不存在')
                # Fully validate the candidate before uploading the current
                # state or replacing it. Temporary files are always removed.
                temp = Path(folder) / 'candidate.sqlite3'
                if legacy:
                    BankStore(temp)._import_legacy(source_path.read_bytes())
                elif source_path.read_bytes()[:16] == b'SQLite format 3\0':
                    source = BankStore(source_path)
                    source.validate()
                    with source.connection() as src:
                        dest = sqlite3.connect(temp)
                        try:
                            src.backup(dest)
                        finally:
                            dest.close()
                else:
                    self._restore_json(source_path, temp)
                BankStore(temp).validate()
                previous = self.backup('before-restore') if self.path.exists() else None
                if previous:
                    with self.transaction() as con:
                        self._check_backed_up(con, previous)
                os.replace(temp, self.path)
                return {'restored':reference, 'previous_backup':previous}

    @staticmethod
    def _restore_json(backup, temp):
        try:
            data = json.loads(backup.read_bytes())
            if (not isinstance(data, dict) or data.get('format') != 'cidaren-wordbank'
                    or type(data.get('version')) is not int or data['version'] not in (1, EXPORT_VERSION)
                    or set(data['tables']) != set(TABLES)):
                raise ValueError('不是兼容的词库导出文件')
            if data['version'] == EXPORT_VERSION and (type(data.get('schema_version')) is not int
                                                      or data['schema_version'] != SCHEMA_VERSION):
                raise ValueError('数据库格式版本不兼容')
            target = BankStore(temp)
            target.initialize()
            with target.transaction() as con:
                for table in TABLES:
                    columns = [row[1] for row in con.execute(f'PRAGMA table_info({table})')]
                    rows = data['tables'][table]
                    if not isinstance(rows, list) or any(not isinstance(row, dict) or set(row) != set(columns) for row in rows):
                        raise ValueError(f'{table} 表格式不合法')
                    marks = ','.join('?' for _ in columns)
                    con.executemany(f"INSERT INTO {table}({','.join(columns)}) VALUES({marks})",
                                    [[row[c] for c in columns] for row in rows])
                if data['version'] == EXPORT_VERSION:
                    sequences = data.get('sequences')
                    maximum = con.execute('SELECT COALESCE(MAX(id),0) FROM records').fetchone()[0]
                    if (not isinstance(sequences, dict) or not set(sequences) <= {'records'}
                            or any(type(v) is not int or not 0 <= v <= 9223372036854775807 for v in sequences.values())
                            or sequences.get('records', 0) < maximum):
                        raise ValueError('自增序号缺失或不合法')
                    con.execute('DELETE FROM sqlite_sequence')
                    con.executemany('INSERT INTO sqlite_sequence(name,seq) VALUES(?,?)', sequences.items())
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise BankError(f'恢复文件损坏或格式不兼容: {exc}') from exc


def _atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.'+path.name,suffix='.tmp',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as stream:
            json.dump(data,stream,ensure_ascii=False,indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp,path)
    finally:
        Path(temp).unlink(missing_ok=True)


def default_store():
    store = BankStore()
    store.validate()
    return store


def prepare_default_store():
    store = BankStore()
    store.migrate()
    store.validate()
    return store
