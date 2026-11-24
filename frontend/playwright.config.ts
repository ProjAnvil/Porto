import { defineConfig } from "@playwright/test";

// E2E: interaction checks for the orchestration settings page. Two web servers — backend
// (FastAPI :8100) + frontend (Next dev :3100). The backend uses a separate temporary data
// dir so the real ~/.porto config is never touched.
export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  retries: 0,
  fullyParallel: false,
  workers: 1,
  expect: { timeout: 10_000 },
  use: {
    // Must be localhost (the origin next dev reports for itself): the cross-origin
    // protection in Next 16 dev treats 127.0.0.1 as a different origin and returns 403
    // for /_next/static assets, which breaks hydration.
    // Ports are 3170/8170 (not the dev defaults) and reusing an existing server is
    // disabled: otherwise the tests would attach to the developer's running make dev and
    // write into the real config.
    baseURL: "http://localhost:3170",
    // channel=chromium: the new headless mode of full Chromium, avoiding a dependency on
    // chromium_headless_shell (a separate download).
    channel: "chromium",
  },
  webServer: [
    {
      command:
        "cd ../backend && uv run uvicorn porto_chatbot.main:app --host 127.0.0.1 --port 8170",
      url: "http://127.0.0.1:8170/api/health",
      reuseExistingServer: false,
      timeout: 120_000,
      env: {
        PORTO_CHATBOT_DATA_DIR: "./.e2e-data",
        PORTO_CHATBOT_LOG_DIR: "./.e2e-logs",
      },
    },
    {
      // Production build instead of next dev: the project-level dev singleton in Next 16
      // refuses a second dev server for the same directory (the lock is held by the 3100
      // instance from the user's make start), so build+start is decoupled from it.
      command: "npm run build && npm run start -- -p 3170",
      url: "http://localhost:3170",
      reuseExistingServer: false,
      timeout: 300_000,
      env: {
        PORTO_API_BASE_URL: "http://127.0.0.1:8170",
      },
    },
  ],
});
