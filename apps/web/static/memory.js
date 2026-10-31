// 长期记忆面板（14.D #81）。列表、预览只在本机浏览器里；面板里的写入是用户自己的操作，不经模型。
(() => {
  const dialog = document.querySelector("#memory");
  const form = document.querySelector("#memory-settings");
  const list = document.querySelector("#memory-list");
  const storage = document.querySelector("#memory-storage");
  const preview = document.querySelector("#memory-preview");
  const note = document.querySelector("#memory-note");
  const MODE = { plaintext: "明文存储", encrypted: "加密存储（12.F）", memory: "仅内存（重启后清空）" };

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function say(text) {
    note.textContent = text || "";
    note.hidden = !text;
  }
  function fill(view) {
    const values = (view.settings && view.settings.values) || {};
    form.elements.mem_enabled.checked = !!values.enabled;
    form.elements.mem_inject_preferences.checked = !!values.inject_preferences;
    form.elements.mem_max_inject.value = values.max_inject;
    form.elements.mem_allow_plaintext_content.checked = !!values.allow_plaintext_content;
    let line = `存储：${MODE[view.storage_mode] || view.storage_mode}。`;
    line += view.content_allowed ? "可以记本地内容。" : "现在不收本地内容记忆（明文存储默认不收，或记忆没打开）。";
    if (view.corrupt) line += " 记忆文件读不出来，已停止写入，请检查 data/memory/。";
    storage.textContent = line;
    render(view.items || []);
  }
  function render(items) {
    list.replaceChildren();
    if (!items.length) list.append(el("li", "hint", "还没有记忆。"));
    for (const item of items) {
      const li = el("li", "res-item");
      const head = el("div", "res-head");
      head.append(el("strong", "", item.kind === "preference" ? "偏好" : "本地内容"));
      head.append(el("span", "hint", ` ${item.label || ""} · ${item.memory_id} · ${item.source === "user" ? "你写的" : "模型经确认写的"}`));
      li.append(head);
      if (item.kind === "preference") li.append(el("p", "", item.text || ""));
      else li.append(el("p", "hint", `${item.size || 0} 字 · 原用途 ${item.origin_purpose || "keep"} · 只以句柄给模型`));
      const actions = el("div", "dialog-actions");
      const edit = el("button", "ghost", "编辑");
      edit.type = "button";
      edit.addEventListener("click", () => change(item));
      const del = el("button", "ghost", "删除");
      del.type = "button";
      del.addEventListener("click", () => remove(item));
      actions.append(edit, del);
      if (item.kind === "content") {
        const look = el("button", "ghost", "本机预览");
        look.type = "button";
        look.addEventListener("click", () => show(item));
        actions.append(look);
      }
      li.append(actions);
      list.append(li);
    }
  }
  async function load() {
    try {
      fill(await api("GET", "/api/memory"));
    } catch (error) {
      say(error.message);
    }
  }
  async function show(item) {
    try {
      const result = await api("GET", `/api/memory/${encodeURIComponent(item.memory_id)}/preview`);
      preview.replaceChildren(el("pre", "", result.text || ""));
      preview.hidden = false;
    } catch (error) {
      say(error.message);
    }
  }
  async function change(item) {
    const label = window.prompt("标签", item.label || "");
    if (label === null) return;
    const body = { label };
    if (item.kind === "preference") {
      const text = window.prompt("偏好", item.text || "");
      if (text === null) return;
      body.text = text;
    }
    try {
      await api("PUT", `/api/memory/${encodeURIComponent(item.memory_id)}`, body);
      say("已保存。");
      load();
    } catch (error) {
      say(error.message);
    }
  }
  async function remove(item) {
    if (!window.confirm(`删除这条记忆（${item.label || item.memory_id}）？它在各会话里重发过的句柄也会作废。`)) return;
    try {
      const result = await api("POST", `/api/memory/${encodeURIComponent(item.memory_id)}/delete`, {});
      say(`已删除，作废了 ${result.revoked_handles || 0} 枚句柄。`);
      preview.hidden = true;
      load();
    } catch (error) {
      say(error.message);
    }
  }
  document.querySelector("#memory-save-settings").addEventListener("click", async () => {
    const values = {
      enabled: form.elements.mem_enabled.checked,
      inject_preferences: form.elements.mem_inject_preferences.checked,
      max_inject: Number(form.elements.mem_max_inject.value),
      allow_plaintext_content: form.elements.mem_allow_plaintext_content.checked,
    };
    if (values.allow_plaintext_content && !window.confirm("明文存储模式下，本地内容记忆的正文会以明文写在 data/memory/ 里。确定打开？")) return;
    try {
      fill(await api("PUT", "/api/memory/settings", { values }));
      say("记忆设置已保存。");
    } catch (error) {
      say(error.message);
    }
  });
  document.querySelector("#memory-create").addEventListener("click", async () => {
    const text = document.querySelector("#memory-new-text");
    const label = document.querySelector("#memory-new-label");
    try {
      await api("POST", "/api/memory", { kind: "preference", text: text.value, label: label.value });
      text.value = "";
      label.value = "";
      say("已添加。");
      load();
    } catch (error) {
      say(error.message);
    }
  });
  document.querySelector("#open-memory").addEventListener("click", () => {
    say("");
    preview.hidden = true;
    dialog.showModal();
    load();
  });
  document.querySelector("#close-memory").addEventListener("click", () => dialog.close());
})();
