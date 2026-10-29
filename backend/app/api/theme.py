"""The site's color theme, one for everyone, set by admins (Admin → Theme).

The theme is the 30 color roles of the brand's design (frontend/src/index.css), stored
as given by the editor. Presets live in the frontend (src/lib/themes.ts); the server
only checks every value is a plain color, so nothing but colors can reach the CSS. The
stylesheet is public: the sign-in page is themed too, and it says nothing about content.
"""

import re

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app import maintenance

THEME_KEY = "theme"
# Same order and names as frontend/src/index.css and src/lib/themes.ts.
ROLES = (
    "bg", "surface", "surface-raised", "surface-hover", "border", "border-strong",
    "text", "text-muted", "text-subtle", "accent", "accent-hover", "accent-fg",
    "primary", "primary-hover", "primary-fg", "link", "focus",
    "success", "success-bg", "warning", "warning-bg", "danger", "danger-bg", "info", "info-bg",
    "overlay", "danger-solid", "danger-solid-hover", "danger-solid-fg", "danger-solid-border",
)
HEX = re.compile(r"#[0-9a-fA-F]{6}")
RGBA = re.compile(r"rgba\((\d{1,3}), ?(\d{1,3}), ?(\d{1,3}), ?(0|1|0?\.\d{1,3})\)")

public_router = APIRouter()   # the stylesheet: no sign-in needed
router = APIRouter()          # the editor: admins only (see main.py)


def _color(role: str, value: str) -> str:
    if role == "overlay":
        m = RGBA.fullmatch(value)
        if not m or any(int(c) > 255 for c in m.groups()[:3]):
            raise ValueError(f"{role}: expected rgba(r, g, b, alpha)")
    elif not HEX.fullmatch(value):
        raise ValueError(f"{role}: expected #rrggbb")
    return value


class Theme(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preset: str = Field(min_length=1, max_length=40, pattern=r"^[a-z0-9-]+$")
    colors: dict[str, str]

    @field_validator("colors")
    @classmethod
    def all_roles(cls, colors: dict[str, str]) -> dict[str, str]:
        if set(colors) != set(ROLES):
            missing, unknown = set(ROLES) - set(colors), set(colors) - set(ROLES)
            raise ValueError(f"colors must have exactly the theme roles (missing {sorted(missing)}, unknown {sorted(unknown)})")
        return {r: _color(r, colors[r]) for r in ROLES}


@public_router.get("/theme.css")
async def theme_css(request: Request) -> Response:
    """The saved theme as CSS variables; empty when none was saved (the app's built-in
    colors apply). Not cached, so a change shows on the next page load."""
    theme = await maintenance.get_state(request.app.state.pool, THEME_KEY)
    body = ""
    if theme:
        body = ":root {\n" + "".join(f"  --color-{r}: {_color(r, theme['colors'][r])};\n" for r in ROLES) + "}\n"
    return Response(body, media_type="text/css", headers={"Cache-Control": "no-cache"})


@router.get("/theme")
async def get_theme(request: Request) -> dict:
    """The saved theme, or null when the built-in one applies."""
    return {"theme": await maintenance.get_state(request.app.state.pool, THEME_KEY)}


@router.put("/theme")
async def save_theme(body: Theme, request: Request) -> dict:
    await maintenance.set_state(request.app.state.pool, THEME_KEY, body.model_dump())
    return await get_theme(request)


@router.delete("/theme", status_code=204)
async def reset_theme(request: Request) -> None:
    """Back to the built-in theme."""
    await request.app.state.pool.execute("DELETE FROM system_state WHERE key = $1", THEME_KEY)
