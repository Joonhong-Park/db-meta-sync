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

# 컬럼 비교 시 변경 여부 판단 대상 (DATA_TYPE_ID는 DATA_TYPE_NAME에서 파생되므로 미포함)
_COL_VISIBLE = ["COLUMN_NAME", "DATA_TYPE_NAME", "SORT_IDX", "DISTRIBUTION_YN", "DISTRIBUTION_IDX"]


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


def _fetch_c_columns(table_id):
    """C DB에서 컬럼 목록 조회. DATA_TYPE_ID와 DATA_TYPE_NAME 둘 다 가져옴."""
    return execute_query(
        f'SELECT "TABLE_ID", "COLUMN_NAME", "DATA_TYPE_ID", "DATA_TYPE_NAME", "SORT_IDX", '
        f'"DISTRIBUTION_YN", "DISTRIBUTION_IDX" '
        f'FROM "C_TABLE_COLUMN" WHERE "TABLE_ID" = {table_id} ORDER BY "SORT_IDX"',
        fetch_result=True,
    )


# ── 비교 데이터 생성 ────────────────────────────────────────────────────

def _d_col_to_c(d_row):
    """
    D 컬럼 행 1개를 C 컬럼명 공간으로 변환.
    - D의 소문자 컬럼명 → C의 대문자 컬럼명
    - data_type_name → TYPE_ID_MAP 조회로 DATA_TYPE_ID 파생
    - 매핑에 없는 data_type_name이면 즉시 ValueError (동기화 전 조기 차단)
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


def build_comparison(table_id):
    """
    D/C 양쪽 데이터를 조회해 비교 결과 구조체를 반환.

    반환 구조:
    {
        "table_id":     int,
        "d_meta":       dict | None,   # D에서 조회한 원본 행
        "c_meta":       dict | None,   # C에서 조회한 원본 행
        "meta_diffs":   list[dict],    # 필드별 D값/C값/상태
        "column_diffs": list[dict],    # 컬럼별 D행/C행/상태
        "has_changes":  bool,          # 메뉴에서 동기화 진행 여부 판단용
    }

    meta_diffs 상태값: "동일" | "변경" | "C없음"
    column_diffs 상태값: "동일" | "변경" | "추가 예정" | "삭제 예정"
    """
    d_meta = _fetch_d_meta(table_id)
    c_meta = _fetch_c_meta(table_id)
    d_cols = _fetch_d_columns(table_id)
    c_cols = _fetch_c_columns(table_id)

    # ── 메타 비교 ──
    # D에 행이 있을 때만 필드별 비교 수행 (D없음 케이스는 _sync_meta에서 처리)
    meta_diffs = []
    if d_meta:
        for d_key, c_key in [("db", "DB_NAME"), ("name", "TABLE_NAME")]:
            d_val  = d_meta.get(d_key)
            c_val  = c_meta.get(c_key) if c_meta else None
            status = "C없음" if not c_meta else ("동일" if d_val == c_val else "변경")
            meta_diffs.append({"field": c_key, "d_val": d_val, "c_val": c_val, "status": status})

    # ── 컬럼 비교 ──
    # D 컬럼을 C 컬럼명 공간으로 변환 후 COLUMN_NAME 기준으로 매칭
    mapped_d_cols = [_d_col_to_c(r) for r in d_cols]
    d_col_map = {r["COLUMN_NAME"]: r for r in mapped_d_cols}
    c_col_map = {r["COLUMN_NAME"]: r for r in c_cols}
    all_keys  = sorted(set(d_col_map) | set(c_col_map))

    column_diffs = []
    for col_name in all_keys:
        d_row = d_col_map.get(col_name)
        c_row = c_col_map.get(col_name)

        if d_row and c_row:
            # 양쪽 존재: _COL_VISIBLE 항목 중 하나라도 다르면 "변경"
            status = "변경" if any(d_row.get(v) != c_row.get(v) for v in _COL_VISIBLE) else "동일"
        elif d_row:
            status = "추가 예정"   # D에만 존재 → C에 INSERT 필요
        else:
            status = "삭제 예정"   # C에만 존재 → C에서 DELETE 필요

        column_diffs.append({
            "sort_idx":         d_row.get("SORT_IDX")         if d_row else c_row.get("SORT_IDX"),
            "d_column_name":    d_row.get("COLUMN_NAME")      if d_row else None,
            "d_data_type_name": d_row.get("DATA_TYPE_NAME")   if d_row else None,
            "c_column_name":    c_row.get("COLUMN_NAME")      if c_row else None,
            "c_data_type_name": c_row.get("DATA_TYPE_NAME")   if c_row else None,
            "d_dist_yn":        d_row.get("DISTRIBUTION_YN")  if d_row else None,
            "d_dist_idx":       d_row.get("DISTRIBUTION_IDX") if d_row else None,
            "c_dist_yn":        c_row.get("DISTRIBUTION_YN")  if c_row else None,
            "c_dist_idx":       c_row.get("DISTRIBUTION_IDX") if c_row else None,
            "status":           status,
            "d_row":            d_row,   # _sync_columns에서 INSERT/UPDATE 값으로 사용
            "c_row":            c_row,   # _sync_columns에서 WHERE 조건 식별에 사용
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
    """헤더 + 데이터 행을 ASCII 표로 출력"""
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
    """
    build_comparison 결과를 터미널 표로 출력.
    메타 비교 → 컬럼 비교 → distribution 정보(값 있는 행만) 순서.
    """
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
            ["sort_idx", "D_column_name", "D_data_type_name", "C_column_name", "C_data_type_name", "상태"],
            [[_str(d["sort_idx"]), _str(d["d_column_name"]), _str(d["d_data_type_name"]),
              _str(d["c_column_name"]), _str(d["c_data_type_name"]), d["status"]]
             for d in cmp["column_diffs"]],
        )

    # distribution 값이 있는 컬럼만 별도 섹션으로 표시
    dist_rows = [d for d in cmp["column_diffs"]
                 if any((d["d_dist_yn"], d["d_dist_idx"], d["c_dist_yn"], d["c_dist_idx"]))]
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

def _sync_meta(cur, table_id, cmp):
    """
    테이블 메타 동기화 (C_TABLE_META).
    타임스탬프는 PostgreSQL now() 사용.

    케이스별 동작:
    - D없음 + C없음 : skip (None 반환)
    - D없음 + C있음 : C에서 DELETE
    - D있음 + C없음 : C에 INSERT (CREATE_DATE = UPDATE_DATE = now())
    - D있음 + C있음 : 변경 있으면 UPDATE (UPDATE_DATE = now()), 없으면 skip

    반환: "INSERT" | "UPDATE" | "DELETE" | None
    """
    d = cmp["d_meta"]
    c = cmp["c_meta"]

    if not d and not c:
        return None

    if not d and c:
        # D에서 테이블이 삭제된 경우 C에서도 제거
        cur.execute(f'DELETE FROM "C_TABLE_META" WHERE "TABLE_ID" = {table_id}')
        return "DELETE"

    if not c:
        # C에 아직 없는 테이블 → 신규 등록
        cur.execute(
            f"INSERT INTO \"C_TABLE_META\" (\"TABLE_ID\", \"DB_NAME\", \"TABLE_NAME\", \"CREATE_DATE\", \"UPDATE_DATE\") "
            f"VALUES ({d['table_id']}, '{d['db']}', '{d['name']}', now(), now())"
        )
        return "INSERT"

    if any(diff["status"] == "변경" for diff in cmp["meta_diffs"]):
        # DB_NAME 또는 TABLE_NAME이 D 기준으로 변경됨
        cur.execute(
            f"UPDATE \"C_TABLE_META\" "
            f"SET \"DB_NAME\" = '{d['db']}', \"TABLE_NAME\" = '{d['name']}', \"UPDATE_DATE\" = now() "
            f"WHERE \"TABLE_ID\" = {table_id}"
        )
        return "UPDATE"

    return None


def _sync_columns(cur, table_id, cmp):
    """
    컬럼 목록 증분 동기화 (C_TABLE_COLUMN).
    타임스탬프는 PostgreSQL now() 사용.

    매칭 기준 : COLUMN_NAME
      → 같은 이름의 컬럼을 D/C 양쪽에서 찾아 비교

    동일 판단 : _COL_VISIBLE 항목(COLUMN_NAME, DATA_TYPE_NAME, SORT_IDX,
                DISTRIBUTION_YN, DISTRIBUTION_IDX) 모두 일치해야 "동일"

    행 식별   : UPDATE/DELETE의 WHERE 절은 TABLE_ID + COLUMN_NAME + SORT_IDX
      → c_row(C의 기존 값)로 행을 찾고, d_row(D의 새 값)로 갱신
      → sort_idx가 바뀌는 경우에도 기존 행을 정확히 식별하기 위해 c_row 기준 사용

    반환: (inserted, updated, deleted) 건수
    """
    inserted = updated = deleted = 0

    for diff in cmp["column_diffs"]:
        status = diff["status"]
        d = diff["d_row"]   # D에서 가져온 행 (C 컬럼명 공간으로 변환된 상태)
        c = diff["c_row"]   # C에서 가져온 현재 행

        if status == "추가 예정" and d:
            # D에만 존재하는 컬럼 → C에 신규 삽입
            # nullable: DISTRIBUTION_YN(문자열), DISTRIBUTION_IDX(정수)
            dist_yn  = f"'{d['DISTRIBUTION_YN']}'" if d['DISTRIBUTION_YN']  is not None else 'NULL'
            dist_idx = str(d['DISTRIBUTION_IDX'])   if d['DISTRIBUTION_IDX'] is not None else 'NULL'
            cur.execute(
                f"INSERT INTO \"C_TABLE_COLUMN\" "
                f"(\"TABLE_ID\", \"COLUMN_NAME\", \"DATA_TYPE_ID\", \"DATA_TYPE_NAME\", \"SORT_IDX\", "
                f"\"DISTRIBUTION_YN\", \"DISTRIBUTION_IDX\", \"CREATE_DATE\", \"UPDATE_DATE\") "
                f"VALUES ({d['TABLE_ID']}, '{d['COLUMN_NAME']}', {d['DATA_TYPE_ID']}, '{d['DATA_TYPE_NAME']}', "
                f"{d['SORT_IDX']}, {dist_yn}, {dist_idx}, now(), now())"
            )
            inserted += 1

        elif status == "변경" and d and c:
            # D/C 모두 존재하나 값이 다름 → C를 D 기준으로 갱신
            # WHERE: C의 기존 COLUMN_NAME + SORT_IDX로 행 식별 (sort_idx 변경 케이스 대응)
            dist_yn  = f"'{d['DISTRIBUTION_YN']}'" if d['DISTRIBUTION_YN']  is not None else 'NULL'
            dist_idx = str(d['DISTRIBUTION_IDX'])   if d['DISTRIBUTION_IDX'] is not None else 'NULL'
            cur.execute(
                f"UPDATE \"C_TABLE_COLUMN\" "
                f"SET \"DATA_TYPE_NAME\" = '{d['DATA_TYPE_NAME']}', \"DATA_TYPE_ID\" = {d['DATA_TYPE_ID']}, "
                f"\"SORT_IDX\" = {d['SORT_IDX']}, \"DISTRIBUTION_YN\" = {dist_yn}, \"DISTRIBUTION_IDX\" = {dist_idx}, "
                f"\"UPDATE_DATE\" = now() "
                f"WHERE \"TABLE_ID\" = {table_id} "
                f"AND \"COLUMN_NAME\" = '{c['COLUMN_NAME']}' AND \"SORT_IDX\" = {c['SORT_IDX']}"
            )
            updated += 1

        elif status == "삭제 예정" and c:
            # C에만 존재 (D에서 제거됨) → C에서 삭제
            cur.execute(
                f"DELETE FROM \"C_TABLE_COLUMN\" "
                f"WHERE \"TABLE_ID\" = {table_id} "
                f"AND \"COLUMN_NAME\" = '{c['COLUMN_NAME']}' AND \"SORT_IDX\" = {c['SORT_IDX']}"
            )
            deleted += 1

    return inserted, updated, deleted


def apply_sync(table_id, cmp):
    """
    메타 + 컬럼을 단일 트랜잭션으로 동기화.
    _sync_meta 실행 후 _sync_columns 실행. 어느 쪽이든 실패 시 전체 rollback.
    """
    with get_connection(DB_C) as conn:
        with get_cursor(conn) as cur:
            meta_op = _sync_meta(cur, table_id, cmp)
            inserted, updated, deleted = _sync_columns(cur, table_id, cmp)
    print("\n동기화 완료")
    if meta_op:
        print(f"  메타: {meta_op}")
    print(f"  컬럼: 추가 {inserted} / 수정 {updated} / 삭제 {deleted}")


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
    메뉴 2 — D/C 비교 후 확인 입력을 받아 동기화.
    --sync <table_id> 인자로도 진입 가능.
    D에 table_id가 없으면 동기화 불가 (D가 소스).
    """
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
