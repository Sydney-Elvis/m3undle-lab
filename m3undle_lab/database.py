"""M3Undle's SQLite-specific fresh-state implementation."""

from __future__ import annotations

from agent import common as lab_common
from agent.database.plugin import DatabasePlugin


class M3UndleDatabase(DatabasePlugin):
    """Delete only M3Undle's SQLite database files for ``recreate --fresh``."""

    def reset(self) -> None:
        data_dir = lab_common.runtime_data_dir()
        removed: list[str] = []
        for name in ("m3undle.db", "m3undle.db-shm", "m3undle.db-wal"):
            path = data_dir / name
            if path.exists():
                path.unlink()
                removed.append(name)
        print(
            f"Reset M3Undle database ({', '.join(removed) if removed else 'no existing database files'}).",
            flush=True,
        )

