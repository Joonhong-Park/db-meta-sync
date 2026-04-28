"""
Impala 타입 텍스트 → D DB type_id 매핑
실제 D DB type_id 값으로 업데이트 필요
"""

# Impala base type (소문자) → D type_id
IMPALA_TYPE_MAP = {
    "string":    1,
    "varchar":   1,
    "char":      1,
    "int":       2,
    "integer":   2,
    "smallint":  2,
    "tinyint":   2,
    "bigint":    3,
    "boolean":   4,
    "timestamp": 5,
    "float":     6,
    "double":    6,
    "decimal":   6,
}


def resolve_type_id(impala_type):
    """Impala 타입 문자열 → D type_id 반환, 매핑 없으면 None"""
    base = impala_type.lower().split("(")[0].strip()
    return IMPALA_TYPE_MAP.get(base)
