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

## c_table_sync

D ↔ C DB 메타·컬럼 동기화 대화형 CLI

### 설치

```bash
pip3 install --user psycopg2-binary
```

### SSH 터널 설정 (A서버에서 실행)

```bash
chmod +x c_table_sync/tunnel.sh
cd c_table_sync

./tunnel.sh start    # 시작
./tunnel.sh stop     # 중지
./tunnel.sh restart  # 재시작
./tunnel.sh status   # 상태 확인
./tunnel.sh log      # 실시간 로그
```

**재부팅 자동 시작** (`crontab -e`):
```
@reboot sleep 30 && /home/my_username/tunnel.sh start
```

### 접속 정보 설정

`c_table_sync/config.py` 또는 환경변수로 설정:

```bash
# C서버 DB (SSH 터널)
export C_DB_HOST=localhost
export C_DB_PORT=15432
export C_DB_NAME=your_c_database
export C_DB_USER=your_c_username
export C_DB_PASSWORD=your_c_password

# D서버 DB
export D_DB_HOST=d_server_host
export D_DB_PORT=5432
export D_DB_NAME=your_d_database
export D_DB_USER=your_d_username
export D_DB_PASSWORD=your_d_password
```

### 실행

```bash
cd c_table_sync
python3 main.py                   # 대화형 메뉴
python3 main.py --sync <table_id> # 동기화 직접 실행
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

**1. 테이블 정보 조회** — `table_id` 입력 → C DB 메타·컬럼 정보 출력

**2. 테이블 동기화** — `table_id` 입력 → D/C 비교화면 출력 → 확인 후 동기화

| 비교 섹션 | 내용 |
|----------|------|
| 메타 비교 | db_name, table_name D/C 값 및 변경 여부 |
| 컬럼 비교 | sort_idx, 컬럼명, 타입 D/C 비교 및 상태 |
| distribution 정보 | 값이 있는 row만 출력 |

- D → C 단방향 동기화
- 컬럼 매칭: `column_name` + `sort_idx` 두 값 모두 일치해야 동일 컬럼
- `create_date_ts` / `update_date_ts` 동기화 제외 (현재 시각 자동 기록)

**3. 테이블 정보 삭제** — `table_id` 입력 → 확인 후 C DB에서 컬럼·메타 삭제

---

## impala_sync

Impala 테이블 스키마를 D DB `d_table_column`에 반영하는 CLI

### 설치

```bash
pip3 install --user impyla
```

### 접속 정보 설정

`impala_sync/config.py` (D DB) 및 `impala_sync/impala_config.py` (Impala) 수정:

```bash
# D서버 DB
export D_DB_HOST=d_server_host
export D_DB_PORT=5432
export D_DB_NAME=your_d_database
export D_DB_USER=your_d_username
export D_DB_PASSWORD=your_d_password

# Impala
export IMPALA_HOST=impala_host
export IMPALA_PORT=21050
export IMPALA_AUTH=PLAIN   # PLAIN / GSSAPI / LDAP
```

### 실행

```bash
cd impala_sync
python3 main.py <table_id>
```

### 동작 흐름

1. D DB에서 `table_id` → `db.name` 조회
2. Impala `DESCRIBE FORMATTED db.name` 실행
3. 일반 컬럼 / 파티션 컬럼 분리 (일반 테이블 + Iceberg 모두 지원)
4. 단일 트랜잭션으로 기존 컬럼 삭제 후 새 컬럼 삽입

| 컬럼 구분 | distribution_yn | distribution_idx |
|----------|----------------|-----------------|
| 일반 컬럼 | NULL | NULL |
| 파티션 (timestamp) | Y | 1 |
| 파티션 (string계열) | Y | 2 |

### type_map 설정

`impala_sync/type_map.py`의 `IMPALA_TYPE_MAP`에 Impala 타입 → D `type_id` 매핑 값 설정 필요
