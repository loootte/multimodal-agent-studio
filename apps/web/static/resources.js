// 本地资源面板（issue #51）。列表和预览只在本机浏览器里，不进对话、不发给模型。
(() => {
  const dialog = document.querySelector("#resources");
  const list = document.querySelector("#res-list");
  const total = document.querySelector("#res-total");
  const typeSel = document.querySelector("#res-type");
  const sessionSel = document.querySelector("#res-session");
  const search = document.querySelector("#res-q");
  const preview = document.querySelector("#res-preview");
  const resNote = document.querySelector("#res-note");
  const TYPE_LABEL = { prompt: "本地提示词", text: "本地文字", file: "本地文件", attachment: "附件", artifact: "生成文件" };
  let items = [];
  const selected = new Set();

  function size(n) {
    if (n >= 1048576) return (n / 1048576).toFixed(1) + " MB";
    if (n >= 1024) return (n / 1024).toFixed(1) + " KB";
    return n + " B";
  }
  function when(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    return isNaN(d) ? iso : d.toLocaleString();
  }
  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function say(text) {
    resNote.textContent = text || "";
    resNote.hidden = !text;
  }
  function rawUrl(item, download) {
    return `/api/resources/raw?id=${encodeURIComponent(item.id)}${download ? "&download=1" : ""}`;
  }

  async function load() {
    const params = new URLSearchParams();
    if (typeSel.value) params.set("type", typeSel.value);
    if (sessionSel.value) params.set("session", sessionSel.value);
    if (search.value.trim()) params.set("q", search.value.trim());
    let payload;
    try {
      payload = await api("GET", `/api/resources?${params}`);
    } catch (error) {
      say(error.message);
      return;
    }
    items = payload.items || [];
    for (const id of [...selected]) if (!items.some((i) => i.id === id)) selected.delete(id);
    const t = payload.total || {};
    total.textContent = `共 ${t.count || 0} 项，占用 ${size(t.bytes || 0)}（文件字节 ${size(t.blobs || 0)}，文字 ${size(t.texts || 0)}，生成文件 ${size(t.generated || 0)}）`;
    const keep = sessionSel.value;
    sessionSel.replaceChildren(new Option("全部会话", ""));
    for (const s of payload.sessions || []) sessionSel.add(new Option(s.title || s.session_id, s.session_id));
    sessionSel.value = keep;
    render();
  }

  function render() {
    list.replaceChildren();
    if (!items.length) {
      list.append(el("li", "hint", "没有本地资源。"));
      return;
    }
    for (const item of items) {
      const li = el("li", "res-item");
      const box = el("input");
      box.type = "checkbox";
      box.checked = selected.has(item.id);
      box.setAttribute("aria-label", "选择");
      box.addEventListener("change", () => (box.checked ? selected.add(item.id) : selected.delete(item.id)));
      const main = el("div", "res-main");
      const title = item.label || item.name || item.handle;
      main.append(el("strong", "", title));
      const sessions = (item.sessions || []).map((s) => s.title || s.session_id).join("、");
      const meta = `${TYPE_LABEL[item.type] || item.type} · ${size(item.size || 0)} · ${when(item.created_at)} · 会话：${sessions}`;
      main.append(el("div", "hint", meta));
      const flags = [];
      if (item.running) flags.push("会话正在回答");
      if (item.shared) flags.push("字节被多个会话共用");
      if (item.mentions) flags.push(`会话里提到 ${item.mentions} 次`);
      if (item.state === "expired") flags.push("句柄已过期（复用会换新句柄）");
      else if (item.state === "revoked") flags.push("句柄已撤销（复用会换新句柄）");
      else if (item.state === "used") flags.push("句柄已用过");
      else if (item.expires_at) flags.push(`句柄有效到 ${when(item.expires_at)}`);
      if (flags.length) main.append(el("div", "hint", flags.join(" · ")));
      const actions = el("div", "res-actions");
      const button = (label, fn) => {
        const b = el("button", "ghost", label);
        b.type = "button";
        b.addEventListener("click", fn);
        actions.append(b);
      };
      button("查看", () => show(item));
      button("标签", () => relabel(item));
      button("下载", () => { window.location.href = rawUrl(item, true); });
      if (["prompt", "text", "file"].includes(item.type)) button("用在当前对话", () => reuse(item));
      if (["prompt", "text", "file"].includes(item.type) && item.state === "active") button("撤销句柄", () => revoke(item));
      button("删除", () => remove([item.id]));
      li.append(box, main, actions);
      list.append(li);
    }
  }

  async function show(item) {
    preview.replaceChildren();
    preview.hidden = false;
    preview.append(el("div", "hint", `预览（只在本机）：${item.handle}`));
    if (item.type === "prompt" || item.type === "text") {
      try {
        const payload = await api("GET", `/api/resources/preview?id=${encodeURIComponent(item.id)}`);
        preview.append(el("pre", "res-text", payload.text));
      } catch (error) {
        say(error.message);
      }
      return;
    }
    const media = item.media_type || "";
    if (media.startsWith("image/")) {
      const img = el("img");
      img.src = rawUrl(item);
      img.alt = item.label || item.name || "";
      preview.append(img);
    } else if (media.startsWith("video/")) {
      const v = el("video");
      v.src = rawUrl(item);
      v.controls = true;
      preview.append(v);
    } else if (media.startsWith("audio/")) {
      const a = el("audio");
      a.src = rawUrl(item);
      a.controls = true;
      preview.append(a);
    } else {
      const link = el("a", "", "在新标签页打开");
      link.href = rawUrl(item);
      link.target = "_blank";
      link.rel = "noopener";
      preview.append(link);
    }
  }

  async function relabel(item) {
    const label = window.prompt("标签（只在本机显示，留空清除）", item.label || "");
    if (label === null) return;
    try {
      await api("POST", "/api/resources/label", { id: item.id, label });
      await load();
    } catch (error) {
      say(error.message);
    }
  }

  async function remove(ids, confirmed) {
    if (!ids.length) return say("先勾选要删的资源。");
    if (!confirmed && !window.confirm(`删除 ${ids.length} 项本地资源？删除后正文从磁盘和轨迹中清除，不能恢复。`)) return;
    try {
      const result = await api("POST", "/api/resources/delete", { ids, confirm: !!confirmed });
      const refused = (result.refused || []).length;
      say(`已删除 ${result.deleted.length} 项${refused ? `，${refused} 项所在会话正在回答，没删` : ""}。`);
      ids.forEach((id) => selected.delete(id));
      preview.hidden = true;
      await load();
      if (state.currentId && typeof openSession === "function") openSession(state.currentId).catch(() => {});
    } catch (error) {
      const payload = error.payload || {};
      if (error.code === "needs_confirm") {
        const lines = Object.values(payload.warnings || {}).flat();
        if (window.confirm(`${error.message}\n\n${[...new Set(lines)].join("\n")}\n\n仍然删除？`)) return remove(ids, true);
        return;
      }
      say(error.message);
    }
  }

  async function revoke(item) {
    try {
      await api("POST", "/api/resources/revoke", { id: item.id });
      say(`句柄 ${item.handle} 已撤销：正文还在本机，这个句柄不能再用来出图。`);
      load();
    } catch (error) {
      say(error.message);
    }
  }

  async function reuse(item) {
    if (!state.currentId) return say("先打开一个对话。");
    try {
      const result = await api("POST", "/api/resources/reuse", { id: item.id, session_id: state.currentId });
      input.value = (input.value ? input.value + "\n" : "") + result.snippet;
      say(`已放进当前对话：句柄 ${result.handle}。模型只看到句柄，看不到正文。`);
      dialog.close();
      input.focus();
    } catch (error) {
      say(error.message);
    }
  }

  document.querySelector("#open-resources").addEventListener("click", () => {
    say("");
    preview.hidden = true;
    dialog.showModal();
    load();
  });
  document.querySelector("#close-resources").addEventListener("click", () => dialog.close());
  document.querySelector("#res-tools").addEventListener("click", () => {
    dialog.close();
    document.querySelector("#open-tools").click();
  });
  document.querySelector("#res-select-all").addEventListener("click", () => {
    const all = items.every((i) => selected.has(i.id));
    items.forEach((i) => (all ? selected.delete(i.id) : selected.add(i.id)));
    render();
  });
  document.querySelector("#res-delete-selected").addEventListener("click", () => remove([...selected]));
  typeSel.addEventListener("change", load);
  sessionSel.addEventListener("change", load);
  let timer = null;
  search.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(load, 250);
  });
})();
