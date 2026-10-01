import { expect, test, type Page } from "@playwright/test";

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
  await expect(page.getByText("No scenes match.")).toBeVisible();
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

test("items per page: paged browsing instead of continuous scroll", async () => {
  await nav("Images").click();
  await expect(page.getByTestId("image-tile")).toHaveCount(15); // 12 loose + 3 in the zip
  await expect(page.getByTestId("pager")).toHaveCount(0);

  await page.getByLabel("Items per page").selectOption("10");
  await expect(page.getByTestId("image-tile")).toHaveCount(10);
  await expect(page.getByTestId("page-indicator").first()).toHaveText("Page 1 of 2");
  await expect(page.getByRole("button", { name: "‹ Previous" }).first()).toBeDisabled();
  await page.screenshot({ path: `${SHOTS}/09a-paged.png` });

  await page.getByRole("button", { name: "Next ›" }).first().click();
  await expect(page.getByTestId("image-tile")).toHaveCount(5);
  await expect(page.getByTestId("page-indicator").first()).toHaveText("Page 2 of 2");
  await expect(page.getByRole("button", { name: "Next ›" }).first()).toBeDisabled();
  await page.getByRole("button", { name: "‹ Previous" }).first().click();
  await expect(page.getByTestId("image-tile")).toHaveCount(10);

  // the lightbox steps through the current page
  await page.getByTestId("image-tile").first().click();
  await expect(page.getByTestId("lightbox-counter")).toHaveText("1 / 10");
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
  await bulk.getByLabel("Performers…").fill("Ali");
  await page.getByRole("option", { name: "Alice" }).click();
  await bulk.getByRole("button", { name: "Add performers" }).click();
  await expect(bulk.getByRole("button", { name: "Add performers" })).toBeDisabled(); // applied, picker reset
  await page.screenshot({ path: `${SHOTS}/09b-bulk-images.png` });
  await page.getByRole("button", { name: "Done selecting" }).click();

  // lightbox: shows who is in the image, and can edit it
  await page.getByTestId("image-tile").first().click();
  await expect(page.getByTestId("lightbox-meta")).toContainText("Alice");
  await page.getByTestId("lightbox").getByRole("button", { name: "Edit" }).click();
  const dialog = page.getByRole("dialog", { name: "Edit image" });
  await dialog.getByLabel("Add tags…").pressSequentially("Portrait", { delay: 15 });
  await dialog.getByRole("option", { name: "Create “Portrait”" }).click();
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
  await expect(dialog).toBeHidden();
  await expect(portrait).toBeVisible();

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
