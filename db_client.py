"""
PostgreSQL DB 클라이언트
C서버(SSH 터널 경유)와 D서버(직접 접속) 두 DB를 DBTarget으로 구분
"""
import psycopg2
import psycopg2.extras
from contextlib import contextmanager
from enum import Enum
from typing import Any, Generator, Iterator

import config


class DBTarget(Enum):
    C = "C"  # C서버 DB — SSH 리버스 터널 경유 (localhost:15432)
    D = "D"  # D서버 DB — 직접 접속


def _build_dsn(target: DBTarget) -> str:
    """접속 대상에 따른 DSN 문자열 생성"""
    if target == DBTarget.C:
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
def get_connection(
    target: DBTarget = DBTarget.C,
) -> Generator[psycopg2.extensions.connection, None, None]:
    """
    DB 커넥션 컨텍스트 매니저
    - 정상 종료 시 commit, 예외 발생 시 rollback 후 예외 전파
    """
    conn: psycopg2.extensions.connection | None = None
    try:
        conn = psycopg2.connect(_build_dsn(target))
        yield conn
        conn.commit()
    except psycopg2.OperationalError as e:
        if conn is not None:
            conn.rollback()
        hint = (
            "A서버에서 tunnel.sh start를 실행했는지 확인하세요"
            if target == DBTarget.C
            else "D서버 접속 정보(config.py)를 확인하세요"
        )
        raise ConnectionError(
            f"[DB-{target.value}] 연결 실패 ({hint}): {e}"
        ) from e
    except psycopg2.DatabaseError:
        if conn is not None:
            conn.rollback()
        raise
    finally:
        if conn is not None and not conn.closed:
            conn.close()


@contextmanager
def get_cursor(
    conn: psycopg2.extensions.connection,
) -> Iterator[psycopg2.extensions.cursor]:
    """커서 컨텍스트 매니저 (커넥션은 외부에서 관리)"""
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        yield cur
    finally:
        cur.close()


# ── SELECT ─────────────────────────────────────────────────────────────

def fetch_all(
    query: str,
    params: tuple[Any, ...] | None = None,
    target: DBTarget = DBTarget.C,
) -> list[dict[str, Any]]:
    """SELECT 다건 조회"""
    with get_connection(target) as conn:
        with get_cursor(conn) as cur:
            cur.execute(query, params)
            return [dict(row) for row in cur.fetchall()]


def fetch_one(
    query: str,
    params: tuple[Any, ...] | None = None,
    target: DBTarget = DBTarget.C,
) -> dict[str, Any] | None:
    """SELECT 단건 조회"""
    with get_connection(target) as conn:
        with get_cursor(conn) as cur:
            cur.execute(query, params)
            row = cur.fetchone()
            return dict(row) if row is not None else None


# ── INSERT ─────────────────────────────────────────────────────────────

def insert_one(
    query: str,
    params: tuple[Any, ...],
    target: DBTarget = DBTarget.C,
) -> dict[str, Any] | None:
    """
    단건 INSERT
    RETURNING 절이 있으면 삽입된 행을 반환, 없으면 None 반환
    """
    with get_connection(target) as conn:
        with get_cursor(conn) as cur:
            cur.execute(query, params)
            if cur.description:
                row = cur.fetchone()
                return dict(row) if row is not None else None
            return None


def insert_bulk(
    query: str,
    params_list: list[tuple[Any, ...]],
    target: DBTarget = DBTarget.C,
) -> int:
    """bulk INSERT (executemany), 삽입 행 수 반환"""
    if not params_list:
        return 0
    with get_connection(target) as conn:
        with get_cursor(conn) as cur:
            cur.executemany(query, params_list)
            return len(params_list)


# ── UPDATE ─────────────────────────────────────────────────────────────

def update(
    query: str,
    params: tuple[Any, ...],
    target: DBTarget = DBTarget.C,
) -> int:
    """UPDATE 실행, 영향받은 행 수 반환"""
    with get_connection(target) as conn:
        with get_cursor(conn) as cur:
            cur.execute(query, params)
            return cur.rowcount


# ── DELETE ─────────────────────────────────────────────────────────────

def delete(
    query: str,
    params: tuple[Any, ...],
    target: DBTarget = DBTarget.C,
) -> int:
    """DELETE 실행, 영향받은 행 수 반환"""
    with get_connection(target) as conn:
        with get_cursor(conn) as cur:
            cur.execute(query, params)
            return cur.rowcount
