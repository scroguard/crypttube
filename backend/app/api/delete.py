"""Delete media for good (file, database entries, generated content): admins in the main
libraries, everyone in their own libraries."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.auth import User, require_user
from app.config import get_settings
from app.deletion import DeleteError, delete_content

router = APIRouter(tags=["delete"])


class DeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["scenes", "images", "galleries"]
    ids: list[int] = Field(min_length=1, max_length=5000)


@router.post("/delete")
async def delete(body: DeleteRequest, request: Request, user: User = Depends(require_user)) -> dict:
    """Deletes the files from disk. A gallery takes its archive, or all the images in its
    folder, with it. Items that can't be deleted are listed under `skipped`."""
    pool = request.app.state.pool
    # A Live Photo's motion goes with it.
    live = [r["live_scene_id"] for r in await pool.fetch(
        "SELECT live_scene_id FROM images WHERE id = ANY($1::bigint[]) AND live_scene_id IS NOT NULL",
        body.ids)] if body.kind == "images" else []
    try:
        result = await delete_content(pool, get_settings().generated_dir, body.kind, body.ids,
                                      user.acl, admin=user.is_admin, user_id=user.id)
        if live and result["deleted"]:
            gone = set(body.ids) - {s["id"] for s in result["skipped"]}
            still = {r["live_scene_id"] for r in await pool.fetch(
                "SELECT live_scene_id FROM images WHERE id = ANY($1::bigint[])", list(gone)) if r["live_scene_id"]}
            motion = [sid for sid in live if sid not in still]
            if motion:
                extra = await delete_content(pool, get_settings().generated_dir, "scenes", motion,
                                             user.acl, admin=user.is_admin, user_id=user.id)
                result["files"] += extra["files"]
                result["bytes"] += extra["bytes"]
        return result
    except DeleteError as e:
        raise HTTPException(409, str(e)) from e
