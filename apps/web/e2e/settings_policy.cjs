// 浏览器端到端：设置页「工具安全策略」和工具配置的保存路径（#93）。
// 用法：先起服务（python apps/web/server.py --port 18799，CHAT_DATA_DIR 指向临时目录），再
//   MAS_URL=http://127.0.0.1:18799 CHROME=/usr/bin/google-chrome node apps/web/e2e/settings_policy.cjs
// 需要 puppeteer-core（npm i puppeteer-core，不进仓库依赖）。会改并恢复该服务的策略和工具配置，别对正在用的服务跑。
const puppeteer = require("puppeteer-core");
const BASE = process.env.MAS_URL || "http://127.0.0.1:18799";
const CHROME = process.env.CHROME || "/usr/bin/google-chrome";

function fail(message) {
  throw new Error(message);
}

(async () => {
  const browser = await puppeteer.launch({ executablePath: CHROME, headless: "new", args: ["--no-sandbox"] });
  const page = await browser.newPage();
  await page.setViewport({ width: 1200, height: 1400 });
  const errors = [];
  const puts = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("dialog", async (dialog) => { errors.push("native " + dialog.type()); await dialog.dismiss(); });
  page.on("response", (response) => {
    const request = response.request();
    if (request.method() === "PUT") puts.push(`${response.status()} ${new URL(response.url()).pathname} ${request.postData() || ""}`);
  });
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const policy = async () => (await page.evaluate(async () => (await fetch("/api/tool-policy")).json())).policy;
  const openSettings = async () => {
    await page.click("#open-settings");
    await page.waitForFunction(() => /默认值|变化|已保存|已恢复/.test(document.querySelector("#tool-policy-hint").textContent));
  };
  const mainSave = () => page.evaluate(() => document.querySelector('#settings-form button[type="submit"]').click());
  try {
    await page.goto(BASE + "/", { waitUntil: "networkidle0" });
    await page.evaluate(async () => fetch("/api/tool-policy", { method: "PUT", headers: { "Content-Type": "application/json" }, body: '{"reset":true}' }));

    // 1. 用户路径：表单里改内容审核 → 点设置页底部主「保存」→ 必须先 409、弹页内确认
    await openSettings();
    await page.select("select[name=pol_content_guard]", "off");
    puts.length = 0;
    await mainSave();
    await page.waitForSelector("#policy-ack[open]", { timeout: 5000 });
    if (!puts.some((line) => line.startsWith("409 /api/tool-policy"))) fail("主保存没有先拿到 409：" + puts.join(" | "));
    const listed = await page.$eval("#policy-ack-fields", (node) => node.textContent);
    if (!listed.includes("global.content_guard")) fail("确认弹窗没列出 global.content_guard：" + listed);
    // 1a. 取消：不保存，设置页留着，看得到原因
    await page.click("#policy-ack-cancel");
    await page.waitForFunction(() => !document.querySelector("#settings-error").hidden);
    const error = await page.$eval("#settings-error", (node) => node.textContent);
    if (!(await page.$eval("#settings", (node) => node.open)) || !error.includes("409")) fail("取消后没留在设置页或没提示：" + error);
    if ((await policy()).global.content_guard) fail("取消后策略被改了");
    // 1b. 再点主保存 → 确认 → 关闭设置页、出现保存提示；再打开是新值
    await mainSave();
    await page.waitForSelector("#policy-ack[open]", { timeout: 5000 });
    await page.click("#policy-ack-ok");
    await page.waitForFunction(() => !document.querySelector("#save-toast").hidden, { timeout: 5000 });
    const toast = await page.$eval("#save-toast", (node) => node.textContent);
    if (!toast.includes("工具安全策略已保存") || !toast.includes("global.content_guard")) fail("保存提示不对：" + toast);
    if ((await policy()).global.content_guard !== "off") fail("确认后没保存 content_guard=off");
    await openSettings();
    if ((await page.$eval("select[name=pol_content_guard]", (node) => node.value)) !== "off") fail("再次打开设置看到的不是保存后的值");
    // 1c. 什么都不改再点主保存：不发策略 PUT，提示没有改动
    puts.length = 0;
    await mainSave();
    await page.waitForFunction(() => !document.querySelector("#save-toast").hidden && document.querySelector("#save-toast").textContent.includes("没有改动"));
    if (puts.some((line) => line.includes("/api/tool-policy"))) fail("没改策略也发了 PUT");
    // 1d. 区块内「保存策略」按钮：恢复默认
    await openSettings();
    await page.click("#tool-policy-reset");
    await page.waitForFunction(() => document.querySelector("#tool-policy-hint").textContent.includes("已恢复默认"));
    await page.keyboard.press("Escape");

    // 2. 工具配置：改字段保存 → 页面提示已保存；再打开是新值；generate_image 的 remote_local_prompt=confirm 走页内 409 确认
    await page.click("#open-tools");
    await sleep(500);
    await page.select("#tool-select", "generate_image");
    await sleep(500);
    await page.select("#tool-fields [name=remote_local_prompt]", "confirm");
    puts.length = 0;
    await page.click("#tool-save");
    await page.waitForSelector("#policy-ack[open]", { timeout: 5000 });
    if (!puts.some((line) => line.startsWith("409 /api/tools/generate_image/config"))) fail("工具配置放宽没 409：" + puts.join(" | "));
    await page.click("#policy-ack-ok");
    await page.waitForFunction(() => document.querySelector("#tool-note").textContent.includes("已保存"));
    await page.click("#close-tools");
    await page.click("#open-tools");
    await sleep(500);
    await page.select("#tool-select", "generate_image");
    await sleep(500);
    if ((await page.$eval("#tool-fields [name=remote_local_prompt]", (node) => node.value)) !== "confirm") fail("工具配置再次打开不是新值");
    await page.click("#tool-save");
    await page.waitForFunction(() => document.querySelector("#tool-note").textContent.includes("没有变化"));
    await page.click("#tool-reset");
    await page.waitForFunction(() => document.querySelector("#tool-note").textContent.includes("已恢复默认值"));
    if (errors.length) fail("页面报错：" + errors.join(" | "));
    console.log("settings policy e2e ok");
  } finally {
    await page.evaluate(async () => fetch("/api/tool-policy", { method: "PUT", headers: { "Content-Type": "application/json" }, body: '{"reset":true}' })).catch(() => {});
    await browser.close();
  }
})().catch((error) => {
  console.error("FAIL", error.message);
  process.exit(1);
});
