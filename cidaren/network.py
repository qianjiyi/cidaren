"""Explicit proxy routing, bounded retries and payload-free network diagnostics."""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import os
from pathlib import Path
import random
import threading
import time

import certifi
import requests
from requests.adapters import HTTPAdapter

RETRY_STATUSES = {429, 502, 503, 504}
_LOCAL = threading.local()
_LOG_LOCK = threading.Lock()


class NetworkError(RuntimeError):
    def __init__(self, kind, service, action, *, status=None, attempts=1):
        self.kind, self.status, self.attempts = kind, status, attempts
        labels = {
            "proxy": "代理连接失败，请检查代理服务或切换直连",
            "connect_timeout": "连接超时", "read_timeout": "读取响应超时",
            "connection": "连接中断", "tls": "HTTPS 证书校验失败",
            "http": "服务器返回 HTTP 错误", "json": "响应不是有效 JSON",
            "shape": "响应结构无效", "deadline": "请求等待预算已用完",
            "request": "网络请求失败",
        }
        label = "模型" if service == "llm" else "词达人"
        suffix = f"，HTTP {status}" if status is not None else ""
        super().__init__(f"{label} {action}：{labels.get(kind, kind)}{suffix}（尝试 {attempts} 次）")


def proxy_mode(config, service):
    mode = config.get("LLM_PROXY_MODE" if service == "llm" else "VOCABGO_PROXY_MODE", "direct")
    if mode not in {"direct", "system", "custom"}:
        raise ValueError("代理模式必须为 direct、system 或 custom")
    return mode


def create_session(config, service):
    mode = proxy_mode(config, service)
    proxies = {}
    if mode == "custom":
        key = "LLM_PROXY_URL" if service == "llm" else "VOCABGO_PROXY_URL"
        url = str(config.get(key) or "").strip()
        from urllib.parse import urlsplit
        try:
            parsed = urlsplit(url)
            valid = parsed.scheme in {"http", "https"} and parsed.hostname and parsed.port != 0
        except ValueError:
            valid = False
        if not valid:
            raise ValueError(f"{key} 需要有效的 http:// 或 https:// 代理地址")
        proxies = {"http": url, "https": url}
    session = requests.Session()
    session.trust_env = mode == "system"
    # Proxy selection must not disable certificate verification.
    session.verify = os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE") or certifi.where()
    session.proxies = proxies
    # Retries belong to the operation policy, never the HTTP verb/adapter.
    for scheme in ("http://", "https://"):
        session.mount(scheme, HTTPAdapter(max_retries=0, pool_connections=2, pool_maxsize=2, pool_block=True))
    return session


class _SessionOwner:
    def __init__(self, key, config):
        self.key = key
        self.session = None
        self.session = create_session(config, "llm")

    def close(self):
        if self.session is not None:
            self.session.close()
            self.session = None

    __del__ = close


def llm_session(config):
    """One reusable session per owner thread; retire it when its configuration changes."""
    key = tuple(config.get(k, "") for k in ("LLM_URL", "LLM_KEY", "LLM_MODEL", "LLM_PROXY_MODE", "LLM_PROXY_URL"))
    owner = getattr(_LOCAL, "llm", None)
    if owner is None or owner.key != key:
        close_thread_sessions()
        owner = _LOCAL.llm = _SessionOwner(key, config)
    return owner.session


def close_thread_sessions():
    owner = getattr(_LOCAL, "llm", None)
    if owner is not None:
        owner.close()
        del _LOCAL.llm


def _record_event(event):
    """A shared 3 x 1 MiB ring, serialized across processes; diagnostics never fail a task."""
    root = Path(os.environ.get("CIDAREN_DIAGNOSTIC_DIR") or Path(__file__).resolve().parents[1] / "data" / "diagnostics")
    try:
        with _LOG_LOCK:
            root.mkdir(parents=True, exist_ok=True)
            with (root / ".network.lock").open("a+b") as lock:
                lock.seek(0, 2)
                if not lock.tell():
                    lock.write(b"0")
                    lock.flush()
                lock.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                try:
                    path = root / "network.jsonl"
                    if path.exists() and path.stat().st_size >= 1024 * 1024:
                        (root / "network.jsonl.2").unlink(missing_ok=True)
                        older = root / "network.jsonl.1"
                        if older.exists():
                            older.replace(root / "network.jsonl.2")
                        path.replace(older)
                    with path.open("a", encoding="utf-8") as output:
                        output.write(json.dumps({"time": datetime.now(timezone.utc).isoformat(), "pid": os.getpid(), **event}, ensure_ascii=False) + "\n")
                finally:
                    lock.seek(0)
                    if os.name == "nt":
                        msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(lock, fcntl.LOCK_UN)
    except OSError:
        pass


def _retry_after(value):
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            when = parsedate_to_datetime(value)
            seconds = when.timestamp() - time.time()
        except (TypeError, ValueError, OverflowError):
            return 0
    return max(0, seconds) if seconds < float("inf") else 0


def request_json(send, *, service, action, route="direct", attempts=1, read_timeout=20, deadline=None):
    """Only explicitly read-only operations may pass attempts > 1.

    deadline bounds retry scheduling and rejects late replies. Requests' socket
    timeouts are not hard wall-clock cancellation; test owners also enforce their
    own question deadline independently of this transport.
    """
    if deadline is None and attempts > 1:
        deadline = time.monotonic() + (90 if service == "llm" else 65)
    last = None
    for attempt in range(1, attempts + 1):
        remaining = float("inf") if deadline is None else deadline - time.monotonic()
        if remaining <= 0:
            raise NetworkError("deadline", service, action, attempts=attempt - 1)
        response, status, retryable, wait = None, None, False, 0
        start = time.monotonic()
        try:
            response = send((min(5, remaining), min(read_timeout, remaining)))
            status = response.status_code
            if not 200 <= status < 300:
                retryable = status in RETRY_STATUSES
                wait = _retry_after(response.headers.get("Retry-After"))
                raise NetworkError("http", service, action, status=status, attempts=attempt)
            try:
                data = response.json()
            except ValueError:
                raise NetworkError("json", service, action, status=status, attempts=attempt) from None
            if not isinstance(data, dict):
                raise NetworkError("shape", service, action, status=status, attempts=attempt)
            if deadline is not None and time.monotonic() >= deadline:
                raise NetworkError("deadline", service, action, attempts=attempt)
            _record_event(dict(service=service, action=action, route=route, attempt=attempt,
                               status=status, kind="ok", elapsed_ms=round((time.monotonic() - start) * 1000)))
            return data
        except requests.RequestException as exc:
            if isinstance(exc, requests.exceptions.ProxyError):
                kind = "proxy"
            elif isinstance(exc, requests.exceptions.SSLError):
                kind = "tls"
            elif isinstance(exc, requests.ConnectTimeout):
                kind = "connect_timeout"
            elif isinstance(exc, requests.Timeout):
                kind = "read_timeout"
            elif isinstance(exc, (requests.ConnectionError, requests.exceptions.ChunkedEncodingError)):
                kind = "connection"
            else:
                kind = "request"
            retryable = kind in {"proxy", "connect_timeout", "read_timeout", "connection"}
            last = NetworkError(kind, service, action, attempts=attempt)
        except NetworkError as exc:
            last = exc
        finally:
            if response is not None:
                response.close()
        _record_event(dict(service=service, action=action, route=route, attempt=attempt,
                           status=status, kind=last.kind, elapsed_ms=round((time.monotonic() - start) * 1000)))
        if not retryable or attempt == attempts:
            raise last from None
        wait = max(wait, 0.5 * (2 ** (attempt - 1)) + random.uniform(0, 0.25))
        if deadline is not None and wait >= deadline - time.monotonic():
            raise last from None
        print(f"[网络重试] {last}；{wait:.1f} 秒后重试")
        time.sleep(wait)
    raise last  # pragma: no cover


class LogBuffer(deque):
    """Bounded log text with an opaque generation/sequence cursor."""
    def __init__(self, iterable=(), maxlen=500):
        import uuid
        super().__init__(maxlen=maxlen)
        self.generation, self.sequence = uuid.uuid4().hex, 0
        self.extend(iterable)

    def append(self, item):
        super().append(str(item))
        self.sequence += 1

    def extend(self, items):
        for item in items:
            self.append(item)

    def since(self, cursor=None):
        reset, after = True, self.sequence - len(self)
        if isinstance(cursor, str):
            try:
                generation, number = cursor.split(":", 1)
                number = int(number)
                if generation == self.generation and after <= number <= self.sequence:
                    reset, after = False, number
            except (ValueError, TypeError):
                pass
        offset = after - (self.sequence - len(self))
        return {"logs": list(self)[offset:], "log_cursor": f"{self.generation}:{self.sequence}", "log_reset": reset}
