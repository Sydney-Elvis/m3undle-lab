"""Real-service fixtures for administrator-notification validation.

Pure stdlib plus the `openssl` and `docker` CLIs; nothing here imports se-lab, so the same helpers drive both the registered
`notifications` suite and a manual run. They give the suite independent observers: Mailpit's REST API reads what the SMTP
server actually received, and a separate Matrix account reads what the homeserver actually stored in the room.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import shutil
import socket
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


# Pinned and reviewed 2026-10-04 (multi-arch index digests). Bump deliberately, with a re-run of the notifications suite.
MAILPIT_IMAGE = "axllent/mailpit@sha256:b68349e3a014b90c5610bfb26b2ae36f3892d7b8cf25ee140c6c71c98d2fcf48"  # Mailpit v1.31.4
SYNAPSE_IMAGE = "matrixdotorg/synapse@sha256:abeb932f2a9293fcbb886d5ddd0f55a34d532708ebb8dc5642934dd91ca26237"  # Synapse 1.162.0

SYNAPSE_SERVER_NAME = "lab.test"


class FixtureError(RuntimeError):
    pass


def wait_for(predicate: Callable[[], Any], *, timeout: float, interval: float = 2.0, what: str = "condition") -> Any:
    """Poll until the predicate returns something truthy; raises with `what` on timeout."""
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    raise FixtureError(f"Timed out after {timeout:.0f}s waiting for {what}")


# ---------------------------------------------------------------------------------------------------------------------
# Locally trusted test CA
# ---------------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class TestCertificates:
    directory: Path

    @property
    def ca_cert(self) -> Path:
        return self.directory / "ca.crt"

    @property
    def server_cert(self) -> Path:
        return self.directory / "server.crt"

    @property
    def server_key(self) -> Path:
        return self.directory / "server.key"

    @property
    def untrusted_cert(self) -> Path:
        return self.directory / "untrusted.crt"

    @property
    def untrusted_key(self) -> Path:
        return self.directory / "untrusted.key"


def _openssl(*args: str, cwd: Path) -> None:
    result = subprocess.run(["openssl", *args], cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise FixtureError(f"openssl {' '.join(args[:2])} failed: {result.stderr.strip()}")


def ensure_test_certificates(
    directory: Path,
    names: tuple[str, ...] = ("localhost", "notif-smtp", "notif-mailpit", "host.docker.internal"),
    crl_url: str = "http://127.0.0.1:18099/ca.crl",
) -> TestCertificates:
    """A disposable CA, a CA-signed SMTP server certificate that publishes a CRL, and a self-signed one the CA does NOT vouch for.

    M3Undle validates revocation like any other check, so the CA must publish one: without a distribution point .NET reports
    "revocation status unknown" and rejects the certificate. The CRL (DER) is written to `ca.der.crl`; serve it with `serve_crl`.
    """
    directory.mkdir(parents=True, exist_ok=True)
    certs = TestCertificates(directory)
    if certs.server_cert.exists() and certs.untrusted_cert.exists():
        return certs

    # A real v3 CA (basicConstraints CA:TRUE, keyCertSign): .NET refuses a v1 or constraint-less issuer even when it is trusted.
    (directory / "ca.ext").write_text("basicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign,cRLSign\nsubjectKeyIdentifier=hash\n")
    _openssl("genrsa", "-out", "ca.key", "2048", cwd=directory)
    _openssl("req", "-new", "-key", "ca.key", "-subj", "/CN=M3Undle Lab Notification Test CA", "-out", "ca.csr", cwd=directory)
    _openssl("x509", "-req", "-in", "ca.csr", "-signkey", "ca.key", "-sha256", "-days", "30", "-extfile", "ca.ext", "-out", "ca.crt", cwd=directory)

    # Minimal `openssl ca` database so the CA can sign with a CRL distribution point and publish a CRL.
    (directory / "index.txt").write_text("")
    (directory / "serial").write_text("1000\n")
    (directory / "crlnumber").write_text("01\n")
    (directory / "ca.cnf").write_text(
        "[ca]\ndefault_ca=CA_default\n[CA_default]\ndir=.\ndatabase=./index.txt\nnew_certs_dir=.\nserial=./serial\ncrlnumber=./crlnumber\n"
        "certificate=./ca.crt\nprivate_key=./ca.key\ndefault_md=sha256\ndefault_days=30\ndefault_crl_days=30\npolicy=policy_any\n"
        "[policy_any]\ncommonName=supplied\n")

    san = ",".join([*(f"DNS:{n}" for n in names), "IP:127.0.0.1"])
    (directory / "san.cnf").write_text(
        f"subjectAltName={san}\nbasicConstraints=CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n"
        f"subjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid\ncrlDistributionPoints=URI:{crl_url}\n")
    _openssl("genrsa", "-out", "server.key", "2048", cwd=directory)
    _openssl("req", "-new", "-key", "server.key", "-subj", "/CN=localhost", "-out", "server.csr", cwd=directory)
    _openssl("ca", "-batch", "-config", "ca.cnf", "-in", "server.csr", "-out", "server.crt", "-extfile", "san.cnf", "-notext", cwd=directory)
    _openssl("ca", "-config", "ca.cnf", "-gencrl", "-out", "ca.pem.crl", cwd=directory)
    _openssl("crl", "-in", "ca.pem.crl", "-outform", "DER", "-out", "ca.der.crl", cwd=directory)

    _openssl("genrsa", "-out", "untrusted.key", "2048", cwd=directory)
    _openssl("req", "-new", "-key", "untrusted.key", "-subj", "/CN=localhost", "-out", "untrusted.csr", cwd=directory)
    _openssl("x509", "-req", "-in", "untrusted.csr", "-signkey", "untrusted.key", "-out", "untrusted.crt", "-days", "30", "-sha256", "-extfile", "san.cnf", cwd=directory)
    for path in directory.iterdir():
        if path.is_file():
            path.chmod(0o644)  # read by container users; lab-only material
    return certs


def serve_crl(certs: TestCertificates, *, bind: str = "0.0.0.0", port: int = 18099) -> Any:
    """Serve the CA's CRL so M3Undle's normal revocation check can complete. Returns the server; call shutdown()/server_close()."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    crl = (certs.directory / "ca.der.crl").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "application/pkix-crl")
            self.send_header("Content-Length", str(len(crl)))
            self.end_headers()
            self.wfile.write(crl)

        def log_message(self, *_: Any) -> None:
            pass

    server = ThreadingHTTPServer((bind, port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# ---------------------------------------------------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------------------------------------------------

def _parse(raw: str) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def http_json(method: str, url: str, *, body: Any = None, headers: dict[str, str] | None = None, timeout: float = 20.0) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    request_headers = {"Accept": "application/json", **(headers or {})}
    if data is not None:
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return response.status, _parse(raw)
    except urllib.error.HTTPError as error:
        return error.code, _parse(error.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
        return 0, str(getattr(error, "reason", error))


# ---------------------------------------------------------------------------------------------------------------------
# Mailpit: an independent reader of what the SMTP server really received
# ---------------------------------------------------------------------------------------------------------------------

class MailpitClient:
    def __init__(self, api_base: str) -> None:
        self.api = api_base.rstrip("/")

    def clear(self) -> None:
        http_json("DELETE", f"{self.api}/api/v1/messages")

    def messages(self) -> list[dict[str, Any]]:
        status, body = http_json("GET", f"{self.api}/api/v1/messages?limit=200")
        if status != 200 or not isinstance(body, dict):
            raise FixtureError(f"Mailpit message list failed with status {status}")
        return list(body.get("messages") or [])

    def message(self, message_id: str) -> dict[str, Any]:
        status, body = http_json("GET", f"{self.api}/api/v1/message/{message_id}")
        if status != 200 or not isinstance(body, dict):
            raise FixtureError(f"Mailpit message {message_id} failed with status {status}")
        return body

    def find(self, *, subject_contains: str | None = None, to: str | None = None) -> list[dict[str, Any]]:
        found = []
        for summary in self.messages():
            if subject_contains and subject_contains.lower() not in str(summary.get("Subject", "")).lower():
                continue
            if to and not any(str(t.get("Address", "")).lower() == to.lower() for t in summary.get("To") or []):
                continue
            found.append(summary)
        return found

    def wait_for(self, *, subject_contains: str | None = None, to: str | None = None, count: int = 1, timeout: float = 120.0) -> list[dict[str, Any]]:
        return wait_for(
            lambda: (lambda hits: hits if len(hits) >= count else None)(self.find(subject_contains=subject_contains, to=to)),
            timeout=timeout, what=f"{count} email(s) to={to} subject~{subject_contains!r}")


# ---------------------------------------------------------------------------------------------------------------------
# Synapse + an independent observer account
# ---------------------------------------------------------------------------------------------------------------------

def prepare_synapse_config(directory: Path, *, bind: str = "0.0.0.0", shared_secret: str = "lab-shared-secret") -> Path:
    """Generate a throwaway homeserver config with the pinned image, then relax it for a disposable lab."""
    directory.mkdir(parents=True, exist_ok=True)
    config = directory / "homeserver.yaml"
    if not config.exists():
        result = subprocess.run(
            ["docker", "run", "--rm", "-v", f"{directory}:/data", "-e", f"SYNAPSE_SERVER_NAME={SYNAPSE_SERVER_NAME}",
             "-e", "SYNAPSE_REPORT_STATS=no", SYNAPSE_IMAGE, "generate"],
            capture_output=True, text=True)
        if result.returncode != 0:
            raise FixtureError(f"Synapse config generation failed: {result.stderr.strip()[-400:]}")

    text = config.read_text()
    if "m3undle-lab-patched" not in text:
        text = re.sub(r"bind_addresses:\s*\n(\s*-\s*'[^']*'\s*\n)+", f"bind_addresses:\n      - '{bind}'\n", text)
        text += (
            "\n# m3undle-lab-patched\n"
            f"registration_shared_secret: \"{shared_secret}\"\n"
            "rc_message: {per_second: 1000, burst_count: 1000}\n"
            "rc_registration: {per_second: 1000, burst_count: 1000}\n"
            "rc_login:\n  address: {per_second: 1000, burst_count: 1000}\n  account: {per_second: 1000, burst_count: 1000}\n  failed_attempts: {per_second: 1000, burst_count: 1000}\n"
            "rc_joins:\n  local: {per_second: 1000, burst_count: 1000}\n"
            "rc_invites:\n  per_room: {per_second: 1000, burst_count: 1000}\n  per_user: {per_second: 1000, burst_count: 1000}\n"
        )
        config.write_text(text)
    for path in directory.iterdir():
        path.chmod(0o666 if path.is_file() else 0o777)
    directory.chmod(0o777)
    return config


@dataclass
class MatrixAccount:
    user_id: str
    access_token: str
    device_id: str


class MatrixFixture:
    """Registers a bot and an independent observer, builds a private unencrypted room, and reads it as the observer."""

    def __init__(self, client_base: str, *, shared_secret: str = "lab-shared-secret") -> None:
        self.base = client_base.rstrip("/")
        self.secret = shared_secret
        self.bot: MatrixAccount | None = None
        self.observer: MatrixAccount | None = None
        self.room_id: str | None = None

    def wait_ready(self, timeout: float = 90.0) -> None:
        wait_for(lambda: http_json("GET", f"{self.base}/_matrix/client/versions", timeout=5)[0] == 200, timeout=timeout, what="Synapse to answer")

    def _register(self, localpart: str, password: str) -> None:
        status, body = http_json("GET", f"{self.base}/_synapse/admin/v1/register")
        if status != 200 or not isinstance(body, dict):
            raise FixtureError(f"Synapse registration nonce failed ({status})")
        nonce = body["nonce"]
        # The MAC covers nonce\0user\0password\0notadmin with no trailing NUL.
        mac = hmac.new(self.secret.encode(), f"{nonce}\x00{localpart}\x00{password}\x00notadmin".encode(), hashlib.sha1)
        status, body = http_json("POST", f"{self.base}/_synapse/admin/v1/register",
                                 body={"nonce": nonce, "username": localpart, "password": password, "admin": False, "mac": mac.hexdigest()})
        if status != 200 and not (isinstance(body, dict) and body.get("errcode") == "M_USER_IN_USE"):
            raise FixtureError(f"Registering {localpart} failed: {status} {body}")

    def _login(self, localpart: str, password: str) -> MatrixAccount:
        status, body = http_json("POST", f"{self.base}/_matrix/client/v3/login",
                                 body={"type": "m.login.password", "identifier": {"type": "m.id.user", "user": localpart},
                                       "password": password, "initial_device_display_name": f"lab-{localpart}"})
        if status != 200 or not isinstance(body, dict):
            raise FixtureError(f"Logging in {localpart} failed: {status} {body}")
        return MatrixAccount(body["user_id"], body["access_token"], body["device_id"])

    def provision(self) -> None:
        """Idempotent for a fresh server; creates the room once per fixture instance."""
        self._register("m3bot", "lab-bot-password-1")
        self._register("labobserver", "lab-observer-password-1")
        self.bot = self._login("m3bot", "lab-bot-password-1")
        self.observer = self._login("labobserver", "lab-observer-password-1")

        status, body = http_json(
            "POST", f"{self.base}/_matrix/client/v3/createRoom", headers=self._auth(self.bot),
            body={"preset": "private_chat", "name": "M3Undle alerts (lab)", "invite": [self.observer.user_id]})
        if status != 200 or not isinstance(body, dict):
            raise FixtureError(f"Creating the room failed: {status} {body}")
        self.room_id = body["room_id"]
        status, body = http_json("POST", f"{self.base}/_matrix/client/v3/join/{urllib.parse.quote(self.room_id)}", headers=self._auth(self.observer), body={})
        if status != 200:
            raise FixtureError(f"Observer could not join the room: {status} {body}")

    @staticmethod
    def _auth(account: MatrixAccount) -> dict[str, str]:
        return {"Authorization": f"Bearer {account.access_token}"}

    def room_events(self, limit: int = 100) -> list[dict[str, Any]]:
        """Messages as the independent observer sees them, oldest first."""
        assert self.observer and self.room_id
        status, body = http_json("GET", f"{self.base}/_matrix/client/v3/rooms/{urllib.parse.quote(self.room_id)}/messages?dir=b&limit={limit}",
                                 headers=self._auth(self.observer))
        if status != 200 or not isinstance(body, dict):
            raise FixtureError(f"Reading the room failed: {status} {body}")
        return [e for e in reversed(body.get("chunk", [])) if e.get("type") == "m.room.message"]

    def notices(self, *, body_contains: str | None = None) -> list[dict[str, Any]]:
        found = []
        for event in self.room_events():
            content = event.get("content", {})
            if content.get("msgtype") != "m.notice":
                continue
            if body_contains and body_contains.lower() not in str(content.get("body", "")).lower():
                continue
            found.append(event)
        return found

    def wait_for_notice(self, *, body_contains: str, count: int = 1, timeout: float = 120.0) -> list[dict[str, Any]]:
        return wait_for(lambda: (lambda hits: hits if len(hits) >= count else None)(self.notices(body_contains=body_contains)),
                        timeout=timeout, what=f"{count} Matrix notice(s) containing {body_contains!r}")


# ---------------------------------------------------------------------------------------------------------------------
# Fault-injecting SMTP server for what a catcher cannot produce
# ---------------------------------------------------------------------------------------------------------------------

@dataclass
class ReceivedMail:
    sender: str
    recipients: list[str]
    data: str


@dataclass
class FaultSmtpServer:
    """A scripted SMTP server. Faults are plain attributes so a case can flip them between sends."""

    certificate: Path
    key: Path
    host: str = "0.0.0.0"
    port: int = 0
    require_user: str = "mailer"
    require_password: str = "  lab pass:word 1!  "
    rcpt_replies: dict[str, str] = field(default_factory=dict)      # address -> full reply line, e.g. "550 no such user"
    final_reply: str = "250 2.0.0 queued"
    drop_before_final_reply: bool = False
    close_after_final_reply: bool = False
    offer_starttls: bool = True

    messages: list[ReceivedMail] = field(default_factory=list)
    auth_attempts: list[tuple[str, str]] = field(default_factory=list)
    plaintext_auth_seen: bool = False

    _listener: socket.socket | None = field(default=None, init=False, repr=False)
    _thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _stop: threading.Event = field(default_factory=threading.Event, init=False, repr=False)

    def start(self) -> None:
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind((self.host, self.port))
        self._listener.listen(8)
        self._listener.settimeout(0.5)
        self.port = self._listener.getsockname()[1]
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._listener:
            self._listener.close()
        if self._thread:
            self._thread.join(timeout=3)

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                connection, _ = self._listener.accept()  # type: ignore[union-attr]
            except (socket.timeout, OSError):
                continue
            threading.Thread(target=self._handle, args=(connection,), daemon=True).start()

    def _context(self) -> ssl.SSLContext:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(self.certificate), str(self.key))
        return context

    def _handle(self, connection: socket.socket) -> None:
        connection.settimeout(30)
        stream: Any = connection
        secure = False
        sender = ""
        recipients: list[str] = []

        def send(line: str) -> None:
            stream.sendall((line + "\r\n").encode())

        def read_line() -> str | None:
            data = bytearray()
            while True:
                try:
                    char = stream.recv(1)
                except (OSError, ssl.SSLError):
                    return None
                if not char:
                    return data.decode(errors="replace") if data else None
                if char == b"\n":
                    return data.decode(errors="replace").rstrip("\r")
                data += char

        try:
            send("220 lab.fault.smtp ESMTP ready")
            while (line := read_line()) is not None:
                upper = line.upper()
                if upper.startswith(("EHLO", "HELO")):
                    send("250-lab.fault.smtp")
                    if not secure and self.offer_starttls:
                        send("250-STARTTLS")
                    if secure:
                        send("250-AUTH PLAIN LOGIN")
                    send("250 8BITMIME")
                elif upper == "STARTTLS":
                    send("220 Ready to start TLS")
                    stream = self._context().wrap_socket(connection, server_side=True)
                    secure = True
                elif upper.startswith("AUTH"):
                    if not secure:
                        self.plaintext_auth_seen = True
                    self._auth(stream, line, read_line, send)
                elif upper.startswith("MAIL FROM"):
                    sender = re.sub(r".*<(.*)>.*", r"\1", line)
                    recipients = []
                    send("250 OK")
                elif upper.startswith("RCPT TO"):
                    address = re.sub(r".*<(.*)>.*", r"\1", line)
                    reply = self.rcpt_replies.get(address.lower(), "250 OK")
                    if reply.startswith("2"):
                        recipients.append(address)
                    send(reply)
                elif upper == "DATA":
                    send("354 End data with <CR><LF>.<CR><LF>")
                    lines = []
                    while (chunk := read_line()) is not None and chunk != ".":
                        lines.append(chunk[1:] if chunk.startswith("..") else chunk)
                    if self.drop_before_final_reply:
                        return
                    if self.final_reply.startswith("2"):
                        self.messages.append(ReceivedMail(sender, list(recipients), "\r\n".join(lines)))
                    send(self.final_reply)
                    if self.close_after_final_reply:
                        return
                elif upper == "QUIT":
                    send("221 Bye")
                    return
                else:
                    send("250 OK" if upper in ("RSET", "NOOP") else "502 Command not implemented")
        except (OSError, ssl.SSLError):
            pass
        finally:
            try:
                connection.close()
            except OSError:
                pass

    def _auth(self, stream: Any, line: str, read_line: Callable[[], str | None], send: Callable[[str], None]) -> None:
        parts = line.split()
        mechanism = parts[1].upper() if len(parts) > 1 else ""
        if mechanism == "PLAIN":
            encoded = parts[2] if len(parts) > 2 else None
            if encoded is None:
                send("334 ")
                encoded = read_line() or ""
            fields = base64.b64decode(encoded).decode().split("\0")
            user, password = (fields[1], fields[2]) if len(fields) > 2 else ("", "")
        elif mechanism == "LOGIN":
            send("334 VXNlcm5hbWU6")
            user = base64.b64decode(read_line() or "").decode()
            send("334 UGFzc3dvcmQ6")
            password = base64.b64decode(read_line() or "").decode()
        else:
            send("504 Unrecognized authentication type")
            return
        self.auth_attempts.append((user, password))
        ok = user == self.require_user and password == self.require_password
        send("235 2.7.0 Authentication successful" if ok else "535 5.7.8 Authentication credentials invalid")


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def copy_ca_for_container(certs: TestCertificates, destination: Path) -> Path:
    """Put the CA where the M3Undle container mounts it (read-only) so normal certificate validation trusts it."""
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / "ca.crt"
    shutil.copyfile(certs.ca_cert, target)
    return target
