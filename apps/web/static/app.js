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
document.querySelector("#export-egress").addEventListener("click", exportEgress);
document.querySelector("#refresh-dataflow").addEventListener("click", refreshDataflow);
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
  input.value = "";
  state.pending = [];
  await runTurn(`/api/sessions/${state.currentId}/messages`, {
    text,
    attachments: attachments.map((item) => ({ handle: item.handle })),
    ...(document.querySelector("#multi-agent").checked ? { mode: "multi" } : {}),
  }, text.trim(), attachments);
}

// #53 定制角色表单：同样的流式回合，只是请求发到 /character，不经过对话模型。
async function sendCharacter(body, label) {
  if (state.sending) return false;
  if (!state.currentId) await createSession(false);
  await runTurn(`/api/sessions/${state.currentId}/character`, body, label, []);
  return true;
}
window.sendCharacter = sendCharacter;

async function runTurn(url, body, userText, attachments) {
  state.stopped = false;
  state.sending = true;
  state.session.messages.push({
    message_id: "local-user",
    role: "user",
    text: userText,
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
    egress: [],
  });
  showNote("");
  render();
  try {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
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
    assistant.egress = [];
    state.session.run_id = event.run_id;
    state.session.running = true;
  } else if (event.type === "runtime.step") {
    rememberRuntime(assistant, event);
  } else if (typeof event.type === "string" && event.type.startsWith("egress.")) {
    rememberEgress(assistant, event);
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
    event.type === "content_request" ||
    event.type === "image_config" ||
    event.type === "agent"
  ) {
    if (state.stopped && (event.type === "progress" || event.type === "artifact")) return;
    if (event.type === "content_request" && event.run_id) assistant.run_id = event.run_id;
    rememberAgentEvent(assistant, event);
    if (event.type === "content_request") {
      document.querySelector("#live").textContent = event.hint || "请输入要留在本机的内容。";
    }
    if (event.type === "artifact") {
      document.querySelector("#live").textContent = event.media === "html" ? "已收到等距图" : "已收到图片";
    }
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
  renderEgress();
  if (event.type === "message.completed" || event.type === "run.failed") scheduleDataflow();
  updateButtons();
}

function resetAssistantForReplay() {
  const assistant = currentAssistant();
  if (!assistant) return;
  assistant.text = "";
  assistant.events = [];
  assistant.runtime = [];
  assistant.egress = [];
  assistant.error = null;
}

function rememberAgentEvent(assistant, event) {
  const events = assistant.events || (assistant.events = []);
  if (event.type === "content_request" && events.some((item) => item.type === "content_request")) return;
  if (event.type === "agent") {
    const node = events.find((item) => item.type === "agent" && item.run_id === event.run_id);
    if (node) {
      Object.assign(node, event);
      return;
    }
  }
  if (event.type === "tool_call") {
    const open = [...events].reverse().find(
      (item) => item.type === "tool_call" && item.tool === event.tool
        && (item.agent_run_id || "") === (event.agent_run_id || "")
        && (item.status === "running" || item.status === "waiting_confirm"),
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
  if (
    event.type === "artifact"
    && event.artifact_id
    && events.some((item) => item.type === "artifact" && item.artifact_id === event.artifact_id)
  ) return;
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
    notice: event.notice,
    egress: event.egress,
    agent: event.agent,
    agent_run_id: event.agent_run_id,
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
  renderEgress();
  scheduleDataflow();
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
  const tree = agentTree(assistant);
  if (tree) log.append(tree);
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

// 数据出站（#57）：网关和出图代码在请求离开本进程前后记下的审计记录。只显示主机、大小和白话原因，不显示正文。
const EGRESS_FIELDS = [
  "type", "egress_id", "ts", "channel", "destination_host", "destination_scope", "modality",
  "char_count", "byte_count", "message_count", "tool_contract_bytes", "contains_local_derived",
  "content_fingerprint", "handle_id_prefix", "model", "session_id", "run_id", "agent_run_id",
  "reason_code", "http_status", "policy", "exempt_bytes", "tool",
];
const EGRESS_STATES = {
  "egress.planned": "准备发出",
  "egress.sent": "已发出",
  "egress.blocked": "已拦下",
  "egress.failed": "发出失败",
};
const EGRESS_CHANNELS = {
  chat_completion: "对话模型",
  remote_image: "第三方出图",
  comfyui_local: "ComfyUI",
  toolpack: "工具包",
  other: "其他出口",
};
const EGRESS_SCOPES = { local: "本机", lan: "局域网", third_party: "第三方" };
const EGRESS_REASONS = {
  local_content: "请求里混进了本地内容，已在发出前拦下；本地内容没有离开本机。",
  local_prompt_remote_refused: "本地提示词默认不发给第三方出图服务，已拦下。",
  user_declined: "你没有同意把本地提示词发给第三方出图服务，已拦下。",
  http_error: "对方返回了错误。",
  connect_error: "连接失败，请求没有送达。",
  timeout: "等待超时。",
  cancelled: "已停止。",
  rejected: "对方拒绝了这次请求。",
  handle_not_found: "句柄不存在或不属于本会话，已拦下；没有读取任何本地内容。",
  handle_expired: "本地内容句柄已过期或已撤销，已拦下；本地内容没有离开本机。",
  handle_forbidden: "本地内容句柄的用途或作用域不允许这次调用，已拦下。",
  local_content_encoded: "请求里有经过编码（如 base64）的本地内容，已在发出前拦下；本地内容没有离开本机。",
  egress_policy_denied: "这次请求的字段或出口不在出站策略里，已拦下，没有打开连接。",
  permission_denied: "这个工具包没有联网权限（或试图连本机地址），已在沙箱里拦下，没有打开连接。",
  local_fingerprint: "请求里有本地内容的入库指纹，已在发出前拦下；本地内容和它的指纹都没有离开本机。",
};

function rememberEgress(assistant, event) {
  const items = assistant.egress || (assistant.egress = []);
  if (items.some((item) => item.egress_id === event.egress_id && item.type === event.type)) return;
  const entry = {};
  for (const key of EGRESS_FIELDS) if (event[key] !== undefined) entry[key] = event[key];
  items.push(entry);
}

// 同一次请求（egress_id）只画一行，状态取最新的那条。
function egressRows(items) {
  const rows = [];
  const byId = {};
  for (const item of items || []) {
    const key = item.egress_id || `${rows.length}`;
    if (byId[key]) {
      Object.assign(byId[key], item);
    } else {
      byId[key] = { ...item };
      rows.push(byId[key]);
    }
  }
  return rows;
}

function egressLine(row) {
  const channel = (EGRESS_CHANNELS[row.channel] || row.channel || "出站") + (row.tool ? `（${row.tool}）` : "");
  const scope = EGRESS_SCOPES[row.destination_scope] || "";
  const host = row.destination_host || "未配置的地址";
  const parts = [`${channel} → ${scope ? scope + " " : ""}${host}`];
  if (row.byte_count != null) parts.push(`${row.byte_count} 字节`);
  if (row.message_count != null) parts.push(`${row.message_count} 条消息`);
  if (row.contains_local_derived) parts.push("含本地派生内容");
  if (row.http_status != null) parts.push(`HTTP ${row.http_status}`);
  return parts.join(" · ");
}

function renderEgress() {
  const log = document.querySelector("#egress-log");
  const status = document.querySelector("#egress-status");
  if (!log || !status) return;
  const assistant = currentAssistant();
  const rows = egressRows(assistant && assistant.egress);
  log.replaceChildren();
  if (!rows.length) {
    status.textContent = "这一轮发出的请求会列在这里：发给谁、多大、有没有被拦下。不显示正文。";
    return;
  }
  const blocked = rows.filter((row) => row.type === "egress.blocked").length;
  const sent = rows.filter((row) => row.type === "egress.sent").length;
  status.textContent = `这一轮 ${rows.length} 次出站：已发出 ${sent}，拦下 ${blocked}。`;
  for (const row of rows) {
    const item = document.createElement("li");
    item.className = "egress-row";
    if (row.type === "egress.blocked") item.classList.add("egress-blocked");
    if (row.type === "egress.failed") item.classList.add("egress-failed");
    const kind = document.createElement("span");
    kind.className = "kind";
    kind.textContent = EGRESS_STATES[row.type] || row.type || "";
    const body = document.createElement("span");
    body.textContent = egressLine(row);
    item.append(kind, body);
    if (row.reason_code) {
      const reason = document.createElement("p");
      reason.className = "egress-reason";
      reason.textContent = EGRESS_REASONS[row.reason_code] || `原因：${row.reason_code}`;
      item.append(reason);
    }
    log.append(item);
  }
}

async function exportEgress() {
  const query = state.currentId ? `?session_id=${encodeURIComponent(state.currentId)}` : "";
  const native = desktopApi();
  if (native && native.save_egress_export) {
    // 13.B：桌面壳用系统「另存为」对话框；路径不回到页面。
    try {
      const result = await native.save_egress_export(state.currentId || null);
      if (result && result.ok) showNote("出站审计已导出。");
      else if (!result || result.code !== "cancelled") showNote("导出失败。");
    } catch (error) {
      showNote("导出失败。");
    }
    return;
  }
  try {
    const response = await fetch(`/api/egress/export${query}`);
    if (!response.ok) throw new Error((await response.json().catch(() => ({}))).message || "导出失败。");
    const blob = await response.blob();
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = `egress-${state.currentId || "all"}.json`;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(link.href), 1000);
  } catch (error) {
    showNote(error.message || "导出失败。");
  }
}

// 确认卡上的出站摘要由宿主生成（会不会联网、发给谁、读不读本地内容、句柄用途），不是模型的解释文字。
function egressSummaryNode(summary) {
  if (!summary || typeof summary !== "object") return null;
  const box = document.createElement("div");
  box.className = "confirm-egress";
  const title = document.createElement("strong");
  title.textContent = "数据出站：";
  const text = document.createElement("span");
  text.textContent = summary.text || (summary.network ? "会联网。" : "不联网，只在本机执行。");
  box.append(title, text);
  // 12.E 固定区块：出站主机 / 含本地派生内容 / 句柄用途，三项总在，顺序固定，值由宿主生成。
  const fixed = Array.isArray(summary.fixed) ? summary.fixed : [];
  if (fixed.length) {
    const list = document.createElement("dl");
    list.className = "confirm-fixed";
    for (const row of fixed) {
      const term = document.createElement("dt");
      term.textContent = String(row.label || row.key || "");
      const value = document.createElement("dd");
      value.textContent = String(row.value || "");
      value.dataset.key = String(row.key || "");
      list.append(term, value);
    }
    box.append(list);
  }
  if (summary.local_to_third_party) {
    const warn = document.createElement("p");
    warn.className = "confirm-warn";
    warn.textContent = "本地提示词将发往第三方出图服务（不会发给对话模型）。";
    box.append(warn);
  }
  return box;
}

// 12.E 会话级数据流图：GET /api/dataflow 由宿主从出站审计和句柄元数据拼出来，这里只按泳道列出节点和边。
const DATAFLOW_EDGE = { holds: "持有", uses: "使用", egress: "出站" };
let dataflowTimer = null;

function scheduleDataflow() {
  clearTimeout(dataflowTimer);
  dataflowTimer = setTimeout(refreshDataflow, 300);
}

async function refreshDataflow() {
  const graph = document.querySelector("#dataflow-graph");
  const status = document.querySelector("#dataflow-status");
  if (!graph || !status) return;
  if (!state.currentId) {
    graph.replaceChildren();
    return;
  }
  let flow;
  try {
    const response = await fetch(`/api/dataflow?session_id=${encodeURIComponent(state.currentId)}`);
    if (!response.ok) throw new Error((await response.json().catch(() => ({}))).message || "读不了数据流图。");
    flow = await response.json();
  } catch (error) {
    status.textContent = error.message || "读不了数据流图。";
    return;
  }
  renderDataflow(flow, graph, status);
}

function renderDataflow(flow, graph, status) {
  const nodes = {};
  for (const node of flow.nodes || []) nodes[node.id] = node;
  const sum = flow.summary || {};
  status.textContent = `本会话：${sum.handles || 0} 个句柄；出站 已发 ${sum.sent || 0} · 拦下 ${sum.blocked || 0} · 失败 ${sum.failed || 0}` +
    (sum.local_derived_to_third_party ? `；本地派生内容发往第三方 ${sum.local_derived_to_third_party} 次` : "") + "。";
  graph.replaceChildren();
  for (const edge of flow.edges || []) {
    const from = nodes[edge.from] || {};
    const to = nodes[edge.to] || {};
    const item = document.createElement("li");
    const counts = edge.counts || {};
    if (to.destination === "third_party") item.classList.add("flow-third-party");
    if (counts.blocked) item.classList.add("flow-blocked");
    const kind = document.createElement("span");
    kind.className = "kind";
    kind.textContent = DATAFLOW_EDGE[edge.kind] || edge.kind || "";
    const body = document.createElement("span");
    const parts = [`${from.label || edge.from} → ${to.label || edge.to}`];
    if (from.purpose_label) parts.push(`用途：${from.purpose_label}`);
    if (to.purpose_label) parts.push(`用途：${to.purpose_label}`);
    if (edge.kind !== "holds") parts.push(`已发 ${counts.sent || 0} · 拦下 ${counts.blocked || 0} · 失败 ${counts.failed || 0}`);
    if (edge.local_derived) parts.push(`含本地派生 ${edge.local_derived} 次`);
    body.textContent = parts.join(" · ");
    item.append(kind, body);
    const reasons = Object.entries(edge.reasons || {});
    if (reasons.length) {
      const reason = document.createElement("p");
      reason.className = "egress-reason";
      reason.textContent = reasons.map(([code, n]) => `${EGRESS_REASONS[code] || code}（${n}）`).join(" ");
      item.append(reason);
    }
    graph.append(item);
  }
}

// markdown.js 只产出转义后的白名单 HTML；万一没加载，退回纯文本转义。
function markdownHtml(source) {
  if (typeof window.renderMarkdown === "function") return window.renderMarkdown(source);
  const div = document.createElement("div");
  div.textContent = source;
  return div.innerHTML.replace(/\n/g, "<br>");
}

// 多智能体：编排者 → 子 Agent → 工具调用，按 parent_run_id 挂成一棵树。
function agentTree(assistant) {
  const events = (assistant && assistant.events) || [];
  const nodes = events.filter((item) => item.type === "agent");
  if (!nodes.length) return null;
  const root = document.createElement("li");
  root.className = "agent-tree";
  const head = document.createElement("span");
  head.className = "kind";
  head.textContent = "编排者";
  root.append(head);
  const byParent = {};
  for (const node of nodes) (byParent[node.parent_run_id] = byParent[node.parent_run_id] || []).push(node);
  const known = new Set(nodes.map((node) => node.run_id));
  const tops = nodes.filter((node) => !known.has(node.parent_run_id));
  const build = (list) => {
    const ul = document.createElement("ul");
    for (const node of list) {
      const li = document.createElement("li");
      const label = { running: "进行中", completed: "完成", failed: "失败", cancelled: "已停止" }[node.status] || node.status || "";
      const ms = node.ms != null ? ` · ${node.ms}ms` : "";
      li.textContent = `${node.agent}（${node.role || ""}）· ${label}${ms}`;
      const calls = events.filter((item) => item.type === "tool_call" && item.agent_run_id === node.run_id);
      if (node.summary) {
        const details = document.createElement("details");
        const title = document.createElement("summary");
        title.textContent = "子 Agent 摘要";
        const body = document.createElement("div");
        body.className = "markdown";
        body.innerHTML = markdownHtml(node.summary);
        details.append(title, body);
        li.append(details);
      }
      if (calls.length) {
        const sub = document.createElement("ul");
        for (const call of calls) {
          const row = document.createElement("li");
          row.textContent = `${call.tool} · ${toolLine(call)}`;
          sub.append(row);
        }
        li.append(sub);
      }
      if (byParent[node.run_id]) li.append(build(byParent[node.run_id]));
      ul.append(li);
    }
    return ul;
  };
  root.append(build(tops));
  return root;
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
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "session-delete";
    remove.title = "删除这个对话";
    remove.setAttribute("aria-label", "删除这个对话");
    remove.textContent = "×";
    remove.disabled = Boolean(session.running);
    remove.addEventListener("click", (event) => {
      event.stopPropagation();
      deleteSession(session).catch((error) => showNote(error.message || "没能删除这个对话。"));
    });
    item.className = "session-item";
    item.append(button, remove);
    list.append(item);
  }
}

// 删除对话：先确认；还在回答的会话由服务端拒绝（409 session_running），这里只提示先停下。
async function deleteSession(session) {
  if (session.running) {
    showNote("这个对话还在回答，先停下再删。");
    return;
  }
  const name = session.title || "新对话";
  if (!window.confirm(`删除“${name}”？对话记录、附件和生成的图片都会删掉，不能恢复。`)) return;
  await api("POST", `/api/sessions/${session.session_id}/delete`, {});
  const wasCurrent = session.session_id === state.currentId;
  await loadSessions();
  if (wasCurrent) {
    state.currentId = null;
    state.session = null;
    try { localStorage.removeItem("mas.session"); } catch (error) { /* 忽略 */ }
    await createSession(false);
    return;
  }
  render();
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
  const askedAt = events.map((entry) => entry.type).lastIndexOf("content_request");
  const pendingContent = askedAt >= 0 ? events[askedAt] : undefined;
  const artifacts = events.filter((entry) => entry.type === "artifact");
  // 只看收集请求之后的事件：之前的工具时间线（如 accept_local_content 本身）不该挡住输入框。
  const collecting = askedAt >= 0 && !events.slice(askedAt + 1).some((entry) => entry.type === "tool_call" || entry.type === "error");
  const showEntry = pendingContent && !artifacts.length && message.status === "running" && collecting;
  if (message.status === "running" && !showEntry) item.classList.add("streaming");
  const who = document.createElement("span");
  who.className = "who";
  who.textContent = message.role === "user" ? "你" : "助手";
  // 助手回复按 Markdown 渲染（流式时每次整段重渲染）；用户消息保持纯文本，原样显示。
  const text = document.createElement("div");
  text.className = "body";
  if (message.role === "user") {
    text.classList.add("plain");
    text.textContent = message.text || "";
  } else {
    text.classList.add("markdown");
    text.innerHTML = markdownHtml(message.text || "");
  }
  item.append(who, text);
  const config = events.filter((entry) => entry.type === "image_config").pop();
  if (config && config.summary) {
    const line = document.createElement("p");
    line.className = "image-config";
    line.textContent = config.summary;
    item.append(line);
  }
  for (const attachment of message.attachments || []) {
    item.append(attachmentView(attachment));
  }
  const calls = events.filter((entry) => entry.type === "tool_call");
  if (calls.length) item.append(toolTimeline(message, calls));
  // 只显示仍在进行中的进度：其后已有成片或新的工具调用时，旧进度不再显示。
  const latest = [...events].reverse().find((entry) => ["progress", "artifact", "tool_call"].includes(entry.type));
  const progress = latest && latest.type === "progress" ? latest : null;
  if (showEntry) item.append(localEntry(message, pendingContent));
  if (progress && message.status === "running") {
    const line = document.createElement("p");
    line.className = "status-line";
    line.textContent = progressLine(progress);
    item.append(line);
  }
  for (const artifact of artifacts) {
    if (artifact.media !== "image") continue;
    const src = artifactSrc(artifact);
    if (!src) continue;
    const image = document.createElement("img");
    image.alt = "生成的图片";
    image.src = src;
    item.append(image);
  }
  for (const artifact of artifacts) {
    if (artifact.media !== "html") continue;
    const src = artifactSrc(artifact);
    if (src) {
      const link = document.createElement("a");
      link.className = "artifact-link";
      link.href = src;
      link.target = "_blank";
      link.rel = "noopener";
      link.textContent = "打开等距图";
      item.append(link);
    }
  }
  const agentError = events.find((entry) => entry.type === "error");
  const failure = agentError || message.error;
  if (failure && failure.message && (!artifacts.length || failure.code === "cancelled")) {
    const extra = document.createElement("p");
    extra.className = failure.code === "cancelled" ? "status-line" : "error";
    extra.textContent = failure.message;
    item.append(extra);
  }
  if (canRetry(message, failure) && !artifacts.length) {
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
  const actions = document.createElement("div");
  actions.className = "confirm-row";
  const button = document.createElement("button");
  button.type = "submit";
  button.textContent = "加入本地内容并继续";
  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.textContent = "取消";
  cancel.addEventListener("click", () => skipLocal(message, request, cancel, button));
  actions.append(button, cancel);
  if (request.kind === "file") {
    const file = document.createElement("input");
    file.type = "file";
    file.setAttribute("aria-label", "留在本机的文件");
    form.classList.add("local-entry-file");
    form.append(hint, file);
    const native = desktopApi();
    if (native && native.pick_local_file) {
      // 13.B：桌面壳用系统「打开文件」对话框。路径和字节只在宿主 Python 里，JS 只拿到 ok / 错误码。
      const pick = document.createElement("button");
      pick.type = "button";
      pick.textContent = "用系统对话框选择…";
      pick.addEventListener("click", () => pickNativeFile(message, request, pick, button));
      form.append(pick);
    }
    const drop = document.createElement("p");
    drop.className = "status-line";
    drop.textContent = "也可以把文件拖到这里。文件只存在本机，模型只看到句柄。";
    form.append(drop, actions);
    form.addEventListener("dragover", (event) => event.preventDefault());
    form.addEventListener("drop", (event) => {
      event.preventDefault();
      event.stopPropagation();
      const dropped = event.dataTransfer && event.dataTransfer.files && event.dataTransfer.files[0];
      if (dropped) submitLocalBlob(message, request, dropped, button);
    });
    form._localDrop = (dropped) => submitLocalBlob(message, request, dropped, button);
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      submitLocalFile(message, request, file, button);
    });
    return form;
  }
  const area = document.createElement("textarea");
  area.value = request.draft || "";
  area.setAttribute("aria-label", "留在本机的内容");
  form.append(hint, area, actions);
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

async function skipLocal(message, request, cancel, submit) {
  const runId = message.run_id || request.run_id;
  if (!runId) {
    showNote("现在没有在等待本地内容。");
    return;
  }
  cancel.disabled = true;
  if (submit) submit.disabled = true;
  try {
    await api("POST", `/api/runs/${encodeURIComponent(runId)}/content`, { skip: true });
  } catch (error) {
    cancel.disabled = false;
    if (submit) submit.disabled = false;
    showNote(error.message || "没能取消。");
  }
}

function desktopApi() {
  return (window.pywebview && window.pywebview.api) || null;
}

async function pickNativeFile(message, request, pick, button) {
  const runId = message.run_id || request.run_id;
  const native = desktopApi();
  if (!runId || !native) return;
  pick.disabled = true;
  button.disabled = true;
  try {
    const result = await native.pick_local_file(runId);
    if (!result || !result.ok) {
      pick.disabled = false;
      button.disabled = false;
      if (result && result.code === "cancelled") return;
      const reasons = {
        payload_too_large: "文件太大了，本机内容最大 256 KB。",
        empty: "这个文件是空的。",
        not_waiting: "现在没有在等待本地内容。",
      };
      showNote(reasons[result && result.code] || "没能加入本地内容。");
    }
  } catch (error) {
    pick.disabled = false;
    button.disabled = false;
    showNote("没能打开系统文件对话框。");
  }
}

// 13.B：拖进窗口的文件。WebView 默认会跳转去打开 file://，这里拦下来：
// 有等待中的「本机文件」请求就交给它（走同一个上传接口、只得句柄），否则只提示，不读文件。
document.addEventListener("dragover", (event) => event.preventDefault());
document.addEventListener("drop", (event) => {
  event.preventDefault();
  const dropped = event.dataTransfer && event.dataTransfer.files && event.dataTransfer.files[0];
  if (!dropped) return;
  const forms = [...document.querySelectorAll("form.local-entry-file")].filter((form) => form._localDrop);
  const target = forms[forms.length - 1];
  if (target) {
    target._localDrop(dropped);
  } else {
    showNote("现在没有在等待本机文件。要让模型看图，请用输入框旁的附件按钮。");
  }
});

async function submitLocalFile(message, request, file, button) {
  const chosen = file.files && file.files[0];
  return submitLocalBlob(message, request, chosen, button);
}

async function submitLocalBlob(message, request, chosen, button) {
  const runId = message.run_id || request.run_id;
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
  if (event.tool === "retrieve_local") return "检索本机会话";
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
    name.textContent = (call.agent ? `${call.agent} · ` : "") + (call.tool || "工具");
    const status = document.createElement("div");
    status.className = "tool-status";
    status.textContent = toolLine(call);
    item.append(name, status);
    if (call.status === "waiting_confirm" && message.status === "running") {
      item.append(confirmRow(message, call));
    }
    if (call.tool === "retrieve_local") {
      item.append(retrieveView(call.result));
    }
    list.append(item);
  }
  return list;
}

function retrieveView(result) {
  const list = document.createElement("ul");
  list.className = "retrieve-list";
  const items = (result && result.items) || [];
  if (!items.length) {
    const empty = document.createElement("li");
    empty.textContent = "没有匹配的来源";
    list.append(empty);
    return list;
  }
  for (const entry of items) {
    const row = document.createElement("li");
    const source = entry.source_id || "";
    if (entry.url) {
      const link = document.createElement("a");
      link.href = entry.url;
      link.textContent = source;
      row.append(link);
    } else {
      const label = document.createElement("span");
      label.textContent = source;
      row.append(label);
    }
    const meta = document.createElement("span");
    meta.className = "retrieve-meta";
    meta.textContent = [entry.kind, entry.media_type].filter(Boolean).join(" · ");
    if (meta.textContent) row.append(meta);
    list.append(row);
  }
  return list;
}

// 12.G 宿主模板确认卡（mas.confirm/1）：标题、宿主说明、参数表都来自宿主，只用 textContent 显示；
// 不显示模型这一步说的话，也不显示工具结果里的文字。参数是模型给的数据，标明「仅供核对」。
function confirmCardNode(card) {
  if (!card || typeof card !== "object" || card.by !== "host") return null;
  const box = document.createElement("div");
  box.className = "confirm-card" + (card.risk === "high" ? " confirm-high" : "");
  const title = document.createElement("strong");
  title.textContent = String(card.title || "");
  box.append(title);
  for (const line of Array.isArray(card.lines) ? card.lines : []) {
    const item = document.createElement("p");
    item.textContent = String(line);
    box.append(item);
  }
  const rows = Array.isArray(card.args) ? card.args : [];
  if (rows.length) {
    const label = document.createElement("div");
    label.className = "confirm-args-label";
    label.textContent = String(card.args_label || "参数");
    const list = document.createElement("dl");
    list.className = "confirm-args";
    for (const row of rows) {
      const term = document.createElement("dt");
      term.textContent = String(row.key || "");
      const value = document.createElement("dd");
      value.textContent = String(row.value ?? "");
      list.append(term, value);
    }
    box.append(label, list);
  }
  if (card.risk === "high") {
    const warn = document.createElement("p");
    warn.className = "confirm-warn";
    warn.textContent = String(card.ack_text || "高危组合：读本地内容又联网，确认后还要再确认一次。");
    box.append(warn);
  }
  return box;
}

function confirmRow(message, call) {
  const row = document.createElement("div");
  row.className = "confirm-row";
  const card = call && call.card;
  const yes = document.createElement("button");
  yes.type = "button";
  yes.textContent = card && card.risk === "high" ? "确认（还要再确认一次）" : "确认";
  yes.addEventListener("click", () => {
    // 12.G 高危组合：第二次确认的文字也来自宿主卡片；取消就不提交。
    if (card && card.requires_ack) {
      if (!window.confirm(String(card.ack_text || "高危组合：读本地内容又联网。确定继续吗？"))) return;
      submitConfirm(message, true, yes, card.requires_ack);
      return;
    }
    submitConfirm(message, true, yes);
  });
  const no = document.createElement("button");
  no.type = "button";
  no.textContent = "拒绝";
  no.addEventListener("click", () => submitConfirm(message, false, no));
  if (call && call.notice) {
    const notice = document.createElement("p");
    notice.className = "confirm-notice";
    notice.textContent = call.notice;
    row.append(notice);
  }
  const cardNode = confirmCardNode(card);
  if (cardNode) row.append(cardNode);
  const summary = egressSummaryNode(call && call.egress);
  if (summary) row.append(summary);
  row.append(yes, no);
  return row;
}

function waitingConfirm() {
  const assistant = currentAssistant();
  if (!assistant || assistant.status !== "running") return null;
  const found = (assistant.events || []).find((item) => item.type === "tool_call" && item.status === "waiting_confirm");
  return found ? assistant : null;
}

async function submitConfirm(message, allowed, button, acknowledge) {
  const runId = message.run_id || (waitingConfirm() && waitingConfirm().run_id);
  if (!runId) {
    showNote("现在没有在等待确认。");
    return;
  }
  if (button) button.disabled = true;
  try {
    const body = { allowed: Boolean(allowed) };
    if (allowed && acknowledge) body.acknowledge = acknowledge;
    await api("POST", `/api/runs/${encodeURIComponent(runId)}/confirm`, body);
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
  refreshStorage();
  settings.showModal();
}

// 12.F（#62）：本地存储状态。只显示模式、算法、key_id，不显示密钥和路径。
async function refreshStorage() {
  const node = document.querySelector("#storage-status");
  if (!node) return;
  try {
    const response = await fetch("/api/storage");
    const body = await response.json();
    node.textContent = storageText(body);
  } catch (error) {
    node.textContent = "读不到存储状态。";
  }
}

function storageText(body) {
  if (!body || !body.mode) return "读不到存储状态。";
  if (body.mode === "memory") return "仅内存会话：会话和文件不落盘，停止服务就没了。";
  if (body.mode === "plaintext") {
    return "未加密：会话正文和文件字节以明文存在数据目录里，拷走 data/ 就能读。要加密请停服务后运行 storage_tool.py encrypt。";
  }
  const left = body.plaintext_left || {};
  const rest = (left.sessions || 0) + (left.blobs || 0) + (left.legacy_files || 0);
  return `已加密（${body.alg}，每个会话单独派生密钥，key_id ${body.key_id}）。`
    + (rest ? ` 还有 ${rest} 项明文残留，请运行 storage_tool.py encrypt。` : "")
    + (body.decrypt_failures ? ` 有 ${body.decrypt_failures} 次解密失败（密钥不对或文件被改）。` : "")
    + (body.key_source === "keychain" ? " 主密钥在系统钥匙串里。" : "")
    + " 丢了主密钥和备份就无法恢复。";
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
  if (provider.key_source === "keychain") {
    return `密钥在系统钥匙串里（页面拿不到原文），末四位 ${provider.key_hint}。留空表示不修改。`;
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
    error.errors = payload.errors || null;
    error.payload = payload;
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

// ---- 工具配置：按工具声明自动生成表单 ----
const toolDialog = document.querySelector("#tool-settings");
document.querySelector("#open-tools").addEventListener("click", openToolSettings);
document.querySelector("#close-tools").addEventListener("click", () => toolDialog.close());
document.querySelector("#tool-form").addEventListener("submit", saveToolConfig);
document.querySelector("#tool-reset").addEventListener("click", resetToolConfig);
document.querySelector("#tool-test").addEventListener("click", testToolConfig);
document.querySelector("#tool-select").addEventListener("change", (event) => loadToolConfig(event.target.value));
document.querySelector("#tool-export").addEventListener("click", exportToolPack);
document.querySelector("#tool-uninstall").addEventListener("click", uninstallToolPack);
document.querySelector("#toolpack-install").addEventListener("click", installToolPack);
document.querySelector("#toolpack-review-accept").addEventListener("click", acceptToolPack);
document.querySelector("#toolpack-review-cancel").addEventListener("click", closeToolPackReview);
document.querySelector("#presetpack-review-accept").addEventListener("click", acceptPresetPack);
document.querySelector("#presetpack-review-cancel").addEventListener("click", closePresetPackReview);
document.querySelector("#agent-install").addEventListener("click", installAgent);
(() => {
  const box = document.querySelector("#multi-agent");
  box.checked = localStorage.getItem("mas.multiAgent") === "1";
  box.addEventListener("change", () => localStorage.setItem("mas.multiAgent", box.checked ? "1" : "0"));
})();

const SOURCE_LABEL = { env: "来自环境变量（优先，页面改不了）", saved: "已保存", default: "默认值" };

const PERMISSION_LABEL = {
  read_local_content: "读本机内容",
  network: "联网",
  write_files: "写文件",
  send_local_to_model: "把本机内容交给模型",
};

// 下拉框按实际工具名（plan_images、generate_image…）列出；后端（ComfyUI）单独一组。
function toolOptions(listing) {
  const groups = [];
  const tools = document.createElement("optgroup");
  tools.label = "工具";
  for (const tool of listing.tools || []) {
    const option = document.createElement("option");
    option.value = tool.name;
    const flags = [];
    if (!tool.builtin) flags.push("已安装");
    if (!tool.ready) flags.push("未就绪");
    option.textContent = flags.length ? `${tool.name}（${flags.join("，")}）` : tool.name;
    tools.append(option);
  }
  groups.push(tools);
  if ((listing.backends || []).length) {
    const backends = document.createElement("optgroup");
    backends.label = "后端";
    for (const backend of listing.backends) {
      const option = document.createElement("option");
      option.value = backend.name;
      option.textContent = backend.complete ? backend.title : `${backend.title}（未配全）`;
      backends.append(option);
    }
    groups.push(backends);
  }
  return groups;
}

async function refreshToolList(selected) {
  const listing = await api("GET", "/api/tools");
  renderPresetPacks(listing.preset_packs || []);
  const select = document.querySelector("#tool-select");
  select.replaceChildren(...toolOptions(listing));
  const names = [...(listing.tools || []), ...(listing.backends || [])].map((item) => item.name);
  const pick = names.includes(selected) ? selected : names[0];
  if (pick) {
    select.value = pick;
    await loadToolConfig(pick);
  }
}

// #86：预设包（pack.json，纯数据）：类型、分级、扩展哪个工具；页面上装的可以卸载。
function renderPresetPacks(items) {
  const list = document.querySelector("#presetpack-list");
  list.replaceChildren();
  document.querySelector("#presetpack-empty").hidden = items.length > 0;
  for (const pack of items) {
    const item = document.createElement("li");
    const text = document.createElement("span");
    const rating = pack.rating === "adult" ? `成人分级（${pack.min_age}+，仅本机出图）` : "通用";
    const origin = pack.origin === "installed" ? "已安装" : "来自 MAS_LOCAL_TOOLPACKS";
    text.textContent = `${pack.label}（${pack.name}${pack.version ? ` ${pack.version}` : ""}）· 预设包 · 扩展 ${pack.extends} · ${rating} · ${origin}`;
    item.append(text);
    if (pack.origin === "installed") {
      const remove = document.createElement("button");
      remove.type = "button";
      remove.textContent = "卸载";
      remove.addEventListener("click", async () => {
        if (!window.confirm(`卸载预设包 ${pack.label}（${pack.name}）？角色定制里就不再有这组预设。`)) return;
        try {
          await api("POST", `/api/toolpacks/presets/${encodeURIComponent(pack.name)}/uninstall`);
          await refreshToolList(state.toolView && state.toolView.name);
          toolNote(`已卸载预设包 ${pack.name}（不用重启）。`);
        } catch (error) {
          toolNote(error.message || "卸载失败。");
        }
      });
      item.append(" ", remove);
    }
    list.append(item);
  }
}

async function refreshAgents() {
  const list = document.querySelector("#agent-list");
  const data = await api("GET", "/api/agents");
  list.replaceChildren();
  for (const agent of data.agents || []) {
    const item = document.createElement("li");
    const text = document.createElement("span");
    text.textContent = `${agent.name} ${agent.version} · ${agent.role} · ${agent.builtin ? "内置" : "已安装"} · 工具：${(agent.tools || []).join("、") || "无"}${agent.model ? ` · 模型 ${agent.model}` : ""}`;
    item.append(text);
    const link = document.createElement("a");
    link.href = `/api/agents/${encodeURIComponent(agent.name)}/export`;
    link.textContent = "导出";
    item.append(" ", link);
    if (!agent.builtin) {
      const remove = document.createElement("button");
      remove.type = "button";
      remove.textContent = "卸载";
      remove.addEventListener("click", async () => {
        if (!window.confirm(`卸载 Agent ${agent.name}？`)) return;
        try {
          await api("POST", `/api/agents/${encodeURIComponent(agent.name)}/uninstall`);
          await refreshAgents();
        } catch (error) {
          toolNote(error.message || "卸载失败。");
        }
      });
      item.append(" ", remove);
    }
    list.append(item);
  }
}

async function installAgent() {
  const source = document.querySelector("#agent-source").value.trim();
  if (!source) {
    toolNote("先填 Agent 目录或 .zip 路径。");
    return;
  }
  try {
    const info = await api("POST", "/api/agents/inspect", { source });
    if ((info.problems || []).length) {
      toolNote(`Agent 不合格：${info.problems.join("；")}`);
      return;
    }
    if (!window.confirm(`安装 Agent ${info.name} ${info.version}？\n${info.description}\n角色：${info.role}\n工具：${(info.tools || []).join("、") || "无"}`)) return;
    await api("POST", "/api/agents/install", { source });
    await refreshAgents();
    toolNote(`已安装 Agent ${info.name}。`);
  } catch (error) {
    toolNote(error.message || "安装失败。");
  }
}

async function openToolSettings() {
  toolNote("");
  try {
    await refreshToolList(state.toolView && state.toolView.name);
    await refreshAgents().catch(() => {});
  } catch (error) {
    toolNote(error.message || "读不到工具配置。");
  }
  toolDialog.showModal();
}

function renderToolMeta(view) {
  const box = document.querySelector("#tool-meta");
  const pack = view.pack;
  document.querySelector("#tool-export").hidden = !pack || pack.builtin;
  document.querySelector("#tool-uninstall").hidden = !pack || pack.builtin;
  box.replaceChildren();
  box.hidden = !pack;
  if (!pack) return;
  const line = (text) => {
    const p = document.createElement("p");
    p.className = "hint";
    p.textContent = text;
    box.append(p);
    return p;
  };
  line(`版本 ${pack.version} · ${pack.builtin ? "内置" : "已安装"} · ${pack.exposed ? "模型可调用" : "只在本机使用"} · 确认：${{ always: "每次确认", never: "不用确认", by_args: "按参数" }[pack.confirm] || pack.confirm}`);
  const perms = (pack.permissions || []).map((key) => PERMISSION_LABEL[key] || key);
  line(`权限：${perms.length ? perms.join("、") : "无"}`);
  if (pack.sandbox === "subprocess") line("运行方式：沙箱子进程（拿不到你的环境变量和本地内容，越权会被拦下）");
  const GRANT_STATE = {
    missing: "没有授权记录，请重新安装并确认权限。",
    outdated: "旧版本装的包，授权没有绑定包内容，请重新安装并确认权限。",
    changed: "装好后包里的文件被改过，授权已作废，请重新安装。",
  };
  if (pack.grant && GRANT_STATE[pack.grant]) line(GRANT_STATE[pack.grant]).className = "note";
  if ((pack.denied || []).length) line(`未授权：${pack.denied.map((key) => PERMISSION_LABEL[key] || key).join("、")}`).className = "note";
  if ((pack.missing || []).length) line(`缺少依赖：${pack.missing.join("、")}`).className = "note";
  for (const backend of pack.backend_status || []) {
    const row = line(`依赖后端 ${backend.title}：${backend.complete ? "已配好" : "未配全（" + backend.missing.join("、") + "）"} `);
    const jump = document.createElement("button");
    jump.type = "button";
    jump.className = "ghost";
    jump.textContent = `去配置 ${backend.title}`;
    jump.addEventListener("click", async () => {
      document.querySelector("#tool-select").value = backend.name;
      await loadToolConfig(backend.name);
    });
    row.append(jump);
  }
  if (!view.fields.length) line("这个工具没有自己的配置项。");
}

async function loadToolConfig(name) {
  state.toolView = await api("GET", `/api/tools/${encodeURIComponent(name)}/config`);
  renderToolForm(state.toolView);
}

function renderToolForm(view, errors) {
  const box = document.querySelector("#tool-fields");
  document.querySelector("#tool-description").textContent = view.description || "";
  document.querySelector("#tool-test").hidden = !view.testable;
  document.querySelector("#tool-save").hidden = !view.fields.length;
  document.querySelector("#tool-reset").hidden = !view.fields.length;
  renderToolMeta(view);
  box.replaceChildren();
  for (const field of view.fields) {
    const current = view.values[field.name] || {};
    const label = document.createElement("label");
    label.append(document.createTextNode(field.name + (field.required ? " *" : "")));
    let control;
    if (field.type === "enum") {
      control = document.createElement("select");
      for (const choice of field.choices || []) {
        const option = document.createElement("option");
        option.value = choice;
        option.textContent = choice;
        control.append(option);
      }
      control.value = current.value ?? field.default;
    } else {
      control = document.createElement("input");
      control.autocomplete = "off";
      control.spellcheck = false;
      if (field.type === "int") control.type = "number";
      if (field.type === "secret") {
        control.type = "password";
        control.placeholder = current.configured ? `已保存（…${current.hint || ""}），留空保持不变` : "未设置";
      } else {
        control.value = current.value ?? "";
      }
      if (field.type === "path" && field.choices && field.choices.length) {
        const list = document.createElement("datalist");
        list.id = `choices-${field.name}`;
        for (const choice of field.choices) {
          const option = document.createElement("option");
          option.value = choice;
          list.append(option);
        }
        label.append(list);
        control.setAttribute("list", list.id);
      }
    }
    control.name = field.name;
    control.dataset.type = field.type;
    if (current.source === "env") control.disabled = true;
    label.append(control);
    const hint = document.createElement("span");
    hint.className = "hint";
    hint.textContent = `${field.description || ""} ${SOURCE_LABEL[current.source] || ""}`.trim();
    label.append(hint);
    // 12.E：放宽隐私的取值（如 remote_local_prompt=confirm）选中时显示强警示，保存前还要再确认一次。
    const warned = (view.warnings || {})[field.name];
    if (warned && control.tagName === "SELECT") {
      const warning = document.createElement("p");
      warning.className = "tool-warning";
      warning.setAttribute("role", "alert");
      const sync = () => {
        const text = warned[control.value];
        warning.hidden = !text;
        warning.textContent = text ? `⚠ ${text}` : "";
      };
      control.addEventListener("change", sync);
      sync();
      label.append(warning);
    }
    const problem = errors && errors[field.name];
    if (problem) {
      const bad = document.createElement("span");
      bad.className = "note";
      bad.textContent = problem;
      label.append(bad);
    }
    box.append(label);
  }
}

function toolFormValues() {
  const values = {};
  for (const control of document.querySelectorAll("#tool-fields [name]")) {
    if (control.disabled) continue;
    const type = control.dataset.type;
    if (type === "secret" && !control.value) continue;
    values[control.name] = type === "int" ? Number(control.value || 0) : control.value;
  }
  return values;
}

async function saveToolConfig(event) {
  event.preventDefault();
  const view = state.toolView;
  if (!view) return;
  const values = toolFormValues();
  const put = (acknowledge) =>
    api("PUT", `/api/tools/${encodeURIComponent(view.name)}/config`, acknowledge ? { values, acknowledge } : { values });
  try {
    try {
      state.toolView = await put(null);
    } catch (error) {
      // 宿主要求显式确认放宽隐私的设置：把宿主给的警示原样给人看，同意了才带 acknowledge 重发。
      if (error.code !== "acknowledge_required") throw error;
      const warnings = (error.payload && error.payload.warnings) || {};
      const text = Object.values(warnings).join("\n");
      if (!window.confirm(`${text}\n\n确定要放宽这项隐私设置吗？`)) {
        toolNote("没有保存：放宽隐私的设置需要你确认。");
        return;
      }
      state.toolView = await put(Object.keys(warnings));
    }
    renderToolForm(state.toolView);
    toolNote("已保存，下一次运行就会用新配置。");
  } catch (error) {
    renderToolForm(view, error.errors);
    toolNote(error.message || "保存失败。");
  }
}

async function resetToolConfig() {
  const view = state.toolView;
  if (!view) return;
  try {
    state.toolView = await api("PUT", `/api/tools/${encodeURIComponent(view.name)}/config`, { reset: true, values: {} });
    renderToolForm(state.toolView);
    toolNote("已恢复默认值。");
  } catch (error) {
    toolNote(error.message || "恢复失败。");
  }
}

async function testToolConfig() {
  const view = state.toolView;
  if (!view) return;
  toolNote("正在测试连接…");
  try {
    const result = await api("POST", `/api/tools/${encodeURIComponent(view.name)}/test`);
    toolNote(result.message || (result.ok ? "连接正常。" : "连接失败。"));
  } catch (error) {
    toolNote(error.message || "测试失败。");
  }
}

function exportToolPack() {
  const view = state.toolView;
  if (!view || !view.pack || view.pack.builtin) return;
  // 导出的 zip 只有清单和代码：配置值、密钥和授权记录都不带走。
  const link = document.createElement("a");
  link.href = `/api/toolpacks/${encodeURIComponent(view.name)}/export`;
  link.download = `${view.name}.zip`;
  document.body.append(link);
  link.click();
  link.remove();
  toolNote("已导出（不含配置和密钥）。");
}

async function uninstallToolPack() {
  const view = state.toolView;
  if (!view || !view.pack || view.pack.builtin) return;
  if (!window.confirm(`卸载工具 ${view.name}？它保存的配置会留在 tools.json 里。`)) return;
  try {
    await api("POST", `/api/toolpacks/${encodeURIComponent(view.name)}/uninstall`);
    state.toolView = null;
    await refreshToolList();
    toolNote(`已卸载 ${view.name}。`);
  } catch (error) {
    toolNote(error.message || "卸载失败。");
  }
}

// 12.D（#60）：安装第三方工具包前的权限审阅。逐项列出它要的权限、白话说明、它拿不到的东西和沙箱限制；
// 每个权限都勾上才能点“同意并安装”，提交的就是勾上的那几项（服务端要求和清单完全一致）。
let pendingToolPack = null;

async function installToolPack() {
  const source = document.querySelector("#toolpack-source").value.trim();
  if (!source) {
    toolNote("先填工具包目录或 .zip 路径。");
    return;
  }
  try {
    const info = await api("POST", "/api/toolpacks/inspect", { source });
    if ((info.problems || []).length) {
      toolNote(`${info.type === "preset" ? "预设包" : "工具包"}不合格：${info.problems.join("；")}`);
      return;
    }
    if (info.type === "preset") {
      closeToolPackReview();
      showPresetPackReview(source, info);
      return;
    }
    closePresetPackReview();
    showToolPackReview(source, info);
  } catch (error) {
    toolNote(error.message || "安装失败。");
  }
}

function showToolPackReview(source, info) {
  pendingToolPack = { source, info };
  const review = info.review || { permissions: [], not_granted: [], sandbox: [] };
  document.querySelector("#toolpack-review-title").textContent = `权限审阅：${info.name} ${info.version}`;
  document.querySelector("#toolpack-review-desc").textContent = info.description || "";
  const perms = document.querySelector("#toolpack-review-perms");
  perms.replaceChildren();
  for (const item of review.permissions || []) {
    const row = document.createElement("li");
    const label = document.createElement("label");
    const box = document.createElement("input");
    box.type = "checkbox";
    box.className = "toolpack-perm";
    box.value = item.permission;
    box.addEventListener("change", syncToolPackReview);
    const text = document.createElement("span");
    text.textContent = ` ${item.label || PERMISSION_LABEL[item.permission] || item.permission}（风险：${item.risk === "high" ? "高" : "中"}）：${item.effect}`;
    label.append(box, text);
    row.append(label);
    perms.append(row);
  }
  if (!(review.permissions || []).length) {
    const row = document.createElement("li");
    row.textContent = "不要任何权限：不能联网、不能写临时目录以外的文件、拿不到本地内容。";
    perms.append(row);
  }
  document.querySelector("#toolpack-review-none").textContent = (review.not_granted || []).join("、") || "无";
  const sandbox = document.querySelector("#toolpack-review-sandbox");
  sandbox.replaceChildren(...(review.sandbox || []).map((fact) => {
    const row = document.createElement("li");
    row.textContent = fact;
    return row;
  }));
  const missing = document.querySelector("#toolpack-review-missing");
  missing.hidden = !(info.missing || []).length;
  missing.textContent = missing.hidden ? "" : `缺少依赖：${info.missing.join("、")}（装上后调用会报 dependency_missing）`;
  document.querySelector("#toolpack-review").hidden = false;
  syncToolPackReview();
}

function syncToolPackReview() {
  const boxes = [...document.querySelectorAll("#toolpack-review-perms .toolpack-perm")];
  document.querySelector("#toolpack-review-accept").disabled = !pendingToolPack || boxes.some((box) => !box.checked);
}

function closeToolPackReview() {
  pendingToolPack = null;
  document.querySelector("#toolpack-review").hidden = true;
}

async function acceptToolPack() {
  if (!pendingToolPack) return;
  const { source, info } = pendingToolPack;
  const accepted = [...document.querySelectorAll("#toolpack-review-perms .toolpack-perm")]
    .filter((box) => box.checked).map((box) => box.value);
  try {
    await api("POST", "/api/toolpacks/install", { source, accept_permissions: accepted });
    closeToolPackReview();
    await refreshToolList(info.name);
    toolNote(`已安装 ${info.name}（在沙箱子进程里运行）。`);
  } catch (error) {
    toolNote(error.message || "安装失败。");
  }
}

// #86：预设包审阅：没有权限，但要列出类型/分级/扩展的工具；rating=adult 额外警告，要再勾一项明确确认。
let pendingPresetPack = null;

function showPresetPackReview(source, info) {
  pendingPresetPack = { source, info };
  const adult = info.rating === "adult";
  const section = document.querySelector("#presetpack-review");
  section.classList.toggle("rated", adult);
  document.querySelector("#presetpack-review-title").textContent =
    `预设包审阅：${info.label}（${info.name}${info.version ? ` ${info.version}` : ""}）${info.upgrade !== undefined ? " · 覆盖已装版本" : ""}`;
  document.querySelector("#presetpack-review-desc").textContent = info.description || "";
  const facts = [
    "类型：预设包（mas.local_pack/1，纯数据）",
    `扩展的工具：${info.extends}`,
    `分级：${adult ? `成人（rating=adult），年龄下限 ${info.min_age} 岁，只用本机 ComfyUI 出图` : "通用（rating=general）"}`,
    `替换预设：${(info.replaces || []).join("、") || "无"}；追加预设：${(info.extends_presets || []).join("、") || "无"}；新字段：${(info.fields || []).join("、") || "无"}`,
    info.chat ? "对话里可用：要在那一句里说出包的关键词才开启" : "对话里不可用：只能在角色定制表单里选",
    `装到：${info.target}`,
  ];
  const fill = (selector, rows) => document.querySelector(selector).replaceChildren(...rows.map((text) => {
    const row = document.createElement("li");
    row.textContent = text;
    return row;
  }));
  fill("#presetpack-review-facts", facts);
  fill("#presetpack-review-perms", info.permission_notes || []);
  document.querySelector("#presetpack-review-warnings").replaceChildren(...(info.warnings || []).map((text) => {
    const p = document.createElement("p");
    p.className = "note";
    p.textContent = text;
    return p;
  }));
  const confirmText = document.querySelector("#presetpack-review-confirm");
  confirmText.hidden = !(adult && info.confirm);
  confirmText.textContent = adult ? info.confirm || "" : "";
  const accepts = [["data", "我知道它只是预设数据，不要任何权限。"]];
  if (adult) accepts.push(["adult", "我已成年，确认安装这个成人分级预设包（只在本机出图，结果标记为成人内容）。"]);
  document.querySelector("#presetpack-review-accepts").replaceChildren(...accepts.map(([value, text]) => {
    const row = document.createElement("li");
    const label = document.createElement("label");
    const box = document.createElement("input");
    box.type = "checkbox";
    box.className = "presetpack-accept";
    box.value = value;
    box.addEventListener("change", syncPresetPackReview);
    const span = document.createElement("span");
    span.textContent = ` ${text}`;
    label.append(box, span);
    row.append(label);
    return row;
  }));
  section.hidden = false;
  syncPresetPackReview();
}

function syncPresetPackReview() {
  const boxes = [...document.querySelectorAll("#presetpack-review-accepts .presetpack-accept")];
  document.querySelector("#presetpack-review-accept").disabled = !pendingPresetPack || boxes.some((box) => !box.checked);
}

function closePresetPackReview() {
  pendingPresetPack = null;
  document.querySelector("#presetpack-review").hidden = true;
}

async function acceptPresetPack() {
  if (!pendingPresetPack) return;
  const { source, info } = pendingPresetPack;
  const boxes = [...document.querySelectorAll("#presetpack-review-accepts .presetpack-accept")];
  if (boxes.some((box) => !box.checked)) return;
  const adult = boxes.some((box) => box.value === "adult" && box.checked);
  const body = { source, accept_permissions: [] };
  if (adult) body.accept_rating = "adult";
  try {
    await api("POST", "/api/toolpacks/install", body);
    closePresetPackReview();
    await refreshToolList(state.toolView && state.toolView.name);
    document.querySelector("#presetpack-box").open = true;
    toolNote(`已安装预设包 ${info.label}（不用重启）。在“角色定制”顶部的“预设来源”里选它。`);
  } catch (error) {
    toolNote(error.message || "安装失败。");
  }
}

function toolNote(message) {
  const line = document.querySelector("#tool-note");
  line.hidden = !message;
  line.textContent = message || "";
}
