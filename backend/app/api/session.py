"""Login, logout, first-run setup, own password and sessions."""

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from app import auth
from app.auth import User, require_user

router = APIRouter(prefix="/auth", tags=["auth"])

Username = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
Password = Field(min_length=8, max_length=1024)


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Username
    password: str = Field(min_length=1, max_length=1024)


class SetupBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Username
    password: str = Password


class PasswordChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_password: str = Field(max_length=1024)
    new_password: str = Password


class LogoutBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    everywhere: bool = False


async def _optional_user(request: Request) -> User | None:
    token = request.cookies.get(auth.COOKIE_NAME)
    return await auth.user_from_token(request.app.state.pool, token) if token else None


@router.get("/status")
async def status(request: Request) -> dict:
    """Public: whether first-run setup is needed, and who (if anyone) is signed in."""
    pool = request.app.state.pool
    user = await _optional_user(request)
    return {
        "setup_required": not await pool.fetchval("SELECT EXISTS (SELECT 1 FROM users)"),
        "user": user.public() if user else None,
    }


@router.post("/setup", status_code=201)
async def setup(body: SetupBody, request: Request, response: Response) -> dict:
    """Create the first admin. Only possible while no users exist."""
    auth.check_csrf(request)
    pool = request.app.state.pool
    password_hash = await asyncio.to_thread(auth.hash_password, body.password)
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("LOCK TABLE users IN EXCLUSIVE MODE")  # two racing setups: one wins
        user_id = await conn.fetchval(
            """INSERT INTO users (username, password_hash, role)
               SELECT $1, $2, 'admin' WHERE NOT EXISTS (SELECT 1 FROM users) RETURNING id""",
            body.username, password_hash)
        if user_id is None:
            raise HTTPException(409, "setup already completed")
        token = await auth.create_session(conn, user_id, request.headers.get("user-agent"))
    auth.set_session_cookie(response, token)
    return {"id": user_id, "username": body.username, "role": "admin"}


@router.post("/login")
async def login(body: Credentials, request: Request, response: Response) -> dict:
    auth.check_csrf(request)
    key = (request.client.host if request.client else "?", body.username.lower())
    wait = auth.throttle.retry_after(key)
    if wait > 0:
        raise HTTPException(429, "too many failed attempts", headers={"Retry-After": str(int(wait) + 1)})
    pool = request.app.state.pool
    user_id = await auth.check_credentials(pool, body.username, body.password)
    if user_id is None:
        auth.throttle.failure(key)
        raise HTTPException(401, "invalid username or password")
    auth.throttle.success(key)
    token = await auth.create_session(pool, user_id, request.headers.get("user-agent"))
    auth.set_session_cookie(response, token)
    user = await auth.user_from_token(pool, token)
    return user.public()


@router.post("/logout", status_code=204)
async def logout(request: Request, body: LogoutBody | None = None, user: User = Depends(require_user)) -> Response:
    pool = request.app.state.pool
    if body and body.everywhere:
        await pool.execute("DELETE FROM sessions WHERE user_id = $1", user.id)
    else:
        await pool.execute("DELETE FROM sessions WHERE id = $1", user.session_id)
    response = Response(status_code=204)
    auth.clear_session_cookie(response)
    return response


@router.get("/me")
async def me(user: User = Depends(require_user)) -> dict:
    return user.public() | {"can_edit": user.can_edit}


@router.post("/password", status_code=204)
async def change_password(body: PasswordChange, request: Request, user: User = Depends(require_user)) -> Response:
    """Change own password; signs out every other session."""
    pool = request.app.state.pool
    stored = await pool.fetchval("SELECT password_hash FROM users WHERE id = $1", user.id)
    if not await asyncio.to_thread(auth.verify_password, stored, body.current_password):
        raise HTTPException(403, "current password is wrong")
    new_hash = await asyncio.to_thread(auth.hash_password, body.new_password)
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("UPDATE users SET password_hash = $2 WHERE id = $1", user.id, new_hash)
        await conn.execute("DELETE FROM sessions WHERE user_id = $1 AND id <> $2", user.id, user.session_id)
    return Response(status_code=204)


@router.get("/sessions")
async def my_sessions(request: Request, user: User = Depends(require_user)) -> list[dict]:
    rows = await request.app.state.pool.fetch(
        """SELECT id, user_agent, created_at, last_seen_at FROM sessions
           WHERE user_id = $1 AND expires_at > now() ORDER BY last_seen_at DESC""", user.id)
    return [dict(r) | {"current": r["id"] == user.session_id} for r in rows]


@router.delete("/sessions/{session_id}", status_code=204)
async def revoke_session(session_id: int, request: Request, user: User = Depends(require_user)) -> Response:
    deleted = await request.app.state.pool.fetchval(
        "DELETE FROM sessions WHERE id = $1 AND user_id = $2 RETURNING id", session_id, user.id)
    if deleted is None:
        raise HTTPException(404, "session not found")
    return Response(status_code=204)
