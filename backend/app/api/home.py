"""Each user's home page: an ordered list of sections they choose.

The server stores the layout and knows which section types exist; the content of a
section comes from the ordinary list endpoints (so visibility, restrictions and
ownership apply exactly as everywhere else).
"""

from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.acl import visible
from app.api import browse
from app.auth import User, require_user
from app.sql import Q

router = APIRouter(prefix="/me", tags=["home"])

SectionType = Literal[
    "continue_watching", "recent_scenes", "top_rated_scenes", "recent_galleries", "recent_images",
    "favorite_performers", "recent_performers", "studios", "my_uploads",
]

# type -> (label, description, only for users who can upload)
SECTION_TYPES: dict[str, tuple[str, str, bool]] = {
    "continue_watching": ("Continue watching", "Videos you started and haven't finished", False),
    "recent_scenes": ("Recently added scenes", "The newest videos", False),
    "top_rated_scenes": ("Top rated scenes", "The highest rated videos", False),
    "recent_galleries": ("Recently added galleries", "The newest galleries", False),
    "recent_images": ("Recently added images", "The newest images", False),
    "favorite_performers": ("Favourite performers", "Performers you marked with a heart", False),
    "recent_performers": ("Recently added performers", "The newest performers", False),
    "studios": ("Studios", "Studios, A to Z", False),
    "my_uploads": ("My uploads", "The newest videos from your own libraries", True),
}
DEFAULT_HOME = ["continue_watching", "recent_scenes", "favorite_performers", "recent_galleries"]
DEFAULT_LIMIT = 12


class HomeSection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: SectionType
    limit: int = Field(DEFAULT_LIMIT, ge=1, le=48)
    title: str | None = Field(None, max_length=60)   # optional custom heading


class HomeLayout(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sections: list[HomeSection] = Field(max_length=20)


def _available(user: User) -> list[dict]:
    return [{"type": t, "label": label, "description": desc}
            for t, (label, desc, needs_upload) in SECTION_TYPES.items() if user.can_upload or not needs_upload]


def _default() -> list[dict]:
    return [{"type": t, "limit": DEFAULT_LIMIT, "title": None} for t in DEFAULT_HOME]


@router.get("/home")
async def get_home(request: Request, user: User = Depends(require_user)) -> dict:
    stored = await request.app.state.pool.fetchval("SELECT home FROM user_settings WHERE user_id = $1", user.id)
    allowed = {a["type"] for a in _available(user)}
    sections = stored if stored is not None else _default()
    # Drop sections whose type no longer applies (e.g. uploads were switched off).
    sections = [s for s in sections if s.get("type") in allowed]
    return {"sections": sections, "available": _available(user), "customized": stored is not None}


@router.put("/home")
async def set_home(body: HomeLayout, request: Request, user: User = Depends(require_user)) -> dict:
    sections = [s.model_dump() for s in body.sections]
    await request.app.state.pool.execute(
        """INSERT INTO user_settings (user_id, home) VALUES ($1, $2)
           ON CONFLICT (user_id) DO UPDATE SET home = EXCLUDED.home, updated_at = now()""",
        user.id, sections)
    return await get_home(request, user)


@router.delete("/home")
async def reset_home(request: Request, user: User = Depends(require_user)) -> dict:
    """Back to the default layout."""
    await request.app.state.pool.execute("UPDATE user_settings SET home = NULL WHERE user_id = $1", user.id)
    return await get_home(request, user)


@router.get("/continue-watching")
async def continue_watching(request: Request, limit: int = Query(DEFAULT_LIMIT, ge=1, le=48),
                            thumb_w: int = 640, user: User = Depends(require_user)) -> dict:
    """My unfinished videos, most recently watched first."""
    browse._check_thumb(thumb_w)
    pool = request.app.state.pool
    q = Q()
    conds = [f"us.user_id = {q.p(user.id)}", "f.missing_since IS NULL", "NOT s.is_live_photo",
             *visible(user.acl, "scene", "s", q)]
    ids = [r["id"] for r in await pool.fetch(
        f"""SELECT s.id FROM user_scene_state us
            JOIN scenes s ON s.id = us.scene_id JOIN files f ON f.id = s.file_id
            WHERE {' AND '.join(conds)}
            ORDER BY us.updated_at DESC LIMIT {q.p(limit)}""", *q.args)]
    items = await browse.scenes_by_id(pool, ids, user.acl, thumb_w) if ids else {}
    return {"items": [items[i] for i in ids if i in items], "next_cursor": None, "total": None}
