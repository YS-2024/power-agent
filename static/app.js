"use strict";
const $ = (selector) => document.querySelector(selector);
let csrf = "";
let currentUser = null;
let passwordTarget = null;
let previousQuestion = "";
let dailyBusy = false;
let dailyOperation = null;
let imageBusy = false;
let pendingImages = [];
let toastTimer;
let activeSession = "", oldestTurn = null, memoryEditId = null, pendingRequest = null;
const savedSetting = (key, value) => {
  if (currentUser) key += ":" + currentUser.id;
  try { if (value === undefined) return localStorage.getItem(key); localStorage.setItem(key, value); }
  catch { return null; }
};
const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};
function toast(message, error = false) {
  const node = $("#toast");
  node.textContent = message;
  node.className = "toast" + (error ? " error" : "");
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { node.hidden = true; }, 5500);
}
async function api(path, method = "GET", body, signal) {
  const timeout = AbortSignal.timeout(path === "/api/assistant" || (path === "/api/documents" && method === "POST") ? 300000 : 65000);
  const response = await fetch(path, {
    method, headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf, ...(currentUser ? {"X-Workspace-User": currentUser.id} : {}) },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: signal ? AbortSignal.any([signal, timeout]) : timeout,
  });
  const result = await response.json();
  if (response.status === 401 && result.code === "login_required") window.location.assign("/login");
  if (response.status === 409 && result.code === "workspace_changed") window.location.replace("/");
  if (!response.ok) throw new Error(result.error || "请求失败，请重试。");
  return result;
}
function showPage(page) {
  document.querySelectorAll(".page").forEach(node => { node.hidden = node.id !== "page-" + page; });
  document.querySelectorAll(".nav-item").forEach(node => node.classList.toggle("active", node.dataset.page === page));
  $("#page-label").textContent = {chat: "资料研读", daily: "智能助手", library: "资料库", reports: "研究报告"}[page];
}
async function refresh() {
  const [status, documents] = await Promise.all([api("/api/status"), api("/api/documents")]);
  csrf = status.csrf_token;
  currentUser = status.user || null;
  if (currentUser) $("#memory-enabled").checked = savedSetting("power-memory-enabled") !== "false";
  if (status.shared_demo && !$("#demo-notice")) {
    const banner = el("div", "notice", currentUser ? "你的聊天、长期记忆、报告及个人资料仅属于当前账号。公共资料按管理员授权共享；模型调用仍使用服务提供者的接口额度。" : "独立演示空间：与分享者的个人空间隔离。");
    banner.id = "demo-notice";
    $(".main-body").prepend(banner);
    $(".workspace-tag").textContent = currentUser ? `${currentUser.username} · ${currentUser.role === "admin" ? "管理员" : "个人工作空间"}` : "独立演示工作空间";
    const logout = el("button", "quiet", "退出登录");
    logout.type = "button";
    logout.onclick = async () => {
      if (dailyBusy) return toast("请先停止生成，再退出登录。");
      logout.disabled = true;
      try { await api("/api/auth/logout", "POST", {}); window.location.replace("/login"); }
      catch (error) { toast(error.message, true); logout.disabled = false; }
    };
    $(".topbar").append(logout);
    if (currentUser) {
      const change = el("button", "quiet", "修改密码"); change.type = "button";
      change.onclick = () => openPasswordDialog(); $(".topbar").append(change);
      if (currentUser.role === "admin") {
        const manage = el("button", "quiet", "账号管理"); manage.type = "button";
        manage.onclick = async () => { try { await refreshAccounts(); $("#accounts-dialog").showModal(); } catch (error) { toast(error.message, true); } };
        $(".topbar").append(manage);
        $("#library-scope-controls").hidden = false;
      }
    }
  }
  $("#doc-count").textContent = status.documents;
  $("#nav-doc-count").textContent = status.documents;
  $("#chunk-count").textContent = status.chunks;
    $("#import-note").textContent = status.retrieval === "hybrid"
      ? "语义检索已启用：导入的资料文本和检索问题将发送至配置的向量服务，产生接口用量。删除资料会同时删除正文与向量。"
      : "资料保存在本机，使用关键词检索。";
  $("#mode-badge").textContent = status.mode === "model" ? "模型已配置 · " + status.model : "本地摘录模式";
  $("#model-note").textContent = status.mode === "model"
    ? `模型模式：提问与检索到的资料片段将发送至 ${status.provider}。回答附引用，请核对原文。`
    : "本地摘录模式：检索并展示原文。配置模型后可生成综合回答。";
  $("#daily-model-note").textContent = status.mode === "model"
    ? `${status.provider} · ${status.model} · 按需查资料 · 对话可持续`
    : "尚未配置模型，智能助手需要先接入模型服务。";
  const list = $("#document-list");
  list.replaceChildren();
  if (!documents.length) list.append(el("div", "empty", "还没有资料。导入第一份文档，开始建立知识库。"));
  documents.forEach(doc => {
    const row = el("div", "document-row");
    const details = el("div", "document-details");
    details.append(el("strong", "", doc.title), el("small", "", `${doc.scope === "public" ? "公共资料 · " : doc.scope === "private" ? "我的资料 · " : ""}${doc.kind} · ${doc.characters.toLocaleString()} 字符 · ${doc.filename}`));
    const view = el("button", "quiet", /\.(pdf|docx)$/i.test(doc.filename) ? "查看提取文本" : "查看原文");
    view.onclick = () => openDocument(doc.id);
    const remove = el("button", "quiet delete", "移除");
    remove.hidden = doc.can_delete === false;
    remove.onclick = async () => {
      if (!confirm(`从知识库移除《${doc.title}》？原始文件不受影响。`)) return;
      remove.disabled = true;
      try { await api("/api/documents/" + doc.id, "DELETE"); await refresh(); toast("资料已移除"); }
      catch (error) { toast(error.message, true); remove.disabled = false; }
    };
    row.append(el("span", "document-icon", "▤"), details, view, remove);
    list.append(row);
  });
}
async function openDocument(id, line) {
  try {
    const doc = await api("/api/documents/" + id);
    $("#dialog-title").textContent = doc.title;
    $("#dialog-meta").textContent = `${doc.kind} · ${doc.filename}` + (line ? ` · 引用从第 ${line} 行开始` : "");
    if (/\.(pdf|docx)$/i.test(doc.filename)) $("#dialog-meta").textContent += " · 下方行号为提取文本行号，请按页码或段落与原文件核对";
    $("#document-content").textContent = doc.content.split("\n").map((value, index) => `${String(index + 1).padStart(3)}  ${value}`).join("\n");
    $("#document-dialog").showModal();
    $("#document-dialog").scrollTop = 0;
  } catch (error) { toast(error.message, true); }
}
function sourceCard(source) {
  const card = el("div", "source");
  card.append(el("strong", "", `[${source.citation}] ${source.title}`), el("p", "", source.text));
  const meta = el("div", "source-meta");
  meta.append(el("span", "", (source.location ? `${source.location} · 提取文本 ` : "") + `第 ${source.start_line}–${source.end_line} 行 · ${source.kind}`));
  const button = el("button", "quiet", source.location ? "查看提取文本 ↗" : "查看原文 ↗");
  button.onclick = () => openDocument(source.document_id, source.start_line);
  meta.append(button);
  if (source.source_url && /^https:\/\//i.test(source.source_url)) {
    const link = el("a", "", "原始发布页面 ↗");
    link.href = source.source_url; link.target = "_blank"; link.rel = "noopener noreferrer";
    meta.append(link);
  }
  card.append(meta);
  return card;
}
function renderDocument(text) {
  const container = el("div", "answer-content markdown");
  let code = null;
  for (const line of text.split("\n")) {
    if (line.startsWith("```")) {
      if (code) code = null;
      else { code = el("pre", "code-block"); container.append(code); }
      continue;
    }
    if (code) { code.textContent += line + "\n"; continue; }
    const heading = line.match(/^(#{1,6})\s+(.+)$/);
    if (heading) container.append(el("h" + Math.min(heading[1].length + 2, 6), "", heading[2]));
    else if (line.startsWith("> ")) container.append(el("div", "quote-line", line.slice(2).replace(/^#{1,6}\s+/, "")));
    else if (/^[-*] /.test(line)) container.append(el("div", "list-line", "• " + line.slice(2)));
    else if (line.trim()) container.append(el("div", "markdown-line", line));
    else container.append(el("div", "paragraph-gap"));
  }
  return container;
}
function resultCard(result, report = false) {
  const card = el("article", "answer-card");
  card.append(el("div", "answer-label", report ? "RESEARCH REPORT / 研究报告" : "KNOWLEDGE ANSWER / 资料回答"));
  card.append(el("div", "subnote", result.mode === "model" ? `由 ${result.provider} · ${result.model} 生成` : "本地资料摘录 · 未使用模型生成"));
  card.append(el("h3", "", result.question));
  if (result.notice) card.append(el("div", "notice", result.notice));
  // Treat all document/model text as text, never executable HTML.
  card.append(renderDocument(result.report_markdown || result.answer));
  const trace = el("div", "trace");
  result.trace.forEach(step => {
    const badge = el("span", "", "✓ " + step.tool);
    badge.title = step.detail;
    trace.append(badge);
  });
  card.append(trace);
  if (result.sources.length) {
    const sources = el("details", "sources");
    sources.append(el("summary", "", `${result.sources.length} 个引用片段 · 展开核对来源`));
    result.sources.forEach(source => sources.append(sourceCard(source)));
    card.append(sources);
  }
  if (result.report_url) {
    const link = el("a", "primary download", "下载 Markdown 报告 ↓");
    link.href = result.report_url; link.download = "电力知识研究报告.md";
    card.append(link);
  }
  const copy = el("button", "quiet copy-button", "复制正文");
  copy.onclick = async () => {
    try { await navigator.clipboard.writeText(result.report_markdown || result.answer); toast("正文已复制"); }
    catch { toast("浏览器未允许复制，请手动选择正文复制。", true); }
  };
  card.append(copy);
  return card;
}
async function submitQuestion(event) {
  event.preventDefault();
  const question = $("#question").value.trim();
  if (question.length < 2) return toast("请输入至少 2 个字的问题。", true);
  const button = $("#ask-button");
  if (button.disabled) return;
  button.disabled = true;
  const pending = el("div", "pending", "正在检索资料、整理依据…");
  $("#chat-log").append(pending);
  try {
    const result = await api("/api/chat", "POST", {question, previous_question: previousQuestion});
    previousQuestion = question;
    const card = resultCard(result);
    pending.replaceWith(card);
    $("#question").value = "";
    card.scrollIntoView({behavior: "smooth", block: "start"});
  } catch (error) { pending.remove(); toast(error.message, true); }
  finally { button.disabled = false; }
}
function dailyBubble(role, text, label, images = []) {
  const bubble = el("article", "daily-bubble " + (role === "user" ? "daily-user" : "daily-assistant"));
  bubble.append(el("div", "daily-speaker", label || (role === "user" ? "你" : "电知")));
  bubble.append(role === "user" ? el("div", "daily-user-text", text) : renderDocument(text));
  if (images.length) {
    const gallery = el("div", "message-images");
    images.forEach((url, index) => {
      const image = el("img");
      image.src = url;
      image.alt = `消息附件 ${index + 1}`;
      gallery.append(image);
    });
    bubble.append(gallery);
  }
  return bubble;
}
function assistantBubble(result) {
  const bubble = dailyBubble("assistant", result.answer, result.response_kind === "conversation_recall" ? "电知 · 会话记录核对" : `电知 · ${result.model}`);
  if (result.notice) bubble.append(el("p", "notice", result.notice));
  if (result.sources?.length) {
    const details = el("details", "sources");
    details.append(el("summary", "", `引用 ${result.sources.length} 个资料片段`));
    result.sources.forEach(source => details.append(sourceCard(source)));
    bubble.append(details);
  }
  if (result.saved_memories?.length) bubble.append(el("p", "subnote", "本轮记录的记忆：" + result.saved_memories.map(m => m.content).join("；")));
  return bubble;
}
async function refreshSessions() {
  const sessions = await api("/api/conversations");
  const select = $("#conversation-select");
  select.replaceChildren(new Option("新对话", ""), ...sessions.map(s => new Option(s.title, s.id)));
  select.value = activeSession;
  return sessions;
}
function turnBubbles(turn) {
  return [dailyBubble("user", turn.user_text, "你", turn.images), assistantBubble(turn.response)];
}
async function loadSession(id, older = false) {
  if (!id) { $("#daily-clear").onclick(); return; }
  dailyBusy = true; syncDailyControls();
  try {
    const session = await api("/api/conversations/" + id + (older ? "?before=" + oldestTurn : ""));
    const log = $("#daily-log");
    if (!older) {
      log.querySelectorAll(".daily-bubble").forEach(node => node.remove());
      pendingImages = []; pendingRequest = null; $("#daily-message").value = "";
      activeSession = id; savedSetting("power-active-session", id);
      $("#conversation-select").value = id;
      $("#daily-error").hidden = true;
    }
    const nodes = session.turns.flatMap(turnBubbles);
    older ? log.prepend(...nodes) : log.append(...nodes);
    oldestTurn = session.turns[0]?.ordinal || null;
    $("#conversation-older").hidden = !session.has_older;
    $("#daily-welcome").hidden = log.querySelectorAll(".daily-bubble").length > 0;
    if (!older) log.scrollTop = log.scrollHeight;
  } catch (error) { toast(error.message, true); $("#conversation-select").value = activeSession; }
  finally { dailyBusy = false; renderImagePreviews(); }
}
$("#conversation-select").onchange = event => loadSession(event.target.value);
$("#conversation-older").onclick = () => loadSession(activeSession, true);
$("#conversation-delete").onclick = async () => {
  if (!activeSession || dailyBusy) return;
  if (!confirm("删除此会话的聊天记录、图片，以及从此会话自动生成的长期记忆？此操作不可恢复。")) return;
  dailyBusy = true; syncDailyControls();
  try {
    await api("/api/conversations/" + activeSession, "DELETE");
    dailyBusy = false; $("#daily-clear").onclick(); await refreshSessions(); toast("会话已删除");
  } catch (error) { toast(error.message, true); }
  finally { dailyBusy = false; syncDailyControls(); }
};
$("#memory-enabled").checked = savedSetting("power-memory-enabled") !== "false";
$("#memory-enabled").onchange = event => savedSetting("power-memory-enabled", String(event.target.checked));
async function refreshMemories() {
  const memories = await api("/api/memories");
  const list = $("#memory-list"); list.replaceChildren();
  if (!memories.length) list.append(el("p", "empty", "还没有长期记忆。你可以在聊天中说“请记住…”，也可以在下面添加。"));
  memories.forEach(memory => {
    const row = el("div", "memory-row"); row.append(el("p", "", memory.content));
    const edit = el("button", "quiet", "编辑"); edit.type = "button";
    edit.onclick = () => { memoryEditId = memory.id; $("#memory-content").value = memory.content; $("#memory-content").focus(); };
    const remove = el("button", "quiet delete", "删除"); remove.type = "button";
    remove.onclick = async () => {
      if (!confirm("删除这条长期记忆？聊天原文仍会保留。")) return;
      try { await api("/api/memories/" + memory.id, "DELETE"); await refreshMemories(); }
      catch (error) { toast(error.message, true); }
    };
    row.append(edit, remove); list.append(row);
  });
}
$("#memory-open").onclick = async () => {
  try { await refreshMemories(); $("#memory-dialog").showModal(); }
  catch (error) { toast(error.message, true); }
};
$("#memory-close").onclick = () => $("#memory-dialog").close();
$("#memory-cancel").onclick = () => { memoryEditId = null; $("#memory-content").value = ""; };
$("#memory-form").onsubmit = async event => {
  event.preventDefault(); const button = event.submitter; button.disabled = true;
  try { await api("/api/memories", "POST", {id: memoryEditId, content: $("#memory-content").value}); $("#memory-cancel").onclick(); await refreshMemories(); }
  catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
};
$("#memory-clear").onclick = async () => {
  if (!confirm("清空全部长期记忆？聊天原文仍会保留；以后聊天可以生成新的记忆。")) return;
  try { await api("/api/memories", "DELETE"); $("#memory-cancel").onclick(); await refreshMemories(); }
  catch (error) { toast(error.message, true); }
};
function syncDailyControls() {
  const busy = dailyBusy || imageBusy;
  $("#daily-send").disabled = busy;
  $("#daily-send").hidden = Boolean(dailyOperation);
  $("#daily-stop").hidden = !dailyOperation;
  $("#daily-stop").disabled = Boolean(dailyOperation?.stopping);
  $("#daily-stop").textContent = dailyOperation?.stopping ? "正在停止…" : "■ 停止生成";
  $("#daily-clear").disabled = busy;
  $("#daily-images").disabled = busy;
  $("#daily-message").disabled = dailyBusy;
  for (const id of ["conversation-select", "conversation-delete", "memory-open", "memory-enabled", "conversation-older"]) $("#" + id).disabled = busy;
  $("#image-previews").querySelectorAll("button").forEach(button => { button.disabled = busy; });
}
function renderImagePreviews() {
  const previews = $("#image-previews");
  previews.replaceChildren();
  pendingImages.forEach((item, index) => {
    const card = el("div", "image-preview");
    const image = el("img"); image.src = item.url; image.alt = item.name;
    const remove = el("button", "quiet", "移除"); remove.type = "button";
    remove.setAttribute("aria-label", `移除图片 ${index + 1}`);
    remove.onclick = () => {
      if (dailyBusy || imageBusy) return;
      pendingImages.splice(index, 1); renderImagePreviews();
    };
    card.append(image, el("span", "", item.name), remove); previews.append(card);
  });
  $("#image-status").textContent = imageBusy ? "正在处理图片…" : pendingImages.length ? `已添加 ${pendingImages.length} / 4 张 · 点击发送后交给模型分析` : "最多 4 张 · PNG / JPEG / WebP / GIF";
  syncDailyControls();
}
async function prepareImage(file) {
  if (!["image/png", "image/jpeg", "image/webp", "image/gif"].includes(file.type)) throw new Error("支持 PNG、JPEG、WebP 和 GIF 图片。");
  if (!file.size || file.size > 10 * 1024 * 1024) throw new Error("单张原图不能超过 10 MB，且不能为空。");
  // Decode locally, flatten GIF to its first frame and discard metadata.
  const bitmap = await createImageBitmap(file);
  try {
    if (!bitmap.width || !bitmap.height || bitmap.width * bitmap.height > 40_000_000) throw new Error("图片像素过大，请先缩小图片（最多 4000 万像素）。");
    const scale = Math.min(1, 2048 / Math.max(bitmap.width, bitmap.height));
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(bitmap.width * scale));
    canvas.height = Math.max(1, Math.round(bitmap.height * scale));
    const context = canvas.getContext("2d");
    context.fillStyle = "white"; context.fillRect(0, 0, canvas.width, canvas.height);
    context.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    let blob = await new Promise(resolve => canvas.toBlob(resolve, "image/png"));
    if (!blob || blob.size > 2 * 1024 * 1024) blob = await new Promise(resolve => canvas.toBlob(resolve, "image/jpeg", .85));
    if (!blob || blob.size > 2 * 1024 * 1024) throw new Error("图片优化后仍超过 2 MB，请裁剪或缩小后重试。");
    const url = await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result); reader.onerror = () => reject(new Error("读取图片失败。"));
      reader.readAsDataURL(blob);
    });
    return {name: file.name || "粘贴的图片", url};
  } finally { bitmap.close(); }
}
async function addImages(files) {
  if (dailyBusy || imageBusy) return;
  if (!files.length) return;
  imageBusy = true; renderImagePreviews();
  const errors = [];
  for (const file of files) {
    if (pendingImages.length >= 4) { errors.push("每条消息最多添加 4 张图片。"); break; }
    try { pendingImages.push(await prepareImage(file)); }
    catch (error) { errors.push(`${file.name || "图片"}：${error.message}`); }
  }
  imageBusy = false; renderImagePreviews();
  if (errors.length) toast(errors.join("；"), true);
}
$("#daily-images").addEventListener("change", async event => {
  await addImages([...event.target.files]); event.target.value = "";
});
$("#daily-message").addEventListener("paste", event => {
  const images = [...(event.clipboardData?.items || [])].filter(item => item.kind === "file" && item.type.startsWith("image/"));
  if (images.length) { event.preventDefault(); addImages(images.map(item => item.getAsFile()).filter(Boolean)); }
});
for (const name of ["dragenter", "dragover"]) {
  $("#daily-form").addEventListener(name, event => { event.preventDefault(); $("#daily-form").classList.add("drag-active"); });
}
$("#daily-form").addEventListener("dragleave", event => {
  if (!event.currentTarget.contains(event.relatedTarget)) event.currentTarget.classList.remove("drag-active");
});
$("#daily-form").addEventListener("drop", event => {
  event.preventDefault(); event.currentTarget.classList.remove("drag-active"); addImages([...event.dataTransfer.files]);
});
$("#daily-stop").onclick = async () => {
  const operation = dailyOperation;
  if (!operation || operation.stopping) return;
  operation.stopping = true;
  syncDailyControls();
  // A new conversation may still be being created; submit checks this flag before sending.
  if (!operation.sessionId) return;
  operation.cancelPromise = (async () => {
    try {
      const result = await api("/api/assistant/cancel", "POST", {session_id: operation.sessionId, request_id: operation.requestId});
      if (result.status === "cancelled") {
        operation.cancelled = true;
        operation.controller.abort();
      }
    } catch (error) {
      operation.stopping = false;
      toast("停止未确认，可再次点击停止：" + error.message, true);
      syncDailyControls();
    }
  })();
  await operation.cancelPromise;
};
$("#daily-form").addEventListener("submit", async event => {
  event.preventDefault();
  if (dailyBusy || imageBusy) return;
  const input = $("#daily-message");
  const message = input.value.trim();
  if (!message && !pendingImages.length) return toast("先写一条消息或添加图片吧。", true);
  const images = pendingImages.map(item => item.url);
  const draft = {text: input.value, images: pendingImages.slice()};
  const operation = {controller: new AbortController(), sessionId: null, requestId: null, stopping: false, cancelled: false};
  dailyOperation = operation;
  dailyBusy = true;
  input.value = "";
  pendingImages = [];
  renderImagePreviews();
  $("#daily-error").hidden = true;
  $("#daily-welcome").hidden = true;
  const log = $("#daily-log");
  const userBubble = dailyBubble("user", message, "你", images);
  const pending = el("div", "pending", "正在理解上下文，按需查阅资料并组织回答…");
  log.append(userBubble, pending);
  log.scrollTop = log.scrollHeight;
  try {
    if (!activeSession) {
      activeSession = (await api("/api/conversations", "POST", {})).id;
      savedSetting("power-active-session", activeSession);
    }
    const signature = JSON.stringify({message, images, session: activeSession, remember: $("#memory-enabled").checked});
    if (!pendingRequest || pendingRequest.signature !== signature) pendingRequest = {signature, id: crypto.randomUUID().replaceAll("-", "")};
    operation.sessionId = activeSession;
    operation.requestId = pendingRequest.id;
    let result;
    if (operation.stopping) {
      operation.cancelled = true;
      result = {cancelled: true};
    } else {
      result = await api("/api/assistant", "POST", {message, images, session_id: activeSession,
        request_id: pendingRequest.id, remember: $("#memory-enabled").checked}, operation.controller.signal)
        .catch(error => { if (operation.cancelled) return {cancelled: true}; throw error; });
    }
    if (operation.cancelPromise) await operation.cancelPromise;
    if (result.cancelled) {
      pending.replaceWith(dailyBubble("assistant", "已停止生成，本轮未保存。可以发送新的消息。", "电知 · 已停止"));
    } else {
      pending.replaceWith(assistantBubble(result));
    }
    pendingRequest = null;
    await refreshSessions().catch(error => toast("回复已保存，但会话列表刷新失败：" + error.message, true));
  } catch (error) {
    if (operation.cancelPromise) await operation.cancelPromise;
    pending.remove();
    userBubble.remove();
    input.value = draft.text;
    pendingImages = draft.images;
    $("#daily-error").textContent = (error.name === "TimeoutError" ? "等待回复超时，请稍后重试。" : error.message) + " 输入内容和附件已保留，可再次发送。";
    $("#daily-error").hidden = false;
    $("#daily-welcome").hidden = log.querySelectorAll(".daily-bubble").length > 0;
  } finally {
    dailyOperation = null;
    dailyBusy = false;
    renderImagePreviews();
    log.scrollTop = log.scrollHeight;
    if (!$("#page-daily").hidden) input.focus();
  }
});
PowerChat.bindSend($("#daily-message"), $("#daily-form"), () => dailyBusy || imageBusy);
$("#daily-clear").onclick = () => {
  if (dailyBusy || imageBusy) return;
  activeSession = ""; pendingRequest = null; oldestTurn = null;
  savedSetting("power-active-session", "");
  $("#conversation-select").value = "";
  $("#conversation-older").hidden = true;
  pendingImages = []; renderImagePreviews();
  $("#daily-log").querySelectorAll(".daily-bubble").forEach(node => node.remove());
  $("#daily-welcome").hidden = false;
  $("#daily-error").hidden = true;
  $("#daily-message").value = "";
  $("#daily-message").focus();
  toast("已开始新的对话");
};
document.querySelectorAll("[data-daily]").forEach(button => button.onclick = () => {
  if (dailyBusy || imageBusy) return;
  $("#daily-message").value = button.dataset.daily;
  $("#daily-form").requestSubmit();
});
$("#ask-form").addEventListener("submit", submitQuestion);
PowerChat.bindSend($("#question"), $("#ask-form"), () => $("#ask-button").disabled);
document.querySelectorAll("[data-page]").forEach(button => button.onclick = () => showPage(button.dataset.page));
document.querySelectorAll("[data-question]").forEach(button => button.onclick = () => {
  $("#question").value = button.dataset.question;
  $("#question").focus();
  $("#ask-form").requestSubmit();
});
$("#close-dialog").onclick = () => $("#document-dialog").close();
$("#file-input").addEventListener("change", async event => {
  const files = [...event.target.files];
  event.target.disabled = true;
  let added = 0, duplicates = 0;
  const errors = [], warnings = [];
  const progress = $("#import-result");
  progress.hidden = false;
  for (const file of files) {
    try {
      progress.textContent = `正在导入 ${file.name}：提取文字并建立检索索引，请稍候…`;
      if (!/\.(md|txt|pdf|docx)$/i.test(file.name)) throw new Error("仅支持 TXT、Markdown、PDF 和 DOCX 文件");
      const binary = /\.(pdf|docx)$/i.test(file.name);
      if (file.size > (binary ? 10_000_000 : 1_000_000)) throw new Error(`文件超过 ${binary ? 10 : 1} MB`);
      let payload;
      if (binary) {
        const data = await new Promise((resolve, reject) => {
          const reader = new FileReader();
          reader.onload = () => resolve(reader.result.split(",")[1]);
          reader.onerror = () => reject(new Error("文件读取失败，请重试"));
          reader.readAsDataURL(file);
        });
        payload = {filename: file.name, data_base64: data};
      } else {
        const content = new TextDecoder("utf-8", {fatal: true}).decode(await file.arrayBuffer());
        payload = {filename: file.name, content};
      }
      if (currentUser) payload.scope = currentUser.role === "admin" ? $("#library-scope").value : "private";
      const result = await api("/api/documents", "POST", payload);
      result.duplicate ? duplicates++ : added++;
      (result.warnings || []).forEach(warning => warnings.push(file.name + "：" + warning));
    } catch (error) { errors.push(file.name + "：" + error.message); }
  }
  try { await refresh(); }
  catch (error) { errors.push(error.message); }
  event.target.value = "";
  event.target.disabled = false;
  const summary = `新增 ${added} 份，重复 ${duplicates} 份，失败 ${errors.length} 份。`;
  progress.replaceChildren(el("p", "", summary), ...[...warnings, ...errors].map(message => el("p", "", message)));
  toast(summary + (warnings.length ? "部分页面未提取到文字，请查看导入提示。" : ""), !!errors.length);
});
$("#search-form").addEventListener("submit", async event => {
  event.preventDefault();
  const button = event.target.querySelector("button");
  button.disabled = true;
  const target = $("#search-results");
  try {
    const result = await api("/api/search", "POST", {question: $("#search-query").value.trim()});
    target.replaceChildren();
      if (result.notice) target.append(el("div", "notice", result.notice));
    if (!result.sources.length) target.append(el("div", "empty", "未找到相关片段，请换个关键词或导入相关资料。"));
    result.sources.forEach(source => target.append(sourceCard(source)));
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
});
$("#report-form").addEventListener("submit", async event => {
  event.preventDefault();
  const question = $("#report-topic").value.trim();
  if (question.length < 2) return toast("请输入至少 2 个字的报告主题。", true);
  const button = $("#report-button");
  if (button.disabled) return;
  button.disabled = true;
  const pending = el("div", "pending", "正在收集依据并生成报告…");
  $("#report-result").prepend(pending);
  try {
    const result = await api("/api/report", "POST", {question});
    pending.replaceWith(resultCard(result, true));
    toast("报告已生成并保存到本地");
  } catch (error) { pending.remove(); toast(error.message, true); }
  finally { button.disabled = false; }
});
async function refreshAccounts() {
  const users = await api("/api/users");
  const list = $("#accounts-list"); list.replaceChildren();
  users.forEach(user => {
    const row = el("div", "account-row");
    row.append(el("strong", "", `${user.username} · ${user.role === "admin" ? "管理员" : "普通用户"} · ${user.active ? "已启用" : "已禁用"}`));
    const label = el("label", "memory-toggle");
    const permission = el("input"); permission.type = "checkbox"; permission.checked = user.can_read_public;
    permission.disabled = user.role === "admin";
    permission.onchange = async () => {
      permission.disabled = true;
      try { await api("/api/users/" + user.id, "POST", {can_read_public: permission.checked}); await refreshAccounts(); }
      catch (error) { permission.checked = user.can_read_public; permission.disabled = false; toast(error.message, true); }
    };
    label.append(permission, document.createTextNode("公共资料阅读权")); row.append(label);
    const reset = el("button", "quiet", "重置密码"); reset.type = "button";
    reset.onclick = () => openPasswordDialog(user); row.append(reset);
    const toggle = el("button", "quiet", user.active ? "禁用账号" : "启用账号"); toggle.type = "button";
    toggle.disabled = user.id === currentUser.id;
    toggle.onclick = async () => {
      if (!confirm(`${user.active ? "禁用" : "启用"}账号 ${user.username}？账号数据会保留。`)) return;
      toggle.disabled = true;
      try { await api("/api/users/" + user.id, "POST", {active: !user.active}); await refreshAccounts(); }
      catch (error) { toggle.disabled = false; toast(error.message, true); }
    };
    row.append(toggle); list.append(row);
  });
}
$("#accounts-close").onclick = () => $("#accounts-dialog").close();
$("#account-role").onchange = () => {
  const admin = $("#account-role").value === "admin";
  $("#account-public").disabled = admin;
  if (admin) $("#account-public").checked = true;
};
$("#account-create-form").onsubmit = async event => {
  event.preventDefault(); const button = event.submitter; button.disabled = true;
  $("#account-message").textContent = "";
  try {
    const result = await api("/api/users", "POST", {username: $("#account-name").value.trim(), password: $("#account-password").value,
      role: $("#account-role").value, can_read_public: $("#account-public").checked});
    $("#account-password").value = ""; $("#account-name").value = "";
    $("#account-role").value = "member"; $("#account-public").disabled = false; $("#account-public").checked = true;
    $("#account-message").textContent = `已创建 ${result.username}，请将账号和初始密码单独告知本人。`;
    await refreshAccounts();
  } catch (error) { $("#account-message").textContent = error.message; }
  finally { button.disabled = false; }
};
function openPasswordDialog(user = null) {
  passwordTarget = user;
  $("#password-form").reset(); $("#password-message").textContent = "";
  $("#password-dialog-title").textContent = user ? `重置 ${user.username} 的密码` : "修改我的密码";
  $("#current-password").hidden = Boolean(user); $("#current-password-label").hidden = Boolean(user);
  $("#current-password").required = !user;
  $("#password-dialog").showModal();
}
$("#password-close").onclick = () => { $("#password-form").reset(); $("#password-dialog").close(); };
$("#password-form").onsubmit = async event => {
  event.preventDefault(); const button = event.submitter;
  const password = $("#new-password").value;
  if (password !== $("#confirm-password").value) { $("#password-message").textContent = "两次输入的新密码不一致。"; return; }
  button.disabled = true;
  try {
    if (passwordTarget) await api("/api/users/" + passwordTarget.id, "POST", {password});
    else await api("/api/auth/password", "POST", {current_password: $("#current-password").value, new_password: password});
    $("#password-form").reset();
    if (!passwordTarget || passwordTarget.id === currentUser.id) window.location.replace("/login");
    else { $("#password-dialog").close(); toast("密码已重置，该账号需要重新登录。"); }
  } catch (error) { $("#password-message").textContent = error.message; }
  finally { button.disabled = false; }
};
async function initialize() {
  dailyBusy = true; syncDailyControls();
  try {
    await refresh();
    const sessions = await refreshSessions();
    const saved = savedSetting("power-active-session");
    const chosen = sessions.find(s => s.id === saved) || (saved === null ? sessions[0] : null);
    if (chosen) await loadSession(chosen.id);
  } catch (error) { $("#mode-badge").textContent = "连接失败"; toast("无法连接本地服务：" + error.message, true); }
  finally { dailyBusy = false; syncDailyControls(); }
}
initialize();
