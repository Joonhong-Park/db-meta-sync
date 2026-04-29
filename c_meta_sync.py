"""
D ↔ C DB 메타·컬럼 동기화 도구
실행: python3 c_meta_sync.py
       python3 c_meta_sync.py --sync <table_id>
"""
import os
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime

import psycopg2
import psycopg2.extras


# ── 설정 ──────────────────────────────────────────────────────────────

_C_DB_CONFIG = {
    "host":            os.environ.get("C_DB_HOST",     "localhost"),
    "port":            int(os.environ.get("C_DB_PORT", "15432")),
    "dbname":          os.environ.get("C_DB_NAME",     "your_c_database"),
    "user":            os.environ.get("C_DB_USER",     "your_c_username"),
    "password":        os.environ.get("C_DB_PASSWORD", "your_c_password"),
    "connect_timeout": int(os.environ.get("DB_CONNECT_TIMEOUT", "10")),
}

_D_DB_CONFIG = {
    "host":            os.environ.get("D_DB_HOST",     "d_server_host"),
    "port":            int(os.environ.get("D_DB_PORT", "5432")),
    "dbname":          os.environ.get("D_DB_NAME",     "your_d_database"),
    "user":            os.environ.get("D_DB_USER",     "your_d_username"),
    "password":        os.environ.get("D_DB_PASSWORD", "your_d_password"),
    "connect_timeout": int(os.environ.get("DB_CONNECT_TIMEOUT", "10")),
}

DB_C = "C"
DB_D = "D"


# ── DB 클라이언트 ──────────────────────────────────────────────────────

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


# ── 매핑 정의 ──────────────────────────────────────────────────────────

@dataclass
class TypeMapping:
    data_type_id: int
    display_data_type: str


@dataclass
class TableMapping:
    source_table: str
    target_table: str
    column_map: dict

    def __post_init__(self):
        if "table_id" not in self.column_map:
            raise ValueError(f"column_map에 'table_id' 항목이 없습니다: {self.source_table}")


C_TIMESTAMP_COLS = ("create_date_ts", "update_date_ts")

TYPE_ID_MAP = {
    1: TypeMapping(data_type_id=101, display_data_type="varchar"),
    2: TypeMapping(data_type_id=102, display_data_type="integer"),
    3: TypeMapping(data_type_id=103, display_data_type="bigint"),
    4: TypeMapping(data_type_id=104, display_data_type="boolean"),
    5: TypeMapping(data_type_id=105, display_data_type="timestamp"),
}

TABLE_MAPPINGS = {
    "table_meta": TableMapping(
        source_table="d_table_meta",
        target_table="c_table_meta",
        column_map={
            "table_id": "table_id",
            "db":       "db_name",
            "name":     "table_name",
        },
    ),
    "table_column": TableMapping(
        source_table="d_table_column",
        target_table="c_table_column",
        column_map={
            "table_id":         "table_id",
            "column_name":      "column_name",
            "data_type":        "display_data_type",
            "type_id":          "data_type_id",
            "sort_idx":         "sort_idx",
            "distribution_yn":  "distribution_yn",
            "distribution_idx": "distribution_idx",
        },
    ),
}

_META = TABLE_MAPPINGS["table_meta"]
_COL  = TABLE_MAPPINGS["table_column"]

_COL_VISIBLE    = ["column_name", "display_data_type", "distribution_yn", "distribution_idx"]
_COL_UPDATEABLE = ["display_data_type", "data_type_id", "distribution_yn", "distribution_idx"]


# ── 동기화 유틸 ────────────────────────────────────────────────────────

def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _q(identifier):
    return f'"{identifier}"'


def _val(v):
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def _map_row(row, col_map):
    return {col_map[k]: v for k, v in row.items() if k in col_map}


def _resolve_type_id(row, d_type_id_raw):
    if d_type_id_raw is None:
        return row
    if d_type_id_raw not in TYPE_ID_MAP:
        raise ValueError(
            f"TYPE_ID_MAP에 없는 type_id: {d_type_id_raw} "
            f"(column: {row.get('column_name')}) — TYPE_ID_MAP을 확인하세요."
        )
    mapping = TYPE_ID_MAP[d_type_id_raw]
    return {**row, "data_type_id": mapping.data_type_id, "display_data_type": mapping.display_data_type}


# ── 데이터 조회 ────────────────────────────────────────────────────────

def _fetch_d_meta(table_id):
    cols   = ", ".join(_META.column_map.keys())
    result = execute_query(
        f"SELECT {cols} FROM {_META.source_table} WHERE table_id = {table_id}",
        target=DB_D, fetch_result=True,
    )
    return result[0] if result else None


def _fetch_c_meta(table_id):
    cols   = ", ".join(_q(c) for c in _META.column_map.values())
    result = execute_query(
        f'SELECT {cols} FROM {_q(_META.target_table)} WHERE {_q("table_id")} = {table_id}',
        target=DB_C, fetch_result=True,
    )
    return result[0] if result else None


def _fetch_d_columns(table_id):
    cols = ", ".join(_COL.column_map.keys())
    return execute_query(
        f"SELECT {cols} FROM {_COL.source_table} "
        f"WHERE table_id = {table_id} ORDER BY sort_idx",
        target=DB_D, fetch_result=True,
    )


def _fetch_c_columns(table_id):
    cols = ", ".join(_q(c) for c in _COL.column_map.values())
    return execute_query(
        f'SELECT {cols} FROM {_q(_COL.target_table)} '
        f'WHERE {_q("table_id")} = {table_id} ORDER BY {_q("sort_idx")}',
        target=DB_C, fetch_result=True,
    )


# ── 비교 데이터 생성 ────────────────────────────────────────────────────

def build_comparison(table_id):
    d_meta = _fetch_d_meta(table_id)
    c_meta = _fetch_c_meta(table_id)
    d_cols = _fetch_d_columns(table_id)
    c_cols = _fetch_c_columns(table_id)

    meta_diffs = []
    if d_meta:
        mapped_d = _map_row(d_meta, _META.column_map)
        for c_col in [c for c in _META.column_map.values() if c != "table_id"]:
            d_val = mapped_d.get(c_col)
            c_val = c_meta.get(c_col) if c_meta else None
            if c_meta is None:
                status = "C없음"
            elif d_val == c_val:
                status = "동일"
            else:
                status = "변경"
            meta_diffs.append({"field": c_col, "d_val": d_val, "c_val": c_val, "status": status})

    mapped_d_cols = []
    for r in d_cols:
        mapped = _map_row(r, _COL.column_map)
        mapped["_d_data_type_raw"] = r.get("data_type")
        mapped_d_cols.append(_resolve_type_id(mapped, r.get("type_id")))

    d_col_map = {(r["column_name"], r["sort_idx"]): r for r in mapped_d_cols}
    c_col_map = {(r["column_name"], r["sort_idx"]): r for r in c_cols}
    all_keys  = sorted(set(d_col_map) | set(c_col_map), key=lambda k: (k[1], k[0]))

    column_diffs = []
    for col_name, sort_idx in all_keys:
        d_row = d_col_map.get((col_name, sort_idx))
        c_row = c_col_map.get((col_name, sort_idx))

        if d_row and c_row:
            status = "변경" if any(d_row.get(v) != c_row.get(v) for v in _COL_VISIBLE) else "동일"
        elif d_row:
            status = "추가 예정"
        else:
            status = "삭제 예정"

        column_diffs.append({
            "sort_idx":            sort_idx,
            "d_column_name":       d_row.get("column_name")       if d_row else None,
            "d_data_type":         d_row.get("_d_data_type_raw")  if d_row else None,
            "c_column_name":       c_row.get("column_name")       if c_row else None,
            "c_display_data_type": c_row.get("display_data_type") if c_row else None,
            "d_dist_yn":           d_row.get("distribution_yn")   if d_row else None,
            "d_dist_idx":          d_row.get("distribution_idx")  if d_row else None,
            "c_dist_yn":           c_row.get("distribution_yn")   if c_row else None,
            "c_dist_idx":          c_row.get("distribution_idx")  if c_row else None,
            "status":              status,
            "d_row":               d_row,
            "c_row":               c_row,
        })

    has_changes = (
        any(d["status"] != "동일" for d in meta_diffs) or
        any(d["status"] != "동일" for d in column_diffs)
    )

    return {
        "table_id":     table_id,
        "d_meta":       d_meta,
        "c_meta":       c_meta,
        "meta_diffs":   meta_diffs,
        "column_diffs": column_diffs,
        "has_changes":  has_changes,
    }


# ── 비교 화면 출력 ──────────────────────────────────────────────────────

def _print_rows(headers, rows):
    if not rows:
        print("  (없음)")
        return
    widths = [max(len(h), max(len(str(r[i])) for r in rows)) for i, h in enumerate(headers)]
    sep = "+-" + "-+-".join("-" * w for w in widths) + "-+"
    print(sep)
    print("| " + " | ".join(h.ljust(widths[i]) for i, h in enumerate(headers)) + " |")
    print(sep)
    for row in rows:
        print("| " + " | ".join(str(v).ljust(widths[i]) for i, v in enumerate(row)) + " |")
    print(sep)


def _str(val):
    return str(val) if val is not None else "(없음)"


def print_comparison(cmp):
    print(f"\n[table_id {cmp['table_id']} 비교]")

    print("\n[메타 비교]")
    if not cmp["meta_diffs"]:
        print("  D에 해당 table_id가 없습니다.")
    else:
        _print_rows(
            ["항목", "D값", "C값", "상태"],
            [[d["field"], _str(d["d_val"]), _str(d["c_val"]), d["status"]] for d in cmp["meta_diffs"]],
        )

    print("\n[컬럼 비교]")
    if not cmp["column_diffs"]:
        print("  컬럼 정보가 없습니다.")
    else:
        _print_rows(
            ["sort_idx", "D_column_name", "D_data_type", "C_column_name", "C_display_data_type", "상태"],
            [[_str(d["sort_idx"]), _str(d["d_column_name"]), _str(d["d_data_type"]),
              _str(d["c_column_name"]), _str(d["c_display_data_type"]), d["status"]]
             for d in cmp["column_diffs"]],
        )

    dist_rows = [d for d in cmp["column_diffs"]
                 if any([d["d_dist_yn"], d["d_dist_idx"], d["c_dist_yn"], d["c_dist_idx"]])]
    if dist_rows:
        print("\n[distribution 정보]")
        _print_rows(
            ["sort_idx", "distribution_yn", "distribution_idx", "상태"],
            [[
                _str(d["sort_idx"]),
                _str(d["d_dist_yn"]  if d["d_dist_yn"]  is not None else d["c_dist_yn"]),
                _str(d["d_dist_idx"] if d["d_dist_idx"] is not None else d["c_dist_idx"]),
                d["status"],
            ] for d in dist_rows],
        )


# ── 동기화 실행 ────────────────────────────────────────────────────────

def _sync_meta(cur, table_id, cmp, now):
    if not cmp["d_meta"]:
        return None

    mapped = _map_row(cmp["d_meta"], _META.column_map)
    non_pk = [c for c in _META.column_map.values() if c != "table_id"]

    if cmp["c_meta"] is None:
        c_cols     = list(_META.column_map.values()) + list(C_TIMESTAMP_COLS)
        cols_q     = ", ".join(_q(c) for c in c_cols)
        vals       = [mapped[c] for c in _META.column_map.values()] + [now, now]
        values_str = ", ".join(_val(v) for v in vals)
        cur.execute(f'INSERT INTO {_q(_META.target_table)} ({cols_q}) VALUES ({values_str})')
        return "INSERT"

    if any(d["status"] == "변경" for d in cmp["meta_diffs"]):
        set_parts = [f'{_q(c)} = {_val(mapped[c])}' for c in non_pk]
        set_parts.append(f'{_q("update_date_ts")} = {_val(now)}')
        cur.execute(
            f'UPDATE {_q(_META.target_table)} SET {", ".join(set_parts)} '
            f'WHERE {_q("table_id")} = {table_id}'
        )
        return "UPDATE"

    return None


def _sync_columns(cur, table_id, cmp, now):
    inserted = updated = deleted = 0

    for diff in cmp["column_diffs"]:
        status = diff["status"]
        d_row  = diff["d_row"]
        c_row  = diff["c_row"]

        if status == "추가 예정" and d_row:
            c_cols     = list(_COL.column_map.values()) + list(C_TIMESTAMP_COLS)
            cols_q     = ", ".join(_q(c) for c in c_cols)
            vals       = [d_row[c] for c in _COL.column_map.values()] + [now, now]
            values_str = ", ".join(_val(v) for v in vals)
            cur.execute(f'INSERT INTO {_q(_COL.target_table)} ({cols_q}) VALUES ({values_str})')
            inserted += 1

        elif status == "변경" and d_row and c_row:
            set_parts = [f'{_q(c)} = {_val(d_row[c])}' for c in _COL_UPDATEABLE]
            set_parts.append(f'{_q("update_date_ts")} = {_val(now)}')
            cur.execute(
                f'UPDATE {_q(_COL.target_table)} SET {", ".join(set_parts)} '
                f'WHERE {_q("table_id")} = {table_id} '
                f'AND {_q("column_name")} = {_val(c_row["column_name"])} '
                f'AND {_q("sort_idx")} = {c_row["sort_idx"]}'
            )
            updated += 1

        elif status == "삭제 예정" and c_row:
            cur.execute(
                f'DELETE FROM {_q(_COL.target_table)} '
                f'WHERE {_q("table_id")} = {table_id} '
                f'AND {_q("column_name")} = {_val(c_row["column_name"])} '
                f'AND {_q("sort_idx")} = {c_row["sort_idx"]}'
            )
            deleted += 1

    return inserted, updated, deleted


def apply_sync(table_id, cmp):
    now = _now()
    with get_connection(DB_C) as conn:
        with get_cursor(conn) as cur:
            meta_op = _sync_meta(cur, table_id, cmp, now)
            inserted, updated, deleted = _sync_columns(cur, table_id, cmp, now)
    print("\n동기화 완료")
    if meta_op:
        print(f"  메타: {meta_op}")
    print(f"  컬럼: 추가 {inserted} / 수정 {updated} / 삭제 {deleted}")


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

    rows = execute_query(
        f'SELECT * FROM "c_table_meta" WHERE "table_id" = {table_id}',
        fetch_result=True,
    )
    if not rows:
        print(f"  table_id {table_id} 가 존재하지 않습니다.")
        return

    print("\n[메타 정보]")
    print_table(rows)

    columns = execute_query(
        f'SELECT * FROM "c_table_column" WHERE "table_id" = {table_id} ORDER BY "sort_idx"',
        fetch_result=True,
    )
    print("\n[컬럼 정보]")
    print_table(columns)


def handle_sync(table_id=None):
    if table_id is None:
        table_id = _input_table_id()
    if table_id is None:
        return

    cmp = build_comparison(table_id)

    if cmp["d_meta"] is None:
        print(f"  D DB에 table_id {table_id} 가 존재하지 않습니다.")
        return

    print_comparison(cmp)

    if not cmp["has_changes"]:
        print("\n  변경 사항이 없습니다.")
        return

    answer = input("\n동기화를 진행하시겠습니까? (yes/no): ").strip().lower()
    if answer != "yes":
        print("  취소되었습니다.")
        return

    apply_sync(table_id, cmp)


def handle_delete():
    table_id = _input_table_id()
    if table_id is None:
        return

    rows = execute_query(
        f'SELECT "table_id", "db_name", "table_name" FROM "c_table_meta" WHERE "table_id" = {table_id}',
        fetch_result=True,
    )
    if not rows:
        print(f"  table_id {table_id} 가 존재하지 않습니다.")
        return

    meta     = rows[0]
    db_table = f"{meta['db_name']}.{meta['table_name']}"
    answer   = input(
        f"\n  table_id: {table_id} / {db_table}\n"
        f"  정말 삭제하시겠습니까? (yes/no): "
    ).strip().lower()

    if answer != "yes":
        print("  취소되었습니다.")
        return

    execute_query(f'DELETE FROM "c_table_column" WHERE "table_id" = {table_id}', commit=True)
    execute_query(f'DELETE FROM "c_table_meta" WHERE "table_id" = {table_id}', commit=True)
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
