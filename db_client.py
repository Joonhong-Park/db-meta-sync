"""
PostgreSQL DB 클라이언트
C서버(SSH 터널 경유), D서버(직접 접속)
"""
import psycopg2
import psycopg2.extras
from contextlib import contextmanager

import config

DB_C = "C"  # C서버 DB — SSH 리버스 터널 경유 (localhost:15432)
DB_D = "D"  # D서버 DB — 직접 접속


def _build_dsn(target):
    if target == DB_C:
        return (
            f"host={config.C_DB_HOST} "
            f"port={config.C_DB_PORT} "
            f"dbname={config.C_DB_NAME} "
            f"user={config.C_DB_USER} "
            f"password={config.C_DB_PASSWORD} "
            f"connect_timeout={config.DB_CONNECT_TIMEOUT}"
        )
    return (
        f"host={config.D_DB_HOST} "
        f"port={config.D_DB_PORT} "
        f"dbname={config.D_DB_NAME} "
        f"user={config.D_DB_USER} "
        f"password={config.D_DB_PASSWORD} "
        f"connect_timeout={config.DB_CONNECT_TIMEOUT}"
    )


@contextmanager
def get_connection(target=DB_C):
    """DB 커넥션 컨텍스트 매니저 — 정상 종료 시 commit, 예외 시 rollback"""
    conn = None
    try:
        conn = psycopg2.connect(_build_dsn(target))
        yield conn
        conn.commit()
    except psycopg2.OperationalError as e:
        if conn is not None:
            conn.rollback()
        hint = (
            "A서버에서 tunnel.sh start를 실행했는지 확인하세요"
            if target == DB_C
            else "D서버 접속 정보(config.py)를 확인하세요"
        )
        raise ConnectionError(f"[DB-{target}] 연결 실패 ({hint}): {e}") from e
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

def fetch_all(query, params=None, target=DB_C):
    with get_connection(target) as conn:
        with get_cursor(conn) as cur:
            cur.execute(query, params)
            return [dict(row) for row in cur.fetchall()]


def fetch_one(query, params=None, target=DB_C):
    with get_connection(target) as conn:
        with get_cursor(conn) as cur:
            cur.execute(query, params)
            row = cur.fetchone()
            return dict(row) if row is not None else None


# ── INSERT ─────────────────────────────────────────────────────────────

def insert_one(query, params, target=DB_C):
    """RETURNING 절이 있으면 삽입된 행을 반환, 없으면 None"""
    with get_connection(target) as conn:
        with get_cursor(conn) as cur:
            cur.execute(query, params)
            if cur.description:
                row = cur.fetchone()
                return dict(row) if row is not None else None
            return None


def insert_bulk(query, params_list, target=DB_C):
    if not params_list:
        return 0
    with get_connection(target) as conn:
        with get_cursor(conn) as cur:
            cur.executemany(query, params_list)
            return len(params_list)


# ── UPDATE ─────────────────────────────────────────────────────────────

def update(query, params, target=DB_C):
    with get_connection(target) as conn:
        with get_cursor(conn) as cur:
            cur.execute(query, params)
            return cur.rowcount


# ── DELETE ─────────────────────────────────────────────────────────────

def delete(query, params, target=DB_C):
    with get_connection(target) as conn:
        with get_cursor(conn) as cur:
            cur.execute(query, params)
            return cur.rowcount
