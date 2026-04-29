"""
Impala → D DB 컬럼 동기화 도구
실행: python3 impala_sync.py <table_id>
"""
import os
import sys
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
from impala.dbapi import connect as impala_connect


# ── 설정 ──────────────────────────────────────────────────────────────

_D_DB_CONFIG = {
    "host":            os.environ.get("D_DB_HOST",     "d_server_host"),
    "port":            int(os.environ.get("D_DB_PORT", "5432")),
    "dbname":          os.environ.get("D_DB_NAME",     "your_d_database"),
    "user":            os.environ.get("D_DB_USER",     "your_d_username"),
    "password":        os.environ.get("D_DB_PASSWORD", "your_d_password"),
    "connect_timeout": int(os.environ.get("DB_CONNECT_TIMEOUT", "10")),
}

_IMPALA_CONFIG = {
    "host":            os.environ.get("IMPALA_HOST",     "localhost"),
    "port":            int(os.environ.get("IMPALA_PORT", "21050")),
    "user":            os.environ.get("IMPALA_USER",     "") or None,
    "password":        os.environ.get("IMPALA_PASSWORD", "") or None,
    "auth_mechanism":  os.environ.get("IMPALA_AUTH",     "PLAIN"),
    "use_ssl":         os.environ.get("IMPALA_USE_SSL",  "false").lower() == "true",
    "timeout":         int(os.environ.get("IMPALA_TIMEOUT", "30")),
}

# Impala 타입 → D type_id  (실제 D DB type_id 값으로 업데이트 필요)
IMPALA_TYPE_MAP = {
    "string":    1,
    "int":       2,
    "bigint":    3,
    "long":      3,
    "double":    4,
    "timestamp": 5,
    "date":      6,
}


# ── D DB 클라이언트 ────────────────────────────────────────────────────

@contextmanager
def get_connection():
    """DB 커넥션 컨텍스트 매니저 — 정상 종료 시 commit, 예외 시 rollback"""
    conn = None
    try:
        conn = psycopg2.connect(**_D_DB_CONFIG)
        yield conn
        conn.commit()
    except psycopg2.OperationalError as e:
        if conn is not None:
            conn.rollback()
        raise ConnectionError(f"D서버 접속 실패 (_D_DB_CONFIG를 확인하세요): {e}") from e
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
    fetch_result=True : SELECT — list[dict] 반환
    commit=True       : INSERT / UPDATE / DELETE — 영향받은 행 수 반환
    """
    with get_connection() as conn:
        with get_cursor(conn) as cur:
            cur.execute(query)
            if fetch_result:
                return [dict(row) for row in cur.fetchall()]
            if commit:
                return cur.rowcount


# ── Impala 클라이언트 ──────────────────────────────────────────────────

@contextmanager
def _impala_connection():
    conn = None
    try:
        conn = impala_connect(**_IMPALA_CONFIG)
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
                      이름만 수집 후 일반 컬럼 섹션의 data_type 사용 (순서 유지)
    """
    with _impala_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"DESCRIBE FORMATTED {db_name}.{table_name}")
            rows = cur.fetchall()

    columns   = []
    split_idx = 0
    for i, row in enumerate(rows):
        col_name = (row[0] or "").strip()
        if not col_name or col_name.startswith("#"):
            split_idx = i
            break
        columns.append({"column_name": col_name, "data_type": (row[1] or "").strip(), "is_partition": False})

    in_partition       = False
    skip_next_header   = False
    is_iceberg         = False
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
                break
            continue

        if not col_name:
            break

        if is_iceberg:
            iceberg_part_names.add(col_name)
        else:
            columns.append({"column_name": col_name, "data_type": data_type, "is_partition": True})

    if is_iceberg:
        for col in columns:
            if col["column_name"] in iceberg_part_names:
                col["is_partition"] = True

    return columns


# ── 타입 매핑 ──────────────────────────────────────────────────────────

def resolve_type_id(impala_type):
    base = impala_type.lower().split("(")[0].strip()
    return IMPALA_TYPE_MAP.get(base)


def _dist_idx(data_type):
    """파티션 컬럼 타입 → distribution_idx (timestamp: 1, string: 2)"""
    base = data_type.lower().split("(")[0].strip()
    if base == "timestamp":
        return 1
    if base == "string":
        return 2
    return None


# ── 동기화 실행 ────────────────────────────────────────────────────────

def sync_columns(table_id):
    rows = execute_query(
        f"SELECT table_id, db, name FROM d_table_meta WHERE table_id = {table_id}",
        fetch_result=True,
    )
    if not rows:
        print(f"  [오류] D DB에 table_id {table_id} 가 존재하지 않습니다.")
        sys.exit(1)

    meta       = rows[0]
    db_name    = meta["db"]
    table_name = meta["name"]
    full_name  = f"{db_name}.{table_name}"
    print(f"  대상: {full_name} (table_id: {table_id})")

    print(f"  Impala DESCRIBE FORMATTED {full_name} ...")
    columns = describe_columns(db_name, table_name)
    if not columns:
        print("  [오류] Impala에서 컬럼 정보를 가져올 수 없습니다.")
        sys.exit(1)

    regular   = [c for c in columns if not c["is_partition"]]
    partition = [c for c in columns if c["is_partition"]]
    print(f"  일반 컬럼 {len(regular)}개, 파티션 컬럼 {len(partition)}개")

    unmapped = [c for c in columns if resolve_type_id(c["data_type"]) is None]
    if unmapped:
        for c in unmapped:
            print(f"  [오류] type_map에 없는 타입: '{c['data_type']}' (column: {c['column_name']})")
        print("  IMPALA_TYPE_MAP을 업데이트하세요.")
        sys.exit(1)

    bad_part = [c for c in partition if _dist_idx(c["data_type"]) is None]
    if bad_part:
        for c in bad_part:
            print(f"  [오류] 파티션 타입 미지원: '{c['data_type']}' (column: {c['column_name']})")
        sys.exit(1)

    with get_connection() as conn:
        with get_cursor(conn) as cur:
            cur.execute(f"DELETE FROM d_table_column WHERE table_id = {table_id}")
            deleted = cur.rowcount

            for sort_idx, col in enumerate(columns, start=1):
                col_name  = col["column_name"].replace("'", "''")
                data_type = col["data_type"].replace("'", "''")
                type_id   = resolve_type_id(col["data_type"])

                if col["is_partition"]:
                    dist_yn  = "'Y'"
                    dist_idx = _dist_idx(col["data_type"])
                else:
                    dist_yn  = "NULL"
                    dist_idx = "NULL"

                cur.execute(
                    f"INSERT INTO d_table_column "
                    f"(table_id, column_name, data_type, type_id, sort_idx, distribution_yn, distribution_idx) "
                    f"VALUES ({table_id}, '{col_name}', '{data_type}', {type_id}, {sort_idx}, {dist_yn}, {dist_idx})"
                )

    print(f"  기존 컬럼 {deleted}개 삭제, 새 컬럼 {len(columns)}개 삽입 완료")
    if partition:
        for col in partition:
            print(f"    파티션: {col['column_name']} ({col['data_type']}) → distribution_idx={_dist_idx(col['data_type'])}")


# ── 메인 ──────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) != 2:
        print("사용법: python3 impala_sync.py <table_id>")
        sys.exit(1)

    try:
        table_id = int(sys.argv[1])
    except ValueError:
        print("[오류] table_id는 정수여야 합니다.")
        sys.exit(1)

    try:
        sync_columns(table_id)
    except ConnectionError as e:
        print(f"[연결 오류] {e}")
        sys.exit(1)
    except Exception as e:
        print(f"[오류] {type(e).__name__}: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
