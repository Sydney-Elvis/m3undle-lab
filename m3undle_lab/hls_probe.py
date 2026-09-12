"""Decode generated HLS media with the product container's FFmpeg."""

from __future__ import annotations

import subprocess
import time
import urllib.request
from typing import Any
from urllib.parse import urljoin

from m3undle_lab.commands import CONTAINER_NAME


def probe_generated_hls(base: str, location: str) -> dict[str, Any]:
    """Require independently decodable, changing video from two generated segments."""
    try:
        manifest_url = urljoin(base, location)
        deadline = time.monotonic() + 15
        segments: list[str] = []
        while time.monotonic() < deadline:
            with urllib.request.urlopen(manifest_url, timeout=10) as response:
                manifest = response.read().decode("utf-8")
            if not manifest.startswith("#EXTM3U"):
                return {"ok": False, "error": "invalid generated manifest"}
            segments = [line.strip() for line in manifest.splitlines() if line.strip() and not line.startswith("#")]
            if len(segments) >= 2:
                break
            time.sleep(0.2)

        decoded = []
        for segment in segments[-2:]:
            with urllib.request.urlopen(urljoin(manifest_url, segment), timeout=10) as response:
                data = response.read()
            result = subprocess.run(
                ["docker", "exec", "-i", CONTAINER_NAME, "ffmpeg", "-hide_banner", "-loglevel", "error",
                 "-xerror", "-i", "pipe:0", "-map", "0:v:0", "-an", "-f", "framemd5", "pipe:1"],
                input=data, capture_output=True, timeout=20,
            )
            frames = [line for line in result.stdout.decode().splitlines() if line and not line.startswith("#")]
            if result.returncode != 0 or len(frames) < 20:
                return {"ok": False, "error": result.stderr.decode()[:500], "decoded_frames": len(frames)}
            decoded.append(frames)
        hashes = [{line.rsplit(",", 1)[-1].strip() for line in frames} for frames in decoded]
        return {"ok": len(hashes) == 2 and all(len(h) > 1 for h in hashes) and hashes[0] != hashes[1],
                "segments": len(segments), "segment_names": segments[-2:],
                "decoded_frames": [len(frames) for frames in decoded]}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

