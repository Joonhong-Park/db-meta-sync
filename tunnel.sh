#!/bin/bash
# A서버에서 실행: B서버에 SSH 리버스 터널을 열고 유지하는 스크립트
# 용도: B서버 localhost:15432 → C서버:5432 포워딩
#
# 사용법:
#   chmod +x tunnel.sh
#   ./tunnel.sh start    # 터널 시작 (백그라운드)
#   ./tunnel.sh stop     # 터널 중지
#   ./tunnel.sh restart  # 터널 재시작
#   ./tunnel.sh status   # 실행 상태 확인
#
# 재부팅 자동 시작 (crontab -e):
#   @reboot sleep 30 && /home/my_username/tunnel.sh start

# ── 설정 ──────────────────────────────────────────────────────────────
BASE_DIR="/home/my_username"
PID_FILE="${BASE_DIR}/.tunnel/tunnel.pid"

B_SERVER_USER="b_username"
B_SERVER_HOST="b_server_host"
B_SERVER_SSH_PORT=22

C_SERVER_HOST="c_server_host"
C_SERVER_DB_PORT=5432

TUNNEL_BIND_PORT=15432   # B서버에서 열릴 로컬 포트

RECONNECT_DELAY=5        # 초기 재연결 대기(초)
RECONNECT_DELAY_MAX=60   # 최대 재연결 대기(초)
# ──────────────────────────────────────────────────────────────────────

mkdir -p "${BASE_DIR}/.tunnel"

is_running() {
    if [ ! -f "${PID_FILE}" ]; then
        return 1
    fi
    local pid
    pid=$(cat "${PID_FILE}")
    if kill -0 "${pid}" 2>/dev/null; then
        return 0
    else
        rm -f "${PID_FILE}"
        return 1
    fi
}

run_tunnel_loop() {
    local delay=${RECONNECT_DELAY}
    local ssh_pid=""

    trap '
        if [ -n "${ssh_pid}" ]; then
            kill "${ssh_pid}" 2>/dev/null
            wait "${ssh_pid}" 2>/dev/null
        fi
        exit 0
    ' TERM INT

    while true; do
        ssh \
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
        ssh_pid=""

        sleep "${delay}"
        delay=$(( delay * 2 ))
        if [ "${delay}" -gt "${RECONNECT_DELAY_MAX}" ]; then
            delay=${RECONNECT_DELAY_MAX}
        fi
    done
}

start() {
    if is_running; then
        echo "터널이 이미 실행 중입니다. (PID: $(cat ${PID_FILE}))"
        exit 0
    fi

    run_tunnel_loop &
    echo $! > "${PID_FILE}"
    echo "터널 시작됨 (PID: $(cat ${PID_FILE}))"
}

stop() {
    if ! is_running; then
        echo "실행 중인 터널이 없습니다."
        exit 0
    fi

    local pid
    pid=$(cat "${PID_FILE}")
    kill "${pid}" 2>/dev/null

    local waited=0
    while kill -0 "${pid}" 2>/dev/null && [ "${waited}" -lt 5 ]; do
        sleep 1
        waited=$(( waited + 1 ))
    done

    rm -f "${PID_FILE}"
    echo "터널 중지됨 (PID: ${pid})"
}

status() {
    if is_running; then
        echo "터널 실행 중 (PID: $(cat ${PID_FILE}))"
    else
        echo "터널이 실행되지 않고 있습니다."
    fi
}

# ── 진입점 ────────────────────────────────────────────────────────────
case "$1" in
    start)   start ;;
    stop)    stop ;;
    restart) stop; sleep 2; start ;;
    status)  status ;;
    *)
        echo "사용법: $0 {start|stop|restart|status}"
        exit 1
        ;;
esac
