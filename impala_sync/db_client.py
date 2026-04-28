"""
PostgreSQL DB 클라이언트 — D서버 전용
"""
import psycopg2
import psycopg2.extras
from contextlib import contextmanager

import config

_DB_CONFIG = {
    "host":            config.D_DB_HOST,
    "port":            config.D_DB_PORT,
    "dbname":          config.D_DB_NAME,
    "user":            config.D_DB_USER,
    "password":        config.D_DB_PASSWORD,
    "connect_timeout": config.DB_CONNECT_TIMEOUT,
}


@contextmanager
def get_connection():
    """DB 커넥션 컨텍스트 매니저 — 정상 종료 시 commit, 예외 시 rollback"""
    conn = None
    try:
        conn = psycopg2.connect(**_DB_CONFIG)
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


def execute_query(query, fetch_result=False, commit=False):
    """
    fetch_result=True : SELECT — list[dict] 반환 (결과 없으면 빈 리스트)
    commit=True       : INSERT / UPDATE / DELETE — 영향받은 행 수(int) 반환
    """
    with get_connection() as conn:
        with get_cursor(conn) as cur:
            cur.execute(query)
            if fetch_result:
                return [dict(row) for row in cur.fetchall()]
            if commit:
                return cur.rowcount
