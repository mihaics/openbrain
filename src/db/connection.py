"""
Database connection management for Open Brain.
"""
import os
from contextlib import contextmanager
from typing import Optional

import yaml
import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor


class DatabaseConfig:
    """Configuration loader for database settings."""
    
    _instance: Optional['DatabaseConfig'] = None
    
    def __init__(self, config_path: str = None):
        # Load YAML defaults first, then let env vars override per-field.
        # The previous all-or-nothing fallback meant setting e.g. DB_HOST
        # alone would silently drop the YAML password.
        if config_path is None:
            config_path = os.path.join(
                os.path.dirname(__file__),
                '..', '..', 'config', 'settings.yaml'
            )

        defaults = {
            'host': 'localhost',
            'port': 5432,
            'name': 'openbrain',
            'user': 'postgres',
            'password': '',
        }
        try:
            with open(config_path, 'r') as f:
                cfg = (yaml.safe_load(f) or {}).get('database', {}) or {}
            defaults.update({
                'host': cfg.get('host', defaults['host']),
                'port': int(cfg.get('port', defaults['port'])),
                'name': cfg.get('name', defaults['name']),
                'user': cfg.get('user', defaults['user']),
                'password': cfg.get('password', defaults['password']),
            })
        except Exception:
            pass

        self.host = os.environ.get('DB_HOST', defaults['host'])
        self.port = int(os.environ.get('DB_PORT', defaults['port']))
        self.name = os.environ.get('DB_NAME', defaults['name'])
        self.user = os.environ.get('DB_USER', defaults['user'])
        self.password = os.environ.get('DB_PASSWORD', defaults['password'])
    
    @classmethod
    def get_instance(cls, config_path: str = None) -> 'DatabaseConfig':
        if cls._instance is None:
            cls._instance = cls(config_path)
        return cls._instance


class ConnectionPool:
    """PostgreSQL connection pool manager."""
    
    _pool: Optional[pool.ThreadedConnectionPool] = None
    
    def __init__(self, minconn: int = 1, maxconn: int = 10):
        self.minconn = minconn
        self.maxconn = maxconn
    
    def initialize(self) -> None:
        """Initialize the connection pool."""
        if self._pool is None:
            config = DatabaseConfig.get_instance()
            self._pool = pool.ThreadedConnectionPool(
                self.minconn,
                self.maxconn,
                host=config.host,
                port=config.port,
                database=config.name,
                user=config.user,
                password=config.password
            )
    
    @contextmanager
    def get_connection(self):
        """Get a connection from the pool.

        Broken connections (server restart, idle timeout) are discarded via
        `putconn(close=True)` instead of being recycled — otherwise the next
        caller inherits an `InterfaceError` from a dead socket.
        """
        if self._pool is None:
            self.initialize()

        conn = self._pool.getconn()
        broken = False
        try:
            yield conn
        except (psycopg2.InterfaceError, psycopg2.OperationalError):
            broken = True
            raise
        finally:
            # conn.closed != 0 when libpq has torn down the socket; drop it.
            self._pool.putconn(conn, close=broken or bool(conn.closed))

    @contextmanager
    def get_cursor(self, dict_cursor: bool = True):
        """Get a cursor from the pool."""
        with self.get_connection() as conn:
            cursor = conn.cursor(
                cursor_factory=RealDictCursor if dict_cursor else None
            )
            try:
                yield cursor
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except psycopg2.InterfaceError:
                    # Socket already gone; let get_connection mark it broken.
                    pass
                raise
            finally:
                try:
                    cursor.close()
                except psycopg2.InterfaceError:
                    pass
    
    def close_all(self) -> None:
        """Close all connections in the pool."""
        if self._pool is not None:
            self._pool.closeall()
            self._pool = None


# Global connection pool instance
_pool = ConnectionPool()


def get_pool() -> ConnectionPool:
    """Get the global connection pool."""
    return _pool


@contextmanager
def get_db_connection():
    """Context manager for database connections."""
    with _pool.get_connection() as conn:
        yield conn


@contextmanager
def get_db_cursor(dict_cursor: bool = True):
    """Context manager for database cursors."""
    with _pool.get_cursor(dict_cursor) as cursor:
        yield cursor


def init_db(config_path: str = None) -> None:
    """Initialize the database connection pool."""
    if config_path:
        DatabaseConfig._instance = None
        DatabaseConfig.get_instance(config_path)
    _pool.initialize()


_VECTOR_DIM_CACHE: Optional[int] = None


def get_vector_dim() -> Optional[int]:
    """Return the declared dimension of memory.embedding, or None if unknown."""
    global _VECTOR_DIM_CACHE
    if _VECTOR_DIM_CACHE is not None:
        return _VECTOR_DIM_CACHE
    try:
        with get_db_cursor() as cursor:
            cursor.execute("""
                SELECT atttypmod
                FROM pg_attribute
                WHERE attrelid = 'memory'::regclass
                  AND attname = 'embedding'
                  AND NOT attisdropped
            """)
            row = cursor.fetchone()
        if row:
            dim = row['atttypmod'] if isinstance(row, dict) else row[0]
            if dim and dim > 0:
                _VECTOR_DIM_CACHE = int(dim)
                return _VECTOR_DIM_CACHE
    except Exception:
        pass
    return None


class SchemaMismatchError(RuntimeError):
    """The database schema is not what this code requires.

    Distinct from every other startup error on purpose: the servers catch
    broad exceptions during initialisation and carry on in a degraded state,
    which for a schema mismatch would mean running-but-broken rather than a
    barrier. Callers must let this one escape.
    """


# name -> (data_type, is_nullable, default_contains)
REQUIRED_COLUMNS = {
    "access_count": ("integer", "NO", "0"),
    "last_accessed_at": ("timestamp without time zone", "YES", None),
}


def validate_schema() -> None:
    """Verify the access-tracking columns exist AND are correctly defined.

    Existence is not enough: ALTER TABLE ... ADD COLUMN IF NOT EXISTS silently
    does nothing when a column of the wrong type, nullability or default is
    already present, so an existence-only check would pass on a schema that
    breaks at query time.
    """
    with get_db_cursor() as cursor:
        cursor.execute(
            """
            SELECT column_name, data_type, is_nullable, column_default
              FROM information_schema.columns
             WHERE table_name = 'memory' AND column_name = ANY(%s)
            """,
            (list(REQUIRED_COLUMNS),),
        )
        found = {r["column_name"]: r for r in cursor.fetchall()}

    for name, (dtype, nullable, default) in REQUIRED_COLUMNS.items():
        row = found.get(name)
        if row is None:
            raise SchemaMismatchError(
                f"memory.{name} is missing - run `openbrain migrate`"
            )
        if row["data_type"] != dtype:
            raise SchemaMismatchError(
                f"memory.{name} is {row['data_type']}, expected {dtype} - "
                f"run `openbrain migrate` after correcting it"
            )
        if row["is_nullable"] != nullable:
            expected = "nullable" if nullable == "YES" else "not nullable"
            raise SchemaMismatchError(
                f"memory.{name} nullability is wrong: expected {expected}"
            )
        if default is not None and default not in (row["column_default"] or ""):
            raise SchemaMismatchError(
                f"memory.{name} default is {row['column_default']!r}, "
                f"expected one containing {default!r}"
            )
