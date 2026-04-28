"""
PostgreSQL DB 클라이언트 — D서버 전용
"""
import psycopg2
import psycopg2.extras
from contextlib import contextmanager

import config


def _build_dsn():
    return (
        f"host={config.D_DB_HOST} "
        f"port={config.D_DB_PORT} "
        f"dbname={config.D_DB_NAME} "
        f"user={config.D_DB_USER} "
        f"password={config.D_DB_PASSWORD} "
        f"connect_timeout={config.DB_CONNECT_TIMEOUT}"
    )


@contextmanager
def get_connection():
    """DB 커넥션 컨텍스트 매니저 — 정상 종료 시 commit, 예외 시 rollback"""
    conn = None
    try:
        conn = psycopg2.connect(_build_dsn())
        yield conn
        conn.commit()
    except psycopg2.OperationalError as e:
        if conn is not None:
            conn.rollback()
        raise ConnectionError(f"D서버 접속 실패 (config.py를 확인하세요): {e}") from e
    except psycopg2.DatabaseError:
        if conn is not None:
            conn.rollback()
        raise
    finally:
        if conn is not None and not conn.closed:
            conn.close()


@contextmanager
def get_cursor(conn):
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        yield cur
    finally:
        cur.close()


# ── SELECT ─────────────────────────────────────────────────────────────

def fetch_all(query):
    with get_connection() as conn:
        with get_cursor(conn) as cur:
            cur.execute(query)
            return [dict(row) for row in cur.fetchall()]


def fetch_one(query):
    with get_connection() as conn:
        with get_cursor(conn) as cur:
            cur.execute(query)
            row = cur.fetchone()
            return dict(row) if row is not None else None


# ── INSERT ─────────────────────────────────────────────────────────────

def insert_one(query):
    """RETURNING 절이 있으면 삽입된 행을 반환, 없으면 None"""
    with get_connection() as conn:
        with get_cursor(conn) as cur:
            cur.execute(query)
            if cur.description:
                row = cur.fetchone()
                return dict(row) if row is not None else None
            return None


# ── UPDATE ─────────────────────────────────────────────────────────────

def update(query):
    with get_connection() as conn:
        with get_cursor(conn) as cur:
            cur.execute(query)
            return cur.rowcount


# ── DELETE ─────────────────────────────────────────────────────────────

def delete(query):
    with get_connection() as conn:
        with get_cursor(conn) as cur:
            cur.execute(query)
            return cur.rowcount
