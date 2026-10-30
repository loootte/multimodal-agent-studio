// 沙箱 iframe 里的面板桥（宿主自己的代码）。/api/tools/<name>/ui/frame 把它和第三方面板脚本按哈希内联进同一个文档。
// 面板照样调用 MASPanels.define(mount)，拿到的 api 和同源面板一样，只是每个调用都经 postMessage 交给宿主页面
// （apps/web/static/panels.js）去做。iframe 不同源、没有 Cookie、CSP 禁止联网，面板没有别的路可走。
(() => {
  const host = window.parent;
  const HANDLE = /^(c|a|art)_[0-9a-f]{32}$/;
  const pending = new Map();
  let seq = 0;
  let info = null;
  let mountFn = null;
  let started = false;

  function call(method, ...args) {
    return new Promise((resolve, reject) => {
      const id = ++seq;
      pending.set(id, { resolve, reject });
      host.postMessage({ mas: "panel-call", id, method, args }, "*");
    });
  }

  window.addEventListener("message", (event) => {
    if (event.source !== host) return;
    const data = event.data;
    if (!data || typeof data !== "object") return;
    if (data.mas === "panel-init" && !info && data.tool && typeof data.tool === "object") {
      info = Object.freeze({ ...data.tool });
      start();
    } else if (data.mas === "panel-reply" && pending.has(data.id)) {
      const waiter = pending.get(data.id);
      pending.delete(data.id);
      if (data.ok) {
        waiter.resolve(data.result);
      } else {
        const error = new Error((data.error && data.error.message) || "出错了。");
        error.code = data.error && data.error.code;
        waiter.reject(error);
      }
    }
  });

  window.MASPanels = Object.freeze({
    define(mount) {
      if (typeof mount !== "function" || mountFn) return;
      mountFn = mount;
      start();
    },
  });

  function start() {
    if (started || !info || !mountFn) return;
    started = true;
    const api = Object.freeze({
      tool: info,
      catalog: () => call("catalog"),
      preview: (fields) => call("preview", fields),
      run: (request) => call("run", request),
      confirm: (text) => call("confirm", String(text || "")),
      notify: (text) => call("notify", String(text || "")),
      close: () => call("close"),
      handle: Object.freeze({ is: (value) => HANDLE.test(String(value || "")) }),
    });
    mountFn(document.getElementById("mas-panel-root"), api);
    const report = () => host.postMessage({ mas: "panel-size", height: document.documentElement.scrollHeight }, "*");
    if (window.ResizeObserver) new ResizeObserver(report).observe(document.body);
    report();
  }

  host.postMessage({ mas: "panel-ready" }, "*");
})();
