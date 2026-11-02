// 浏览器回归：真实环境仅 GET；写操作统一路由到独立 TEST/MOCK 服务。
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const { chromium } = require(process.env.AIOPS_PLAYWRIGHT_PATH || "playwright");

const base = process.env.AIOPS_UI_URL || "http://127.0.0.1:5173";
const mock = process.env.AIOPS_TEST_API || "http://127.0.0.1:8011";
const output = path.resolve(__dirname, "../docs/evidence");
const results = [];
let browser;
let activePage;
let closing = false;
const transportErrors = [];

async function check(name, operation) {
  await operation();
  results.push({ name, result: "PASS" });
  console.log(`PASS ${name}`);
}

async function noOverflow(page) {
  const sizes = await page.evaluate(() => ({
    content: document.documentElement.scrollWidth,
    viewport: document.documentElement.clientWidth,
  }));
  assert.ok(sizes.content <= sizes.viewport, JSON.stringify(sizes));
}

async function inputToken(page) {
  await page.getByRole("button", { name: /设置/ }).click();
  await page.getByLabel("本机管理员令牌").fill("TEST-admin-token");
  await page.getByRole("button", { name: "完成", exact: true }).click();
}

async function waitEnabled(locator) {
  await locator.waitFor({ state: "visible" });
  await locator.evaluate(
    (button) =>
      new Promise((resolve, reject) => {
        const deadline = Date.now() + 10000;
        const poll = () => {
          if (!button.disabled) resolve();
          else if (Date.now() >= deadline)
            reject(new Error("Button stayed disabled"));
          else setTimeout(poll, 50);
        };
        poll();
      }),
  );
}

async function run() {
  await fs.mkdir(output, { recursive: true });
  browser = await chromium.launch({
    headless: true,
    executablePath: process.env.AIOPS_BROWSER_EXECUTABLE,
  });
  const realContext = await browser.newContext({
    viewport: { width: 1440, height: 1000 },
  });
  const realPage = await realContext.newPage();
  const pageErrors = [];
  realPage.on("pageerror", (error) => pageErrors.push(error.message));
  await realPage.route(
    (url) => url.pathname.startsWith("/api/"),
    async (route) => {
      assert.equal(
        route.request().method(),
        "GET",
        "Real environment is read-only during UI regression",
      );
      await route.continue();
    },
  );
  await check(
    "REAL environment, probe tab and 1440/1280/1024 layout",
    async () => {
      await realPage.goto(`${base}/environment`);
      await realPage.getByRole("heading", { name: "Planning LLM" }).waitFor();
      assert.equal(await realPage.locator(".service-card-item").count(), 4);
      assert.ok(
        (await realPage.locator(".environment-control").innerText()).includes(
          "aiops-demo",
        ),
      );
      assert.ok(
        (
          await realPage.locator(".integration-item").last().innerText()
        ).includes("UNKNOWN"),
      );
      for (const width of [1440, 1280, 1024]) {
        await realPage.setViewportSize({ width, height: 1000 });
        await noOverflow(realPage);
        if (width === 1440)
          await realPage.screenshot({
            path: path.join(output, "console-environment-real-1440.png"),
            fullPage: true,
          });
      }
      await realPage
        .getByRole("button", { name: "基线探针", exact: true })
        .click();
      await realPage.getByText("mysql_select_1", { exact: true }).waitFor();
      // 页面上同时存在「核心依赖状态」表和基线探针表，必须只统计探针行。
      assert.equal(await realPage.locator("tbody tr:has(.probe-observation)").count(), 6);
      await realPage.locator(".probe-observation summary").first().click();
      assert.ok(
        (
          await realPage.locator(".probe-observation[open] pre").innerText()
        ).includes("SELECT 1"),
      );
    },
  );
  await check(
    "REAL archive filtering, workbench evidence and Markdown download",
    async () => {
      await realPage.goto(`${base}/incidents`);
      await realPage.locator(".incidents-table tbody tr").first().waitFor();
      await realPage.locator("select").first().selectOption("NGINX_502");
      await realPage.waitForResponse(
        (response) =>
          response.url().includes("scenario=NGINX_502") &&
          response.status() === 200,
      );
      await realPage.locator(".incidents-table tbody tr").first().waitFor();
      for (const cell of await realPage
        .locator(".incidents-table tbody tr td:nth-child(2)")
        .allTextContents())
        assert.equal(cell, "NGINX_502");
      await realPage.locator("select").last().selectOption("RESOLVED");
      await realPage.waitForResponse(
        (response) =>
          response.url().includes("status=RESOLVED") &&
          response.status() === 200,
      );
      await realPage.locator(".incident-link").first().click();
      await realPage.getByRole("heading", { name: "根因诊断" }).waitFor();
      assert.ok((await realPage.locator(".evidence-row-item").count()) > 0);
      await realPage.locator(".evidence-row-item button").first().click();
      await realPage.locator(".drawer-tab-btn").filter({ hasText: "原始数据" }).click();
      assert.ok(
        await realPage.locator(".drawer-pre").first().isVisible(),
      );
      await realPage.locator(".drawer-close-btn").click();
      await realPage
        .locator(".service-health-tags .state-badge")
        .filter({ hasText: "running" })
        .first()
        .waitFor();
      await realPage.setViewportSize({ width: 1440, height: 1000 });
      await realPage.evaluate(() => window.scrollTo(0, 0));
      await realPage.screenshot({
        path: path.join(output, "console-workbench-real-1440.png"),
        fullPage: true,
      });
      for (const width of [1280, 1024]) {
        await realPage.setViewportSize({ width, height: 1000 });
        await noOverflow(realPage);
      }
      const downloadPromise = realPage.waitForEvent("download");
      await realPage.getByRole("link", { name: "下载报告", exact: true }).click();
      const download = await downloadPromise;
      assert.ok(download.suggestedFilename().endsWith(".md"));
    },
  );

  const testContext = await browser.newContext({
    viewport: { width: 1280, height: 1000 },
  });
  const testHealth = await (
    await testContext.request.get(`${mock}/api/health`)
  ).json();
  assert.equal(
    testHealth.mode,
    "TEST/MOCK",
    "Write tests require isolated TEST/MOCK provider",
  );
  if (testHealth.lab.active_incident_id) {
    const reset = await testContext.request.post(`${mock}/api/lab/reset`, {
      headers: {
        "X-Admin-Token": "TEST-admin-token",
        "Idempotency-Key": `TEST-browser-${Date.now()}`,
      },
      data: { confirm: true },
    });
    assert.equal(reset.status(), 200);
  }
  const page = await testContext.newPage();
  activePage = page;
  page.on("pageerror", (error) => pageErrors.push(error.message));
  let responseMode = "backend";
  let syntheticIncident;
  const injectionPaths = [];
  await page.route(
    (url) => url.pathname.startsWith("/api/"),
    async (route) => {
      try {
        const url = new URL(route.request().url());
        if (url.pathname.endsWith("/inject")) injectionPaths.push(url.pathname);
        if (responseMode === "offline" && url.pathname === "/api/health") {
          await route.abort("connectionrefused");
          return;
        }
        if (responseMode === "missing" && url.pathname === "/api/health") {
          const response = await route.fetch({ url: `${mock}${url.pathname}` });
          const data = await response.json();
          data.lab.services = {};
          data.lab.probes = [];
          data.lab.healthy = false;
          data.lab.error = {
            code: "TEST_DOCKER_UNAVAILABLE",
            message: "TEST/MOCK Docker unavailable",
          };
          await route.fulfill({ json: data });
          return;
        }
        if (
          responseMode === "synthetic" &&
          url.pathname === `/api/incidents/${syntheticIncident.id}`
        ) {
          await route.fulfill({ json: syntheticIncident });
          return;
        }
        const headers = {
          ...route.request().headers(),
          origin: "http://127.0.0.1:5173",
        };
        const response = await route.fetch({
          url: `${mock}${url.pathname}${url.search}`,
          headers,
        });
        await route.fulfill({ response });
      } catch (error) {
        if (!closing) transportErrors.push(error.message);
        await route.abort().catch(() => {});
      }
    },
  );
  const scenarios = [
    { key: "MYSQL_DOWN", target: "demo-mysql" },
    { key: "REDIS_DOWN", target: "demo-redis" },
    { key: "NGINX_502", target: "demo-app" },
  ];
  let sample;
  for (const [index, scenario] of scenarios.entries()) {
    await check(
      `TEST/MOCK ${scenario.key}: correct injection, waiting refresh, ${index === 1 ? "rejection" : "approval"}`,
      async () => {
        await page.goto(`${base}/lab`);
        await page
          .getByText("TEST/MOCK · 自动化测试夹具，不是真实故障记录", {
            exact: true,
          })
          .waitFor();
        await inputToken(page);
        const inject = page
          .getByRole("button", { name: "注入此故障", exact: true })
          .nth(index);
        await waitEnabled(inject);
        await inject.click();
        const dialog = page.getByRole("dialog");
        // 确认对话框必须展示该场景的真实目标容器，而不是场景编码。
        assert.ok(
          (await dialog.innerText()).includes(scenario.target),
          `Dialog must show the real target ${scenario.target}`,
        );
        await dialog.getByRole("button", { name: "确认停止并注入" }).click();
        await page.getByRole("heading", { name: "根因诊断" }).waitFor();
        assert.equal(
          injectionPaths.at(-1),
          `/api/scenarios/${scenario.key}/inject`,
        );
        assert.equal(
          await page.locator(".sample-details").count(),
          0,
          "No fake verification samples",
        );
        await page.getByText("尚未取得复测采样。", { exact: true }).waitFor();
        // 页头与根因卡片空状态各有一个「开始智能诊断」，行为一致，取第一个。
        await page.getByRole("button", { name: "开始智能诊断" }).first().click();
        await page
          .getByRole("button", { name: "批准并执行", exact: true })
          .waitFor();
        await page.reload();
        await page
          .getByRole("button", { name: "批准并执行", exact: true })
          .waitFor();
        assert.ok(
          await page
            .getByRole("button", { name: "批准并执行", exact: true })
            .isDisabled(),
        );
        await inputToken(page);
        if (index === 1) {
          await page
            .getByRole("button", { name: "拒绝计划", exact: true })
            .click();
          await page
            .getByRole("button", { name: "确认拒绝", exact: true })
            .click();
          await page.getByText("人工已拒绝", { exact: true }).waitFor();
          assert.equal(await page.locator(".sample-details").count(), 0);
        } else {
          await page
            .getByRole("button", { name: "批准并执行", exact: true })
            .click();
          await page
            .locator(".el-popconfirm")
            .getByRole("button", { name: "批准并执行", exact: true })
            .click();
          await page
            .getByText("本轮场景的全部强制探针通过。", { exact: true })
            .waitFor();
          const response = await testContext.request.get(
            `${mock}/api/incidents/${page.url().split("/").at(-1)}`,
          );
          sample = await response.json();
          assert.equal(
            sample.records.filter(
              (record) =>
                record.kind === "execution" &&
                record.payload.state === "SUCCEEDED",
            ).length,
            1,
          );
        }
        await noOverflow(page);
        await page.goto(`${base}/environment`);
        await page.getByRole("heading", { name: "实验控制" }).waitFor();
        await inputToken(page);
        const reset = page.getByRole("button", {
          name: "重置演示环境",
          exact: true,
        });
        await waitEnabled(reset);
        await reset.click();
        await page
          .getByRole("button", { name: "确认重置", exact: true })
          .click();
        await page
          .getByText("重置完成，服务端已确认四个 demo 服务及全部基线通过。", {
            exact: true,
          })
          .waitFor();
      },
    );
  }
  await check(
    "TEST/MOCK missing Docker state never falls back to healthy",
    async () => {
      responseMode = "missing";
      await page.goto(`${base}/environment?tab=probes`);
      await page.getByRole("heading", { name: "Planning LLM" }).waitFor();
      assert.equal(
        await page
          .locator(".service-health-tags .state-badge")
          .filter({ hasText: "UNKNOWN" })
          .count(),
        4,
      );
      assert.equal(
        await page.locator('.service-cards-bar [data-tone="success"]').count(),
        0,
      );
      await page
        .getByText("尚未取得探针观测，不能判定恢复结果。", { exact: true })
        .waitFor();
      await page.screenshot({
        path: path.join(output, "console-test-missing-1280.png"),
        fullPage: true,
      });
    },
  );
  for (const verdict of ["FAIL", "UNKNOWN"]) {
    await check(
      `TEST/MOCK ${verdict} verification remains visible`,
      async () => {
        responseMode = "synthetic";
        syntheticIncident = structuredClone(sample);
        syntheticIncident.mode = "TEST/MOCK";
        syntheticIncident.status = verdict === "FAIL" ? "FAILED" : "UNKNOWN";
        syntheticIncident.detail.verification.verdict = verdict;
        for (const record of syntheticIncident.records) {
          if (record.kind === "probe_sample")
            for (const probe of record.payload.probes) probe.verdict = verdict;
        }
        await page.goto(`${base}/incidents/${syntheticIncident.id}`);
        await page
          .getByRole("heading", { name: /复测/ })
          .waitFor();
        assert.equal(
          await page
            .locator(".verification-result")
            .getAttribute("data-verdict"),
          verdict,
        );
        assert.equal(
          await page
            .locator('.sample-details .verdict[data-verdict="PASS"]')
            .count(),
          0,
        );
        await noOverflow(page);
      },
    );
  }
  await check(
    "TEST/MOCK network failure and empty archive show honest states",
    async () => {
      responseMode = "offline";
      await page.goto(`${base}/environment`);
      await page.locator(".el-alert--error").waitFor();
      assert.equal(
        await page.locator('.service-cards-bar [data-tone="success"]').count(),
        0,
      );
      responseMode = "backend";
      await page.goto(`${base}/incidents`);
      await page.locator("select").last().selectOption("BLOCKED");
      await page.getByText("暂无符合条件的工单。", { exact: false }).waitFor();
    },
  );
  await check(
    "TEST/MOCK visual attachment upload states the observation time honestly",
    async () => {
      await page.goto(`${base}/incidents`);
      await page.locator(".incidents-table tbody tr").first().waitFor();
      await page.locator(".incidents-table tbody tr .incident-link").first().click();
      await page.getByRole("heading", { name: "视觉证据" }).waitFor();
      await inputToken(page);
      await page.setInputFiles('input[type="file"]', {
        name: "shot.png",
        mimeType: "image/png",
        buffer: Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 1, 2, 3]),
      });
      // 没有可识别时间戳的截图必须说「时间未知」，不能拿上传时间顶替。
      await page.getByText("时间未知：截图内没有可识别的观测时间").first().waitFor();
      const card = await page
        .locator(".proto-card")
        .filter({ hasText: "视觉证据" })
        .innerText();
      assert.ok(
        card.includes("上传时间不能代替截图实际发生时间"),
        "honesty note about observation time must be shown",
      );
      await noOverflow(page);
    },
  );
  await check(
    "TEST/MOCK impact graph and review summary distinguish relation kinds",
    async () => {
      await page.goto(`${base}/incidents`);
      await page.locator(".incidents-table tbody tr").first().waitFor();
      await page.locator(".incidents-table tbody tr .incident-link").first().click();
      await page.getByRole("heading", { name: "服务影响图" }).waitFor();
      const graph = await page
        .locator(".proto-card")
        .filter({ hasText: "服务影响图" })
        .innerText();
      // 三类关系必须分开标注，不能混为一谈。
      assert.ok(graph.includes("配置依赖"), "configured relation must be labelled");
      assert.ok(graph.includes("推断传播"), "inferred relation must be labelled");
      assert.ok(graph.includes("已观测异常"), "observed anomaly must be labelled");
      assert.ok(graph.includes("受影响接口"), "impacted interfaces must be listed");
      await page.getByRole("heading", { name: "复盘摘要" }).waitFor();
      const review = await page
        .locator(".proto-card")
        .filter({ hasText: "复盘摘要" })
        .innerText();
      assert.ok(review.includes("批准动作"), "approved action must be listed");
      assert.ok(review.includes("修复前后探针"), "probes before/after must be listed");
      assert.ok(
        review.includes("不重新执行任何容器动作"),
        "review must state it is a projection of saved records",
      );
      await noOverflow(page);
    },
  );
  await check(
    "TEST/MOCK candidate root causes and supplement rationale are visible",
    async () => {
      await page.goto(`${base}/incidents`);
      await page.locator(".incidents-table tbody tr").first().waitFor();
      await page.locator(".incidents-table tbody tr .incident-link").first().click();
      await page.getByRole("heading", { name: "候选根因与补采" }).waitFor();
      const card = await page
        .locator(".proto-card")
        .filter({ hasText: "候选根因与补采" })
        .innerText();
      // 展示的是可核对的结论摘要与证据引用，不是模型的隐藏思考过程。
      assert.ok(card.includes("新增证据排除了什么"), "against-evidence column must exist");
      assert.ok(card.includes("仍缺失的检查"), "missing checks column must exist");
      assert.ok(card.includes("不展示模型隐藏思考过程"), "honesty note must be shown");
      await noOverflow(page);
    },
  );
  await check(
    "TEST/MOCK evaluation page: arms comparison, hidden truth and honest metrics",
    async () => {
      await page.goto(`${base}/evaluation`);
      await page.getByRole("heading", { name: "盲诊评测" }).waitFor();
      await inputToken(page);
      await page.getByRole("button", { name: "触发实验" }).click();
      await page.getByText("实验已触发", { exact: false }).waitFor();
      await page.getByText("A · 单次诊断", { exact: false }).first().waitFor();
      await page.getByText("B · 多 Agent 复核", { exact: false }).first().waitFor();
      const cards = await page.locator(".proto-card").allInnerTexts();
      const comparison = cards.find((text) => text.includes("各臂对照"));
      assert.ok(comparison, "comparison card must exist");
      // 纯诊断对照不评估处置提议与拦截率：必须显示「不适用」，不能显示 0。
      assert.ok(comparison.includes("不适用"), "proposal metrics must be not applicable");
      assert.ok(comparison.includes("统计显著性"), "honesty note must be shown");
      // 耗时必须按阶段拆分显示；审批等待在纯诊断下标「不适用」。
      assert.ok(comparison.includes("模型时间"), "model time must be split out");
      assert.ok(comparison.includes("采证时间"), "collection time must be split out");
      assert.ok(comparison.includes("人工审批等待"), "approval wait must be listed");
      assert.ok(comparison.includes("token 用量"), "token usage must be listed");
      // 默认不显示真值列。
      const records = cards.find((text) => text.includes("实验记录"));
      assert.ok(records, "records card must exist");
      assert.ok(!records.includes("MYSQL_DOWN"), "truth must not be exposed by default");
      // 切到评测视图后才显示真值。
      await page.getByText("评测视图（显示真值与判卷结果）").click();
      await page.getByText("真值：", { exact: false }).first().waitFor();
      await page.screenshot({
        path: path.join(output, "console-evaluation-mock-1440.png"),
        fullPage: true,
      });
      await noOverflow(page);
    },
  );
  assert.deepEqual(pageErrors, []);
  assert.deepEqual(transportErrors, []);
  results.push({ name: "No browser runtime errors", result: "PASS" });
}

run()
  .catch(async (error) => {
    console.error(error);
    results.push({ name: error.message, result: "FAIL" });
    if (activePage && !activePage.isClosed()) {
      console.error(
        (await activePage.locator("body").innerText()).slice(-2500),
      );
      await activePage.screenshot({
        path: path.join(output, "console-test-failure.png"),
        fullPage: true,
      });
    }
    process.exitCode = 1;
  })
  .finally(async () => {
    await fs.writeFile(
      path.join(output, "console-browser-report.json"),
      JSON.stringify(
        {
          captured_at: new Date().toISOString(),
          real_policy: "READ_ONLY",
          mock_policy:
            "TEST/MOCK isolated API and explicitly synthetic error responses",
          results,
        },
        null,
        2,
      ),
    );
    closing = true;
    await browser?.close();
  });
