import { expect, test, type Locator, type Page } from "@playwright/test";

// Orchestration settings page E2E: grouped sidebar, intent engine persistence, TypeSafe
// panel validation, the decision test button, and the agent_sdk locked state. The backend
// runs with an isolated data dir (see playwright.config.ts); the rule engine path has no
// external dependencies.

async function openSettings(page: Page) {
  await page.goto("/");
  // Next dev hydration race: a click that lands too early does nothing, so retry
  // once when the assertion fails
  await page.getByRole("button", { name: "Settings" }).click();
  try {
    await expect(page.getByText("通用", { exact: true })).toBeVisible({
      timeout: 15_000,
    });
  } catch {
    await page.getByRole("button", { name: "Settings" }).click();
    await expect(page.getByText("通用", { exact: true })).toBeVisible({
      timeout: 10_000,
    });
  }
}

async function openOrchestration(page: Page) {
  await openSettings(page);
  await page.getByRole("button", { name: /^编排/ }).click();
  await expect(page.getByTestId("pipeline-overview")).toBeVisible();
}

async function saveAgentSettings(page: Page) {
  // AgentSettingsForm's SettingsCard uses the default saveLabel "Save"
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByText("Agent settings saved")).toBeVisible({
    timeout: 10_000,
  });
}

async function selectChatbotEngine(page: Page, engine: RegExp) {
  await openSettings(page);
  await page.getByRole("button", { name: "Agent", exact: true }).click();
  // "知识库问答所使用的后端" is the description text unique to the Chatbot engine
  // group; scope to its parent container (the group's root div) to avoid matching the
  // outer wrapper
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
  // text-transform:uppercase only changes the rendering, not the DOM text; getByText
  // matches the original case
  for (const group of ["通用", "Chat · 对话调优", "Workflow · 流程调优"]) {
    await expect(page.getByText(group, { exact: true })).toBeVisible();
  }
  await expect(page.getByText("仅 Langchain", { exact: true })).toBeVisible();
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
  // The panel stays mounted (0fr collapse animation): assert on the clip container whose
  // height is really 0 — clipped children still count as visible to Playwright
  const clip = page.getByTestId("typesafe-panel-clip");
  await expect(clip).toBeHidden();
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
  await page.getByRole("button", { name: /^编排/ }).click();
  const banner = page.getByTestId("orchestration-locked-banner");
  await expect(banner).toBeVisible();
  await expect(banner).toContainText("Claude Agent SDK");
  await expect(page.locator('[aria-disabled="true"]').first()).toBeVisible();
  await banner.getByRole("button", { name: /前往 Agent 设置/ }).click();
  await expect(page.getByText("Chatbot 引擎")).toBeVisible();

  await selectChatbotEngine(page, /Langchain RAG/);
  await page.getByRole("button", { name: /^编排/ }).click();
  await expect(page.getByTestId("orchestration-locked-banner")).toHaveCount(0);
  await expect(page.getByTestId("pipeline-overview")).toBeVisible();
});

test("inspector hides on settings page and pin keeps it visible", async ({
  page,
}) => {
  await openSettings(page);
  // Not pinned: the Inspector collapses once settings opens
  await expect(page.getByRole("complementary", { name: "Inspector" })).toBeHidden();
  // The Settings button is not a toggle: going back to the workbench via "问答" brings the
  // Inspector back
  await page.getByRole("button", { name: "问答" }).click();
  await expect(
    page.getByRole("complementary", { name: "Inspector" }).getByText("Agent Steps"),
  ).toBeVisible();
  // Pinned: the Inspector stays visible when entering settings again
  await page.getByTestId("inspector-pin").click();
  await page.getByRole("button", { name: "Settings" }).click();
  await expect(
    page.getByRole("complementary", { name: "Inspector" }).getByText("Agent Steps"),
  ).toBeVisible();
  // Restore: unpin (so other test cases and the locally persisted state are unaffected)
  await page.getByTestId("inspector-pin").click();
});


test("orchestration link card jumps to RAG retrieval card", async ({ page }) => {
  await openOrchestration(page);
  await page.getByRole("button", { name: /前往 RAG 配置/ }).click();
  await expect(page.getByTestId("scope-badge").first()).toContainText("共享");
  await expect(page.getByTestId("rag-retrieval")).toBeVisible();
});
