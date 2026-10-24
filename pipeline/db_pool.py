"""pipeline/db_pool.py — Thread-local read connection pool.

Removes per-request cost of sqlite3.connect + 6 PRAGMA + ATTACH shards +
CREATE TEMP VIEW. Each thread keeps one warm connection with shards
already attached and v_* UNION views already built; the connection is
reused across requests for the life of the thread (or until a corrupting
DatabaseError invalidates it).

Why this is safe even though _conn is also used for writes:
  - Writes still target `main` (the unaliased pipeline.db); attached shards
    are read-only views.
  - WAL mode lets multiple processes coexist; busy_timeout absorbs the
    short stalls when the daemon process commits / checkpoints.
  - `check_same_thread` is left at its default (True). Each thread owns
    its own conn, so the SQLite thread-affinity check is honored.
"""
from __future__ import annotations

import sqlite3
import threading
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Optional

from core.logger import get_logger

log = get_logger(__name__)


_INVALIDATING = (
    "malformed",
    "corrupt",
    "disk image",
    "not a database",
    "file is encrypted",
    "i/o error",
)


class ReadPool:
    """Thread-local sqlite connection pool for the read path."""

    def __init__(self, db_file: Path, init_fn: Callable[[sqlite3.Connection], None]):
        self.db_file = Path(db_file)
        self._init_fn = init_fn
        self._max_conns = max(1, int(os.getenv("FB_DB_POOL_MAX_CONNS", "8")))
        self._tls = threading.local()
        self._lock = threading.Lock()
        self._all: list[sqlite3.Connection] = []

    def _build(self, *, register: bool = True) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_file), timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            self._init_fn(conn)
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            raise
        if register:
            with self._lock:
                self._all.append(conn)
        return conn

    @contextmanager
    def acquire(self):
        conn: Optional[sqlite3.Connection] = getattr(self._tls, "conn", None)
        ephemeral = False
        if conn is not None:
            try:
                conn.execute("SELECT 1")
            except sqlite3.Error:
                self._invalidate(conn)
                conn = None
        if conn is None:
            with self._lock:
                can_register = len(self._all) < self._max_conns
            conn = self._build(register=can_register)
            ephemeral = not can_register
            if can_register:
                self._tls.conn = conn

        try:
            yield conn
            try:
                if conn.in_transaction:
                    conn.commit()
            except sqlite3.DatabaseError:
                pass
        except sqlite3.DatabaseError as e:
            try:
                conn.rollback()
            except Exception:
                pass
            msg = str(e).lower()
            if any(token in msg for token in _INVALIDATING):
                self._invalidate(conn)
            raise
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            if ephemeral:
                try:
                    conn.close()
                except Exception:
                    pass

    def _invalidate(self, conn: sqlite3.Connection) -> None:
        try:
            conn.close()
        except Exception:
            pass
        self._tls.conn = None
        with self._lock:
            try:
                self._all.remove(conn)
            except ValueError:
                pass

    def stats(self) -> dict:
        with self._lock:
            return {"open_conns": len(self._all), "max_conns": self._max_conns}

    def close_all(self) -> None:
        with self._lock:
            for c in self._all:
                try:
                    c.close()
                except Exception:
                    pass
            self._all.clear()
