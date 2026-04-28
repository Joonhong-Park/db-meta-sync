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
    DESCRIBE FORMATTED {db}.{table} 실행 후 컬럼 목록을 순서 그대로 반환

    반환: [{"column_name": str, "data_type": str, "is_partition": bool}, ...]

    - 일반 테이블 : 파티션 컬럼은 # Partition Information 섹션에서 파싱
    - Iceberg 테이블: 파티션 컬럼은 # Partition Transform Information 섹션에서
                      이름만 수집 후 일반 컬럼 섹션의 data_type을 사용
                      (컬럼 순서는 일반 컬럼 섹션 기준으로 유지)
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"DESCRIBE FORMATTED {db_name}.{table_name}")
            rows = cur.fetchall()

    # ── 일반 컬럼 파싱 ──────────────────────────────────────────────────
    columns = []
    split_idx = 0
    for i, row in enumerate(rows):
        col_name = (row[0] or "").strip()
        if not col_name or col_name.startswith("#"):
            split_idx = i
            break
        columns.append({"column_name": col_name, "data_type": (row[1] or "").strip(), "is_partition": False})

    # ── 파티션 섹션 파싱 ────────────────────────────────────────────────
    in_partition     = False
    skip_next_header = False
    is_iceberg       = False
    iceberg_part_names = set()

    for row in rows[split_idx:]:
        col_name  = (row[0] or "").strip()
        data_type = (row[1] or "").strip()

        if col_name in ("# Partition Information", "# Partition Transform Information"):
            in_partition     = True
            skip_next_header = True
            is_iceberg       = (col_name == "# Partition Transform Information")
            continue

        if not in_partition:
            continue

        if col_name.startswith("#"):
            if skip_next_header:
                skip_next_header = False
            else:
                break  # 다음 섹션 시작 → 종료
            continue

        if not col_name:
            break  # 빈 행 → 섹션 종료

        if is_iceberg:
            iceberg_part_names.add(col_name)
        else:
            # 일반 테이블: 파티션 컬럼이 별도 섹션에 data_type과 함께 존재
            columns.append({"column_name": col_name, "data_type": data_type, "is_partition": True})

    # Iceberg: 일반 컬럼 섹션에서 파티션 컬럼에 플래그 설정
    if is_iceberg:
        for col in columns:
            if col["column_name"] in iceberg_part_names:
                col["is_partition"] = True

    return columns
