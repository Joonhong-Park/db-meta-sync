# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 환경 개요

B서버에서 실행되는 코드베이스. 두 PostgreSQL DB에 접근한다.

- **C서버 DB**: B서버에서 직접 접근 불가 → A서버가 SSH 리버스 터널(`tunnel.sh`)로 `localhost:15432` 포워딩
- **D서버 DB**: B서버에서 직접 접속
- sudo 권한 없음, autossh 없음

## 파일 구성

```
c_meta_sync.py   D ↔ C DB 메타·컬럼 동기화 CLI (단일 파일)
impala_sync.py   Impala → D DB 컬럼 동기화 (단일 파일)
tunnel.sh        A서버 실행용 SSH 리버스 터널 유지 스크립트
```

---

## c_meta_sync.py

### 실행

```bash
pip3 install --user psycopg2-binary   # 최초 1회
python3 c_meta_sync.py                # 대화형 메뉴
python3 c_meta_sync.py --sync <table_id>  # 동기화 직접 실행
```

### SSH 터널 관리 (A서버에서 실행)

```bash
chmod +x tunnel.sh
./tunnel.sh start | stop | restart | status | log
```

재부팅 자동 시작: `crontab -e` → `@reboot sleep 30 && /home/my_username/tunnel.sh start`

### 파일 내부 구성 (섹션 순서)

```
설정          _C_DB_CONFIG / _D_DB_CONFIG 딕셔너리
DB 클라이언트  get_connection(target) / get_cursor / execute_query
매핑 정의     TypeMapping, TableMapping, TABLE_MAPPINGS, TYPE_ID_MAP
동기화 유틸   _q, _val, _map_row, _resolve_type_id
데이터 조회   _fetch_d/c_meta, _fetch_d/c_columns
비교 로직     build_comparison, print_comparison
동기화 실행   apply_sync (_sync_meta + _sync_columns, 단일 트랜잭션)
메뉴 핸들러  handle_select / handle_sync / handle_delete
```

### execute_query API

```python
execute_query(query, target=DB_C, fetch_result=False, commit=False)
# fetch_result=True  → list[dict] 반환 (SELECT)
# commit=True        → 영향받은 행 수 반환 (INSERT/UPDATE/DELETE)

get_connection(target)  # 단일 트랜잭션이 필요한 경우 직접 사용
get_cursor(conn)
```

### 식별자 쌍따옴표 규칙

- **C DB**: 모든 테이블명·컬럼명에 쌍따옴표 필수 (`"c_table_meta"`, `"table_id"`)
- **D DB**: 쌍따옴표 불필요
- `_q(identifier)` 헬퍼로 처리

### 테이블 매핑 구조

모든 테이블의 PK 컬럼명은 `table_id`로 고정.

| 매핑키 | D 테이블 | C 테이블 |
|--------|---------|---------|
| `table_meta` | `d_table_meta` | `c_table_meta` |
| `table_column` | `d_table_column` | `c_table_column` |

**컬럼 매핑:**

| D 컬럼 | C 컬럼 | 비고 |
|--------|--------|------|
| db | db_name | |
| name | table_name | |
| data_type | display_data_type | |
| type_id | data_type_id | 비교화면 미출력 |
| distribution_yn | distribution_yn | |
| distribution_idx | distribution_idx | |
| _(기타 D 전용)_ | — | 동기화 제외 |
| — | create_date_ts | INSERT 시 현재시각 |
| — | update_date_ts | INSERT/UPDATE 시 현재시각 |
| — | _(기타 C 전용)_ | 동기화 제외 |

### 동기화 흐름 (table_id 단건)

1. D, C 양쪽에서 지정 컬럼만 SELECT
2. D 행을 C 컬럼명 공간으로 변환 (`_map_row`)
3. 컬럼 매칭: `(column_name, sort_idx)` 두 값 모두 일치해야 동일 컬럼
4. 비교화면 출력 → 확인 입력
5. 단일 트랜잭션으로 meta + column 완전 동기화 (INSERT/UPDATE/DELETE)

### 주요 제약사항

- 동기화는 항상 완전 일치 (D가 소스)
- column 삭제 순서: `c_table_column` 먼저, `c_table_meta` 나중 (FK 순서)
- `tunnel.sh`는 `pkill` 미사용 — `trap` + SSH PID 직접 추적으로 자식 프로세스 정리
- 새 테이블 추가 시 파일 내 `TABLE_MAPPINGS`에만 항목 추가

---

## impala_sync.py

### 실행

```bash
pip3 install --user impyla   # 최초 1회
python3 impala_sync.py <table_id>
```

### 파일 내부 구성 (섹션 순서)

```
설정          _D_DB_CONFIG / _IMPALA_CONFIG 딕셔너리, IMPALA_TYPE_MAP
D DB 클라이언트 get_connection / get_cursor / execute_query (D서버 전용)
Impala 클라이언트 _impala_connection / describe_columns
타입 매핑     resolve_type_id / _dist_idx
동기화 실행   sync_columns
```

### 동작 흐름

1. D DB에서 `table_id` → `db`, `name` 조회
2. Impala `DESCRIBE FORMATTED {db}.{name}` 실행
3. 일반 컬럼 / 파티션 컬럼 분리 파싱 (일반 테이블 + Iceberg 모두 지원)
4. type_id 매핑 사전 검증 (미매핑 타입 있으면 중단)
5. 단일 트랜잭션: `d_table_column` 기존 행 삭제 후 새 컬럼 삽입

### describe_columns 반환 형식

```python
[{"column_name": str, "data_type": str, "is_partition": bool}, ...]
```

- 일반 테이블: `# Partition Information` 섹션에서 파티션 컬럼 파싱, 순서는 일반 컬럼 뒤
- Iceberg: `# Partition Transform Information` 섹션에서 이름 수집 후 일반 컬럼 섹션의 data_type 사용, 원래 위치 유지

### 타입 및 파티션 규칙

사용 가능한 타입: `string`, `int`, `bigint`/`long`, `double`, `timestamp`, `date`

| 컬럼 구분 | distribution_yn | distribution_idx |
|----------|----------------|-----------------|
| 일반 컬럼 | NULL | NULL |
| 파티션 (timestamp) | Y | 1 |
| 파티션 (string) | Y | 2 |

### 수정 필요 항목

- `_D_DB_CONFIG`: D DB 접속 정보
- `_IMPALA_CONFIG`: Impala 접속 정보
- `IMPALA_TYPE_MAP`: Impala 타입 → 실제 D type_id 값
