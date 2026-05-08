"""
D ↔ C DB 메타·컬럼 동기화 도구
실행: python3 c_meta_sync.py
       python3 c_meta_sync.py --sync <table_id>
"""
import sys
from contextlib import contextmanager
from dataclasses import dataclass

import psycopg2
import psycopg2.extras


# ── 설정 ──────────────────────────────────────────────────────────────
# C DB: SSH 리버스 터널 경유 (A서버에서 tunnel.sh start 실행 필요)
# D DB: B서버에서 직접 접속

_C_DB_CONFIG = {
    "host":            "localhost",
    "port":            15432,
    "dbname":          "your_c_database",
    "user":            "your_c_username",
    "password":        "your_c_password",
    "connect_timeout": 10,
}

_D_DB_CONFIG = {
    "host":            "d_server_host",
    "port":            5432,
    "dbname":          "your_d_database",
    "user":            "your_d_username",
    "password":        "your_d_password",
    "connect_timeout": 10,
}

DB_C = "C"
DB_D = "D"


# ── DB 클라이언트 ──────────────────────────────────────────────────────

@contextmanager
def get_connection(target=DB_C):
    """
    DB 커넥션 컨텍스트 매니저.
    - 정상 종료 시 commit, 예외 시 rollback 후 재raise
    - target=DB_C(기본): C DB, target=DB_D: D DB
    - 단일 트랜잭션이 필요한 경우 직접 사용 (apply_sync 참고)
    """
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
            else "D서버 접속 정보를 확인하세요"
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
    """RealDictCursor 반환 — 결과 행이 dict로 접근 가능"""
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        yield cur
    finally:
        cur.close()


def execute_query(query, target=DB_C, fetch_result=False, commit=False):
    """
    단건 쿼리 실행 헬퍼.
    - fetch_result=True : SELECT — list[dict] 반환 (결과 없으면 빈 리스트)
    - commit=True       : INSERT/UPDATE/DELETE — 영향받은 행 수(int) 반환
    - 단일 트랜잭션이 필요하면 get_connection/get_cursor를 직접 사용
    """
    with get_connection(target) as conn:
        with get_cursor(conn) as cur:
            cur.execute(query)
            if fetch_result:
                return [dict(row) for row in cur.fetchall()]
            if commit:
                return cur.rowcount


# ── 타입 매핑 ──────────────────────────────────────────────────────────

@dataclass
class TypeMapping:
    data_type_id:   int   # C DB의 DATA_TYPE_ID 값
    data_type_name: str   # C DB의 DATA_TYPE_NAME 값 (= D DB의 data_type_name과 동일)


# D data_type_name → C DATA_TYPE_ID + DATA_TYPE_NAME
# 키: D d_table_column.data_type_name 의 실제 값
# 값: C C_TABLE_COLUMN 에 들어갈 DATA_TYPE_ID (실제 값으로 업데이트 필요)
TYPE_ID_MAP: dict[str, TypeMapping] = {
    "varchar":   TypeMapping(data_type_id=101, data_type_name="varchar"),
    "integer":   TypeMapping(data_type_id=102, data_type_name="integer"),
    "bigint":    TypeMapping(data_type_id=103, data_type_name="bigint"),
    "boolean":   TypeMapping(data_type_id=104, data_type_name="boolean"),
    "timestamp": TypeMapping(data_type_id=105, data_type_name="timestamp"),
}

# ── 데이터 조회 ────────────────────────────────────────────────────────

def _fetch_d_meta(table_id):
    """D DB에서 테이블 메타 1건 조회. 없으면 None."""
    result = execute_query(
        f"SELECT table_id, db, name FROM d_table_meta WHERE table_id = {table_id}",
        target=DB_D, fetch_result=True,
    )
    return result[0] if result else None


def _fetch_c_meta(table_id):
    """C DB에서 테이블 메타 1건 조회. 없으면 None."""
    result = execute_query(
        f'SELECT "TABLE_ID", "DB_NAME", "TABLE_NAME" FROM "C_TABLE_META" WHERE "TABLE_ID" = {table_id}',
        fetch_result=True,
    )
    return result[0] if result else None


def _fetch_d_columns(table_id):
    """D DB에서 컬럼 목록 조회. data_type_name(문자열)을 가져옴."""
    return execute_query(
        f"SELECT table_id, column_name, data_type_name, sort_idx, distribution_yn, distribution_idx "
        f"FROM d_table_column WHERE table_id = {table_id} ORDER BY sort_idx",
        target=DB_D, fetch_result=True,
    )


def _d_col_to_c(d_row):
    """
    D 컬럼 행 1개를 C 컬럼명 공간으로 변환.
    data_type_name → TYPE_ID_MAP 조회로 DATA_TYPE_ID 파생.
    매핑에 없는 data_type_name이면 즉시 ValueError.
    """
    type_name = d_row.get("data_type_name")
    if type_name and type_name not in TYPE_ID_MAP:
        raise ValueError(
            f"TYPE_ID_MAP에 없는 data_type_name: '{type_name}' "
            f"(column: {d_row.get('column_name')})"
        )
    return {
        "TABLE_ID":         d_row["table_id"],
        "COLUMN_NAME":      d_row["column_name"],
        "DATA_TYPE_NAME":   type_name,
        "DATA_TYPE_ID":     TYPE_ID_MAP[type_name].data_type_id if type_name else None,
        "SORT_IDX":         d_row["sort_idx"],
        "DISTRIBUTION_YN":  d_row["distribution_yn"],
        "DISTRIBUTION_IDX": d_row["distribution_idx"],
    }


# ── 동기화 실행 ────────────────────────────────────────────────────────

def _sync_meta(cur, table_id, d_meta, c_meta):
    """
    테이블 메타 동기화 (C_TABLE_META).

    케이스별 동작:
    - D없음 + C없음 : skip (None 반환)
    - D없음 + C있음 : DELETE
    - D있음 + C없음 : INSERT (CREATE_DATE = UPDATE_DATE = now())
    - D있음 + C있음 : D 기준으로 UPDATE (UPDATE_DATE = now())

    반환: "INSERT" | "UPDATE" | "DELETE" | None
    """
    if not d_meta and not c_meta:
        return None

    if not d_meta:
        cur.execute(f'DELETE FROM "C_TABLE_META" WHERE "TABLE_ID" = {table_id}')
        return "DELETE"

    if not c_meta:
        cur.execute(
            f"INSERT INTO \"C_TABLE_META\" (\"TABLE_ID\", \"DB_NAME\", \"TABLE_NAME\", \"CREATE_DATE\", \"UPDATE_DATE\") "
            f"VALUES ({d_meta['table_id']}, '{d_meta['db']}', '{d_meta['name']}', now(), now())"
        )
        return "INSERT"

    if d_meta["db"] != c_meta["DB_NAME"] or d_meta["name"] != c_meta["TABLE_NAME"]:
        cur.execute(
            f"UPDATE \"C_TABLE_META\" "
            f"SET \"DB_NAME\" = '{d_meta['db']}', \"TABLE_NAME\" = '{d_meta['name']}', \"UPDATE_DATE\" = now() "
            f"WHERE \"TABLE_ID\" = {table_id}"
        )
        return "UPDATE"

    return None


def _sync_columns(cur, table_id, mapped_d_cols):
    """
    컬럼 전체 재삽입 (C_TABLE_COLUMN).
    기존 컬럼 전부 DELETE 후 D 컬럼 전부 INSERT.
    CREATE_DATE / UPDATE_DATE 모두 now().

    반환: 삽입된 컬럼 수
    """
    cur.execute(f'DELETE FROM "C_TABLE_COLUMN" WHERE "TABLE_ID" = {table_id}')

    for d in mapped_d_cols:
        dist_yn  = f"'{d['DISTRIBUTION_YN']}'" if d['DISTRIBUTION_YN']  is not None else 'NULL'
        dist_idx = str(d['DISTRIBUTION_IDX'])   if d['DISTRIBUTION_IDX'] is not None else 'NULL'
        cur.execute(
            f"INSERT INTO \"C_TABLE_COLUMN\" "
            f"(\"TABLE_ID\", \"COLUMN_NAME\", \"DATA_TYPE_ID\", \"DATA_TYPE_NAME\", \"SORT_IDX\", "
            f"\"DISTRIBUTION_YN\", \"DISTRIBUTION_IDX\", \"CREATE_DATE\", \"UPDATE_DATE\") "
            f"VALUES ({d['TABLE_ID']}, '{d['COLUMN_NAME']}', {d['DATA_TYPE_ID']}, '{d['DATA_TYPE_NAME']}', "
            f"{d['SORT_IDX']}, {dist_yn}, {dist_idx}, now(), now())"
        )

    return len(mapped_d_cols)


def apply_sync(table_id):
    """
    D에서 데이터를 조회해 메타 + 컬럼을 단일 트랜잭션으로 동기화.
    어느 쪽이든 실패 시 전체 rollback.
    """
    d_meta       = _fetch_d_meta(table_id)
    c_meta       = _fetch_c_meta(table_id)
    mapped_d_cols = [_d_col_to_c(r) for r in _fetch_d_columns(table_id)]

    with get_connection(DB_C) as conn:
        with get_cursor(conn) as cur:
            meta_op  = _sync_meta(cur, table_id, d_meta, c_meta)
            inserted = _sync_columns(cur, table_id, mapped_d_cols)

    if meta_op:
        print(f"  메타: {meta_op}")
    print(f"  컬럼: {inserted}개 재삽입")


# ── 터미널 표 출력 ─────────────────────────────────────────────────────

def print_table(rows):
    """dict 목록을 ASCII 표로 출력 (handle_select에서 사용)"""
    if not rows:
        print("  (결과 없음)")
        return
    columns = list(rows[0].keys())
    widths  = {col: max(len(col), max(len(str(r[col])) for r in rows)) for col in columns}
    sep     = "+-" + "-+-".join("-" * widths[c] for c in columns) + "-+"
    print(sep)
    print("| " + " | ".join(c.ljust(widths[c]) for c in columns) + " |")
    print(sep)
    for row in rows:
        print("| " + " | ".join(str(row[c]).ljust(widths[c]) for c in columns) + " |")
    print(sep)
    print(f"  {len(rows)}행")


# ── 메뉴 핸들러 ────────────────────────────────────────────────────────

def _input_table_id():
    raw = input("table_id 입력: ").strip()
    try:
        return int(raw)
    except ValueError:
        print("  [오류] 정수를 입력하세요.")
        return None


def handle_select():
    """메뉴 1 — C DB에서 메타 + 컬럼 정보 조회 후 출력"""
    table_id = _input_table_id()
    if table_id is None:
        return

    rows = execute_query(
        f'SELECT * FROM "C_TABLE_META" WHERE "TABLE_ID" = {table_id}',
        fetch_result=True,
    )
    if not rows:
        print(f"  table_id {table_id} 가 존재하지 않습니다.")
        return

    print("\n[메타 정보]")
    print_table(rows)

    columns = execute_query(
        f'SELECT * FROM "C_TABLE_COLUMN" WHERE "TABLE_ID" = {table_id} ORDER BY "SORT_IDX"',
        fetch_result=True,
    )
    print("\n[컬럼 정보]")
    print_table(columns)


def handle_sync(table_id=None):
    """
    메뉴 2 — 확인 입력 후 동기화 실행.
    --sync <table_id> 인자로도 진입 가능.
    D에 table_id가 없으면 동기화 불가 (D가 소스).
    """
    if table_id is None:
        table_id = _input_table_id()
    if table_id is None:
        return

    if _fetch_d_meta(table_id) is None:
        print(f"  D DB에 table_id {table_id} 가 존재하지 않습니다.")
        return

    answer = input("동기화를 진행하시겠습니까? (yes/no): ").strip().lower()
    if answer != "yes":
        print("  취소되었습니다.")
        return

    apply_sync(table_id)


def handle_delete():
    """
    메뉴 3 — C DB에서 컬럼 + 메타 삭제.
    FK 순서: C_TABLE_COLUMN 먼저 삭제 후 C_TABLE_META 삭제.
    """
    table_id = _input_table_id()
    if table_id is None:
        return

    rows = execute_query(
        f'SELECT "TABLE_ID", "DB_NAME", "TABLE_NAME" FROM "C_TABLE_META" WHERE "TABLE_ID" = {table_id}',
        fetch_result=True,
    )
    if not rows:
        print(f"  table_id {table_id} 가 존재하지 않습니다.")
        return

    meta     = rows[0]
    db_table = f"{meta['DB_NAME']}.{meta['TABLE_NAME']}"
    answer   = input(
        f"\n  table_id: {table_id} / {db_table}\n"
        f"  정말 삭제하시겠습니까? (yes/no): "
    ).strip().lower()

    if answer != "yes":
        print("  취소되었습니다.")
        return

    execute_query(f'DELETE FROM "C_TABLE_COLUMN" WHERE "TABLE_ID" = {table_id}', commit=True)
    execute_query(f'DELETE FROM "C_TABLE_META" WHERE "TABLE_ID" = {table_id}', commit=True)
    print(f"  삭제 완료: {db_table} (table_id: {table_id})")


def _run(func):
    """메뉴 핸들러 공통 예외 처리 래퍼"""
    try:
        func()
    except ConnectionError as e:
        print(f"\n  [연결 오류] {e}")
    except Exception as e:
        print(f"\n  [오류] {type(e).__name__}: {e}")


# ── 메인 ──────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]
    if len(args) == 2 and args[0] == "--sync":
        try:
            handle_sync(int(args[1]))
        except ValueError:
            print("[오류] table_id는 정수여야 합니다.")
            sys.exit(1)
        except ConnectionError as e:
            print(f"[연결 오류] {e}")
            sys.exit(1)
        except Exception as e:
            print(f"[오류] {type(e).__name__}: {e}")
            sys.exit(1)
        return

    while True:
        print("\n=== DB 동기화 도구 ===")
        print("1. 테이블 정보 조회")
        print("2. 테이블 동기화")
        print("3. 테이블 정보 삭제")
        print("0. 종료")
        choice = input("선택: ").strip()

        if choice == "1":
            _run(handle_select)
        elif choice == "2":
            _run(handle_sync)
        elif choice == "3":
            _run(handle_delete)
        elif choice == "0":
            print("종료합니다.")
            break
        else:
            print("  1, 2, 3, 0 중에서 선택하세요.")


if __name__ == "__main__":
    main()
