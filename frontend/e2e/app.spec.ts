import { expect, test, type Page } from "@playwright/test";
import fs from "node:fs";

// One long, ordered journey through the app on a fresh, isolated stack.
test.describe.configure({ mode: "serial" });

const ADMIN = { username: "admin", password: "e2e-admin-password" };
const SHOTS = "test-results/screens";
let page: Page;

async function waitForApi(p: Page) {
  for (let i = 0; i < 60; i++) {
    const r = await p.request.get("/api/health").catch(() => null);
    if (r?.ok()) return;
    await p.waitForTimeout(1000);
  }
  throw new Error("API never became healthy");
}

const nav = (name: string) => page.getByRole("navigation", { name: "Main" }).getByRole("link", { name, exact: true });
const adminNav = (name: string) => page.getByRole("navigation", { name: "Admin" }).getByRole("link", { name, exact: true });

async function login(username: string, password: string) {
  await page.goto("/");
  await page.getByLabel("Username").fill(username);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("navigation", { name: "Main" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Home" })).toBeVisible();   // signed-in users land on Home
  await nav("Scenes").click();
  await expect(page).toHaveURL(/\/scenes/);
}

test.beforeAll(async ({ browser }) => {
  page = await browser.newPage();
  await waitForApi(page);
});

test("first run: create the admin account", async () => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Welcome" })).toBeVisible();
  await page.screenshot({ path: `${SHOTS}/01-setup.png` });
  await page.getByLabel("Username").fill(ADMIN.username);
  await page.getByLabel("Password (8+ characters)").fill(ADMIN.password);
  await page.getByLabel("Confirm password").fill(ADMIN.password);
  await page.getByRole("button", { name: "Create account" }).click();
  // a brand-new install lands on an empty home page that points the admin at Libraries
  await expect(page.getByTestId("home-empty")).toContainText("Add a library under Admin → Libraries");
  await nav("Scenes").click();
  await expect(page.getByText("No scenes match.")).toBeVisible();
});

test("admin sets how many preview jobs run at once", async () => {
  await nav("Admin").click();
  await adminNav("Jobs").click();
  const box = page.getByTestId("worker-settings");
  await expect(box.getByLabel("Preview jobs at once")).toHaveValue("1");
  await box.getByLabel("Preview jobs at once").selectOption("3");
  await box.getByRole("button", { name: "Save" }).click();
  await expect(box.getByRole("button", { name: "Save" })).toBeDisabled();
  await page.reload();
  await expect(box.getByLabel("Preview jobs at once")).toHaveValue("3");
});

test("admin adds a library and it gets indexed", async () => {
  await nav("Admin").click();
  // pick the folder with the browser rather than typing it
  await page.getByRole("button", { name: "Browse…" }).click();
  const picker = page.getByRole("dialog", { name: "Choose a library folder" });
  await picker.getByRole("button", { name: "Open lib" }).click();
  await expect(picker.getByTestId("picker-path")).toHaveText("/media/lib");
  await expect(picker.getByRole("button", { name: "Open videos" })).toBeVisible();
  await page.screenshot({ path: `${SHOTS}/02a-folder-picker.png` });
  await picker.getByRole("button", { name: "Use this folder" }).click();
  await expect(page.getByLabel("Library path")).toHaveValue("/media/lib");
  await page.getByRole("button", { name: "Add library" }).click();
  await expect(page.getByTestId("library")).toContainText("/media/lib");
  await expect(page.getByTestId("watch-state")).toHaveText("Watching");
  await expect(page.getByText(/Queued job #\d+/)).toBeVisible();
  await page.screenshot({ path: `${SHOTS}/02-libraries.png` });

  await adminNav("Jobs").click();
  // preview batches and count refreshes are hidden unless asked for
  await page.getByLabel("Show preview batches and count refreshes").check();
  // scan -> generate + refresh_counts all finish
  await expect(async () => {
    const rows = page.getByTestId("job-row");
    expect(await rows.count()).toBeGreaterThanOrEqual(3);
    await expect(rows.filter({ hasText: /queued|running/ })).toHaveCount(0);
  }).toPass({ timeout: 90_000 });
  await expect(page.getByTestId("job-row").filter({ hasText: "failed" })).toHaveCount(0);
  await page.screenshot({ path: `${SHOTS}/03-jobs.png` });
});

test("scan just one folder of a library", async () => {
  await adminNav("Libraries").click();
  await page.getByRole("button", { name: "Scan folder…" }).click();
  const picker = page.getByRole("dialog", { name: "Scan a single folder" });
  await expect(picker.getByTestId("picker-path")).toHaveText("/media/lib");
  await expect(picker.getByRole("button", { name: "Up one level" })).toHaveCount(0); // can't leave the library
  await picker.getByRole("button", { name: "Open pics" }).click();
  await picker.getByRole("button", { name: "Scan this folder" }).click();
  await expect(page.getByText(/Queued job #\d+/)).toBeVisible();

  // the existing library is offered as "library" and can't be added again
  await page.getByRole("button", { name: "Browse…" }).click();
  const add = page.getByRole("dialog", { name: "Choose a library folder" });
  await expect(add.getByRole("button", { name: "Open lib" })).toContainText("library");
  await add.getByRole("button", { name: "Open lib" }).click();
  await expect(add.getByText("Already part of the library /media/lib")).toBeVisible();
  await expect(add.getByRole("button", { name: "Use this folder" })).toBeDisabled();
  await add.getByRole("button", { name: "Cancel" }).click();

  await adminNav("Jobs").click();
  const row = page.getByTestId("job-row").filter({ hasText: "/media/lib/pics" });
  await expect(row).toContainText("done");
  await expect(row).toContainText("no changes");
});

test("force-rebuild a library's previews", async () => {
  await adminNav("Libraries").click();
  page.once("dialog", (d) => d.accept());
  await page.getByRole("button", { name: "Rebuild previews", exact: true }).click();
  await expect(page.getByText(/Queued job #\d+/)).toBeVisible();
  await adminNav("Jobs").click();
  const row = page.getByTestId("job-row").filter({ hasText: "generate" }).first();
  await expect(row).toContainText("3 rebuilt", { timeout: 60_000 });
  await expect(row).toContainText("done");
});

test("scene cards all have the same height, whatever their titles", async () => {
  // Uneven rows made the virtualized grid jitter while scrolling.
  await nav("Scenes").click();
  const card = page.getByTestId("scene-card").first();
  const href = await card.getByRole("link").first().getAttribute("href");
  const headers = { "X-Requested-With": "e2e" };
  const before = await (await page.request.get(`/api${href}`)).json();
  await page.request.patch(`/api${href}`, { headers, data: { title: "A very long title that certainly wraps onto more than one line, and then some more words" } });
  await page.reload();
  await expect(page.getByTestId("scene-card")).toHaveCount(3);
  await expect(page.getByText("A very long title")).toBeVisible();
  const heights = await page.getByTestId("scene-card").evaluateAll((els) => els.map((e) => Math.round(e.getBoundingClientRect().height)));
  expect(heights.length).toBe(3);
  expect(new Set(heights).size).toBe(1);
  await page.request.patch(`/api${href}`, { headers, data: { title: before.title } });
});

test("scene grid shows indexed videos", async () => {
  await nav("Scenes").click();
  // Grouped-by-performer is the default; nothing is assigned yet, so it's one section.
  await expect(page.getByTestId("group")).toHaveCount(1);
  await expect(page.getByRole("heading", { name: "Unassigned" })).toBeVisible();
  await page.getByRole("button", { name: "Grid", exact: true }).click(); // remembered for the rest of the run
  await expect(page.getByTestId("scene-card")).toHaveCount(3);
  await expect(page.getByTestId("total")).toHaveText("3 scenes");
  await expect(page.getByTestId("scene-card").first().locator("img")).toHaveJSProperty("complete", true);
  // hover-scrub over the sprite sheet
  await page.getByTestId("scene-card").first().locator("img").hover({ position: { x: 150, y: 60 } });
  await page.screenshot({ path: `${SHOTS}/04-scenes.png` });

  await page.getByRole("combobox", { name: "Sort by" }).selectOption("title");
  await page.getByRole("button", { name: "Descending" }).click();
  await expect(page.getByTestId("scene-card").first()).toContainText("Beach Day");
  expect(page.url()).toContain("sort=title");
});

test("player: direct play, seeking, quality menu", async () => {
  await page.getByText("City Lights").click();
  const player = page.getByTestId("player");
  await expect(player).toBeVisible();
  const canPlayVp9 = await page.evaluate(() => document.createElement("video").canPlayType('video/webm; codecs="vp09.00.10.08, opus"'));
  test.skip(!canPlayVp9, "browser build lacks VP9");

  await player.getByRole("button", { name: "Play" }).first().click();
  await expect(async () => {
    const t = await page.locator("video").evaluate((v: HTMLVideoElement) => v.currentTime);
    expect(t).toBeGreaterThan(0.5);
  }).toPass();

  // click the middle of the seek bar -> ~10s of 20s
  const bar = page.getByTestId("seekbar");
  const box = (await bar.boundingBox())!;
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.screenshot({ path: `${SHOTS}/05-player-scrub.png` });
  await page.mouse.down();
  await page.mouse.up();
  await expect(page.getByTestId("time")).toContainText(/^0:1[0-2] \/ 0:20/);

  await page.getByTestId("quality").click();
  await expect(page.getByRole("menuitemradio", { name: /Original/ })).toBeChecked();
  await expect(page.getByRole("menuitemradio", { name: "360p" })).toBeVisible();
  const canH264 = await page.evaluate(() => document.createElement("video").canPlayType('video/mp4; codecs="avc1.640028, mp4a.40.2"'));
  if (canH264) {
    await page.getByRole("menuitemradio", { name: "360p" }).click();
    await expect(page.getByTestId("quality")).toHaveText("360p");
    await expect(async () => {
      expect(await page.locator("video").evaluate((v: HTMLVideoElement) => v.src)).toContain("mode=transcode");
      expect(await page.locator("video").evaluate((v: HTMLVideoElement) => v.currentTime)).toBeGreaterThan(0.3);
    }).toPass();
    // position was preserved across the switch (converted stream started at the offset)
    await expect(page.getByTestId("time")).toContainText(/^0:1\d \/ 0:20/);
  } else {
    await page.keyboard.press("Escape");
  }
  await page.locator("video").evaluate((v: HTMLVideoElement) => v.pause());
});

test("editor workflow: rate and tag a scene", async () => {
  await page.getByRole("button", { name: "Edit" }).click();
  const dialog = page.getByRole("dialog", { name: "Edit scene" });
  await dialog.getByLabel("tags").fill("Outdoor");
  await expect(dialog.getByRole("listbox")).toHaveAttribute("data-drop", "down");   // room below: opens downward
  await dialog.getByRole("option", { name: "Create “Outdoor”" }).click();
  await dialog.getByLabel("performers").fill("Alice");
  await dialog.getByRole("option", { name: "Create “Alice”" }).click();
  await dialog.getByRole("button", { name: "Save" }).click();
  await expect(dialog).toBeHidden();
  await expect(page.getByRole("link", { name: "Outdoor" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Alice" }).first()).toBeVisible();
  await page.getByLabel("Rating 0 of 5").getByRole("button").nth(3).click();
  await expect(page.getByLabel("Rating 4 of 5")).toBeVisible();
  await page.screenshot({ path: `${SHOTS}/06-scene-detail.png`, fullPage: true });
});

test("filters: tag filter via the filter panel", async () => {
  await nav("Scenes").click();
  await page.getByRole("button", { name: /^Filters/ }).click();
  await page.getByLabel("Add tags…").first().fill("Out");
  await page.getByRole("option", { name: "Outdoor" }).click();
  await expect(page.getByTestId("scene-card")).toHaveCount(1);
  await expect(page.getByTestId("scene-card")).toContainText("City Lights");
  await page.screenshot({ path: `${SHOTS}/07-filtered.png` });
  await page.getByRole("button", { name: "Clear" }).click();
  await expect(page.getByTestId("scene-card")).toHaveCount(3);
});

test("gallery lightbox navigation", async () => {
  await nav("Galleries").click();
  await expect(page.getByTestId("gallery-card")).toHaveCount(2); // folder + zip
  await page.getByTestId("gallery-card").filter({ hasText: "pics" }).getByRole("link").first().click();
  await expect(page.getByTestId("image-tile")).toHaveCount(12);
  await page.screenshot({ path: `${SHOTS}/08-gallery.png` });
  await page.getByTestId("image-tile").first().click();
  await expect(page.getByTestId("lightbox-file")).toContainText("/media/lib/");   // where the file lives
  const box = page.getByTestId("lightbox");
  await expect(box).toBeVisible();
  await expect(page.getByTestId("lightbox-counter")).toHaveText("1 / 12");
  await page.keyboard.press("ArrowRight");
  await expect(page.getByTestId("lightbox-counter")).toHaveText("2 / 12");
  await expect(box.locator("img").last()).toHaveJSProperty("complete", true);
  await page.screenshot({ path: `${SHOTS}/09-lightbox.png` });
  await page.keyboard.press("Escape");
  await expect(box).toBeHidden();
});

test("lightbox closes with Esc or a click outside; Esc leaves select mode", async () => {
  await nav("Images").click();
  await expect(page.getByLabel("Filter by performer…")).toBeVisible();
  const box = page.getByTestId("lightbox");
  const firstTile = page.getByTestId("image-tile").first();

  // Esc closes even when a field behind the viewer still has keyboard focus (Firefox and
  // Safari don't move focus to a clicked thumbnail, so e.g. the search box keeps it).
  await firstTile.click();
  await expect(box).toBeVisible();
  await page.getByLabel("Search").evaluate((el: HTMLElement) => el.focus());
  await page.keyboard.press("Escape");
  await expect(box).toBeHidden();

  // ...and when the picture is zoomed in
  await firstTile.click();
  await box.locator("img").last().click();
  await expect(box.locator("img.cursor-zoom-out")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(box).toBeHidden();

  // a click outside the picture closes; a click on the picture only zooms
  await firstTile.click();
  await box.locator("img").last().click();
  await expect(box).toBeVisible();
  await box.locator("img").last().click();                     // zoom back out
  await page.getByTestId("lightbox-backdrop").click({ position: { x: 80, y: 120 } });
  await expect(box).toBeHidden();

  // select mode on images: Esc clears the selection and leaves select mode
  await page.getByRole("button", { name: "Select images" }).click();
  await page.getByTestId("image-tile").nth(0).click();
  await page.getByTestId("image-tile").nth(1).click();
  const bulk = page.getByTestId("bulk-bar");
  await expect(bulk).toContainText("2 selected");
  await page.keyboard.press("Escape");
  await expect(bulk).toBeHidden();
  await expect(page.getByRole("button", { name: "Select images" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Done selecting" })).toHaveCount(0);

  // select mode on scenes; Esc while typing in a picker only leaves the field first
  await nav("Scenes").click();
  await page.getByRole("button", { name: "Select", exact: true }).click();
  await page.getByTestId("scene-card").first().click();
  await expect(bulk).toContainText("1 selected");
  await bulk.getByLabel("Tags…").fill("zz");
  await page.keyboard.press("Escape");
  await expect(bulk).toBeVisible();                            // still selecting
  await page.keyboard.press("Escape");
  await expect(bulk).toBeHidden();
  await expect(page.getByRole("button", { name: "Select", exact: true })).toBeVisible();
});

test("items per page: paged browsing instead of continuous scroll", async () => {
  await nav("Images").click();
  await expect(page.getByTestId("image-tile")).toHaveCount(15); // 12 loose + 3 in the zip
  await expect(page.getByTestId("pager")).toHaveCount(0);

  await page.getByLabel("Items per page").selectOption("10");
  await expect(page.getByTestId("image-tile")).toHaveCount(10);
  const pageBox = page.getByLabel("Page number").first();
  await expect(pageBox).toHaveValue("1");
  await expect(page.getByTestId("page-indicator").first()).toContainText("of 2");
  await expect(page.getByTestId("page-range").first()).toHaveText("1–10 of 15");
  await expect(page.getByRole("button", { name: "‹ Previous" }).first()).toBeDisabled();
  await page.screenshot({ path: `${SHOTS}/09a-paged.png` });

  await page.getByRole("button", { name: "Next ›" }).first().click();
  await expect(page.getByTestId("image-tile")).toHaveCount(5);
  await expect(pageBox).toHaveValue("2");
  await expect(page.getByTestId("page-range").first()).toHaveText("11–15 of 15");
  await expect(page.getByTestId("page-range")).toHaveCount(2);   // above and below the list
  await expect(page.getByRole("button", { name: "Next ›" }).first()).toBeDisabled();
  await page.getByRole("button", { name: "‹ Previous" }).first().click();
  await expect(page.getByTestId("image-tile")).toHaveCount(10);

  // straight to the last and first page, or to a typed page number
  await page.getByRole("button", { name: "Last page" }).first().click();
  await expect(pageBox).toHaveValue("2");
  await expect(page.getByTestId("image-tile")).toHaveCount(5);
  await expect(page.getByRole("button", { name: "Last page" }).first()).toBeDisabled();
  await page.getByRole("button", { name: "First page" }).first().click();
  await expect(pageBox).toHaveValue("1");
  await expect(page.getByTestId("image-tile")).toHaveCount(10);
  await pageBox.fill("2");
  await pageBox.press("Enter");
  await expect(page.getByTestId("image-tile")).toHaveCount(5);
  await pageBox.fill("99");                       // beyond the end: lands on the last page
  await pageBox.press("Enter");
  await expect(pageBox).toHaveValue("2");
  await pageBox.fill("1");
  await pageBox.press("Enter");
  await expect(page.getByTestId("image-tile")).toHaveCount(10);
  await expect(page.getByLabel("Items per page").locator("option")).toContainText(["500 per page", "1000 per page"]);

  // the lightbox steps through the current page
  await page.getByTestId("image-tile").first().click();
  await expect(page.getByTestId("lightbox-counter")).toHaveText("1 / 10");
  await page.keyboard.press("Escape");

  // "Select this page" takes just what is on screen, and adds up across pages
  await page.getByRole("button", { name: "Select images" }).first().click();
  await page.getByRole("button", { name: "Select this page" }).first().click();
  await expect(page.getByTestId("bulk-bar")).toContainText("10 selected");
  // moving to another page starts with nothing selected...
  await page.getByRole("button", { name: "Next ›" }).first().click();
  await expect(page.getByTestId("image-tile")).toHaveCount(5);
  await expect(page.getByTestId("bulk-bar")).toHaveCount(0);
  await page.getByRole("button", { name: "Select this page" }).first().click();
  await expect(page.getByTestId("bulk-bar")).toContainText("5 selected");
  // ...and "Deselect all" empties the selection while staying in select mode
  await page.getByRole("button", { name: "Deselect all" }).first().click();
  await expect(page.getByTestId("bulk-bar")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Done selecting" }).first()).toBeVisible();

  // with "keep my selection" switched on (account page), pages add up instead
  await page.goto("/account");
  const keep = page.getByTestId("selection-preferences").getByLabel("Keep my selection when I change pages");
  await keep.check();
  await expect(keep).toBeChecked();
  await page.screenshot({ path: `${SHOTS}/09f-selection-preference.png` });
  await nav("Images").click();
  await page.getByRole("button", { name: "Select images" }).first().click();
  await page.getByRole("button", { name: "Select this page" }).first().click();
  await expect(page.getByTestId("bulk-bar")).toContainText("10 selected");
  await page.getByRole("button", { name: "Next ›" }).first().click();
  await expect(page.getByTestId("image-tile")).toHaveCount(5);
  await page.getByRole("button", { name: "Select this page" }).first().click();
  await expect(page.getByTestId("bulk-bar")).toContainText("15 selected");
  // the same buttons sit below the page as well
  const below = page.getByTestId("select-bottom");
  await expect(below.getByRole("button", { name: "Select this page" })).toBeVisible();
  await expect(below.getByRole("button", { name: "Select all matching" })).toBeVisible();
  await below.getByRole("button", { name: "Done selecting" }).click();
  await expect(page.getByTestId("bulk-bar")).toHaveCount(0);
  await below.getByRole("button", { name: "Select images" }).click();
  await below.getByRole("button", { name: "Select this page" }).click();
  await expect(page.getByTestId("bulk-bar")).toContainText("5 selected");   // still on page 2
  // scrolled down, the site header, the toolbar and the selection buttons stay on screen
  await page.setViewportSize({ width: 1440, height: 420 });
  await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBeGreaterThan(100);
  await expect(page.getByRole("navigation", { name: "Main" })).toBeInViewport();
  await expect(page.getByTestId("toolbar")).toBeInViewport();
  await expect(page.getByTestId("select-top").getByRole("button", { name: "Done selecting" })).toBeInViewport();
  const headerBox = (await page.locator("header").boundingBox())!;
  const toolbarBox = (await page.getByTestId("toolbar").boundingBox())!;
  const selectBox = (await page.getByTestId("select-top").boundingBox())!;
  expect(toolbarBox.y).toBeGreaterThanOrEqual(headerBox.y + headerBox.height - 1);   // stacked, not overlapping
  expect(selectBox.y).toBeGreaterThanOrEqual(toolbarBox.y + toolbarBox.height - 1);
  await page.screenshot({ path: `${SHOTS}/09e-sticky-toolbar.png` });
  await page.setViewportSize({ width: 1440, height: 900 });

  // the selection bar never covers the bottom page navigation: scrolled to the end, the
  // pager sits fully above it
  await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
  const pagerBox = await page.getByTestId("pager").last().boundingBox();
  const barBox = await page.getByTestId("bulk-bar").boundingBox();
  expect(pagerBox!.y + pagerBox!.height).toBeLessThanOrEqual(barBox!.y);
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.keyboard.press("Escape");
  await expect(page.getByTestId("bulk-bar")).toHaveCount(0);

  // click and drag across the grid: everything the box touches is selected
  await page.getByRole("button", { name: "Select images" }).first().click();
  const tiles = page.getByTestId("image-tile");
  const a = (await tiles.nth(0).boundingBox())!;
  const b = (await tiles.nth(2).boundingBox())!;      // same row: the first three
  await page.mouse.move(a.x + 10, a.y + 10);
  await page.mouse.down();
  await page.mouse.move(b.x + b.width / 2, b.y + b.height / 2, { steps: 8 });
  await expect(page.getByTestId("marquee")).toBeVisible();
  await page.screenshot({ path: `${SHOTS}/09c-drag-select.png` });
  await page.mouse.up();
  await expect(page.getByTestId("marquee")).toHaveCount(0);
  await expect(page.getByTestId("bulk-bar")).toContainText("3 selected");
  for (const n of [0, 1, 2]) await expect(tiles.nth(n)).toHaveAttribute("aria-pressed", "true");
  await expect(tiles.nth(3)).toHaveAttribute("aria-pressed", "false");
  // a plain click still toggles one
  await tiles.nth(3).click();
  await expect(page.getByTestId("bulk-bar")).toContainText("4 selected");
  await page.keyboard.press("Escape");

  // the bulk bar shows what a selection already has, and can remove it
  await page.getByRole("button", { name: "Select images" }).first().click();
  await page.getByRole("button", { name: "Select this page" }).first().click();
  const bar = page.getByTestId("bulk-bar");
  await bar.getByLabel("Tags…").pressSequentially("Paged Tag");
  await bar.getByRole("option", { name: /Create/ }).click();
  await bar.getByRole("button", { name: "Add tags" }).click();
  await expect(bar.getByTestId("assigned-tags")).toContainText("Paged Tag");
  await expect(bar.getByTestId("assigned-tags")).toContainText("all");
  await page.getByTestId("image-tile").first().click();          // deselect one
  await expect(bar).toContainText("4 selected");
  await page.screenshot({ path: `${SHOTS}/09b-assigned.png` });
  await bar.getByRole("button", { name: "Remove Paged Tag from the selection" }).click();
  await expect(bar.getByTestId("assigned-tags")).toHaveCount(0);
  await page.getByTestId("image-tile").first().click();          // the deselected one kept its tag
  await expect(bar.getByTestId("assigned-tags")).toContainText("1/5");
  await bar.getByRole("button", { name: "Remove Paged Tag from the selection" }).click();
  await expect(bar.getByTestId("assigned-tags")).toHaveCount(0);

  // a studio, the same way: set it on the selection, see it, take it off again
  await bar.getByLabel("Studio…").pressSequentially("Bulk Studio");
  await bar.getByRole("option", { name: /Create/ }).click();
  // the picker and its buttons stay together on one line
  const studioGroup = bar.getByRole("group", { name: "Studio" });
  const pickerBox = (await studioGroup.getByLabel("Studio…").boundingBox())!;
  const setBox = (await studioGroup.getByRole("button", { name: "Set studio" }).boundingBox())!;
  expect(Math.abs(pickerBox.y + pickerBox.height - (setBox.y + setBox.height))).toBeLessThan(12);
  await page.screenshot({ path: `${SHOTS}/09d-bulk-studio.png` });
  await bar.getByRole("button", { name: "Set studio" }).click();
  await expect(bar.getByTestId("assigned-studios")).toContainText("Bulk Studio");
  await expect(bar.getByTestId("assigned-studios")).toContainText("all");
  await bar.getByRole("button", { name: "Remove Bulk Studio from the selection" }).click();
  await expect(bar.getByTestId("assigned-studios")).toHaveCount(0);
  await page.keyboard.press("Escape");

  // the choice is remembered, and applies to other lists too
  await page.reload();
  await expect(page.getByLabel("Items per page")).toHaveValue("10");
  await expect(page.getByTestId("image-tile")).toHaveCount(10);
  await nav("Scenes").click();
  await expect(page.getByLabel("Items per page")).toHaveValue("10");
  await expect(page.getByTestId("scene-card")).toHaveCount(3); // fewer than a page: no pager
  await expect(page.getByTestId("pager")).toHaveCount(0);

  await page.getByLabel("Items per page").selectOption("");   // back to continuous scrolling
  await nav("Images").click();
  await expect(page.getByTestId("image-tile")).toHaveCount(15);
});

test("on a performer's page, search stays within that performer and matches names", async () => {
  await nav("Performers").click();
  await page.getByTestId("performer-card").filter({ hasText: "Alice" }).click();
  const search = page.getByPlaceholder("Search in this performer…");
  await expect(search).toBeVisible();
  await expect(page.getByTestId("scene-card").first()).toBeVisible();
  const all = await page.getByTestId("scene-card").count();
  await search.fill("alice");                       // her own name: all of her scenes, nothing else
  await expect(page.getByTestId("scene-card")).toHaveCount(all);
  await search.fill("zz-nothing-matches");
  await expect(page.getByTestId("scene-card")).toHaveCount(0);
  await search.fill("");
  await page.getByRole("tab", { name: "Images" }).click();
  await expect(page.getByPlaceholder("Search in this performer…")).toBeVisible();
  await page.getByRole("tab", { name: "Scenes" }).click();
});

test("performer page lists their scenes", async () => {
  await nav("Performers").click();
  await page.getByTestId("performer-card").filter({ hasText: "Alice" }).click();
  await expect(page.getByRole("heading", { name: "Alice" })).toBeVisible();
  await expect(page.getByRole("tab", { name: "Scenes" })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByTestId("scene-card")).toHaveCount(1);
  await page.screenshot({ path: `${SHOTS}/10-performer.png` });
});

test("assign performers to images (bulk + lightbox) and to a scene (inline)", async () => {
  // bulk: select two images of a gallery and add a performer
  await nav("Galleries").click();
  await page.getByTestId("gallery-card").filter({ hasText: "pics" }).getByRole("link").first().click();
  await page.getByRole("button", { name: "Select images" }).click();
  await page.getByTestId("image-tile").nth(0).click();
  await page.getByTestId("image-tile").nth(1).click();
  const bulk = page.getByTestId("bulk-bar");
  await expect(bulk).toContainText("2 selected");
  // Regression: in the bar pinned to the bottom of the window the suggestion list opened
  // downward, off-screen. Check at a common laptop size that it opens upward and is visible.
  await page.setViewportSize({ width: 1366, height: 768 });
  await bulk.getByLabel("Performers…").fill("Ali");
  const suggestion = page.getByRole("option", { name: "Alice" });
  await expect(suggestion).toBeInViewport({ ratio: 1 });
  await expect(bulk.getByRole("listbox")).toHaveAttribute("data-drop", "up");
  // Geometry, independent of how many suggestions there are: the whole list sits above the
  // input (with one suggestion a downward list would still happen to fit on screen).
  const listBox = (await bulk.getByRole("listbox").boundingBox())!;
  const inputBox = (await bulk.getByLabel("Performers…").boundingBox())!;
  expect(listBox.y + listBox.height).toBeLessThanOrEqual(inputBox.y);
  expect(listBox.y).toBeGreaterThanOrEqual(0);
  await page.screenshot({ path: `${SHOTS}/09b0-bulk-picker-open.png` });
  await suggestion.click();
  await bulk.getByLabel("Tags…").fill("Out");
  await expect(page.getByRole("option", { name: "Outdoor" })).toBeInViewport({ ratio: 1 });
  await bulk.getByLabel("Tags…").fill("");
  await page.keyboard.press("Escape");
  await page.setViewportSize({ width: 1440, height: 900 });
  await bulk.getByRole("button", { name: "Add performers" }).click();
  await expect(bulk.getByRole("button", { name: "Add performers" })).toBeDisabled(); // applied, picker reset
  await page.screenshot({ path: `${SHOTS}/09b-bulk-images.png` });
  await page.getByRole("button", { name: "Done selecting" }).click();

  // lightbox: shows who is in the image, and can edit it
  await page.getByTestId("image-tile").first().click();
  await expect(page.getByTestId("lightbox-meta")).toContainText("Alice");
  await page.getByTestId("lightbox").getByRole("button", { name: "Edit" }).click();
  const dialog = page.getByRole("dialog", { name: "Edit image" });
  // make this image Alice's picture, straight from the image's edit dialog
  await dialog.getByRole("button", { name: "Set as Alice's picture" }).click();
  await expect(dialog.getByRole("button", { name: "✓ Alice's picture" })).toBeVisible();
  const alice = await page.getByTestId("lightbox-meta").getByRole("link", { name: "Alice" }).getAttribute("href");
  const pic = await page.request.get(`/api${alice}/image`);
  expect(pic.ok()).toBeTruthy();
  expect(pic.headers()["content-type"]).toBe("image/webp");
  // (a later test starts Alice without a picture)
  expect((await page.request.delete(`/api${alice}/image`, { headers: { "X-Requested-With": "e2e" } })).ok()).toBeTruthy();
  await dialog.getByLabel("Add tags…").pressSequentially("Portrait", { delay: 15 });
  await dialog.getByRole("option", { name: "Create “Portrait”" }).click();
  await expect(dialog.getByRole("button", { name: "Remove Portrait" })).toBeVisible();   // created and added
  await page.keyboard.press("ArrowRight"); // typing keys must not flip to the next image
  await dialog.getByRole("button", { name: "Save" }).click();
  await expect(dialog).toBeHidden();
  await expect(page.getByTestId("lightbox-counter")).toHaveText("1 / 12");
  await expect(page.getByTestId("lightbox-meta")).toContainText("Portrait");
  await page.screenshot({ path: `${SHOTS}/09c-lightbox-meta.png` });
  await page.keyboard.press("Escape");
  await expect(page.getByTestId("lightbox")).toBeHidden();

  // scene page: assign a performer right there, no dialog
  await nav("Scenes").click();
  await page.getByText("Night Drive").click();
  const quick = page.getByTestId("quick-assign");
  await quick.getByLabel("Add performers…").fill("Ali");
  await page.getByRole("option", { name: "Alice" }).click();
  await expect(page.getByRole("link", { name: "Alice" }).first()).toBeVisible(); // saved and shown
  await page.screenshot({ path: `${SHOTS}/09d-scene-assign.png` });

  // the performer's page reflects both
  await nav("Performers").click();
  await page.getByTestId("performer-card").filter({ hasText: "Alice" }).click();
  await expect(page.getByTestId("scene-card")).toHaveCount(2);
  await page.getByRole("tab", { name: "Images" }).click();
  await expect(page.getByTestId("image-tile")).toHaveCount(2);
});

test("scenes and images grouped by performer", async () => {
  await nav("Scenes").click();
  await page.getByRole("button", { name: "By performer" }).click();
  const alice = page.getByRole("region", { name: "Alice" });
  const rest = page.getByRole("region", { name: "Unassigned" });
  await expect(page.getByTestId("group")).toHaveCount(2);              // Alice first, then Unassigned
  await expect(alice.getByTestId("group-count")).toHaveText("2 scenes");
  await expect(alice.getByTestId("scene-card")).toHaveCount(2);
  await expect(rest.getByTestId("group-count")).toHaveText("1 scene");
  await expect(rest.getByTestId("scene-card")).toContainText("Beach Day");
  await page.screenshot({ path: `${SHOTS}/09g-grouped-scenes.png` });

  // a performer's section opens as the flat grid filtered to them
  await alice.getByRole("link", { name: /Open as grid|See all/ }).click();
  await expect(page).toHaveURL(/performers=\d+/);
  await expect(page.getByTestId("scene-card")).toHaveCount(2);
  await expect(page.getByTestId("total")).toHaveText("2 scenes");

  // images: 2 are Alice's, 13 unassigned (12 shown + "See all")
  await nav("Images").click();
  const aliceImgs = page.getByRole("region", { name: "Alice" });
  const restImgs = page.getByRole("region", { name: "Unassigned" });
  await expect(aliceImgs.getByTestId("group-count")).toHaveText("2 images");
  await expect(restImgs.getByTestId("group-count")).toHaveText("13 images");
  await expect(restImgs.getByTestId("image-tile")).toHaveCount(12);
  await page.screenshot({ path: `${SHOTS}/09h-grouped-images.png` });
  await aliceImgs.getByTestId("image-tile").first().click();
  await expect(page.getByTestId("lightbox-counter")).toHaveText("1 / 2");  // steps through that group
  await page.keyboard.press("Escape");
  await restImgs.getByRole("link", { name: "See all 13 →" }).click();
  await expect(page).toHaveURL(/unassigned=1/);
  await expect(page.getByTestId("image-tile")).toHaveCount(13);

  // back to the flat grid for the rest of the run
  await nav("Scenes").click();
  await page.getByRole("button", { name: "Grid", exact: true }).click();
  await expect(page.getByTestId("scene-card")).toHaveCount(3);
});

test("search by folder, select all matching, assign a performer in bulk", async () => {
  // scenes: the search box matches folder names as well as titles
  await nav("Scenes").click();
  await page.getByLabel("Search").fill("lib/videos");
  await expect(page.getByTestId("scene-card")).toHaveCount(3);
  await page.getByLabel("Search").fill("no-such-folder");
  await expect(page.getByText("No scenes match.")).toBeVisible();
  await page.getByLabel("Search").fill("");

  // images: narrow to one folder, select everything in it, assign a performer
  await nav("Images").click();
  await expect(page.getByLabel("Filter by performer…")).toBeVisible();      // the Images page is up
  await page.getByLabel("Search").fill("lib/pics");
  await expect(page.getByTestId("image-tile")).toHaveCount(12);           // the zip's 3 images are elsewhere
  await page.getByRole("button", { name: "Select images" }).click();
  await page.getByRole("button", { name: "Select all matching" }).click();
  const bulk = page.getByTestId("bulk-bar");
  await expect(bulk).toContainText("12 selected");
  await bulk.getByLabel("Performers…").fill("Brad");
  await page.getByRole("option", { name: "Create “Brad”" }).click();
  await bulk.getByRole("button", { name: "Add performers" }).click();
  await expect(bulk.getByText("Done")).toBeVisible();
  await page.screenshot({ path: `${SHOTS}/09i-select-all.png` });
  await page.getByRole("button", { name: "Done selecting" }).click();

  await nav("Performers").click();
  await page.getByTestId("performer-card").filter({ hasText: "Brad" }).click();
  await page.getByRole("tab", { name: "Images" }).click();
  await expect(page.getByTestId("image-tile")).toHaveCount(12);
});

test("performer image: from the library, from server files, by upload; unsafe URLs refused", async () => {
  await nav("Performers").click();
  await page.getByTestId("performer-card").filter({ hasText: "Alice" }).click();
  const portrait = page.locator("img[src*='/api/performers/']").first();
  await expect(portrait).toHaveCount(0);

  // from the library
  await page.getByRole("button", { name: "Add image" }).click();
  const dialog = page.getByRole("dialog", { name: "Choose an image" });
  await dialog.getByRole("tab", { name: "From library" }).click();
  await expect(dialog.getByTestId("source-item").first()).toBeVisible();
  await page.screenshot({ path: `${SHOTS}/09e-image-from-library.png` });
  await dialog.getByTestId("source-item").first().click();
  // after choosing, the picture can be positioned; here it is kept whole
  const adjust = page.getByRole("dialog", { name: "Adjust the picture" });
  await expect(adjust.getByTestId("crop-frame")).toBeVisible();
  await adjust.getByRole("button", { name: "Keep the whole picture" }).click();
  await expect(dialog).toBeHidden();
  await expect(portrait).toBeVisible();
  const first = await portrait.getAttribute("src");

  // from the read-only media store (admin)
  await page.getByRole("button", { name: "Change image" }).click();
  await dialog.getByRole("tab", { name: "Server files" }).click();
  await dialog.getByRole("button", { name: "Open lib" }).click();
  await dialog.getByRole("button", { name: "Open pics" }).click();
  await expect(dialog.getByTestId("server-path")).toHaveText("/media/lib/pics");
  await expect(dialog.getByTestId("server-file")).toHaveCount(12);
  await page.screenshot({ path: `${SHOTS}/09f-image-from-server.png` });
  await dialog.getByTestId("server-file").nth(5).click();
  await adjust.getByRole("button", { name: "Keep the whole picture" }).click();
  await expect(dialog).toBeHidden();
  await expect(portrait).not.toHaveAttribute("src", first!);

  // from a URL: internal addresses are refused with a clear message
  await page.getByRole("button", { name: "Change image" }).click();
  await dialog.getByRole("tab", { name: "From URL" }).click();
  await dialog.getByLabel("Image address").fill("http://127.0.0.1:8000/api/health");
  await dialog.getByRole("button", { name: "Fetch image" }).click();
  await expect(dialog.getByRole("alert")).toContainText("not on the public internet");

  // by upload
  await dialog.getByRole("tab", { name: "Upload" }).click();
  await dialog.getByTestId("image-upload").setInputFiles("e2e/media/lib/pics/photo3.jpg");
  // this time: zoom in, drag, save
  const frame = adjust.getByTestId("crop-frame");
  await expect(frame.locator("img")).toBeVisible();
  const before = await portrait.getAttribute("src");
  await adjust.getByLabel("Zoom").fill("2.5");
  const box = (await frame.boundingBox())!;
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width / 2 + 60, box.y + box.height / 2 + 30, { steps: 5 });
  await page.mouse.up();
  await page.screenshot({ path: `${SHOTS}/09h-crop.png` });
  await adjust.getByRole("button", { name: "Save" }).click();
  await expect(adjust).toBeHidden();
  await expect(dialog).toBeHidden();
  await expect(portrait).not.toHaveAttribute("src", before!);
  const shown = await portrait.evaluate((i: HTMLImageElement) => i.naturalWidth / i.naturalHeight);
  expect(Math.abs(shown - 2 / 3)).toBeLessThan(0.02);              // cut to the card's shape
  // and it can be adjusted again later from the performer page
  const cropped = await portrait.getAttribute("src");
  await page.getByRole("button", { name: "Adjust crop" }).click();
  await expect(adjust.getByTestId("crop-frame")).toBeVisible();
  await adjust.getByRole("button", { name: "Use the whole picture" }).click();   // undo the crop
  await expect(adjust).toBeHidden();
  await expect(portrait).not.toHaveAttribute("src", cropped!);
  await expect.poll(() => portrait.evaluate((i: HTMLImageElement) => i.naturalWidth / i.naturalHeight)).toBeGreaterThan(1);
  await expect(portrait).toBeVisible();

  // random: one of the images assigned to her; "pick another" always gives a different one
  const uploaded = await portrait.getAttribute("src");
  await page.getByRole("button", { name: "Change image" }).click();
  await dialog.getByRole("button", { name: "Random" }).click();
  const pick = dialog.getByTestId("random-pick");
  await expect(pick).toBeVisible();
  await expect(dialog).toContainText("Saved. Not the one? Pick another.");
  const firstPick = await pick.getAttribute("data-image-id");
  await page.screenshot({ path: `${SHOTS}/09g-image-random.png` });
  await dialog.getByRole("button", { name: "Pick another" }).click();
  await expect(pick).not.toHaveAttribute("data-image-id", firstPick!);
  await dialog.getByRole("button", { name: "Done" }).click();
  await expect(dialog).toBeHidden();
  await expect(portrait).not.toHaveAttribute("src", uploaded!);

  // shown on the performer's card in the list too
  await nav("Performers").click();
  await expect(page.getByTestId("performer-card").filter({ hasText: "Alice" }).locator("img")).toBeVisible();
});

test("dialogs keep focus while typing (create performer, studio, tag)", async () => {
  // Regression: the modal re-ran its focus logic on every keystroke and moved focus to
  // the close button. fill() sets values in one go, so type key by key like a person.
  for (const [navName, button, name] of [
    ["Performers", "New performer", "Charlie Typed"],
    ["Studios", "New studio", "Typed Studio"],
    ["Tags", "New tag", "Typed Tag"],
  ] as const) {
    await nav(navName).click();
    await page.getByRole("button", { name: button }).click();
    const dialog = page.getByRole("dialog", { name: button });
    const input = dialog.getByLabel("Name");
    await expect(input).toBeFocused();
    await input.pressSequentially(name, { delay: 20 });
    await expect(input).toBeFocused();
    await expect(input).toHaveValue(name);
    await page.keyboard.press("Enter");
    await expect(page.getByRole("heading", { name })).toBeVisible();
  }
});

test("edit dialog keeps focus while typing", async () => {
  await nav("Scenes").click();
  await page.getByText("Beach Day").click();
  await page.getByRole("button", { name: "Edit" }).click();
  const dialog = page.getByRole("dialog", { name: "Edit scene" });
  const title = dialog.getByLabel("Title");
  await expect(title).toBeFocused();
  await title.press("End");
  await title.pressSequentially(" Typed", { delay: 20 });
  await expect(title).toBeFocused();
  const details = dialog.getByLabel("Details");
  await details.click();
  await details.pressSequentially("Some notes", { delay: 20 });
  await expect(details).toBeFocused();
  await dialog.getByRole("button", { name: "Save" }).click();
  await expect(page.getByTestId("scene-title")).toHaveText("Beach Day Typed");
  await expect(page.getByText("Some notes")).toBeVisible();
});

test("home page: sections, per-user favourites, customize and reorder", async () => {
  await nav("Home").click();
  const section = (name: string) => page.getByRole("region", { name });
  const headings = () => page.getByTestId("home-section").locator("h2").allTextContents();
  await expect(section("Recently added scenes").getByTestId("scene-card")).toHaveCount(3);
  await expect(section("Continue watching").getByTestId("scene-card")).toHaveCount(1);   // City Lights was paused midway
  await expect(section("Recently added galleries").getByTestId("gallery-card")).toHaveCount(2);
  await expect(section("Favourite performers")).toHaveCount(0);                          // empty sections are hidden
  expect(await headings()).toEqual(["Continue watching", "Recently added scenes", "Recently added galleries"]);

  // favourites are personal: mark Alice, and she appears on my home page
  await nav("Performers").click();
  await page.getByTestId("performer-card").filter({ hasText: "Alice" }).click();
  await page.getByRole("button", { name: "Add to my favourites" }).click();
  await expect(page.getByRole("button", { name: "Remove from my favourites" })).toBeVisible();
  await nav("Home").click();
  await expect(section("Favourite performers").getByTestId("performer-card")).toContainText("Alice");
  await page.screenshot({ path: `${SHOTS}/10a-home.png`, fullPage: true });

  // customize: add Studios, move it to the top, drop the galleries, rename a heading
  await page.getByRole("button", { name: "Customize" }).click();
  const editor = page.getByTestId("home-editor");
  await editor.getByLabel("Section to add").selectOption("studios");
  await editor.getByRole("button", { name: "Add", exact: true }).click();
  await expect(editor.getByTestId("home-row")).toHaveCount(5);
  for (let i = 0; i < 4; i++) await editor.getByRole("button", { name: "Move Studios up" }).click();
  await editor.getByRole("button", { name: "Remove Recently added galleries" }).click();
  await editor.getByTestId("home-row").filter({ hasText: "Recently added scenes" }).getByLabel("Custom heading").fill("Fresh");
  await page.screenshot({ path: `${SHOTS}/10b-home-customize.png` });
  await editor.getByRole("button", { name: "Save" }).click();
  await expect(editor).toBeHidden();
  await expect.poll(headings).toEqual(["Studios", "Continue watching", "Fresh", "Favourite performers"]);

  await page.reload();                                                                    // stored on the server
  await expect.poll(headings).toEqual(["Studios", "Continue watching", "Fresh", "Favourite performers"]);

  await page.getByRole("button", { name: "Customize" }).click();
  await editor.getByRole("button", { name: "Reset to default" }).click();
  await expect.poll(headings).toEqual(["Continue watching", "Recently added scenes", "Favourite performers", "Recently added galleries"]);
});

test("admin restricts a viewer, who then can't see the content", async () => {
  await page.goto("/admin/users");
  await page.getByRole("button", { name: "New user" }).click();
  const dialog = page.getByRole("dialog", { name: "New user" });
  await dialog.getByLabel("Username").fill("kid");
  await dialog.getByLabel("Password (8+ characters)").fill("kid-password");
  await dialog.getByRole("button", { name: "Create" }).click();
  await expect(page.getByRole("heading", { name: "kid" })).toBeVisible();
  await page.getByLabel("Add tags…").fill("Outdoor");
  await page.getByRole("option", { name: "Outdoor" }).click();
  await page.getByRole("button", { name: "Save restrictions" }).click();
  await expect(page.getByText("Saved")).toBeVisible();
  await page.screenshot({ path: `${SHOTS}/11-user-restrictions.png` });

  await page.getByRole("button", { name: `${ADMIN.username} ▾` }).click();
  await page.getByRole("menuitem", { name: "Log out" }).click();
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();

  await login("kid", "kid-password");
  await expect(page.getByTestId("scene-card")).toHaveCount(2);
  await expect(page.getByText("City Lights")).toHaveCount(0);
  await expect(nav("Admin")).toHaveCount(0);
  const hidden = await page.request.get("/api/scenes?q=City");
  expect((await hidden.json()).items).toHaveLength(0);
  await page.goto("/tags");
  await expect(page.getByTestId("tag-card")).toHaveCount(0);
});

test("wrong password is rejected", async () => {
  await page.getByRole("button", { name: "kid ▾" }).click();
  await page.getByRole("menuitem", { name: "Log out" }).click();
  await page.getByLabel("Username").fill("kid");
  await page.getByLabel("Password").fill("not-the-password");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("alert")).toHaveText("invalid username or password");
});

// --- user libraries -------------------------------------------------------------------

async function logout(username: string) {
  await page.getByRole("button", { name: `${username} ▾` }).click();
  await page.getByRole("menuitem", { name: "Log out" }).click();
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
}

test("admin enables uploads for a user", async () => {
  await login(ADMIN.username, ADMIN.password);
  await expect(nav("My uploads")).toHaveCount(0); // not enabled for the admin's own account
  await page.goto("/admin/users");
  await page.getByRole("button", { name: "New user" }).click();
  const dialog = page.getByRole("dialog", { name: "New user" });
  await dialog.getByLabel("Username").fill("uma");
  await dialog.getByLabel("Password (8+ characters)").fill("uma-password");
  await dialog.getByRole("button", { name: "Create" }).click();
  await expect(page.getByRole("heading", { name: "uma" })).toBeVisible();
  // the switch reflects the saved state, so it flips once the server has confirmed
  await page.getByLabel("May create own libraries and upload").click();
  await expect(page.getByLabel("May create own libraries and upload")).toBeChecked();
  await page.getByLabel("Storage quota in GB").fill("1");
  await page.getByRole("button", { name: "Set", exact: true }).last().click();
  await expect(page.getByRole("button", { name: "Set", exact: true }).last()).toBeDisabled(); // saved
  await page.screenshot({ path: `${SHOTS}/12-user-upload-settings.png` });
  await logout(ADMIN.username);
});

test("user creates a private library and uploads a video", async () => {
  await login("uma", "uma-password");
  await nav("My uploads").click();
  await expect(page.getByTestId("quota")).toContainText("of 1.0 GB");
  await page.getByRole("button", { name: "New library" }).click();
  const dialog = page.getByRole("dialog", { name: "New library" });
  await dialog.getByLabel("Name").pressSequentially("Uma's clips", { delay: 10 });
  await expect(dialog.getByText("including administrators")).toBeVisible();
  await dialog.getByRole("button", { name: "Create" }).click();
  await expect(page.getByRole("heading", { name: "Uma's clips" })).toBeVisible();

  await page.getByTestId("file-input").setInputFiles(["e2e/media/lib/videos/City Lights.webm", "package.json"]);
  const items = page.getByTestId("upload-item");
  await expect(items).toHaveCount(2);
  await expect(items.filter({ hasText: "City Lights.webm" })).toHaveAttribute("data-status", "done");
  await expect(items.filter({ hasText: "package.json" })).toHaveAttribute("data-status", "skipped"); // not media
  // indexed in the background, then shown
  await expect(page.getByTestId("scene-card")).toHaveCount(1, { timeout: 40_000 });
  await page.screenshot({ path: `${SHOTS}/13-my-library.png` });
  // (this stack runs with encryption of uploads on: the file on disk is encrypted, the
  // browser gets the real thing)
  const stream = await page.request.get(`/api${await page.getByTestId("scene-card").getByRole("link").first().getAttribute("href")}/stream`, { headers: { Range: "bytes=0-3" } });
  expect(stream.status()).toBe(206);
  expect((await stream.body()).toString("latin1")).toBe("\x1aE\xdf\xa3");   // the WebM signature

  await page.getByTestId("scene-card").getByRole("link").first().click();
  await expect(page.getByTestId("scene-title")).toHaveText("City Lights");
  await expect(page.getByText("Your upload")).toBeVisible();
  await expect(page.getByRole("button", { name: "Edit" })).toBeVisible(); // a viewer, but the owner
  await logout("uma");
});

test("a private library is invisible to other users and to the admin", async () => {
  await login("kid", "kid-password");
  await expect(page.getByTestId("scene-card")).toHaveCount(2);
  await expect(nav("My uploads")).toHaveCount(0);
  await logout("kid");

  await login(ADMIN.username, ADMIN.password);
  await expect(page.getByTestId("total")).toHaveText("3 scenes"); // unchanged: no fourth scene
  await page.goto("/admin/libraries");
  const row = page.getByTestId("user-library");
  await expect(row).toContainText("uma");
  await expect(row).toContainText("Private");
  await expect(row).toContainText("1 files");
  await page.screenshot({ path: `${SHOTS}/14-admin-user-libraries.png` });
  await logout(ADMIN.username);
});

test("owner shares the library with one user", async () => {
  await login("uma", "uma-password");
  await nav("My uploads").click();
  await page.getByTestId("my-library").click();
  await page.getByRole("button", { name: "Sharing & settings" }).click();
  const dialog = page.getByRole("dialog", { name: "Library settings" });
  await dialog.getByLabel("Who can see it").selectOption("users");
  await dialog.getByLabel("kid").check();
  await page.screenshot({ path: `${SHOTS}/15-sharing.png` });
  await dialog.getByRole("button", { name: "Save" }).click();
  await expect(page.getByText("Shared with 1")).toBeVisible();
  await logout("uma");

  await login("kid", "kid-password");
  await expect(page.getByTestId("scene-card")).toHaveCount(3);
  await page.getByText("City Lights").click();
  await expect(page.getByTestId("player")).toBeVisible();
  await expect(page.getByRole("button", { name: "Edit" })).toHaveCount(0); // shared, not editable
  await logout("kid");

  await login(ADMIN.username, ADMIN.password); // still not shared with the admin
  await expect(page.getByTestId("total")).toHaveText("3 scenes");
});

test("an uploader deletes their own file from a selection", async () => {
  await logout(ADMIN.username);   // (the previous test ends signed in as the admin)
  await login("uma", "uma-password");
  await nav("My uploads").click();
  await page.getByTestId("my-library").click();
  await expect(page.getByTestId("scene-card")).toHaveCount(1);
  await page.getByRole("button", { name: "Select", exact: true }).first().click();
  await page.getByTestId("scene-card").first().click();
  page.once("dialog", (d) => d.accept());
  await page.getByTestId("bulk-bar").getByRole("button", { name: "Delete" }).click();
  await expect(page.getByTestId("scene-card")).toHaveCount(0);
  await page.getByRole("tab", { name: /Files/ }).click();
  await expect(page.getByTestId("library-file")).toHaveCount(0);
  await logout("uma");
  await login(ADMIN.username, ADMIN.password);   // later tests continue as the admin
});

test("import from Stash: review, approve selectively, reject, studio pictures by name", async () => {
  await page.goto("/admin/libraries");
  await adminNav("Import").click();
  await expect(page.getByRole("heading", { name: "Import from Stash" })).toBeVisible();

  // something that isn't a Stash database is refused and changes nothing
  await page.getByLabel("Stash database file").setInputFiles("package.json");
  await expect(page.getByRole("alert")).toContainText("not an SQLite database");

  const read = async () => {
    await page.getByLabel("Path on the server").fill("/media/stash/stash-go.sqlite");
    await page.getByRole("button", { name: "Read", exact: true }).click();
    await expect(page.getByTestId("stash-review")).toContainText("schema 85");
  };

  // rejecting an import adds nothing
  await read();
  page.once("dialog", (d) => d.accept());
  await page.getByRole("button", { name: "Reject import" }).click();
  await expect(page.getByLabel("Path on the server")).toBeVisible();
  await page.goto("/tags");
  await expect(page.getByText("Stash Setting")).toHaveCount(0);

  // tags: approve two of the three
  await page.goto("/admin/import");
  await read();
  const review = page.getByTestId("stash-review");
  await expect(review.getByRole("tab", { name: "Tags (3 to review)" })).toBeVisible();
  await expect(page.getByTestId("stash-item")).toHaveCount(3);
  await expect(page.getByTestId("stash-item").filter({ hasText: "Stash Rooftop" })).toContainText("under Stash Setting");
  await page.getByLabel("Stash Setting", { exact: true }).check();
  await page.getByLabel("Stash Rooftop", { exact: true }).check();
  await page.screenshot({ path: `${SHOTS}/16-stash-review.png` });
  await page.getByRole("button", { name: "Import selected (2)" }).click();
  await expect(page.getByRole("status")).toContainText("Imported 2 tags");
  await expect(page.getByTestId("stash-counts")).toContainText("2 imported · 0 already here · 1 to review");
  await expect(page.getByTestId("stash-item").filter({ hasText: "Stash Rooftop" })).toContainText("Imported");
  await expect(page.getByLabel("Stash Unused", { exact: true })).toBeEnabled();

  // performers: everything
  await review.getByRole("tab", { name: /Performers/ }).click();
  await expect(page.getByTestId("stash-item")).toHaveCount(2);
  page.once("dialog", (d) => d.accept());
  await page.getByRole("button", { name: "Import all 2" }).click();
  await expect(page.getByRole("status")).toContainText("Imported 2 performers");

  // studios: everything, then finish (one tag was deliberately left out)
  await review.getByRole("tab", { name: /Studios/ }).click();
  await expect(page.getByTestId("stash-item").filter({ hasText: "Velvet Shorts" })).toContainText("part of Velvet Works");
  page.once("dialog", (d) => d.accept());
  await page.getByRole("button", { name: "Import all 2" }).click();
  await expect(page.getByRole("status")).toContainText("Imported 2 studios");
  page.once("dialog", (d) => d.accept());
  await page.getByRole("button", { name: "Finish" }).click();
  await expect(page.getByLabel("Path on the server")).toBeVisible();

  // studio pictures, matched by file name ("velvet works.jpg" -> Velvet Works)
  await page.getByLabel("Picture folder").fill("/media/_studio-images");
  await page.getByRole("button", { name: "Assign pictures" }).click();
  await expect(page.getByTestId("images-result")).toContainText("Assigned 1 of 1 pictures");
  await page.screenshot({ path: `${SHOTS}/17-stash-done.png` });

  // the result is ordinary metadata
  await page.goto("/tags");
  await expect(page.getByText("Stash Rooftop").first()).toBeVisible();
  await expect(page.getByText("Stash Unused")).toHaveCount(0);
  await page.goto("/studios");
  await expect(page.getByText("Velvet Shorts").first()).toBeVisible();
  await expect(page.locator('img[src*="/studios/"]')).toHaveCount(1);
  await page.goto("/performers");
  await page.getByText("Imported Ivy").last().click();
  await expect(page.getByRole("heading", { name: "Imported Ivy" })).toBeVisible();
  await expect(page.getByText("Stash Rooftop").first()).toBeVisible();   // her tag came along
});

test("unreadable files are listed with a reason and can be deleted", async () => {
  const junk = "e2e/media/lib/videos/zz broken.mp4";
  fs.writeFileSync(junk, Buffer.alloc(4000, 7));          // not a video at all
  try {
    await page.waitForTimeout(1500);
    await page.goto("/admin/libraries");
    await page.getByTestId("library").getByRole("button", { name: "Scan", exact: true }).click();
    await expect(async () => {
      await page.goto("/admin/maintenance");
      await expect(page.getByTestId("unreadable-file")).toHaveCount(1, { timeout: 3000 });
    }).toPass({ timeout: 40_000 });
    const row = page.getByTestId("unreadable-file");
    await expect(row).toContainText("/media/lib/videos/zz broken.mp4");
    await expect(row).toContainText("Incomplete video");   // ffprobe reads a .mp4 by its name: "moov atom not found"
    await page.screenshot({ path: `${SHOTS}/26-unreadable.png` });
    page.once("dialog", (d) => d.accept());
    await row.getByRole("button", { name: "Delete /media/lib/videos/zz broken.mp4" }).click();
    await expect(page.getByTestId("unreadable")).toHaveCount(0);
    expect(fs.existsSync(junk)).toBe(false);
  } finally {
    fs.rmSync(junk, { force: true });
  }
});

test("maintenance: check, clean up media deleted outside the app, run routine maintenance", async () => {
  const extra = "e2e/media/lib/pics/zz-cleanup.jpg";
  fs.copyFileSync("e2e/media/lib/pics/photo1.jpg", extra);
  try {
    await page.waitForTimeout(1500);   // let the new file settle before it is scanned
    await page.goto("/admin/libraries");
    await adminNav("Maintenance").click();
    const cleanup = page.getByTestId("cleanup");
    const report = page.getByTestId("cleanup-report");

    // the check rescans: the new picture is indexed, and it is not something to remove
    await cleanup.getByRole("button", { name: "Check what would be removed" }).click();
    await expect(report).toContainText("Check", { timeout: 30_000 });
    await expect(report).not.toContainText("1 images");
    await page.goto("/images?q=zz-cleanup");
    await expect(page.getByTestId("image-tile")).toHaveCount(1);

    // deleted behind the app's back
    fs.unlinkSync(extra);
    await page.goto("/admin/maintenance");
    await cleanup.getByRole("button", { name: "Check what would be removed" }).click();
    await expect(report).toContainText("Would remove 0 scenes, 1 images and 0 galleries", { timeout: 30_000 });
    await page.screenshot({ path: `${SHOTS}/18-maintenance-check.png` });

    page.once("dialog", (d) => d.accept());
    await report.getByRole("button", { name: "Remove these…" }).click();
    await expect(report).toContainText("Removed 0 scenes, 1 images and 0 galleries", { timeout: 30_000 });

    // a second cleanup has nothing left to do
    page.once("dialog", (d) => d.accept());
    await cleanup.getByRole("button", { name: "Clean up now…" }).click();
    await expect(report).toContainText("Nothing to clean up.", { timeout: 30_000 });

    // encryption of uploads is on in this stack, and nothing is left unencrypted
  await expect(page.getByTestId("encryption")).toContainText("On");
  await expect(page.getByTestId("encryption-counts")).toContainText(/(\d+) of \1 uploaded files encrypted/);

  // routine maintenance: change the schedule, run it now
    const routine = page.getByTestId("routine");
    await routine.getByLabel("Hour").selectOption("3");
    await routine.getByRole("button", { name: "Save" }).click();
    await expect(routine.getByRole("button", { name: "Save" })).toBeDisabled();
    await routine.getByRole("button", { name: "Run now" }).click();
    await expect(page.getByTestId("maintenance-report")).toContainText(/Optimized \d+ tables/, { timeout: 60_000 });
    await page.reload();
    await expect(routine.getByLabel("Hour")).toHaveValue("3");
    await page.screenshot({ path: `${SHOTS}/19-maintenance.png` });

    // the library still works
    await nav("Scenes").click();
    await expect(page.getByTestId("total")).toHaveText("3 scenes");
  } finally {
    fs.rmSync(extra, { force: true });
  }
});

test("duplicates: same file under another name is found, details combined, list downloaded", async () => {
  const copy = "e2e/media/lib/videos/Beach Day (copy).mp4";
  fs.copyFileSync("e2e/media/lib/videos/Beach Day.mp4", copy);
  try {
    await page.waitForTimeout(1500);   // let the copy settle before it is scanned
    await page.goto("/admin/libraries");
    await page.getByTestId("library").getByRole("button", { name: "Scan", exact: true }).click();
    await adminNav("Duplicates").click();
    await expect(async () => {
      await page.reload();
      await expect(page.getByTestId("duplicate-group")).toHaveCount(1, { timeout: 3000 });
    }).toPass({ timeout: 40_000 });
    const group = page.getByTestId("duplicate-group");
    await expect(group.getByTestId("duplicate-item")).toHaveCount(2);
    await expect(group).toContainText("/media/lib/videos/Beach Day (copy).mp4");
    await expect(page.getByTestId("duplicates-summary")).toContainText("1 sets of duplicates · 1 extra copies");
    await expect(group).toContainText("Details match");
    // rate one copy only: now the copies differ
    const href = await group.getByRole("link").first().getAttribute("href");
    const r = await page.request.patch(`/api${href}`, { data: { rating: 80 }, headers: { "X-Requested-With": "e2e" } });
    expect(r.ok()).toBeTruthy();
    await page.reload();
    await expect(group).toContainText("Details differ");
    await page.screenshot({ path: `${SHOTS}/20-duplicates.png` });
    await group.getByRole("button", { name: "Combine details" }).click();
    await expect(group).toContainText("Details match");

    const [download] = await Promise.all([
      page.waitForEvent("download"),
      page.getByRole("link", { name: "Download list of extra copies" }).click(),
    ]);
    expect(download.suggestedFilename()).toBe("duplicate-scenes.txt");
    const text = fs.readFileSync(await download.path(), "utf8");
    expect(text.trim().split("\n")).toHaveLength(1);
    expect(text).toContain("/media/lib/videos/Beach Day");

    // each copy has its own Delete button, and the set one that removes all but one copy;
    // here the page-wide "Delete all extra copies" runs in the background and reports back
    await expect(group.getByRole("button", { name: "Delete /media/lib/videos/Beach Day (copy).mp4" })).toBeVisible();
    await expect(group.getByRole("button", { name: "Delete 1 extra copy" })).toBeVisible();
    page.once("dialog", (d) => d.accept());
    await page.getByRole("button", { name: "Delete all extra copies…" }).click();
    await expect(page.getByTestId("delete-job")).toContainText("deleted 1 extra copies", { timeout: 40_000 });
    await expect(page.getByTestId("duplicate-group")).toHaveCount(0);
    expect(fs.existsSync(copy)).toBe(false);
    expect(fs.existsSync("e2e/media/lib/videos/Beach Day.mp4")).toBe(true);
    await nav("Scenes").click();
    await expect(page.getByTestId("total")).toHaveText("3 scenes");
  } finally {
    fs.rmSync(copy, { force: true });
  }
});

test("admin deletes media from disk: one scene from its page, images in bulk", async () => {
  const video = "e2e/media/lib/videos/zz Delete Me.mp4";
  const pics = ["e2e/media/lib/pics/zz-del-1.jpg", "e2e/media/lib/pics/zz-del-2.jpg"];
  fs.copyFileSync("e2e/media/lib/videos/Night Drive.mkv", video.replace(".mp4", ".mkv"));
  const mkv = video.replace(".mp4", ".mkv");
  fs.copyFileSync("e2e/media/lib/pics/photo5.jpg", pics[0]);
  fs.copyFileSync("e2e/media/lib/pics/photo6.jpg", pics[1]);
  try {
    await page.waitForTimeout(1500);
    await page.goto("/admin/libraries");
    await page.getByTestId("library").getByRole("button", { name: "Scan", exact: true }).click();
    await expect(async () => {
      await page.goto("/scenes?q=zz+Delete");
      await expect(page.getByTestId("scene-card")).toHaveCount(1, { timeout: 3000 });
    }).toPass({ timeout: 40_000 });

    await page.getByTestId("scene-card").getByRole("link").first().click();
    await expect(page.getByTestId("scene-title")).toHaveText("zz Delete Me");
    // the edit dialog shows where the file lives
    await page.getByRole("button", { name: "Edit" }).click();
    await expect(page.getByTestId("file-location")).toHaveText("/media/lib/videos/zz Delete Me.mkv");
    await page.keyboard.press("Escape");
    page.once("dialog", (d) => d.accept());
    await page.getByRole("button", { name: "Delete", exact: true }).click();
    await expect(page).toHaveURL(/\/scenes$/);
    expect(fs.existsSync(mkv)).toBe(false);
    await expect(page.getByTestId("total")).toHaveText("3 scenes");

    await page.goto("/images?q=zz-del");
    await expect(page.getByTestId("image-tile")).toHaveCount(2);
    await page.getByRole("button", { name: "Select images" }).click();
    await page.getByTestId("image-tile").nth(0).click();
    await page.getByTestId("image-tile").nth(1).click();
    page.once("dialog", (d) => d.accept());
    await page.getByTestId("bulk-bar").getByRole("button", { name: "Delete files" }).click();
    await expect(page.getByTestId("image-tile")).toHaveCount(0);
    expect(pics.some((p) => fs.existsSync(p))).toBe(false);
    expect(fs.existsSync("e2e/media/lib/pics/photo5.jpg")).toBe(true);
  } finally {
    for (const p of [mkv, ...pics]) fs.rmSync(p, { force: true });
  }
});

test("content filters: a user hides a performer for themselves, then switches the filter off", async () => {
  await nav("Performers").click();
  await expect(page.getByTestId("performer-card").filter({ hasText: "Alice" })).toBeVisible();
  await nav("Scenes").click();
  await expect(page.getByTestId("total")).toHaveText("3 scenes");

  await page.goto("/account");
  const filters = page.getByTestId("content-filters");
  await filters.getByLabel("Add a performer to hide…").pressSequentially("Ali");
  await filters.getByRole("option", { name: "Alice" }).click();
  await filters.getByRole("button", { name: "Save filters" }).click();
  await expect(filters.getByRole("status")).toHaveText("Filters saved.");
  await page.screenshot({ path: `${SHOTS}/21-content-filters.png` });

  await nav("Performers").click();
  await expect(page.getByTestId("performer-card").first()).toBeVisible();
  await expect(page.getByTestId("performer-card").filter({ hasText: "Alice" })).toHaveCount(0);
  await nav("Scenes").click();
  await expect(page.getByTestId("total")).not.toHaveText("3 scenes");

  // the choice is still listed by name after a reload, and can be switched off
  await page.goto("/account");
  await expect(filters.getByRole("button", { name: "Remove Alice" })).toBeVisible();
  await filters.getByLabel("Apply my filters").uncheck();
  await filters.getByRole("button", { name: "Save filters" }).click();
  await expect(filters.getByRole("status")).toHaveText("Filters saved.");
  await nav("Scenes").click();
  await expect(page.getByTestId("total")).toHaveText("3 scenes");
});

test("content filters: only show one performer's content", async () => {
  await page.goto("/account");
  const filters = page.getByTestId("content-filters");
  await filters.getByRole("button", { name: "Remove Alice" }).click();     // no longer hidden
  await filters.getByLabel("Apply my filters").check();
  await filters.getByLabel("Add a performer to show…").pressSequentially("Ali");
  await filters.getByRole("option", { name: "Alice" }).click();
  await filters.getByRole("button", { name: "Save filters" }).click();
  await expect(filters.getByRole("status")).toHaveText("Filters saved.");
  await page.screenshot({ path: `${SHOTS}/22-only-show.png` });

  await nav("Scenes").click();
  await expect(page.getByTestId("scene-card").first()).toBeVisible();
  await expect(page.getByTestId("total")).not.toHaveText("3 scenes");
  for (const card of await page.getByTestId("scene-card").all()) await expect(card).toContainText("Alice");

  // clear it again
  await page.goto("/account");
  await filters.getByRole("button", { name: "Remove Alice" }).click();
  await filters.getByRole("button", { name: "Save filters" }).click();
  await expect(filters.getByRole("status")).toHaveText("Filters saved.");
  await nav("Scenes").click();
  await expect(page.getByTestId("total")).toHaveText("3 scenes");
});

test("fullscreen on phones: falls back to the browser's own video fullscreen", async () => {
  await nav("Scenes").click();
  await page.getByTestId("scene-card").getByRole("link").first().click();
  const player = page.getByTestId("player");
  await expect(player).toBeVisible();
  // iPhone Safari: no element fullscreen at all, only webkitEnterFullscreen on the video
  await page.evaluate(() => {
    const w = window as unknown as { __nativeFs: number };
    w.__nativeFs = 0;
    for (const name of ["requestFullscreen", "webkitRequestFullscreen", "webkitRequestFullScreen"]) {
      Object.defineProperty(Element.prototype, name, { value: undefined, configurable: true });
    }
    (HTMLVideoElement.prototype as unknown as { webkitEnterFullscreen: () => void }).webkitEnterFullscreen = () => { w.__nativeFs += 1; };
  });
  await player.hover();
  await player.getByRole("button", { name: "Fullscreen" }).click();
  expect(await page.evaluate(() => (window as unknown as { __nativeFs: number }).__nativeFs)).toBe(1);
  await page.reload();   // drop the stubs
});

test("a Live Photo plays its motion in the image viewer", async () => {
  const headers = { "X-Requested-With": "e2e" };
  const lib = await (await page.request.post("/api/libraries", { data: { path: "/media/livephotos" }, headers })).json();
  await page.request.post(`/api/libraries/${lib.id}/scan`, { data: {}, headers });
  await expect(async () => {
    await page.goto("/images?q=IMG_LIVE");
    await expect(page.getByTestId("live-badge")).toHaveCount(1, { timeout: 3000 });
  }).toPass({ timeout: 60_000 });
  // its video is part of the photo, not a scene of its own
  await page.goto("/scenes?q=IMG_LIVE");
  await expect(page.getByText("No scenes match.")).toBeVisible();

  await page.goto("/images?q=IMG_LIVE");
  await page.getByTestId("image-tile").first().click();
  await page.getByTestId("live-button").click();
  const video = page.getByTestId("live-video");
  await expect(video).toBeVisible();
  await expect.poll(() => video.evaluate((v: HTMLVideoElement) => v.currentTime), { timeout: 20_000 }).toBeGreaterThan(0.5);
  await page.screenshot({ path: `${SHOTS}/24-live-photo.png` });
  await expect(video).toHaveCount(0, { timeout: 20_000 });          // back to the still when it ends
  await page.keyboard.press("Escape");
});

test("move files to another folder, choosing a new name on a clash", async () => {
  const base = "e2e/media/livephotos";
  fs.mkdirSync(`${base}/Trip`, { recursive: true });
  fs.copyFileSync(`${base}/IMG_LIVE.JPG`, `${base}/Trip/IMG_LIVE.JPG`);     // the name is taken there
  try {
    await page.goto("/images?q=IMG_LIVE");
    await expect(page.getByTestId("image-tile")).toHaveCount(1);
    await page.getByRole("button", { name: "Select images" }).first().click();
    await page.getByTestId("image-tile").first().click();
    await page.getByTestId("bulk-bar").getByRole("button", { name: "Move…" }).click();
    const dialog = page.getByRole("dialog", { name: "Move to folder" });
    await dialog.getByRole("button", { name: "Open /media/livephotos" }).click();
    await dialog.getByRole("button", { name: "Open Trip" }).click();
    await expect(dialog.getByTestId("move-path")).toHaveText("/media/livephotos / Trip");
    await page.screenshot({ path: `${SHOTS}/25-move.png` });
    await dialog.getByRole("button", { name: "Move 1 image here" }).click();

    const conflicts = dialog.getByTestId("move-conflicts");
    const name = conflicts.getByLabel("New name for IMG_LIVE.JPG");
    await expect(name).toHaveValue("IMG_LIVE (2).JPG");
    await name.fill("Beach sunset.JPG");
    await conflicts.getByRole("button", { name: "Move with these names" }).click();
    await expect(dialog.getByTestId("move-summary")).toContainText("Moved 1 item");
    await dialog.getByRole("button", { name: "Done" }).click();
    // the photo and its video moved together and kept their pairing
    expect(fs.existsSync(`${base}/Trip/Beach sunset.JPG`)).toBe(true);
    expect(fs.existsSync(`${base}/Trip/Beach sunset.mov`)).toBe(true);
    expect(fs.existsSync(`${base}/IMG_LIVE.JPG`)).toBe(false);
    await page.goto("/images?q=Beach+sunset");
    await expect(page.getByTestId("live-badge")).toHaveCount(1);
  } finally {
    // put the fixture back for the next run
    for (const ext of ["JPG", "mov"]) {
      if (fs.existsSync(`${base}/Trip/Beach sunset.${ext}`)) fs.renameSync(`${base}/Trip/Beach sunset.${ext}`, `${base}/IMG_LIVE.${ext}`);
    }
    fs.rmSync(`${base}/Trip`, { recursive: true, force: true });
  }
});

test("studios: a search typed just before ticking Top-level only keeps both", async () => {
  await page.goto("/studios");
  await page.getByRole("searchbox", { name: "Search" }).fill("a");
  await page.getByLabel("Top-level only").click();               // within the search's 300 ms delay
  await expect(page).toHaveURL(/[?&]q=a(&|$)/);
  await expect(page).toHaveURL(/[?&]roots=1(&|$)/);
  await expect(page.getByLabel("Top-level only")).toBeChecked();
});

test("the server's own stylesheet loads last and overrides theme roles", async () => {
  await page.goto("/");
  const css = await page.evaluate(() => {
    const root = getComputedStyle(document.documentElement);
    return [root.getPropertyValue("--e2e-custom-css").trim(), root.getPropertyValue("--color-link").trim().toLowerCase()];
  });
  expect(css).toEqual(["loaded", "#9e77d1"]);
});

test("admin theme: a preset saved for everyone, contrast warnings, back to built-in", async () => {
  const bodyBg = () => page.evaluate(() => getComputedStyle(document.body).backgroundColor);
  await page.goto("/admin/theme");
  await expect(page.getByRole("heading", { name: "Theme" })).toBeVisible();
  expect(await bodyBg()).toBe("rgb(11, 11, 16)");                        // built-in Crypt

  await page.getByRole("button", { name: /Ossuary/ }).click();
  expect(await bodyBg()).toBe("rgb(0, 0, 0)");                           // live preview
  await nav("Scenes").click();                                           // leaving without saving undoes the preview
  await expect(page).toHaveURL(/\/scenes/);
  await expect.poll(bodyBg).toBe("rgb(11, 11, 16)");                    // once the editor has unmounted
  await page.goBack();
  await page.getByRole("button", { name: /Ossuary/ }).click();
  await page.getByLabel("text muted", { exact: true }).fill("#202020");  // unreadable on black
  await expect(page.getByTestId("contrast-warnings")).toContainText("text muted on bg");
  await page.getByLabel("text muted", { exact: true }).fill("#A0A0A0");
  await expect(page.getByTestId("contrast-warnings")).toHaveCount(0);
  await page.screenshot({ path: `${SHOTS}/21-theme-editor.png`, fullPage: true });
  await page.getByRole("button", { name: "Save for everyone" }).click();
  await expect(page.getByRole("button", { name: "Save for everyone" })).toBeDisabled();

  await page.goto("/scenes");                                            // a fresh page load: from /api/theme.css
  expect(await bodyBg()).toBe("rgb(0, 0, 0)");
  await page.goto("/admin/theme");
  await page.getByRole("button", { name: "Use the built-in theme" }).click();
  await expect(page.getByRole("button", { name: "Use the built-in theme" })).toBeDisabled();
  await page.goto("/scenes");
  expect(await bodyBg()).toBe("rgb(11, 11, 16)");
});
