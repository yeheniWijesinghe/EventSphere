"""Password hashing (PBKDF2-SHA256, salted), CSRF tokens and unguessable ticket codes."""
import hashlib
import hmac
import secrets

from fastapi import HTTPException, Request

from .config import settings


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    iterations = settings.pbkdf2_iterations
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt_hex, digest_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations))
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def new_ticket_code() -> str:
    """~144 bits of randomness from the OS CSPRNG -> cannot be guessed or enumerated."""
    return secrets.token_urlsafe(18)


def get_csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


def check_csrf(request: Request, token: str | None) -> None:
    expected = request.session.get("csrf")
    if not expected or not token or not hmac.compare_digest(str(expected), str(token)):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token. Reload the page and try again.")
