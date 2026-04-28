"""
Impala 접속 정보 — 환경변수 오버라이드 가능
"""
import os

IMPALA_HOST    = os.environ.get("IMPALA_HOST",    "localhost")
IMPALA_PORT    = int(os.environ.get("IMPALA_PORT", "21050"))
IMPALA_USER    = os.environ.get("IMPALA_USER",    "")
IMPALA_PASSWORD = os.environ.get("IMPALA_PASSWORD", "")
IMPALA_AUTH    = os.environ.get("IMPALA_AUTH",    "PLAIN")   # PLAIN / GSSAPI / LDAP
IMPALA_USE_SSL = os.environ.get("IMPALA_USE_SSL", "false").lower() == "true"
IMPALA_TIMEOUT = int(os.environ.get("IMPALA_TIMEOUT", "30"))
