"""
Impala 클라이언트 — 접속 및 DESCRIBE FORMATTED 파싱
"""
from contextlib import contextmanager
from impala.dbapi import connect

import impala_config


@contextmanager
def get_connection():
    conn = None
    try:
        conn = connect(
            host=impala_config.IMPALA_HOST,
            port=impala_config.IMPALA_PORT,
            user=impala_config.IMPALA_USER or None,
            password=impala_config.IMPALA_PASSWORD or None,
            auth_mechanism=impala_config.IMPALA_AUTH,
            use_ssl=impala_config.IMPALA_USE_SSL,
            timeout=impala_config.IMPALA_TIMEOUT,
        )
        yield conn
    finally:
        if conn:
            conn.close()


def describe_columns(db_name, table_name):
    """
    DESCRIBE FORMATTED {db}.{table} 실행 후 일반 컬럼 목록 반환
    반환: [{"column_name": str, "data_type": str}, ...]  (select 순서 = sort_idx 순서)
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"DESCRIBE FORMATTED {db_name}.{table_name}")
            columns = []
            for row in cur.fetchall():
                col_name  = (row[0] or "").strip()
                data_type = (row[1] or "").strip()
                # 빈 col_name 또는 '#' 시작 → 일반 컬럼 섹션 종료
                if not col_name or col_name.startswith("#"):
                    break
                columns.append({"column_name": col_name, "data_type": data_type})
    return columns
