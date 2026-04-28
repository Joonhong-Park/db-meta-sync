"""
D ↔ C 동기화 매니저
table_id 단건 기준으로 meta + column 완전 동기화
"""
from datetime import datetime
from typing import Any

from db_client import DBTarget, get_connection, get_cursor
from mappings import TABLE_MAPPINGS, C_TIMESTAMP_COLS, TYPE_ID_MAP, TypeMapping

_META = TABLE_MAPPINGS["table_meta"]
_COL  = TABLE_MAPPINGS["table_column"]

# 비교화면에서 변경 감지 대상 컬럼 (data_type_id 제외)
_COL_VISIBLE = ["column_name", "display_data_type", "distribution_yn", "distribution_idx"]

# 컬럼 UPDATE 시 SET 대상 (table_id·column_name·sort_idx는 식별자이므로 제외)
_COL_UPDATEABLE = ["display_data_type", "data_type_id", "distribution_yn", "distribution_idx"]


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _q(identifier: str) -> str:
    """C DB 식별자 쌍따옴표 처리"""
    return f'"{identifier}"'


def _map_row(row: dict[str, Any], col_map: dict[str, str]) -> dict[str, Any]:
    """D 행을 C 컬럼명 기준으로 변환"""
    return {col_map[k]: v for k, v in row.items() if k in col_map}


def _resolve_type_id(row: dict[str, Any], d_type_id_raw: int | None) -> dict[str, Any]:
    """D type_id → C (data_type_id, display_data_type) 변환 (매핑 누락 시 ValueError 발생)"""
    if d_type_id_raw is None:
        return row
    if d_type_id_raw not in TYPE_ID_MAP:
        raise ValueError(
            f"TYPE_ID_MAP에 없는 type_id: {d_type_id_raw} "
            f"(column: {row.get('column_name')}) — mappings.py를 확인하세요."
        )
    mapping: TypeMapping = TYPE_ID_MAP[d_type_id_raw]
    return {
        **row,
        "data_type_id":      mapping.data_type_id,
        "display_data_type": mapping.display_data_type,
    }


# ── 데이터 조회 ─────────────────────────────────────────────────────────

def _fetch_d_meta(table_id: int) -> dict[str, Any] | None:
    cols = ", ".join(_META.column_map.keys())
    with get_connection(DBTarget.D) as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                f"SELECT {cols} FROM {_META.source_table} WHERE table_id = %s",
                (table_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None


def _fetch_c_meta(table_id: int) -> dict[str, Any] | None:
    cols = ", ".join(_q(c) for c in _META.column_map.values())
    with get_connection(DBTarget.C) as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                f'SELECT {cols} FROM {_q(_META.target_table)} WHERE {_q("table_id")} = %s',
                (table_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None


def _fetch_d_columns(table_id: int) -> list[dict[str, Any]]:
    cols = ", ".join(_COL.column_map.keys())
    with get_connection(DBTarget.D) as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                f"SELECT {cols} FROM {_COL.source_table} "
                f"WHERE table_id = %s ORDER BY sort_idx",
                (table_id,),
            )
            return [dict(r) for r in cur.fetchall()]


def _fetch_c_columns(table_id: int) -> list[dict[str, Any]]:
    cols = ", ".join(_q(c) for c in _COL.column_map.values())
    with get_connection(DBTarget.C) as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                f'SELECT {cols} FROM {_q(_COL.target_table)} '
                f'WHERE {_q("table_id")} = %s ORDER BY {_q("sort_idx")}',
                (table_id,),
            )
            return [dict(r) for r in cur.fetchall()]


# ── 비교 데이터 생성 ────────────────────────────────────────────────────

def build_comparison(table_id: int) -> dict[str, Any]:
    """D, C 데이터를 조회하여 비교 결과 반환"""
    d_meta = _fetch_d_meta(table_id)
    c_meta = _fetch_c_meta(table_id)
    d_cols = _fetch_d_columns(table_id)
    c_cols = _fetch_c_columns(table_id)

    # ── 메타 비교 ──────────────────────────────────────────────────────
    meta_diffs: list[dict[str, Any]] = []
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

    # ── 컬럼 비교 ──────────────────────────────────────────────────────
    # D 행을 C 컬럼명 공간으로 변환 후 type_id → (data_type_id, display_data_type) 매핑 적용
    # 매핑 누락 시 ValueError 발생 → 동기화 중단
    mapped_d_cols: list[dict[str, Any]] = []
    for r in d_cols:
        mapped = _map_row(r, _COL.column_map)
        mapped["_d_data_type_raw"] = r.get("data_type")  # 비교화면 D측 표시용 원본 텍스트
        mapped_d_cols.append(_resolve_type_id(mapped, r.get("type_id")))

    d_col_map: dict[tuple[str, Any], dict] = {
        (r["column_name"], r["sort_idx"]): r for r in mapped_d_cols
    }
    c_col_map: dict[tuple[str, Any], dict] = {
        (r["column_name"], r["sort_idx"]): r for r in c_cols
    }

    all_keys = sorted(
        set(d_col_map) | set(c_col_map),
        key=lambda k: (k[1], k[0]),  # sort_idx 우선, 동일하면 column_name
    )

    column_diffs: list[dict[str, Any]] = []
    for col_name, sort_idx in all_keys:
        d_row = d_col_map.get((col_name, sort_idx))
        c_row = c_col_map.get((col_name, sort_idx))

        if d_row and c_row:
            differs = any(d_row.get(v) != c_row.get(v) for v in _COL_VISIBLE)
            status = "변경" if differs else "동일"
        elif d_row:
            status = "추가 예정"
        else:
            status = "삭제 예정"

        column_diffs.append({
            "sort_idx":            sort_idx,
            "d_column_name":       d_row.get("column_name")        if d_row else None,
            "d_data_type":         d_row.get("_d_data_type_raw")   if d_row else None,
            "c_column_name":       c_row.get("column_name")        if c_row else None,
            "c_display_data_type": c_row.get("display_data_type")  if c_row else None,
            "d_dist_yn":           d_row.get("distribution_yn")    if d_row else None,
            "d_dist_idx":          d_row.get("distribution_idx")   if d_row else None,
            "c_dist_yn":           c_row.get("distribution_yn")    if c_row else None,
            "c_dist_idx":          c_row.get("distribution_idx")   if c_row else None,
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

def _print_rows(headers: list[str], rows: list[list[str]]) -> None:
    """표 출력 유틸"""
    if not rows:
        print("  (없음)")
        return
    widths = [
        max(len(h), max(len(str(r[i])) for r in rows))
        for i, h in enumerate(headers)
    ]
    sep = "+-" + "-+-".join("-" * w for w in widths) + "-+"
    print(sep)
    print("| " + " | ".join(h.ljust(widths[i]) for i, h in enumerate(headers)) + " |")
    print(sep)
    for row in rows:
        print("| " + " | ".join(str(v).ljust(widths[i]) for i, v in enumerate(row)) + " |")
    print(sep)


def _str(val: Any) -> str:
    return str(val) if val is not None else "(없음)"


def print_comparison(cmp: dict[str, Any]) -> None:
    print(f"\n[table_id {cmp['table_id']} 비교]")

    # 메타 비교
    print("\n[메타 비교]")
    if not cmp["meta_diffs"]:
        print("  D에 해당 table_id가 없습니다.")
    else:
        _print_rows(
            ["항목", "D값", "C값", "상태"],
            [
                [d["field"], _str(d["d_val"]), _str(d["c_val"]), d["status"]]
                for d in cmp["meta_diffs"]
            ],
        )

    # 컬럼 비교
    print("\n[컬럼 비교]")
    if not cmp["column_diffs"]:
        print("  컬럼 정보가 없습니다.")
    else:
        _print_rows(
            ["sort_idx", "D_column_name", "D_data_type", "C_column_name", "C_display_data_type", "상태"],
            [
                [
                    _str(d["sort_idx"]),
                    _str(d["d_column_name"]),
                    _str(d["d_data_type"]),
                    _str(d["c_column_name"]),
                    _str(d["c_display_data_type"]),
                    d["status"],
                ]
                for d in cmp["column_diffs"]
            ],
        )

    # distribution 섹션 — 값이 있는 row만 출력, 없으면 섹션 생략
    dist_rows = [
        d for d in cmp["column_diffs"]
        if any([d["d_dist_yn"], d["d_dist_idx"], d["c_dist_yn"], d["c_dist_idx"]])
    ]
    if dist_rows:
        print("\n[distribution 정보]")
        _print_rows(
            ["sort_idx", "distribution_yn", "distribution_idx", "상태"],
            [
                [
                    _str(d["sort_idx"]),
                    _str(d["d_dist_yn"]  if d["d_dist_yn"]  is not None else d["c_dist_yn"]),
                    _str(d["d_dist_idx"] if d["d_dist_idx"] is not None else d["c_dist_idx"]),
                    d["status"],
                ]
                for d in dist_rows
            ],
        )


# ── 동기화 실행 ─────────────────────────────────────────────────────────

def _sync_meta(
    cur: Any,
    table_id: int,
    cmp: dict[str, Any],
    now: str,
) -> str | None:
    """메타 테이블 INSERT 또는 UPDATE, 실행한 작업명 반환"""
    if not cmp["d_meta"]:
        return None

    mapped  = _map_row(cmp["d_meta"], _META.column_map)
    non_pk  = [c for c in _META.column_map.values() if c != "table_id"]

    if cmp["c_meta"] is None:
        c_cols = list(_META.column_map.values()) + list(C_TIMESTAMP_COLS)
        cols_q = ", ".join(_q(c) for c in c_cols)
        placeholders = ", ".join(["%s"] * len(c_cols))
        vals = [mapped[c] for c in _META.column_map.values()] + [now, now]
        cur.execute(
            f'INSERT INTO {_q(_META.target_table)} ({cols_q}) VALUES ({placeholders})',
            vals,
        )
        return "INSERT"

    if any(d["status"] == "변경" for d in cmp["meta_diffs"]):
        set_clause = ", ".join(_q(c) + " = %s" for c in non_pk + ["update_date_ts"])
        vals = [mapped[c] for c in non_pk] + [now, table_id]
        cur.execute(
            f'UPDATE {_q(_META.target_table)} SET {set_clause} '
            f'WHERE {_q("table_id")} = %s',
            vals,
        )
        return "UPDATE"

    return None


def _sync_columns(
    cur: Any,
    table_id: int,
    cmp: dict[str, Any],
    now: str,
) -> tuple[int, int, int]:
    """컬럼 테이블 INSERT / UPDATE / DELETE, (inserted, updated, deleted) 반환"""
    inserted = updated = deleted = 0

    for diff in cmp["column_diffs"]:
        status = diff["status"]
        d_row  = diff["d_row"]
        c_row  = diff["c_row"]

        if status == "추가 예정" and d_row:
            c_cols = list(_COL.column_map.values()) + list(C_TIMESTAMP_COLS)
            cols_q = ", ".join(_q(c) for c in c_cols)
            placeholders = ", ".join(["%s"] * len(c_cols))
            vals = [d_row[c] for c in _COL.column_map.values()] + [now, now]
            cur.execute(
                f'INSERT INTO {_q(_COL.target_table)} ({cols_q}) VALUES ({placeholders})',
                vals,
            )
            inserted += 1

        elif status == "변경" and d_row and c_row:
            set_clause = ", ".join(_q(c) + " = %s" for c in _COL_UPDATEABLE + ["update_date_ts"])
            vals = (
                [d_row[c] for c in _COL_UPDATEABLE]
                + [now, table_id, c_row["column_name"], c_row["sort_idx"]]
            )
            cur.execute(
                f'UPDATE {_q(_COL.target_table)} SET {set_clause} '
                f'WHERE {_q("table_id")} = %s '
                f'AND {_q("column_name")} = %s '
                f'AND {_q("sort_idx")} = %s',
                vals,
            )
            updated += 1

        elif status == "삭제 예정" and c_row:
            cur.execute(
                f'DELETE FROM {_q(_COL.target_table)} '
                f'WHERE {_q("table_id")} = %s '
                f'AND {_q("column_name")} = %s '
                f'AND {_q("sort_idx")} = %s',
                (table_id, c_row["column_name"], c_row["sort_idx"]),
            )
            deleted += 1

    return inserted, updated, deleted


def apply_sync(table_id: int, cmp: dict[str, Any]) -> None:
    """동기화 실행 (단일 트랜잭션)"""
    now = _now()

    with get_connection(DBTarget.C) as conn:
        with get_cursor(conn) as cur:
            meta_op               = _sync_meta(cur, table_id, cmp, now)
            inserted, updated, deleted = _sync_columns(cur, table_id, cmp, now)

    print("\n동기화 완료")
    if meta_op:
        print(f"  메타: {meta_op}")
    print(f"  컬럼: 추가 {inserted} / 수정 {updated} / 삭제 {deleted}")
