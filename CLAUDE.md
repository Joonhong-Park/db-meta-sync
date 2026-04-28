# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 환경 개요

B서버에서 실행되는 코드베이스. 두 PostgreSQL DB에 접근한다.

- **C서버 DB**: B서버에서 직접 접근 불가 → A서버가 SSH 리버스 터널(`tunnel.sh`)로 `localhost:15432` 포워딩
- **D서버 DB**: B서버에서 직접 접속
- sudo 권한 없음, autossh 없음

## 디렉토리 구조

두 도구는 완전히 독립적으로 동작하며 각 디렉토리 내에서 단독 실행된다.

```
c_table_sync/    D ↔ C DB 메타·컬럼 동기화 CLI
impala_sync/     Impala → D DB 컬럼 동기화
```

---

## c_table_sync

### 실행

```bash
pip3 install --user psycopg2-binary   # 최초 1회
cd c_table_sync
python3 main.py                        # 대화형 메뉴 실행
python3 main.py --sync <table_id>      # 동기화 직접 실행
```

### SSH 터널 관리 (A서버에서 실행)

```bash
chmod +x tunnel.sh
./tunnel.sh start | stop | restart | status | log
```

재부팅 자동 시작: `crontab -e` → `@reboot sleep 30 && /home/my_username/tunnel.sh start`

### 파일 구성

```
config.py        접속 정보 (C_DB_*, D_DB_*) — 환경변수 오버라이드 가능
db_client.py     DB_C / DB_D 상수 + _C/_D_DB_CONFIG 딕셔너리 + execute_query
mappings.py      TableMapping 데이터클래스 + C_TIMESTAMP_COLS + TABLE_MAPPINGS
sync_manager.py  비교 데이터 생성 / 비교화면 출력 / 동기화 실행
main.py          대화형 메뉴 (1.조회 / 2.동기화 / 3.삭제)
tunnel.sh        A서버 실행용 SSH 리버스 터널 유지 스크립트
```

### db_client API

```python
execute_query(query, target=DB_C, fetch_result=False, commit=False)
# fetch_result=True  → list[dict] 반환 (SELECT)
# commit=True        → 영향받은 행 수 반환 (INSERT/UPDATE/DELETE)

get_connection(target)  # 트랜잭션이 필요한 경우 직접 사용
get_cursor(conn)
```

### 식별자 쌍따옴표 규칙

- **C DB**: 모든 테이블명·컬럼명에 쌍따옴표 필수 (`"c_table_meta"`, `"table_id"`)
- **D DB**: 쌍따옴표 불필요
- sync_manager.py 내 `_q(identifier)` 헬퍼로 처리

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

1. D, C 양쪽에서 지정 컬럼만 SELECT (SELECT * 미사용)
2. D 행을 C 컬럼명 공간으로 변환 (`_map_row`)
3. 컬럼 매칭: `(column_name, sort_idx)` 두 값 모두 일치해야 동일 컬럼
4. 비교화면 출력 → 확인 입력
5. 단일 트랜잭션으로 meta + column 완전 동기화 (INSERT/UPDATE/DELETE)

### 주요 제약사항

- 동기화는 항상 완전 일치 (D가 소스)
- column 삭제 순서: `c_table_column` 먼저, `c_table_meta` 나중 (FK 순서)
- `tunnel.sh`는 `pkill` 미사용 — `trap` + SSH PID 직접 추적으로 자식 프로세스 정리
- 새 테이블 추가 시 `mappings.py`의 `TABLE_MAPPINGS`에만 항목 추가

---

## impala_sync

### 실행

```bash
pip3 install --user impyla   # 최초 1회
cd impala_sync
python3 main.py <table_id>
```

### 파일 구성

```
config.py        D DB 접속 정보 — 환경변수 오버라이드 가능
db_client.py     _DB_CONFIG 딕셔너리 + execute_query (D서버 전용)
impala_config.py Impala 접속 정보 — 환경변수 오버라이드 가능
impala_client.py Impala 연결 + DESCRIBE FORMATTED 파싱
type_map.py      Impala 타입 텍스트 → D type_id 매핑
main.py          진입점
```

### db_client API

```python
execute_query(query, fetch_result=False, commit=False)
# D서버 전용 — target 인자 없음

get_connection()  # 트랜잭션이 필요한 경우 직접 사용
get_cursor(conn)
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

- 일반 테이블: 파티션 컬럼은 `# Partition Information` 섹션에서 파싱, 순서는 일반 컬럼 뒤
- Iceberg 테이블: 파티션 컬럼은 `# Partition Transform Information` 섹션에서 이름 수집 후 일반 컬럼 섹션의 data_type 사용, 원래 위치 유지

### 파티션 컬럼 규칙

| 구분 | distribution_yn | distribution_idx |
|------|----------------|-----------------|
| 일반 컬럼 | `NULL` | `NULL` |
| 파티션 (timestamp) | `'Y'` | `1` |
| 파티션 (string/varchar/char) | `'Y'` | `2` |

### 수정 필요 항목

- `impala_config.py`: `IMPALA_HOST`, `IMPALA_PORT`, 인증 방식
- `type_map.py`: `IMPALA_TYPE_MAP` — Impala 타입 → 실제 D type_id 값
- `config.py`: D DB 접속 정보
