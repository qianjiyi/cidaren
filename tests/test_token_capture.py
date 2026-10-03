from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from cidaren.token_capture import CAPTURE_SERVER, CaptureManager, ProxySnapshot, is_loopback_request


class FakeProcess:
    def __init__(self):
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = 0

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def kill(self):
        self.returncode = -9


class FakeProxy:
    def __init__(self):
        self.original = ProxySnapshot(False, "old.proxy:9000", "<local>")
        self.capture_enabled = False
        self.restore_calls = 0
        self.on_enable = None

    def read(self):
        return self.original

    def enable_capture(self):
        self.capture_enabled = True
        if self.on_enable:
            self.on_enable()

    def restore_if_owned(self, original):
        self.restore_calls += 1
        self.capture_enabled = False
        return True, "原代理设置已恢复"


class CaptureManagerTests(unittest.TestCase):
    def make_manager(self, root: Path, *, validator=None, jobs=None, timeout=0.25):
        saved = []
        proxy = FakeProxy()
        manager = CaptureManager(
            root,
            lambda payload: saved.append(payload.copy()),
            validator or (lambda payload: (True, "测试账号")),
            jobs or (lambda: False),
            timeout_seconds=timeout,
            proxy=proxy,
        )
        manager._port_available = lambda: True
        manager._ensure_certificate_trusted = lambda: None
        return manager, proxy, saved

    @staticmethod
    def arrange_result(manager: CaptureManager, payload: dict):
        process = FakeProcess()
        captured = {}

        def start(capture_id):
            captured["id"] = capture_id
            return process

        def ready(_process):
            manager.data_dir.mkdir(parents=True, exist_ok=True)
            manager.result_path.write_text(
                json.dumps({**payload, "capture_id": captured["id"]}), encoding="utf-8"
            )

        manager._start_proxy_process = start
        manager._wait_until_ready = ready
        return process

    def test_complete_fields_are_validated_saved_and_user_agent_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager, proxy, saved = self.make_manager(Path(temp_dir))
            self.arrange_result(
                manager,
                {"USERTOKEN": "token", "ABC": "abc", "AUTH_V": "auth", "USER_AGENT": "wechat ua"},
            )

            manager._run()

            self.assertEqual(manager.status()["state"], "succeeded")
            self.assertEqual(saved[0]["USER_AGENT"], "wechat ua")
            self.assertEqual(proxy.restore_calls, 1)
            self.assertFalse(manager.result_path.exists())

    def test_incomplete_result_is_not_accepted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager, _proxy, _saved = self.make_manager(Path(temp_dir))
            manager.data_dir.mkdir(parents=True)
            manager.result_path.write_text(
                json.dumps({"capture_id": "same", "USERTOKEN": "token", "ABC": "abc"}),
                encoding="utf-8",
            )
            self.assertIsNone(manager._load_result("same"))

    def test_validation_failure_keeps_existing_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager, proxy, saved = self.make_manager(
                Path(temp_dir), validator=lambda payload: (False, "token 已失效")
            )
            self.arrange_result(
                manager,
                {"USERTOKEN": "token", "ABC": "abc", "AUTH_V": "auth", "USER_AGENT": "ua"},
            )

            manager._run()

            self.assertEqual(manager.status()["state"], "failed")
            self.assertEqual(saved, [])
            self.assertEqual(proxy.restore_calls, 1)

    def test_cancel_after_proxy_enable_restores_proxy(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager, proxy, saved = self.make_manager(Path(temp_dir), timeout=1)
            process = FakeProcess()
            manager._start_proxy_process = lambda capture_id: process
            manager._wait_until_ready = lambda process: None
            proxy.on_enable = manager._cancel.set

            manager._run()

            self.assertEqual(manager.status()["state"], "cancelled")
            self.assertEqual(proxy.restore_calls, 1)
            self.assertEqual(saved, [])

    def test_timeout_restores_proxy(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager, proxy, saved = self.make_manager(Path(temp_dir), timeout=0)
            process = FakeProcess()
            manager._start_proxy_process = lambda capture_id: process
            manager._wait_until_ready = lambda process: None

            manager._run()

            self.assertEqual(manager.status()["state"], "timed_out")
            self.assertEqual(proxy.restore_calls, 1)
            self.assertEqual(saved, [])

    def test_start_rejects_job_conflict_and_duplicate_capture(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager, _proxy, _saved = self.make_manager(Path(temp_dir), jobs=lambda: True)
            ok, _message = manager.start()
            self.assertFalse(ok)

            blocker = threading.Event()
            manager.has_active_jobs = lambda: False
            manager._run = lambda: blocker.wait(0.5)
            ok, _message = manager.start()
            self.assertTrue(ok)
            second_ok, _message = manager.start()
            self.assertFalse(second_ok)
            blocker.set()
            manager._thread.join(timeout=1)

    def test_loopback_control_guard(self):
        self.assertTrue(is_loopback_request("127.0.0.1", "localhost"))
        self.assertTrue(is_loopback_request("::1", None))
        self.assertFalse(is_loopback_request("192.168.1.5", "localhost"))
        self.assertFalse(is_loopback_request("127.0.0.1", "evil.example"))


if __name__ == "__main__":
    unittest.main()
