"""
DB 접속 설정
환경변수로 오버라이드 가능 (운영/개발 환경 분리용)
"""
import os

# ── C서버 DB (SSH 리버스 터널 경유: localhost:15432 → C서버:5432) ────────
C_DB_HOST: str = os.environ.get("C_DB_HOST", "localhost")
C_DB_PORT: int = int(os.environ.get("C_DB_PORT", "15432"))
C_DB_NAME: str = os.environ.get("C_DB_NAME", "your_c_database")
C_DB_USER: str = os.environ.get("C_DB_USER", "your_c_username")
C_DB_PASSWORD: str = os.environ.get("C_DB_PASSWORD", "your_c_password")

# ── D서버 DB (직접 접속) ──────────────────────────────────────────────────
D_DB_HOST: str = os.environ.get("D_DB_HOST", "d_server_host")
D_DB_PORT: int = int(os.environ.get("D_DB_PORT", "5432"))
D_DB_NAME: str = os.environ.get("D_DB_NAME", "your_d_database")
D_DB_USER: str = os.environ.get("D_DB_USER", "your_d_username")
D_DB_PASSWORD: str = os.environ.get("D_DB_PASSWORD", "your_d_password")

# ── 공통 ──────────────────────────────────────────────────────────────────
DB_CONNECT_TIMEOUT: int = int(os.environ.get("DB_CONNECT_TIMEOUT", "10"))
