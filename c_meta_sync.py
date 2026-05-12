"""
IMP ↔ SP DB 메타·컬럼 동기화 도구
실행: python3 c_meta_sync.py
       python3 c_meta_sync.py --sync <table_id>
"""
import sys
from contextlib import contextmanager

import psycopg2
import psycopg2.extras


# ── 설정 ──────────────────────────────────────────────────────────────
# SP DB: SSH 리버스 터널 경유 (A서버에서 tunnel.sh start 실행 필요)
# IMP DB: B서버에서 직접 접속

_SP_DB_CONFIG = {
    "host":            "localhost",
    "port":            15432,
    "dbname":          "your_c_database",
    "user":            "your_c_username",
    "password":        "your_c_password",
    "connect_timeout": 10,
}

_IMP_DB_CONFIG = {
    "host":            "d_server_host",
    "port":            5432,
    "dbname":          "your_d_database",
    "user":            "your_d_username",
    "password":        "your_d_password",
    "connect_timeout": 10,
}

DB_SP  = "SP"
DB_IMP = "IMP"


# ── DB 클라이언트 ──────────────────────────────────────────────────────

@contextmanager
def get_connection(target):
    cfg  = _SP_DB_CONFIG if target == DB_SP else _IMP_DB_CONFIG
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
            if target == DB_SP
            else "IMP서버 접속 정보를 확인하세요"
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


# ── 타입 매핑 ──────────────────────────────────────────────────────────

# IMP data_type_name → SP DATA_TYPE_ID (실제 값으로 업데이트 필요)
TYPE_ID_MAP: dict[str, int] = {
    "varchar":   101,
    "integer":   102,
    "bigint":    103,
    "boolean":   104,
    "timestamp": 105,
}

# ── 데이터 조회 ────────────────────────────────────────────────────────

def _fetch_imp_data(table_id):
    with get_connection(DB_IMP) as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                "SELECT table_id, db, name, table_type FROM d_table_meta WHERE table_id = %s",
                (table_id,)
            )
            meta = cur.fetchone()
            if not meta:
                return None, []
            cur.execute(
                "SELECT table_id, column_name, data_type_name, sort_idx, distribution_yn, distribution_idx "
                "FROM d_table_column WHERE table_id = %s ORDER BY sort_idx",
                (table_id,)
            )
            cols = [dict(r) for r in cur.fetchall()]
    return dict(meta), cols


def _fetch_sp_data(table_id):
    with get_connection(DB_SP) as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                'SELECT "TABLE_ID", "DB_NAME", "TABLE_NAME", "DB_CODE", "TABLE_TYPE", "IS_WORKING" FROM "C_TABLE_META" WHERE "TABLE_ID" = %s',
                (table_id,)
            )
            meta = cur.fetchone()
            cur.execute(
                'SELECT "COLUMN_NAME", "DATA_TYPE_NAME", "SORT_IDX", "DISTRIBUTION_YN", "DISTRIBUTION_IDX" '
                'FROM "C_TABLE_COLUMN" WHERE "TABLE_ID" = %s ORDER BY "SORT_IDX"',
                (table_id,)
            )
            cols = [dict(r) for r in cur.fetchall()]
    return (dict(meta) if meta else None), cols


def _imp_col_to_sp(d_row):
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
        "DATA_TYPE_ID":     TYPE_ID_MAP[type_name] if type_name else None,
        "SORT_IDX":         d_row["sort_idx"],
        "DISTRIBUTION_YN":  d_row["distribution_yn"],
        "DISTRIBUTION_IDX": d_row["distribution_idx"],
    }


# ── 동기화 실행 ────────────────────────────────────────────────────────

def _sync_meta(cur, table_id, imp_meta, sp_meta):
    cur.execute(
        "SELECT schema FROM db_code_list WHERE schema_name = %s",
        (imp_meta['db'],)
    )
    row      = cur.fetchone()
    db_code  = row['schema'] if row else None

    if not sp_meta:
        cur.execute(
            'INSERT INTO "C_TABLE_META" '
            '("TABLE_ID", "DB_NAME", "TABLE_NAME", "DB_CODE", "TABLE_TYPE", "IS_WORKING", "CREATE_DATE", "UPDATE_DATE") '
            'VALUES (%s, %s, %s, %s, %s, false, now(), now())',
            (imp_meta['table_id'], imp_meta['db'], imp_meta['name'], db_code, imp_meta['table_type'])
        )
        return "INSERT"

    cur.execute(
        'UPDATE "C_TABLE_META" '
        'SET "DB_NAME" = %s, "TABLE_NAME" = %s, "DB_CODE" = %s, "TABLE_TYPE" = %s, "UPDATE_DATE" = now() '
        'WHERE "TABLE_ID" = %s',
        (imp_meta['db'], imp_meta['name'], db_code, imp_meta['table_type'], table_id)
    )
    return "UPDATE"


def _sync_columns(cur, table_id, mapped_cols):
    cur.execute('DELETE FROM "C_TABLE_COLUMN" WHERE "TABLE_ID" = %s', (table_id,))
    if not mapped_cols:
        return 0

    placeholders = ", ".join(["(%s, %s, %s, %s, %s, %s, %s, now(), now())"] * len(mapped_cols))
    params = []
    for d in mapped_cols:
        params.extend((d['TABLE_ID'], d['COLUMN_NAME'], d['DATA_TYPE_ID'], d['DATA_TYPE_NAME'],
                       d['SORT_IDX'], d['DISTRIBUTION_YN'], d['DISTRIBUTION_IDX']))
    cur.execute(
        'INSERT INTO "C_TABLE_COLUMN" '
        '("TABLE_ID", "COLUMN_NAME", "DATA_TYPE_ID", "DATA_TYPE_NAME", "SORT_IDX", '
        '"DISTRIBUTION_YN", "DISTRIBUTION_IDX", "CREATE_DATE", "UPDATE_DATE") VALUES '
        + placeholders,
        params
    )
    return len(mapped_cols)


def apply_sync(table_id, imp_meta, sp_meta, imp_cols):
    mapped_cols = [_imp_col_to_sp(r) for r in imp_cols]
    with get_connection(DB_SP) as conn:
        with get_cursor(conn) as cur:
            meta_op  = _sync_meta(cur, table_id, imp_meta, sp_meta)
            inserted = _sync_columns(cur, table_id, mapped_cols)
    print(f"  메타: {meta_op}")
    print(f"  컬럼: {inserted}개 재삽입")


# ── 터미널 표 출력 ─────────────────────────────────────────────────────

def print_table(rows):
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


# ── 비교 출력 ─────────────────────────────────────────────────────────

def _print_meta_comparison(imp_meta, sp_meta):
    sp = sp_meta or {}
    rows = [
        {"항목": "TABLE_ID",   "IMP (소스)": imp_meta["table_id"],   "SP (현재)": sp.get("TABLE_ID",   "-")},
        {"항목": "DB_NAME",    "IMP (소스)": imp_meta["db"],          "SP (현재)": sp.get("DB_NAME",    "-")},
        {"항목": "TABLE_NAME", "IMP (소스)": imp_meta["name"],        "SP (현재)": sp.get("TABLE_NAME", "-")},
        {"항목": "DB_CODE",    "IMP (소스)": "-",                     "SP (현재)": sp.get("DB_CODE",    "-")},
        {"항목": "TABLE_TYPE", "IMP (소스)": imp_meta["table_type"],  "SP (현재)": sp.get("TABLE_TYPE",  "-")},
        {"항목": "IS_WORKING", "IMP (소스)": "-",                     "SP (현재)": sp.get("IS_WORKING",  "-")},
    ]
    print_table(rows)


def _print_column_comparison(imp_cols, sp_cols):
    ni, ns = len(imp_cols), len(sp_cols)
    rows = []
    for i in range(max(ni, ns)):
        d = imp_cols[i] if i < ni else {}
        c = sp_cols[i]  if i < ns else {}
        rows.append({
            "imp_column":   d.get("column_name",      "-"),
            "imp_type":     d.get("data_type_name",   "-"),
            "imp_sort":     d.get("sort_idx",          "-"),
            "imp_dist_yn":  d.get("distribution_yn",  "-"),
            "imp_dist_idx": d.get("distribution_idx", "-"),
            "sp_column":    c.get("COLUMN_NAME",      "-"),
            "sp_type":      c.get("DATA_TYPE_NAME",   "-"),
            "sp_sort":      c.get("SORT_IDX",          "-"),
            "sp_dist_yn":   c.get("DISTRIBUTION_YN",  "-"),
            "sp_dist_idx":  c.get("DISTRIBUTION_IDX", "-"),
        })
    print_table(rows)


# ── 메뉴 핸들러 ────────────────────────────────────────────────────────

def _input_table_id():
    raw = input("table_id 입력: ").strip()
    try:
        return int(raw)
    except ValueError:
        print("  [오류] 정수를 입력하세요.")
        return None


def handle_select():
    table_id = _input_table_id()
    if table_id is None:
        return

    with get_connection(DB_SP) as conn:
        with get_cursor(conn) as cur:
            cur.execute('SELECT * FROM "C_TABLE_META" WHERE "TABLE_ID" = %s', (table_id,))
            meta = cur.fetchone()
            if not meta:
                print(f"  table_id {table_id} 가 존재하지 않습니다.")
                return
            cur.execute(
                'SELECT * FROM "C_TABLE_COLUMN" WHERE "TABLE_ID" = %s ORDER BY "SORT_IDX"',
                (table_id,)
            )
            cols = [dict(r) for r in cur.fetchall()]

    print("\n[메타 정보]")
    print_table([dict(meta)])
    print("\n[컬럼 정보]")
    print_table(cols)


def handle_sync(table_id=None):
    if table_id is None:
        table_id = _input_table_id()
    if table_id is None:
        return

    imp_meta, imp_cols = _fetch_imp_data(table_id)
    if imp_meta is None:
        print(f"  IMP DB에 table_id {table_id} 가 존재하지 않습니다.")
        return

    sp_meta, sp_cols = _fetch_sp_data(table_id)

    print("\n[메타 비교]")
    _print_meta_comparison(imp_meta, sp_meta)
    print("\n[컬럼 비교]")
    _print_column_comparison(imp_cols, sp_cols)

    answer = input("\n동기화를 진행하시겠습니까? (yes/no): ").strip().lower()
    if answer != "yes":
        print("  취소되었습니다.")
        return

    apply_sync(table_id, imp_meta, sp_meta, imp_cols)


def handle_delete():
    table_id = _input_table_id()
    if table_id is None:
        return

    with get_connection(DB_SP) as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                'SELECT "DB_NAME", "TABLE_NAME" FROM "C_TABLE_META" WHERE "TABLE_ID" = %s',
                (table_id,)
            )
            meta = cur.fetchone()

    if not meta:
        print(f"  table_id {table_id} 가 존재하지 않습니다.")
        return

    db_table = f"{meta['DB_NAME']}.{meta['TABLE_NAME']}"
    answer   = input(
        f"\n  table_id: {table_id} / {db_table}\n"
        f"  정말 삭제하시겠습니까? (yes/no): "
    ).strip().lower()

    if answer != "yes":
        print("  취소되었습니다.")
        return

    with get_connection(DB_SP) as conn:
        with get_cursor(conn) as cur:
            cur.execute('DELETE FROM "C_TABLE_COLUMN" WHERE "TABLE_ID" = %s', (table_id,))
            cur.execute('DELETE FROM "C_TABLE_META"   WHERE "TABLE_ID" = %s', (table_id,))
    print(f"  삭제 완료: {db_table} (table_id: {table_id})")


def _run(func):
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
