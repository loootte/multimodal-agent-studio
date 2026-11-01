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
//   api.draft             只给内置、声明了 ui.panel.local_draft、并且是从对话里（tool.panel 事件）打开的面板：
//     .handle             这份本地草稿的句柄
//     .read()             → 草稿正文（本机网页 ↔ 宿主，不经对话模型）
//     .save(text)         原句柄保存编辑后的正文
//     .generate({text, count, aspect_ratio, backend})  先保存再按这枚句柄出图（宿主出图路径，规则同表单 privacy=local）
//   已安装工具的面板（iframe）永远没有 draft，FRAME_CALLS 里也没有。
//
// ui.panel.launcher=chat 的面板不在侧栏加入口，只能由对话里的 tool.panel 事件经 MASPanels.openFor 打开。
//
// 内置工具的面板同源加载：<script src="/api/tools/<name>/ui/<file>" data-tool="<name>">，
// 面板调用 MASPanels.define(mount)，宿主按 data-tool 认领。已安装（第三方）工具的面板只在
// <iframe sandbox="allow-scripts"> 里运行（不同源、没有 Cookie、CSP 禁止联网），api 经 postMessage 转过来。
(() => {
  const registry = new Map();
  const waiting = new Map();
  const HANDLE = /^(c|a|art)_[0-9a-f]{32}$/;
  const FIELD = /^[a-z][a-z0-9_]{0,47}$/;
  const SESSION = /^s_[0-9a-f]{32}$/;
  const DRAFT_MAX = 8000;
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
    // 宿主页面（app.js）在收到 tool.panel 事件时调用；context 只认 {handle, session_id}。
    openFor(name, context) {
      if (typeof name !== "string" || !name) return;
      const ctx = context && typeof context === "object" ? context : {};
      const handle = HANDLE.test(String(ctx.handle || "")) && String(ctx.handle).startsWith("c_") ? String(ctx.handle) : "";
      const sessionId = SESSION.test(String(ctx.session_id || "")) ? String(ctx.session_id) : "";
      open({ name, title: name }, handle && sessionId ? { handle, sessionId } : null);
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

  function draftText(text) {
    const value = typeof text === "string" ? text.trim() : "";
    if (!value) throw new Error("提示词不能是空的。");
    if (value.length > DRAFT_MAX) throw new Error(`提示词最多 ${DRAFT_MAX} 个字。`);
    return value;
  }

  function makeDraft(tool, draft) {
    const path = `/api/sessions/${draft.sessionId}/tools/${encodeURIComponent(tool.name)}/drafts/${draft.handle}`;
    return Object.freeze({
      handle: draft.handle,
      async read() {
        const data = await call("GET", path);
        return typeof data.text === "string" ? data.text : "";
      },
      async save(text) {
        const data = await call("POST", path, { text: draftText(text) });
        return { ok: data.ok === true, size: data.size };
      },
      async generate(request) {
        const r = request && typeof request === "object" ? request : {};
        const n = Number(r.count);
        if (!Number.isInteger(n) || n < 1 || n > 20) throw new Error("张数要是 1 到 20。");
        const backend = r.backend === "remote" ? "remote" : "local";
        const payload = {
          text: draftText(r.text),
          count: n,
          aspect_ratio: typeof r.aspect_ratio === "string" ? r.aspect_ratio.slice(0, 10) : "1:1",
          backend,
        };
        if (!window.MASHost || typeof window.MASHost.runToolDraft !== "function") throw new Error("页面还没准备好。");
        if (window.MASHost.busy()) throw new Error("这一轮还没结束，等它结束再出图（可以先保存）。");
        const where = backend === "local" ? "本机 ComfyUI" : "第三方出图";
        const label = `${tool.title}出图（本地提示词，${n} 张，${where}）`;
        dialog.close();
        return Boolean(await window.MASHost.runToolDraft(draft.sessionId, `${path}/generate`, payload, label));
      },
    });
  }

  function makeApi(tool, session, draft) {
    const base = `/api/tools/${encodeURIComponent(tool.name)}`;
    const rated = tool.rating === "adult";
    const extra = draft && tool.panel && tool.panel.local_draft === true && !tool.panel.isolated && tool.builtin === true
      ? { draft: makeDraft(tool, draft) } : {};
    return Object.freeze({
      ...extra,
      tool: toolInfo(tool),
      catalog: () => call("GET", `${base}/catalog`),
      preview: (fields) => call("POST", `${base}/preview`, { fields: cleanFields(fields) }),
      async run(request) {
        const r = request && typeof request === "object" ? request : {};
        const n = Number(r.count);
        if (!Number.isInteger(n) || n < 1 || n > 20) throw new Error("张数要是 1 到 20。");
        const backend = r.backend === "remote" ? "remote" : "local";
        if ((rated || tool.local_only) && backend !== "local") throw new Error(`“${tool.label || tool.title}”只能用本机 ComfyUI 出图。`);
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

  async function lookup(name) {
    const tools = (await call("GET", "/api/tools")).form_tools || [];
    return tools.find((item) => item.name === name) || null;
  }

  function showBlocked(tool, text) {
    say("");
    body.replaceChildren();
    active = { tool, cleanup: null };
    dialog.setAttribute("aria-label", (tool.panel && (tool.panel.title || tool.title)) || tool.title || "工具");
    if (!dialog.open) dialog.showModal();
    say(text || "这个工具现在不可用。");
  }

  // 同步收尾：跑面板自己的 cleanup，清空正文。dialog 的 close 事件是异步派发的，
  // 不能靠它在「关掉旧的、马上打开新的」时收尾，否则它会晚到、把刚挂上的新面板清空（#101）。
  function teardown() {
    if (active && typeof active.cleanup === "function") {
      try { active.cleanup(); } catch (_error) { /* 面板自己的收尾出错不影响关闭 */ }
    }
    active = null;
    body.replaceChildren();
    say("");
  }

  let opening = 0; // 每次 open 领一个号；await 回来发现号变了（又开了别的/用户关了）就放弃，不往正文里挂旧面板
  let selfClosed = 0; // open() 自己关掉旧面板的次数；对应的 close 事件晚到时只认领、不收尾

  async function open(tool, draft) {
    const ticket = ++opening;
    if (dialog.open) {
      selfClosed += 1;
      dialog.close();
    }
    teardown();
    let fresh = tool;
    try {
      fresh = (await lookup(tool.name)) || tool;
    } catch (_error) {
      fresh = tool;
    }
    if (ticket !== opening) return;
    if (!fresh.panel || fresh.unavailable) {
      showBlocked(fresh, fresh.unavailable || "这个工具现在不可用。");
      return;
    }
    const session = { confirmed: false };
    if (fresh.rating === "adult") {
      if (!window.confirm(fresh.confirm || `开启“${fresh.label || fresh.title}”？`)) return;
      session.confirmed = true;
    }
    if (ticket !== opening) return;
    say("");
    body.replaceChildren();
    if (fresh.panel.launcher === "chat" && !draft) {
      showBlocked(fresh, "这个面板只能从对话里打开。");
      return;
    }
    const api = makeApi(fresh, session, draft);
    const current = { tool: fresh, cleanup: null, ticket };
    active = current;
    dialog.setAttribute("aria-label", fresh.panel.title || fresh.title);
    if (!dialog.open) dialog.showModal();
    try {
      if (fresh.panel.isolated) {
        current.cleanup = mountFrame(fresh, api);
      } else {
        loadStyle(fresh.panel.style);
        await loadScript(fresh);
        if (ticket !== opening || active !== current || !dialog.open) return;
        const result = registry.get(fresh.name)(body, api);
        if (typeof result === "function") current.cleanup = result;
      }
    } catch (error) {
      if (ticket === opening) say(error.message || "面板打不开。");
    }
  }

  dialog.addEventListener("close", () => {
    if (selfClosed > 0) {
      selfClosed -= 1; // open() 换面板时自己关的，已经同步收尾过
      return;
    }
    if (dialog.open) return; // 保险：新面板已经打开了，别清掉它
    opening += 1; // 用户关掉后，还在加载中的 open 不再挂上来
    teardown();
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
      if (tool.panel.launcher === "chat") continue; // 对话唤起的面板没有侧栏入口
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

  document.querySelector("#close-tool-panel").addEventListener("click", () => dialog.close());
  document.addEventListener("mas:tools-changed", refresh);
  refresh();
})();
