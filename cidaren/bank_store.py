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
import shutil
import socket
import sqlite3
import tempfile
import unicodedata
import uuid

ROOT = Path(__file__).resolve().parent.parent
LEGACY_FILE = Path(__file__).with_name("bank.json")
SCHEMA_VERSION = 1
TABLES = ("metadata", "legacy", "records", "knowledge", "rejections", "observations")
KNOWN_MODES = {0, 11, 15, 17, 22, 31, 32, 41, 51, 52}


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
    return out


def exact_key(topic):
    return hashlib.sha256(dump(normalized(snapshot(topic))).encode()).hexdigest()


def scope_key(topic):
    data = snapshot(topic)
    data["options"] = sorted(norm(o.get("content")) for o in data["options"])
    return hashlib.sha256(dump(normalized(data)).encode()).hexdigest()


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
    if mode in (0, 15, 22) and re.fullmatch(r"[A-Za-z][A-Za-z' -]*", stem.strip()):
        return dump(["definition", norm(stem)])
    if mode == 17:
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
    return dump([mode, "collocation" if is_collocation(topic) else "context", norm(stem), normalized(remark)])


def tag_for_option(option, index):
    return option.get("answer_tag", index)


def _same_tag(left, right):
    if isinstance(left, bool) or isinstance(right, bool):
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
        items = saved.get("items") or []
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
        return tags[0] if kind == "choice" and tags else tags or None
    return None


def legacy_keys(topic):
    mode = topic.get("topic_mode", "?")
    stem = norm((topic.get("stem") or {}).get("content", ""))
    remark = (topic.get("stem") or {}).get("remark", "") or ""
    if isinstance(remark, list):
        remark = json.dumps(remark, ensure_ascii=False, sort_keys=True)
    options = "|".join((o.get("content") or "")[:20] for o in (topic.get("options") or [])).lower()
    kind = "coll" if is_collocation(topic) else "norm"
    return [f"{mode}::{kind}::{stem}::{remark}::{options}", f"{mode}::{stem}::{remark}::{options}", f"{mode}::{stem}::{options}"]


def parse_legacy(key, value):
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
    topic = {"topic_mode": mode, "stem": {"content": value.get("stem", stem), "remark": remark}, "options": opts}
    answer = value.get("ans")
    saved = encode_answer(topic, answer)
    if saved["kind"] in ("choice", "choices"):
        texts = [saved["text"]] if saved["kind"] == "choice" else saved["items"]
        if any(len(s) == 20 for s in texts):
            raise BankError("正确选项可能被截断，需重新验证")
    return topic, saved


@contextmanager
def _file_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        stream.seek(0)
        if stream.read(1) == b"":
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
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
    def __init__(self, path=None):
        self.path = Path(path or os.environ.get("CIDAREN_BANK_DB") or ROOT / "data" / "lexicon.sqlite3").resolve()

    @contextmanager
    def connection(self, *, create=False):
        if not create and not self.path.is_file():
            raise BankError("词库尚未迁移，请运行词库管理 migrate")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            con = sqlite3.connect(self.path, timeout=10)
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA foreign_keys=ON")
            con.execute("PRAGMA busy_timeout=10000")
        except sqlite3.Error as exc:
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
                        raise BankError("首次迁移前请关闭5001网页服务和任务")
            for path in (self.path.parent / "runtime").glob("*.lock"):
                with _file_lock(path):
                    pass  # Stale files are harmless; live holders make the lock fail.
            yield

    @contextmanager
    def runtime(self):
        path = self.path.parent / "runtime" / f"{os.getpid()}-{uuid.uuid4().hex}.lock"
        with _file_lock(self.path.parent / ".maintenance.lock"):
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
                    id INTEGER PRIMARY KEY, exact_key TEXT NOT NULL, semantic_key TEXT,
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
        with self.maintenance(check_old_server=True):
            raw = Path(source).read_bytes()
            try:
                bank = json.loads(raw)
            except (ValueError, UnicodeError) as exc:
                raise BankError("原题库JSON损坏，迁移已取消") from exc
            if not isinstance(bank, dict) or any(not isinstance(v, dict) or 'ans' not in v for v in bank.values()):
                raise BankError("原题库记录格式不合法，迁移已取消")
            digest = hashlib.sha256(raw).hexdigest()
            backup_dir = self.path.parent / "backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup = backup_dir / f"legacy-{digest[:12]}.json"
            if not backup.exists():
                backup.write_bytes(raw)
            elif backup.read_bytes() != raw:
                raise BankError("历史备份校验失败")
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
            return {"already_migrated": False, "backup": str(backup), **self.status()}

    def validate(self):
        with self.connection() as con:
            result = con.execute("PRAGMA quick_check").fetchone()[0]
            if result != "ok" or con.execute("PRAGMA foreign_key_check").fetchone():
                raise BankError(f"词库完整性检查失败: {result}")
            if not con.execute("SELECT 1 FROM metadata WHERE key='migration'").fetchone():
                raise BankError("词库迁移未完成，请重新执行 migrate")

    def status(self):
        with self.connection() as con:
            stages = dict(con.execute("SELECT stage,COUNT(*) FROM records GROUP BY stage").fetchall())
            return {"database": str(self.path), "legacy": con.execute("SELECT COUNT(*) FROM legacy").fetchone()[0],
                    "legacy_issues": con.execute("SELECT COUNT(*) FROM legacy WHERE issue<>''").fetchone()[0],
                    "formal": stages.get("formal", 0), "historical": stages.get("historical", 0), "cache": stages.get("cache", 0),
                    "pending": con.execute("SELECT COUNT(*) FROM records WHERE stage='cache' AND verification='pending'").fetchone()[0],
                    "knowledge": con.execute("SELECT COUNT(DISTINCT semantic_key) FROM knowledge").fetchone()[0],
                    "conflicts": con.execute("SELECT COUNT(*) FROM (SELECT semantic_key FROM knowledge GROUP BY semantic_key HAVING COUNT(DISTINCT answer_json)>1)").fetchone()[0],
                    "rejections": con.execute("SELECT COUNT(*) FROM rejections").fetchone()[0]}

    def record(self, topic, answer, source, verification="pending", *, complete=True, detail=None):
        if verification not in ("pending", "confirmed", "official"):
            raise BankError("未知验证状态")
        saved = encode_answer(topic, answer)
        if saved['kind'] == 'choices' and not complete:
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
        for opt in topic.get('options') or []:
            if opt.get('content'):
                # Definition cards can repeat answer_tag, so record one complete
                # definition at a time and retain its original topic information.
                card = {**snapshot(topic), 'options': [opt]}
                ids.append(self.record(card, tag_for_option(opt, 0), 'official_definitions', 'official'))
        return ids

    def reject(self, topic, answer, *, detail=None):
        saved = encode_answer(topic, answer)
        with self.transaction() as con:
            con.execute("INSERT OR REPLACE INTO rejections VALUES(?,?,?)", (scope_key(topic), dump(normalized(saved)), now()))
            con.execute("INSERT INTO observations(exact_key,outcome,detail_json,created_at) VALUES(?,?,?,?)",
                        (exact_key(topic), 'rejected', dump(detail or {}), now()))

    def lookup(self, topic):
        key, skey, scope = exact_key(topic), semantic_key(topic), scope_key(topic)
        reasons = []
        with self.connection() as con:
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
                return None

            for stage, label in [('formal', '精确题库'), ('cache', '临时缓存')]:
                rows = con.execute("SELECT answer_json FROM records WHERE exact_key=? AND stage=? AND verification IN ('confirmed','official')", (key,stage)).fetchall()
                hit = resolve(rows, label)
                if hit:
                    return hit
            # Only complete legacy answers participate. New full records replace
            # legacy index assumptions after successful verification.
            for old_key in legacy_keys(topic):
                row = con.execute("SELECT value_json,issue FROM legacy WHERE key=?", (old_key,)).fetchone()
                if row and not row['issue']:
                    old_topic, saved = parse_legacy(old_key, json.loads(row['value_json']))
                    if not _has_media(topic):
                        hit = resolve([{'answer_json': dump(normalized(saved))}], '历史词库候选')
                        if hit:
                            return hit
            if skey:
                for stage, label in [('formal', '正式词库'), ('historical', '历史词库候选')]:
                    rows = con.execute("SELECT k.answer_json FROM knowledge k JOIN records r ON r.id=k.origin_id WHERE k.semantic_key=? AND r.stage=?", (skey,stage)).fetchall()
                    hit = resolve(rows, label)
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

    def backup(self, label='manual'):
        self.validate()
        folder = self.path.parent / 'backups'
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{label}-{uuid.uuid4().hex[:8]}.sqlite3"
        with self.connection() as src:
            dest = sqlite3.connect(path)
            try:
                src.backup(dest)
            finally:
                dest.close()
        return path

    def promote(self, ids=None):
        backup = self.backup('before-promote')
        promoted, skipped = [], []
        with self.transaction() as con:
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
        return {'promoted':promoted,'skipped':skipped,'backup':str(backup)}

    def clear_cache(self):
        backup = self.backup('before-clear')
        with self.transaction() as con:
            count = con.execute("SELECT COUNT(*) FROM records WHERE stage='cache'").fetchone()[0]
            con.execute("DELETE FROM records WHERE stage='cache'")
        return {'cleared':count,'backup':str(backup)}

    def export(self, target):
        self.validate()
        target = Path(target).resolve()
        if target == self.path or target == LEGACY_FILE:
            raise BankError('导出路径不能覆盖词库或原题库')
        with self.connection() as con:
            con.execute('BEGIN')
            data = {name:[dict(row) for row in con.execute(f'SELECT * FROM {name}')] for name in TABLES}
        _atomic_json(target, {'format':'cidaren-wordbank','version':SCHEMA_VERSION,'exported_at':now(),'tables':data})
        return {'export':str(target)}

    def restore(self, backup):
        backup = Path(backup).resolve()
        if backup == self.path:
            raise BankError('不能用当前数据库恢复自身')
        with self.maintenance():
            # Validate the source before backing up or replacing the destination.
            source = BankStore(backup)
            source.validate()
            previous = self.backup('before-restore') if self.path.exists() else None
            fd, temp = tempfile.mkstemp(prefix='.restore-',suffix='.sqlite3',dir=self.path.parent)
            os.close(fd)
            try:
                with source.connection() as src:
                    dest = sqlite3.connect(temp)
                    try:
                        src.backup(dest)
                    finally:
                        dest.close()
                BankStore(temp).validate()
                os.replace(temp,self.path)
            finally:
                Path(temp).unlink(missing_ok=True)
            return {'restored':str(backup),'previous_backup':str(previous) if previous else None}


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
