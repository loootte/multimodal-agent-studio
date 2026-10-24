// 定制角色图片（issue #53）。字段 → 预设或自定义文字；预览是本机拼出的提示词；生成走 /api/sessions/<id>/character。
(() => {
  const dialog = document.querySelector("#character");
  const box = document.querySelector("#char-fields");
  const preview = document.querySelector("#char-preview");
  const note = document.querySelector("#char-note");
  const count = document.querySelector("#char-count");
  const CUSTOM = "__custom__";
  let catalog = null;
  const controls = {};
  let timer = null;

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
  async function call(method, path, body) {
    const response = await fetch(path, {
      method,
      headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(data.message || "出错了。");
      error.payload = data;
      throw error;
    }
    return data;
  }

  function setValue(name, value) {
    const c = controls[name];
    const field = catalog.fields.find((item) => item.name === name);
    const hit = field.options.find((item) => item.key === value || item.label === value);
    if (hit) {
      c.select.value = hit.key;
      c.custom.value = "";
      c.custom.hidden = true;
    } else {
      c.select.value = CUSTOM;
      c.custom.value = value || "";
      c.custom.hidden = false;
    }
  }

  function build() {
    box.replaceChildren();
    for (const field of catalog.fields) {
      const wrap = el("label", "char-field");
      wrap.append(el("span", "", field.label));
      const select = document.createElement("select");
      select.dataset.field = field.name;
      for (const option of field.options) {
        const node = el("option", "", option.label);
        node.value = option.key;
        select.append(node);
      }
      const custom = el("option", "", "自定义…");
      custom.value = CUSTOM;
      select.append(custom);
      const input = document.createElement("input");
      input.type = "text";
      input.maxLength = 200;
      input.placeholder = `自己写${field.label}`;
      input.hidden = true;
      select.addEventListener("change", () => {
        input.hidden = select.value !== CUSTOM;
        if (!input.hidden) input.focus();
        schedule();
      });
      input.addEventListener("input", schedule);
      wrap.append(select, input);
      box.append(wrap);
      controls[field.name] = { select, custom: input };
    }
  }

  function fields() {
    const out = {};
    for (const [name, c] of Object.entries(controls)) {
      out[name] = c.select.value === CUSTOM ? c.custom.value.trim() : c.select.value;
    }
    return out;
  }

  function reset() {
    for (const field of catalog.fields) setValue(field.name, catalog.defaults[field.name]);
    count.value = 1;
    schedule();
  }

  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(refresh, 150);
  }

  async function refresh() {
    try {
      const result = await call("POST", "/api/character/preview", { fields: fields() });
      preview.textContent = result.prompt;
      say("");
      return true;
    } catch (error) {
      preview.textContent = "";
      say(error.message);
      return false;
    }
  }

  async function open() {
    say("");
    try {
      catalog = await call("GET", "/api/character");
    } catch (error) {
      say(error.message);
      dialog.showModal();
      return;
    }
    build();
    reset();
    dialog.showModal();
  }

  async function generate() {
    const n = parseInt(count.value, 10);
    if (!(n >= 1 && n <= 20)) {
      say("张数要是 1 到 20。");
      return;
    }
    if (!(await refresh())) return;
    const body = {
      fields: fields(),
      count: n,
      aspect_ratio: document.querySelector("#char-aspect").value,
      backend: document.querySelector("#char-backend").value,
      privacy: document.querySelector("#char-privacy").value,
    };
    dialog.close();
    const where = body.backend === "local" ? "本机 ComfyUI" : "第三方出图";
    const label = body.privacy === "local" ? `定制角色出图（本地提示词，${n} 张，${where}）` : `定制角色出图（${n} 张，${where}）`;
    if (typeof window.sendCharacter === "function") await window.sendCharacter(body, label);
  }

  document.querySelector("#open-character").addEventListener("click", open);
  document.querySelector("#char-close").addEventListener("click", () => dialog.close());
  document.querySelector("#char-reset").addEventListener("click", reset);
  document.querySelector("#char-generate").addEventListener("click", generate);
})();
