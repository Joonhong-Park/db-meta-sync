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
./tunnel.sh start | stop | restart | status
```

재부팅 자동 시작: `crontab -e` → `@reboot sleep 30 && /home/my_username/tunnel.sh start`

### 파일 내부 구성 (섹션 순서)

```
설정           _C_DB_CONFIG / _D_DB_CONFIG 딕셔너리
DB 클라이언트   get_connection(target) / get_cursor / execute_query
타입 매핑      TypeMapping, TYPE_ID_MAP
데이터 조회    _fetch_d/c_meta, _fetch_d/c_columns, _d_col_to_c
비교 로직      build_comparison, print_comparison
동기화 실행    _sync_meta / _sync_columns / apply_sync (단일 트랜잭션)
메뉴 핸들러   handle_select / handle_sync / handle_delete
```

### execute_query API

```python
execute_query(query, target=DB_C, fetch_result=False, commit=False)
# fetch_result=True  → list[dict] 반환 (SELECT)
# commit=True        → 영향받은 행 수 반환 (INSERT/UPDATE/DELETE)

get_connection(target)  # 단일 트랜잭션이 필요한 경우 직접 사용 (apply_sync 참고)
get_cursor(conn)
```

### 식별자 쌍따옴표 규칙

- **C DB**: 모든 테이블명·컬럼명에 쌍따옴표 + 대문자 필수 → `"C_TABLE_META"`, `"TABLE_ID"`
- **D DB**: 쌍따옴표 불필요, 소문자
- SQL을 f-string으로 직접 작성. 헬퍼 함수(`_q`, `_val`) 없음

### 타입 매핑

D의 `data_type_name`(문자열) → C의 `DATA_TYPE_ID`(정수) + `DATA_TYPE_NAME` 변환.

```python
TYPE_ID_MAP: dict[str, TypeMapping] = {
    "varchar": TypeMapping(data_type_id=101, data_type_name="varchar"),
    ...
}
```

키는 D `d_table_column.data_type_name`의 실제 값과 일치해야 함.

### 컬럼 변환 (_d_col_to_c)

D 컬럼 행(소문자 키)을 C 컬럼명 공간(대문자 키)으로 변환. `data_type_name`으로 `TYPE_ID_MAP`을 조회해 `DATA_TYPE_ID`를 파생.

### 동기화 흐름 (table_id 단건)

1. D/C 양쪽에서 각각 메타·컬럼 SELECT
2. D 컬럼을 `_d_col_to_c`로 C 컬럼명 공간으로 변환
3. 비교:
   - 메타: `("db","DB_NAME")`, `("name","TABLE_NAME")` 쌍으로 직접 비교
   - 컬럼: `COLUMN_NAME` 기준 매칭, `_COL_VISIBLE` 항목 비교로 변경 여부 판단
4. 비교화면 출력 → 확인 입력
5. 단일 트랜잭션으로 `_sync_meta` → `_sync_columns` 실행

### 주요 제약사항

- 동기화 방향: D → C (D가 소스, C를 D에 맞춤)
- 컬럼 매칭 기준: `COLUMN_NAME`
- UPDATE/DELETE WHERE 조건: `TABLE_ID + COLUMN_NAME + SORT_IDX` (c_row 기준)
  - sort_idx가 변경되는 경우 C의 기존 sort_idx로 행을 찾아야 하므로 c_row 기준 사용
- 타임스탬프: Python 아닌 PostgreSQL `now()` 사용
- FK 삭제 순서: `C_TABLE_COLUMN` 먼저, `C_TABLE_META` 나중
- `tunnel.sh`는 `pkill` 미사용 — `trap` + SSH PID 직접 추적으로 자식 프로세스 정리

---

## impala_sync.py

### 실행

```bash
pip3 install --user impyla   # 최초 1회
python3 impala_sync.py <table_id>
python3 impala_sync.py <table_id> --dry-run
```

### 파일 내부 구성 (섹션 순서)

```
설정           _D_DB_CONFIG / _IMPALA_CONFIG 딕셔너리, IMPALA_TYPE_MAP
D DB 클라이언트  get_connection / get_cursor / execute_query (D서버 전용)
Impala 클라이언트 _impala_connection / describe_columns
타입 매핑      resolve_type_id
동기화 실행    sync_columns
```

### 동작 흐름

1. D DB에서 `table_id` → `db`, `name` 조회
2. Impala `DESCRIBE FORMATTED {db}.{name}` 실행 (Iceberg 전용 — `# Partition Transform Information` 섹션 필수)
3. 컬럼 파싱: 헤더·빈 행 스킵, 첫 번째 `#` 섹션에서 종료
4. `IMPALA_TYPE_MAP` 사전 검증 후 기존 컬럼과 비교해 증분 동기화
5. `d_table_partition`에서 파티션 컬럼 조회 → `distribution_yn/idx` 업데이트

### 수정 필요 항목

- `_D_DB_CONFIG`: D DB 접속 정보
- `_IMPALA_CONFIG`: Impala 접속 정보 (LDAP/SSL 포함)
- `IMPALA_TYPE_MAP`: Impala 타입 문자열 → 실제 D `data_type_name` 값으로 업데이트 필요
