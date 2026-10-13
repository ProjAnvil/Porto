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
    // 端口用 3170/8170（非 dev 默认端口）+ 禁止复用存量 server：
    // 否则会挂到开发者正跑着的 make dev 上，把测试写入真实配置。
    baseURL: "http://localhost:3170",
    // channel=chromium：用完整 Chromium 的新 headless 模式，避免依赖
    // chromium_headless_shell（独立下载物）。
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
      // 生产构建而非 next dev：Next 16 的项目级 dev 单例会拒绝同一目录的第二个
      // dev server（用户 make start 的 3100 实例持有锁），build+start 与之解耦。
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
