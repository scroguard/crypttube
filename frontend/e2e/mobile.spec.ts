import { devices, expect, test, type Page } from "@playwright/test";

// A phone browser with touch input, after the main journey (which it builds on).
test.use({ ...devices["iPhone 15 Pro Max"] });

const stars = (page: Page) => page.locator('[aria-label^="Rating "]').first();
const WRITE = { headers: { "X-Requested-With": "e2e" } };

async function signIn(page: Page) {
  await page.goto("/");
  await page.getByLabel("Username").fill("admin");
  await page.getByLabel("Password").fill("e2e-admin-password");
  await page.getByRole("button", { name: "Sign in" }).tap();
  await expect(page.getByRole("heading", { name: "Home" })).toBeVisible();
}

async function first(page: Page, kind: "scenes" | "images"): Promise<number> {
  const { items } = await (await page.request.get(`/api/${kind}?limit=1`)).json();
  await page.request.patch(`/api/${kind}/${items[0].id}`, { ...WRITE, data: { rating: null } });
  return items[0].id;
}

test("@mobile rate a video by touch, with the video playing", async ({ page }) => {
  await signIn(page);
  const id = await first(page, "scenes");
  await page.goto(`/scenes/${id}`);
  await page.getByTestId("player").tap({ position: { x: 100, y: 60 } });

  const star = page.getByRole("button", { name: "Rate 4 of 5" });
  const box = (await star.boundingBox())!;
  expect(box.height).toBeGreaterThanOrEqual(44);    // a finger's worth
  expect(box.width).toBeGreaterThanOrEqual(36);
  await star.tap();
  await expect(stars(page)).toHaveAttribute("aria-label", "Rating 4 of 5");
  await expect.poll(async () => (await (await page.request.get(`/api/scenes/${id}`)).json()).rating).toBe(80);

  await page.reload();
  await expect(stars(page)).toHaveAttribute("aria-label", "Rating 4 of 5");
  await page.getByRole("button", { name: "Rate 4 of 5" }).tap();        // the same star clears it
  await expect(stars(page)).toHaveAttribute("aria-label", "Rating 0 of 5");
});

test("@mobile a rating that can't be saved says so and goes back", async ({ page }) => {
  await signIn(page);
  const id = await first(page, "scenes");
  await page.goto(`/scenes/${id}`);
  await expect(page.getByTestId("scene-title")).toBeVisible();
  await page.route("**/api/scenes/*", (r) => (r.request().method() === "PATCH"
    ? r.fulfill({ status: 403, contentType: "application/json", body: '{"detail":"not allowed"}' }) : r.continue()));
  await page.getByRole("button", { name: "Rate 2 of 5" }).tap();
  await expect(page.getByRole("alert")).toHaveText("Rating not saved: not allowed");
  await expect(stars(page)).toHaveAttribute("aria-label", "Rating 0 of 5");
});

test("@mobile a star answers before the save does, and again after a failed one", async ({ page }) => {
  await signIn(page);
  const id = await first(page, "scenes");
  await page.goto(`/scenes/${id}`);
  await expect(page.getByTestId("scene-title")).toBeVisible();
  let release = (_status: number) => {};
  await page.route("**/api/scenes/*", async (r) => {
    if (r.request().method() !== "PATCH") return r.continue();
    const status = await new Promise<number>((resolve) => { release = resolve; });
    return status === 200 ? r.continue() : r.fulfill({ status, contentType: "application/json", body: '{"detail":"no"}' });
  });
  await page.getByRole("button", { name: "Rate 2 of 5" }).tap();
  await expect(stars(page)).toHaveAttribute("aria-label", "Rating 2 of 5");   // still saving
  release(500);
  await expect(page.getByRole("alert")).toBeVisible();
  await page.getByRole("button", { name: "Rate 3 of 5" }).tap();
  await expect(stars(page)).toHaveAttribute("aria-label", "Rating 3 of 5");   // a retry shows at once too
  await expect(page.getByRole("alert")).toHaveCount(0);
  release(200);
  await expect.poll(async () => (await (await page.request.get(`/api/scenes/${id}`)).json()).rating).toBe(60);
});

test("@mobile rate an image by touch in the viewer", async ({ page }) => {
  await signIn(page);
  await page.goto("/images");
  await page.getByTestId("image-tile").first().tap();
  const viewer = page.getByTestId("lightbox");
  await expect(viewer).toBeVisible();
  const star = viewer.getByRole("button", { name: "Rate 5 of 5" });
  await expect(star).toBeInViewport();
  const [saved] = await Promise.all([page.waitForResponse((r) => r.request().method() === "PATCH"), star.tap()]);
  expect(saved.status()).toBe(200);
  await expect(viewer.locator('[aria-label^="Rating "]')).toHaveAttribute("aria-label", "Rating 5 of 5");
  expect((await (await page.request.get(saved.url())).json()).rating).toBe(100);
  await page.screenshot({ path: "test-results/screens/mobile-image-rating.png" });
});
