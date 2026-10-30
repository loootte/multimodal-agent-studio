// 定制角色图片（issue #53，#86）。字段 → 预设或自定义文字；预览是本机拼出的提示词；生成走 /api/sessions/<id>/character。
// #86：每个字段是「可搜索下拉 + 自定义输入」（input + datalist：输入能筛选预设，也能直接写自己的描述）；
// 加了摄像机位、光照、表情、动作/场景；成人模式默认关，打开要在弹窗里确认，且只能本机 ComfyUI 出图。
(() => {
  const dialog = document.querySelector("#character");
  const box = document.querySelector("#char-fields");
  const extraBox = document.querySelector("#char-extra");
  const preview = document.querySelector("#char-preview");
  const note = document.querySelector("#char-note");
  const count = document.querySelector("#char-count");
  const adultBox = document.querySelector("#char-adult");
  const adultNote = document.querySelector("#char-adult-note");
  const backendSelect = document.querySelector("#char-backend");
  const GROUP_LABELS = { camera: "摄像机位", light: "光照", detail: "细节" };
  let catalog = null;
  let adult = false;
  let adultConfirmed = false;
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
    input.addEventListener("focus", () => input.select());
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
      const text = c.input.value.trim();
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
      const result = await call("POST", "/api/character/preview", { fields: fields(), adult });
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
    catalog = await call("GET", adult ? "/api/character?adult=1" : "/api/character");
    build();
    reset();
  }

  function syncAdult() {
    adultBox.checked = adult;
    adultNote.hidden = !adult;
    dialog.classList.toggle("adult", adult);
    for (const option of backendSelect.options) {
      if (option.value === "remote") option.disabled = adult;
    }
    if (adult) backendSelect.value = "local";
  }

  async function toggleAdult() {
    if (adultBox.checked) {
      const ok = window.confirm(
        "开启成人模式？\n\n" +
          "· 会生成成人/露骨内容，只用本机 ComfyUI 出图，不会发给第三方出图服务；\n" +
          "· 只画原创的成年角色（20 岁以上），未成年、校服、幼态体型、真人和版权角色一律拒绝；\n" +
          "· 生成结果会标记为成人内容（rating: adult，文件名带 adult_ 前缀）。\n\n" +
          "请确认你已成年，并且本机使用合法。",
      );
      if (!ok) {
        adultBox.checked = false;
        return;
      }
      adult = true;
      adultConfirmed = true;
    } else {
      adult = false;
      adultConfirmed = false;
    }
    syncAdult();
    try {
      await load();
    } catch (error) {
      say(error.message);
    }
  }

  async function open() {
    say("");
    adult = false; // 每次打开都回到默认的 SFW。
    adultConfirmed = false;
    syncAdult();
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
    if (adult && backendSelect.value !== "local") {
      say("成人模式只能用本机 ComfyUI 出图。");
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
    if (adult) {
      body.adult = true;
      body.adult_confirmed = adultConfirmed;
    }
    dialog.close();
    const where = body.backend === "local" ? "本机 ComfyUI" : "第三方出图";
    const mode = adult ? "成人模式，" : "";
    const label = body.privacy === "local"
      ? `定制角色出图（${mode}本地提示词，${n} 张，${where}）`
      : `定制角色出图（${mode}${n} 张，${where}）`;
    if (typeof window.sendCharacter === "function") await window.sendCharacter(body, label);
  }

  document.querySelector("#open-character").addEventListener("click", open);
  document.querySelector("#char-close").addEventListener("click", () => dialog.close());
  document.querySelector("#char-reset").addEventListener("click", reset);
  document.querySelector("#char-generate").addEventListener("click", generate);
  adultBox.addEventListener("change", toggleAdult);
})();
