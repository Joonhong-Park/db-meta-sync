#!/bin/bash
# A서버에서 실행: B서버에 SSH 리버스 터널을 열고 유지하는 스크립트
# 용도: B서버 localhost:15432 → C서버:5432 포워딩
# 주의: sudo 권한 불필요 — 외부 명령(pkill 등) 미사용, 홈 디렉토리만 사용

# ── 설정 ──────────────────────────────────────────────────────────────
BASE_DIR="/home/my_username"
PID_FILE="${BASE_DIR}/.tunnel/tunnel.pid"
LOG_FILE="${BASE_DIR}/.tunnel/tunnel.log"
LOG_MAX_BYTES=5242880  # 로그 최대 크기: 5MB

B_SERVER_USER="b_username"
B_SERVER_HOST="b_server_host"
B_SERVER_SSH_PORT=22

C_SERVER_HOST="c_server_host"
C_SERVER_DB_PORT=5432

TUNNEL_BIND_PORT=15432   # B서버에서 열릴 로컬 포트

RECONNECT_DELAY=5        # 초기 재연결 대기(초)
RECONNECT_DELAY_MAX=60   # 최대 재연결 대기(초)
# ──────────────────────────────────────────────────────────────────────

# sudo 없이 설치 가능한 경로에서 ssh 바이너리 탐색
SSH_BIN=$(command -v ssh)
if [ -z "${SSH_BIN}" ]; then
    echo "[오류] ssh 명령어를 찾을 수 없습니다."
    exit 1
fi

# 디렉토리 초기화
mkdir -p "${BASE_DIR}/.tunnel"

# 로그 기록 함수 (크기 초과 시 로테이션)
log() {
    local timestamp
    timestamp=$(date '+%Y-%m-%d %H:%M:%S')

    # 로그 파일이 최대 크기를 초과하면 로테이션
    if [ -f "${LOG_FILE}" ]; then
        local size
        size=$(wc -c < "${LOG_FILE}" 2>/dev/null || echo 0)
        if [ "${size}" -gt "${LOG_MAX_BYTES}" ]; then
            mv "${LOG_FILE}" "${LOG_FILE}.old"
        fi
    fi

    echo "[${timestamp}] $1" >> "${LOG_FILE}"
}

# 현재 PID가 실제로 살아있는 프로세스인지 확인
is_running() {
    if [ ! -f "${PID_FILE}" ]; then
        return 1
    fi
    local pid
    pid=$(cat "${PID_FILE}")
    # kill -0: 프로세스 존재 여부만 확인 (시그널 전송 없음)
    if kill -0 "${pid}" 2>/dev/null; then
        return 0
    else
        # stale PID 파일 제거
        rm -f "${PID_FILE}"
        return 1
    fi
}

# 터널 시작
start() {
    if is_running; then
        echo "터널이 이미 실행 중입니다. (PID: $(cat ${PID_FILE}))"
        exit 0
    fi

    log "터널 시작 요청"
    echo "터널 시작 중..."

    # 백그라운드에서 터널 유지 루프 실행
    run_tunnel_loop &
    local loop_pid=$!
    echo "${loop_pid}" > "${PID_FILE}"

    log "터널 루프 시작 (PID: ${loop_pid})"
    echo "터널 루프 시작됨 (PID: ${loop_pid})"
    echo "로그: ${LOG_FILE}"
}

# SSH 터널 유지 루프 (자동 재연결 + 지수 백오프)
# pkill 미사용: trap으로 SIGTERM 수신 시 자식 SSH 프로세스를 직접 종료
run_tunnel_loop() {
    local delay=${RECONNECT_DELAY}
    local ssh_pid=""

    # SIGTERM(stop 명령) 수신 시 실행 중인 SSH 자식 프로세스를 정리하고 종료
    trap '
        if [ -n "${ssh_pid}" ]; then
            kill "${ssh_pid}" 2>/dev/null
            wait "${ssh_pid}" 2>/dev/null
        fi
        exit 0
    ' TERM INT

    while true; do
        log "SSH 터널 연결 시도: ${B_SERVER_USER}@${B_SERVER_HOST}"

        # 백그라운드로 실행 후 PID 저장 → SIGTERM 시 직접 종료 가능
        "${SSH_BIN}" \
            -N \
            -R "${TUNNEL_BIND_PORT}:${C_SERVER_HOST}:${C_SERVER_DB_PORT}" \
            -p "${B_SERVER_SSH_PORT}" \
            -o "ServerAliveInterval=30" \
            -o "ServerAliveCountMax=3" \
            -o "ExitOnForwardFailure=yes" \
            -o "StrictHostKeyChecking=no" \
            -o "BatchMode=yes" \
            "${B_SERVER_USER}@${B_SERVER_HOST}" &
        ssh_pid=$!
        wait "${ssh_pid}"
        local exit_code=$?
        ssh_pid=""

        log "SSH 종료 (exit code: ${exit_code}). ${delay}초 후 재연결..."
        sleep "${delay}"

        # 지수 백오프: 최대 RECONNECT_DELAY_MAX 초까지 증가
        delay=$(( delay * 2 ))
        if [ "${delay}" -gt "${RECONNECT_DELAY_MAX}" ]; then
            delay=${RECONNECT_DELAY_MAX}
        fi
    done
}

# 터널 중지
stop() {
    if ! is_running; then
        echo "실행 중인 터널이 없습니다."
        exit 0
    fi

    local pid
    pid=$(cat "${PID_FILE}")
    log "터널 중지 요청 (PID: ${pid})"

    # SIGTERM 전송 → run_tunnel_loop 내 trap이 SSH 자식 프로세스까지 정리
    kill "${pid}" 2>/dev/null
    # 루프가 종료될 때까지 최대 5초 대기
    local waited=0
    while kill -0 "${pid}" 2>/dev/null && [ "${waited}" -lt 5 ]; do
        sleep 1
        waited=$(( waited + 1 ))
    done

    rm -f "${PID_FILE}"
    echo "터널이 중지되었습니다. (PID: ${pid})"
    log "터널 중지 완료"
}

# 상태 확인
status() {
    if is_running; then
        local pid
        pid=$(cat "${PID_FILE}")
        echo "터널 실행 중 (PID: ${pid})"

        # B서버에서 포트가 실제로 열려있는지 확인 (ssh 세션이 아닌 로컬 확인)
        echo ""
        echo "최근 로그 (마지막 10줄):"
        tail -n 10 "${LOG_FILE}" 2>/dev/null || echo "로그 없음"
    else
        echo "터널이 실행되지 않고 있습니다."
    fi
}

# 로그 실시간 확인
follow_log() {
    tail -f "${LOG_FILE}" 2>/dev/null || echo "로그 파일이 없습니다: ${LOG_FILE}"
}

# ── 진입점 ────────────────────────────────────────────────────────────
case "$1" in
    start)   start ;;
    stop)    stop ;;
    restart) stop; sleep 2; start ;;
    status)  status ;;
    log)     follow_log ;;
    *)
        echo "사용법: $0 {start|stop|restart|status|log}"
        echo ""
        echo "  start    터널 시작"
        echo "  stop     터널 중지"
        echo "  restart  터널 재시작"
        echo "  status   상태 및 최근 로그 확인"
        echo "  log      실시간 로그 출력 (Ctrl+C로 종료)"
        exit 1
        ;;
esac
