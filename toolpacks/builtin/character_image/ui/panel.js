// 角色定制面板（character_image 的 ui.panel，见 docs/tool-code-contract.md「网页面板」）。
// 字段来自 api.catalog()，预览走 api.preview(fields)，出图交给 api.run(...)。
// 面板只经宿主给的 api 说话：拿不到会话、本地内容和别的接口。分级（rating=adult）的确认、
// 只能本机出图、内容检查都由宿主强制，这里只是把选项摆出来。同一份文件也可以给别的表单工具用（比如成人模式包）。
MASPanels.define(function mount(root, api) {
  const tool = api.tool;
  const rated = tool.rating === "adult";
  const GROUP_LABELS = { camera: "摄像机位", light: "光照", detail: "细节", pack: "更多" };
  let catalog = null;
  let controls = {};
  let timer = null;
  let listSeq = 0;

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function option(value, text) {
    const node = el("option", "", text);
    node.value = value;
    return node;
  }
  function select(choices) {
    const node = document.createElement("select");
    for (const [value, text] of choices) node.append(option(value, text));
    return node;
  }
  function labelled(text, control) {
    const wrap = el("label", "");
    wrap.append(text + " ", control);
    return wrap;
  }

  // ---- 界面 ----
  root.replaceChildren();
  const panel = el("div", "char-panel" + (rated ? " rated" : ""));
  const heading = el("h2", "", `${tool.title || "定制角色"}图片`);
  const hint = el("p", "hint", "每项可以输入搜索预设，也可以直接写自己的描述；清空就用默认值。默认是一个年轻东亚女性角色（二次元风格）；「参考已有角色」可留空。预设可以在「工具 → 对应工具」里改。");
  const packNote = el("p", "note char-pack-note");
  packNote.hidden = !rated;
  if (rated) packNote.textContent = `已开启“${tool.label}”：只用本机 ComfyUI 出图，结果标记为成人内容（rating: adult）。`;
  const box = el("div", "char-fields");
  const extraBox = el("div", "char-fields char-extra");
  const count = document.createElement("input");
  count.type = "number";
  count.min = "1";
  count.max = "20";
  count.value = "1";
  const aspect = select([["1:1", "1:1"], ["16:9", "16:9"], ["9:16", "9:16"]]);
  const backend = select([["local", "本机 ComfyUI"], ["remote", "第三方出图"]]);
  if (tool.local_only) {
    backend.options[1].disabled = true;
    backend.value = "local";
  }
  const privacy = select([["chat", "写进聊天记录（1.1）"], ["local", "本地提示词，只留本机（1.3）"]]);
  const options = el("div", "char-options");
  options.append(labelled("张数", count), labelled("画幅", aspect), labelled("出图", backend), labelled("提示词", privacy));
  const previewHint = el("p", "hint", "提示词预览（这次会发给出图服务的英文提示词）：");
  const preview = el("pre", "char-preview");
  const note = el("p", "note");
  note.hidden = true;
  const actions = el("div", "dialog-actions");
  const resetButton = el("button", "ghost", "恢复默认");
  const closeButton = el("button", "ghost", "关闭");
  const runButton = el("button", "", tool.run_label || "生成");
  for (const button of [resetButton, closeButton, runButton]) button.type = "button";
  actions.append(resetButton, closeButton, runButton);
  panel.append(heading, hint, packNote, box, extraBox, options, previewHint, preview, note, actions);
  root.append(panel);

  function say(text) {
    note.textContent = text || "";
    note.hidden = !text;
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
    list.id = `char-list-${tool.name}-${++listSeq}`;
    for (const item of spec.options) {
      const node = document.createElement("option");
      node.value = item.label;
      const tag = item.group ? `${item.group} · ` : "";
      node.label = `${tag}${item.prompt || "不加"}`;
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
    if (!catalog) return;
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
      const result = await api.preview(fields());
      preview.textContent = result.prompt;
      say("");
      return true;
    } catch (error) {
      preview.textContent = "";
      say(error.message);
      return false;
    }
  }

  async function generate() {
    const n = parseInt(count.value, 10);
    if (!(n >= 1 && n <= 20)) {
      say("张数要是 1 到 20。");
      return;
    }
    if (tool.local_only && backend.value !== "local") {
      say(`“${tool.label || tool.title}”只能用本机 ComfyUI 出图。`);
      return;
    }
    if (!(await refresh())) return;
    try {
      await api.run({ fields: fields(), count: n, aspect_ratio: aspect.value, backend: backend.value, privacy: privacy.value });
    } catch (error) {
      say(error.message);
    }
  }

  resetButton.addEventListener("click", reset);
  closeButton.addEventListener("click", () => api.close());
  runButton.addEventListener("click", generate);

  api.catalog().then((data) => {
    catalog = data;
    build();
    reset();
  }).catch((error) => say(error.message));

  return () => clearTimeout(timer);
});
