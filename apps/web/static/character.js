// 定制角色图片（issue #53，#86）。字段 → 预设或自定义文字；预览是本机拼出的提示词；生成走 /api/sessions/<id>/character。
// #86：每个字段是「可搜索下拉 + 自定义输入」（input + datalist：输入能筛选预设，也能直接写自己的描述）；
// 加了摄像机位、光照、表情、动作/场景。本机扩展包（仓库外的 pack.json）默认不选；选了要在弹窗里确认，
// 分级（rating=adult）的扩展包只能本机 ComfyUI 出图。本机没有扩展包时整行不显示。
(() => {
  const dialog = document.querySelector("#character");
  const box = document.querySelector("#char-fields");
  const extraBox = document.querySelector("#char-extra");
  const preview = document.querySelector("#char-preview");
  const note = document.querySelector("#char-note");
  const count = document.querySelector("#char-count");
  const packRow = document.querySelector("#char-pack-row");
  const packSelect = document.querySelector("#char-pack");
  const packNote = document.querySelector("#char-pack-note");
  const backendSelect = document.querySelector("#char-backend");
  const GROUP_LABELS = { camera: "摄像机位", light: "光照", detail: "细节", pack: "扩展包" };
  let catalog = null;
  let pack = null; // 当前扩展包简介（来自 catalog.packs），null = 内置预设。
  let packConfirmed = false;
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
      const result = await call("POST", "/api/character/preview", { fields: fields(), pack: pack ? pack.name : "" });
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
    const path = pack ? `/api/character?pack=${encodeURIComponent(pack.name)}` : "/api/character";
    catalog = await call("GET", path);
    build();
    reset();
    fillPacks();
  }

  function rated() {
    return Boolean(pack && pack.rating === "adult");
  }

  function fillPacks() {
    const packs = catalog.packs || [];
    packRow.hidden = packs.length === 0;
    const keep = pack ? pack.name : "";
    packSelect.replaceChildren(new Option("内置预设", ""));
    for (const item of packs) {
      const tag = item.rating === "adult" ? "（分级，仅本机）" : "";
      packSelect.append(new Option(`${item.label}${tag}`, item.name));
    }
    packSelect.value = keep;
  }

  function syncPack() {
    packSelect.value = pack ? pack.name : "";
    packNote.hidden = !pack;
    packNote.textContent = pack ? `已开启“${pack.label}”${rated() ? "：只用本机 ComfyUI 出图，结果标记为成人内容（rating: adult）。" : "。"}` : "";
    dialog.classList.toggle("rated", rated());
    for (const option of backendSelect.options) {
      if (option.value === "remote") option.disabled = rated();
    }
    if (rated()) backendSelect.value = "local";
  }

  async function choosePack() {
    const name = packSelect.value;
    const next = (catalog.packs || []).find((item) => item.name === name) || null;
    if (next && next.confirm && !window.confirm(next.confirm)) {
      packSelect.value = pack ? pack.name : "";
      return;
    }
    pack = next;
    packConfirmed = Boolean(next);
    syncPack();
    try {
      await load();
    } catch (error) {
      say(error.message);
    }
  }

  async function open() {
    say("");
    pack = null; // 每次打开都回到内置预设（SFW）。
    packConfirmed = false;
    syncPack();
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
      say(`“${pack.label}”只能用本机 ComfyUI 出图。`);
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
    if (pack) {
      body.pack = pack.name;
      body.pack_confirmed = packConfirmed;
    }
    dialog.close();
    const where = body.backend === "local" ? "本机 ComfyUI" : "第三方出图";
    const mode = pack ? `${pack.label}，` : "";
    const label = body.privacy === "local"
      ? `定制角色出图（${mode}本地提示词，${n} 张，${where}）`
      : `定制角色出图（${mode}${n} 张，${where}）`;
    if (typeof window.sendCharacter === "function") await window.sendCharacter(body, label);
  }

  document.querySelector("#open-character").addEventListener("click", open);
  document.querySelector("#char-close").addEventListener("click", () => dialog.close());
  document.querySelector("#char-reset").addEventListener("click", reset);
  document.querySelector("#char-generate").addEventListener("click", generate);
  packSelect.addEventListener("change", choosePack);
})();
