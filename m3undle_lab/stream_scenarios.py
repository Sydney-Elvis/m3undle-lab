"""Shared helpers for the stream-scenario suites (clean-relay, cooldown,
diagnostics, health, safe-start, limit), ported from the frozen lab's
scripts/srv1/run_stream_limit_scenarios.py and its four dependent scripts.

Factored into a real m3undle_lab module (mirroring api.py/simulator.py)
instead of the frozen scripts' own sys.path-based file-to-file imports,
which agent.suites.discover_suites()'s per-file test_*.py discovery isn't
designed around.

Deliberately smaller than the frozen scripts' combined helper set: gateway
detection (detect_docker_gateway/container_can_reach), simulator-process
lifecycle (start_provider_sim), and provider setup/snapshot orchestration
(setup_provider_and_snapshot) already have working, already-verified
equivalents this migration built earlier this session --
agent.container.get_docker_gateway(), m3undle_lab.simulator.SimulatorInstance,
and m3undle_lab.api.M3UndleClient.setup() -- so those are reused directly by
each suite rather than re-ported. What's left here is the one real gap: raw
JSON HTTP against arbitrary absolute URLs (the provider simulator's own
/debug/* endpoints, not M3Undle's API, so M3UndleClient's base-URL-relative
methods don't apply), pure stdlib urllib rather than a new `requests`
dependency this lab doesn't otherwise have.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any


def get_json(url: str, timeout: float = 15.0) -> tuple[int | None, Any]:
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as resp:
            body = resp.read()
            try:
                return resp.status, json.loads(body)
            except ValueError:
                return resp.status, body.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            return exc.code, json.loads(body)
        except ValueError:
            return exc.code, body.decode("utf-8", errors="replace")
    except Exception as exc:
        return None, str(exc)


def post_json(url: str, body: Any = None, timeout: float = 15.0) -> tuple[int | None, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else b""
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            response_body = resp.read()
            try:
                return resp.status, json.loads(response_body)
            except ValueError:
                return resp.status, response_body.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        response_body = exc.read()
        try:
            return exc.code, json.loads(response_body)
        except ValueError:
            return exc.code, response_body.decode("utf-8", errors="replace")
    except Exception as exc:
        return None, str(exc)


def read_stream_bytes(url: str, num_bytes: int, timeout: float = 20.0) -> bytes:
    """Open url, read up to num_bytes, close. Empty bytes on any failure --
    callers treat "no data" as a normal, assertable outcome, not an error."""
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as resp:
            buf = bytearray()
            while len(buf) < num_bytes:
                chunk = resp.read(min(8192, num_bytes - len(buf)))
                if not chunk:
                    break
                buf.extend(chunk)
            return bytes(buf)
    except Exception:
        return b""


def stream_status(url: str, *, read_timeout_seconds: float = 20.0) -> tuple[int | None, str]:
    """Open url, read a small amount (enough to surface startup failures
    without holding the connection for long), close. Returns (status,
    Retry-After header value)."""
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=read_timeout_seconds) as resp:
            resp.read(512)
            return resp.status, resp.headers.get("Retry-After", "")
    except urllib.error.HTTPError as exc:
        exc.read()
        return exc.code, exc.headers.get("Retry-After", "") if exc.headers else ""
    except Exception:
        return None, ""


def iter_chunks_with_deadline(
    response: Any,
    *,
    chunk_size: int,
    deadline_seconds: float = 60.0,
    progress_interval_seconds: float = 15.0,
):
    """Yield chunks read from an already-open urllib response within an
    absolute wall-clock observation window.

    A socket read timeout only bounds inactivity between chunks; a slow
    trickle of bytes could otherwise keep a scenario alive indefinitely.
    This wall-clock bound preserves the scenario's assertions while
    guaranteeing partial evidence still gets assessed.
    """
    started = time.monotonic()
    deadline = started + deadline_seconds
    next_progress = started + progress_interval_seconds
    while True:
        now = time.monotonic()
        if now >= deadline:
            print(f"  Capture observation window reached ({deadline_seconds:.0f} seconds).", flush=True)
            return
        if now >= next_progress:
            print(f"  Still capturing downstream evidence ({now - started:.0f} seconds elapsed)...", flush=True)
            next_progress = now + progress_interval_seconds
        chunk = response.read(chunk_size)
        if not chunk:
            return
        yield chunk
