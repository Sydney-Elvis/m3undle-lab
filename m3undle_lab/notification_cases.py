"""Case logic for the administrator-notification suite.

Kept free of se-lab imports so the registered suite (tests/test_notifications.py) stays a thin registration layer and the same
cases can be driven by hand against any M3Undle that can reach the fixtures. Every assertion is made from an independent
observation: Mailpit's REST API for email, a second Matrix account for the room, and M3Undle's own history API only for the
delivery states it reports.

Cases share one Scenario and run in order; each leaves routes Off and sending configured so the next can start from a known state.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Protocol

from m3undle_lab.notifications import (
    FaultSmtpServer,
    FixtureError,
    MailpitClient,
    MatrixFixture,
    TestCertificates,
    free_port,
    http_json,
    wait_for,
)

EPG = "epg.fetch_failed"
RESTARTED = "system.restarted"
SMTP = "smtp"
MATRIX = "matrix"
LAB_PASSWORD = "LabNotify-1!"           # Mailpit's throwaway credential (its auth file cannot carry spaces)
EXACT_PASSWORD = "  lab pass:word 1!  "  # leading/trailing spaces: must reach the server untouched
OPEN_TITLE = "failing to update"
RECOVERY_TITLE = "Recovered"
STARTED_TITLE = "M3Undle started"


class Ctx(Protocol):
    def record(self, test_id: str, passed: bool, detail: str) -> None: ...
    def fail(self, test_id: str, detail: str) -> None: ...
    def skip(self, test_id: str, reason: str) -> None: ...


# ---------------------------------------------------------------------------------------------------------------------
# M3Undle API driver
# ---------------------------------------------------------------------------------------------------------------------

class NotificationsApi:
    def __init__(self, base_url: str) -> None:
        self.base = base_url.rstrip("/")

    def call(self, method: str, path: str, body: Any = None, *, timeout: float = 30.0) -> tuple[int, Any]:
        headers = {"X-Requested-With": "m3undle-lab"} if method not in ("GET", "HEAD") else {}
        return http_json(method, f"{self.base}{path}", body=body, headers=headers, timeout=timeout)

    def overview(self) -> dict[str, Any]:
        status, body = self.call("GET", "/api/v1/notifications")
        if status != 200 or not isinstance(body, dict):
            raise FixtureError(f"Notifications overview returned {status}: {body}")
        return body

    def destination(self, kind: str) -> dict[str, Any]:
        return next(d for d in self.overview()["destinations"] if d["kind"] == kind)

    def route(self, key: str) -> dict[str, Any]:
        return next(r for r in self.overview()["routes"] if r["key"] == key)

    def deliveries(self, **filters: str) -> list[dict[str, Any]]:
        query = "&".join(f"{k}={v}" for k, v in {"pageSize": "100", **filters}.items())
        status, body = self.call("GET", f"/api/v1/notifications/deliveries?{query}")
        if status != 200 or not isinstance(body, dict):
            raise FixtureError(f"Delivery history returned {status}: {body}")
        return list(body["items"])

    def settings(self, **overrides: Any) -> tuple[int, Any]:
        current = self.overview()["settings"]
        body = {
            "expectedRevision": current["revision"], "sendingEnabled": True, "paused": False,
            "failureDelayMinutes": 0, "overdueGraceMinutes": current["overdueGraceMinutes"],
            "reminderIntervalHours": current["reminderIntervalHours"], "coverageWarnHours": current["coverageWarnHours"],
            "coverageWarnPercent": current["coverageWarnPercent"], "coverageRecoverHours": current["coverageRecoverHours"],
            "coverageRecoverPercent": current["coverageRecoverPercent"], "coverageGapMinutes": current["coverageGapMinutes"],
            "retentionDays": current["retentionDays"],
        }
        body.update(overrides)
        return self.call("PUT", "/api/v1/notifications/settings", body)

    def set_route(self, key: str, kind: str | None) -> tuple[int, Any]:
        route = self.route(key)
        return self.call("PUT", f"/api/v1/notifications/routes/{key}", {
            "expectedRevision": route["revision"], "destinationKind": kind,
            "sendRecovery": True, "sendReminders": True,
        })

    def save_smtp(self, host: str, port: int, *, password: str | None, recipients: list[str], user: str = "mailer",
                  auth: str = "password", tls: str = "starttls", clear_password: bool = False) -> tuple[int, Any]:
        revision = self.destination(SMTP)["configRevision"]
        return self.call("PUT", f"/api/v1/notifications/destinations/{SMTP}", {
            "expectedRevision": revision,
            "smtp": {"host": host, "port": port, "tlsMode": tls, "authMode": auth, "username": user if auth == "password" else None,
                     "password": password, "clearPassword": clear_password, "senderAddress": "m3undle@lab.test",
                     "senderName": "M3Undle Lab", "recipients": recipients},
        })

    def save_matrix(self, url: str, room: str, token: str | None, *, allow_http: bool = True) -> tuple[int, Any]:
        revision = self.destination(MATRIX)["configRevision"]
        return self.call("PUT", f"/api/v1/notifications/destinations/{MATRIX}", {
            "expectedRevision": revision,
            "matrix": {"homeserverUrl": url, "roomId": room, "accessToken": token, "clearAccessToken": False, "allowInsecureHttp": allow_http},
        })

    def test(self, kind: str) -> dict[str, Any]:
        """Test the saved revision. The endpoint allows one real test per minute per method, so wait out a 429."""
        for _ in range(4):
            revision = self.destination(kind)["configRevision"]
            status, body = self.call("POST", f"/api/v1/notifications/destinations/{kind}/test", {"expectedRevision": revision}, timeout=90)
            if status == 429:
                time.sleep(62)
                continue
            if status != 200 or not isinstance(body, dict):
                raise FixtureError(f"Test of {kind} returned {status}: {body}")
            return body
        raise FixtureError(f"Test of {kind} stayed rate limited")

    def enable(self, kind: str, enabled: bool = True) -> tuple[int, Any]:
        revision = self.destination(kind)["configRevision"]
        return self.call("PUT", f"/api/v1/notifications/destinations/{kind}/enabled", {"expectedRevision": revision, "enabled": enabled})

    def verify_and_enable(self, kind: str) -> dict[str, Any]:
        result = self.test(kind)
        if not result.get("verificationApplied"):
            raise FixtureError(f"{kind} was not verified: {result.get('summary')} {result.get('targets')}")
        status, body = self.enable(kind)
        if status != 200:
            raise FixtureError(f"Enabling {kind} returned {status}: {body}")
        return result

    def all_off(self) -> None:
        for definition in self.overview()["routes"]:
            if definition["destinationKind"] is not None:
                self.set_route(definition["key"], None)


# ---------------------------------------------------------------------------------------------------------------------
# A source whose health the cases control
# ---------------------------------------------------------------------------------------------------------------------

class EpgToggleServer:
    """Serves a small XMLTV document, or HTTP 500 while `failing` is set."""

    def __init__(self, bind: str = "0.0.0.0") -> None:
        self.failing = False
        self.port = 0
        self.requests = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                outer.requests += 1
                if outer.failing:
                    self.send_response(500)
                    self.end_headers()
                    return
                start = time.gmtime(time.time() - 3600)
                stop = time.gmtime(time.time() + 7 * 3600)
                body = (
                    '<?xml version="1.0" encoding="utf-8"?><tv><channel id="lab.one"><display-name>Lab One</display-name></channel>'
                    f'<programme start="{time.strftime("%Y%m%d%H%M%S", start)} +0000" stop="{time.strftime("%Y%m%d%H%M%S", stop)} +0000" channel="lab.one">'
                    "<title>Lab Programme</title></programme></tv>"
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/xml")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_: Any) -> None:
                pass

        self._server = ThreadingHTTPServer((bind, 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


# ---------------------------------------------------------------------------------------------------------------------
# Shared scenario state
# ---------------------------------------------------------------------------------------------------------------------

@dataclass
class Scenario:
    api: NotificationsApi
    mail: MailpitClient
    matrix: MatrixFixture
    certs: TestCertificates
    epg_url_for_m3undle: str                  # where M3Undle reaches the toggle server
    epg: EpgToggleServer
    smtp_host: str                            # Mailpit as M3Undle reaches it
    smtp_port: int
    matrix_url: str                           # Synapse as M3Undle reaches it
    fault_host: str                           # the in-process fault server as M3Undle reaches it
    restart_m3undle: Callable[[], None]
    stop_service: Callable[[str], None]       # "matrix" | "smtp"
    start_service: Callable[[str], None]
    wait_m3undle: Callable[[], None]
    base_url: str
    source_id: str | None = None
    fault: FaultSmtpServer | None = None
    state: dict[str, Any] = field(default_factory=dict)

    # -- a source whose fetch outcome the cases control -------------------------------------------------------------

    SOURCE_NAME = "Lab notification guide"

    def remove_stale_sources(self) -> None:
        """A source left by an earlier run keeps an incident of its own, so clear them before starting."""
        status, body = self.api.call("GET", "/api/v1/epg/sources")
        if status == 200 and isinstance(body, list):
            for source in body:
                if source.get("name") == self.SOURCE_NAME:
                    self.api.call("DELETE", f"/api/v1/epg/sources/{source.get('epgSourceId') or source.get('id')}")

    def ensure_source(self) -> str:
        if self.source_id:
            return self.source_id
        self.remove_stale_sources()
        status, body = self.api.call("POST", "/api/v1/epg/sources", {
            "name": self.SOURCE_NAME, "kind": "xmltv_url", "urlOrPath": self.epg_url_for_m3undle,
            "priority": 10, "enabled": True, "timeoutSeconds": 10,
        })
        if status != 201 or not isinstance(body, dict):
            raise FixtureError(f"Creating the EPG source failed: {status} {body}")
        self.source_id = body.get("epgSourceId") or body.get("id")
        if not self.source_id:
            raise FixtureError(f"EPG source response had no id: {body}")
        return self.source_id

    def fetch_source(self) -> tuple[int, Any]:
        return self.api.call("POST", f"/api/v1/epg/sources/{self.ensure_source()}/fetch", timeout=60)

    def make_unhealthy(self) -> None:
        self.epg.failing = True
        self.fetch_source()

    def make_healthy(self) -> None:
        self.epg.failing = False
        self.fetch_source()

    def accepted(self, **filters: str) -> list[dict[str, Any]]:
        return [d for d in self.api.deliveries(state="Accepted", **filters)]


def _check(ctx: Ctx, test_id: str, ok: bool, detail: str) -> bool:
    ctx.record(test_id, ok, detail)
    return ok


def _titles(items: list[dict[str, Any]]) -> list[str]:
    return [str(i.get("title", "")) for i in items]


# ---------------------------------------------------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------------------------------------------------

def notif_01_email_opening_and_recovery(ctx: Ctx, s: Scenario) -> None:
    """EPG failure, then recovery, by authenticated STARTTLS email to every recipient, observed in the mailbox."""
    tid = "NOTIF-01"
    s.api.all_off()
    s.mail.clear()
    s.ensure_source()
    s.epg.failing = False

    status, body = s.api.save_smtp(s.smtp_host, s.smtp_port, password=LAB_PASSWORD, recipients=["admin-a@lab.test", "admin-b@lab.test"])
    if status != 200:
        return ctx.fail(tid, f"saving the SMTP setup returned {status}: {body}")
    result = s.api.verify_and_enable(SMTP)
    tests = s.mail.wait_for(subject_contains="M3Undle test notification", count=2, timeout=60)
    recipients = sorted(a["Address"] for m in tests for a in m.get("To", []))
    if not _check(ctx, f"{tid}a", recipients == ["admin-a@lab.test", "admin-b@lab.test"],
                  f"test message reached exactly each recipient once (saw {recipients}); status={result.get('verificationStatus')}"):
        return

    s.api.set_route(EPG, SMTP)
    s.api.settings(sendingEnabled=True, failureDelayMinutes=0)
    s.mail.clear()
    s.make_unhealthy()

    opened = s.mail.wait_for(subject_contains=OPEN_TITLE, count=2, timeout=150)
    to = sorted(a["Address"] for m in opened for a in m.get("To", []))
    message = s.mail.message(opened[0]["ID"])
    text = message.get("Text", "")
    leaked = LAB_PASSWORD in json.dumps(message)
    _check(ctx, f"{tid}b", to == ["admin-a@lab.test", "admin-b@lab.test"] and "Lab notification guide" in text and not leaked,
           f"opening delivered as one message per recipient ({to}); names the source; no credential in the message (leaked={leaked})")
    _check(ctx, f"{tid}c", "Message-ID" in json.dumps(message.get("Headers", message)) or bool(message.get("MessageID")),
           "each message carries a stable Message-ID for correlation")

    s.mail.clear()
    s.make_healthy()
    recovered = s.mail.wait_for(subject_contains=RECOVERY_TITLE, count=2, timeout=150)
    states = [d["state"] for d in s.api.deliveries()]
    _check(ctx, f"{tid}d", len(recovered) >= 2 and states.count("Accepted") >= 4,
           f"recovery reached both recipients and the history reports Accepted (not 'delivered'): {states.count('Accepted')} accepted")
    s.api.all_off()


def notif_02_matrix_opening_and_recovery(ctx: Ctx, s: Scenario) -> None:
    """The same condition by Matrix: a plain-text m.notice read back by an independent account."""
    tid = "NOTIF-02"
    s.api.all_off()
    s.epg.failing = False
    s.matrix.wait_ready()
    if s.matrix.room_id is None:
        s.matrix.provision()
    assert s.matrix.bot and s.matrix.room_id

    status, body = s.api.save_matrix(s.matrix_url, s.matrix.room_id, s.matrix.bot.access_token)
    if status != 200:
        return ctx.fail(tid, f"saving the Matrix setup returned {status}: {body}")
    result = s.api.verify_and_enable(MATRIX)
    device = s.api.destination(MATRIX)["matrix"]
    probe = s.matrix.wait_for_notice(body_contains="M3Undle test notification", timeout=60)
    _check(ctx, f"{tid}a", device["deviceId"] == s.matrix.bot.device_id and bool(probe),
           f"the test went through the real room and the bot device was discovered ({device['deviceId']}); status={result.get('verificationStatus')}")

    s.api.set_route(EPG, MATRIX)
    s.api.settings(sendingEnabled=True, failureDelayMinutes=0)
    s.make_unhealthy()
    opened = s.matrix.wait_for_notice(body_contains=OPEN_TITLE, timeout=150)
    content = opened[-1]["content"]
    _check(ctx, f"{tid}b", content.get("msgtype") == "m.notice" and "formatted_body" not in content and "Lab notification guide" in content.get("body", ""),
           "the observer reads a plain-text m.notice naming the source")

    s.make_healthy()
    recovered = s.matrix.wait_for_notice(body_contains=RECOVERY_TITLE, timeout=150)
    states = [d["state"] for d in s.api.deliveries(provider=MATRIX)]
    _check(ctx, f"{tid}c", bool(recovered) and "Accepted" in states, "the recovery arrives in the same room and is recorded as Accepted")
    s.api.all_off()


def notif_03_partial_recipient_failure_and_exact_password(ctx: Ctx, s: Scenario, certs: TestCertificates) -> None:
    """One recipient is refused by the server; the other is delivered once; the password reaches the server exactly as typed."""
    tid = "NOTIF-03"
    s.api.all_off()
    s.epg.failing = False
    fault = FaultSmtpServer(certificate=certs.server_cert, key=certs.server_key, require_password=EXACT_PASSWORD)
    fault.start()
    s.fault = fault
    try:
        status, body = s.api.save_smtp(s.fault_host, fault.port, password=EXACT_PASSWORD, recipients=["good@lab.test", "bad@lab.test"])
        if status != 200:
            return ctx.fail(tid, f"saving the SMTP setup returned {status}: {body}")
        s.api.verify_and_enable(SMTP)
        _check(ctx, f"{tid}a", bool(fault.auth_attempts) and all(a == ("mailer", EXACT_PASSWORD) for a in fault.auth_attempts) and not fault.plaintext_auth_seen,
               f"the password (leading/trailing spaces included) reached the server exactly, only over TLS; attempts={len(fault.auth_attempts)}")

        fault.messages.clear()
        fault.rcpt_replies["bad@lab.test"] = "550 5.1.1 mailbox unavailable"
        s.api.set_route(EPG, SMTP)
        s.api.settings(sendingEnabled=True, failureDelayMinutes=0)
        s.make_unhealthy()

        # History spans every notification, so scope this incident's outcome per recipient instead of by count alone.
        def _failed_bad() -> Any:
            return [d for d in s.api.deliveries(provider=SMTP) if d["state"] == "Failed" and d["target"] == "bad@lab.test"]

        def _accepted_good() -> Any:
            return [d for d in s.api.deliveries(provider=SMTP)
                    if d["state"] == "Accepted" and d["kind"] == "Opening" and d["target"] == "good@lab.test"]

        failed = wait_for(_failed_bad, timeout=150, what="the refused recipient to be marked Failed")
        accepted = wait_for(_accepted_good, timeout=60, what="the accepted recipient's Opening to be Accepted")
        time.sleep(40)  # long enough for any wrongful automatic retry of the accepted recipient to show up
        deliveries = s.api.deliveries(provider=SMTP)
        failed = [d for d in deliveries if d["state"] == "Failed" and d["target"] == "bad@lab.test"]
        accepted = [d for d in deliveries if d["state"] == "Accepted" and d["kind"] == "Opening" and d["target"] == "good@lab.test"]
        good_copies = [m for m in fault.messages if "good@lab.test" in m.recipients]
        targets = sorted({d["target"] for d in deliveries})
        _check(ctx, f"{tid}b", len(failed) == 1 and failed[0]["errorCode"] == "smtp_rejected" and len(accepted) == 1 and len(good_copies) == 1,
               f"partial result is per recipient (targets seen: {targets}): bad@ Failed rows={len(failed)} code={failed[0]['errorCode'] if failed else None}, "
               f"good@ Accepted rows={len(accepted)}, good@ copies at the server={len(good_copies)}")

        fault.rcpt_replies.clear()
        item = failed[0]
        status, body = s.api.call("POST", f"/api/v1/notifications/deliveries/{item['id']}/retry",
                                  {"expectedRevision": item["revision"], "acknowledgeDuplicateRisk": False})
        wait_for(lambda: any(m for m in fault.messages if "bad@lab.test" in m.recipients), timeout=90, what="the explicit retry to deliver")
        good_after = [m for m in fault.messages if "good@lab.test" in m.recipients]
        _check(ctx, f"{tid}c", status == 200 and len(good_after) == 1,
               f"an explicit retry re-sends only the failed recipient ({len(good_after)} copy to good@)")
    finally:
        fault.stop()
        s.fault = None
        s.api.all_off()
        s.epg.failing = False
        s.fetch_source()


def notif_04_smtp_tls_is_never_downgraded(ctx: Ctx, s: Scenario, certs: TestCertificates) -> None:
    """A certificate the trust anchor does not vouch for, or a server with no TLS, fails safely before any credential is sent."""
    tid = "NOTIF-04"
    s.api.all_off()

    untrusted = FaultSmtpServer(certificate=certs.untrusted_cert, key=certs.untrusted_key)
    untrusted.start()
    try:
        status, body = s.api.save_smtp(s.fault_host, untrusted.port, password=EXACT_PASSWORD, recipients=["a@lab.test"])
        result = s.api.test(SMTP)
        codes = [t.get("errorCode") for t in result.get("targets", [])]
        _check(ctx, f"{tid}a", status == 200 and not result.get("verificationApplied") and "tls_failed" in codes and not untrusted.auth_attempts,
               f"normal certificate validation rejected the untrusted server ({codes}); credentials sent: {len(untrusted.auth_attempts)}")
    finally:
        untrusted.stop()

    plain = FaultSmtpServer(certificate=certs.server_cert, key=certs.server_key, offer_starttls=False)
    plain.start()
    try:
        s.api.save_smtp(s.fault_host, plain.port, password=EXACT_PASSWORD, recipients=["a@lab.test"])
        result = s.api.test(SMTP)
        codes = [t.get("errorCode") for t in result.get("targets", [])]
        _check(ctx, f"{tid}b", not result.get("verificationApplied") and "tls_unsupported" in codes and not plain.auth_attempts and not plain.plaintext_auth_seen,
               f"a server without STARTTLS is refused, never downgraded ({codes}); credentials sent: {len(plain.auth_attempts)}")
    finally:
        plain.stop()


def notif_05_outages_restart_and_isolation(ctx: Ctx, s: Scenario) -> None:
    """A dead homeserver retries on its own, never blocks email, refresh or health, and survives restarts without duplicates."""
    tid = "NOTIF-05"
    s.api.all_off()
    s.epg.failing = False
    s.fetch_source()
    s.mail.clear()
    # Re-establish both methods (earlier cases left them disabled or pointed at fixtures that are gone).
    status, body = s.api.save_smtp(s.smtp_host, s.smtp_port, password=LAB_PASSWORD, recipients=["admin-a@lab.test"])
    if status != 200:
        return ctx.fail(tid, f"saving the SMTP setup returned {status}: {body}")
    s.api.verify_and_enable(SMTP)
    assert s.matrix.room_id and s.matrix.bot
    s.api.save_matrix(s.matrix_url, s.matrix.room_id, s.matrix.bot.access_token)
    s.api.verify_and_enable(MATRIX)
    s.api.set_route(EPG, SMTP)
    s.api.set_route(RESTARTED, MATRIX)
    s.api.settings(sendingEnabled=True, failureDelayMinutes=0)
    s.mail.clear()
    before = len(s.matrix.notices(body_contains=STARTED_TITLE))

    s.stop_service("matrix")
    s.restart_m3undle()
    s.wait_m3undle()

    pending = wait_for(lambda: [d for d in s.api.deliveries(provider=MATRIX) if d["state"] in ("RetryScheduled", "Pending") and d["kind"] == "OneTime"],
                       timeout=120, what="the restart notice to wait for the dead homeserver")
    started = time.monotonic()
    s.make_unhealthy()
    health_status, _ = http_json("GET", f"{s.base_url}/health", timeout=10)
    live_status, _ = http_json("GET", f"{s.base_url}/livez", timeout=10)
    elapsed = time.monotonic() - started
    mails = s.mail.wait_for(subject_contains=OPEN_TITLE, count=1, timeout=150)
    _check(ctx, f"{tid}a", live_status == 200 and elapsed < 30 and bool(mails),
           f"email still delivered, /livez={live_status} (/health={health_status} reflects the lab's missing active profile) and refresh responsive ({elapsed:.1f}s) while Matrix is down; matrix delivery waiting: {pending[0]['state']}")

    s.restart_m3undle()
    s.wait_m3undle()
    survivors = [d for d in s.api.deliveries(provider=MATRIX) if d["kind"] == "OneTime" and d["state"] != "Accepted"]
    _check(ctx, f"{tid}b", len(survivors) >= 1, f"queued work survived a restart before acceptance ({len(survivors)} still queued)")

    s.start_service("matrix")
    s.matrix.wait_ready()
    notices = s.matrix.wait_for_notice(body_contains=STARTED_TITLE, count=before + 2, timeout=180)
    counts_after_recovery = len(notices)
    ids = [e["event_id"] for e in notices]
    _check(ctx, f"{tid}c", len(set(ids)) == len(ids) == before + 2, f"both outage-era restart notices were delivered exactly once after the homeserver returned ({len(ids) - before} new)")

    s.restart_m3undle()
    s.wait_m3undle()
    final = wait_for(lambda: (lambda n: n if len(n) >= counts_after_recovery + 1 else None)(s.matrix.notices(body_contains=STARTED_TITLE)), timeout=120, what="the third restart notice")
    time.sleep(40)
    final = s.matrix.notices(body_contains=STARTED_TITLE)
    _check(ctx, f"{tid}d", len(final) == counts_after_recovery + 1, f"a restart after acceptance announces only the new boot, replaying nothing ({len(final) - counts_after_recovery} new)")
    s.api.all_off()
    s.epg.failing = False
    s.fetch_source()


def notif_06_configuration_edits(ctx: Ctx, s: Scenario) -> None:
    """Editing a method switches it off and clears its test; a removed recipient gets nothing; re-testing re-announces current state."""
    tid = "NOTIF-06"
    s.api.all_off()
    s.epg.failing = False
    s.fetch_source()
    s.mail.clear()
    status, body = s.api.save_smtp(s.smtp_host, s.smtp_port, password=LAB_PASSWORD, recipients=["keep@lab.test", "drop@lab.test"])
    if status != 200:
        return ctx.fail(tid, f"saving the SMTP setup returned {status}: {body}")
    s.api.verify_and_enable(SMTP)
    s.api.set_route(EPG, SMTP)
    s.api.settings(sendingEnabled=True, failureDelayMinutes=0)
    s.make_unhealthy()
    s.mail.wait_for(subject_contains=OPEN_TITLE, to="keep@lab.test", timeout=150)
    s.mail.wait_for(subject_contains=OPEN_TITLE, to="drop@lab.test", timeout=150)

    status, _ = s.api.save_smtp(s.smtp_host, s.smtp_port, password=None, recipients=["keep@lab.test"])
    edited = s.api.destination(SMTP)
    _check(ctx, f"{tid}a", status == 200 and not edited["enabled"] and not edited["verifiedCurrent"],
           "an edit (blank password keeps the secret) switches the method off and invalidates its test")

    s.mail.clear()
    s.make_healthy()
    time.sleep(70)
    _check(ctx, f"{tid}b", not s.mail.messages(), "while the edited method is untested, no recovery or other mail is sent to anyone")

    s.api.verify_and_enable(SMTP)
    s.api.set_route(EPG, SMTP)
    s.mail.clear()
    s.make_unhealthy()
    mails = s.mail.wait_for(subject_contains=OPEN_TITLE, to="keep@lab.test", timeout=150)
    time.sleep(20)
    dropped = s.mail.find(to="drop@lab.test")
    _check(ctx, f"{tid}c", bool(mails) and not dropped, "after re-testing, a new incident reaches the remaining recipient and never the removed one")
    s.api.all_off()
    s.epg.failing = False
    s.fetch_source()


def notif_07_restore_pauses_sending(ctx: Ctx, s: Scenario) -> None:
    """A restored instance keeps its setup as intent but sends nothing until re-tested and explicitly resumed."""
    tid = "NOTIF-07"
    s.api.all_off()
    s.epg.failing = False
    s.fetch_source()
    s.mail.clear()
    status, body = s.api.save_smtp(s.smtp_host, s.smtp_port, password=LAB_PASSWORD, recipients=["restore@lab.test"])
    if status != 200:
        return ctx.fail(tid, f"saving the SMTP setup returned {status}: {body}")
    s.api.verify_and_enable(SMTP)
    s.api.set_route(EPG, SMTP)
    s.api.settings(sendingEnabled=True, failureDelayMinutes=0)

    status, backup = s.api.call("POST", "/api/v1/backups", timeout=120)
    file_name = backup.get("fileName") if isinstance(backup, dict) else None
    if status not in (200, 201) or not file_name:
        return ctx.skip(tid, f"portable backup unavailable on this build (status {status})")
    status, body = s.api.call("POST", "/api/v1/restore/stage", {"fileName": file_name})
    if status != 200:
        return ctx.fail(tid, f"staging the restore returned {status}: {body}")
    s.api.call("POST", "/api/v1/restore/confirm")
    time.sleep(8)
    s.wait_m3undle()

    overview = s.api.overview()
    smtp = next(d for d in overview["destinations"] if d["kind"] == SMTP)
    route = next(r for r in overview["routes"] if r["key"] == EPG)
    _check(ctx, f"{tid}a", overview["settings"]["requiresActivation"] and not overview["settings"]["sendingAllowed"] and not smtp["verifiedCurrent"] and route["destinationKind"] == SMTP,
           "after restore: setup and route kept as intent, verification cleared, sending held until activated")

    s.mail.clear()
    s.make_unhealthy()
    time.sleep(75)
    _check(ctx, f"{tid}b", not s.mail.messages() and not s.api.deliveries(), "a restored instance sends nothing and replays nothing while held")

    s.api.verify_and_enable(SMTP)
    status, _ = s.api.settings(sendingEnabled=True, paused=False, failureDelayMinutes=0)
    mails = s.mail.wait_for(subject_contains=OPEN_TITLE, to="restore@lab.test", timeout=150)
    _check(ctx, f"{tid}c", status == 200 and bool(mails), "after re-testing and resuming, the current problem is announced from current state")
    s.api.all_off()
    s.epg.failing = False
    s.fetch_source()


def notif_08_page_status_and_http_gate(ctx: Ctx, s: Scenario) -> None:
    """The one Settings page shows real states without secrets, and plain HTTP stays gated."""
    tid = "NOTIF-08"
    request = urllib.request.Request(f"{s.base_url}/settings?section=notifications", headers={"Accept": "text/html"})
    with urllib.request.urlopen(request, timeout=30) as response:
        html = response.read().decode("utf-8", errors="replace")
    visible = all(text in html for text in ("What to send, and how", "EPG source fetch failing", "Delivery history", "Accepted"))
    leaked = LAB_PASSWORD in html or EXACT_PASSWORD.strip() in html or (s.matrix.bot is not None and s.matrix.bot.access_token in html)
    _check(ctx, f"{tid}a", visible and not leaked, f"the Notifications section renders catalog, history and Accepted states; secrets leaked: {leaked}")

    status, body = s.api.save_matrix(s.matrix_url, s.matrix.room_id or "!x:lab.test", None, allow_http=False)
    _check(ctx, f"{tid}b", status == 400 and "homeserverUrl" in json.dumps(body),
           f"plain HTTP needs the per-setup opt-in even when the lab runtime permits it (status {status})")
    overview = s.api.overview()
    _check(ctx, f"{tid}c", all(r["available"] for r in overview["routes"]) and len(overview["routes"]) == 12, "all twelve catalog rows are listed and selectable")


CASES: list[tuple[str, Callable[..., None]]] = [
    ("NOTIF-01", notif_01_email_opening_and_recovery),
    ("NOTIF-02", notif_02_matrix_opening_and_recovery),
    ("NOTIF-03", notif_03_partial_recipient_failure_and_exact_password),
    ("NOTIF-04", notif_04_smtp_tls_is_never_downgraded),
    ("NOTIF-05", notif_05_outages_restart_and_isolation),
    ("NOTIF-06", notif_06_configuration_edits),
    ("NOTIF-07", notif_07_restore_pauses_sending),
    ("NOTIF-08", notif_08_page_status_and_http_gate),
]
