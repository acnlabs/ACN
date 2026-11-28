"""The directory's own records. Not the database of any one network."""

import sqlite3
import threading
from pathlib import Path

from network_registry.catalog import NETWORKS, Network


class Collision(Exception):
    """The network id is already in the directory."""


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS networks (
                network_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                framework TEXT NOT NULL,
                gateway_url TEXT NOT NULL,
                listed INTEGER NOT NULL,
                seq INTEGER
            )
            """
        )
        # Older databases have no seq column. Add it and number the rows in
        # the order they were registered (rowid), so the two hosted networks
        # keep 1 and 2.
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(networks)")}
        if "seq" not in columns:
            self._conn.execute("ALTER TABLE networks ADD COLUMN seq INTEGER")
        # Backfill: order by rowid, assign numbers to rows that have no seq
        # yet, starting after the current maximum.
        rows = self._conn.execute(
            "SELECT rowid FROM networks WHERE seq IS NULL ORDER BY rowid"
        ).fetchall()
        if rows:
            (highest,) = self._conn.execute("SELECT COALESCE(MAX(seq), 0) FROM networks").fetchone()
            for offset, (rowid,) in enumerate(rows, start=1):
                self._conn.execute(
                    "UPDATE networks SET seq = ? WHERE rowid = ?", (highest + offset, rowid)
                )
        self._conn.commit()

    def seed(self, records: tuple[Network, ...] = NETWORKS) -> None:
        """Insert the hosted networks if they are missing. Never overwrite them."""
        with self._lock:
            for record in records:
                self._conn.execute(
                    """
                    INSERT OR IGNORE INTO networks
                        (network_id, name, framework, gateway_url, listed, seq)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.network_id,
                        record.name,
                        record.framework,
                        record.gateway_url,
                        int(record.listed),
                        self._next_seq(),
                    ),
                )
            self._conn.commit()

    def add(self, network: Network) -> Network:
        """Insert and return the record with its sequence number. A duplicate id is rejected."""
        with self._lock:
            try:
                seq = self._next_seq()
                self._conn.execute(
                    """
                    INSERT INTO networks
                        (network_id, name, framework, gateway_url, listed, seq)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        network.network_id,
                        network.name,
                        network.framework,
                        network.gateway_url,
                        int(network.listed),
                        seq,
                    ),
                )
                self._conn.commit()
            except sqlite3.IntegrityError:
                self._conn.rollback()
                raise Collision(network.network_id) from None
        return Network(
            name=network.name,
            network_id=network.network_id,
            framework=network.framework,
            gateway_url=network.gateway_url,
            listed=network.listed,
            seq=seq,
        )

    def listed(self) -> tuple[Network, ...]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT name, network_id, framework, gateway_url, listed, seq
                FROM networks
                WHERE listed = 1
                ORDER BY seq
                """
            ).fetchall()
        return tuple(_network(row) for row in rows)

    def get(self, network_id: str) -> Network | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT name, network_id, framework, gateway_url, listed, seq
                FROM networks
                WHERE network_id = ?
                """,
                (network_id,),
            ).fetchone()
        if row is None:
            return None
        return _network(row)

    def _next_seq(self) -> int:
        (highest,) = self._conn.execute("SELECT COALESCE(MAX(seq), 0) FROM networks").fetchone()
        return highest + 1


def _network(row: tuple[str, str, str, str, int, int]) -> Network:
    name, network_id, framework, gateway_url, listed, seq = row
    return Network(
        name=name,
        network_id=network_id,
        framework=framework,
        gateway_url=gateway_url,
        listed=bool(listed),
        seq=seq,
    )
