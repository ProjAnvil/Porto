import { expect, test, type Locator, type Page } from "@playwright/test";

// 编排设置页 E2E：分组 sidebar、意图引擎持久化、TypeSafe 面板校验、
// 决策测试按钮、agent_sdk 锁定态。backend 运行在隔离 data dir（见
// playwright.config.ts），rule 引擎路径零外部依赖。

async function openSettings(page: Page) {
  await page.goto("/");
  // Next dev 水合竞态：过早点击不生效，断言失败时重试一次
  await page.getByRole("button", { name: "Settings" }).click();
  try {
    await expect(page.getByText("对话引擎", { exact: true })).toBeVisible({
      timeout: 15_000,
    });
  } catch {
    await page.getByRole("button", { name: "Settings" }).click();
    await expect(page.getByText("对话引擎", { exact: true })).toBeVisible({
      timeout: 10_000,
    });
  }
}

async function openOrchestration(page: Page) {
  await openSettings(page);
  await page.getByRole("button", { name: "编排", exact: true }).click();
  await expect(page.getByTestId("pipeline-overview")).toBeVisible();
}

async function saveAgentSettings(page: Page) {
  // AgentSettingsForm 的 SettingsCard 用默认 saveLabel "Save"
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByText("Agent settings saved")).toBeVisible({
    timeout: 10_000,
  });
}

async function selectChatbotEngine(page: Page, engine: RegExp) {
  await openSettings(page);
  await page.getByRole("button", { name: "Agent", exact: true }).click();
  // “知识库问答所使用的后端”是 Chatbot 引擎分组独有的描述文案；
  // 取其父容器（分组根 div）作为作用域，避免匹配到外层大容器
  const group = page
    .getByText("知识库问答所使用的后端")
    .locator("..");
  await group.getByRole("button", { name: engine }).click();
  await saveAgentSettings(page);
}

test("sidebar shows four grouped sections and navigates to orchestration", async ({
  page,
}) => {
  await openOrchestration(page);
  for (const group of ["对话引擎", "编排 · 仅 Langchain", "知识检索", "运行环境"]) {
    await expect(page.getByText(group, { exact: true })).toBeVisible();
  }
  for (const stage of ["意图路由", "检索前变换", "重排"]) {
    await expect(
      page.getByTestId("pipeline-overview").getByText(stage),
    ).toBeVisible();
  }
});

test("intent engine persists as rule after save and reload", async ({ page }) => {
  await openOrchestration(page);
  await page.getByRole("button", { name: /规则引擎/ }).click();
  await page.getByRole("button", { name: "保存意图路由" }).click();
  await expect(page.getByText("编排设置已保存")).toBeVisible({ timeout: 10_000 });
  await page.reload();
  await openOrchestration(page);
  const ruleCard: Locator = page.getByRole("button", { name: /规则引擎/ });
  await expect(ruleCard).toHaveClass(/border-zinc-900/);
  await expect(page.getByTestId("intent-test-result")).toHaveCount(0);
});

test("typesafe panel expands and validates empty api key", async ({ page }) => {
  await openOrchestration(page);
  await expect(page.getByTestId("typesafe-panel")).toHaveCount(0);
  await page.getByRole("button", { name: /TypeSafe 外部引擎/ }).click();
  await expect(page.getByTestId("typesafe-panel")).toBeVisible();
  await page.getByRole("button", { name: "保存意图路由" }).click();
  await expect(page.getByTestId("typesafe-key-error")).toHaveText(/API Key/);
});

test("intent test button returns rule-engine decision", async ({ page }) => {
  await openOrchestration(page);
  await page.getByRole("button", { name: /规则引擎/ }).click();
  await page.getByTestId("intent-test-input").fill("你好");
  await page.getByRole("button", { name: "测试", exact: true }).click();
  const result = page.getByTestId("intent-test-result");
  await expect(result).toContainText("direct", { timeout: 10_000 });
  await expect(result).toContainText("规则");
});

test("orchestration locks under agent_sdk and recovers after switching back", async ({
  page,
}) => {
  await selectChatbotEngine(page, /Claude Agent SDK/);
  await page.getByRole("button", { name: "编排", exact: true }).click();
  const banner = page.getByTestId("orchestration-locked-banner");
  await expect(banner).toBeVisible();
  await expect(banner).toContainText("Claude Agent SDK");
  await expect(page.locator('[aria-disabled="true"]').first()).toBeVisible();
  await banner.getByRole("button", { name: /前往 Agent 设置/ }).click();
  await expect(page.getByText("Chatbot 引擎")).toBeVisible();

  await selectChatbotEngine(page, /Langchain RAG/);
  await page.getByRole("button", { name: "编排", exact: true }).click();
  await expect(page.getByTestId("orchestration-locked-banner")).toHaveCount(0);
  await expect(page.getByTestId("pipeline-overview")).toBeVisible();
});
