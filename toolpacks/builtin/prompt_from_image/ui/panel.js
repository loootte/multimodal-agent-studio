// 读图提示词的编辑面板（prompt_from_image 的 ui.panel，launcher=chat，见 docs/prompt-from-image.md）。
// 只由对话里宿主发出的 tool.panel 事件打开，没有侧栏入口。正文经 api.draft 在本机网页和宿主之间往返，
// 从不进对话模型；面板拿不到别的会话内容和接口。「保存」原句柄更新正文，「出图」先保存再走宿主出图路径
// （第三方出图的确认、只发给出图服务等规则都由宿主强制）。
MASPanels.define(function mount(root, api) {
  const tool = api.tool;
  const draft = api.draft;

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function select(choices) {
    const node = document.createElement("select");
    for (const [value, text] of choices) {
      const item = el("option", "", text);
      item.value = value;
      node.append(item);
    }
    return node;
  }
  function labelled(text, control) {
    const wrap = el("label", "");
    wrap.append(text + " ", control);
    return wrap;
  }

  root.replaceChildren();
  const panel = el("div", "pfi-panel");
  panel.append(el("h2", "", "编辑读出的提示词"));
  if (!draft) {
    panel.append(el("p", "note", "这个面板只能从对话里打开：在对话里让我“从图片读提示词”，选图后会自动打开。"));
    root.append(panel);
    return undefined;
  }
  panel.append(el("p", "hint", "这是从你选的图片里读出的正向提示词，只存在本机，对话模型看不到正文。改好后可以「保存」，或直接「出图」。"));
  const text = document.createElement("textarea");
  text.className = "pfi-text";
  text.rows = 10;
  text.maxLength = 8000;
  text.disabled = true;
  text.placeholder = "正在读取…";
  text.setAttribute("aria-label", "正向提示词");
  const meta = el("p", "hint pfi-meta", "");
  const options = el("div", "pfi-options");
  const count = document.createElement("input");
  count.type = "number";
  count.min = "1";
  count.max = "20";
  count.value = "1";
  const aspect = select([["1:1", "1:1"], ["16:9", "16:9"], ["9:16", "9:16"]]);
  const backend = select([["local", "本机 ComfyUI"], ["remote", "第三方出图"]]);
  options.append(labelled("张数", count), labelled("画幅", aspect), labelled("出图", backend));
  const note = el("p", "note", "");
  note.hidden = true;
  const actions = el("div", "dialog-actions");
  const save = el("button", "ghost", "保存");
  save.type = "button";
  const generate = el("button", "", "出图");
  generate.type = "button";
  actions.append(save, generate);
  panel.append(text, meta, options, note, actions);
  root.append(panel);

  let saved = "";
  function show(message, ok) {
    note.textContent = message || "";
    note.hidden = !message;
    note.classList.toggle("ok", Boolean(ok));
  }
  function updateMeta() {
    const value = text.value.trim();
    const dirty = value !== saved;
    meta.textContent = `${value.length} 字${dirty ? "，未保存" : "，已保存"}`;
  }
  function busy(on) {
    save.disabled = on;
    generate.disabled = on;
    text.disabled = on;
  }

  text.addEventListener("input", () => { show(""); updateMeta(); });
  save.addEventListener("click", async () => {
    busy(true);
    try {
      await draft.save(text.value);
      saved = text.value.trim();
      show("已保存到本地，原句柄不变。", true);
    } catch (error) {
      show(error.message || "没能保存。");
    } finally {
      busy(false);
      updateMeta();
    }
  });
  generate.addEventListener("click", async () => {
    const n = Number(count.value);
    if (!Number.isInteger(n) || n < 1 || n > 20) {
      show("张数要是 1 到 20。");
      return;
    }
    if (backend.value === "remote" && !api.confirm("第三方出图会把这段本地提示词正文发给出图服务（不发给对话模型），宿主还会再确认一次。继续？")) return;
    busy(true);
    try {
      const started = await draft.generate({ text: text.value, count: n, aspect_ratio: aspect.value, backend: backend.value });
      if (!started) show("没能开始出图：请回到读图的那个会话，等这一轮结束再试。");
    } catch (error) {
      show(error.message || "没能开始出图。");
    } finally {
      busy(false);
    }
  });

  let alive = true;
  draft.read().then(
    (value) => {
      if (!alive) return;
      text.value = value;
      saved = value.trim();
      text.disabled = false;
      text.placeholder = "";
      updateMeta();
      text.focus();
    },
    (error) => {
      if (!alive) return;
      text.placeholder = "";
      show(error.message || "读不到这份本地提示词。");
      save.disabled = true;
      generate.disabled = true;
    },
  );
  return () => { alive = false; };
});
