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
                listed INTEGER NOT NULL
            )
            """
        )
        self._conn.commit()

    def seed(self, records: tuple[Network, ...] = NETWORKS) -> None:
        """Insert the hosted networks if they are missing. Never overwrite them."""
        with self._lock:
            for record in records:
                self._conn.execute(
                    """
                    INSERT OR IGNORE INTO networks
                        (network_id, name, framework, gateway_url, listed)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        record.network_id,
                        record.name,
                        record.framework,
                        record.gateway_url,
                        int(record.listed),
                    ),
                )
            self._conn.commit()

    def add(self, network: Network) -> None:
        with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT INTO networks
                        (network_id, name, framework, gateway_url, listed)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        network.network_id,
                        network.name,
                        network.framework,
                        network.gateway_url,
                        int(network.listed),
                    ),
                )
                self._conn.commit()
            except sqlite3.IntegrityError:
                self._conn.rollback()
                raise Collision(network.network_id) from None

    def listed(self) -> tuple[Network, ...]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT name, network_id, framework, gateway_url, listed
                FROM networks
                WHERE listed = 1
                ORDER BY rowid
                """
            ).fetchall()
        return tuple(_network(row) for row in rows)

    def get(self, network_id: str) -> Network | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT name, network_id, framework, gateway_url, listed
                FROM networks
                WHERE network_id = ?
                """,
                (network_id,),
            ).fetchone()
        if row is None:
            return None
        return _network(row)


def _network(row: tuple[str, str, str, str, int]) -> Network:
    name, network_id, framework, gateway_url, listed = row
    return Network(
        name=name,
        network_id=network_id,
        framework=framework,
        gateway_url=gateway_url,
        listed=bool(listed),
    )
