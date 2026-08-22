"""M3Undle's settings-archive implementation for the generic lab command."""

from __future__ import annotations

import json
import mimetypes
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from agent import common as lab_common
from agent.settings.plugin import SettingsCapability, SettingsPlugin


class M3UndleSettings(SettingsPlugin):
    """Call M3Undle's settings archive API without encoding product details in se-lab core."""

    def capability(self) -> SettingsCapability:
        status, _ = _request("/api/v1/restore/apply-settings", method="OPTIONS")
        # ASP.NET's method-mismatch response advertises POST on images that include the endpoint.
        return "settings-only" if status in (200, 204, 405) else "unsupported"

    def default_export_filename(self) -> str:
        return "settings-backup.m3undle-backup"

    def export_settings(self, out_path: Path) -> None:
        status, body = _request("/api/v1/backups?scope=settings", method="POST")
        if status != 200:
            raise SystemExit(_request_failure("create settings archive", status, body))
        response = _json_response(body, "create settings archive")
        file_name = response.get("fileName")
        if not isinstance(file_name, str) or not file_name:
            raise SystemExit("M3Undle did not return a settings archive filename.")

        status, contents = _request_bytes(f"/api/v1/backups/{file_name}/download")
        if status != 200:
            raise SystemExit(_request_failure("download settings archive", status, contents.decode(errors="replace")))
        out_path.write_bytes(contents)

    def import_settings(self, archive_path: Path) -> dict:
        upload_body, content_type = _multipart_body(archive_path)
        status, body = _request(
            "/api/v1/backups/upload",
            method="POST",
            data=upload_body,
            headers={"Content-Type": content_type, "X-Requested-With": "m3undle-lab"},
        )
        if status != 200:
            raise SystemExit(_request_failure("upload settings archive", status, body))
        uploaded = _json_response(body, "upload settings archive")
        file_name = uploaded.get("fileName")
        if not isinstance(file_name, str) or not file_name:
            raise SystemExit("M3Undle did not return an uploaded archive filename.")
        if uploaded.get("valid") is not True:
            errors = uploaded.get("validationErrors", [])
            raise SystemExit(f"Uploaded settings archive failed validation: {errors}")

        status, body = _request(
            "/api/v1/restore/apply-settings",
            method="POST",
            data=json.dumps({"fileName": file_name}).encode(),
            headers={"Content-Type": "application/json"},
        )
        response = _json_response(body, "apply settings archive")
        if status != 200 or response.get("success") is not True:
            raise SystemExit(_request_failure("apply settings archive", status, str(response.get("errors", body))))
        counts = response.get("appliedCounts")
        if not isinstance(counts, dict):
            raise SystemExit("M3Undle did not return settings import counts.")
        return counts


def _base_url() -> str:
    return lab_common.resolve_setting("M3UNDLE_BASE_URL", default="http://127.0.0.1:8080") or "http://127.0.0.1:8080"


def _request(path: str, *, method: str, data: bytes | None = None, headers: dict[str, str] | None = None) -> tuple[int, str]:
    status, body = _request_bytes(path, method=method, data=data, headers=headers)
    return status, body.decode("utf-8", errors="replace")


def _request_bytes(path: str, *, method: str = "GET", data: bytes | None = None, headers: dict[str, str] | None = None) -> tuple[int, bytes]:
    request = urllib.request.Request(f"{_base_url().rstrip('/')}{path}", data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()
    except urllib.error.URLError as error:
        return 0, str(error.reason).encode()


def _json_response(body: str, operation: str) -> dict[str, Any]:
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as error:
        raise SystemExit(f"M3Undle returned invalid JSON while attempting to {operation}: {error}") from error
    if not isinstance(parsed, dict):
        raise SystemExit(f"M3Undle returned an invalid response while attempting to {operation}.")
    return parsed


def _request_failure(operation: str, status: int, detail: str) -> str:
    return f"M3Undle could not {operation} (HTTP {status or 'unavailable'}): {detail[:500]}"


def _multipart_body(archive_path: Path) -> tuple[bytes, str]:
    boundary = f"----m3undle-lab-{uuid.uuid4().hex}"
    content_type = mimetypes.guess_type(archive_path.name)[0] or "application/octet-stream"
    contents = archive_path.read_bytes()
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{archive_path.name}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode()
    return head + contents + f"\r\n--{boundary}--\r\n".encode(), f"multipart/form-data; boundary={boundary}"
