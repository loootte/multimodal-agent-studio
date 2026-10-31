// 定时任务面板（14.E #82）。任务正文只在本机浏览器里录入和预览；到点只进「待开始」，每次都要你点「开始本次」。
(() => {
  const dialog = document.querySelector("#schedule");
  const form = document.querySelector("#schedule-settings");
  const list = document.querySelector("#schedule-list");
  const pending = document.querySelector("#schedule-pending");
  const storage = document.querySelector("#schedule-storage");
  const preview = document.querySelector("#schedule-preview");
  const note = document.querySelector("#schedule-note");
  const add = document.querySelector("#schedule-add");
  const MODE = { plaintext: "明文存储", encrypted: "加密存储（12.F）", memory: "仅内存（重启后清空）" };
  const STATE = { active: "生效", needs_reauth: "待重新授权", done: "已结束", disabled: "已停用" };
  let timer = null;

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function button(text, onClick, cls = "ghost") {
    const node = el("button", cls, text);
    node.type = "button";
    node.addEventListener("click", onClick);
    return node;
  }
  function say(text) {
    note.textContent = text || "";
    note.hidden = !text;
  }
  async function act(promise, done) {
    try {
      const result = await promise;
      if (done) say(typeof done === "function" ? done(result) : done);
      load();
      return result;
    } catch (error) {
      say(error.message);
      return null;
    }
  }
  function fill(view) {
    const values = (view.settings && view.settings.values) || {};
    form.elements.sch_enabled.checked = !!values.enabled;
    form.elements.sch_confirm_timeout_min.value = values.confirm_timeout_min;
    form.elements.sch_max_jobs.value = values.max_jobs;
    form.elements.sch_allow_plaintext_image.checked = !!values.allow_plaintext_image;
    let line = `存储：${MODE[view.storage_mode] || view.storage_mode}。ComfyUI：${view.comfy.host}（${view.comfy.scope}）`;
    line += view.comfy.standing_possible ? "，可以给出图任务开常驻授权。" : "，不是本机，出图任务不能开常驻授权。";
    if (view.corrupt) line += " 任务文件读不出来，已停止写入，请检查 data/schedules/。";
    storage.textContent = line;
    renderPending(view.pending || []);
    renderJobs(view.jobs || [], view.running || {});
  }
  function renderPending(items) {
    pending.replaceChildren();
    if (!items.length) pending.append(el("li", "hint", "没有待开始的任务。"));
    for (const item of items) {
      const li = el("li", "res-item");
      li.append(el("strong", "", item.name || item.job_id));
      li.append(el("span", "hint", ` 到点 ${item.due_at}${item.caught_up ? "（补跑）" : ""} · ${item.expires_at} 前不点就记为 confirm_timeout`));
      const actions = el("div", "dialog-actions");
      actions.append(
        button("开始本次", () => act(api("POST", `/api/schedule/pending/${item.pending_id}/start`, {}), "已开始，可在任务的会话里看进度。"), ""),
        button("跳过", () => act(api("POST", `/api/schedule/pending/${item.pending_id}/dismiss`, {}), "已跳过这一次。")),
      );
      li.append(actions);
      pending.append(li);
    }
  }
  function renderJobs(items, running) {
    list.replaceChildren();
    if (!items.length) list.append(el("li", "hint", "还没有定时任务。"));
    for (const job of items) {
      const li = el("li", "res-item");
      const head = el("div", "res-head");
      head.append(el("strong", "", job.name));
      const kind = job.kind === "image" ? `出图 ${job.images} 张` : "文字";
      head.append(el("span", "hint", ` ${kind} · ${job.schedule_text} · ${STATE[job.state] || job.state}${job.enabled ? "" : "（关）"}`
        + ` · ${job.source === "model" ? "模型经确认建的" : "你建的"}`));
      li.append(head);
      const b = job.budget || {};
      let info = `下次 ${job.next_at || "—"} · 上限：${b.steps} 步 / ${b.time_min} 分钟 / ${b.tokens} tokens / 每天 ${b.daily_runs} 次`;
      info += job.catch_up ? " · 补跑开" : "";
      if (job.handle) info += ` · 句柄 ${job.handle.slice(0, 11)}… 到 ${job.handle_expires_at}`;
      if (job.stop_reason) info += ` · 停止原因 ${job.stop_reason}`;
      li.append(el("p", "hint", info));
      if (job.kind === "image") {
        li.append(el("p", "hint", job.standing
          ? `常驻授权：开（只免本机 ${job.standing.backend_host} 的 generate_image 确认，${job.standing.granted_at} 打开）`
          : "常驻授权：关（每张图都要你确认）"));
      }
      const hist = (job.history || []).slice(-5).reverse();
      if (hist.length) {
        const ul = el("ul", "hint");
        for (const h of hist) ul.append(el("li", "", `${h.at || h.due_at || ""} ${h.status}${h.code && h.code !== h.status ? `（${h.code}）` : ""}`));
        li.append(ul);
      }
      const actions = el("div", "dialog-actions");
      const id = encodeURIComponent(job.job_id);
      actions.append(button("本机预览", () => show(job)));
      actions.append(button(job.enabled ? "停用" : "启用", () => act(api("PUT", `/api/schedule/jobs/${id}`, { enabled: !job.enabled }), "已保存。")));
      actions.append(button("改正文", () => edit(job)));
      actions.append(button("改上限", () => editBudget(job)));
      actions.append(button(job.catch_up ? "关补跑" : "开补跑", () => act(api("PUT", `/api/schedule/jobs/${id}`, { catch_up: !job.catch_up }), "已保存。")));
      if (job.state === "needs_reauth") actions.append(button("重新授权", () => act(api("POST", `/api/schedule/jobs/${id}/reauthorize`, {}), "已发新句柄。"), ""));
      if (job.kind === "image" && !job.standing && job.state === "active") actions.append(button("打开常驻授权…", () => standing(job)));
      if (job.standing) actions.append(button("撤销常驻授权", () => act(api("POST", `/api/schedule/jobs/${id}/standing-revoke`, {}), "已撤销。")));
      actions.append(button("现在跑一次", () => act(api("POST", `/api/schedule/jobs/${id}/run-now`, {}), (r) => r.action === "waiting_start" ? "已进待开始，点「开始本次」。" : `没有进队列：${r.action}`)));
      if (running[job.job_id]) actions.append(button("取消运行", () => act(api("POST", `/api/schedule/jobs/${id}/cancel`, {}), "已取消。")));
      if (job.session_id) actions.append(button("打开会话", () => { dialog.close(); openSession(job.session_id); }));
      actions.append(button("删除", () => remove(job)));
      li.append(actions);
      list.append(li);
    }
  }
  async function load() {
    try {
      fill(await api("GET", "/api/schedule"));
    } catch (error) {
      say(error.message);
    }
  }
  async function show(job) {
    try {
      const result = await api("GET", `/api/schedule/jobs/${encodeURIComponent(job.job_id)}/preview`);
      preview.replaceChildren(el("pre", "", result.body || ""));
      preview.hidden = false;
    } catch (error) {
      say(error.message);
    }
  }
  async function edit(job) {
    const text = window.prompt("新正文（改了以后句柄和常驻授权作废，要重新授权）", "");
    if (text === null || !text.trim()) return;
    act(api("PUT", `/api/schedule/jobs/${encodeURIComponent(job.job_id)}`, { body: text }), "已保存，需要「重新授权」。");
  }
  async function editBudget(job) {
    const b = job.budget || {};
    const raw = window.prompt("步数,分钟,tokens,每天次数", `${b.steps},${b.time_min},${b.tokens},${b.daily_runs}`);
    if (raw === null) return;
    const [steps, time_min, tokens, daily_runs] = raw.split(",").map((v) => Number(v.trim()));
    act(api("PUT", `/api/schedule/jobs/${encodeURIComponent(job.job_id)}`, { budget: { steps, time_min, tokens, daily_runs } }), "已保存。");
  }
  async function standing(job) {
    const id = encodeURIComponent(job.job_id);
    let offer;
    try {
      offer = await api("POST", `/api/schedule/jobs/${id}/standing`, {});
    } catch (error) {
      say(error.message);
      return;
    }
    // 宿主确认卡：服务端给的说明原样给你看，你确认了才把 acknowledge 发回去
    if (!window.confirm(`${offer.card.title}\n\n${offer.card.lines.join("\n")}\n\n确定打开？`)) return;
    act(api("POST", `/api/schedule/jobs/${id}/standing-confirm`, { offer_id: offer.offer_id, acknowledge: offer.card.requires_ack }), "常驻授权已打开。");
  }
  async function remove(job) {
    if (!window.confirm(`删除定时任务「${job.name}」？正文、句柄、常驻授权一起清掉。`)) return;
    preview.hidden = true;
    act(api("POST", `/api/schedule/jobs/${encodeURIComponent(job.job_id)}/delete`, {}), "已删除。");
  }
  document.querySelector("#schedule-save-settings").addEventListener("click", async () => {
    const values = {
      enabled: form.elements.sch_enabled.checked,
      confirm_timeout_min: Number(form.elements.sch_confirm_timeout_min.value),
      max_jobs: Number(form.elements.sch_max_jobs.value),
      allow_plaintext_image: form.elements.sch_allow_plaintext_image.checked,
    };
    if (values.allow_plaintext_image && !window.confirm("明文存储模式下，出图任务的提示词会以明文写在 data/schedules/ 里。确定打开？")) return;
    try {
      fill(await api("PUT", "/api/schedule/settings", { values }));
      say("定时任务设置已保存。");
    } catch (error) {
      say(error.message);
    }
  });
  document.querySelector("#schedule-create").addEventListener("click", () => {
    const f = add.elements;
    const kind = f.sch_new_kind.value;
    const spec = { kind: f.sch_new_spec.value };
    if (spec.kind === "once") spec.at = f.sch_new_at.value;
    if (spec.kind === "every") spec.minutes = Number(f.sch_new_minutes.value);
    if (spec.kind === "daily" || spec.kind === "weekly") spec.time = f.sch_new_time.value;
    if (spec.kind === "weekly") spec.days = f.sch_new_weekdays.value.split(",").map((v) => Number(v.trim())).filter((v) => v >= 0);
    if (spec.kind === "cron") spec.expr = f.sch_new_cron.value;
    const body = { name: f.sch_new_name.value, kind, schedule: spec, body: f.sch_new_body.value, catch_up: f.sch_new_catch_up.checked };
    if (kind === "image") body.images = Number(f.sch_new_images.value);
    act(api("POST", "/api/schedule/jobs", body), "已新建。").then((r) => { if (r) f.sch_new_body.value = ""; });
  });
  document.querySelector("#open-schedule").addEventListener("click", () => {
    say("");
    preview.hidden = true;
    dialog.showModal();
    load();
    clearInterval(timer);
    timer = setInterval(() => { if (dialog.open) load(); else clearInterval(timer); }, 15000);
  });
  document.querySelector("#close-schedule").addEventListener("click", () => dialog.close());
})();
