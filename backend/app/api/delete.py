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
    try:
        return await delete_content(request.app.state.pool, get_settings().generated_dir, body.kind, body.ids,
                                    user.acl, admin=user.is_admin, user_id=user.id)
    except DeleteError as e:
        raise HTTPException(409, str(e)) from e
