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
from dataclasses import dataclass, replace
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
    can_upload: bool = False
    upload_quota_bytes: int | None = None

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def can_edit(self) -> bool:
        return self.role in ("admin", "editor")

    def public(self) -> dict:
        return {"id": self.id, "username": self.username, "role": self.role, "can_upload": self.can_upload}


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
        """SELECT s.id AS session_id, s.last_seen_at, u.id, u.username, u.role,
                  u.can_upload, u.upload_quota_bytes
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
    # Admins skip the deny rules, but not other users' library privacy.
    acl = await load_acl(pool, row["id"], deny_rules=row["role"] != "admin")
    acl = replace(acl, editor=row["role"] in ("admin", "editor"), can_upload=row["can_upload"])
    return User(row["id"], row["username"], row["role"], acl, row["session_id"],
                row["can_upload"], row["upload_quota_bytes"])


# ---------------------------------------------------------------------------
# Login throttling
# ---------------------------------------------------------------------------

class _Backoff:
    """Exponential backoff per key after `free` failures; a key's failures are forgotten after `window`."""

    MAX_DELAY = 300.0
    MAX_KEYS = 10_000

    def __init__(self, free: int, window: float, clock, max_delay: float = MAX_DELAY):
        self.free, self.window, self.clock, self.max_delay = free, window, clock, max_delay
        self._state: dict[object, tuple[int, float, float]] = {}    # failures, allowed_at, last failure

    def retry_after(self, key) -> float:
        _, allowed_at, _ = self._state.get(key, (0, 0.0, 0.0))
        return max(0.0, allowed_at - self.clock())

    def failure(self, key) -> None:
        now = self.clock()
        failures, _, last = self._state.pop(key, (0, 0.0, now))   # re-inserted below: the newest last
        if len(self._state) >= self.MAX_KEYS:
            del self._state[next(iter(self._state))]                # bounded: drop the least recent key
        failures = 1 if now - last >= self.window else failures + 1
        delay = 0.0 if failures <= self.free else min(2.0 ** min(failures - self.free, 16), self.max_delay)
        self._state[key] = (failures, now + delay, now)

    def clear(self, key) -> None:
        self._state.pop(key, None)


class LoginThrottle:
    """Login backoff, in memory (one API process on a home server; resets on restart), on three keys:
    - (client IP, username): 3 free failures; the usual typo case.
    - username, from any address: 10 free, then at most 30 s apart, so guessing one account from many
      addresses is slowed too, while someone failing on purpose can't hold the real user out for long.
    - client IP, for any username: 20 free, so one address can't try many accounts.
    A correct password clears the first two, not the address's (one valid account mustn't reset it)."""

    WINDOW = 3600.0

    def __init__(self, clock=time.monotonic):
        self.pair = _Backoff(3, self.WINDOW, clock)
        self.account = _Backoff(10, self.WINDOW, clock, max_delay=30.0)
        self.address = _Backoff(20, self.WINDOW, clock)

    def retry_after(self, key: tuple[str, str]) -> float:
        ip, user = key
        return max(self.pair.retry_after(key), self.account.retry_after(user), self.address.retry_after(ip))

    def failure(self, key: tuple[str, str]) -> None:
        ip, user = key
        self.pair.failure(key)
        self.account.failure(user)
        self.address.failure(ip)

    def success(self, key: tuple[str, str]) -> None:
        self.pair.clear(key)
        self.account.clear(key[1])


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
