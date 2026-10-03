"""Managed, Windows-only credential capture for the local web console."""

from __future__ import annotations

import atexit
import hashlib
import ipaddress
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

if sys.platform == "win32":  # pragma: no branch - the first release is Windows-only
    import ctypes
    import winreg


ACTIVE_STATES = {"starting", "waiting", "validating", "cancelling"}
CAPTURE_SERVER = "127.0.0.1:8888"


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


@dataclass(frozen=True)
class ProxySnapshot:
    enabled: bool
    server: str
    override: str


class WindowsProxySettings:
    REGISTRY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"

    def _require_windows(self) -> None:
        if sys.platform != "win32":
            raise RuntimeError("Token 获取功能首版仅支持 Windows")

    def read(self) -> ProxySnapshot:
        self._require_windows()
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.REGISTRY_PATH, 0, winreg.KEY_READ) as key:
            def value(name: str, default):
                try:
                    return winreg.QueryValueEx(key, name)[0]
                except FileNotFoundError:
                    return default

            return ProxySnapshot(
                enabled=bool(value("ProxyEnable", 0)),
                server=str(value("ProxyServer", "")),
                override=str(value("ProxyOverride", "")),
            )

    def write(self, snapshot: ProxySnapshot) -> None:
        self._require_windows()
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.REGISTRY_PATH, 0, winreg.KEY_WRITE) as key:
            winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, int(snapshot.enabled))
            winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, snapshot.server)
            winreg.SetValueEx(key, "ProxyOverride", 0, winreg.REG_SZ, snapshot.override)
        self._notify()

    @staticmethod
    def _notify() -> None:
        internet_set_option = ctypes.windll.Wininet.InternetSetOptionW
        internet_set_option(0, 39, 0, 0)  # INTERNET_OPTION_SETTINGS_CHANGED
        internet_set_option(0, 37, 0, 0)  # INTERNET_OPTION_REFRESH

    def enable_capture(self) -> None:
        current = self.read()
        self.write(ProxySnapshot(True, CAPTURE_SERVER, current.override or "<local>"))

    def restore_if_owned(self, original: ProxySnapshot) -> tuple[bool, str]:
        current = self.read()
        if current.enabled and current.server.lower() == CAPTURE_SERVER:
            self.write(original)
            return True, "原代理设置已恢复"
        return False, "检测到代理设置已被其他程序修改，未覆盖当前设置"


def is_loopback_request(remote_addr: str | None, origin_host: str | None) -> bool:
    try:
        remote_ok = bool(remote_addr and ipaddress.ip_address(remote_addr).is_loopback)
    except ValueError:
        remote_ok = False
    if not remote_ok:
        return False
    if not origin_host:
        return True
    return origin_host.lower() in {"localhost", "127.0.0.1", "::1"}


def _certificate_fingerprint(path: Path) -> bytes:
    pem = path.read_text(encoding="ascii")
    der = ssl.PEM_cert_to_DER_cert(pem)
    return hashlib.sha256(der).digest()


def certificate_is_trusted(cert_path: Path) -> bool:
    if sys.platform != "win32" or not cert_path.exists():
        return False
    target = _certificate_fingerprint(cert_path)
    for cert_bytes, encoding, _trust in ssl.enum_certificates("ROOT"):
        if encoding == "x509_asn" and hashlib.sha256(cert_bytes).digest() == target:
            return True
    return False


class CaptureManager:
    def __init__(
        self,
        base_dir: Path,
        save_credentials: Callable[[dict[str, str]], object],
        validate_credentials: Callable[[dict[str, str]], tuple[bool, str]],
        has_active_jobs: Callable[[], bool],
        *,
        timeout_seconds: float = 120,
        proxy: WindowsProxySettings | None = None,
        proxy_port: int = 8888,
    ) -> None:
        self.base_dir = Path(base_dir)
        self.data_dir = self.base_dir / ".capture"
        self.conf_dir = self.data_dir / "mitmproxy"
        self.result_path = self.data_dir / "credentials.json"
        self.recovery_path = self.data_dir / "proxy-recovery.json"
        self.last_recovery_path = self.data_dir / "last-proxy-recovery.json"
        self.save_credentials = save_credentials
        self.validate_credentials = validate_credentials
        self.has_active_jobs = has_active_jobs
        self.timeout_seconds = timeout_seconds
        self.proxy_port = proxy_port
        self.proxy = proxy or WindowsProxySettings()
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._process: subprocess.Popen | None = None
        self._status = {
            "state": "idle",
            "message": "尚未开始获取",
            "started_at": None,
            "expires_at": None,
            "updated_at": time.time(),
        }
        atexit.register(self.shutdown)

    def status(self) -> dict:
        with self._lock:
            state = self._status["state"]
            return {
                **self._status,
                "active": state in ACTIVE_STATES,
                "can_cancel": state in ACTIVE_STATES,
            }

    def is_active(self) -> bool:
        with self._lock:
            return self._status["state"] in ACTIVE_STATES

    def _set_status(self, state: str, message: str, **values) -> None:
        with self._lock:
            self._status.update(state=state, message=message, updated_at=time.time(), **values)

    def start(self) -> tuple[bool, str]:
        with self._lock:
            if self._status["state"] in ACTIVE_STATES:
                return False, "鉴权获取正在进行"
            if self.has_active_jobs():
                return False, "存在运行中或等待重启的任务，请先停止任务"
            self._cancel.clear()
            started = time.time()
            self._set_status(
                "starting",
                "正在启动本地抓取服务…",
                started_at=started,
                expires_at=started + self.timeout_seconds,
            )
            self._thread = threading.Thread(target=self._run, name="credential-capture", daemon=True)
            self._thread.start()
        return True, "已开始获取"

    def cancel(self) -> tuple[bool, str]:
        with self._lock:
            if self._status["state"] not in ACTIVE_STATES:
                return False, "当前没有正在进行的鉴权获取"
            self._set_status("cancelling", "正在取消并恢复代理设置…")
            self._cancel.set()
            process = self._process
        if process and process.poll() is None:
            process.terminate()
        return True, "已请求取消"

    def shutdown(self) -> None:
        self._cancel.set()
        with self._lock:
            process = self._process
        if process and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=3)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
        self.recover_stale_proxy()

    def recover_stale_proxy(self) -> None:
        if not self.recovery_path.exists():
            return
        try:
            record = json.loads(self.recovery_path.read_text(encoding="utf-8"))
            original = ProxySnapshot(**record["original"])
            restored, message = self.proxy.restore_if_owned(original)
            outcome = "restored" if restored else "skipped_external_change"
        except Exception as exc:
            outcome, message = "failed", f"恢复代理失败: {exc}"
        self._write_recovery_history(outcome, message)
        try:
            self.recovery_path.unlink()
        except FileNotFoundError:
            pass

    def _write_recovery_history(self, outcome: str, message: str) -> None:
        _atomic_write_json(
            self.last_recovery_path,
            {"outcome": outcome, "message": message, "time": time.time()},
        )

    def _find_mitmdump(self) -> str:
        candidates = [
            Path(sys.executable).parent / "Scripts" / "mitmdump.exe",
            Path(sys.executable).parent / "Scripts" / "mitmdump",
        ]
        for candidate in candidates:
            if candidate.exists():
                return str(candidate)
        found = shutil.which("mitmdump")
        if found:
            return found
        raise RuntimeError("当前 Conda 环境未安装 mitmproxy/mitmdump")

    def _port_available(self) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", self.proxy_port))
            except OSError:
                return False
        return True

    def _port_listening(self) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", self.proxy_port), timeout=0.2):
                return True
        except OSError:
            return False

    def _start_proxy_process(self, capture_id: str) -> subprocess.Popen:
        addon = Path(__file__).with_name("mitm_addon.py")
        env = os.environ.copy()
        env["CIDAREN_CAPTURE_RESULT"] = str(self.result_path)
        env["CIDAREN_CAPTURE_ID"] = capture_id
        command = [
            self._find_mitmdump(),
            "-s", str(addon),
            "--listen-host", "127.0.0.1",
            "--listen-port", str(self.proxy_port),
            "--quiet",
            "--set", f"confdir={self.conf_dir}",
            "--set", "console_eventlog_verbosity=error",
            "--allow-hosts", r"^app\.vocabgo\.com(?::443)?$",
        ]
        startupinfo = None
        creationflags = 0
        if sys.platform == "win32":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            creationflags = subprocess.CREATE_NO_WINDOW
        return subprocess.Popen(
            command,
            cwd=self.base_dir,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            startupinfo=startupinfo,
            creationflags=creationflags,
        )

    def _wait_until_ready(self, process: subprocess.Popen) -> None:
        deadline = time.time() + 12
        cert_path = self.conf_dir / "mitmproxy-ca-cert.pem"
        while time.time() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"抓取服务启动失败，退出码 {process.returncode}")
            if self._port_listening() and cert_path.exists():
                return
            if self._cancel.wait(0.1):
                raise InterruptedError
        raise RuntimeError("抓取服务启动超时，请检查 8888 端口")

    def _ensure_certificate_trusted(self) -> None:
        cert_path = self.conf_dir / "mitmproxy-ca-cert.pem"
        if certificate_is_trusted(cert_path):
            return
        certutil = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "certutil.exe"
        result = subprocess.run(
            [str(certutil), "-addstore", "-f", "-user", "Root", str(cert_path)],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        if result.returncode != 0 or not certificate_is_trusted(cert_path):
            raise RuntimeError("无法信任本项目的抓取证书，请检查当前用户证书权限")

    def _load_result(self, capture_id: str) -> dict[str, str] | None:
        if not self.result_path.exists():
            return None
        try:
            payload = json.loads(self.result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if payload.get("capture_id") != capture_id:
            return None
        fields = {key: str(payload.get(key) or "").strip() for key in ("USERTOKEN", "ABC", "AUTH_V", "USER_AGENT")}
        if not all(fields[key] for key in ("USERTOKEN", "ABC", "AUTH_V")):
            return None
        return fields

    def _terminate_process(self, process: subprocess.Popen | None) -> None:
        if not process or process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=3)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

    def _run(self) -> None:
        process = None
        original = None
        proxy_enabled = False
        capture_id = uuid.uuid4().hex
        try:
            if sys.platform != "win32":
                raise RuntimeError("Token 获取功能首版仅支持 Windows")
            self.recover_stale_proxy()
            if not self._port_available():
                raise RuntimeError(f"端口 {self.proxy_port} 已被占用，无法启动抓取服务")
            self.data_dir.mkdir(parents=True, exist_ok=True)
            try:
                self.result_path.unlink()
            except FileNotFoundError:
                pass

            process = self._start_proxy_process(capture_id)
            with self._lock:
                self._process = process
            self._wait_until_ready(process)
            self._ensure_certificate_trusted()
            if self._cancel.is_set():
                raise InterruptedError

            original = self.proxy.read()
            _atomic_write_json(
                self.recovery_path,
                {"capture_id": capture_id, "original": asdict(original), "capture_server": CAPTURE_SERVER},
            )
            self.proxy.enable_capture()
            proxy_enabled = True
            self._set_status("waiting", "请在 PC 微信中打开词达人学生端，等待自动获取…")

            deadline = time.time() + self.timeout_seconds
            credentials = None
            while time.time() < deadline:
                if self._cancel.wait(0.15):
                    raise InterruptedError
                credentials = self._load_result(capture_id)
                if credentials:
                    break
                if process.poll() is not None:
                    credentials = self._load_result(capture_id)
                    if credentials:
                        break
                    raise RuntimeError(f"抓取服务意外退出，退出码 {process.returncode}")
            if not credentials:
                self._set_status("timed_out", "获取超时，未读取到完整鉴权字段")
                return

            self._terminate_process(process)
            if proxy_enabled and original:
                restored, restore_message = self.proxy.restore_if_owned(original)
                proxy_enabled = False
                self._write_recovery_history("restored" if restored else "skipped_external_change", restore_message)
                try:
                    self.recovery_path.unlink()
                except FileNotFoundError:
                    pass

            self._set_status("validating", "已获取完整字段，正在验证账号…")
            valid, validation_message = self.validate_credentials(credentials)
            if not valid:
                self._set_status("failed", f"鉴权验证失败：{validation_message}；原配置未改动")
                return
            self.save_credentials(credentials)
            self._set_status("succeeded", f"鉴权已保存并验证成功：{validation_message}")
        except InterruptedError:
            self._set_status("cancelled", "已取消获取并恢复代理设置")
        except Exception as exc:
            self._set_status("failed", str(exc))
        finally:
            self._terminate_process(process)
            if proxy_enabled and original:
                try:
                    restored, restore_message = self.proxy.restore_if_owned(original)
                    self._write_recovery_history("restored" if restored else "skipped_external_change", restore_message)
                except Exception as exc:
                    self._write_recovery_history("failed", f"恢复代理失败: {exc}")
                try:
                    self.recovery_path.unlink()
                except FileNotFoundError:
                    pass
            try:
                self.result_path.unlink()
            except FileNotFoundError:
                pass
            with self._lock:
                self._process = None
