"""Admin: user accounts and per-user content restrictions."""

import asyncio
from typing import Literal

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from app import auth, counts
from app.api.session import Password, Username
from app.auth import User, require_admin

router = APIRouter(prefix="/users", tags=["users"])

Role = Literal["admin", "editor", "viewer"]
COLUMNS = "u.id, u.username, u.role, u.is_active, u.created_at, u.last_login_at"


class UserCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Username
    password: str = Password
    role: Role = "viewer"


class UserPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str | None = Field(None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    password: str | None = Field(None, min_length=8, max_length=1024)
    role: Role | None = None
    is_active: bool | None = None


class Restrictions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tag_ids: list[int] = []
    studio_ids: list[int] = []
    performer_ids: list[int] = []


RESTRICTION_TABLES = (("tag_ids", "user_hidden_tags", "tag_id", "tags"),
                      ("studio_ids", "user_hidden_studios", "studio_id", "studios"),
                      ("performer_ids", "user_hidden_performers", "performer_id", "performers"))


async def _restrictions(db, user_id: int) -> dict:
    out = {}
    for key, table, col, entity in RESTRICTION_TABLES:
        rows = await db.fetch(
            f"""SELECT e.id, e.name FROM {table} r JOIN {entity} e ON e.id = r.{col}
                WHERE r.user_id = $1 ORDER BY e.name""", user_id)
        out[key.replace("_ids", "s")] = [dict(r) for r in rows]
    return out


async def _get(db, user_id: int) -> dict:
    row = await db.fetchrow(f"SELECT {COLUMNS} FROM users u WHERE u.id = $1", user_id)
    if row is None:
        raise HTTPException(404, "user not found")
    return dict(row) | {"restrictions": await _restrictions(db, user_id)}


async def _ensure_an_admin_remains(conn) -> None:
    if not await conn.fetchval("SELECT EXISTS (SELECT 1 FROM users WHERE role = 'admin' AND is_active)"):
        raise HTTPException(409, "at least one active admin must remain")


@router.get("")
async def list_users(request: Request, _: User = Depends(require_admin)) -> list[dict]:
    rows = await request.app.state.pool.fetch(f"""
        SELECT {COLUMNS},
               (SELECT count(*) FROM user_hidden_tags WHERE user_id = u.id)
             + (SELECT count(*) FROM user_hidden_studios WHERE user_id = u.id)
             + (SELECT count(*) FROM user_hidden_performers WHERE user_id = u.id) AS restriction_count
        FROM users u ORDER BY lower(u.username)""")
    return [dict(r) for r in rows]


@router.post("", status_code=201)
async def create_user(body: UserCreate, request: Request, _: User = Depends(require_admin)) -> dict:
    pool = request.app.state.pool
    password_hash = await asyncio.to_thread(auth.hash_password, body.password)
    try:
        user_id = await pool.fetchval(
            "INSERT INTO users (username, password_hash, role) VALUES ($1, $2, $3) RETURNING id",
            body.username, password_hash, body.role)
    except asyncpg.UniqueViolationError:
        raise HTTPException(409, "username already taken") from None
    return await _get(pool, user_id)


@router.get("/{user_id}")
async def get_user(user_id: int, request: Request, _: User = Depends(require_admin)) -> dict:
    return await _get(request.app.state.pool, user_id)


@router.patch("/{user_id}")
async def update_user(user_id: int, body: UserPatch, request: Request, _: User = Depends(require_admin)) -> dict:
    pool = request.app.state.pool
    fields = body.model_dump(exclude_unset=True)
    if any(v is None for v in fields.values()):
        raise HTTPException(422, "fields cannot be null")
    password = fields.pop("password", None)
    if password is not None:
        fields["password_hash"] = await asyncio.to_thread(auth.hash_password, password)
    if not fields:
        return await _get(pool, user_id)
    sets = ", ".join(f"{k} = ${i}" for i, k in enumerate(fields, start=2))
    try:
        async with pool.acquire() as conn, conn.transaction():
            if not await conn.fetchval(f"UPDATE users SET {sets} WHERE id = $1 RETURNING true",
                                       user_id, *fields.values()):
                raise HTTPException(404, "user not found")
            await _ensure_an_admin_remains(conn)
            # Credentials changed or access revoked: end their sessions immediately.
            if password is not None or fields.get("is_active") is False:
                await conn.execute("DELETE FROM sessions WHERE user_id = $1", user_id)
    except asyncpg.UniqueViolationError:
        raise HTTPException(409, "username already taken") from None
    if "role" in fields:
        await counts.request_refresh(pool)  # admins ignore restrictions; counts differ
    return await _get(pool, user_id)


@router.delete("/{user_id}", status_code=204)
async def delete_user(user_id: int, request: Request, admin: User = Depends(require_admin)) -> Response:
    if user_id == admin.id:
        raise HTTPException(409, "you cannot delete your own account")
    async with request.app.state.pool.acquire() as conn, conn.transaction():
        if not await conn.fetchval("DELETE FROM users WHERE id = $1 RETURNING true", user_id):
            raise HTTPException(404, "user not found")
        await _ensure_an_admin_remains(conn)
    await counts.request_refresh(request.app.state.pool)
    return Response(status_code=204)


@router.put("/{user_id}/restrictions")
async def set_restrictions(user_id: int, body: Restrictions, request: Request,
                           _: User = Depends(require_admin)) -> dict:
    """Replace the user's deny rules. Hidden tags/studios include their descendants."""
    pool = request.app.state.pool
    try:
        async with pool.acquire() as conn, conn.transaction():
            if not await conn.fetchval("SELECT true FROM users WHERE id = $1", user_id):
                raise HTTPException(404, "user not found")
            for key, table, col, _entity in RESTRICTION_TABLES:
                await conn.execute(f"DELETE FROM {table} WHERE user_id = $1", user_id)
                await conn.execute(
                    f"INSERT INTO {table} (user_id, {col}) SELECT $1, unnest($2::bigint[]) ON CONFLICT DO NOTHING",
                    user_id, getattr(body, key))
    except asyncpg.ForeignKeyViolationError:
        raise HTTPException(422, "referenced tag/studio/performer does not exist") from None
    await counts.request_refresh(pool)
    return await _get(pool, user_id)
