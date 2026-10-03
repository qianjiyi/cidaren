from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from cidaren import web


class WebApiTests(unittest.TestCase):
    def setUp(self):
        web.app.config.update(TESTING=True)
        self.client = web.app.test_client()

    def test_index_contains_capture_and_task_controls(self):
        response = self.client.get("/")
        text = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn('id="capture-start"', text)
        self.assertIn('id="capture-cancel"', text)
        self.assertIn('id="task-card"', text)

    def test_capture_status_never_returns_credentials(self):
        with patch.object(
            web.CAPTURE,
            "status",
            return_value={"state": "waiting", "message": "等待请求", "active": True, "can_cancel": True},
        ):
            response = self.client.get("/api/auth/capture/status")

        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("USERTOKEN", body)
        self.assertNotIn("AUTH_V", body)

    def test_capture_control_rejects_non_local_origin(self):
        response = self.client.post(
            "/api/auth/capture/start",
            json={},
            headers={"Origin": "https://evil.example"},
        )

        self.assertEqual(response.status_code, 403)

    def test_task_endpoints_pause_while_capturing(self):
        with patch.object(web.CAPTURE, "is_active", return_value=True):
            task_list = self.client.get("/api/tasks")
            start_one = self.client.post("/api/start", json={})
            start_all = self.client.post("/api/start_all", json={})

        self.assertEqual(task_list.status_code, 409)
        self.assertTrue(task_list.get_json()["capturing"])
        self.assertEqual(start_one.status_code, 409)
        self.assertEqual(start_all.status_code, 409)

    def test_web_client_uses_web_implementation_with_captured_user_agent(self):
        config = {
            "USERTOKEN": "token",
            "ABC": "abc",
            "AUTH_V": "auth",
            "USER_AGENT": "captured ua",
        }
        with patch.object(web.quiz, "Client") as client_type:
            web._client(config)

        client_type.assert_called_once_with("token", "abc", "auth", ua="captured ua")

    def test_captured_credentials_are_validated_through_web_client(self):
        credentials = {
            "USERTOKEN": "token",
            "ABC": "abc",
            "AUTH_V": "auth",
            "USER_AGENT": "captured ua",
        }
        client = MagicMock()
        client.main_info.return_value = {
            "code": 1,
            "data": {"user_info": {"student_name": "测试用户"}},
        }
        with patch.object(web.quiz, "Client", return_value=client) as client_type:
            valid, identity = web._validate_captured_credentials(credentials)

        self.assertTrue(valid)
        self.assertEqual(identity, "测试用户")
        client_type.assert_called_once_with("token", "abc", "auth", ua="captured ua")
        client.main_info.assert_called_once_with()

    def test_capture_start_reports_running_job_conflict(self):
        status = {"state": "idle", "message": "等待", "active": False, "can_cancel": False}
        with patch.object(web.CAPTURE, "start", return_value=(False, "存在运行中的任务")), patch.object(
            web.CAPTURE, "status", return_value=status
        ):
            response = self.client.post("/api/auth/capture/start", json={})

        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.get_json()["ok"])


if __name__ == "__main__":
    unittest.main()
