import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "tests",
  testMatch: "**/*.spec.ts",
  workers: 1,
  timeout: 20_000,
  use: {
    baseURL: "http://127.0.0.1:5214",
    viewport: { width: 1440, height: 1000 },
    launchOptions: { args: ["--use-angle=swiftshader"] },
  },
  webServer: {
    command: "npm run dev -- --port 5214 --strictPort",
    url: "http://127.0.0.1:5214",
    reuseExistingServer: false,
    env: { FARSIGHT_JETSON_ORIGIN: "http://127.0.0.1:18900" },
  },
});
