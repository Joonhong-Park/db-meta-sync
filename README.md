# db-meta-sync

D서버 DB의 테이블 메타·컬럼 정보를 C서버 DB와 동기화하는 대화형 CLI 도구

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

## 설치

```bash
pip3 install --user psycopg2-binary
```

## SSH 터널 설정 (A서버에서 실행)

```bash
chmod +x tunnel.sh

# 시작 / 중지 / 재시작 / 상태 / 실시간 로그
./tunnel.sh start
./tunnel.sh stop
./tunnel.sh restart
./tunnel.sh status
./tunnel.sh log
```

**재부팅 자동 시작** (`crontab -e`):
```
@reboot sleep 30 && /home/my_username/tunnel.sh start
```

## 접속 정보 설정

`config.py` 또는 환경변수로 설정:

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

## 실행

```bash
python3 main.py
```

```
=== DB 동기화 도구 ===
1. 테이블 정보 조회
2. 테이블 동기화
3. 테이블 정보 삭제
0. 종료
선택:
```

### 1. 테이블 정보 조회

`table_id` 입력 → C DB에서 메타 정보 + 컬럼 정보 출력

### 2. 테이블 동기화

`table_id` 입력 → D, C 비교화면 출력 → 확인 후 동기화 실행

**비교화면 구성:**

| 섹션 | 내용 |
|------|------|
| 메타 비교 | db_name, table_name D/C 값 및 변경 여부 |
| 컬럼 비교 | sort_idx, 컬럼명, 타입 D/C 비교 및 상태 |
| distribution 정보 | 값이 있는 row만 출력 |

**동기화 규칙:**
- D → C 단방향
- 컬럼 매칭: `column_name` + `sort_idx` 두 값 모두 일치해야 동일 컬럼
- C에만 있는 컬럼 삭제, D에만 있는 컬럼 추가, 내용이 다른 컬럼 수정
- `create_date_ts` / `update_date_ts` 는 동기화 제외 (INSERT/UPDATE 시 현재 시각 자동 기록)

### 3. 테이블 정보 삭제

`table_id` 입력 → 삭제 대상 확인 → C DB에서 컬럼·메타 정보 삭제

## 파일 구조

```
├── tunnel.sh        SSH 리버스 터널 유지 스크립트 (A서버에서 실행)
├── config.py        DB 접속 정보
├── db_client.py     DB 커넥션 및 CRUD 함수
├── mappings.py      테이블·컬럼 매핑 정의
├── sync_manager.py  동기화 핵심 로직
└── main.py          대화형 CLI 진입점
```

## 테이블 매핑

| D 테이블 | C 테이블 |
|----------|----------|
| `d_table_meta` | `c_table_meta` |
| `d_table_column` | `c_table_column` |

새 테이블 동기화가 필요할 경우 `mappings.py`의 `TABLE_MAPPINGS`에 항목만 추가
