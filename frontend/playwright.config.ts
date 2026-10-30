import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  timeout: 90_000,
  expect: { timeout: 20_000 },
  fullyParallel: false,
  workers: 1,
  reporter: [["list"]],
  outputDir: "test-results",
  use: {
    baseURL: process.env.BASE_URL ?? "http://localhost:8420",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    viewport: { width: 1440, height: 900 },
  },
  projects: [
    { name: "chromium", grepInvert: /@firefox|@mobile/, use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } } },
    // Firefox-specific behaviour, after the main journey (which it builds on).
    { name: "mobile", grep: /@mobile/, dependencies: ["chromium"], use: { ...devices["iPhone 15 Pro Max"] } },
    { name: "firefox", grep: /@firefox/, dependencies: ["chromium"], use: { ...devices["Desktop Firefox"], viewport: { width: 1440, height: 900 } } },
  ],
});
