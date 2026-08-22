"""Port of the frozen auth-gate suite, registered case-by-case with se-lab."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from agent import common as lab_common
from agent.container import wait_up
from agent.suites import suite

from m3undle_lab.commands import CONTAINER_NAME, HOST_OVERRIDE


SUITE = suite("auth-gate", group="core", order=100)
AUTH_ENABLED_ENV = "M3UNDLE_AUTH_ENABLED"
ADMIN_USER_ENV = "M3UNDLE_ADMIN_USER"
ADMIN_PASSWORD_ENV = "M3UNDLE_ADMIN_PASSWORD"
LAB_ADMIN_USER = "labadmin"
LAB_ADMIN_PASSWORD = "Lab-AuthGate-1!"
API_REJECT = (401,)
REDIRECT_REJECT = (302, 401)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request: Any, fp: Any, code: int, msg: str, headers: Any, new_url: str) -> None:
        return None


def _request(base_url: str, path: str, *, method: str = "GET", body: object | None = None) -> tuple[int, str]:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Accept": "application/json"}
    if data:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(f"{base_url.rstrip('/')}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.build_opener(_NoRedirectHandler).open(request, timeout=15) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as error:
        return 0, str(error.reason)


def _restart_with_auth(base_url: str, state: dict[str, object]) -> tuple[bool, str]:
    runtime_env = lab_common.load_env_file(lab_common.runtime_env_file())
    original = {key: runtime_env.get(key) for key in (AUTH_ENABLED_ENV, ADMIN_USER_ENV, ADMIN_PASSWORD_ENV)}
    state["original"] = original
    lab_common.set_runtime_env_values({
        AUTH_ENABLED_ENV: "true",
        ADMIN_USER_ENV: original[ADMIN_USER_ENV] or LAB_ADMIN_USER,
        ADMIN_PASSWORD_ENV: original[ADMIN_PASSWORD_ENV] or LAB_ADMIN_PASSWORD,
    })
    lab_common.compose_up(extra_compose_files=[HOST_OVERRIDE])
    if wait_up(base_url, CONTAINER_NAME, health_paths=("/livez", "/health")):
        return True, "restarted with UI authentication enabled"
    return False, "stack did not become healthy after enabling UI authentication"


def _restore(base_url: str, state: dict[str, object]) -> tuple[bool, str]:
    original = state.get("original")
    if not isinstance(original, dict):
        return True, "no runtime authentication values needed restoration"
    updates: dict[str, str] = {}
    for key, value in original.items():
        if value is None:
            lab_common.unset_runtime_env_var(key)
        else:
            updates[key] = str(value)
    if updates:
        lab_common.write_env_file_values(lab_common.runtime_env_file(), updates)
    lab_common.compose_up(extra_compose_files=[HOST_OVERRIDE])
    return wait_up(base_url, CONTAINER_NAME, health_paths=("/livez", "/health")), "restored original authentication environment"


@SUITE.setup
def setup(base_url: str) -> dict[str, object]:
    _request(base_url, "/api/v1/settings/endpoint-security", method="PUT", body={"enabled": False})
    return {"state": {}}


@SUITE.case("AUTH-01")
def auth_01(ctx, base_url: str) -> None:
    status, _ = _request(base_url, "/api/v1/settings/endpoint-security")
    ctx.record("AUTH-01", status == 200, f"gate disabled request returned {status} (expected 200)")


@SUITE.case("AUTH-02")
def auth_02(ctx, base_url: str, state: dict[str, object]) -> None:
    started, detail = _restart_with_auth(base_url, state)
    if not started:
        ctx.fail("AUTH-02", detail)
        state["gate_ready"] = False
        return
    state["gate_ready"] = True
    status, _ = _request(base_url, "/api/v1/settings/endpoint-security")
    ctx.record("AUTH-02", status in API_REJECT, f"expected {API_REJECT}, got {status}")


def _gate_ready(ctx, state: dict[str, object], test_id: str) -> bool:
    if state.get("gate_ready"):
        return True
    ctx.skip(test_id, "Skipped because M3Undle could not restart with UI authentication enabled")
    return False


@SUITE.case("AUTH-03")
def auth_03(ctx, base_url: str, state: dict[str, object]) -> None:
    if _gate_ready(ctx, state, "AUTH-03"):
        status, _ = _request(base_url, "/api/v1/encryption/status")
        ctx.record("AUTH-03", status in API_REJECT, f"expected {API_REJECT}, got {status}")


@SUITE.case("AUTH-04")
def auth_04(ctx, base_url: str, state: dict[str, object]) -> None:
    if _gate_ready(ctx, state, "AUTH-04"):
        status, _ = _request(base_url, "/api/v1/settings/endpoint-security", method="PUT", body={"enabled": False})
        ctx.record("AUTH-04", status in API_REJECT, f"expected {API_REJECT}, got {status}")


@SUITE.case("AUTH-05")
def auth_05(ctx, base_url: str, state: dict[str, object]) -> None:
    if _gate_ready(ctx, state, "AUTH-05"):
        status, _ = _request(base_url, "/debug/streams/strikes")
        ctx.record("AUTH-05", status in REDIRECT_REJECT, f"expected {REDIRECT_REJECT}, got {status}")


@SUITE.case("AUTH-06")
def auth_06(ctx, base_url: str, state: dict[str, object]) -> None:
    if _gate_ready(ctx, state, "AUTH-06"):
        status, body = _request(base_url, "/m3u/m3undle.m3u")
        valid = status in (200, 503) and not (status == 200 and "#EXTM3U" not in body)
        ctx.record("AUTH-06", valid, f"expected client delivery without UI auth, got {status}")


@SUITE.case("AUTH-07")
def auth_07(ctx, base_url: str, state: dict[str, object]) -> None:
    if _gate_ready(ctx, state, "AUTH-07"):
        status, _ = _request(base_url, "/health/ready")
        ctx.record("AUTH-07", status in (200, 503), f"expected 200 or 503, got {status}")


@SUITE.case("AUTH-RESTORE")
def auth_restore(ctx, base_url: str, state: dict[str, object]) -> None:
    restored, detail = _restore(base_url, state)
    ctx.record("AUTH-RESTORE", restored, detail)
