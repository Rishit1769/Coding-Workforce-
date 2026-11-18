import { defineConfig } from "@playwright/test";

const port = 8799;

export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  use: { baseURL: `http://127.0.0.1:${port}`, headless: true },
  webServer: {
    // Seeds a temporary workforce home by running the real engine with scripted agents, then serves API + dashboard.
    command: `../.venv/bin/python -m workforce.selftest.demo_server --port ${port}`,
    url: `http://127.0.0.1:${port}/api/overview`,
    timeout: 120_000,
    reuseExistingServer: false,
  },
});
