"""mitmproxy addon used by the local credential capture flow.

The addon deliberately inspects only requests to app.vocabgo.com and never
prints credentials. A complete set must come from one request.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from mitmproxy import ctx, http


RESULT_ENV = "CIDAREN_CAPTURE_RESULT"
CAPTURE_ID_ENV = "CIDAREN_CAPTURE_ID"


def _atomic_write_json(path: Path, payload: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False)
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


class CredentialCaptureAddon:
    def request(self, flow: http.HTTPFlow) -> None:
        if flow.request.pretty_host.lower() != "app.vocabgo.com":
            return

        headers = flow.request.headers
        payload = {
            "USERTOKEN": (headers.get("UserToken") or "").strip(),
            "ABC": (headers.get("Abc") or "").strip(),
            "AUTH_V": (headers.get("Authorization-V") or "").strip(),
            "USER_AGENT": (headers.get("User-Agent") or "").strip(),
            "capture_id": os.environ.get(CAPTURE_ID_ENV, ""),
        }
        if not all(payload[key] for key in ("USERTOKEN", "ABC", "AUTH_V")):
            return

        result_name = os.environ.get(RESULT_ENV)
        if not result_name:
            return
        result_path = Path(result_name)
        if result_path.exists():
            return

        _atomic_write_json(result_path, payload)
        ctx.master.shutdown()


addons = [CredentialCaptureAddon()]
