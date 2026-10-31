// 工具面板宿主（ui.panel，见 docs/tool-code-contract.md「网页面板」）。这里没有任何具体工具的代码：
// 读 /api/tools 的 form_tools，凡是带 panel 的工具都在侧栏加一个入口，打开时把面板挂进通用对话框 #tool-panel。
//
// 面板接口（mount(el, api) 拿到的 api）：
//   api.tool              工具简介（name、title、rating、label、min_age、local_only、run_label），只读
//   api.catalog()         → 表单目录（工具 catalog() 的结果，宿主守卫过）
//   api.preview(fields)   → {prompt, summary, ...}（宿主守卫过）
//   api.run({fields, count, aspect_ratio, backend, privacy})  在当前会话里出图；宿主补上分级确认、强制只能本机
//   api.confirm(text)     → true/false（宿主弹的确认框）
//   api.notify(text)      在对话框底部显示一行宿主提示
//   api.close()           关掉面板
//   api.handle.is(value)  value 是不是本地内容句柄（面板只会见到句柄，拿不到本地内容本身）
//
// 内置工具的面板同源加载：<script src="/api/tools/<name>/ui/<file>" data-tool="<name>">，
// 面板调用 MASPanels.define(mount)，宿主按 data-tool 认领。已安装（第三方）工具的面板只在
// <iframe sandbox="allow-scripts"> 里运行（不同源、没有 Cookie、CSP 禁止联网），api 经 postMessage 转过来。
(() => {
  const registry = new Map();
  const waiting = new Map();
  const HANDLE = /^(c|a|art)_[0-9a-f]{32}$/;
  const FIELD = /^[a-z][a-z0-9_]{0,47}$/;
  const FRAME_CALLS = new Set(["catalog", "preview", "run", "confirm", "notify", "close"]);
  const dialog = document.querySelector("#tool-panel");
  const body = document.querySelector("#tool-panel-body");
  const hostNote = document.querySelector("#tool-panel-note");
  const launchers = document.querySelector("#tool-panels");
  let active = null;

  window.MASPanels = Object.freeze({
    define(mount) {
      const script = document.currentScript;
      const name = script && script.dataset ? script.dataset.tool : "";
      if (!name || typeof mount !== "function" || registry.has(name)) return;
      registry.set(name, mount);
      const done = waiting.get(name);
      if (done) done.resolve();
    },
  });

  async function call(method, path, payload) {
    const response = await fetch(path, {
      method,
      headers: payload ? { "Content-Type": "application/json" } : {},
      body: payload ? JSON.stringify(payload) : undefined,
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(data.message || "出错了。");
      error.code = data.code;
      throw error;
    }
    return data;
  }

  function say(text) {
    hostNote.textContent = text ? String(text).slice(0, 500) : "";
    hostNote.hidden = !text;
  }

  function cleanFields(fields) {
    const out = {};
    if (!fields || typeof fields !== "object") return out;
    for (const [key, value] of Object.entries(fields).slice(0, 64)) {
      if (FIELD.test(key) && typeof value === "string" && value.length <= 2000) out[key] = value;
    }
    return out;
  }

  function toolInfo(tool) {
    const keys = ["name", "title", "rating", "label", "min_age", "local_only", "run_label", "version"];
    const out = {};
    for (const key of keys) if (tool[key] !== undefined) out[key] = tool[key];
    return Object.freeze(out);
  }

  function makeApi(tool, session) {
    const base = `/api/tools/${encodeURIComponent(tool.name)}`;
    const rated = tool.rating === "adult";
    return Object.freeze({
      tool: toolInfo(tool),
      catalog: () => call("GET", `${base}/catalog`),
      preview: (fields) => call("POST", `${base}/preview`, { fields: cleanFields(fields) }),
      async run(request) {
        const r = request && typeof request === "object" ? request : {};
        const n = Number(r.count);
        if (!Number.isInteger(n) || n < 1 || n > 20) throw new Error("张数要是 1 到 20。");
        const backend = r.backend === "remote" ? "remote" : "local";
        if (tool.local_only && backend !== "local") throw new Error(`“${tool.label || tool.title}”只能用本机 ComfyUI 出图。`);
        const payload = {
          fields: cleanFields(r.fields),
          count: n,
          aspect_ratio: typeof r.aspect_ratio === "string" ? r.aspect_ratio.slice(0, 10) : "1:1",
          backend,
          privacy: r.privacy === "local" ? "local" : "chat",
        };
        if (rated) payload.confirmed = session.confirmed === true; // 只认宿主自己弹的确认
        const where = backend === "local" ? "本机 ComfyUI" : "第三方出图";
        const mode = rated ? `${tool.label}，` : "";
        const label = payload.privacy === "local"
          ? `${tool.title}出图（${mode}本地提示词，${n} 张，${where}）`
          : `${tool.title}出图（${mode}${n} 张，${where}）`;
        dialog.close();
        if (!window.MASHost || typeof window.MASHost.runFormTool !== "function") throw new Error("页面还没准备好。");
        return Boolean(await window.MASHost.runFormTool(tool.name, payload, label));
      },
      confirm: (text) => window.confirm(String(text || "").slice(0, 1000)),
      notify: (text) => say(text),
      close: () => dialog.close(),
      handle: Object.freeze({ is: (value) => HANDLE.test(String(value || "")) }),
    });
  }

  function loadStyle(url) {
    if (!url || document.querySelector(`link[data-panel-style="${CSS.escape(url)}"]`)) return;
    const link = document.createElement("link");
    link.rel = "stylesheet";
    link.href = url;
    link.dataset.panelStyle = url;
    document.head.append(link);
  }

  function loadScript(tool) {
    if (registry.has(tool.name)) return Promise.resolve();
    if (waiting.has(tool.name)) return waiting.get(tool.name).promise;
    const entry = {};
    entry.promise = new Promise((resolve, reject) => {
      entry.resolve = resolve;
      const script = document.createElement("script");
      script.src = tool.panel.script;
      script.dataset.tool = tool.name;
      script.addEventListener("error", () => {
        waiting.delete(tool.name);
        script.remove();
        reject(new Error("面板脚本加载失败。"));
      });
      script.addEventListener("load", () => {
        if (!registry.has(tool.name)) {
          waiting.delete(tool.name);
          reject(new Error("面板没有调用 MASPanels.define。"));
        }
      });
      document.body.append(script);
    });
    waiting.set(tool.name, entry);
    return entry.promise;
  }

  function mountFrame(tool, api) {
    const frame = document.createElement("iframe");
    frame.setAttribute("sandbox", "allow-scripts");
    frame.setAttribute("referrerpolicy", "no-referrer");
    frame.className = "tool-panel-frame";
    frame.title = tool.panel.title || tool.title;
    const listener = (event) => {
      if (event.source !== frame.contentWindow) return;
      const data = event.data;
      if (!data || typeof data !== "object") return;
      if (data.mas === "panel-ready") {
        frame.contentWindow.postMessage({ mas: "panel-init", tool: { ...api.tool } }, "*");
      } else if (data.mas === "panel-size") {
        const height = Math.max(120, Math.min(2000, Number(data.height) || 0));
        frame.style.height = `${height}px`;
      } else if (data.mas === "panel-call" && FRAME_CALLS.has(data.method)) {
        const args = Array.isArray(data.args) ? data.args.slice(0, 2) : [];
        Promise.resolve().then(() => api[data.method](...args)).then(
          (result) => frame.contentWindow && frame.contentWindow.postMessage({ mas: "panel-reply", id: data.id, ok: true, result }, "*"),
          (error) => frame.contentWindow && frame.contentWindow.postMessage(
            { mas: "panel-reply", id: data.id, ok: false, error: { code: error.code || "error", message: error.message || "出错了。" } }, "*"),
        );
      }
    };
    window.addEventListener("message", listener);
    frame.src = tool.panel.frame;
    body.append(frame);
    return () => window.removeEventListener("message", listener);
  }

  async function open(tool) {
    if (active) dialog.close();
    const session = { confirmed: false };
    if (tool.rating === "adult") {
      if (!window.confirm(tool.confirm || `开启“${tool.label || tool.title}”？`)) return;
      session.confirmed = true;
    }
    say("");
    body.replaceChildren();
    const api = makeApi(tool, session);
    active = { tool, cleanup: null };
    dialog.setAttribute("aria-label", tool.panel.title || tool.title);
    dialog.showModal();
    try {
      if (tool.panel.isolated) {
        active.cleanup = mountFrame(tool, api);
      } else {
        loadStyle(tool.panel.style);
        await loadScript(tool);
        const result = registry.get(tool.name)(body, api);
        if (typeof result === "function") active.cleanup = result;
      }
    } catch (error) {
      say(error.message || "面板打不开。");
    }
  }

  dialog.addEventListener("close", () => {
    if (active && typeof active.cleanup === "function") {
      try { active.cleanup(); } catch (_error) { /* 面板自己的收尾出错不影响关闭 */ }
    }
    active = null;
    body.replaceChildren();
    say("");
  });

  async function refresh() {
    let tools = [];
    try {
      tools = (await call("GET", "/api/tools")).form_tools || [];
    } catch (_error) {
      tools = [];
    }
    launchers.replaceChildren();
    for (const tool of tools) {
      if (!tool.panel || tool.unavailable) continue;
      const button = document.createElement("button");
      button.type = "button";
      button.className = "ghost";
      button.dataset.tool = tool.name;
      const title = tool.panel.title || tool.title;
      button.textContent = tool.rating === "adult" ? `${title}（${tool.label || "成人"}，仅本机）` : title;
      button.addEventListener("click", () => open(tool));
      launchers.append(button);
    }
  }

  document.addEventListener("mas:tools-changed", refresh);
  refresh();
})();
