// 浏览器端到端：#101 读图提示词 → 对话唤起本机编辑面板，面板里要有带正文的可编辑文本框，保存 / 再打开 / 出图都能用。
// 用法：CHROME=/usr/bin/google-chrome PYTHON=python3 node apps/web/e2e/prompt_panel.cjs
// 自己起 apps/web/e2e/prompt_panel_serve.py（假供应商 + 假 ComfyUI + 临时数据目录），不碰正在用的服务。
// 需要 puppeteer-core（npm i puppeteer-core，或 NODE_PATH 指向已装的那份；不进仓库依赖）。
const { spawn } = require("child_process");
const path = require("path");
const puppeteer = require("puppeteer-core");
const CHROME = process.env.CHROME || "/usr/bin/google-chrome";
const PYTHON = process.env.PYTHON || "python3";

function fail(message) {
  throw new Error(message);
}

function startServer() {
  return new Promise((resolve, reject) => {
    const child = spawn(PYTHON, [path.join(__dirname, "prompt_panel_serve.py")], { stdio: ["pipe", "pipe", "pipe"] });
    const info = {};
    let out = "";
    let err = "";
    const timer = setTimeout(() => reject(new Error("假环境没起来：" + err.slice(-500))), 30000);
    child.stdout.on("data", (chunk) => {
      out += chunk.toString();
      for (const line of out.split("\n")) {
        const at = line.indexOf("=");
        if (at > 0) info[line.slice(0, at)] = line.slice(at + 1).trim();
      }
      if (info.MAS_URL && info.PNG && info.SECRET) {
        clearTimeout(timer);
        resolve({ child, info });
      }
    });
    child.stderr.on("data", (chunk) => { err += chunk.toString(); });
    child.on("exit", (code) => reject(new Error(`假环境退出了（${code}）：` + err.slice(-500))));
  });
}

(async () => {
  const { child, info } = await startServer();
  const browser = await puppeteer.launch({ executablePath: CHROME, headless: "new", args: ["--no-sandbox"] });
  const page = await browser.newPage();
  await page.setViewport({ width: 1200, height: 1000 });
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => { if (message.type() === "error") errors.push("console " + message.text() + " @" + JSON.stringify(message.location())); });
  page.on("response", (response) => { if (response.status() >= 400) errors.push(`http ${response.status()} ${new URL(response.url()).pathname}`); });
  page.on("dialog", async (dialog) => { errors.push("native " + dialog.type()); await dialog.dismiss(); });
  const panelText = "#tool-panel[open] #tool-panel-body textarea.pfi-text";
  try {
    await page.goto(info.MAS_URL + "/", { waitUntil: "networkidle0" });
    // 侧栏没有它的入口
    if (await page.$('#tool-panels [data-tool="prompt_from_image"]')) fail("读图提示词出现在侧栏");
    await page.type("#input", "从这张图读出提示词。");
    await page.click("#send");
    const picker = await page.waitForSelector(".local-entry-file input[type=file]", { timeout: 15000 });
    await picker.uploadFile(info.PNG);
    await page.click(".local-entry-file button[type=submit]");
    // 1. 自动打开面板，文本框里是读出的正文，可编辑
    await page.waitForFunction((selector, secret) => {
      const node = document.querySelector(selector);
      return node && !node.disabled && node.value === secret;
    }, { timeout: 15000 }, panelText, info.SECRET).catch(async () => {
      const state = await page.evaluate(() => ({
        open: document.querySelector("#tool-panel").open,
        body: document.querySelector("#tool-panel-body").innerText.slice(0, 300),
        note: document.querySelector("#tool-panel-note").innerText,
        reopen: document.querySelectorAll(".tool-panel-reopen").length,
        last: [...document.querySelectorAll(".bubble.assistant .body")].map((n) => n.innerText).slice(-1)[0] || "",
      }));
      fail("读图后面板里没有带正文的可编辑文本框：" + JSON.stringify(state) + " 页面错误：" + errors.join(" | "));
    });
    if (process.env.SHOT) await page.screenshot({ path: process.env.SHOT });
    const box = await page.$eval(panelText, (node) => {
      const r = node.getBoundingClientRect();
      const style = getComputedStyle(node);
      const top = document.elementFromPoint(r.left + r.width / 2, r.top + Math.min(10, r.height / 2));
      return { w: r.width, h: r.height, x: r.left, y: r.top, vis: style.visibility, disp: style.display, op: style.opacity, onTop: top === node,
               vw: innerWidth, vh: innerHeight };
    });
    if (!(box.w > 100 && box.h > 40 && box.vis === "visible" && box.disp !== "none" && Number(box.op) > 0.5 && box.onTop
          && box.x >= 0 && box.y >= 0 && box.x + box.w <= box.vw && box.y < box.vh)) fail("文本框看不见：" + JSON.stringify(box));
    // 1b. 这一轮结束（含结束后的接回/回放）以后，面板还开着、文本框还在：回放不会再弹一次把它清空
    await page.waitForFunction(() => document.querySelector("#stop").disabled, { timeout: 15000 });
    await new Promise((resolve) => setTimeout(resolve, 800));
    const after = await page.$eval("#tool-panel-body", (node) => ({
      count: node.querySelectorAll("textarea.pfi-text").length,
      value: (node.querySelector("textarea.pfi-text") || {}).value || "",
    }));
    if (after.count !== 1 || after.value !== info.SECRET) fail("这一轮结束后面板被清空或重复了：" + JSON.stringify({ count: after.count, same: after.value === info.SECRET }));
    // 2. 改了保存：面板提示已保存，服务端读回是新正文
    const edited = "E2E-EDITED-91aa silver fox on a snowy ridge";
    await page.$eval(panelText, (node) => { node.value = ""; });
    await page.type(panelText, edited);
    await page.click("#tool-panel-body .dialog-actions button.ghost");
    await page.waitForFunction(() => /已保存到本地/.test(document.querySelector("#tool-panel-body").innerText), { timeout: 5000 });
    // 3. 关掉再用回复下面的按钮打开：读到的是保存后的正文
    await page.click("#close-tool-panel");
    await page.waitForSelector(".tool-panel-reopen", { timeout: 5000 });
    await page.click(".tool-panel-reopen");
    await page.waitForFunction((selector, text) => {
      const node = document.querySelector(selector);
      return node && !node.disabled && node.value === text;
    }, { timeout: 8000 }, panelText, edited);
    // 3b. 连着打开几次（旧面板 close 事件晚到）也只剩一个带正文的文本框
    await page.evaluate(() => {
      const button = document.querySelector(".tool-panel-reopen");
      button.click();
      button.click();
      setTimeout(() => button.click(), 0);
    });
    await page.waitForFunction((selector, text) => {
      const node = document.querySelector(selector);
      return node && !node.disabled && node.value === text;
    }, { timeout: 10000 }, panelText, edited).catch(() => {});
    await new Promise((resolve) => setTimeout(resolve, 500));
    const again = await page.$eval("#tool-panel", (node) => {
      const area = node.querySelector("#tool-panel-body textarea.pfi-text");
      return {
        open: node.open,
        count: node.querySelectorAll("#tool-panel-body textarea.pfi-text").length,
        value: (area || {}).value || "",
        disabled: area ? area.disabled : null,
        note: (node.querySelector("#tool-panel-body .note") || {}).textContent || "",
        host: (document.querySelector("#tool-panel-note") || {}).textContent || "",
      };
    });
    if (!again.open || again.count !== 1 || again.value !== edited) {
      fail("连着打开后面板不对：" + JSON.stringify({ open: again.open, count: again.count, same: again.value === edited,
        len: again.value.length, disabled: again.disabled, note: again.note, host: again.host }));
    }
    // 4. 出图：面板关掉，对话里出现本地提示词出图那一轮并出图
    await page.click("#tool-panel-body .dialog-actions button:not(.ghost)");
    await page.waitForFunction(() => !document.querySelector("#tool-panel").open, { timeout: 5000 });
    await page.waitForFunction(() => [...document.querySelectorAll(".bubble.user")].some((n) => n.innerText.includes("本地提示词")), { timeout: 8000 });
    await page.waitForFunction(() => document.querySelectorAll(".bubble.assistant img").length > 0 && !document.querySelector("#stop").disabled === false, { timeout: 20000 });
    const view = await page.evaluate(() => document.querySelector("#transcript").innerText);
    if (view.includes(info.SECRET) || view.includes(edited)) fail("对话里出现了提示词正文");
    // 5. 服务换了新代码（X-MAS-Build 变了）时，旧页面顶上提示刷新
    if (await page.$("#stale-page")) fail("构建没变也提示刷新了");
    await page.evaluate(() => noteBuild({ headers: { get: () => "e2e-other-build" } }));
    if (!(await page.$("#stale-page"))) fail("构建变了没有提示刷新");
    if (errors.length) fail("页面报错：" + errors.join(" | "));
    console.log("prompt panel e2e ok");
  } finally {
    await browser.close();
    child.stdin.end();
    child.kill();
  }
})().catch((error) => {
  console.error("FAIL", error.message);
  process.exit(1);
});
