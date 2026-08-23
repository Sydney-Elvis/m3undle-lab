#!/usr/bin/env python3
"""
Content-identity fingerprinting for clean-remux relay scenarios.

Clean relay is `-c copy`: downstream video elementary-stream bytes are
byte-identical to the source fixture, even though container-level DTS/PTS
values get rewritten (FFmpeg's mpegts muxer clamps a backward jump into a
forward ramp rather than passing it through, so DTS monotonicity alone
cannot prove a provider replay was actually suppressed). This module
reconstructs the video elementary stream from a source fixture and from a
downstream capture, then locates each of the capture's content windows
within the source by exact substring match -- so scenarios can assert the
*content* never moves backward, not just the timestamps.

A captured "replay" segment's boundary can start at any arbitrary byte
offset relative to the source (however many bytes preceded it in the
capture), so a fixed-grid hash index keyed by source offset would miss most
real alignments. Substring search sidesteps that: it finds a window's exact
byte content wherever it actually occurs, at any offset.
"""

from __future__ import annotations

from pathlib import Path

TS_PACKET_SIZE = 188

# fixtures/media/README.md documents every fixture in this lab using the
# same FFmpeg mpegts-muxer default PID layout: PAT/0, PMT/4096, video/256,
# audio/257.
VIDEO_PID = 256

DEFAULT_WINDOW_BYTES = 8192


def _packet_pid(packet: bytes) -> int:
    return ((packet[1] & 0x1F) << 8) | packet[2]


def extract_video_es_stream(data: bytes, video_pid: int = VIDEO_PID) -> bytes:
    """Reconstructs the video elementary stream: every video-PID TS packet's
    payload, in arrival order, with the PES header stripped from packets
    that start a new PES packet. Continuation packets carry raw ES bytes
    with no header at all, so they're kept whole."""
    es = bytearray()
    for offset in range(0, len(data) - TS_PACKET_SIZE + 1, TS_PACKET_SIZE):
        packet = data[offset : offset + TS_PACKET_SIZE]
        if packet[0] != 0x47 or _packet_pid(packet) != video_pid:
            continue
        adaptation_control = (packet[3] >> 4) & 0x03
        if adaptation_control in {0, 2}:
            continue
        payload_offset = 4
        if adaptation_control == 3:
            payload_offset += 1 + packet[payload_offset]
        payload = packet[payload_offset:]
        if not payload:
            continue
        if packet[1] & 0x40:
            # Payload unit start: a new PES packet begins here. Skip the
            # fixed 9-byte header plus its real PES_header_data_length
            # field (payload[8]) rather than assuming 5/10 bytes based on
            # the PTS/DTS flags -- correct regardless of which optional
            # fields are present.
            if len(payload) < 9 or payload[:3] != b"\x00\x00\x01" or not 0xE0 <= payload[3] <= 0xEF:
                continue
            es_start = 9 + payload[8]
            es.extend(payload[es_start:])
        else:
            es.extend(payload)
    return bytes(es)


def build_source_es(source_path: str, video_pid: int = VIDEO_PID) -> bytes:
    """Reads a source fixture and reconstructs its video ES stream once, to
    be reused across resolve_offsets() calls for multiple captures."""
    return extract_video_es_stream(Path(source_path).read_bytes(), video_pid)


def resolve_offsets(
    capture: bytes,
    source_es: bytes,
    window_bytes: int = DEFAULT_WINDOW_BYTES,
    video_pid: int = VIDEO_PID,
) -> list[int | None]:
    """Non-overlapping windows over a downstream capture's video ES stream,
    each located in the source ES stream by exact substring match. `None`
    means that window's content never appeared in the source (unresolved --
    expected for a window straddling a real discontinuity boundary)."""
    es = extract_video_es_stream(capture, video_pid)
    offsets: list[int | None] = []
    for offset in range(0, len(es) - window_bytes + 1, window_bytes):
        window = es[offset : offset + window_bytes]
        found = source_es.find(window)
        offsets.append(found if found >= 0 else None)
    return offsets


def count_resolved(offsets: list[int | None]) -> int:
    return sum(1 for offset in offsets if offset is not None)


def first_backward_jump(offsets: list[int | None]) -> tuple[int, int] | None:
    """First place a later resolved offset is less than an earlier one.
    Returns (index into offsets, offset delta) or None if the resolved
    offset sequence never goes backward. Unresolved (None) entries are
    ignored, not treated as a jump."""
    resolved = [(i, offset) for i, offset in enumerate(offsets) if offset is not None]
    for (_, previous), (index, current) in zip(resolved, resolved[1:]):
        if current < previous:
            return index, current - previous
    return None
