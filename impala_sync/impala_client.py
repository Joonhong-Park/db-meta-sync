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
    DESCRIBE FORMATTED {db}.{table} 실행 후 일반 컬럼과 파티션 컬럼을 분리하여 반환

    반환:
        regular    : [{"column_name": str, "data_type": str}, ...]  (sort_idx 순서)
        partitions : [{"column_name": str, "data_type": str}, ...]  (파티션 컬럼, 1~2개)
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"DESCRIBE FORMATTED {db_name}.{table_name}")
            rows = cur.fetchall()

    regular    = []
    partitions = []

    # ── 일반 컬럼 파싱 (첫 번째 빈 행 또는 '#' 전까지) ──────────────────
    split_idx = 0
    for i, row in enumerate(rows):
        col_name = (row[0] or "").strip()
        if not col_name or col_name.startswith("#"):
            split_idx = i
            break
        regular.append({"column_name": col_name, "data_type": (row[1] or "").strip()})

    # ── 파티션 컬럼 파싱 (# Partition Information 섹션) ──────────────────
    in_partition   = False
    skip_next_header = False
    for row in rows[split_idx:]:
        col_name  = (row[0] or "").strip()
        data_type = (row[1] or "").strip()

        if col_name == "# Partition Information":
            in_partition     = True
            skip_next_header = True  # 다음 '# col_name  data_type  comment' 헤더 행 스킵
            continue

        if not in_partition:
            continue

        if col_name.startswith("#"):
            if skip_next_header:
                skip_next_header = False  # 헤더 행 1개 스킵
            else:
                break  # # Detailed Table Information 등 다음 섹션 → 종료
            continue

        if not col_name:
            break  # 빈 행 → 파티션 섹션 종료

        partitions.append({"column_name": col_name, "data_type": data_type})

    return regular, partitions
