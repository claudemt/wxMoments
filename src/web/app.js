

const $ = (id) => document.getElementById(id);

function notify(message, level = "error") {
  return floating.message(String(message || "操作失败，请重试"), level);
}
const enqueueBanner = (message) => notify(message, "success");

window.addEventListener("unhandledrejection", (event) => {
  event.preventDefault();
  if (!event.reason?.reported) notify(event.reason?.message || "操作失败，请重试");
});
window.addEventListener("error", (event) => notify(event.message || "页面发生错误，请刷新重试"));

async function api(path, body, silent = false) {
  try {
    const response = await fetch(path, {
      method: body === undefined ? "GET" : "POST",
      headers: { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(60000),
    });
    const result = await response.json().catch(() => ({
      ok: false, msg: `服务错误（${response.status}），请重试。`,
    }));
    if (!response.ok || result.ok === false) {
      if (!silent) notify(result.msg || `服务返回错误（${response.status}）`);
      if (!response.ok) {
        const error = new Error(result.msg || `HTTP ${response.status}`);
        error.reported = !silent;
        throw error;
      }
    }
    return result;
  } catch (error) {
    if (!silent && !error.reported) {
      notify("连不上本地服务，请重新启动 run.bat 后刷新。");
      error.reported = true;
    }
    throw error;
  }
}

function show(el, on = true) { el.hidden = !on; }
function escapeHtml(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function renderPager(container, total, size, page, onPage) {
  container.innerHTML = "";
  const pages = Math.max(1, Math.ceil(total / size));
  const btn = (label, disabled, fn) => {
    const b = document.createElement("button");
    b.textContent = label;
    b.disabled = disabled;
    b.onclick = fn;
    container.append(b);
  };
  btn("‹ 上一页", page <= 0, () => onPage(page - 1));
  const info = document.createElement("span");
  info.textContent = `第 ${page + 1} / ${pages} 页 · 共 ${total} 条`;
  container.append(info);
  btn("下一页 ›", page >= pages - 1, () => onPage(page + 1));
}

const task = { jobs: new Map(), poll: null, polling: false };
let diagnosticsCursor = null;
let serviceOffline = false;

async function pollDiagnostics() {
  try {
    const res = await api(`/api/diagnostics?after=${diagnosticsCursor ?? 0}`, undefined, true);
    const firstPoll = diagnosticsCursor === null;
    diagnosticsCursor = res.cursor;
    serviceOffline = false;
    for (const event of firstPoll ? [] : res.events || []) {
      if (event.notice && ["ERROR", "CRITICAL"].includes(event.level)) {
        notify(event.notice, "error");
      }
    }
  } catch (error) {
    if (!serviceOffline) notify("服务连接中断，请重新启动后刷新页面。");
    serviceOffline = true;
  }
  setTimeout(pollDiagnostics, 4000);
}

function startJob(jobId, title, dir, onComplete = null, kind = "export") {
  if (task.jobs.has(jobId)) return;
  task.jobs.set(jobId, { id: jobId, title, dir, onComplete, kind, completed: false,
    snapshot: { status: "running", done: 0, total: 0, message: "准备中…", log: [] } });
  renderJob(task.jobs.get(jobId));
  if (!task.polling) pollJob();
}

function renderJob(item) {
  const job = item.snapshot;
  if (item.kind === "init" && item.completed && job.status === "done") {
    floating.close(`job:${item.id}`);
    return;
  }
  const pct = job.total ? Math.min(100, Math.round(job.done / job.total * 100)) : 0;
  const level = job.status === "running" ? "info" : job.status === "error" ? "error" : job.status === "done" ? "success" : "info";
  const title = `${item.title} · ${job.status === "running" ? `处理中 ${pct}%` : job.status === "done" ? "已完成" : job.status === "canceled" ? "已取消" : "失败"}`;
  const popup = floating.open(`job:${item.id}`, title, level);
  popup.onClose = () => task.jobs.delete(item.id);
  if (!item.view) {
    const progress = document.createElement("div");
    progress.className = "dl-progress";
    const bar = document.createElement("div");
    progress.append(bar);
    const message = document.createElement("div");
    message.className = "dl-msg";
    const row = document.createElement("div");
    row.className = "dl-row";
    const directory = document.createElement("div");
    directory.className = "dl-dir";
    const cancel = document.createElement("button");
    cancel.className = "dl-cancel";
    cancel.textContent = "取消";
    cancel.onclick = async () => {
      cancel.disabled = true;
      try {
        const res = await api(`/api/job/${item.id}/cancel`, {});
        if (res.ok) cancel.textContent = "正在取消…";
        else cancel.disabled = false;
      } catch (error) { cancel.disabled = false; }
    };
    row.append(directory, cancel);
    const details = document.createElement("details");
    details.className = "task-details";
    const summary = document.createElement("summary");
    summary.textContent = "任务日志";
    const log = document.createElement("pre");
    details.append(summary, log);
    popup.body.append(progress, message, row, details);
    item.view = { bar, message, directory, cancel, log };
  }
  const view = item.view;
  view.bar.style.width = `${job.status === "done" ? 100 : pct}%`;
  view.message.textContent = job.message;
  view.directory.textContent = item.dir ? `保存目录：${item.dir}` : "";
  view.log.textContent = (job.log || []).join("\n") || "暂无日志";
  show(view.cancel, job.status === "running" && item.kind !== "init");
  if (item.completed && !item.closing) {
    item.closing = true;
    floating.finish(popup, job.status === "error" ? 6000 : 4000);
  }
}

async function pollJob() {
  task.polling = true;
  clearTimeout(task.poll);
  try {
    for (const item of [...task.jobs.values()]) {
      if (item.completed) continue;
      try {
        const res = await api(`/api/job/${item.id}`, undefined, true);
        if (!res.ok) continue;
        item.snapshot = res.job;
        const job = res.job;
        if (job.results?.[0]?.dir) item.dir = job.results[0].dir;
        if (job.status !== "running") item.completed = true;
        renderJob(item);
        if (item.completed && item.onComplete) await item.onComplete(job);
      } catch (error) {
        if (error.message.includes("任务不存在")) {
          item.completed = true;
          item.snapshot = { status: "error", message: "任务已失效，请重新操作。", log: [] };
          renderJob(item);
          if (item.onComplete) await item.onComplete(item.snapshot);
        }
      }
    }
  } finally {
    task.polling = false;
    if ([...task.jobs.values()].some((item) => !item.completed)) {
      task.poll = setTimeout(pollJob, 900);
    }
  }
}

$("btn-output").onclick = () => api("/api/reveal", { path: "" });

let supportFocus = null;
function openSupport(kind) {
  supportFocus = document.activeElement;
  floating.close("coverage");
  $("support-title").textContent = kind === "help" ? "帮助" : "查看日志";
  show($("help-body"), kind === "help");
  show($("logs-body"), kind === "logs");
  show($("support-overlay"));
  $("support-close").focus();
  if (kind === "logs") loadLogs();
}
function closeSupport() {
  if ($("support-overlay").hidden) return;
  show($("support-overlay"), false);
  (supportFocus?.isConnected ? supportFocus : $("btn-logs")).focus();
}
$("btn-help").onclick = () => openSupport("help");
$("btn-logs").onclick = () => openSupport("logs");
$("support-close").onclick = closeSupport;
$("support-overlay").addEventListener("click", (event) => {
  if (event.target === $("support-overlay")) closeSupport();
});
$("logs-refresh").onclick = loadLogs;
async function loadLogs() {
  $("logs-refresh").disabled = true;
  $("logs-coverage").textContent = "正在读取日志…";
  try {
    const res = await api("/api/logs", undefined, true);
    if (!res.ok) throw new Error(res.msg || "读取日志失败");
    fillCoverage($("logs-coverage"), res.coverage);
    $("logs-file").textContent = res.log_file || "";
  } catch (error) {
    $("logs-coverage").textContent = `读取日志失败：${error.message}。请重启 run.bat 再试。`;
    $("logs-file").textContent = "";
  } finally { $("logs-refresh").disabled = false; }
}
$("logs-open").onclick = async () => {
  const res = await api("/api/logs/open", {}, true);
  if (!res || !res.ok) enqueueBanner((res && res.msg) || "打开日志文件失败");
};

const cState = {
  loaded: false,
  contacts: [],
  columns: ["remark", "nickname", "wechat_id", "region", "signature"],
  page: 0,
  pageSize: 100,
};


let CONTACT_COLS = {
  remark: "备注名",
  nickname: "实际名称",
  wechat_id: "微信号",
  region: "地区",
  signature: "个性签名",
  description: "备注",
  wxid: "用户ID",
  common_groups: "共同群数",
  avatar: "头像链接",
};

function switchTab(which) {
  show($("panel-moments"), which === "moments");
  show($("panel-wechat"), which === "wechat");
  show($("panel-contacts"), which === "contacts");
  $("tab-moments").classList.toggle("active", which === "moments");
  $("tab-wechat").classList.toggle("active", which === "wechat");
  $("tab-contacts").classList.toggle("active", which === "contacts");
  if (which !== "wechat") stopWatch();
  else if (state.article && !$("author").hidden) startWatch();
  if (which === "contacts" && !cState.loaded && mState.inited) loadContactsAll();
}
$("tab-moments").onclick = () => switchTab("moments");
$("tab-wechat").onclick = () => switchTab("wechat");
$("tab-contacts").onclick = () => switchTab("contacts");

const mState = {
  inited: false,
  account: "",
  contacts: [],
  posts: [],
  picked: new Set(),
  selected: new Set(),
  anchor: null,
  focus: null,
  page: 0,
  pageSize: 100,
};

$("m-init").onclick = initMoments;

async function restoreMoments() {
  let res;
  try {
    res = await api("/api/moments/status", undefined, true);
  } catch (e) {
    return;
  }
  if (!res || !res.ok) return;
  if (res.init_job) {
    setInitBusy();
    startJob(res.init_job, "刷新微信缓存", "", finishInit, "init");
  }
  if (!res.ready) return;
  mState.inited = true;
  mState.account = res.account || "";
  if (!res.init_job) resetInitBtn();
  show($("m-body"), true);
  await loadContacts();
  await refreshTimeline();
  loadContactsAll();
}

function setInitBusy() {
  for (const id of ["m-init", "c-init"]) {
    $(id).disabled = true;
    $(id).textContent = "刷新中…";
  }
}

async function initMoments() {
  setInitBusy();
  try {
    const res = await api("/api/moments/init", {});
    if (!res.ok) { resetInitBtn(); return; }
    startJob(res.job, "刷新微信缓存", "", finishInit, "init");
  } catch (error) { resetInitBtn(); }
}

async function finishInit(job) {
  resetInitBtn();
  if (job.status !== "done") return;
  lastCoverageSignature = "";
  mState.inited = true;
  mState.account = job.results?.[0]?.account || "";
  cState.loaded = false;
  show($("m-body"));
  await loadContacts();
  await refreshTimeline();
  await loadContactsAll();
}

function resetInitBtn() {
  $("m-init").disabled = false;
  $("m-init").textContent = "刷新";
  const ci = $("c-init");
  ci.disabled = false;
  ci.textContent = "刷新";
}

async function loadContacts() {
  const res = await api("/api/moments/contacts", {});
  if (!res.ok) return;
  mState.contacts = sortContactsByPinyin(res.contacts || []);
  mState.picked = new Set(mState.contacts.map((c) => c.wxid));
  renderFriends();
}

function sortContactsByPinyin(list) {
  const coll = new Intl.Collator("zh-Hans-CN", { sensitivity: "base", numeric: true });
  const self = list.filter((c) => c.wxid === mState.account || c.remark === "我" || c.nickname === "我");
  const rest = list.filter((c) => !self.includes(c));
  rest.sort((a, b) => coll.compare(a.remark || a.nickname || a.wxid, b.remark || b.nickname || b.wxid));
  return self.concat(rest);
}

function renderFriends() {
  const kw = $("m-friend-search").value.trim().toLowerCase();
  const box = $("m-friend-list");
  box.innerHTML = "";
  mState.contacts
    .filter((c) => !kw || `${c.remark}${c.nickname}${c.wxid}`.toLowerCase().includes(kw))
    .forEach((c) => {
      const label = document.createElement("label");
      label.className = "m-friend";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.dataset.wxid = c.wxid;
      cb.checked = mState.picked.has(c.wxid);

      const parts = [];
      if (c.remark) parts.push(c.remark);
      if (c.nickname && c.nickname !== c.remark) parts.push(c.nickname);
      const name = parts.length ? parts.join("/") : c.wxid;
      const span = document.createElement("span");
      span.textContent = name;
      span.title = c.wxid;
      cb.onchange = () => {
        if (cb.checked) mState.picked.add(c.wxid);
        else mState.picked.delete(c.wxid);
        updateFriendCount();
        scheduleRefresh();
      };
      label.append(cb, span);
      box.append(label);
    });
  updateFriendCount();
}

function updateFriendCount() {
  const all = mState.contacts.length > 0
    && mState.picked.size === mState.contacts.length;
  $("m-friend-count").textContent = mState.contacts.length
    ? `已选 ${mState.picked.size}/${mState.contacts.length} 人` : "";
  const btn = $("m-friend-all");
  btn.classList.toggle("active", all);
  btn.textContent = all ? "已全选" : "全选";

  const dates = [];
  if ($("m-start").value) dates.push($("m-start").value.slice(5).replace("-", "/"));
  if ($("m-end").value) dates.push($("m-end").value.slice(5).replace("-", "/"));
  const people = all ? "全部好友" : `${mState.picked.size} 人`;
  $("m-filter-state").textContent =
    (dates.length ? ` ${dates.join(" ~ ")}` : " 全部时间") + ` · ${people}`;
}

let refreshTimer = null;
function scheduleRefresh() {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(refreshTimeline, 400);
}

$("m-friend-search").addEventListener("input", renderFriends);


$("m-friend-all").onclick = () => {
  if (mState.picked.size === mState.contacts.length) {
    mState.picked.clear();
  } else {
    mState.picked = new Set(mState.contacts.map((c) => c.wxid));
  }
  renderFriends();
  refreshTimeline();
};
$("m-friend-clear").onclick = () => {
  mState.picked.clear();
  renderFriends();
  refreshTimeline();
};
$("m-start").addEventListener("change", refreshTimeline);
$("m-end").addEventListener("change", refreshTimeline);

function openFilter() {
  renderFriends();
  updateFriendCount();
  show($("m-filter-overlay"), true);
}
$("m-filter-btn").onclick = openFilter;
const closeFilter = () => show($("m-filter-overlay"), false);
$("fm-close").onclick = closeFilter;
$("fm-apply").onclick = closeFilter;
$("m-filter-overlay").addEventListener("click", (e) => {
  if (e.target === $("m-filter-overlay")) closeFilter();
});

let lastCoverageSignature = "";
function shiftDate(value, days) {
  const date = new Date(`${value.slice(0, 10)}T12:00:00`);
  date.setDate(date.getDate() + days);
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
}
function coveragePeriods(report) {
  if (!report) return [];
  const periods = (report.large_gaps || []).map((gap) => ({
    start: shiftDate(gap.from, 1), end: shiftDate(gap.to, -1), days: gap.gap_days,
  }));
  if (report.local_stale_days >= (report.large_gap_threshold_days || 21) && report.local_latest) {
    const start = shiftDate(report.local_latest, 1);
    periods.push({ start, end: shiftDate(start, report.local_stale_days - 1), days: report.local_stale_days });
  }
  return periods.sort((a, b) => a.start.localeCompare(b.start));
}
function fillCoverage(box, report) {
  box.replaceChildren();
  if (!report) {
    box.textContent = "请先刷新，再查看朋友圈缺失时间段。";
    return;
  }
  const range = document.createElement("div");
  range.className = "coverage-range";
  range.textContent = report.local_earliest
    ? `本地朋友圈范围：${report.local_earliest.slice(0, 10)} 至 ${report.local_latest.slice(0, 10)} · ${report.local_posts_with_time} 条`
    : "本地尚无朋友圈记录";
  box.append(range);
  const periods = coveragePeriods(report);
  const summary = document.createElement("p");
  summary.textContent = !report.local_earliest ? "本地没有可识别时间的记录。"
    : periods.length ? `发现 ${periods.length} 段疑似缺失时间：`
    : "未发现连续 21 天以上无记录的时段。";
  box.append(summary);
  if (periods.length) {
    const list = document.createElement("ul");
    list.className = "coverage-periods";
    for (const period of periods) {
      const line = document.createElement("li");
      line.textContent = `${period.start} 至 ${period.end} · ${period.days} 天`;
      list.append(line);
    }
    box.append(list);
  }
}
function renderCoverage(report) {
  if (!report) return;
  const periods = coveragePeriods(report);
  const signature = JSON.stringify([report.local_earliest, report.local_latest, periods]);
  if (signature === lastCoverageSignature) return;
  lastCoverageSignature = signature;
  floating.close("coverage");
  if (report.local_earliest && !periods.length) return;
  const popup = floating.open("coverage", "朋友圈疑似缺失时间段", "warning");
  fillCoverage(popup.body, report);
  const link = document.createElement("button");
  link.className = "ghost";
  link.textContent = "查看完整日志";
  link.onclick = () => { floating.close("coverage"); openSupport("logs"); };
  popup.body.append(link);
  floating.finish(popup, 8000);
}

let timelineRequest = 0;
async function refreshTimeline() {
  if (!mState.inited) return;
  const requestId = ++timelineRequest;
  const sv = $("m-start").value;
  const ev = $("m-end").value;
  if (sv && ev && ev < sv) {
    notify("结束日期必须晚于开始日期", "warning");
    return;
  }
  const res = await api("/api/moments/timeline", {
    usernames: [...mState.picked],
    start: sv || "",
    end: ev || "",
  });
  if (requestId !== timelineRequest) return;
  if (!res.ok) {
    if (/未初始化|未就绪|解密|请先点击「重新解密」/.test(res.msg || "")) {
      mState.inited = false;
      show($("m-body"), false);
      resetInitBtn();
    }
    return;
  }
  renderCoverage(res.coverage);
  mState.posts = res.posts || [];
  mState.selected = new Set(mState.posts.map((_, i) => i));
  mState.anchor = null;
  mState.focus = null;
  mState.page = 0;
  renderTimeline();
}

function renderTimeline() {
  const list = $("m-list");
  list.innerHTML = "";
  const size = mState.pageSize;
  const start = mState.page * size;
  const pagePosts = mState.posts.slice(start, start + size);
  pagePosts.forEach((p, j) => {
    const i = start + j;
    const row = document.createElement("div");
    row.className = "row mrow";
    row.dataset.i = i;
    row.tabIndex = 0;

    const t = document.createElement("div");
    t.className = "t";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = mState.selected.has(i);
    const span = document.createElement("span");
    span.textContent = p.datetime;
    span.title = p.datetime;
    cb.onchange = () => {
      const k = Number(row.dataset.i);
      if (cb.checked) mState.selected.add(k);
      else mState.selected.delete(k);
      mState.anchor = k;
      mState.focus = k;
      updateExportBtn();
    };
    const qb = document.createElement("button");
    qb.type = "button";
    qb.className = "qbtn";
    qb.title = "预览详情";
    qb.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>';
    qb.onclick = () => openPostDetail(i);
    t.append(cb, qb, span);

    const d = document.createElement("div");
    d.className = "d"; d.textContent = p.display; d.title = p.username || "";
    const c = document.createElement("div");
    c.className = "c"; c.textContent = p.text || p.title || "（无文字）";
    c.title = p.text || p.title || "";
    const m = document.createElement("div");
    m.className = "media"; m.textContent = p.mediaCount ? `图 ${p.mediaCount}` : "";
    row.append(t, d, c, m);
    list.append(row);
  });
  $("m-status").textContent = `共计 ${mState.posts.length} 条`;
  if (!mState.posts.length) {
    $("m-list-count").textContent = mState.picked.size === 0
      ? "未选择任何好友"
      : "该筛选条件下没有朋友圈";
  } else {
    $("m-list-count").textContent =
      `共 ${mState.posts.length} 条`;
  }
  renderPager($("m-pager"), mState.posts.length, size, mState.page,
    (p) => { mState.page = p; renderTimeline(); });
  updateExportBtn();
}

async function openPostDetail(i) {
  const p = mState.posts[i];
  if (!p) return;
  show($("pd-overlay"), true);
  const body = $("pd-body");
  body.innerHTML = '<div class="pd-empty muted">加载中…</div>';
  const res = await api("/api/moments/post_detail", {
    createTime: p.createTime, username: p.username,
  });
  if (!res.ok) {
    body.innerHTML = `<div class="pd-empty muted">${escapeHtml(res.msg || "加载失败")}</div>`;
    return;
  }
  const d = res.detail || {};
  const media = (d.media || []).map((m) => {
    if (m.data) return `<img src="${m.data}" loading="lazy" alt="">`;
    if (String(m.type).startsWith("image")) return '<span class="pd-noimg">图片不可预览（导出时会下载）</span>';
    return "";
  }).join("");
  const likes = (d.likes && d.likes.length)
    ? `<div class="pd-likes">❤️ 点赞：${d.likes.map(escapeHtml).join("、")}</div>` : "";
  const comments = (d.comments && d.comments.length)
    ? `<div class="pd-comments">${d.comments.map((c) =>
        `<div class="pd-cmt"><b>${escapeHtml(c.author)}</b>：${escapeHtml(c.content)}</div>`).join("")}</div>` : "";
  const loc = d.location
    ? `<div class="pd-loc">📍 ${escapeHtml(d.location)}</div>` : "";
  const url = d.url
    ? `<div class="pd-url"><a href="${escapeHtml(d.url)}" target="_blank" rel="noopener">查看原文</a></div>` : "";
  body.innerHTML = `
    <div class="pd-head">${escapeHtml(d.display)} · ${escapeHtml(d.datetime)}</div>
    ${d.title ? `<div class="pd-title">${escapeHtml(d.title)}</div>` : ""}
    ${d.text ? `<div class="pd-text">${escapeHtml(d.text)}</div>` : '<div class="pd-text muted">（无文字）</div>'}
    ${loc}
    ${media ? `<div class="pd-media">${media}</div>` : ""}
    ${likes}
    ${comments}
    ${url}
  `;
}

$("pd-close").onclick = () => show($("pd-overlay"), false);
$("pd-overlay").addEventListener("click", (e) => {
  if (e.target === $("pd-overlay")) show($("pd-overlay"), false);
});

function updateExportBtn() {
  const n = mState.selected.size;
  $("m-export").disabled = n === 0;
  const all = $("m-select-all");
  if (all) all.textContent = (mState.posts.length && n === mState.posts.length) ? "取消全选" : "全选";
}

$("m-list").addEventListener("click", (e) => {
  const row = e.target.closest(".mrow");
  if (!row || e.target.closest("button")) return;
  if (e.target.closest("input")) return;
  const i = Number(row.dataset.i);
  if (e.shiftKey && mState.anchor !== null) {
    selectRangeM(mState.anchor, i);
  } else {
    toggleM(i);
    mState.anchor = i;
  }
  mState.focus = i;
  paintM();
});

function toggleM(i) {
  mState.selected.has(i) ? mState.selected.delete(i) : mState.selected.add(i);
  updateExportBtn();
}
function selectRangeM(a, b) {
  mState.selected.clear();
  const [s, e] = a <= b ? [a, b] : [b, a];
  for (let i = s; i <= e; i++) mState.selected.add(i);
  updateExportBtn();
}
function paintM() {
  document.querySelectorAll("#m-list .mrow").forEach((row) => {
    const i = Number(row.dataset.i);
    row.querySelector("input").checked = mState.selected.has(i);
    row.classList.toggle("focus", mState.focus === i);
  });
}
function focusM(i) {
  const row = document.querySelector(`#m-list .mrow[data-i="${i}"]`);
  if (row) row.focus();
}

$("m-list").addEventListener("keydown", (e) => {
  const row = e.target.closest(".mrow");
  if (!row) return;
  const i = Number(row.dataset.i);
  const last = mState.posts.length - 1;

  if (e.key === " ") {
    e.preventDefault();
    toggleM(i);
    if (mState.anchor === null) mState.anchor = i;
    mState.focus = i;
    paintM();
  } else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    const next = Math.max(0, Math.min(last, i + (e.key === "ArrowDown" ? 1 : -1)));
    if (e.shiftKey) {
      if (mState.anchor === null) mState.anchor = i;
      selectRangeM(mState.anchor, next);
      mState.focus = next;
      paintM();
    } else {
      mState.anchor = next;
      mState.focus = next;
      paintM();
    }
    focusM(next);
  } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "a") {
    e.preventDefault();
    selectAllM();
  }
});

function selectAllM() {
  mState.selected.clear();
  mState.posts.forEach((_, i) => mState.selected.add(i));
  mState.anchor = 0;
  updateExportBtn();
  paintM();
}

$("m-select-all").onclick = () => {
  const n = mState.posts.length;
  if (n && mState.selected.size === n) {
    mState.selected.clear();
    updateExportBtn();
    paintM();
    $("m-select-all").textContent = "全选";
    return;
  }
  selectAllM();
  $("m-select-all").textContent = "取消全选";
};

$("m-export").onclick = async () => {
  if (!mState.selected.size) return;
  const picked = [...mState.selected].sort((a, b) => a - b)
    .map((i) => mState.posts[i])
    .filter(Boolean)
    .map((p) => ({ createTime: p.createTime, username: p.username }));
  if (!picked.length) { notify("所选条目无效，请重新勾选"); return; }
  const res = await api("/api/export", {
    type: "moments",
    picked,
    keep_interactions: $("m-keep").checked,
  });
  if (!res.ok) return;
  startJob(res.job, `朋友圈 ${picked.length} 条`, res.dir || "");
};

$("c-init").onclick = initMoments;

async function loadContactsAll() {
  let res;
  try {
    res = await api("/api/contacts/all", {});
  } catch (e) {
    return;
  }
  if (!res.ok) {
    return;
  }
  cState.loaded = true;
  cState.contacts = sortContactsByPinyin(res.contacts || []);
  CONTACT_COLS = res.columns || CONTACT_COLS;
  show($("c-body"), true);
  $("c-export").disabled = cState.columns.length === 0;
  renderCols();
  renderContactTable();
}

function renderCols() {
  const box = $("c-cols");
  box.innerHTML = "";
  for (const [key, label] of Object.entries(CONTACT_COLS)) {
    const lab = document.createElement("label");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = cState.columns.includes(key);
    cb.dataset.key = key;
    cb.onchange = () => {
      if (cb.checked) cState.columns.push(key);
      else cState.columns = cState.columns.filter((k) => k !== key);
      $("c-export").disabled = cState.columns.length === 0;
      renderContactTable();
    };
    lab.append(cb, document.createTextNode(label));
    box.append(lab);
  }
}

function filteredContacts() {
  const kw = $("c-search").value.trim().toLowerCase();
  if (!kw) return cState.contacts;
  return cState.contacts.filter((c) =>
    `${c.remark}${c.nickname}${c.wechat_id}${c.wxid}`.toLowerCase().includes(kw));
}

function cellText(c, col) {
  if (col === "remark") return c.remark || c.nickname || c.wxid || "";
  if (col === "region") {
    return [c.country, c.province, c.city].filter(Boolean).join(" ");
  }
  return c[col] || "";
}

function renderContactTable() {
  const cols = cState.columns;
  const items = filteredContacts();
  const thead = $("c-thead");
  thead.innerHTML = "";
  const hr = document.createElement("tr");
  cols.forEach((c) => {
    const th = document.createElement("th");
    th.textContent = CONTACT_COLS[c] || c;
    hr.append(th);
  });
  thead.append(hr);

  const size = cState.pageSize;
  const pages = Math.max(1, Math.ceil(items.length / size));
  if (cState.page > pages - 1) cState.page = pages - 1;
  const start = cState.page * size;
  const tbody = $("c-tbody");
  tbody.innerHTML = "";
  items.slice(start, start + size).forEach((c) => {
    const tr = document.createElement("tr");
    cols.forEach((col) => {
      const td = document.createElement("td");
      const v = cellText(c, col);
      if (col === "avatar" && v) {

        const wrap = document.createElement("span");
        wrap.className = "avatar-cell";
        const link = document.createElement("a");
        link.href = v;
        link.target = "_blank";
        link.rel = "noreferrer";
        link.textContent = "头像链接";
        link.title = v;
        wrap.append(link);
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "copy-btn";
        btn.textContent = "复制";
        btn.onclick = async (ev) => {
          ev.stopPropagation();
          try {
            await navigator.clipboard.writeText(v);
            notify("头像链接已复制", "success");
          } catch (e) {
            notify("复制失败，请检查浏览器的剪贴板权限。");
          }
        };
        wrap.append(btn);
        td.append(wrap);
      } else {
        td.textContent = v;
        td.title = v;
      }
      tr.append(td);
    });
    tbody.append(tr);
  });

  $("c-count").textContent = `共 ${items.length} 人`;
  $("c-status").textContent = `共计 ${cState.contacts.length} 人`;
  renderPager($("c-pager"), items.length, size, cState.page,
    (p) => { cState.page = p; renderContactTable(); });
}

$("c-search").addEventListener("input", () => { cState.page = 0; renderContactTable(); });

$("c-export").onclick = async () => {
  const cols = cState.columns;
  if (!cols.length) { notify("请至少勾选一列再导出", "warning"); return; }
  $("c-export").disabled = true;
  const popup = floating.open("contacts-export", "正在导出好友列表…");
  try {
    const res = await api("/api/contacts/export", { columns: cols });
    if (!res.ok) return;
    popup.title.textContent = "好友列表已导出";
    popup.card.dataset.level = "success";
    popup.body.textContent = `${res.count} 人 · 保存到 ${res.path}`;
    floating.finish(popup, 4000);
  } catch (error) { /* The API helper displays the error. */ }
  finally {
    if (!popup.remaining) floating.close("contacts-export");
    $("c-export").disabled = cState.columns.length === 0;
  }
};

const state = {
  article: null,
  articles: [],
  selected: new Set(),
  anchor: null,
  focus: null,
  watchPoll: null,
  biz: "",
  listKey: "",
  knownKeys: new Set(),
  page: 0,
  pageSize: 50,
};

const keyOf = (a) => `${a.mid || ""}_${a.idx || ""}`;

$("btn-parse").onclick = doParse;
$("url").addEventListener("keydown", (e) => { if (e.key === "Enter") doParse(); });

async function doParse() {
  const url = $("url").value.trim();
  if (!url) return;
  stopWatch();
  $("btn-parse").disabled = true;
  $("btn-parse").textContent = "解析中…";
  try {
    const res = await api("/api/parse", { url });
    if (!res.ok) return;
    state.article = res.article;
    state.biz = res.article.biz;
    renderArticle();
    hideAuthor();
  } finally {
    $("btn-parse").disabled = false;
    $("btn-parse").textContent = "解析";
  }
}

function renderArticle() {
  const a = state.article;
  $("art-title").textContent = a.title || "(无标题)";
  $("art-meta").textContent =
    [a.account, a.author, a.publish_text].filter(Boolean).join(" ｜ ");
  show($("article"), true);
}

function hideAuthor() {
  stopWatch();
  show($("author"), false);
  state.articles = [];
  state.selected.clear();
}

$("btn-export").onclick = async () => {
  if (!state.article) return;
  const res = await api("/api/export", { type: "article", url: state.article.url });
  if (!res.ok) return;
  startJob(res.job, state.article.title || "文章", res.dir || "");
};

$("btn-author").onclick = loadAuthor;

async function loadAuthor() {
  if (!state.article) return;
  stopWatch();
  state.knownKeys = new Set();
  state.listKey = "";
  state.articles = [];
  state.selected.clear();
  state.anchor = null;
  state.focus = null;
  show($("author"), true);
  $("author-name").textContent = state.article.account || "该公众号";
  $("author-count").textContent = "加载中…";
  renderList();

  const res = await api("/api/author/articles", { url: state.article.url });
  if (!res.ok) {
    $("author-count").textContent = "";
    return;
  }
  if (res.need_open) {
    $("author-count").textContent = "已收录 0 篇";
    notify(res.msg || "先在微信中点开该公众号的文章，会自动收录。", "warning");
    state.articles = [];
    startWatch();
    return;
  }
  state.articles = res.articles || [];
  state.knownKeys = new Set(state.articles.map(keyOf));
  $("author-count").innerHTML =
    '<span class="live-dot"></span>正在监听微信 · 已收录 ' +
    state.articles.length + " 篇";
  notify("列表只含已点开过的文章。在微信继续点开该公众号的文章，列表会自动更新。", "info");
  renderList();
  startWatch();
}

let watchGeneration = 0;
let lastWatchWarning = "";
let watchStopPromise = Promise.resolve();
async function startWatch() {
  if (!state.biz || state.watchPoll) return;
  const generation = ++watchGeneration;
  const biz = state.biz;
  try {
    await watchStopPromise;
    if (generation !== watchGeneration) return;
    const res = await api("/api/author/watch/start", { biz });
    if (!res.ok || generation !== watchGeneration) return;
    state.watchPoll = setTimeout(watchPoll, 3000);
  } catch (error) { /* The API helper displays the error. */ }
}

async function watchPoll() {
  if (!state.biz) return;
  const generation = watchGeneration;
  let res;
  try {
    res = await api("/api/author/watch/status", { biz: state.biz });
  } catch (error) {
    if (generation === watchGeneration) state.watchPoll = setTimeout(watchPoll, 3000);
    return;
  }
  if (generation !== watchGeneration) return;
  state.watchPoll = setTimeout(watchPoll, 3000);
  if (!res.ok) return;
  const watchWarning = [res.msg, res.failed_titles ? `${res.failed_titles} 篇文章信息补全失败，请稍后重新收录。` : ""].filter(Boolean).join("\n");
  if (watchWarning && watchWarning !== lastWatchWarning) notify(watchWarning, "warning");
  lastWatchWarning = watchWarning;
  const n = res.total;


  const listening = res.running
    ? '<span class="live-dot"></span>正在监听微信'
    : "未在监听";
  let txt = `${listening} · 已收录 ${n} 篇`;
  if (res.filling_left > 0) txt += `，待补齐标题/时间 ${res.filling_left} 篇`;
  if (res.failed_titles > 0) txt += `，${res.failed_titles} 篇信息补全失败`;
  $("author-count").innerHTML = txt;


  (res.articles || []).forEach((article) => {
    if (!state.knownKeys.has(keyOf(article))) {
      enqueueBanner(`成功读取《${article.title || `文章 ${article.mid}`}》`);
    }
    state.knownKeys.add(keyOf(article));
  });

  const key = (res.articles || [])
    .map((a) => `${a.title || ""}|${a.time || ""}`).join("\n");
  if (key !== state.listKey) {
    const selectedKeys = new Set([...state.selected].map((i) => keyOf(state.articles[i] || {})));
    state.articles = res.articles || [];
    state.selected = new Set(state.articles.flatMap((article, i) => selectedKeys.has(keyOf(article)) ? [i] : []));
    state.anchor = null;
    state.focus = null;
    state.listKey = key;
    renderList();
  }
}

function stopWatch() {
  watchGeneration++;
  if (state.watchPoll) {
    clearTimeout(state.watchPoll);
    state.watchPoll = null;
  }
  if (state.biz) watchStopPromise = api("/api/author/watch/stop", { biz: state.biz }, true).catch(() => {});
}

function renderList() {
  const list = $("list");
  list.innerHTML = "";
  const size = state.pageSize;
  const pages = Math.max(1, Math.ceil(state.articles.length / size));
  if (state.page > pages - 1) state.page = pages - 1;
  const start = state.page * size;
  state.articles.slice(start, start + size).forEach((a, j) => {
    const i = start + j;
    const row = document.createElement("div");
    row.className = "row";
    row.dataset.i = i;
    row.tabIndex = 0;

    const t = document.createElement("div");
    t.className = "t";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = state.selected.has(i);
    const span = document.createElement("span");
    span.textContent = a.title || `文章 ${a.mid}`;
    span.title = a.title || "";
    t.append(cb, span);

    const d = document.createElement("div");
    d.className = "d";
    d.textContent = a.time;

    const btn = document.createElement("button");
    btn.textContent = "下载";

    btn.disabled = !a.title;
    if (a.title) btn.className = "primary";
    btn.title = a.title ? "下载这篇文章" : "正在解析标题，稍等片刻即可下载";
    btn.onclick = (e) => { e.stopPropagation(); downloadOne(i); };

    row.append(t, d, btn);
    list.append(row);
  });
  state.listKey = state.articles
    .map((a) => `${a.title || ""}|${a.time || ""}`).join("\n");
  renderPager($("list-pager"), state.articles.length, size, state.page,
    (p) => { state.page = p; renderList(); });
  updatePickBtn();
}

function updatePickBtn() {
  const n = state.selected.size;
  $("btn-download-all").disabled = state.articles.length === 0;
  $("btn-download-picked").disabled = n === 0;
  $("btn-download-picked").textContent = n ? `下载选中 (${n})` : "下载选中";
}

$("list").addEventListener("click", (e) => {
  const row = e.target.closest(".row");
  if (!row || e.target.closest("button")) return;
  const i = Number(row.dataset.i);
  if (e.shiftKey && state.anchor !== null) {
    selectRange(state.anchor, i);
  } else {
    toggle(i);
    state.anchor = i;
  }
  state.focus = i;
  paint();
});

function toggle(i) {
  state.selected.has(i) ? state.selected.delete(i) : state.selected.add(i);
  updatePickBtn();
}
function selectRange(a, b) {
  state.selected.clear();
  const [s, e] = a <= b ? [a, b] : [b, a];
  for (let i = s; i <= e; i++) state.selected.add(i);
  updatePickBtn();
}
function paint() {
  document.querySelectorAll("#list .row").forEach((row) => {
    const i = Number(row.dataset.i);
    row.querySelector("input").checked = state.selected.has(i);
    row.classList.toggle("focus", state.focus === i);
  });
}
function focusRow(i) {
  const row = document.querySelector(`#list .row[data-i="${i}"]`);
  if (row) row.focus();
}

$("list").addEventListener("keydown", (e) => {
  const row = e.target.closest(".row");
  if (!row) return;
  const i = Number(row.dataset.i);
  const last = state.articles.length - 1;

  if (e.key === " ") {
    e.preventDefault();
    toggle(i);
    if (state.anchor === null) state.anchor = i;
    state.focus = i;
    paint();
  } else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    const next = Math.max(0, Math.min(last, i + (e.key === "ArrowDown" ? 1 : -1)));
    if (e.shiftKey) {
      if (state.anchor === null) state.anchor = i;
      selectRange(state.anchor, next);
      state.focus = next;
      paint();
    } else {
      state.anchor = next;
      state.focus = next;
      paint();
    }
    focusRow(next);
  } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "a") {
    e.preventDefault();
    selectAll();
  }
});

document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "a") {
    const tag = (document.activeElement.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea") return;
    if (!$("panel-wechat").hidden && !$("author").hidden) {
      e.preventDefault();
      selectAll();
    } else if (!$("panel-moments").hidden && !$("m-body").hidden) {
      e.preventDefault();
      selectAllM();
    }
  }
});

function selectAll() {
  state.selected.clear();
  state.articles.forEach((_, i) => state.selected.add(i));
  state.anchor = 0;
  updatePickBtn();
  paint();
}

$("btn-select-all").onclick = () => {
  if (state.selected.size === state.articles.length && state.articles.length) {
    state.selected.clear();
    updatePickBtn();
    paint();
    $("btn-select-all").textContent = "全选";
    return;
  }
  selectAll();
  $("btn-select-all").textContent = "取消全选";
};

async function downloadOne(i) {
  const a = state.articles[i];
  const res = await api("/api/export", {
    type: "author", articles: [a],
    account: state.article.account, biz: state.biz, mode: "select",
  });
  if (!res.ok) return;
  startJob(res.job, `下载：${a.title || a.mid}`, res.dir || "");
}

$("btn-download-picked").onclick = async () => {
  const picked = [...state.selected].sort((x, y) => x - y).map((i) => state.articles[i]);
  if (!picked.length) return;
  const res = await api("/api/export", {
    type: "author", articles: picked,
    account: state.article.account, biz: state.biz, mode: "select",
  });
  if (!res.ok) return;
  startJob(res.job, `下载选中的 ${picked.length} 篇`, res.dir || "");
};

$("btn-download-all").onclick = async () => {
  if (!state.articles.length) return;
  const res = await api("/api/export", {
    type: "author", articles: state.articles,
    account: state.article.account, biz: state.biz, mode: "all",
  });
  if (!res.ok) return;
  startJob(res.job, `下载全部 ${state.articles.length} 篇`, res.dir || "");
};

// Initialize after all state objects exist (including contacts and author state).
restoreMoments();
pollDiagnostics();
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") { closeFilter(); show($("pd-overlay"), false); closeSupport(); }
  if (event.key === "Tab" && !$("support-overlay").hidden) {
    const focusable = [...$("support-overlay").querySelectorAll("button:not(:disabled), summary, [tabindex='0']")]
      .filter((element) => element.getClientRects().length);
    const first = focusable[0], last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  }
});
