"""
Impala → D DB 컬럼 동기화
실행: python3 main.py <table_id>
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db_client import get_connection, get_cursor, execute_query
from impala_client import describe_columns
from type_map import resolve_type_id


def _dist_idx(data_type):
    """파티션 컬럼 타입 → distribution_idx (timestamp: 1, string: 2)"""
    base = data_type.lower().split("(")[0].strip()
    if base == "timestamp":
        return 1
    if base == "string":
        return 2
    return None


def sync_columns(table_id):
    # 1. D DB에서 db, name 조회
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

    # 2. Impala DESCRIBE FORMATTED
    print(f"  Impala DESCRIBE FORMATTED {full_name} ...")
    columns = describe_columns(db_name, table_name)
    if not columns:
        print("  [오류] Impala에서 컬럼 정보를 가져올 수 없습니다.")
        sys.exit(1)

    regular   = [c for c in columns if not c["is_partition"]]
    partition = [c for c in columns if c["is_partition"]]
    print(f"  일반 컬럼 {len(regular)}개, 파티션 컬럼 {len(partition)}개")

    # 3. type_id 매핑 사전 검증
    unmapped = [c for c in columns if resolve_type_id(c["data_type"]) is None]
    if unmapped:
        for c in unmapped:
            print(f"  [오류] type_map에 없는 타입: '{c['data_type']}' (column: {c['column_name']})")
        print("  type_map.py의 IMPALA_TYPE_MAP을 업데이트하세요.")
        sys.exit(1)

    # 4. 파티션 distribution_idx 검증
    bad_part = [c for c in partition if _dist_idx(c["data_type"]) is None]
    if bad_part:
        for c in bad_part:
            print(f"  [오류] 파티션 타입 미지원: '{c['data_type']}' (column: {c['column_name']})")
        sys.exit(1)

    # 5. 단일 트랜잭션: 기존 컬럼 전체 삭제 후 새 컬럼 삽입
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


def main():
    if len(sys.argv) != 2:
        print("사용법: python3 main.py <table_id>")
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
