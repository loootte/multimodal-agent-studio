// 表单工具（issue #53，#86）：通用表单，字段来自 /api/tools/<name>/catalog，预览走 /preview，
// 生成走 /api/sessions/<id>/tools/<name>/run。默认是内置的 character_image；装了别的表单工具（ui.form=true）
// 时顶部出现工具切换。每个字段是「可搜索下拉 + 自定义输入」。分级工具（rating=adult）切换时要弹窗确认，
// 只能本机 ComfyUI 出图（宿主强制）。
(() => {
  const dialog = document.querySelector("#character");
  const box = document.querySelector("#char-fields");
  const extraBox = document.querySelector("#char-extra");
  const preview = document.querySelector("#char-preview");
  const note = document.querySelector("#char-note");
  const count = document.querySelector("#char-count");
  const toolRow = document.querySelector("#char-tools");
  const title = document.querySelector("#character-title");
  const packNote = document.querySelector("#char-pack-note");
  const backendSelect = document.querySelector("#char-backend");
  const GROUP_LABELS = { camera: "摄像机位", light: "光照", detail: "细节", pack: "更多" };
  let catalog = null;
  const DEFAULT_TOOL = "character_image";
  let tools = []; // /api/tools 的 form_tools
  let tool = null; // 当前表单工具简介
  let confirmed = false;
  let controls = {};
  let timer = null;
  let listSeq = 0;

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

  function allFields() {
    return [...catalog.fields, ...(catalog.extra_fields || [])];
  }

  function setValue(name, value) {
    const c = controls[name];
    if (!c) return;
    const hit = c.field.options.find((item) => item.key === value || item.label === value);
    c.input.value = hit ? hit.label : value || "";
  }

  function field(target, spec) {
    const wrap = el("label", "char-field");
    wrap.append(el("span", "", spec.label));
    const input = document.createElement("input");
    input.type = "search";
    input.maxLength = 200;
    input.autocomplete = "off";
    input.placeholder = `搜索预设或自己写${spec.label}`;
    const list = document.createElement("datalist");
    list.id = `char-list-${++listSeq}`;
    for (const option of spec.options) {
      const node = document.createElement("option");
      node.value = option.label;
      const tag = option.group ? `${option.group} · ` : "";
      node.label = `${tag}${option.prompt || "不加"}`;
      list.append(node);
    }
    input.setAttribute("list", list.id);
    input.addEventListener("input", schedule);
    // datalist 会按输入框当前值过滤，已有值时只剩一个选项。
    // 聚焦时先清空（原值放进占位提示），这样下拉显示全部预设；没选新值就还原。
    input.addEventListener("focus", () => {
      input.dataset.prev = input.value;
      if (input.value) {
        input.placeholder = input.value;
        input.value = "";
      }
    });
    input.addEventListener("blur", () => {
      if (!input.value && input.dataset.prev) input.value = input.dataset.prev;
      input.placeholder = `搜索预设或自己写${spec.label}`;
      delete input.dataset.prev;
    });
    wrap.append(input, list);
    target.append(wrap);
    controls[spec.name] = { input, field: spec };
  }

  function build() {
    box.replaceChildren();
    extraBox.replaceChildren();
    controls = {};
    for (const spec of catalog.fields) field(box, spec);
    let group = "";
    let section = null;
    for (const spec of catalog.extra_fields || []) {
      if (spec.group !== group) {
        group = spec.group;
        section = el("fieldset", "char-group");
        section.append(el("legend", "", GROUP_LABELS[group] || group));
        extraBox.append(section);
      }
      field(section, spec);
    }
  }

  function fields() {
    const out = {};
    for (const [name, c] of Object.entries(controls)) {
      const text = (c.input.value || c.input.dataset.prev || "").trim();
      if (text) out[name] = text;
    }
    return out;
  }

  function reset() {
    for (const spec of allFields()) setValue(spec.name, catalog.defaults[spec.name]);
    count.value = 1;
    schedule();
  }

  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(refresh, 150);
  }

  async function refresh() {
    try {
      const result = await call("POST", `/api/tools/${encodeURIComponent(tool.name)}/preview`, { fields: fields() });
      preview.textContent = result.prompt;
      say("");
      return true;
    } catch (error) {
      preview.textContent = "";
      say(error.message);
      return false;
    }
  }

  async function load() {
    catalog = await call("GET", `/api/tools/${encodeURIComponent(tool.name)}/catalog`);
    build();
    reset();
  }

  function rated() {
    return Boolean(tool && tool.rating === "adult");
  }

  function fillTools() {
    toolRow.replaceChildren();
    const usable = tools.filter((item) => !item.unavailable);
    toolRow.hidden = usable.length < 2;
    for (const item of usable) {
      const button = el("button", item.name === tool.name ? "" : "ghost",
        item.rating === "adult" ? `${item.title}（${item.label || "成人"}，仅本机）` : item.title);
      button.type = "button";
      button.setAttribute("role", "tab");
      button.setAttribute("aria-selected", item.name === tool.name ? "true" : "false");
      button.addEventListener("click", () => chooseTool(item.name));
      toolRow.append(button);
    }
  }

  function syncTool() {
    title.textContent = tool.title ? `${tool.title}图片` : "定制角色图片";
    packNote.hidden = !rated();
    packNote.textContent = rated()
      ? `已开启“${tool.label}”：只用本机 ComfyUI 出图，结果标记为成人内容（rating: adult）。`
      : "";
    dialog.classList.toggle("rated", rated());
    for (const option of backendSelect.options) {
      if (option.value === "remote") option.disabled = rated();
    }
    if (rated()) backendSelect.value = "local";
    fillTools();
  }

  async function chooseTool(name) {
    const next = tools.find((item) => item.name === name);
    if (!next || next.name === tool.name) return;
    if (next.rating === "adult" && !window.confirm(next.confirm || `开启“${next.label}”？`)) return;
    tool = next;
    confirmed = next.rating === "adult";
    syncTool();
    try {
      await load();
    } catch (error) {
      say(error.message);
    }
  }

  async function open() {
    say("");
    try {
      const listing = await call("GET", "/api/tools");
      tools = listing.form_tools || [];
    } catch (error) {
      tools = [];
    }
    // 每次打开都回到内置的通用工具。
    tool = tools.find((item) => item.name === DEFAULT_TOOL) || { name: DEFAULT_TOOL, title: "定制角色", rating: "general" };
    confirmed = false;
    syncTool();
    try {
      await load();
    } catch (error) {
      say(error.message);
      dialog.showModal();
      return;
    }
    dialog.showModal();
  }

  async function generate() {
    const n = parseInt(count.value, 10);
    if (!(n >= 1 && n <= 20)) {
      say("张数要是 1 到 20。");
      return;
    }
    if (rated() && backendSelect.value !== "local") {
      say(`“${tool.label}”只能用本机 ComfyUI 出图。`);
      return;
    }
    if (!(await refresh())) return;
    const body = {
      fields: fields(),
      count: n,
      aspect_ratio: document.querySelector("#char-aspect").value,
      backend: backendSelect.value,
      privacy: document.querySelector("#char-privacy").value,
    };
    if (rated()) body.confirmed = confirmed;
    dialog.close();
    const where = body.backend === "local" ? "本机 ComfyUI" : "第三方出图";
    const mode = rated() ? `${tool.label}，` : "";
    const name = tool.title || "定制角色";
    const label = body.privacy === "local"
      ? `${name}出图（${mode}本地提示词，${n} 张，${where}）`
      : `${name}出图（${mode}${n} 张，${where}）`;
    if (typeof window.sendCharacter === "function") await window.sendCharacter(body, label, tool.name);
  }

  document.querySelector("#open-character").addEventListener("click", open);
  document.querySelector("#char-close").addEventListener("click", () => dialog.close());
  document.querySelector("#char-reset").addEventListener("click", reset);
  document.querySelector("#char-generate").addEventListener("click", generate);
})();
