"""Pooled PostgreSQL connections with the shared application row interface."""

from contextlib import contextmanager
from hashlib import sha256
import os
from threading import Lock


class StorageUnavailable(RuntimeError):
    """The transaction could not be reliably completed."""


class Row:
    def __init__(self, indexes, values):
        self.indexes = indexes
        self.values = values

    def keys(self):
        return self.indexes.keys()

    def __getitem__(self, field):
        return self.values[self.indexes[field] if isinstance(field, str) else field]

    def __iter__(self):
        return iter(self.values)


def _rows(cursor):
    indexes = {}
    for index, column in enumerate(cursor.description or ()):
        indexes.setdefault(column.name, index)
    return lambda values: Row(indexes, values)


def database_url():
    return os.getenv("MRCARE_DATABASE_URL", "").strip()


_pools = {}
_pool_lock = Lock()


def _pool(dsn):
    from psycopg_pool import ConnectionPool

    with _pool_lock:
        if dsn not in _pools:
            raw = os.getenv("MRCARE_DB_POOL_SIZE", "12")
            size = min(64, max(2, int(raw))) if raw.isascii() and raw.isdigit() and len(raw) <= 2 else 12
            raw_min = os.getenv("MRCARE_DB_POOL_MIN_SIZE", "4")
            minimum = int(raw_min) if raw_min.isascii() and raw_min.isdigit() and len(raw_min) <= 2 else 4
            _pools[dsn] = ConnectionPool(
                dsn, min_size=min(size, max(1, minimum)), max_size=size, max_waiting=96, timeout=3, open=True,
                check=ConnectionPool.check_connection,
                kwargs={
                    "autocommit": True, "row_factory": _rows, "connect_timeout": 3,
                    "options": "-c statement_timeout=8000 -c lock_timeout=5000 -c idle_in_transaction_session_timeout=15000",
                },
            )
        return _pools[dsn]


def pool_stats():
    dsn = database_url()
    return _pool(dsn).get_stats() if dsn else {}


def close_pools():
    with _pool_lock:
        for pool in _pools.values():
            pool.close()
        _pools.clear()


def _values(params):
    return tuple(int(value) if isinstance(value, bool) else value for value in params)


class PostgreSQLConnection:
    def __init__(self, raw):
        self.raw = raw

    def execute(self, query, params=()):
        if query == "BEGIN":
            return None  # The connection context already selected the transaction isolation.
        return self.raw.execute(query.replace("?", "%s"), _values(params)) if params else self.raw.execute(query)

    def executemany(self, query, batches):
        with self.raw.cursor() as cursor:
            cursor.executemany(query.replace("?", "%s"), (_values(params) for params in batches))

    def begin_write(self, scope=None):
        if scope is None:
            # Shared cross-domain invariants continue to use the compatibility lock.
            self.raw.execute("SELECT pg_advisory_xact_lock(1903147721)")
        else:
            key = int.from_bytes(sha256(scope.encode()).digest()[:4], "big", signed=True)
            self.raw.execute("SELECT pg_advisory_xact_lock(19031, %s)", (key,))

    def lock_event_order(self):
        self.raw.execute("SELECT pg_advisory_xact_lock(1903147722)")


@contextmanager
def postgres_connection(dsn, *, read_only, single_statement=False):
    from psycopg import InterfaceError, OperationalError
    from psycopg_pool import PoolTimeout, TooManyRequests

    try:
        with _pool(dsn).connection() as raw:
            if single_statement:
                yield PostgreSQLConnection(raw)
                return
            with raw.transaction():
                if read_only:
                    raw.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                yield PostgreSQLConnection(raw)
    except (OperationalError, InterfaceError, PoolTimeout, TooManyRequests) as exc:
        raise StorageUnavailable("Database is temporarily unavailable.") from exc
