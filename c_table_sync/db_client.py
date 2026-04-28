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

_C_DB_CONFIG = {
    "host":            config.C_DB_HOST,
    "port":            config.C_DB_PORT,
    "dbname":          config.C_DB_NAME,
    "user":            config.C_DB_USER,
    "password":        config.C_DB_PASSWORD,
    "connect_timeout": config.DB_CONNECT_TIMEOUT,
}

_D_DB_CONFIG = {
    "host":            config.D_DB_HOST,
    "port":            config.D_DB_PORT,
    "dbname":          config.D_DB_NAME,
    "user":            config.D_DB_USER,
    "password":        config.D_DB_PASSWORD,
    "connect_timeout": config.DB_CONNECT_TIMEOUT,
}


@contextmanager
def get_connection(target=DB_C):
    """DB 커넥션 컨텍스트 매니저 — 정상 종료 시 commit, 예외 시 rollback"""
    cfg  = _C_DB_CONFIG if target == DB_C else _D_DB_CONFIG
    conn = None
    try:
        conn = psycopg2.connect(**cfg)
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


def execute_query(query, target=DB_C, fetch_result=False, commit=False):
    """
    fetch_result=True : SELECT — list[dict] 반환 (결과 없으면 빈 리스트)
    commit=True       : INSERT / UPDATE / DELETE — 영향받은 행 수(int) 반환
    """
    with get_connection(target) as conn:
        with get_cursor(conn) as cur:
            cur.execute(query)
            if fetch_result:
                return [dict(row) for row in cur.fetchall()]
            if commit:
                return cur.rowcount
