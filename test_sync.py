"""
c_meta_sync 케이스별 동작 테스트
DB 연결 없이 실행되는 쿼리를 출력해 로직을 검증한다.

실행: python3 test_sync.py
"""
from unittest.mock import patch
from c_meta_sync import build_comparison, _sync_meta, _sync_columns


# ── 공통 픽스처 ────────────────────────────────────────────────────────

# D 메타: 소문자 컬럼명 (d_table_meta 실제 컬럼)
D_META_BASE = {"table_id": 1, "db": "mydb", "name": "my_table"}

# C 메타: 대문자 컬럼명 (C_TABLE_META 실제 컬럼)
C_META_BASE = {"TABLE_ID": 1, "DB_NAME": "mydb", "TABLE_NAME": "my_table"}

# D 컬럼: data_type_name(문자열) 사용 — data_type_id 없음
D_COL_BASE = {
    "table_id": 1, "column_name": "col1", "data_type_name": "varchar",
    "sort_idx": 0, "distribution_yn": None, "distribution_idx": None,
}

# C 컬럼: DATA_TYPE_ID + DATA_TYPE_NAME 둘 다 보유
C_COL_BASE = {
    "TABLE_ID": 1, "COLUMN_NAME": "col1", "DATA_TYPE_ID": 101,
    "DATA_TYPE_NAME": "varchar",
    "SORT_IDX": 0, "DISTRIBUTION_YN": None, "DISTRIBUTION_IDX": None,
}


class MockCursor:
    """실행된 쿼리를 수집하는 가짜 커서"""
    def __init__(self):
        self.queries = []

    def execute(self, query):
        self.queries.append(query.strip())

    def print_queries(self):
        if not self.queries:
            print("  (실행된 쿼리 없음)")
        for q in self.queries:
            print(f"  → {q}")


def run_case(label, d_meta, c_meta, d_cols, c_cols):
    print(f"\n{'='*60}")
    print(f" {label}")
    print('='*60)

    with patch("c_meta_sync._fetch_d_meta", return_value=d_meta), \
         patch("c_meta_sync._fetch_c_meta", return_value=c_meta), \
         patch("c_meta_sync._fetch_d_columns", return_value=d_cols), \
         patch("c_meta_sync._fetch_c_columns", return_value=c_cols):
        cmp = build_comparison(1)

    cur = MockCursor()
    now = "2026-05-06 00:00:00"

    meta_op = _sync_meta(cur, 1, cmp, now)
    inserted, updated, deleted = _sync_columns(cur, 1, cmp, now)

    print(f"  메타: {meta_op}")
    print(f"  컬럼: 추가 {inserted} / 수정 {updated} / 삭제 {deleted}")
    print("  실행 쿼리:")
    cur.print_queries()


# ── 메타 케이스 ────────────────────────────────────────────────────────

run_case(
    "[META-1] D있음, C없음 → INSERT",
    d_meta=D_META_BASE, c_meta=None,
    d_cols=[], c_cols=[],
)

run_case(
    "[META-2] D있음, C있음, 변경 → UPDATE",
    d_meta={**D_META_BASE, "name": "new_table"},
    c_meta=C_META_BASE,
    d_cols=[], c_cols=[],
)

run_case(
    "[META-3] D있음, C있음, 동일 → skip",
    d_meta=D_META_BASE, c_meta=C_META_BASE,
    d_cols=[], c_cols=[],
)

run_case(
    "[META-4] D없음, C있음 → DELETE",
    d_meta=None, c_meta=C_META_BASE,
    d_cols=[], c_cols=[],
)

run_case(
    "[META-5] 모두 없음 → skip",
    d_meta=None, c_meta=None,
    d_cols=[], c_cols=[],
)


# ── 컬럼 케이스 ────────────────────────────────────────────────────────

run_case(
    "[COL-1] D에만 있는 컬럼 → INSERT",
    d_meta=D_META_BASE, c_meta=C_META_BASE,
    d_cols=[D_COL_BASE], c_cols=[],
)

run_case(
    "[COL-2] 변경된 컬럼 (data_type_name 변경) → UPDATE",
    d_meta=D_META_BASE, c_meta=C_META_BASE,
    d_cols=[{**D_COL_BASE, "data_type_name": "integer"}],
    c_cols=[C_COL_BASE],
)

run_case(
    "[COL-3] sort_idx 변경 → UPDATE (WHERE는 C의 기존 sort_idx=0 사용)",
    d_meta=D_META_BASE, c_meta=C_META_BASE,
    d_cols=[{**D_COL_BASE, "sort_idx": 1}],
    c_cols=[C_COL_BASE],
)

run_case(
    "[COL-4] C에만 있는 컬럼 → DELETE",
    d_meta=D_META_BASE, c_meta=C_META_BASE,
    d_cols=[], c_cols=[C_COL_BASE],
)

run_case(
    "[COL-5] 동일한 컬럼 → skip",
    d_meta=D_META_BASE, c_meta=C_META_BASE,
    d_cols=[D_COL_BASE], c_cols=[C_COL_BASE],
)

run_case(
    "[COL-6] 복합: 추가 + 변경 + 삭제 동시",
    d_meta=D_META_BASE, c_meta=C_META_BASE,
    d_cols=[
        D_COL_BASE,                                                              # col1: 동일
        {**D_COL_BASE, "column_name": "col2", "sort_idx": 1},                   # col2: 추가
        {**D_COL_BASE, "column_name": "col3", "sort_idx": 2, "data_type_name": "integer"},  # col3: 타입 변경
    ],
    c_cols=[
        C_COL_BASE,                                                              # col1: 동일
        {**C_COL_BASE, "COLUMN_NAME": "col3", "SORT_IDX": 2},                   # col3: 변경 대상
        {**C_COL_BASE, "COLUMN_NAME": "col4", "SORT_IDX": 3},                   # col4: 삭제 대상
    ],
)
