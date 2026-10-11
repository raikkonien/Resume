import hashlib
import hmac
import secrets


_ITERATIONS = 600_000


def hash_password(password: str) -> str:
    """Hash the exact password; policy is enforced only for newly managed users."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return f"pbkdf2_sha256${_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt_hex, digest_hex = encoded.split("$")
        iterations = int(rounds)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
        if algorithm != "pbkdf2_sha256" or not 1 <= iterations <= 2_000_000:
            return False
        if len(salt) != 16 or len(expected) != 32:
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        return hmac.compare_digest(actual, expected)
    except (AttributeError, TypeError, ValueError, UnicodeError):
        return False
