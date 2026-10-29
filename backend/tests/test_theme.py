from app.api.theme import ROLES

CRYPT = {r: "#0B0B10" for r in ROLES} | {"overlay": "rgba(5,5,8,0.72)", "accent": "#9164CA"}


async def test_no_theme_saved_gives_an_empty_stylesheet(anon, clean):
    r = await anon.get("/api/theme.css")
    assert r.status_code == 200 and r.text == ""
    assert r.headers["content-type"].startswith("text/css") and r.headers["cache-control"] == "no-cache"


async def test_admin_saves_a_theme_everyone_gets_it(client, anon, clean):
    r = await client.put("/api/theme", json={"preset": "crypt", "colors": CRYPT})
    assert r.status_code == 200 and r.json()["theme"]["colors"]["accent"] == "#9164CA"
    css = (await anon.get("/api/theme.css")).text            # no sign-in needed
    assert css.startswith(":root {") and "  --color-accent: #9164CA;\n" in css
    assert css.index("--color-bg:") < css.index("--color-danger-solid-border:")
    assert (await client.delete("/api/theme")).status_code == 204
    assert (await anon.get("/api/theme.css")).text == ""


async def test_only_admins_can_change_or_read_the_editor_state(login_as, anon, clean):
    editor = await login_as("ed", "editor")
    assert (await editor.put("/api/theme", json={"preset": "crypt", "colors": CRYPT})).status_code == 403
    assert (await editor.get("/api/theme")).status_code == 403
    assert (await anon.delete("/api/theme")).status_code == 401


async def test_only_plain_colors_reach_the_css(client, clean):
    bad = [
        CRYPT | {"accent": "red"},
        CRYPT | {"accent": "#9164CA; } body { display: none"},
        CRYPT | {"overlay": "rgba(5,5,8,0.72); color: red"},
        CRYPT | {"overlay": "rgba(300,5,8,0.5)"},
        {k: v for k, v in CRYPT.items() if k != "link"},          # a role missing
        CRYPT | {"evil": "#000000"},                               # an unknown role
    ]
    for colors in bad:
        assert (await client.put("/api/theme", json={"preset": "crypt", "colors": colors})).status_code == 422, colors
    assert (await client.put("/api/theme", json={"preset": "x y", "colors": CRYPT})).status_code == 422
