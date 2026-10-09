"""A user's own preferences (shown on their account page)."""

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from app.auth import User, require_user

router = APIRouter(prefix="/me/preferences", tags=["preferences"])


class Preferences(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Keep what is selected when going to another page of a paged list.
    keep_selection_across_pages: bool = False


@router.get("")
async def get_preferences(request: Request, user: User = Depends(require_user)) -> dict:
    row = await request.app.state.pool.fetchrow(
        "SELECT keep_selection_across_pages FROM user_settings WHERE user_id = $1", user.id)
    return dict(row) if row else Preferences().model_dump()


@router.put("")
async def set_preferences(body: Preferences, request: Request, user: User = Depends(require_user)) -> dict:
    await request.app.state.pool.execute(
        """INSERT INTO user_settings (user_id, keep_selection_across_pages) VALUES ($1, $2)
           ON CONFLICT (user_id) DO UPDATE SET keep_selection_across_pages = EXCLUDED.keep_selection_across_pages,
                                               updated_at = now()""",
        user.id, body.keep_selection_across_pages)
    return body.model_dump()
