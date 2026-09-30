"""Authentication: password hashing, sessions, login throttling, and the FastAPI
dependencies every route hangs off.

Sessions are opaque random tokens in an HttpOnly cookie (media elements such as
<video src> can't send Authorization headers, so a cookie is the only way to protect
streams). The DB stores only SHA-256(token), so sessions are revocable and a DB leak
doesn't yield usable tokens.

CSRF: the cookie is SameSite=Lax, and every state-changing request must carry the
X-Requested-With header. Browsers won't let another site add a custom header without a
CORS preflight, which this API never approves.
"""

import asyncio
import hashlib
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import asyncpg
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Depends, HTTPException, Request

from app.acl import Acl, load_acl
from app.config import get_settings

COOKIE_NAME = "session"
CSRF_HEADER = "x-requested-with"
SESSION_TTL = timedelta(days=30)
LAST_SEEN_RESOLUTION = timedelta(minutes=5)
ROLES = ("viewer", "editor", "admin")
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

_hasher = PasswordHasher()  # argon2id with library defaults
# Verified against when the username doesn't exist, so response time doesn't reveal
# which usernames are real.
_DUMMY_HASH = _hasher.hash(secrets.token_hex(16))


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored: str, password: str) -> bool:
    try:
        return _hasher.verify(stored, password)
    except (VerificationError, InvalidHashError):
        return False


def _token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


@dataclass(frozen=True)
class User:
    id: int
    username: str
    role: str
    acl: Acl
    session_id: int

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def can_edit(self) -> bool:
        return self.role in ("admin", "editor")

    def public(self) -> dict:
        return {"id": self.id, "username": self.username, "role": self.role}


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

async def create_session(db, user_id: int, user_agent: str | None) -> str:
    token = secrets.token_urlsafe(32)
    await db.execute(
        """INSERT INTO sessions (token_hash, user_id, user_agent, expires_at)
           VALUES ($1, $2, $3, $4)""",
        _token_hash(token), user_id, (user_agent or "")[:300],
        datetime.now(timezone.utc) + SESSION_TTL,
    )
    await db.execute("UPDATE users SET last_login_at = now() WHERE id = $1", user_id)
    # Opportunistic cleanup; there is no separate sweeper.
    await db.execute("DELETE FROM sessions WHERE expires_at < now()")
    return token


def set_session_cookie(response, token: str) -> None:
    response.set_cookie(
        COOKIE_NAME, token, max_age=int(SESSION_TTL.total_seconds()), httponly=True,
        samesite="lax", secure=get_settings().cookie_secure, path="/",
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/", httponly=True, samesite="lax",
                           secure=get_settings().cookie_secure)


async def user_from_token(pool: asyncpg.Pool, token: str) -> User | None:
    row = await pool.fetchrow(
        """SELECT s.id AS session_id, s.last_seen_at, u.id, u.username, u.role
           FROM sessions s JOIN users u ON u.id = s.user_id
           WHERE s.token_hash = $1 AND s.expires_at > now() AND u.is_active""",
        _token_hash(token),
    )
    if row is None:
        return None
    now = datetime.now(timezone.utc)
    if now - row["last_seen_at"] > LAST_SEEN_RESOLUTION:
        # Sliding expiry, written at most every few minutes rather than per request.
        await pool.execute(
            "UPDATE sessions SET last_seen_at = $2, expires_at = $3 WHERE id = $1",
            row["session_id"], now, now + SESSION_TTL,
        )
    # Admins manage everything, so deny rules never apply to them.
    acl = Acl(user_id=row["id"]) if row["role"] == "admin" else await load_acl(pool, row["id"])
    return User(row["id"], row["username"], row["role"], acl, row["session_id"])


# ---------------------------------------------------------------------------
# Login throttling
# ---------------------------------------------------------------------------

class LoginThrottle:
    """Exponential backoff per (client IP, username) after a few failures.

    In-memory: good enough for one API process on a home server; resets on restart.
    Keyed on the pair so an attacker can't lock a real user out from elsewhere."""

    FREE_ATTEMPTS = 3
    MAX_DELAY = 300.0
    MAX_KEYS = 10_000

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self._state: dict[tuple[str, str], tuple[int, float]] = {}

    def retry_after(self, key: tuple[str, str]) -> float:
        _, allowed_at = self._state.get(key, (0, 0.0))
        return max(0.0, allowed_at - self.clock())

    def failure(self, key: tuple[str, str]) -> None:
        if len(self._state) > self.MAX_KEYS:
            now = self.clock()
            self._state = {k: v for k, v in self._state.items() if v[1] > now}
        failures = self._state.get(key, (0, 0.0))[0] + 1
        delay = 0.0 if failures <= self.FREE_ATTEMPTS else min(2.0 ** (failures - self.FREE_ATTEMPTS), self.MAX_DELAY)
        self._state[key] = (failures, self.clock() + delay)

    def success(self, key: tuple[str, str]) -> None:
        self._state.pop(key, None)


throttle = LoginThrottle()


async def check_credentials(pool: asyncpg.Pool, username: str, password: str):
    row = await pool.fetchrow(
        "SELECT id, password_hash, is_active FROM users WHERE lower(username) = lower($1)", username)
    stored = row["password_hash"] if row else _DUMMY_HASH
    ok = await asyncio.to_thread(verify_password, stored, password)  # argon2 is CPU-bound
    if not ok or row is None or not row["is_active"]:
        return None
    if _hasher.check_needs_rehash(stored):
        await pool.execute("UPDATE users SET password_hash = $2 WHERE id = $1",
                           row["id"], await asyncio.to_thread(hash_password, password))
    return row["id"]


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------

def check_csrf(request: Request) -> None:
    if request.method in UNSAFE_METHODS and not request.headers.get(CSRF_HEADER):
        raise HTTPException(403, f"missing {CSRF_HEADER} header")


async def require_user(request: Request) -> User:
    cached = getattr(request.state, "user", None)
    if cached is not None:
        return cached
    check_csrf(request)
    token = request.cookies.get(COOKIE_NAME)
    user = await user_from_token(request.app.state.pool, token) if token else None
    if user is None:
        raise HTTPException(401, "not authenticated")
    request.state.user = user
    return user


async def require_editor(user: User = Depends(require_user)) -> User:
    if not user.can_edit:
        raise HTTPException(403, "editor role required")
    return user


async def require_admin(user: User = Depends(require_user)) -> User:
    if not user.is_admin:
        raise HTTPException(403, "admin role required")
    return user


async def get_acl(user: User = Depends(require_user)) -> Acl:
    """The viewer's deny rules. Depending on this also enforces authentication, so a
    route that filters by ACL can't accidentally be public."""
    return user.acl
