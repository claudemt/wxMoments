

const CHAT_KIND_OPTIONS = [
  ["text", "文字"], ["image", "图片"], ["voice", "语音"], ["video", "视频"],
  ["file", "文件"], ["link", "链接"], ["quote", "引用"], ["forward", "合并转发"],
  ["other", "其他"], ["system", "系统消息"],
];

const chatState = {
  loaded: false,
  ready: false,
  account: "",
  snapshotId: "",
  sessions: [],
  allIds: new Set(),
  total: 0,
  page: 0,
  pageSize: 50,
  selected: new Set(),
  scope: "chat",
  draftScope: "chat",
  kinds: new Set(CHAT_KIND_OPTIONS.map((item) => item[0])),
  draftKinds: new Set(CHAT_KIND_OPTIONS.map((item) => item[0])),
  showFolded: false,
  draftShowFolded: false,
  query: "",
  start: "",
  end: "",
  previewId: "",
  previewTitle: "",
  previewMessages: [],
  previewCursor: null,
  previewHasMore: false,
  previewKind: "all",
  previewKeyword: "",
  previewSeq: 0,
  loadingAll: false,
  filterResult: [],
  shards: 0,
};

const CHAT_KINDS = [
  ["all", "全部"], ["text", "文字"], ["image", "图片"], ["voice", "语音"],
  ["video", "视频"], ["file", "文件"], ["link", "链接"], ["quote", "引用"],
  ["forward", "聊天记录"],
];

const CHAT_SCOPES = [
  ["chat", "私聊+群聊"], ["private", "私聊"], ["group", "群聊"],
];

const CHAT_FILTER_LIMIT = 2000;

function chatPad(value) { return String(value).padStart(2, "0"); }

function chatTimeText(ts) {
  if (!ts) return "";
  const date = new Date(ts * 1000);
  const now = new Date();
  const hm = `${chatPad(date.getHours())}:${chatPad(date.getMinutes())}`;
  if (date.toDateString() === now.toDateString()) return hm;
  if (date.getFullYear() === now.getFullYear()) {
    return `${chatPad(date.getMonth() + 1)}-${chatPad(date.getDate())}`;
  }
  return `${date.getFullYear()}-${chatPad(date.getMonth() + 1)}-${chatPad(date.getDate())}`;
}

function chatFullTime(ts) {
  if (!ts) return "";
  const date = new Date(ts * 1000);
  return `${date.getFullYear()}-${chatPad(date.getMonth() + 1)}-${chatPad(date.getDate())} `
    + `${chatPad(date.getHours())}:${chatPad(date.getMinutes())}:${chatPad(date.getSeconds())}`;
}

function chatFilterPayload() {
  return {
    start_date: chatState.start,
    end_date: chatState.end,
    kinds: [...chatState.kinds],
  };
}

function chatKindSummary() {
  const all = CHAT_KIND_OPTIONS.length;
  if (chatState.kinds.size >= all) return "";
  if (!chatState.kinds.size) return "不含任何内容";
  const labels = CHAT_KIND_OPTIONS
    .filter((item) => chatState.kinds.has(item[0]))
    .map((item) => item[1]);
  return labels.length > 4 ? `${labels.length} 类内容` : labels.join("/");
}

function chatFilterSummary() {
  const parts = [];
  if (chatState.scope !== "chat") {
    const found = CHAT_SCOPES.find((item) => item[0] === chatState.scope);
    parts.push(found ? found[1] : "会话");
  }
  if (chatState.start || chatState.end) {
    parts.push(`${chatState.start || "最早"} ～ ${chatState.end || "至今"}`);
  }
  if (chatState.query) parts.push(`“${chatState.query}”`);
  if (chatState.showFolded) parts.push("含折叠群");
  const kinds = chatKindSummary();
  if (kinds) parts.push(kinds);
  return parts.length ? ` · ${parts.join(" · ")}` : "";
}

function chatSetStatus(text) {
  $("chat-status").textContent = text;
}

function chatSetBusy(busy) {
  $("chat-init").disabled = busy;
  $("chat-init").textContent = busy ? "刷新中…" : "刷新";
}

function chatUpdateTools() {
  const count = chatState.selected.size;
  $("chat-count").textContent = chatState.ready && count ? `已选 ${count}` : "";
  $("chat-export").disabled = !chatState.ready || count === 0;
  $("chat-filter-state").textContent = chatFilterSummary();
}

async function chatRefresh() {
  chatSetBusy(true);
  try {
    const res = await api("/api/chat/init", {});
    if (!res.ok) { chatSetBusy(false); return; }
    startJob(res.job, "读取聊天记录", "", chatAfterRefresh, "chat");
  } catch (error) {
    chatSetBusy(false);
  }
}

async function chatAfterRefresh(job) {
  chatSetBusy(false);
  if (job.status !== "done") return;
  await chatLoadStatus();
  await chatLoadSessions(0);
}

async function chatLoadStatus() {
  let res;
  try {
    res = await api("/api/chat/status", undefined, true);
  } catch (error) {
    return;
  }
  if (!res || !res.ok) return;
  chatState.account = res.account || "";
  chatState.snapshotId = res.snapshot_id || "";
  chatState.ready = Boolean(res.ready);
  if (res.refresh_job && !task.jobs.has(res.refresh_job)) {
    chatSetBusy(true);
    startJob(res.refresh_job, "读取聊天记录", "", chatAfterRefresh, "chat");
  }
  if (!chatState.ready) {
    chatSetStatus("");
    show($("chat-body"), false);
    return;
  }
  chatState.shards = res.shards || 0;
  const shown = res.visible_sessions ?? res.sessions;
  chatSetStatus(`共 ${shown} 个会话`);
  $("chat-status").title = res.shards ? `${res.shards} 个消息库` : "";
  show($("chat-body"), true);
  for (const warning of res.warnings || []) notify(warning, "warning");
}

async function chatLoadSessions(page = 0) {
  if (!chatState.ready) return;
  let res;
  try {
    res = await api("/api/chat/sessions", {
      query: chatState.query,
      scope: chatState.scope,
      start_date: chatState.start,
      end_date: chatState.end,
      show_folded: chatState.showFolded,
      page,
      page_size: chatState.pageSize,
    });
  } catch (error) {
    return;
  }
  if (!res.ok) return;
  chatState.sessions = res.sessions || [];
  chatState.allIds = new Set(res.all_ids || []);
  chatState.total = res.total || 0;
  chatState.page = res.page || 0;
  chatState.snapshotId = res.snapshot_id || chatState.snapshotId;
  chatState.selected = new Set([...chatState.selected].filter((id) => chatState.allIds.has(id)));
  chatRenderSessions();
  renderPager($("chat-pager"), chatState.total, chatState.pageSize, chatState.page,
    (next) => chatLoadSessions(next), "个");
  chatSetStatus(`共 ${chatState.total} 个会话`);
  chatUpdateTools();
}

function chatRenderSessions() {
  const list = $("chat-list");
  list.innerHTML = "";
  if (!chatState.sessions.length) {
    const empty = document.createElement("div");
    empty.className = "hint muted";
    empty.textContent = "没有符合条件的会话";
    list.append(empty);
    return;
  }
  for (const item of chatState.sessions) {
    const row = document.createElement("div");
    row.className = "row chat-row";
    row.dataset.id = item.conversation_id;
    row.tabIndex = 0;

    const box = document.createElement("div");
    box.className = "t";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = chatState.selected.has(item.conversation_id);
    input.tabIndex = -1;
    box.append(input);

    const name = document.createElement("div");
    name.className = "c chat-name";
    name.innerHTML = `<span>${escapeHtml(item.display)}</span>`;
    if (item.kind === "group") name.innerHTML += `<em class="chat-tag">群</em>`;
    if (item.folded) name.innerHTML += `<em class="chat-tag chat-tag-fold">折叠</em>`;

    const summary = document.createElement("div");
    summary.className = "c chat-summary";
    summary.textContent = item.last_summary || "";

    const time = document.createElement("div");
    time.className = "d chat-time";
    time.textContent = chatTimeText(item.last_time);

    const actions = document.createElement("div");
    actions.className = "chat-actions";
    const preview = document.createElement("button");
    preview.className = "ghost";
    preview.textContent = "预览";
    preview.onclick = (event) => {
      event.stopPropagation();
      chatOpenPreview(item);
    };
    actions.append(preview);

    row.append(box, name, summary, time, actions);
    row.onclick = () => chatToggle(item.conversation_id, row);
    row.onkeydown = (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        chatToggle(item.conversation_id, row);
      }
    };
    list.append(row);
  }
}

function chatToggle(id, row) {
  if (chatState.selected.has(id)) chatState.selected.delete(id);
  else chatState.selected.add(id);
  if (row) {
    const input = row.querySelector("input");
    if (input) input.checked = chatState.selected.has(id);
  }
  chatUpdateTools();
}

function chatSelectAll() {
  if (!chatState.ready) return;
  const all = [...chatState.allIds];
  const everyPicked = all.length > 0 && all.every((id) => chatState.selected.has(id));
  chatState.selected = new Set(everyPicked ? [] : all);
  chatRenderSessions();
  chatUpdateTools();
}

async function chatExport() {
  if (!chatState.ready || !chatState.selected.size) return;
  const sessions = [...chatState.selected];
  let res;
  try {
    res = await api("/api/export", {
      type: "chat",
      snapshot_id: chatState.snapshotId,
      sessions,
      filters: chatFilterPayload(),
    });
  } catch (error) {
    return;
  }
  if (!res.ok) return;
  startJob(res.job, `聊天记录 · ${res.count || sessions.length} 个会话`, res.dir || "",
    () => { }, "chat");
}

function chatChips(container, options, active, onPick) {
  container.innerHTML = "";
  for (const [value, label] of options) {
    const button = document.createElement("button");
    button.className = "chat-chip";
    button.dataset.value = value;
    button.textContent = label;
    if (value === active) button.classList.add("active");
    button.onclick = () => {
      for (const node of container.querySelectorAll(".chat-chip")) {
        node.classList.toggle("active", node === button);
      }
      onPick(value);
    };
    container.append(button);
  }
}

function chatRenderKinds() {
  const box = $("cf-kinds");
  if (!box) return;
  box.innerHTML = "";
  for (const [value, label] of CHAT_KIND_OPTIONS) {
    const row = document.createElement("label");
    row.className = "chat-kind";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.value = value;
    input.checked = chatState.draftKinds.has(value);
    input.onchange = () => {
      if (input.checked) chatState.draftKinds.add(value);
      else chatState.draftKinds.delete(value);
      row.classList.toggle("on", input.checked);
      chatUpdateKindAll();
    };
    row.classList.toggle("on", input.checked);
    const text = document.createElement("span");
    text.textContent = label;
    row.append(input, text);
    box.append(row);
  }
  chatUpdateKindAll();
}

function chatUpdateKindAll() {
  const btn = $("cf-kinds-all");
  if (!btn) return;
  const all = chatState.draftKinds.size === CHAT_KIND_OPTIONS.length;
  btn.textContent = all ? "清空" : "全选";
}

function chatOpenFilter() {
  $("cf-start").value = chatState.start;
  $("cf-end").value = chatState.end;
  $("cf-search").value = chatState.query;
  chatState.draftScope = chatState.scope;
  chatState.draftKinds = new Set(chatState.kinds);
  chatChips($("cf-scopes"), CHAT_SCOPES, chatState.draftScope, (value) => {
    chatState.draftScope = value;
    chatLoadFilterResult();
  });
  chatState.draftShowFolded = chatState.showFolded;
  const foldBox = $("cf-folded");
  const foldRow = $("cf-folded-row");
  if (foldBox) {
    foldBox.checked = chatState.draftShowFolded;
    if (foldRow) foldRow.classList.toggle("on", foldBox.checked);
    foldBox.onchange = () => {
      chatState.draftShowFolded = foldBox.checked;
      if (foldRow) foldRow.classList.toggle("on", foldBox.checked);
      chatLoadFilterResult();
    };
  }
  chatRenderKinds();
  show($("chat-filter-overlay"), true);
  chatLoadFilterResult();
}

let chatFilterTimer = null;

function chatQueueFilterResult() {
  clearTimeout(chatFilterTimer);
  chatFilterTimer = setTimeout(chatLoadFilterResult, 320);
}

async function chatLoadFilterResult() {
  const box = $("cf-result");
  const countEl = $("cf-result-count");
  if (!box || !countEl) return;
  countEl.textContent = "加载中…";
  let res;
  try {
    res = await api("/api/chat/sessions", {
      query: $("cf-search").value.trim(),
      scope: chatState.draftScope,
      start_date: $("cf-start").value,
      end_date: $("cf-end").value,
      show_folded: chatState.draftShowFolded,
      page: 0,
      page_size: CHAT_FILTER_LIMIT,
    });
  } catch (error) {
    countEl.textContent = "加载失败";
    return;
  }
  if (!res.ok) {
    countEl.textContent = res.msg || "加载失败";
    return;
  }
  chatState.filterResult = res.sessions || [];
  const total = res.total || 0;
  countEl.textContent = total ? `共 ${total} 个会话` : "没有符合条件的会话";
  box.innerHTML = "";
  if (!total) {
    box.append(chatEl("div", "chat-result-empty", "换个日期、范围或名称试试"));
    return;
  }
  for (const item of chatState.filterResult) {
    const row = chatEl("label", "chat-result-row");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = chatState.selected.has(item.conversation_id);
    input.onchange = () => {
      if (input.checked) chatState.selected.add(item.conversation_id);
      else chatState.selected.delete(item.conversation_id);
      chatSyncSelectionToMain();
      chatUpdateTools();
    };
    const name = chatEl("span", "chat-result-name", item.display);
    name.title = item.folded ? `${item.display}（已折叠）` : item.display;
    const time = chatEl("span", "chat-result-time", chatTimeText(item.last_time));
    row.append(input, name, time);
    box.append(row);
  }
}

function chatSyncSelectionToMain() {
  for (const row of document.querySelectorAll("#chat-list .chat-row")) {
    const input = row.querySelector("input");
    if (input) input.checked = chatState.selected.has(row.dataset.id);
  }
}

function chatPickAllResults() {
  const list = chatState.filterResult || [];
  if (!list.length) return;
  const every = list.every((item) => chatState.selected.has(item.conversation_id));
  for (const item of list) {
    if (every) chatState.selected.delete(item.conversation_id);
    else chatState.selected.add(item.conversation_id);
  }
  for (const input of document.querySelectorAll("#cf-result input")) {
    input.checked = !every;
  }
  chatSyncSelectionToMain();
  chatUpdateTools();
}

async function chatApplyFilter() {
  const start = $("cf-start").value;
  const end = $("cf-end").value;
  if (start && end && end < start) {
    notify("结束日期需晚于开始日期", "warning");
    return;
  }
  if (!chatState.draftKinds.size) {
    notify("请至少选择一种导出内容", "warning");
    return;
  }
  chatState.start = start;
  chatState.end = end;
  chatState.query = $("cf-search").value.trim();
  chatState.scope = chatState.draftScope;
  chatState.kinds = new Set(chatState.draftKinds);
  chatState.showFolded = chatState.draftShowFolded;
  show($("chat-filter-overlay"), false);
  await chatLoadSessions(0);
  chatUpdateTools();
}

async function chatResetFilter() {
  chatState.start = "";
  chatState.end = "";
  chatState.query = "";
  chatState.scope = "chat";
  chatState.draftScope = "chat";
  chatState.kinds = new Set(CHAT_KIND_OPTIONS.map((item) => item[0]));
  chatState.draftKinds = new Set(chatState.kinds);
  chatState.showFolded = false;
  chatState.draftShowFolded = false;
  $("cf-start").value = "";
  $("cf-end").value = "";
  $("cf-search").value = "";
  if ($("cf-folded")) $("cf-folded").checked = false;
  if ($("cf-folded-row")) $("cf-folded-row").classList.remove("on");
  chatChips($("cf-scopes"), CHAT_SCOPES, chatState.draftScope, (value) => {
    chatState.draftScope = value;
  });
  chatRenderKinds();
  show($("chat-filter-overlay"), false);
  await chatLoadSessions(0);
  chatUpdateTools();
}

const CHAT_PREVIEW_LIMIT = 1000;

function chatMediaUrl(message, kind, extra = {}) {
  const query = new URLSearchParams({
    kind,
    session: message.conversation_id || chatState.previewId,
    create_time: String(message.create_time || 0),
    server_id: String(message.server_id || ""),
    md5: String((message.extra || {}).md5 || ""),
    ...extra,
  });
  return `/api/chat/media?${query.toString()}`;
}

function chatHumanSize(value) {
  const size = Number(value || 0);
  if (!size) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let index = 0;
  let left = size;
  while (left >= 1024 && index < units.length - 1) { left /= 1024; index += 1; }
  return index === 0 ? `${Math.round(left)} ${units[index]}` : `${left.toFixed(1)} ${units[index]}`;
}

function chatEl(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function chatFileClass(ext) {
  const key = String(ext || "").toLowerCase();
  if (key === "pdf") return "pdf";
  if (["doc", "docx", "rtf", "txt", "md", "odt", "pages"].includes(key)) return "doc";
  if (["xls", "xlsx", "csv", "ods", "numbers"].includes(key)) return "xls";
  if (["ppt", "pptx", "odp", "key"].includes(key)) return "ppt";
  if (["zip", "rar", "7z", "tar", "gz", "bz2", "xz"].includes(key)) return "zip";
  if (["mp4", "mov", "avi", "mkv", "webm", "jpg", "jpeg", "png", "gif", "webp"].includes(key)) return "media";
  if (["mp3", "wav", "silk", "amr", "m4a", "aac", "flac"].includes(key)) return "audio";
  return "";
}

function chatCardCover(label, source, thumb) {
  const cover = chatEl("div", "chat-card-cover");
  const fallback = () => {
    cover.classList.add("is-fallback");
    cover.append(chatEl("span", "chat-card-cover-glyph", (source || label).slice(0, 1)));
    cover.append(chatEl("span", "chat-card-cover-tag", label));
  };
  if (thumb) {
    const img = new Image();
    img.loading = "lazy";
    img.referrerPolicy = "no-referrer";
    img.alt = "";
    img.src = thumb;
    img.onerror = () => { img.remove(); cover.innerHTML = ""; fallback(); };
    cover.append(img);
  } else {
    fallback();
  }
  return cover;
}

function chatCardNode(message, kind, extra) {
  const url = String(extra.url || "").trim();
  const mini = kind === "miniprogram";
  const official = Boolean(extra.official);
  const label = mini ? "小程序" : (official ? "公众号" : "链接");
  const source = String(extra.source || extra.app || "").trim();
  const wrap = chatEl(url ? "a" : "div", "chat-card" + (mini ? " chat-card-mini" : ""));
  if (url) {
    wrap.href = url;
    wrap.target = "_blank";
    wrap.rel = "noreferrer";
  }
  const body = chatEl("div", "chat-card-body");
  body.append(chatEl("div", "chat-card-title",
    message.title || message.text || label));
  if (extra.descr) body.append(chatEl("div", "chat-card-desc", extra.descr));
  const foot = chatEl("div", "chat-card-foot");
  foot.append(chatEl("span", "chat-card-badge", label));
  if (source) foot.append(chatEl("span", "chat-card-src", source));
  body.append(foot);
  wrap.append(body, chatCardCover(label, source, String(extra.thumb || "")));
  return wrap;
}

function chatChipCard(label, title, sub, href) {
  const node = chatEl(href ? "a" : "div", "chat-chipcard");
  if (href) {
    node.href = href;
    node.target = "_blank";
    node.rel = "noreferrer";
  }
  node.append(chatEl("b", "", label));
  const body = chatEl("span", "chat-chip-body");
  // 标题与标签同名（如「群公告 群公告」）时只保留标签
  if (title && title !== label) body.append(chatEl("span", "chat-chip-title", title));
  if (sub) body.append(chatEl("span", "chat-chip-sub", sub));
  if (body.childNodes.length) node.append(body);
  return node;
}

function chatMediaNode(message) {
  const kind = message.kind || "other";
  const extra = message.extra || {};
  const text = String(message.text || "").trim();
  if (kind === "image") {
    const link = chatEl("a", "chat-att-img");
    link.href = chatMediaUrl(message, "image_full");
    link.target = "_blank";
    link.rel = "noreferrer";
    const img = new Image();
    img.loading = "lazy";
    img.decoding = "async";
    img.src = chatMediaUrl(message, "image_full");
    img.alt = "图片";
    img.onerror = () => { link.replaceWith(chatEl("span", "chat-att-miss", "[图片未缓存到本机]")); };
    link.append(img);
    return link;
  }
  if (kind === "sticker") {
    const url = chatMediaUrl(message, "sticker", {
      md5: String(extra.md5 || ""),
      url: String(extra.cdnurl || ""),
      thumb: String(extra.thumburl || ""),
    });
    const link = chatEl("a", "chat-sticker-link");
    link.href = url;
    link.target = "_blank";
    link.rel = "noreferrer";
    const img = new Image();
    img.className = "chat-sticker";
    img.loading = "lazy";
    img.alt = "动画表情";
    img.src = url;
    img.onerror = () => {
      link.replaceWith(chatEl("span", "chat-sticker-miss", "[动画表情]"));
    };
    link.append(img);
    return link;
  }
  if (kind === "video") {
    const thumb = chatMediaUrl(message, "video_thumb", { md5: String(extra.md5 || "") });
    const card = chatEl("div", "chat-video-card");
    const img = new Image();
    img.loading = "lazy";
    img.alt = "视频封面";
    img.src = thumb;
    img.onerror = () => { card.replaceWith(chatChipCard("视频", text || "", "")); };
    const play = chatEl("span", "chat-video-play", "▶");
    card.append(img, play);
    if (text) card.append(chatEl("span", "chat-video-cap", text));
    return card;
  }
  if (kind === "voice" && message.server_id) {
    const box = chatEl("div", "chat-att-voice");
    const audio = document.createElement("audio");
    audio.controls = true;
    audio.preload = "none";
    audio.src = chatMediaUrl(message, "voice");
    audio.onerror = () => { box.replaceWith(chatEl("span", "chat-att-miss", "[语音未缓存]")); };
    box.append(audio);
    if (extra.duration) box.append(chatEl("span", "chat-att-dur", `${extra.duration}"`));
    return box;
  }
  if (kind === "file") {
    const name = extra.filename || message.text || "文件";
    const href = chatMediaUrl(message, "file", {
      filename: name, size: String(extra.size || ""),
    });
    const card = chatEl("a", "chat-att-file");
    card.href = href;
    card.download = name;
    const ext = (extra.ext || name.split(".").pop() || "file").slice(0, 4).toUpperCase();
    card.append(chatEl("span", `chat-att-ext ${chatFileClass(ext)}`.trim(), ext));
    const info = chatEl("span", "chat-att-info");
    info.append(chatEl("span", "chat-att-name", name));
    info.append(chatEl("span", "chat-att-size", chatHumanSize(extra.size) || "点击保存"));
    card.append(info);
    return card;
  }
  if (kind === "link") {
    return chatCardNode(message, kind, extra);
  }
  if (kind === "miniprogram" || kind === "channel") {
    return chatCardNode(message, kind, extra);
  }
  if (kind === "location") {
    const label = extra.label || extra.poi || text || "位置";
    const sub = extra.poi && extra.poi !== label ? extra.poi : "";
    return chatChipCard("位置", label, sub);
  }
  if (kind === "card") {
    return chatChipCard("名片", extra.nickname || text || "微信名片", extra.username || "");
  }
  if (kind === "redpacket") {
    return chatChipCard("红包", text || "微信红包", extra.descr || "");
  }
  if (kind === "transfer") {
    return chatChipCard("转账", text || "转账", extra.descr || "");
  }
  if (kind === "call") {
    return chatChipCard("通话", text || "语音通话", extra.descr || "");
  }
  if (kind === "music") {
    return chatChipCard("音乐", message.title || text || "音乐", extra.source || "");
  }
  if (kind === "note") return chatChipCard("笔记", message.title || text || "收藏笔记", "");
  if (kind === "solitaire") return chatChipCard("接龙", message.title || text || "群接龙", "");
  if (kind === "announce") return chatChipCard("群公告", message.title || text || "群公告", "");
  if (kind === "live") return chatChipCard("直播", message.title || text || "直播", extra.source || "");
  return null;
}

const CHAT_CARD_KINDS = [
  "forward", "image", "voice", "file", "link", "miniprogram", "channel", "sticker",
  "video", "redpacket", "transfer", "call", "location", "card", "music",
  "note", "solitaire", "announce", "live",
];

function chatMessageNode(message, showTime = true) {
  const kind = message.kind || "other";
  const extra = message.extra || {};
  const plain = String(message.text || "").trim();
  // 拍一拍偶尔会被记成普通文本（英文模板 tickled），统一按居中系统提示渲染
  const patLike = /tickled/i.test(plain)
    || (/^["“]?.{0,20}["”]?\s{0,2}拍了拍\s/.test(plain) && plain.length <= 60);
  if (kind === "system" || patLike) {
    const row = chatEl("div", "chat-sys");
    row.append(chatEl("span", "", message.text || ""));
    return row;
  }
  const row = chatEl("div", "chat-msg" + (message.is_self ? " me" : ""));
  const ava = chatEl("div", "chat-ava", (message.sender_display || "?").slice(-1));
  const stack = chatEl("div", "chat-stack");
  stack.append(chatEl("div", "chat-msg-meta",
    message.is_self ? "我" : (message.sender_display || "")));
  const bubble = chatEl("div", "chat-bubble");
  const media = chatMediaNode(message);
  if (CHAT_CARD_KINDS.includes(kind) && media) bubble.classList.add("chat-bubble-plain");
  if (kind === "quote" && extra.quote) {
    const quote = chatEl("div", "chat-quote");
    quote.textContent = `${extra.quote.sender || ""}${extra.quote.sender ? "：" : ""}`
      + (extra.quote.text || extra.quote.summary || "");
    bubble.append(quote);
  }
  if (kind === "forward") {
    const card = chatEl("div", "chat-forward");
    card.append(chatEl("div", "chat-forward-head", message.title || "聊天记录"));
    const list = chatEl("ul", "");
    for (const item of (extra.forward_items || []).slice(0, 40)) {
      list.append(chatEl("li", "", `${item.name || ""} ${item.title || ""}`.trim()));
    }
    card.append(list);
    if (extra.forward_truncated) card.append(chatEl("div", "chat-att-miss", "（已截断）"));
    bubble.append(card);
  }
  // 卡片 / 媒体节点自己带标题，正文不再重复
  if (message.text && !CHAT_CARD_KINDS.includes(kind)) {
    bubble.append(chatEl("div", "chat-text", message.text));
  }
  if (media) bubble.append(media);
  if (!bubble.childNodes.length) {
    const label = (kind === "sticker") ? "[动画表情]" : `[${message.label || "消息"}]`;
    bubble.append(chatEl("div", "chat-text chat-muted", label));
  }
  stack.append(bubble);
  if (showTime) stack.append(chatEl("div", "chat-time", chatFullTime(message.create_time)));
  row.append(ava, stack);
  return row;
}

function chatRenderPreview(batch, append = false) {
  const body = $("cp-body");
  if (!chatState.previewMessages.length) {
    body.innerHTML = "";
    const empty = document.createElement("div");
    empty.className = "hint muted";
    empty.textContent = "没有符合条件的消息";
    body.append(empty);
    return;
  }
  const prevHeight = body.scrollHeight;
  const prevTop = body.scrollTop;
  const frag = document.createDocumentFragment();
  let lastDay = "";
  let lastTs = 0;
  for (const message of batch) {
    const day = chatFullTime(message.create_time).slice(0, 10);
    const newDay = Boolean(day && day !== lastDay);
    if (newDay) {
      lastDay = day;
      const divider = document.createElement("div");
      divider.className = "chat-date";
      divider.textContent = day;
      frag.append(divider);
    }
    const gap = message.create_time - lastTs;
    frag.append(chatMessageNode(message, newDay || lastTs === 0 || gap > 300));
    lastTs = message.create_time;
  }
  if (append) {
    // 增量插到顶部，避免每翻一页都把整棵 DOM 重建一遍
    const oldFirst = body.firstElementChild;
    body.insertBefore(frag, body.firstChild);
    if (oldFirst && oldFirst.classList.contains("chat-date")
        && oldFirst.textContent === lastDay) {
      oldFirst.remove();
    }
    body.scrollTop = prevTop + (body.scrollHeight - prevHeight);
  } else {
    body.innerHTML = "";
    body.append(frag);
    body.scrollTop = body.scrollHeight;
  }
}

function chatPreviewMeta(extra = "") {
  const count = chatState.previewMessages.length;
  let text = extra || `已加载 ${count} 条`;
  if (!extra) {
    if (!chatState.previewHasMore && count) text += " · 已全部加载";
    else if (!chatState.previewHasMore) text += " · 仅显示最近扫描范围";
  }
  $("cp-meta").textContent = text;
}

async function chatOpenPreview(item) {
  chatState.previewId = item.conversation_id;
  chatState.previewTitle = item.display;
  chatState.previewMessages = [];
  chatState.previewCursor = null;
  chatState.previewHasMore = false;
  chatState.loadingAll = false;
  chatState.previewKind = "all";
  chatState.previewKeyword = "";
  $("cp-title").textContent = item.display;
  $("cp-keyword").value = "";
  chatChips($("cp-kinds"), CHAT_KINDS, chatState.previewKind, (value) => {
    chatState.previewKind = value;
    chatState.loadingAll = false;
    chatLoadPreview(true);
  });
  show($("chat-preview-overlay"), true);
  await chatLoadPreview(true);
}

async function chatLoadPreview(reset, budget = 0) {
  if (!chatState.previewId) return null;
  const seq = ++chatState.previewSeq;
  let res;
  try {
    res = await api("/api/chat/messages", {
      conversation_id: chatState.previewId,
      snapshot_id: chatState.snapshotId,
      cursor: reset ? null : chatState.previewCursor,
      limit: CHAT_PREVIEW_LIMIT,
      scan_budget: budget || 6000,
      filter: {
        start_date: chatState.start,
        end_date: chatState.end,
        kind: chatState.previewKind,
        keyword: chatState.previewKeyword,
      },
    }, true);
  } catch (error) {
    return null;
  }
  if (seq !== chatState.previewSeq) return null;
  if (!res.ok) { notify(res.msg || "读取消息失败"); return null; }
  const rows = (res.messages || []).slice().reverse();
  chatState.previewMessages = reset ? rows : rows.concat(chatState.previewMessages);
  chatState.previewCursor = res.next_cursor || null;
  chatState.previewHasMore = Boolean(res.has_more);
  $("cp-older").hidden = !chatState.previewHasMore;
  $("cp-all").hidden = !chatState.previewHasMore;
  $("cp-older").disabled = false;
  $("cp-older").textContent = "加载更早消息";
  chatRenderPreview(rows, !reset);
  if (!chatState.loadingAll) chatPreviewMeta();
  return res;
}

// 单次预览最多堆到 20 万条，超过后只保留手动「加载更早消息」
const CHAT_LOAD_ALL_MAX = 200000;

async function chatLoadAll() {
  const btn = $("cp-all");
  if (chatState.loadingAll) {
    chatState.loadingAll = false;
    btn.textContent = "正在停止…";
    return;
  }
  if (!chatState.previewHasMore) return;
  chatState.loadingAll = true;
  btn.textContent = "停止加载";
  $("cp-older").disabled = true;
  let guard = 0;
  while (chatState.loadingAll && chatState.previewHasMore
         && chatState.previewMessages.length < CHAT_LOAD_ALL_MAX && guard < 500) {
    chatPreviewMeta(`全量加载中… 已加载 ${chatState.previewMessages.length} 条`);
    const before = chatState.previewCursor;
    const res = await chatLoadPreview(false, 20000);
    if (!res) break;
    guard += 1;
    if (!res.has_more) break;
    if (res.next_cursor && res.next_cursor === before) break;  // 游标没前进，避免空转
  }
  chatState.loadingAll = false;
  btn.textContent = "加载全部";
  $("cp-older").disabled = false;
  chatPreviewMeta();
}

function chatClosePreview() {
  chatState.previewSeq += 1;
  chatState.loadingAll = false;
  show($("chat-preview-overlay"), false);
}

function chatCloseFilters() {
  show($("chat-filter-overlay"), false);
}

function chatEnter() {
  if (chatState.loaded) return;
  chatState.loaded = true;
  chatLoadStatus().then(() => {
    if (chatState.ready) chatLoadSessions(0);
  });
}

function chatCloseTransient() {
  let closed = false;
  if (!$("chat-preview-overlay").hidden) { chatClosePreview(); closed = true; }
  if (!$("chat-filter-overlay").hidden) { chatCloseFilters(); closed = true; }
  return closed;
}

$("chat-init").onclick = chatRefresh;
$("chat-export").onclick = chatExport;
$("chat-select-all").onclick = chatSelectAll;
$("chat-filter-btn").onclick = chatOpenFilter;
$("cf-close").onclick = chatCloseFilters;
$("cf-apply").onclick = chatApplyFilter;
$("cf-reset").onclick = chatResetFilter;
$("cf-result-all").onclick = chatPickAllResults;
$("cf-kinds-all").onclick = () => {
  const all = chatState.draftKinds.size === CHAT_KIND_OPTIONS.length;
  chatState.draftKinds = new Set(all ? [] : CHAT_KIND_OPTIONS.map((item) => item[0]));
  chatRenderKinds();
};
$("cf-search").oninput = chatQueueFilterResult;
$("cf-start").onchange = chatQueueFilterResult;
$("cf-end").onchange = chatQueueFilterResult;
$("chat-filter-overlay").onclick = (event) => {
  if (event.target === $("chat-filter-overlay")) chatCloseFilters();
};
$("cp-close").onclick = chatClosePreview;
$("chat-preview-overlay").onclick = (event) => {
  if (event.target === $("chat-preview-overlay")) chatClosePreview();
};
$("cp-older").onclick = () => {
  if (!chatState.previewHasMore) return;
  $("cp-older").disabled = true;
  $("cp-older").textContent = "读取中…";
  chatLoadPreview(false);
};
$("cp-all").onclick = chatLoadAll;
let chatKeywordTimer = null;
$("cp-keyword").oninput = (event) => {
  clearTimeout(chatKeywordTimer);
  const value = event.target.value.trim();
  chatKeywordTimer = setTimeout(() => {
    chatState.previewKeyword = value;
    chatState.loadingAll = false;
    chatLoadPreview(true);
  }, 320);
};

window.chatView = { enter: chatEnter, closeTransient: chatCloseTransient };
