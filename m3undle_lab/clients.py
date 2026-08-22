"""Manual-only client registrations for the future bridge-network workflow."""

from __future__ import annotations

import subprocess

from agent import registry
from agent.clients.plugin import ClientPlugin


class _ImageClient(ClientPlugin):
    """A client whose currently-running image is the useful version signal."""

    container_name: str

    def detect_version(self) -> str | None:
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{.Config.Image}}", self.container_name],
            text=True,
            capture_output=True,
            check=False,
        )
        return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


@registry.register_client
class JellyfinClient(_ImageClient):
    name = "jellyfin"
    compose_service = "jellyfin"
    image_env_var = "JELLYFIN_IMAGE"
    default_image = "jellyfin/jellyfin"
    container_name = "m3undle-lab-jellyfin"


@registry.register_client
class NextPvrClient(_ImageClient):
    name = "nextpvr"
    compose_service = "nextpvr"
    image_env_var = "NEXTPVR_IMAGE"
    default_image = "nextpvr/nextpvr_amd64"
    container_name = "m3undle-lab-nextpvr"

