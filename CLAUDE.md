# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 환경 개요

B서버에서 실행되는 코드베이스. 두 PostgreSQL DB에 접근한다.

- **SP DB (C서버)**: B서버에서 직접 접근 불가 → A서버가 SSH 리버스 터널(`tunnel.sh`)로 `localhost:15432` 포워딩
- **IMP DB (D서버)**: B서버에서 직접 접속
- sudo 권한 없음, autossh 없음

## 파일 구성

```
c_meta_sync.py   IMP ↔ SP DB 메타·컬럼 동기화 CLI (단일 파일)
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
설정           _SP_DB_CONFIG / _IMP_DB_CONFIG 딕셔너리
DB 클라이언트   get_connection(target) / get_cursor
타입 매핑      TYPE_ID_MAP (dict[str, int])
데이터 조회·변환  _fetch_imp_data, _fetch_sp_data, _imp_col_to_sp
동기화 실행    _sync_meta / _sync_columns / apply_sync (단일 트랜잭션)
터미널 표 출력  print_table
비교 출력      _print_meta_comparison, _print_column_comparison
메뉴 핸들러   handle_select / handle_sync / handle_delete
```

### DB 클라이언트

```python
get_connection(target)  # 단일 트랜잭션 — with 블록 정상 종료 시 commit, 예외 시 rollback
get_cursor(conn)        # RealDictCursor
```

### 식별자 쌍따옴표 규칙

- **SP DB**: 모든 테이블명·컬럼명에 쌍따옴표 + 대문자 필수 → `"C_TABLE_META"`, `"TABLE_ID"`
- **IMP DB**: 쌍따옴표 불필요, 소문자
- SQL은 문자열로 직접 작성. 값은 `%s` 파라미터로 전달. 쿼리 빌더 헬퍼 없음

### 타입 매핑

IMP의 `data_type_name`(문자열) → SP의 `DATA_TYPE_ID`(정수) 변환.

```python
TYPE_ID_MAP: dict[str, int] = {
    "varchar": 101,
    ...
}
```

키는 IMP `d_table_column.data_type_name`의 실제 값과 일치해야 함.

### 컬럼 변환 (_imp_col_to_sp)

IMP 컬럼 행(소문자 키)을 SP 컬럼명 공간(대문자 키)으로 변환. `data_type_name`으로 `TYPE_ID_MAP`을 조회해 `DATA_TYPE_ID`를 파생. `DATA_TYPE_NAME`은 IMP `data_type_name`을 그대로 전달.

### 동기화 흐름 (table_id 단건)

1. IMP/SP 양쪽에서 각각 메타·컬럼 SELECT
2. IMP 컬럼을 `_imp_col_to_sp`로 SP 컬럼명 공간으로 변환
3. `_print_meta_comparison` / `_print_column_comparison`으로 비교화면 출력 → 확인 입력
4. 단일 트랜잭션으로 `_sync_meta` → `_sync_columns` 실행

### 주요 제약사항

- 동기화 방향: IMP → SP (IMP가 소스, SP를 IMP에 맞춤)
- 컬럼 동기화 방식: `C_TABLE_COLUMN` DELETE ALL 후 `execute_values` bulk INSERT (개별 UPDATE/DELETE 없음)
- `_sync_meta` 시그니처: `(cur, imp_meta, sp_meta)` — `table_id`는 `imp_meta['table_id']`로 파생
- `DB_CODE`: `db_code_list` 테이블에서 IMP `db` 값으로 조회해 INSERT/UPDATE에 포함
- `_print_column_comparison`: 컬럼명(COLUMN_NAME) 기준 매칭 — IMP/SP 어느 쪽에만 있는 컬럼은 반대쪽을 `-`로 표시
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
설정              _D_DB_CONFIG / _IMPALA_CONFIG 딕셔너리, IMPALA_TYPE_MAP
D DB 클라이언트   get_connection / get_cursor / execute_query(query, params=None, ...) (D서버 전용)
Impala 클라이언트  _impala_connection / describe_columns
타입 매핑         resolve_type_id
동기화 실행       sync_columns
```

### 동작 흐름

1. D DB에서 `table_id` → `db`, `name` 조회
2. Impala `DESCRIBE FORMATTED {db}.{name}` 실행 (Iceberg 전용 — `# Partition Transform Information` 섹션 필수)
3. 컬럼 파싱: 헤더·빈 행 스킵, 첫 번째 `#` 섹션에서 종료
4. `new_map` 빌드(resolve_type_id 1회) → 미매핑 타입 검증 → 기존 컬럼과 비교해 증분 동기화
5. `d_table_partition`에서 파티션 컬럼 조회 → `distribution_yn/idx` 업데이트

### DML 실행 방식

`sync_columns` 내부에서 `queries: list[tuple[str, tuple]]`로 `(sql, params)` 쌍을 수집한 뒤 단일 트랜잭션으로 일괄 실행. dry-run 시 `cur.mogrify(sql, params)` 로 렌더링해 출력.

### 수정 필요 항목

- `_D_DB_CONFIG`: D DB 접속 정보
- `_IMPALA_CONFIG`: Impala 접속 정보 (LDAP/SSL 포함)
- `IMPALA_TYPE_MAP`: Impala 타입 문자열 → 실제 D `data_type_id` 정수 값으로 업데이트 필요
