const state = {
  provider: null,
  identity: null,
  sessions: [],
  currentId: null,
  session: null,
  sending: false,
  stopped: false,
  pending: [],
};

const MODALITY_LABELS = {
  text: "文字",
  image: "图片",
  audio: "音频",
  video: "视频",
  file: "文件",
};

const input = document.querySelector("#input");
const transcript = document.querySelector("#transcript");
const note = document.querySelector("#composer-note");
const settings = document.querySelector("#settings");
const sidebar = document.querySelector("#sidebar");

document.querySelector("#composer").addEventListener("submit", (event) => {
  event.preventDefault();
  sendMessage();
});

input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    sendMessage();
  }
});

input.addEventListener("input", updateButtons);

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !settings.open && state.sending) {
    event.preventDefault();
    stopRun();
    return;
  }
  if (event.key !== "Enter" || event.shiftKey || settings.open || !state.sending) return;
  if (event.target && event.target.closest && event.target.closest("textarea")) return;
  const pending = waitingConfirm();
  if (!pending) return;
  event.preventDefault();
  submitConfirm(pending, true);
});

document.querySelector("#new-chat").addEventListener("click", () => createSession(true));
document.querySelector("#toggle-sessions").addEventListener("click", () => {
  sidebar.classList.toggle("open");
});
document.querySelector("#open-settings").addEventListener("click", openSettings);
document.querySelector("#close-settings").addEventListener("click", () => settings.close());
document.querySelector("#toggle-runtime").addEventListener("click", () => {
  document.querySelector(".app").classList.toggle("runtime-collapsed");
});
document.querySelector("#close-runtime").addEventListener("click", () => {
  document.querySelector(".app").classList.add("runtime-collapsed");
});
document.querySelector("#settings-form").addEventListener("submit", saveSettings);
document.querySelector("#clear-key").addEventListener("click", clearKey);
document.querySelector("#file").addEventListener("change", (event) => {
  const chosen = event.target.files && event.target.files[0];
  event.target.value = "";
  if (chosen) addPendingFile(chosen);
});
document.querySelector("#stop").addEventListener("click", stopRun);

boot();

async function boot() {
  try {
    state.identity = await api("GET", "/api/identity");
    state.provider = await api("GET", "/api/provider");
    await loadSessions();
    const saved = readSavedSession();
    if (saved && state.sessions.some((item) => item.session_id === saved)) {
      await openSession(saved);
    } else {
      render();
    }
    if (state.provider && !state.provider.configured) {
      showNote("还没有配置 API key。请在设置里填写。");
    }
  } catch (error) {
    showNote("连不上本机聊天服务。");
  }
  if (window.matchMedia("(max-width: 960px)").matches) {
    document.querySelector(".app").classList.add("runtime-collapsed");
  }
  input.focus();
}

async function loadSessions() {
  const payload = await api("GET", "/api/sessions");
  state.sessions = payload.sessions || [];
}

async function createSession(focus) {
  const created = await api("POST", "/api/sessions", {});
  state.currentId = created.session_id;
  state.session = created;
  state.pending = [];
  rememberSession(created.session_id);
  await loadSessions();
  render();
  sidebar.classList.remove("open");
  if (focus) input.focus();
}

async function openSession(sessionId) {
  state.currentId = sessionId;
  rememberSession(sessionId);
  await reloadSession();
  render();
  sidebar.classList.remove("open");
  const running = (state.session.messages || []).find((message) => message.status === "running" && message.run_id);
  if (running) {
    state.sending = true;
    state.stopped = false;
    updateButtons();
    resetAssistantForReplay();
    try {
      await subscribe(running.run_id);
    } catch (error) {
      showNote("没能接回这次回答。");
    }
    state.sending = false;
    await reloadSession();
    await loadSessions();
    render();
  }
  input.focus();
}

async function sendMessage() {
  const text = input.value;
  const blocked = attachmentBlock();
  if (state.sending || blocked) return;
  if (!text.trim() && !state.pending.length) return;
  if (!state.currentId) await createSession(false);
  const attachments = state.pending.slice();
  state.stopped = false;
  state.sending = true;
  state.session.messages.push({
    message_id: "local-user",
    role: "user",
    text: text.trim(),
    status: "completed",
    error: null,
    attachments,
  });
  state.session.messages.push({
    message_id: "local-assistant",
    role: "assistant",
    text: "",
    status: "running",
    error: null,
    events: [],
    runtime: [],
  });
  input.value = "";
  state.pending = [];
  showNote("");
  render();
  try {
    const response = await fetch(`/api/sessions/${state.currentId}/messages`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        text,
        attachments: attachments.map((item) => ({ handle: item.handle })),
      }),
    });
    if (!response.ok || !response.body) {
      const problem = await response.json().catch(() => ({}));
      showNote(problem.message || "发送失败。");
    } else {
      await readSse(response, onEvent);
    }
  } catch (error) {
    showNote("连不上本机聊天服务。");
  }
  state.sending = false;
  try {
    await reloadSession();
    if (state.session.running && state.session.run_id && !state.stopped) {
      resetAssistantForReplay();
      await subscribe(state.session.run_id);
      await reloadSession();
    }
    await loadSessions();
  } catch (error) {
    showNote("会话没有刷新成功。");
  }
  render();
  input.focus();
}

async function stopRun() {
  if (!state.sending || !state.currentId) return;
  state.stopped = true;
  updateButtons();
  try {
    await api("POST", `/api/sessions/${state.currentId}/stop`, {});
  } catch (error) {
    showNote(error.message || "停止失败。");
  }
}

async function subscribe(runId) {
  const response = await fetch(`/api/runs/${encodeURIComponent(runId)}/events`);
  if (!response.ok || !response.body) return;
  await readSse(response, onEvent);
}

function onEvent(event) {
  const assistant = currentAssistant();
  if (!assistant) return;
  if (event.type === "run.started") {
    assistant.message_id = event.message_id;
    assistant.run_id = event.run_id;
    assistant.text = "";
    assistant.status = "running";
    assistant.error = null;
    assistant.events = [];
    assistant.runtime = [];
    state.session.run_id = event.run_id;
    state.session.running = true;
  } else if (event.type === "runtime.step") {
    rememberRuntime(assistant, event);
  } else if (event.type === "message.delta") {
    if (state.stopped) return;
    assistant.text += event.text || "";
    assistant.status = "running";
  } else if (
    event.type === "tool_call" ||
    event.type === "tool.confirm" ||
    event.type === "progress" ||
    event.type === "artifact" ||
    event.type === "error" ||
    event.type === "content_request"
  ) {
    if (state.stopped && (event.type === "progress" || event.type === "artifact")) return;
    if (event.type === "content_request" && event.run_id) assistant.run_id = event.run_id;
    rememberAgentEvent(assistant, event);
    if (event.type === "content_request") {
      document.querySelector("#live").textContent = event.hint || "请输入要留在本机的内容。";
    }
    if (event.type === "artifact") document.querySelector("#live").textContent = "已收到图片";
    if (event.type === "progress") document.querySelector("#live").textContent = progressLine(event);
    if (event.type === "error") document.querySelector("#live").textContent = event.message || "";
  } else if (event.type === "message.completed") {
    assistant.text = event.text || "";
    assistant.status = "completed";
    assistant.error = null;
    state.sending = false;
    state.session.running = false;
    if (!hasArtifact(assistant)) document.querySelector("#live").textContent = assistant.text;
  } else if (event.type === "run.failed") {
    if (!(state.stopped && (event.text || "").length < assistant.text.length)) {
      assistant.text = event.text || assistant.text;
    }
    assistant.status = event.code === "cancelled" ? "cancelled" : "failed";
    assistant.error = { code: event.code, message: event.message, retryable: event.retryable };
    state.sending = false;
    state.session.running = false;
    document.querySelector("#live").textContent = event.message || "";
  }
  renderTranscript();
  renderRuntime();
  updateButtons();
}

function resetAssistantForReplay() {
  const assistant = currentAssistant();
  if (!assistant) return;
  assistant.text = "";
  assistant.events = [];
  assistant.runtime = [];
  assistant.error = null;
}

function rememberAgentEvent(assistant, event) {
  const events = assistant.events || (assistant.events = []);
  if (event.type === "content_request" && events.some((item) => item.type === "content_request")) return;
  if (event.type === "tool_call") {
    const open = [...events].reverse().find(
      (item) => item.type === "tool_call" && item.tool === event.tool && (item.status === "running" || item.status === "waiting_confirm"),
    );
    if (open) {
      Object.assign(open, {
        tool: event.tool || open.tool,
        arguments: event.arguments || open.arguments,
        status: event.status || open.status,
        result: event.result || open.result,
        run_id: event.run_id || open.run_id,
      });
      return;
    }
  }
  if (event.type === "artifact" && events.some((item) => item.type === "artifact")) return;
  if (event.type === "error" && events.some((item) => item.type === "error")) return;
  if (event.type === "progress") {
    const previous = events[events.length - 1];
    if (
      previous &&
      previous.type === "progress" &&
      previous.value === event.value &&
      previous.max === event.max &&
      previous.percent === event.percent
    ) {
      return;
    }
  }
  events.push({
    type: event.type,
    tool: event.tool || event.name,
    name: event.name,
    arguments: event.arguments,
    status: event.status,
    result: event.result,
    value: event.value,
    max: event.max,
    percent: event.percent,
    artifact_id: event.artifact_id,
    media: event.media,
    url: event.url,
    width: event.width,
    height: event.height,
    code: event.code,
    message: event.message,
    draft: event.draft,
    hint: event.hint,
    purpose: event.purpose,
    kind: event.kind,
    run_id: event.run_id,
  });
}

function hasArtifact(message) {
  return (message.events || []).some((item) => item.type === "artifact");
}

function currentAssistant() {
  const messages = state.session && state.session.messages;
  if (!messages) return null;
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    if (messages[index].role === "assistant") return messages[index];
  }
  return null;
}

async function reloadSession() {
  state.session = await api("GET", `/api/sessions/${state.currentId}`);
}

function render() {
  renderSessions();
  renderChrome();
  renderTranscript();
  renderRuntime();
  renderPending();
  updateButtons();
}

function rememberRuntime(assistant, event) {
  const steps = assistant.runtime || (assistant.runtime = []);
  if (event.kind === "model" && event.step) {
    const found = steps.find((item) => item.kind === "model" && item.step === event.step);
    if (found) {
      Object.assign(found, event);
      return;
    }
  }
  if (event.kind === "tool" && event.name) {
    const pending = [...steps].reverse().find((item) => item.kind === "tool" && item.name === event.name && item.ms == null);
    if (pending && event.ms != null) {
      Object.assign(pending, event);
      return;
    }
  }
  steps.push({
    type: event.type,
    kind: event.kind,
    name: event.name,
    ms: event.ms,
    step: event.step,
    ok: event.ok,
    label: event.label,
  });
}

function renderRuntime() {
  const log = document.querySelector("#runtime-log");
  const status = document.querySelector("#runtime-status");
  const assistant = currentAssistant();
  const steps = (assistant && assistant.runtime) || [];
  log.replaceChildren();
  if (!steps.length) {
    status.textContent = state.sending ? "正在询问模型…" : "发送后会列出这一次的模型和工具步骤。";
    return;
  }
  status.textContent = runtimeStatus(steps, state.sending);
  for (const step of steps) {
    if (step.kind === "status") continue;
    const item = document.createElement("li");
    const kind = document.createElement("span");
    kind.className = "kind";
    kind.textContent = runtimeKind(step);
    const body = document.createElement("span");
    body.textContent = runtimeLine(step);
    item.append(kind, body);
    log.append(item);
  }
  log.scrollTop = log.scrollHeight;
}

function runtimeKind(step) {
  if (step.kind === "model") return "模型";
  if (step.kind === "tool") return "工具";
  return "步骤";
}

function runtimeLine(step) {
  if (step.kind === "model") {
    const round = step.step ? `第 ${step.step} 步` : "模型调用";
    const ms = step.ms != null ? ` · ${step.ms}ms` : " · 进行中";
    return round + ms;
  }
  if (step.kind === "tool") {
    const name = step.name || "工具";
    const ms = step.ms != null ? ` · ${step.ms}ms` : " · 进行中";
    const ok = step.ok === false ? " · 失败" : "";
    return name + ms + ok;
  }
  return step.label || step.kind || "步骤";
}

function runtimeStatus(steps, sending) {
  const last = [...steps].reverse().find((item) => item.kind !== "status") || steps[steps.length - 1];
  if (sending) {
    if (last && last.kind === "tool" && last.ms == null) return `正在调用 ${last.name || "工具"}…`;
    return "正在询问模型…";
  }
  return `这一次 ${steps.filter((item) => item.kind === "model" || item.kind === "tool").length} 步`;
}

function renderSessions() {
  const list = document.querySelector("#session-list");
  list.replaceChildren();
  if (!state.sessions.length) {
    const empty = document.createElement("li");
    empty.className = "empty";
    empty.textContent = "还没有对话";
    list.append(empty);
    return;
  }
  for (const session of state.sessions) {
    const item = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.className = "session";
    if (session.session_id === state.currentId) button.classList.add("active");
    const title = document.createElement("span");
    title.className = "title";
    title.textContent = session.title || "新对话";
    const time = document.createElement("span");
    time.className = "time";
    time.textContent = session.running ? "回答中" : formatTime(session.updated_at);
    button.append(title, time);
    button.addEventListener("click", () => {
      openSession(session.session_id).catch(() => showNote("打不开这个会话。"));
    });
    item.append(button);
    list.append(item);
  }
}

function renderChrome() {
  const provider = state.provider || {};
  const identity = state.identity || {};
  document.querySelector("#user-line").textContent = identity.display_name || "本机用户";
  document.querySelector("#model-line").textContent = provider.model || "未设置模型";
  const accepted = modalityLine(provider);
  document.querySelector("#modality-line").textContent = provider.configured
    ? `${accepted} · 已保存密钥 ${provider.key_hint}`
    : `${accepted} · 还没有 API key`;
  document.title = state.session && state.session.title ? state.session.title : "对话";
}

function modalityLine(provider) {
  const items = (provider && provider.input_modalities) || ["text"];
  const labels = items.map((item) => MODALITY_LABELS[item] || item);
  return labels.length ? `接受${labels.join("、")}` : "接受文字";
}

function renderTranscript() {
  const nearBottom = transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < 80;
  transcript.replaceChildren();
  const messages = (state.session && state.session.messages) || [];
  if (!messages.length) {
    const empty = document.createElement("p");
    empty.className = "empty-thread";
    empty.textContent = "写一条消息。要留下本机内容时直接说。文字或文件在对话框里提交，不会发给模型。";
    transcript.append(empty);
    return;
  }
  const thread = document.createElement("div");
  thread.className = "thread";
  for (const message of messages) thread.append(bubble(message));
  transcript.append(thread);
  if (nearBottom) transcript.scrollTop = transcript.scrollHeight;
}

function bubble(message) {
  const item = document.createElement("article");
  item.className = `bubble ${message.role === "user" ? "user" : "assistant"}`;
  const events = message.events || [];
  const pendingContent = events.find((entry) => entry.type === "content_request");
  const artifact = events.find((entry) => entry.type === "artifact");
  const collecting = !events.some((entry) => entry.type === "tool_call" || entry.type === "error");
  const showEntry = pendingContent && !artifact && message.status === "running" && collecting;
  if (message.status === "running" && !showEntry) item.classList.add("streaming");
  const who = document.createElement("span");
  who.className = "who";
  who.textContent = message.role === "user" ? "你" : "助手";
  const text = document.createElement("p");
  text.className = "body";
  text.textContent = message.text || "";
  item.append(who, text);
  for (const attachment of message.attachments || []) {
    item.append(attachmentView(attachment));
  }
  const calls = events.filter((entry) => entry.type === "tool_call");
  if (calls.length) item.append(toolTimeline(message, calls));
  const progress = [...events].reverse().find((entry) => entry.type === "progress");
  if (showEntry) item.append(localEntry(message, pendingContent));
  if (progress && !artifact) {
    const line = document.createElement("p");
    line.className = "status-line";
    line.textContent = progressLine(progress);
    item.append(line);
  }
  if (artifact && artifact.media === "image") {
    const src = artifactSrc(artifact);
    if (src) {
      const image = document.createElement("img");
      image.alt = "生成的图片";
      image.src = src;
      item.append(image);
    }
  }
  const agentError = events.find((entry) => entry.type === "error");
  const failure = agentError || message.error;
  if (failure && failure.message && !artifact) {
    const extra = document.createElement("p");
    extra.className = failure.code === "cancelled" ? "status-line" : "error";
    extra.textContent = failure.message;
    item.append(extra);
  }
  if (canRetry(message, failure) && !artifact) {
    item.append(retryRow(message));
  }
  return item;
}

function localEntry(message, request) {
  const form = document.createElement("form");
  form.className = "local-entry";
  const hint = document.createElement("p");
  hint.className = "status-line";
  hint.textContent = request.hint || "请输入要留在本机的内容。";
  const button = document.createElement("button");
  button.type = "submit";
  button.textContent = "加入本地内容并继续";
  if (request.kind === "file") {
    const file = document.createElement("input");
    file.type = "file";
    file.setAttribute("aria-label", "留在本机的文件");
    form.append(hint, file, button);
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      submitLocalFile(message, request, file, button);
    });
    return form;
  }
  const area = document.createElement("textarea");
  area.value = request.draft || "";
  area.setAttribute("aria-label", "留在本机的内容");
  form.append(hint, area, button);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    submitLocal(message, request, area, button);
  });
  return form;
}

async function submitLocal(message, request, area, button) {
  const runId = message.run_id || request.run_id;
  const text = area.value;
  if (!runId || !text.trim()) {
    showNote("请输入要留在本机的内容。");
    return;
  }
  button.disabled = true;
  try {
    await api("POST", `/api/runs/${encodeURIComponent(runId)}/content`, { text });
  } catch (error) {
    button.disabled = false;
    showNote(error.message || "没能加入本地内容。");
  }
}

async function submitLocalFile(message, request, file, button) {
  const runId = message.run_id || request.run_id;
  const chosen = file.files && file.files[0];
  if (!runId || !chosen) {
    showNote("请选择要留在本机的文件。");
    return;
  }
  button.disabled = true;
  try {
    const encoded = await encodeFile(chosen);
    await api("POST", `/api/runs/${encodeURIComponent(runId)}/content`, {
      file: encoded,
      media_type: chosen.type || "application/octet-stream",
    });
  } catch (error) {
    button.disabled = false;
    showNote(error.message || "没能加入本地内容。");
  }
}

async function encodeFile(file) {
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = "";
  const step = 0x8000;
  for (let index = 0; index < bytes.length; index += step) {
    binary += String.fromCharCode(...bytes.subarray(index, index + step));
  }
  return btoa(binary);
}

function toolLine(event) {
  const name = event.tool || event.name || "工具";
  const status = toolStatus(event.status);
  const brief = toolBrief(event);
  return brief ? `${name} · ${status} · ${brief}` : `${name} · ${status}`;
}

function toolStatus(status) {
  if (status === "waiting_confirm") return "等待确认";
  if (status === "success") return "成功";
  if (status === "failed") return "失败";
  return "运行";
}

function toolBrief(event) {
  if (event.result && event.result.error && event.result.error.message) {
    return String(event.result.error.message).slice(0, 40);
  }
  if (event.tool === "list_local_content") return "查看本机内容";
  if (event.tool === "forget_local_content") return "移除本机内容";
  if (event.tool === "lookup_demo") return "查询演示";
  if (event.tool === "generate_image") return "生成图片";
  if (event.tool === "accept_local_content") return "收集本机内容";
  return "";
}

function toolTimeline(message, calls) {
  const list = document.createElement("ol");
  list.className = "timeline";
  for (const call of calls) {
    const item = document.createElement("li");
    const name = document.createElement("div");
    name.className = "tool-name";
    name.textContent = call.tool || "工具";
    const status = document.createElement("div");
    status.className = "tool-status";
    status.textContent = toolLine(call);
    item.append(name, status);
    if (call.status === "waiting_confirm" && message.status === "running") {
      item.append(confirmRow(message, call));
    }
    list.append(item);
  }
  return list;
}

function confirmRow(message, call) {
  const row = document.createElement("div");
  row.className = "confirm-row";
  const yes = document.createElement("button");
  yes.type = "button";
  yes.textContent = "确认";
  yes.addEventListener("click", () => submitConfirm(message, true, yes));
  const no = document.createElement("button");
  no.type = "button";
  no.textContent = "拒绝";
  no.addEventListener("click", () => submitConfirm(message, false, no));
  row.append(yes, no);
  return row;
}

function waitingConfirm() {
  const assistant = currentAssistant();
  if (!assistant || assistant.status !== "running") return null;
  const found = (assistant.events || []).find((item) => item.type === "tool_call" && item.status === "waiting_confirm");
  return found ? assistant : null;
}

async function submitConfirm(message, allowed, button) {
  const runId = message.run_id || (waitingConfirm() && waitingConfirm().run_id);
  if (!runId) {
    showNote("现在没有在等待确认。");
    return;
  }
  if (button) button.disabled = true;
  try {
    await api("POST", `/api/runs/${encodeURIComponent(runId)}/confirm`, { allowed: Boolean(allowed) });
  } catch (error) {
    if (button) button.disabled = false;
    showNote(error.message || "没能提交确认。");
  }
}

function attachmentView(attachment) {
  const chip = document.createElement("div");
  chip.className = "attach-chip";
  if (attachment.kind === "image" && attachment.handle && state.currentId) {
    const image = document.createElement("img");
    image.alt = attachment.name || "附件图片";
    image.src = `/api/sessions/${state.currentId}/attachments/${attachment.handle}`;
    chip.append(image);
  }
  const label = document.createElement("span");
  label.textContent = attachment.name || MODALITY_LABELS[attachment.kind] || "文件";
  chip.append(label);
  return chip;
}

async function addPendingFile(file) {
  if (!file) return;
  if (!file.size) {
    showNote("空文件。");
    return;
  }
  if (file.size > 8 * 1024 * 1024) {
    showNote("这条消息太长了。");
    return;
  }
  const kind = kindFromFile(file);
  const accepted = acceptedModalities();
  if (accepted.length && accepted.indexOf(kind) < 0) {
    showNote(`这个模型不接受${MODALITY_LABELS[kind] || kind}。`);
  } else {
    showNote("");
  }
  try {
    if (!state.currentId) await createSession(false);
    const encoded = await encodeFile(file);
    const record = await api("POST", `/api/sessions/${state.currentId}/attachments`, {
      file: encoded,
      media_type: file.type || "application/octet-stream",
      name: file.name || "",
    });
    state.pending.push(record);
    renderPending();
    updateButtons();
  } catch (error) {
    showNote(error.message || "没能上传附件。");
  }
}

function kindFromFile(file) {
  const type = (file.type || "").toLowerCase();
  if (type.startsWith("image/")) return "image";
  if (type.startsWith("audio/")) return "audio";
  if (type.startsWith("video/")) return "video";
  return "file";
}

function acceptedModalities() {
  const items = (state.provider && state.provider.input_modalities) || ["text"];
  return items.slice();
}

function attachmentBlock() {
  const accepted = acceptedModalities();
  for (const item of state.pending) {
    if (accepted.indexOf(item.kind) < 0) {
      return `这个模型不接受${MODALITY_LABELS[item.kind] || item.kind}。`;
    }
  }
  return "";
}

function renderPending() {
  const box = document.querySelector("#attach-list");
  box.replaceChildren();
  if (!state.pending.length) {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  state.pending.forEach((item, index) => {
    const chip = attachmentView(item);
    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "去掉";
    remove.addEventListener("click", () => {
      state.pending.splice(index, 1);
      const blocked = attachmentBlock();
      showNote(blocked);
      renderPending();
      updateButtons();
    });
    chip.append(remove);
    box.append(chip);
  });
}

function canRetry(message, failure) {
  if (!failure || message.role !== "assistant" || message.status !== "failed") return false;
  if (failure.retryable === true) return true;
  return false;
}

function retryRow(message) {
  const row = document.createElement("div");
  row.className = "retry-row";
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = "重试";
  button.addEventListener("click", () => retryLast(message));
  row.append(button);
  return row;
}

async function retryLast(message) {
  if (state.sending) return;
  const messages = (state.session && state.session.messages) || [];
  const index = messages.indexOf(message);
  let user = null;
  for (let cursor = index - 1; cursor >= 0; cursor -= 1) {
    if (messages[cursor].role === "user") {
      user = messages[cursor];
      break;
    }
  }
  if (!user) {
    showNote("找不到要重试的消息。");
    return;
  }
  input.value = user.text || "";
  state.pending = (user.attachments || []).map((item) => ({ ...item }));
  renderPending();
  await sendMessage();
}

function progressLine(event) {
  if (event.percent != null) return `进度 ${event.percent}%`;
  if (event.max) return `进度 ${event.value}/${event.max}`;
  return "正在生成";
}

function artifactSrc(event) {
  const url = typeof event.url === "string" ? event.url : "";
  if (!url.startsWith("/sessions/")) return "";
  return url;
}

function updateButtons() {
  const blocked = attachmentBlock();
  const empty = !input.value.trim() && !state.pending.length;
  document.querySelector("#send").disabled = state.sending || empty || Boolean(blocked);
  document.querySelector("#stop").disabled = !state.sending;
  if (blocked && !state.sending) showNote(blocked);
}

function showNote(message) {
  if (!message) {
    note.hidden = true;
    note.textContent = "";
    return;
  }
  note.hidden = false;
  note.textContent = message;
}

function openSettings() {
  const form = document.querySelector("#settings-form");
  const provider = state.provider || {};
  form.elements.base_url.value = provider.base_url || "";
  form.elements.model.value = provider.model || "";
  form.elements.api_key.value = "";
  document.querySelector("#key-hint").textContent = keyHint(provider);
  showSettingsError("");
  settings.showModal();
}

async function saveSettings(event) {
  event.preventDefault();
  const form = event.currentTarget;
  showSettingsError("");
  const body = {
    base_url: form.elements.base_url.value.trim(),
    model: form.elements.model.value.trim(),
  };
  const key = form.elements.api_key.value.trim();
  if (key) body.api_key = key;
  try {
    state.provider = await api("PUT", "/api/provider", body);
    form.elements.api_key.value = "";
    document.querySelector("#key-hint").textContent = keyHint(state.provider);
    if (state.provider.configured) showNote("");
    renderChrome();
    settings.close();
  } catch (error) {
    showSettingsError(error.message || "没能保存。");
  }
}

async function clearKey() {
  const form = document.querySelector("#settings-form");
  try {
    state.provider = await api("PUT", "/api/provider", {
      base_url: form.elements.base_url.value.trim(),
      model: form.elements.model.value.trim(),
      clear_key: true,
    });
    form.elements.api_key.value = "";
    document.querySelector("#key-hint").textContent = keyHint(state.provider);
    showSettingsError("");
    renderChrome();
  } catch (error) {
    showSettingsError(error.message || "没能清除。");
  }
}

function showSettingsError(message) {
  const error = document.querySelector("#settings-error");
  if (!message) {
    error.hidden = true;
    error.textContent = "";
    return;
  }
  error.hidden = false;
  error.textContent = message;
}

function keyHint(provider) {
  if (!provider || !provider.configured) return "密钥只保存在本机。保存后这一栏会清空，页面上方会留下末四位。";
  if (provider.key_source === "environment") {
    return `正在使用环境变量里的密钥，末四位 ${provider.key_hint}。在这里填写会改成本机文件。`;
  }
  return `本机已保存密钥，末四位 ${provider.key_hint}。留空表示不修改。`;
}

async function api(method, path, body) {
  const options = { method, headers: {} };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(payload.message || "请求失败。");
    error.code = payload.code;
    throw error;
  }
  return payload;
}

async function readSse(response, onEvent) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const step = await reader.read();
    if (step.done) break;
    buffer += decoder.decode(step.value, { stream: true });
    buffer = buffer.replace(/\r\n/g, "\n");
    let splitAt = buffer.indexOf("\n\n");
    while (splitAt >= 0) {
      const raw = buffer.slice(0, splitAt);
      buffer = buffer.slice(splitAt + 2);
      const event = parseFrame(raw);
      if (event) onEvent(event);
      splitAt = buffer.indexOf("\n\n");
    }
  }
}

function parseFrame(raw) {
  const data = [];
  let name = "";
  for (const line of raw.split("\n")) {
    if (line.startsWith("event:")) name = line.slice(6).trim();
    else if (line.startsWith("data:")) data.push(line.slice(5).trim());
  }
  if (!data.length) return null;
  try {
    const payload = JSON.parse(data.join("\n"));
    if (name && !payload.type) payload.type = name;
    return payload;
  } catch (error) {
    return null;
  }
}

function formatTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function rememberSession(sessionId) {
  try { localStorage.setItem("mas.session", sessionId); } catch (error) { return; }
}

function readSavedSession() {
  try { return localStorage.getItem("mas.session"); } catch (error) { return null; }
}
