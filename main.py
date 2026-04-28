"""
DB 동기화 도구 — 대화형 CLI
실행: python3 main.py
"""
from typing import Any

import db_client
from db_client import DBTarget
from sync_manager import build_comparison, print_comparison, apply_sync


# ── 터미널 표 출력 (순수 Python) ───────────────────────────────────────

def print_table(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print("  (결과 없음)")
        return
    columns = list(rows[0].keys())
    widths = {
        col: max(len(col), max(len(str(r[col])) for r in rows))
        for col in columns
    }
    sep    = "+-" + "-+-".join("-" * widths[c] for c in columns) + "-+"
    header = "| " + " | ".join(c.ljust(widths[c]) for c in columns) + " |"
    print(sep)
    print(header)
    print(sep)
    for row in rows:
        print("| " + " | ".join(str(row[c]).ljust(widths[c]) for c in columns) + " |")
    print(sep)
    print(f"  {len(rows)}행")


# ── 공통 유틸 ─────────────────────────────────────────────────────────

def _input_table_id() -> int | None:
    raw = input("table_id 입력: ").strip()
    try:
        return int(raw)
    except ValueError:
        print("  [오류] 정수를 입력하세요.")
        return None


# ── 1. 테이블 정보 조회 ────────────────────────────────────────────────

def handle_select() -> None:
    table_id = _input_table_id()
    if table_id is None:
        return

    meta = db_client.fetch_one(
        'SELECT * FROM "c_table_meta" WHERE "table_id" = %s',
        params=(table_id,),
        target=DBTarget.C,
    )
    if not meta:
        print(f"  table_id {table_id} 가 존재하지 않습니다.")
        return

    print("\n[메타 정보]")
    print_table([meta])

    columns = db_client.fetch_all(
        'SELECT * FROM "c_table_column" WHERE "table_id" = %s ORDER BY "sort_idx"',
        params=(table_id,),
        target=DBTarget.C,
    )
    print("\n[컬럼 정보]")
    print_table(columns)


# ── 2. 테이블 동기화 ───────────────────────────────────────────────────

def handle_sync() -> None:
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


# ── 3. 테이블 정보 삭제 ────────────────────────────────────────────────

def handle_delete() -> None:
    table_id = _input_table_id()
    if table_id is None:
        return

    meta = db_client.fetch_one(
        'SELECT "table_id", "db_name", "table_name" FROM "c_table_meta" WHERE "table_id" = %s',
        params=(table_id,),
        target=DBTarget.C,
    )
    if not meta:
        print(f"  table_id {table_id} 가 존재하지 않습니다.")
        return

    db_table = f"{meta['db_name']}.{meta['table_name']}"
    answer = input(
        f"\n  table_id: {table_id} / {db_table}\n"
        f"  정말 삭제하시겠습니까? (yes/no): "
    ).strip().lower()

    if answer != "yes":
        print("  취소되었습니다.")
        return

    # FK 순서: column 먼저 삭제 후 meta 삭제
    db_client.delete(
        'DELETE FROM "c_table_column" WHERE "table_id" = %s',
        params=(table_id,),
        target=DBTarget.C,
    )
    db_client.delete(
        'DELETE FROM "c_table_meta" WHERE "table_id" = %s',
        params=(table_id,),
        target=DBTarget.C,
    )
    print(f"  삭제 완료: {db_table} (table_id: {table_id})")


# ── 메뉴 ──────────────────────────────────────────────────────────────

def main() -> None:
    while True:
        print("\n=== DB 동기화 도구 ===")
        print("1. 테이블 정보 조회")
        print("2. 테이블 동기화")
        print("3. 테이블 정보 삭제")
        print("0. 종료")
        choice = input("선택: ").strip()

        try:
            if choice == "1":
                handle_select()
            elif choice == "2":
                handle_sync()
            elif choice == "3":
                handle_delete()
            elif choice == "0":
                print("종료합니다.")
                break
            else:
                print("  1, 2, 3, 0 중에서 선택하세요.")
        except ConnectionError as e:
            print(f"\n  [연결 오류] {e}")
        except Exception as e:
            print(f"\n  [오류] {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
