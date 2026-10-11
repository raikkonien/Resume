import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from itsdangerous import TimestampSigner


class PersistentSessionSecretTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.environment = dict(os.environ, DATA_DIR=str(self.root))
        self.environment.pop("SECRET_KEY", None)
        self.command = [sys.executable, "-c", "from app.config import SECRET_KEY; print(SECRET_KEY)"]
        self.cwd = Path(__file__).resolve().parents[1]

    def start(self, environment=None):
        process = subprocess.Popen(self.command, cwd=self.cwd, env=environment or self.environment,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        return process

    def key(self, process):
        stdout, stderr = process.communicate(timeout=20)
        self.assertEqual(process.returncode, 0, stderr)
        return stdout.strip()

    def test_session_signature_survives_restart_and_concurrent_initialization(self):
        processes = [self.start() for _ in range(6)]
        keys = [self.key(process) for process in processes]
        cookie = TimestampSigner(keys[0]).sign(b"authenticated-session")
        for key in keys[1:] + [self.key(self.start())]:
            self.assertEqual(TimestampSigner(key).unsign(cookie), b"authenticated-session")
        self.assertEqual((self.root / ".session-key").stat().st_mode & 0o777, 0o600)

    def test_explicit_secret_takes_precedence_without_replacing_saved_key(self):
        saved = self.key(self.start())
        overridden = self.key(self.start(dict(self.environment, SECRET_KEY="explicit-session-secret")))
        self.assertEqual(overridden, "explicit-session-secret")
        cookie = TimestampSigner(saved).sign(b"persisted-session")
        self.assertEqual(TimestampSigner(self.key(self.start())).unsign(cookie), b"persisted-session")

    def test_corrupt_saved_secret_stops_startup_instead_of_invalidating_sessions(self):
        (self.root / ".session-key").write_text("", encoding="ascii")
        process = self.start()
        _, stderr = process.communicate(timeout=20)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn("RuntimeError", stderr)
        self.assertEqual((self.root / ".session-key").read_bytes(), b"")


if __name__ == "__main__":
    unittest.main()
