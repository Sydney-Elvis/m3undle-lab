"""Port of the 3 real, non-redundant checks from the frozen
scripts/srv1/run_stream_proxy.py ("Alpha 4 Validation -- Stream Proxy" section),
out of the 7 originally registered there:

  SP1         Concurrent clients on the same channel collapse to one upstream
              session (fan-out, not one upstream connection per client)
  SP3         A late joiner on an already-open channel gets data without a
              second upstream session being opened
  HLS-CAP-01  An Xtream-path stream counts against the same provider
              concurrency cap as a direct M3U stream

SP2/SP4/SP5 are permanent ctx.skip()s in the original script itself (no VOD in
the simulator; no eviction trigger; needs an unbuilt provider-B stall
fixture) -- nothing real to preserve. SP6 asserts the identical cap-then-503
behavior test_stream_limit.py already covers, so it's not ported here to
avoid duplicate coverage.

Same collector-replay structural adaptation as test_stream_limit.py: each
scenario ported with unchanged assertion logic, run once from @SUITE.setup
against a _RecordCollector, each registered case replaying its own
precomputed result.
"""

from __future__ import annotations

import platform
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from agent.container import get_docker_gateway
from agent.suites import suite

from m3undle_lab.api import M3UndleClient
from m3undle_lab.simulator import SimulatorInstance

SUITE = suite("stream-proxy", group="core", order=151)
SIM_PORT = 19102
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "providers" / "provider-a.json"
PROVIDER_NAME = "provider-stream-proxy"
XTREAM_USER = "streamproxyuser"
XTREAM_PASS = "streamproxypass"


def _simulator_address() -> tuple[str, str | None]:
    if platform.system() == "Darwin":
        return "0.0.0.0", f"http://host.docker.internal:{SIM_PORT}"
    gateway = get_docker_gateway("m3undle-lab_media")
    if gateway:
        return "0.0.0.0", f"http://{gateway}:{SIM_PORT}"
    return "127.0.0.1", None


class _RecordCollector:
    def __init__(self) -> None:
        self.records: dict[str, tuple[bool | None, str, Any]] = {}

    def record(self, name: str, passed: bool | None, message: str, detail: Any = None) -> None:
        self.records[name] = (passed, message, detail)


def _run_sp1(ctx: _RecordCollector, client: M3UndleClient, stream_urls: list[str]) -> None:
    """SP1: 3 concurrent clients on the same channel multiplex through one upstream session."""
    url = stream_urls[0]
    errors: list[str] = []
    connected: list[bool] = []

    def read_stream(duration: float) -> None:
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=duration + 5) as resp:
                connected.append(True)
                end = time.monotonic() + duration
                while time.monotonic() < end:
                    chunk = resp.read(1024)
                    if not chunk:
                        break
        except urllib.error.HTTPError as exc:
            connected.append(False)
            errors.append(f"HTTP {exc.code}")
        except Exception as exc:
            connected.append(False)
            errors.append(str(exc))

    threads = [threading.Thread(target=read_stream, args=(6.0,), daemon=True) for _ in range(3)]
    for t in threads:
        t.start()

    time.sleep(2.5)
    sessions = client.get_active_sessions()
    upstream_count = len(sessions)

    for t in threads:
        t.join(timeout=8.0)

    connection_count = sum(1 for c in connected if c)

    if connection_count >= 1 and upstream_count <= 1:
        ctx.record(
            "SP1-shared-upstream", True,
            f"{connection_count}/3 clients connected, {upstream_count} upstream session -- fan-out working",
        )
    elif connection_count < 1:
        ctx.record("SP1-shared-upstream", False, f"No clients connected (errors: {errors[:3]})")
    else:
        ctx.record(
            "SP1-shared-upstream", False,
            f"{connection_count} clients but {upstream_count} upstream sessions (expected <=1)",
            sessions,
        )


def _run_sp3(ctx: _RecordCollector, client: M3UndleClient, stream_urls: list[str]) -> None:
    """SP3: a late joiner on an already-open channel gets data without a second upstream session."""
    url = stream_urls[0]
    client1_chunks: list[int] = []
    client2_chunks: list[int] = []
    errors: list[str] = []

    def read_stream(result: list[int], duration: float) -> None:
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=duration + 5) as resp:
                end = time.monotonic() + duration
                while time.monotonic() < end:
                    chunk = resp.read(1024)
                    if not chunk:
                        break
                    result.append(len(chunk))
        except Exception as exc:
            errors.append(str(exc))

    t1 = threading.Thread(target=read_stream, args=(client1_chunks, 8.0), daemon=True)
    t1.start()
    time.sleep(2.0)

    t2 = threading.Thread(target=read_stream, args=(client2_chunks, 5.0), daemon=True)
    t2.start()

    time.sleep(1.5)
    sessions = client.get_active_sessions()
    upstream_count = len(sessions)

    t1.join(timeout=10.0)
    t2.join(timeout=8.0)

    c1_bytes = sum(client1_chunks)
    c2_bytes = sum(client2_chunks)

    if c1_bytes > 0 and c2_bytes > 0 and upstream_count <= 1:
        ctx.record(
            "SP3-late-joiner", True,
            f"Client1={c1_bytes}B, late-joiner={c2_bytes}B, upstream_sessions={upstream_count}",
        )
    elif c1_bytes == 0:
        ctx.record("SP3-late-joiner", False, f"Client 1 received no data (errors: {errors[:2]})")
    elif c2_bytes == 0:
        ctx.record("SP3-late-joiner", False, f"Late joiner received no data (errors: {errors[:2]})")
    else:
        ctx.record(
            "SP3-late-joiner", False,
            f"Unexpected upstream count {upstream_count} (expected <=1), errors: {errors[:2]}",
            sessions,
        )


def _run_hls_cap_01(ctx: _RecordCollector, client: M3UndleClient, stream_urls: list[str]) -> None:
    """HLS-CAP-01: an Xtream-path stream counts against the same provider cap as a direct stream."""
    if len(stream_urls) < 3:
        ctx.record(
            "HLS-CAP-01", False,
            f"Need >=3 stream URLs, found {len(stream_urls)} -- fixture may have fewer channels",
        )
        return

    auth_status, _ = client._request("PUT", "/api/v1/settings/endpoint-security", body={
        "enabled": True,
        "username": XTREAM_USER,
        "password": XTREAM_PASS,
    })
    if auth_status not in (200, 204):
        ctx.record("HLS-CAP-01", False, f"Could not enable endpoint-security: status={auth_status}")
        return

    xtr_status, xtr_body = client._request(
        "GET",
        f"/player_api.php?username={XTREAM_USER}&password={XTREAM_PASS}&action=get_live_streams",
    )
    if xtr_status != 200 or not isinstance(xtr_body, list) or len(xtr_body) < 3:
        client._request("PUT", "/api/v1/settings/endpoint-security", body={"enabled": False})
        ctx.record(
            "HLS-CAP-01", False,
            f"Xtream get_live_streams returned status={xtr_status}"
            f" count={len(xtr_body) if isinstance(xtr_body, list) else 'n/a'}",
        )
        return

    m3undle_base = client.base_url
    stream_id_b = xtr_body[1].get("stream_id") if isinstance(xtr_body[1], dict) else None
    stream_id_c = xtr_body[2].get("stream_id") if isinstance(xtr_body[2], dict) else None

    if not stream_id_b or not stream_id_c:
        client._request("PUT", "/api/v1/settings/endpoint-security", body={"enabled": False})
        ctx.record("HLS-CAP-01", False, "Could not resolve Xtream stream IDs for channels B and C")
        return

    # The M3U (and the direct stream paths it publishes) is itself gated once
    # endpoint-security is on -- established pattern from test_security.py/test_xtream.py
    # is the credentialed query string. Must re-fetch here rather than reuse the
    # unauthenticated `stream_urls` captured before auth was enabled, or channel A's
    # direct URL 401s.
    _, authed_m3u = client.get(f"/m3u/m3undle.m3u?username={XTREAM_USER}&password={XTREAM_PASS}")
    authed_m3u_text = authed_m3u if isinstance(authed_m3u, str) else ""
    known_paths = ("/stream/", "/live/", "/tune/", "/hdhr/tune/")
    authed_stream_urls = [
        line.strip()
        for line in authed_m3u_text.splitlines()
        if line.startswith("http") and any(p in line for p in known_paths)
    ]
    if not authed_stream_urls:
        client._request("PUT", "/api/v1/settings/endpoint-security", body={"enabled": False})
        ctx.record("HLS-CAP-01", False, "No stream URLs in the authenticated M3U for channel A")
        return

    xtream_url_b = f"{m3undle_base}/live/{XTREAM_USER}/{XTREAM_PASS}/{stream_id_b}.ts"
    xtream_url_c = f"{m3undle_base}/live/{XTREAM_USER}/{XTREAM_PASS}/{stream_id_c}.ts"

    client.reset_debug_state()
    time.sleep(1.0)

    opened: list[bool] = []
    errors: list[str] = []

    def hold_stream(url: str, idx: int) -> None:
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=10.0) as resp:
                opened.append(True)
                time.sleep(6.0)
        except urllib.error.HTTPError as exc:
            opened.append(False)
            errors.append(f"client-{idx}: HTTP {exc.code}")
        except Exception as exc:
            opened.append(False)
            errors.append(f"client-{idx}: {exc}")

    # Channel A: direct M3U stream URL (authenticated). Channel B: Xtream path-credential URL.
    threads = [
        threading.Thread(target=hold_stream, args=(authed_stream_urls[0], 0), daemon=True),
        threading.Thread(target=hold_stream, args=(xtream_url_b, 1), daemon=True),
    ]
    for t in threads:
        t.start()

    time.sleep(2.5)

    # Channel C: third stream -- should be rejected (cap=2 already taken).
    try:
        req = urllib.request.Request(xtream_url_c)
        with urllib.request.urlopen(req, timeout=8.0) as resp:
            status3 = resp.status
            body3: object = None
    except urllib.error.HTTPError as exc:
        status3 = exc.code
        body3 = None
    except Exception as exc:
        status3 = 0
        body3 = str(exc)

    for t in threads:
        t.join(timeout=9.0)

    # Disable auth before asserting so failures don't leave the stack in auth mode.
    client._request("PUT", "/api/v1/settings/endpoint-security", body={"enabled": False})

    sessions = client.get_active_sessions()

    if status3 in (429, 503):
        ctx.record(
            "HLS-CAP-01", True,
            f"3rd stream (Xtream channel C) correctly rejected HTTP {status3};"
            f" direct+Xtream together consumed cap (open={sum(opened)}, sessions={len(sessions)},"
            f" errors={errors[:2]})",
        )
    elif status3 == 200:
        ctx.record(
            "HLS-CAP-01", False,
            f"3rd stream was accepted (HTTP 200) -- Xtream path did not count toward provider cap"
            f" (open={sum(opened)}, errors={errors[:2]})",
            {"sessions": sessions},
        )
    else:
        ctx.record(
            "HLS-CAP-01", False,
            f"Unexpected response {status3} on 3rd stream: {body3} (errors={errors[:2]})",
        )


@SUITE.setup
def setup(base_url: str) -> dict[str, object]:
    collector = _RecordCollector()
    state: dict[str, object] = {"reason": "Stream-proxy suite setup did not complete", "records": collector.records}
    simulator: SimulatorInstance | None = None

    try:
        bind, public_host = _simulator_address()
        simulator = SimulatorInstance(fixture=FIXTURE, port=SIM_PORT, bind=bind, public_host=public_host, suite="stream-proxy")
        simulator.start()
        if not simulator.wait_healthy():
            state["reason"] = "Provider simulator did not become healthy"
            return {"state": state}

        client = M3UndleClient(base_url)
        if not client.setup(playlist_url=simulator.playlist_url, provider_name=PROVIDER_NAME, max_concurrent_streams=2):
            state["reason"] = client.last_setup_error or "Setup sequence failed"
            return {"state": state}

        stream_urls = client.get_stream_urls()
        if len(stream_urls) < 3:
            state["reason"] = f"need >= 3 stream urls, found {len(stream_urls)}"
            return {"state": state}

        _run_sp1(collector, client, stream_urls)
        _run_sp3(collector, client, stream_urls)
        _run_hls_cap_01(collector, client, stream_urls)

        if client.provider_id:
            client.delete_provider_with_retry(client.provider_id)

        state["reason"] = None
        return {"state": state}
    except Exception as exc:  # setup errors are reported as per-case skips below
        state["reason"] = f"Stream-proxy suite setup failed: {exc}"
        return {"state": state}
    finally:
        if simulator is not None:
            try:
                simulator.stop()
            except Exception:
                pass


def _register_case(case_id: str) -> None:
    @SUITE.case(case_id)
    def _replay(ctx, state):
        records: dict[str, tuple[bool | None, str, Any]] = state["records"]  # type: ignore[assignment]
        entry = records.get(case_id)
        if entry is None:
            ctx.skip(case_id, str(state.get("reason") or "Stream-proxy suite setup did not reach this case"))
            return
        passed, message, detail = entry
        ctx.record(case_id, passed, message, detail)


for _case_id in ("SP1-shared-upstream", "SP3-late-joiner", "HLS-CAP-01"):
    _register_case(_case_id)
