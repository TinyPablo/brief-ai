import os
import sys

# Make `import app` resolve to backend/app.py regardless of pytest's rootdir.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# app.py reads these at import time and fails hard if they're missing/invalid.
os.environ.setdefault("TOTP_SECRET", "JBSWY3DPEHPK3PXP")  # dummy but valid base32
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy-key")

import psycopg2  # noqa: E402


class _FakeConn:
    """Stands in for a real DB connection so importing app.py (which calls
    init_db() at module load time) doesn't try to reach a real Postgres."""

    def cursor(self, *args, **kwargs):
        return self

    def execute(self, *args, **kwargs):
        pass

    def fetchone(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        pass


psycopg2.connect = lambda *a, **kw: _FakeConn()
