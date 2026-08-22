"""M3Undle-specific AI analysis vocabulary and compose-log extraction."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

from agent import common as lab_common
from agent.analysis.plugin import AnalysisPlugin


STREAM_LOG_MARKERS = (
    "[Stream", "[Auth", "Upstream EOF", "upstream failure", "upstream cooldown",
    "SessionClosed", "Faulted", "ClientAborted", "407", "UpstreamProxyAuthRequired",
    "HDHR tuner", "Subscriber attached", "Subscriber removed", "Stream stop trigger",
    "Xtream stream tune-in", "HTTP request failed",
)

STREAM_ANALYSIS_CONTRACT = (
    'JSON only: {"classification":"upstream_drop|upstream_auth_failure|network_failure|client_abort|unknown",'
    '"confidence":0.0,"root_cause":"one sentence","upstream_issue":true,'
    '"cooldown_triggered":false,"affected_clients":[],"summary":"2-3 sentences",'
    '"next_steps":"short actionable text"}. '
    "Rules: upstream_drop=provider sent EOF/stream ended normally; "
    "upstream_auth_failure=407/proxy-auth error from provider during reconnect; "
    "network_failure=repeated transport-level errors without auth signal or EOF; "
    "client_abort=client disconnected without an upstream failure."
)


class M3UndleAnalysis(AnalysisPlugin):
    def classification_rubric(self) -> str:
        return """DEPLOY means M3Undle itself is unavailable or failed to start (for example, port 8080 is unreachable).
HARNESS means test infrastructure failed: stale provider/simulator state, a runner traceback, or leaked test state.
PRODUCT means M3Undle is healthy but returned incorrect HTTP/data/stream behaviour.
UNKNOWN means the available evidence cannot distinguish those categories.

A connection refused on a provider-simulator high port is HARNESS; one on M3Undle port 8080 is DEPLOY."""

    def failure_prompt(self, task: str, context: dict[str, Any], *, max_chars: int) -> str:
        payload = json.dumps(context, indent=2)[:max_chars]
        return (
            f"You are analyzing M3Undle lab {task} data. Return only valid JSON with classification, "
            "confidence, affected_suites, shared_root_cause, root_cause, likely_error, summary, and next_steps.\n\n"
            f"M3Undle classification rubric:\n{self.classification_rubric()}\n\nContext:\n{payload}"
        )

    def extract_log_context(self, *, session_id: str | None, max_lines: int) -> dict[str, Any]:
        command = lab_common.compose_command("logs", "--no-log-prefix", "--timestamps", "m3undle", "--since", "30m")
        result = lab_common.run_capture(command, check=False)
        lines = result.stdout.splitlines()
        filtered = [line for line in lines if any(marker in line for marker in STREAM_LOG_MARKERS)]
        if session_id:
            filtered = [line for line in filtered if session_id in line]
        filtered = filtered[-max_lines:]
        sessions = sorted({match.group(1) for line in filtered if (match := re.search(r"SessionId=([^\\s]+)", line))})
        return {
            "collected_at_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "session_filter": session_id,
            "session_ids": sessions,
            "stream_event_line_count": len(filtered),
            "cooldown_triggered": any("cooldown" in line.lower() for line in filtered),
            "eof_count": sum("EOF" in line or "EndOfStream" in line for line in filtered),
            "log_excerpt": "\n".join(filtered)[:10000],
        }

    def eval_cases(self, task: str) -> list[dict[str, Any]]:
        if task != "classification":
            return []
        return [
            {
                "name": "eof-then-407-cooldown",
                "prompt": f"{STREAM_ANALYSIS_CONTRACT} Scenario: EOF, reconnect gets 407, then cooldown.",
                "expected": "upstream_auth_failure",
            },
            {
                "name": "transport-failures-no-auth",
                "prompt": f"{STREAM_ANALYSIS_CONTRACT} Scenario: repeated transport failures, no EOF or 407.",
                "expected": "network_failure",
            },
        ]

