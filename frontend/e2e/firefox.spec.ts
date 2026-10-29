import { expect, test } from "@playwright/test";

const ADMIN = { username: "admin", password: "e2e-admin-password" };

test("@firefox a video with leftover DRM headers still plays", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("Username").fill(ADMIN.username);
  await page.getByLabel("Password").fill(ADMIN.password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("navigation", { name: "Main" })).toBeVisible();

  // a library holding just that file
  const headers = { "X-Requested-With": "e2e" };
  let lib = (await (await page.request.get("/api/libraries")).json()).find((l: { path: string }) => l.path === "/media/drm");
  if (!lib) {
    const r = await page.request.post("/api/libraries", { data: { path: "/media/drm" }, headers });
    expect(r.ok()).toBeTruthy();
    lib = await r.json();
  }
  await page.request.post(`/api/libraries/${lib.id}/scan`, { data: {}, headers });
  await expect(async () => {
    await page.goto("/scenes?q=Leftover");
    await expect(page.getByTestId("scene-card")).toHaveCount(1, { timeout: 3000 });
  }).toPass({ timeout: 40_000 });

  await page.getByTestId("scene-card").getByRole("link").first().click();
  const player = page.getByTestId("player");
  await expect(player).toBeVisible();
  await player.getByRole("button", { name: "Play" }).first().click();
  // it plays: time moves on, and no error is shown
  await expect.poll(() => player.locator("video").evaluate((v: HTMLVideoElement) => v.currentTime), { timeout: 20_000 })
    .toBeGreaterThan(1.5);
  await expect(player.getByText("can't be played")).toHaveCount(0);
  // ...straight from the original file, not via the converted fallback stream
  expect(await player.locator("video").evaluate((v: HTMLVideoElement) => v.currentSrc)).toMatch(/\/stream$/);
  await page.screenshot({ path: "test-results/screens/23-firefox-drm-headers.png" });
});
