"""
M3Undle HTTP client and setup sequence for the lab harness.

Ported from m3undle-lab-private's scripts/common/harness/client.py (961
lines), verbatim -- unlike simulator.py, this file had zero dependencies on
the old lab's common.py/harness package (pure stdlib: json, mimetypes,
secrets, pathlib, threading, time, urllib), so there was nothing to adapt.
Named api.py rather than client.py to avoid colliding with this package's
existing clients.py (Jellyfin/NextPVR ClientPlugin registrations -- an
unrelated concept, se-lab's client-app orchestration, not this HTTP driver).

M3UndleClient wraps all M3Undle API calls used by test scripts and provides
a full setup() method that takes a simulator playlist URL and leaves M3Undle
ready for streaming tests.

Setup sequence:
  1. Reset debug state (streams + strikes)
  2. Upsert provider
  3. Activate provider
  4. Trigger snapshot refresh and poll to completion
  5. Fetch active profile
  6. Select all channels for that profile
  7. Build snapshot and poll to completion
  8. Wait for /health/ready
"""

from __future__ import annotations

import json
import mimetypes
import secrets
from pathlib import Path
from threading import Event, Thread
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


def backup_api_capability(status: int, body: object) -> tuple[str, str]:
    if status == 404:
        return "unsupported", "Portable backup API is not present in this M3Undle build"
    if status == 200 and isinstance(body, list):
        return "supported", "Portable backup API is available"
    return "error", f"Portable backup capability probe returned status={status} body={body}"


class HoldOpenStream:
    """Open a stream in the background and keep it attached for a short window."""

    def __init__(self, url: str, hold_seconds: float = 20.0, chunk_size: int = 8192) -> None:
        self.url = url
        self.hold_seconds = hold_seconds
        self.chunk_size = chunk_size
        self.status: int | None = None
        self.error: str | None = None
        self.bytes_read = 0
        self.finished = False
        self.finish_reason: str | None = None
        self._stop = Event()
        self._thread: Thread | None = None

    def start(self) -> None:
        def run() -> None:
            try:
                req = urllib.request.Request(self.url)
                with urllib.request.urlopen(req, timeout=15.0) as resp:
                    self.status = resp.status
                    if resp.status != 200:
                        self.finish_reason = f"http_{resp.status}"
                        return

                    start = time.monotonic()
                    while not self._stop.is_set() and (time.monotonic() - start) < self.hold_seconds:
                        chunk = resp.read(self.chunk_size)
                        if not chunk:
                            self.finish_reason = "eof"
                            return
                        self.bytes_read += len(chunk)

                    self.finish_reason = "stopped" if self._stop.is_set() else "timeout"
            except urllib.error.HTTPError as exc:
                self.status = exc.code
                self.finish_reason = f"http_{exc.code}"
            except Exception as exc:
                self.error = str(exc)
                self.finish_reason = "error"
            finally:
                self.finished = True

        self._thread = Thread(target=run, daemon=True)
        self._thread.start()

    def wait_header(self, timeout_seconds: float = 20.0) -> int | None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self.status is not None or self.error is not None:
                return self.status
            time.sleep(0.2)
        return None

    def wait_finished(self, timeout_seconds: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self.finished:
                return True
            time.sleep(0.2)
        return self.finished

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)


class M3UndleClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.provider_id: str | None = None
        self.profile_id: str | None = None
        self.last_setup_error: str | None = None

    # -----------------------------------------------------------------------
    # Low-level HTTP
    # -----------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        timeout: float = 15.0,
        ok_statuses: tuple[int, ...] = (200,),
    ) -> tuple[int, Any]:
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode() if body is not None else None
        headers = (
            {"Content-Type": "application/json", "Accept": "application/json"}
            if data
            else {"Accept": "application/json"}
        )
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                try:
                    return resp.status, json.loads(raw)
                except json.JSONDecodeError:
                    return resp.status, raw.decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw)
            except json.JSONDecodeError:
                return exc.code, raw.decode("utf-8", errors="replace")

    def get(self, path: str, **kwargs: Any) -> tuple[int, Any]:
        return self._request("GET", path, **kwargs)

    def post(self, path: str, body: Any = None, **kwargs: Any) -> tuple[int, Any]:
        return self._request("POST", path, body=body, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> tuple[int, Any]:
        return self._request("DELETE", path, **kwargs)

    def download_bytes(self, path: str, *, timeout: float = 60.0) -> tuple[int, bytes]:
        """Download an opaque response without JSON or text decoding."""
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            headers={"Accept": "application/octet-stream"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def upload_file(
        self,
        path: str,
        file_path: str | Path,
        *,
        field_name: str = "file",
        timeout: float = 120.0,
    ) -> tuple[int, Any]:
        """Upload one file as multipart/form-data for the backup import API."""
        source = Path(file_path)
        boundary = f"----m3undle-lab-{secrets.token_hex(16)}"
        content_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
        prefix = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{field_name}"; filename="{source.name}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode("utf-8")
        payload = prefix + source.read_bytes() + f"\r\n--{boundary}--\r\n".encode("ascii")
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=payload,
            headers={
                "Accept": "application/json",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(len(payload)),
                "X-Requested-With": "M3UndleLab",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                try:
                    return resp.status, json.loads(raw)
                except json.JSONDecodeError:
                    return resp.status, raw.decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw)
            except json.JSONDecodeError:
                return exc.code, raw.decode("utf-8", errors="replace")

    # -----------------------------------------------------------------------
    # Debug / test-mode helpers
    # -----------------------------------------------------------------------

    def reset_debug_state(self) -> None:
        """Clear active stream sessions and strike/cooldown state."""
        self.post("/debug/streams/reset")
        self.post("/debug/strikes/reset")

    def get_active_sessions(self) -> list:
        """Return list of active stream session dicts from /status/streams."""
        _, body = self.get("/status/streams")
        if isinstance(body, list):
            sessions = body
        elif isinstance(body, dict):
            sessions = body.get("ActiveSessions") or body.get("activeSessions") or []
        else:
            sessions = []
        active_states = {"Active", "Connecting", "Initializing", "Live", "Reconnecting"}
        active_state_values = {0, 1, 2, 3}
        return [
            s for s in sessions
            if isinstance(s, dict)
            and (
                str(s.get("state") or s.get("State") or "") in active_states
                or (s.get("state") if "state" in s else s.get("State")) in active_state_values
            )
        ]

    def get_session_status(self, session_id: str) -> dict | None:
        status, body = self.get(f"/status/streams/{session_id}")
        return body if status == 200 and isinstance(body, dict) else None

    def wait_for_active_session_count(
        self,
        minimum_count: int = 1,
        *,
        timeout_seconds: float = 20.0,
    ) -> list[dict]:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            sessions = self.get_active_sessions()
            if len(sessions) >= minimum_count:
                return sessions
            time.sleep(0.5)
        return []

    def get_strikes(self) -> list:
        """Return list of channel strike/cooldown entries."""
        _, body = self.get("/debug/streams/strikes")
        return body if isinstance(body, list) else []

    def get_stream_events(self, **filters: str) -> list:
        """Return recent stream diagnostic events, optionally filtered by query args."""
        if filters:
            query = "&".join(f"{key}={urllib.parse.quote(value)}" for key, value in filters.items())
            path = f"/status/streams/events?{query}"
        else:
            path = "/status/streams/events"
        _, body = self.get(path)
        return body if isinstance(body, list) else []

    def get_stream_rca_bundle(self) -> dict | None:
        """Return the stream RCA debug bundle when test mode is enabled."""
        status, body = self.get("/debug/streams/rca")
        return body if status == 200 and isinstance(body, dict) else None

    # -----------------------------------------------------------------------
    # Provider and snapshot API
    # -----------------------------------------------------------------------

    def upsert_provider(
        self,
        *,
        name: str,
        playlist_url: str,
        max_concurrent_streams: int = 2,
        clean_relay_mode: str | None = None,
    ) -> dict:
        payload = {
            "name": name,
            "playlistUrl": playlist_url,
            "maxConcurrentStreams": max_concurrent_streams,
        }
        if clean_relay_mode is not None:
            payload["cleanRelayMode"] = clean_relay_mode
        status, body = self.post("/api/v1/providers/upsert", payload)
        if status not in (200, 201) or not isinstance(body, dict):
            raise RuntimeError(f"upsert_provider failed {status}: {body}")
        return body

    def create_xtream_provider(
        self,
        *,
        name: str,
        xtream_base_url: str,
        xtream_username: str,
        xtream_password: str,
        xtream_include_xmltv: bool = False,
        include_vod: bool = False,
        include_series: bool = False,
        max_concurrent_streams: int = 2,
    ) -> dict:
        status, body = self.post("/api/v1/providers", {
            "name": name,
            "xtreamBaseUrl": xtream_base_url,
            "xtreamUsername": xtream_username,
            "xtreamPassword": xtream_password,
            "xtreamIncludeXmltv": xtream_include_xmltv,
            "includeVod": include_vod,
            "includeSeries": include_series,
            "maxConcurrentStreams": max_concurrent_streams,
        })
        if status not in (200, 201) or not isinstance(body, dict):
            raise RuntimeError(f"create_xtream_provider failed {status}: {body}")
        return body

    def activate_provider(self, provider_id: str) -> None:
        """
        Enable the provider. In the current implementation, profile
        activation is a separate step.
        """
        status, body = self._request(
            "PATCH",
            f"/api/v1/providers/{provider_id}/enabled",
            body={"enabled": True},
        )
        if status not in (200, 204):
            status2, body2 = self._request(
                "PATCH",
                f"/api/v1/providers/{provider_id}/active",
                body={"isActive": True},
            )
            if status2 not in (200, 204):
                raise RuntimeError(f"activate_provider failed {status}/{status2}: {body} / {body2}")

    def list_profiles(self) -> list[dict]:
        status, body = self.get("/api/v1/profiles")
        return body if status == 200 and isinstance(body, list) else []

    def list_providers(self) -> list[dict]:
        status, body = self.get("/api/v1/providers")
        return body if status == 200 and isinstance(body, list) else []

    def delete_provider(self, provider_id: str) -> tuple[int, Any]:
        return self.delete(f"/api/v1/providers/{provider_id}")

    def delete_provider_with_retry(self, provider_id: str, *, max_wait: float = 60.0) -> bool:
        """Delete a single provider, retrying on 409 (a snapshot operation is
        still in flight) until it settles -- teardown for a scenario that
        drove its own provider outside clear_existing_providers()'s bulk
        cleanup loop."""
        deadline = time.monotonic() + max_wait
        while time.monotonic() < deadline:
            status, body = self.delete_provider(provider_id)
            if status in (200, 202, 204, 404):
                return True
            if status == 409:
                self.wait_snapshot_idle(timeout_seconds=20.0)
                time.sleep(1.0)
                continue
            print(f"Provider delete returned {status}: {body}", flush=True)
            return False
        return False

    def clear_existing_providers(self, *, timeout_seconds: float = 60.0) -> None:
        """
        Delete all configured providers before suite setup.

        The integration suites use short-lived local simulators. If an earlier
        suite leaves enabled providers behind, later refreshes can re-fetch
        those stale playlist URLs and poison the current setup run.
        """
        provider_ids = [
            str(item.get("providerId") or item.get("id") or "")
            for item in self.list_providers()
            if isinstance(item, dict)
        ]

        for provider_id in provider_ids:
            if not provider_id:
                continue

            deadline = time.monotonic() + timeout_seconds
            while time.monotonic() < deadline:
                status, body = self.delete_provider(provider_id)
                if status in (200, 202, 204, 404):
                    break
                if status == 409:
                    self.wait_snapshot_idle(timeout_seconds=20.0)
                    time.sleep(1.0)
                    continue
                raise RuntimeError(
                    f"provider cleanup failed for {provider_id}: status={status} body={body}"
                )
            else:
                raise RuntimeError(f"provider cleanup timed out for {provider_id}")

    def get_profile(self, profile_id: str) -> dict | None:
        profiles = self.list_profiles()
        return next(
            (
                item for item in profiles
                if isinstance(item, dict) and str(item.get("profileId") or item.get("id") or "") == profile_id
            ),
            None,
        )

    def get_active_profile_id_from_list(self) -> str | None:
        for item in self.list_profiles():
            if isinstance(item, dict) and item.get("isActive"):
                return str(item.get("profileId") or item.get("id") or "")
        return None

    def set_profile_enabled(self, profile_id: str, enabled: bool) -> tuple[int, Any]:
        return self._request("PATCH", f"/api/v1/profiles/{profile_id}/enabled", body={"enabled": enabled})

    def get_hdhr_settings(self) -> dict:
        status, body = self.get("/api/v1/settings/hdhr")
        return body if status == 200 and isinstance(body, dict) else {}

    def update_hdhr_settings(
        self,
        *,
        enabled: bool,
        tuner_count_override: int | None,
        advertised_base_url: str | None,
        discovery_enabled: bool,
        ssdp_enabled: bool,
        silicon_dust_discovery_enabled: bool,
    ) -> tuple[int, Any]:
        return self._request(
            "PUT",
            "/api/v1/settings/hdhr",
            body={
                "enabled": enabled,
                "tunerCountOverride": tuner_count_override,
                "advertisedBaseUrl": advertised_base_url,
                "discoveryEnabled": discovery_enabled,
                "ssdpEnabled": ssdp_enabled,
                "siliconDustDiscoveryEnabled": silicon_dust_discovery_enabled,
            },
        )

    def set_active_profile(self, profile_id: str) -> tuple[int, Any]:
        status, body = self._request("PUT", f"/api/v1/profiles/{profile_id}/active")
        if status not in (200, 204, 409):
            raise RuntimeError(f"set_active_profile failed {status}: {body}")
        return status, body

    def trigger_refresh(self) -> int:
        """
        Explicitly trigger a refresh.

        409 is treated as success — it means a refresh is already running
        (e.g. auto-triggered by activate_provider), so we just let the
        existing one complete.
        """
        status, body = self.post("/api/v1/snapshots/refresh")
        if status not in (200, 202, 409):
            raise RuntimeError(f"trigger_refresh failed {status}: {body}")
        return status

    def get_snapshot_status_info(self) -> dict:
        status, body = self.get("/api/v1/snapshots/status")
        return body if status == 200 and isinstance(body, dict) else {}

    def get_debug_snapshot_idle(self) -> dict:
        status, body = self.get("/debug/snapshots/idle")
        return body if status == 200 and isinstance(body, dict) else {}

    def poll_snapshot_status(
        self,
        *,
        timeout_seconds: float = 120.0,
        previous_started_utc: str | None = None,
    ) -> bool:
        """
        Poll until a snapshot refresh/build operation completes.

        In the current implementation the trigger endpoints do not return
        a run id, so callers
        can pass the previous startedUtc and wait for it to change. We use the
        test-mode /debug/snapshots/idle endpoint as the source of truth for
        "running vs idle" and /api/v1/snapshots/status for the completed
        result details.
        """
        deadline = time.monotonic() + timeout_seconds
        saw_new_run = previous_started_utc in (None, "")
        saw_running = False

        while time.monotonic() < deadline:
            idle_info = self.get_debug_snapshot_idle()
            status_info = self.get_snapshot_status_info()

            running = bool(idle_info.get("running")) if idle_info else bool(status_info.get("running"))
            current_started = str(status_info.get("startedUtc") or idle_info.get("startedUtc") or "")
            last = str(status_info.get("lastStatus") or idle_info.get("lastStatus") or "")
            seen = status_info.get("channelCountSeen")
            error = status_info.get("errorSummary")

            if previous_started_utc not in (None, "") and current_started and current_started != previous_started_utc:
                saw_new_run = True
            elif previous_started_utc in (None, "") and current_started:
                saw_new_run = True

            if running:
                saw_running = True
            elif saw_new_run and (saw_running or not running):
                if last in ("ok", "completed", "success", "not_modified"):
                    print(f"    Snapshot ok — channelCountSeen={seen}", flush=True)
                    return True
                if last and last not in ("running",):
                    print(
                        f"    Snapshot finished status={last!r}"
                        f" channelCountSeen={seen} error={error!r}",
                        flush=True,
                    )
                    return False

            time.sleep(1.0)
        return False

    def poll_build_completion(
        self,
        profile_id: str,
        *,
        timeout_seconds: float = 120.0,
    ) -> bool:
        """
        Wait for a build-only operation to settle.

        Build-only does not create a new snapshot FetchRun in the current
        implementation, so the
        snapshot status endpoint can remain pinned to the prior refresh result.
        For builds we instead wait for the system to go idle, then verify that
        the profile-scoped channels endpoint is available and readiness is
        restored.
        """
        deadline = time.monotonic() + timeout_seconds
        saw_running = False

        while time.monotonic() < deadline:
            idle_info = self.get_debug_snapshot_idle()
            running = bool(idle_info.get("running")) if idle_info else False

            if running:
                saw_running = True
            elif saw_running or idle_info.get("idle") is True:
                status, _ = self.get(
                    f"/api/v1/profiles/{profile_id}/channels?page=1&pageSize=10",
                    ok_statuses=(200, 404),
                )
                if status == 200 and self.poll_ready(timeout_seconds=10.0):
                    print("    Build settled and profile channels are available.", flush=True)
                    return True

            time.sleep(1.0)
        return False

    def get_active_profile(self) -> dict:
        """Return the profile linked to the active provider as {profileId, name}."""
        status, body = self.get("/api/v1/profiles/active")
        if status != 200 or not isinstance(body, dict):
            raise RuntimeError(f"get_active_profile failed {status}: {body}")
        return body

    def include_all_groups(self, profile_id: str) -> int:
        """
        Set every group filter that is in 'hold' state to 'include'.

        After a provider refresh all new groups land in 'hold', which means
        select-all-channels returns 0.  This call unblocks them.
        Returns the number of groups transitioned.
        """
        status, body = self.get(f"/api/v1/profiles/{profile_id}/group-filters")
        if status != 200 or not isinstance(body, list):
            raise RuntimeError(f"get group-filters failed {status}: {body}")

        print(f"    group-filters returned {len(body)} group(s)", flush=True)
        for f in body:
            print(
                f"      group={f.get('providerGroupRawName')!r}"
                f" decision={f.get('decision')!r}"
                f" channels={f.get('channelCount')}",
                flush=True,
            )

        count = 0
        for f in body:
            if f.get("decision") in ("hold", None, ""):
                filter_id = f.get("profileGroupFilterId")
                s, _ = self._request(
                    "PATCH",
                    f"/api/v1/profiles/{profile_id}/group-filters/{filter_id}",
                    body={"decision": "include"},
                )
                if s == 200:
                    count += 1
        return count

    def resolve_provider_profile_id(self, provider_id: str, provider_name: str) -> str | None:
        """
        Resolve the profile that is actually associated with the upserted provider.

        This avoids ambiguous name-based selection when multiple similarly named
        profiles already exist in the environment.
        """
        associated_profile_ids: set[str] = set()
        status, provider = self.get(f"/api/v1/providers/{provider_id}")
        if status == 200 and isinstance(provider, dict):
            ids = provider.get("associatedProfileIds")
            if isinstance(ids, list):
                associated_profile_ids = {str(pid) for pid in ids if pid}

        status, profiles = self.get("/api/v1/profiles")
        if status != 200 or not isinstance(profiles, list) or not profiles:
            return None

        if associated_profile_ids:
            for item in profiles:
                if not isinstance(item, dict):
                    continue
                profile_id = item.get("profileId") or item.get("id")
                if profile_id in associated_profile_ids:
                    return profile_id

        # Fallback for older payloads that do not expose associatedProfileIds.
        for item in profiles:
            if isinstance(item, dict) and item.get("name") == provider_name:
                return item.get("profileId") or item.get("id")

        first = profiles[0]
        if isinstance(first, dict):
            return first.get("profileId") or first.get("id")
        return None

    def select_all_channels(self, profile_id: str) -> int:
        status, body = self.post(f"/api/v1/profiles/{profile_id}/select-all-channels")
        if status != 200:
            raise RuntimeError(f"select_all_channels failed {status}: {body}")
        return body.get("channelsSelected", 0) if isinstance(body, dict) else 0

    def build_snapshot(self) -> None:
        status, body = self.post("/api/v1/snapshots/build")
        if status not in (200, 202):
            raise RuntimeError(f"build_snapshot failed {status}: {body}")

    def poll_ready(self, *, timeout_seconds: float = 120.0) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            status, body = self.get("/health/ready")
            if status == 200 and isinstance(body, dict) and body.get("ready"):
                return True
            time.sleep(2.0)
        return False

    def wait_snapshot_idle(self, *, timeout_seconds: float = 120.0) -> bool:
        """
        Wait until no snapshot operation is running.

        Prefer the test-mode debug endpoint because it reflects the refresh
        trigger directly. Fall back to /api/v1/snapshots/status if needed.
        """
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            body = self.get_debug_snapshot_idle()
            if body:
                if body.get("idle") is True:
                    return True
            else:
                status, status_body = self.get("/api/v1/snapshots/status")
                if status == 404:
                    return True
                if status == 200 and isinstance(status_body, dict) and not status_body.get("running", False):
                    return True
            time.sleep(1.0)
        return False

    def activate_profile_for_setup(
        self,
        profile_id: str,
        *,
        timeout_seconds: float = 180.0,
    ) -> tuple[bool, bool]:
        """
        Activate a profile, retrying through transient 409 conflicts.

        Returns (activated, refresh_triggered).
        """
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if not self.wait_snapshot_idle(timeout_seconds=10.0):
                continue
            status, body = self.set_active_profile(profile_id)
            if status in (200, 204):
                refresh_triggered = isinstance(body, dict) and bool(body.get("refreshTriggered"))
                return True, refresh_triggered
            elif status == 409:
                time.sleep(1.0)
                continue
            time.sleep(1.0)
        return False, False

    def wait_active_profile(self, expected_profile_id: str, *, timeout_seconds: float = 30.0) -> bool:
        """
        Wait for /api/v1/profiles/active to match expected_profile_id.
        """
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            status, body = self.get("/api/v1/profiles/active")
            if status == 200 and isinstance(body, dict):
                active = str(body.get("profileId") or body.get("id") or "")
                if active == expected_profile_id:
                    return True
            time.sleep(1.0)
        return False

    # -----------------------------------------------------------------------
    # Output helpers
    # -----------------------------------------------------------------------

    def get_stream_urls(self) -> list[str]:
        """
        Return all stream URLs from the published M3U.

        Matches any http(s) line that contains a known M3Undle stream path
        segment: /stream/, /live/, /tune/, /hdhr/tune/.
        """
        _, m3u = self.get("/m3u/m3undle.m3u")
        m3u_text = m3u if isinstance(m3u, str) else ""
        known_paths = ("/stream/", "/live/", "/tune/", "/hdhr/tune/")
        urls = [
            line.strip()
            for line in m3u_text.splitlines()
            if line.startswith("http") and any(p in line for p in known_paths)
        ]
        if not urls and m3u_text:
            # No recognised path — show first few lines to aid diagnosis
            preview = "\n".join(m3u_text.splitlines()[:10])
            print(f"    WARNING: M3U has content but no stream URLs matched.\n{preview}", flush=True)
        return urls

    def find_stream_url_for_channel(self, channel_hint: str) -> str | None:
        """
        Find a stream URL whose preceding EXTINF line contains channel_hint.
        Returns None if not found.
        """
        _, m3u = self.get("/m3u/m3undle.m3u")
        m3u_text = m3u if isinstance(m3u, str) else ""
        lines = m3u_text.splitlines()
        for i, line in enumerate(lines):
            if channel_hint.lower() in line.lower():
                if i + 1 < len(lines) and lines[i + 1].startswith("http"):
                    return lines[i + 1].strip()
        return None

    # -----------------------------------------------------------------------
    # Full setup sequence
    # -----------------------------------------------------------------------

    def setup(
        self,
        *,
        playlist_url: str,
        provider_name: str = "provider-a-sim",
        max_concurrent_streams: int = 2,
        clean_relay_mode: str | None = None,
        clear_providers: bool = True,
    ) -> bool:
        """
        Run the full bring-up sequence against a simulator playlist URL.

        On success, self.provider_id and self.profile_id are populated and
        M3Undle is ready for streaming tests.  Returns True on success.
        """
        self.last_setup_error = None
        print("\n--- M3Undle setup sequence ---", flush=True)

        print("  Resetting debug state...", flush=True)
        self.reset_debug_state()

        if clear_providers:
            print("  Clearing existing providers...", flush=True)
            try:
                self.clear_existing_providers()
            except RuntimeError as exc:
                self.last_setup_error = str(exc)
                print(f"  SETUP FAILED: {exc}", flush=True)
                return False

        print("  Upserting provider...", flush=True)
        try:
            body = self.upsert_provider(
                name=provider_name,
                playlist_url=playlist_url,
                max_concurrent_streams=max_concurrent_streams,
                clean_relay_mode=clean_relay_mode,
            )
        except RuntimeError as exc:
            self.last_setup_error = str(exc)
            print(f"  SETUP FAILED: {exc}", flush=True)
            return False

        provider = body.get("provider", {}) if isinstance(body.get("provider"), dict) else {}
        self.provider_id = str(provider.get("providerId") or provider.get("id") or "")
        action = body.get("action", "?")
        print(f"  Provider {action}, id={self.provider_id}", flush=True)

        return self._finish_setup(provider_name=provider_name)

    def setup_xtream(
        self,
        *,
        xtream_base_url: str,
        xtream_username: str,
        xtream_password: str,
        provider_name: str = "provider-xtream-sim",
        xtream_include_xmltv: bool = False,
        include_vod: bool = False,
        include_series: bool = False,
        max_concurrent_streams: int = 2,
        clear_providers: bool = True,
    ) -> bool:
        """
        Run the full bring-up sequence against an upstream Xtream provider.

        On success, self.provider_id and self.profile_id are populated and
        M3Undle is ready for downstream output tests.
        """
        self.last_setup_error = None
        print("\n--- M3Undle Xtream setup sequence ---", flush=True)

        print("  Resetting debug state...", flush=True)
        self.reset_debug_state()

        if clear_providers:
            print("  Clearing existing providers...", flush=True)
            try:
                self.clear_existing_providers()
            except RuntimeError as exc:
                self.last_setup_error = str(exc)
                print(f"  SETUP FAILED: {exc}", flush=True)
                return False

        print("  Creating Xtream provider...", flush=True)
        try:
            body = self.create_xtream_provider(
                name=provider_name,
                xtream_base_url=xtream_base_url,
                xtream_username=xtream_username,
                xtream_password=xtream_password,
                xtream_include_xmltv=xtream_include_xmltv,
                include_vod=include_vod,
                include_series=include_series,
                max_concurrent_streams=max_concurrent_streams,
            )
        except RuntimeError as exc:
            self.last_setup_error = str(exc)
            print(f"  SETUP FAILED: {exc}", flush=True)
            return False

        self.provider_id = str(body.get("providerId") or body.get("id") or "")
        print(f"  Provider created, id={self.provider_id}", flush=True)

        return self._finish_setup(provider_name=provider_name)

    def _finish_setup(self, *, provider_name: str) -> bool:
        if not self.provider_id:
            self.last_setup_error = "provider ID missing from setup response"
            print("  SETUP FAILED: provider ID missing from setup response", flush=True)
            return False

        print("  Activating provider...", flush=True)
        try:
            self.activate_provider(str(self.provider_id))
        except RuntimeError as exc:
            self.last_setup_error = str(exc)
            print(f"  SETUP FAILED: {exc}", flush=True)
            return False

        print("  Resolving provider profile...", flush=True)
        self.profile_id = self.resolve_provider_profile_id(str(self.provider_id), provider_name)
        if not self.profile_id:
            self.last_setup_error = f"could not resolve profile for provider {self.provider_id}"
            print(f"  SETUP FAILED: could not resolve profile for provider {self.provider_id}", flush=True)
            return False
        print(f"  Using profile id={self.profile_id}", flush=True)

        print("  Waiting for provider-triggered refresh to settle...", flush=True)
        if not self.wait_snapshot_idle(timeout_seconds=180.0):
            self.last_setup_error = "provider-triggered refresh did not settle"
            print("  SETUP FAILED: provider-triggered refresh did not settle", flush=True)
            return False

        print("  Activating profile...", flush=True)
        previous_started_utc = str(self.get_snapshot_status_info().get("startedUtc") or "")
        try:
            activated, refresh_triggered = self.activate_profile_for_setup(str(self.profile_id))
        except RuntimeError as exc:
            self.last_setup_error = str(exc)
            print(f"  SETUP FAILED: {exc}", flush=True)
            return False
        if not activated:
            self.last_setup_error = f"active profile did not switch to {self.profile_id}"
            print(f"  SETUP FAILED: active profile did not switch to {self.profile_id}", flush=True)
            return False

        if not refresh_triggered:
            print("  Triggering refresh...", flush=True)
            previous_started_utc = str(self.get_snapshot_status_info().get("startedUtc") or "")
            try:
                refresh_status = self.trigger_refresh()
            except RuntimeError as exc:
                self.last_setup_error = str(exc)
                print(f"  SETUP FAILED: {exc}", flush=True)
                return False
            if refresh_status == 409:
                print("    Refresh already running; polling existing run...", flush=True)
        else:
            print("  Profile activation triggered refresh.", flush=True)

        print("  Polling refresh...", flush=True)
        if not self.poll_snapshot_status(previous_started_utc=previous_started_utc):
            self.last_setup_error = "snapshot did not complete successfully"
            print("  SETUP FAILED: snapshot did not complete successfully", flush=True)
            return False

        print("  Including all groups (clearing hold state)...", flush=True)
        try:
            included = self.include_all_groups(self.profile_id)
        except RuntimeError as exc:
            self.last_setup_error = str(exc)
            print(f"  SETUP FAILED: {exc}", flush=True)
            return False
        print(f"  Transitioned {included} held group(s) to include", flush=True)

        print("  Selecting all channels...", flush=True)
        try:
            selected = self.select_all_channels(self.profile_id)
        except RuntimeError as exc:
            self.last_setup_error = str(exc)
            print(f"  SETUP FAILED: {exc}", flush=True)
            return False
        print(f"  Selected {selected} channels", flush=True)

        print("  Building snapshot...", flush=True)
        previous_started_utc = str(self.get_snapshot_status_info().get("startedUtc") or "")
        try:
            self.build_snapshot()
        except RuntimeError as exc:
            self.last_setup_error = str(exc)
            print(f"  SETUP FAILED: {exc}", flush=True)
            return False

        print("  Polling build...", flush=True)
        if not self.poll_build_completion(str(self.profile_id)):
            self.last_setup_error = "build did not complete successfully"
            print("  SETUP FAILED: build did not complete successfully", flush=True)
            return False

        print("  Waiting for /health/ready...", flush=True)
        if not self.poll_ready():
            self.last_setup_error = "readiness not reached"
            print("  SETUP FAILED: readiness not reached", flush=True)
            return False

        self.last_setup_error = None
        print("  Setup complete.", flush=True)
        return True
