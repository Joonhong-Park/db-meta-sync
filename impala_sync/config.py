"""
D서버 DB 접속 설정 — 환경변수 오버라이드 가능
"""
import os

D_DB_HOST     = os.environ.get("D_DB_HOST",     "d_server_host")
D_DB_PORT     = int(os.environ.get("D_DB_PORT", "5432"))
D_DB_NAME     = os.environ.get("D_DB_NAME",     "your_d_database")
D_DB_USER     = os.environ.get("D_DB_USER",     "your_d_username")
D_DB_PASSWORD = os.environ.get("D_DB_PASSWORD", "your_d_password")

DB_CONNECT_TIMEOUT = int(os.environ.get("DB_CONNECT_TIMEOUT", "10"))
