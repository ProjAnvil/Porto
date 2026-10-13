import { defineConfig } from "@playwright/test";

// E2E：编排设置页交互验证。双 webServer——backend (FastAPI :8100) + frontend
// (Next dev :3100)。backend 用独立临时 data dir，避免污染 ~/.porto 真实配置。
export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  retries: 0,
  fullyParallel: false,
  workers: 1,
  expect: { timeout: 10_000 },
  use: {
    // 必须用 localhost（与 next dev 自报的源一致）：Next 16 dev 的跨源保护会把
    // 127.0.0.1 视为不同源，对 /_next/static 资源返回 403，导致水合失败。
    baseURL: "http://localhost:3100",
    // channel=chromium：用完整 Chromium 的新 headless 模式，避免依赖
    // chromium_headless_shell（独立下载物）。
    channel: "chromium",
  },
  webServer: [
    {
      command:
        "cd ../backend && uv run uvicorn porto_chatbot.main:app --host 127.0.0.1 --port 8100",
      url: "http://127.0.0.1:8100/api/health",
      reuseExistingServer: true,
      timeout: 120_000,
      env: {
        PORTO_CHATBOT_DATA_DIR: "./.e2e-data",
        PORTO_CHATBOT_LOG_DIR: "./.e2e-logs",
      },
    },
    {
      command: "npm run dev -- -p 3100",
      url: "http://127.0.0.1:3100",
      reuseExistingServer: true,
      timeout: 120_000,
    },
  ],
});
