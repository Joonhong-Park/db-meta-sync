"""
D ↔ C 테이블 매핑 정의
새 테이블 동기화 시 TABLE_MAPPINGS에 항목 추가
"""
from dataclasses import dataclass


@dataclass
class TableMapping:
    source_table: str           # D DB 테이블명
    target_table: str           # C DB 테이블명
    column_map: dict[str, str]  # {D 컬럼명: C 컬럼명}

    def __post_init__(self) -> None:
        if "table_id" not in self.column_map:
            raise ValueError(f"column_map에 'table_id' 항목이 없습니다: {self.source_table}")


# C DB에서 애플리케이션이 직접 관리하는 timestamp 컬럼 (D→C 동기화 제외)
# INSERT 시 둘 다 현재시각, UPDATE 시 update_date_ts만 갱신
C_TIMESTAMP_COLS: tuple[str, str] = ("create_date_ts", "update_date_ts")

TABLE_MAPPINGS: dict[str, TableMapping] = {
    # d_table_meta : table_id, db, name, ...
    # c_table_meta : table_id, db_name, table_name, create_date_ts, update_date_ts, ...
    "table_meta": TableMapping(
        source_table="d_table_meta",
        target_table="c_table_meta",
        column_map={
            "table_id": "table_id",
            "db":       "db_name",
            "name":     "table_name",
        },
    ),

    # d_table_column: table_id, column_name, data_type, type_id, sort_idx, distribution_yn, distribution_idx, ...
    # c_table_column: table_id, column_name, display_data_type, data_type_id, sort_idx, distribution_yn, distribution_idx, create_date_ts, update_date_ts, ...
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
