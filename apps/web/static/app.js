const state = {
  provider: null,
  sessions: [],
  currentId: null,
  session: null,
  sending: false,
  stopped: false,
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
  if (event.key !== "Escape" || settings.open || !state.sending) return;
  event.preventDefault();
  stopRun();
});

document.querySelector("#new-chat").addEventListener("click", () => createSession(true));
document.querySelector("#toggle-sessions").addEventListener("click", () => {
  sidebar.classList.toggle("open");
});
document.querySelector("#open-settings").addEventListener("click", openSettings);
document.querySelector("#close-settings").addEventListener("click", () => settings.close());
document.querySelector("#settings-form").addEventListener("submit", saveSettings);
document.querySelector("#clear-key").addEventListener("click", clearKey);
document.querySelector("#file").addEventListener("change", (event) => {
  if (event.target.files && event.target.files.length) {
    showNote("这一版只接受文字。图片、音频、视频和文件还不能发送。");
    event.target.value = "";
  }
});
document.querySelector("#stop").addEventListener("click", stopRun);

boot();

async function boot() {
  try {
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
  if (!text.trim() || state.sending) return;
  if (!state.currentId) await createSession(false);
  state.stopped = false;
  state.sending = true;
  state.session.messages.push({
    message_id: "local-user",
    role: "user",
    text: text.trim(),
    status: "completed",
    error: null,
  });
  state.session.messages.push({
    message_id: "local-assistant",
    role: "assistant",
    text: "",
    status: "running",
    error: null,
    events: [],
  });
  input.value = "";
  showNote("");
  render();
  try {
    const response = await fetch(`/api/sessions/${state.currentId}/messages`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
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
    state.session.run_id = event.run_id;
    state.session.running = true;
  } else if (event.type === "message.delta") {
    if (state.stopped) return;
    assistant.text += event.text || "";
    assistant.status = "running";
  } else if (
    event.type === "tool_call" ||
    event.type === "progress" ||
    event.type === "artifact" ||
    event.type === "error" ||
    event.type === "content_request"
  ) {
    if (state.stopped && (event.type === "progress" || event.type === "artifact")) return;
    if (event.type === "content_request" && event.run_id) assistant.run_id = event.run_id;
    rememberAgentEvent(assistant, event);
    if (event.type === "content_request") document.querySelector("#live").textContent = "请输入要留在本机的内容。";
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
    assistant.error = { code: event.code, message: event.message };
    state.sending = false;
    state.session.running = false;
    document.querySelector("#live").textContent = event.message || "";
  }
  renderTranscript();
  updateButtons();
}

function resetAssistantForReplay() {
  const assistant = currentAssistant();
  if (!assistant) return;
  assistant.text = "";
  assistant.events = [];
  assistant.error = null;
}

function rememberAgentEvent(assistant, event) {
  const events = assistant.events || (assistant.events = []);
  if (event.type === "content_request" && events.some((item) => item.type === "content_request")) return;
  if (event.type === "tool_call" && events.some((item) => item.type === "tool_call")) return;
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
    tool: event.tool,
    arguments: event.arguments,
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
  updateButtons();
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
  document.querySelector("#model-line").textContent = provider.model || "未设置模型";
  document.querySelector("#modality-line").textContent = provider.configured
    ? `接受文字 · 已保存密钥 ${provider.key_hint}`
    : "接受文字 · 还没有 API key";
  document.title = state.session && state.session.title ? state.session.title : "对话";
}

function renderTranscript() {
  const nearBottom = transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < 80;
  transcript.replaceChildren();
  const messages = (state.session && state.session.messages) || [];
  if (!messages.length) {
    const empty = document.createElement("p");
    empty.className = "empty-thread";
    empty.textContent = "写一条消息。要一张图时，说明要生成图片。画面描述在对话框里填写，留在本机。";
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
  const call = events.find((entry) => entry.type === "tool_call");
  if (call) {
    const line = document.createElement("p");
    line.className = "status-line";
    line.textContent = toolLine(call);
    item.append(line);
  }
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
  return item;
}

function localEntry(message, request) {
  const form = document.createElement("form");
  form.className = "local-entry";
  const hint = document.createElement("p");
  hint.className = "status-line";
  hint.textContent = request.hint || "请输入要留在本机的内容。";
  const area = document.createElement("textarea");
  area.value = request.draft || "";
  area.setAttribute("aria-label", "留在本机的内容");
  const button = document.createElement("button");
  button.type = "submit";
  button.textContent = "加入本地内容并继续";
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

function toolLine(event) {
  const args = event.arguments || {};
  const aspect = args.aspect_ratio ? ` · ${args.aspect_ratio}` : "";
  return `正在生成图片${aspect}`;
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
  document.querySelector("#send").disabled = state.sending || !input.value.trim();
  document.querySelector("#stop").disabled = !state.sending;
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
