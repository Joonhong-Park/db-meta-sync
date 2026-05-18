# db-meta-sync

D서버 DB의 테이블 메타·컬럼 정보를 C서버 DB와 동기화하고, Impala 스키마를 D서버 DB에 반영하는 CLI 도구 모음

## 환경 구성

```
로컬PC → (SMS 인증) → A서버 → (SSH 리버스 터널) → B서버 localhost:15432
                            ↓
                        C서버 DB:5432
```

- **B서버**에서 실행
- **C서버 DB**: SSH 리버스 터널 경유 (`localhost:15432`)
- **D서버 DB**: 직접 접속
- sudo 권한 불필요

---

## c_meta_sync.py — IMP ↔ SP DB 동기화

### 설치

```bash
pip3 install --user psycopg2-binary
```

### SSH 터널 설정 (A서버에서 실행)

```bash
chmod +x tunnel.sh
./tunnel.sh start | stop | restart | status
```

**재부팅 자동 시작** (`crontab -e`):
```
@reboot sleep 30 && /home/my_username/tunnel.sh start
```

### 접속 정보 설정

`c_meta_sync.py` 상단의 `_SP_DB_CONFIG` / `_IMP_DB_CONFIG` 딕셔너리를 직접 수정:

```python
_SP_DB_CONFIG  = {"host": "localhost", "port": 15432, "dbname": "...", "user": "...", "password": "..."}
_IMP_DB_CONFIG = {"host": "d_server_host", "port": 5432, "dbname": "...", "user": "...", "password": "..."}
```

### 실행

```bash
python3 c_meta_sync.py                    # 대화형 메뉴
python3 c_meta_sync.py --sync <table_id>  # 동기화 직접 실행
```

```
=== DB 동기화 도구 ===
1. 테이블 정보 조회
2. 테이블 동기화
3. 테이블 정보 삭제
0. 종료
선택:
```

### 기능

**1. 테이블 정보 조회** — `table_id` 입력 → SP DB 메타·컬럼 정보 출력

**2. 테이블 동기화** — `table_id` 입력 → IMP/SP 비교화면 출력 → 확인 후 동기화

| 비교 섹션 | 내용 |
|----------|------|
| 메타 비교 | TABLE_ID, DB_NAME, TABLE_NAME, DB_CODE, TABLE_TYPE, IS_WORKING IMP/SP 값 |
| 컬럼 비교 | 컬럼명, 타입, SORT_IDX, DISTRIBUTION IMP/SP 나란히 비교 |

- IMP → SP 단방향 동기화
- 컬럼 동기화: `C_TABLE_COLUMN` DELETE ALL 후 bulk INSERT
- `CREATE_DATE` / `UPDATE_DATE` 동기화 제외 (PostgreSQL `now()` 자동 기록)

**3. 테이블 정보 삭제** — `table_id` 입력 → 확인 후 SP DB에서 컬럼·메타 삭제

---

## impala_sync.py — Impala → D DB 컬럼 동기화

### 설치

```bash
pip3 install --user impyla
```

### 접속 정보 설정

`impala_sync.py` 상단의 `_D_DB_CONFIG` / `_IMPALA_CONFIG` 딕셔너리를 직접 수정.

### 실행

```bash
python3 impala_sync.py <table_id>
python3 impala_sync.py <table_id> --dry-run   # 쿼리만 출력, 실행 안 함
```

### 동작 흐름

1. D DB에서 `table_id` → `db`, `name` 조회
2. Impala `DESCRIBE FORMATTED {db}.{name}` 실행 **(Iceberg 테이블 전용)**
3. 컬럼 파싱: `# col_name` 헤더·빈 행 스킵, 첫 번째 `#` 섹션에서 종료
4. `IMPALA_TYPE_MAP` 사전 검증 (미매핑 타입 있으면 중단)
5. 기존 컬럼과 비교해 증분 동기화 (INSERT / UPDATE / DELETE)
6. `d_table_partition`에서 파티션 컬럼 조회 → `distribution_yn/idx` 업데이트

| 컬럼 구분 | distribution_yn | distribution_idx |
|----------|----------------|-----------------|
| 일반 컬럼 | NULL | NULL |
| 파티션 1번 | Y | 1 |
| 파티션 2번 | Y | 2 |

### 수정 필요 항목

- `_D_DB_CONFIG`: D DB 접속 정보
- `_IMPALA_CONFIG`: Impala 접속 정보 (LDAP/SSL 포함)
- `IMPALA_TYPE_MAP`: Impala 타입 문자열 → 실제 D `data_type_name` 값으로 업데이트 필요
