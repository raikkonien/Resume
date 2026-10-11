import os
from pathlib import Path
import secrets
import tempfile

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))


def _session_secret() -> str:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / ".session-key"
    if not path.exists():
        # Publish a complete key atomically; concurrent starters use the winner.
        with tempfile.NamedTemporaryFile(mode="w", dir=DATA_DIR, prefix=".session-key-",
                                         delete=False) as output:
            temporary = Path(output.name)
            output.write(secrets.token_hex(32))
        try:
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
        finally:
            temporary.unlink()
    secret = path.read_text(encoding="ascii").strip()
    if len(secret) != 64 or any(char not in "0123456789abcdef" for char in secret):
        raise RuntimeError("数据目录中的 .session-key 无效，请从备份恢复登录密钥")
    return secret


ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
INITIAL_ADMIN_PASSWORD = os.environ.get("INITIAL_ADMIN_PASSWORD", "")
MCP_TOKEN = os.environ.get("MCP_TOKEN", "")
SECRET_KEY = os.environ.get("SECRET_KEY") or _session_secret()
DB_PATH = DATA_DIR / "resume.db"
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
MCP_ALLOWED_HOSTS = [host.strip() for host in os.environ.get(
    "MCP_ALLOWED_HOSTS", "localhost:*,127.0.0.1:*,[::1]:*"
).split(",") if host.strip()]
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "20"))
