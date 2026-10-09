

const $ = (id) => document.getElementById(id);



async function api(path, body, silent = false) {
  let r;
  try {
    r = await fetch(path, {
      method: body === undefined ? "GET" : "POST",
      headers: { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (e) {
    if (!silent) {
      alert("连不上本地服务（它可能已退出）。请重新双击 run.bat 启动后刷新页面。");
    }
    throw e;
  }
  if (!r.ok) {
    if (!silent) alert(`服务返回错误（${r.status}），请刷新后重试。`);
    throw new Error(`HTTP ${r.status}`);
  }
  return r.json();
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



const bannerQueue = [];
let bannerShowing = false;

function enqueueBanner(text) {
  bannerQueue.push(text);
  if (!bannerShowing) showNextBanner();
}

function showNextBanner() {
  if (!bannerQueue.length) { bannerShowing = false; return; }
  bannerShowing = true;
  const el = $("banner");
  const text = bannerQueue.shift();
  el.textContent = text;
  el.hidden = false;
  el.classList.remove("hide", "show");
  
  el.classList.toggle("below", !$("dl-banner").hidden);
  void el.offsetWidth;               
  el.classList.add("show");
  clearTimeout(el._t);
  el._t = setTimeout(() => {
    el.classList.add("hide");
    setTimeout(() => {
      el.hidden = true;
      showNextBanner();
    }, 420);
  }, 2600);
}



const task = { job: null, poll: null, title: "" };

function startJob(jobId, title, dir) {
  task.job = jobId;
  task.title = title;
  stopJobPoll();
  task.poll = setInterval(pollJob, 900);
  pollJob();
  
  const dl = $("dl-banner");
  $("dl-title").textContent = `下载中：${title}（0%）`;
  $("dl-msg").textContent = "";
  $("dl-dir").textContent = dir ? `下载目录：${dir}` : "";
  $("dl-bar").style.width = "0%";
  show($("btn-dl-cancel"), true);
  dl.hidden = false;
}

function stopJobPoll() {
  if (task.poll) { clearInterval(task.poll); task.poll = null; }
}

async function pollJob() {
  if (!task.job) return;
  let res;
  try {
    res = await api(`/api/job/${task.job}`, undefined, true);
  } catch (e) {
    return;
  }
  if (!res.ok) return;
  const job = res.job;
  const pct = job.total ? Math.round((job.done / job.total) * 100) : 0;
  if (job.status === "running") {
    
    $("dl-title").textContent = `下载中：${task.title}（${pct}%）`;
    const cur = job.message || "";
    $("dl-msg").textContent = cur.startsWith("(") ? `正在处理 ${cur}` : cur;
    $("dl-bar").style.width = `${pct}%`;
  } else {
    clearInterval(task.poll);
    task.poll = null;
    show($("btn-dl-cancel"), false);
    
    const dl = $("dl-banner");
    $("dl-title").textContent = job.status === "done"
      ? `下载完成：${job.message}`
      : job.message;
    $("dl-msg").textContent = "";
    $("dl-bar").style.width = "100%";
    clearTimeout(dl._t);
    dl._t = setTimeout(() => { dl.hidden = true; }, 4000);
  }
}

$("btn-dl-cancel").onclick = async () => {
  if (task.job) await api(`/api/job/${task.job}/cancel`, {});
};

$("btn-output").onclick = async () => {
  const res = await api("/api/reveal", { path: "" });
  if (!res.ok) alert("打开失败：" + res.msg);
};





const cState = {
  loaded: false,
  contacts: [],
  columns: ["remark", "nickname", "wechat_id", "region", "signature"],  
  page: 0,
  pageSize: 100,
};


const CONTACT_COLS = {
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
  if (which === "contacts" && !cState.loaded) loadContactsAll();
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
  if (!res || !res.ok || !res.ready) return;
  mState.inited = true;
  mState.account = res.account || "";
  resetInitBtn();
  show($("m-body"), true);
  await loadContacts();       
  await refreshTimeline();
  loadContactsAll();
}
restoreMoments();

async function initMoments() {
  $("m-init").disabled = true;
  $("m-init").textContent = "解密中…";
  let res;
  try {
    res = await api("/api/moments/init", {});
  } catch (e) { return; }
  if (!res.ok) { alert(res.msg || "初始化解密失败"); resetInitBtn(); return; }
  
  mInitPoll = setInterval(() => pollInitJob(res.job), 1200);
}

let mInitPoll = null;

async function pollInitJob(jobId) {
  let res;
  try {
    res = await api(`/api/job/${jobId}`, undefined, true);
  } catch (e) { return; }
  if (!res.ok) return;
  const job = res.job;
  $("m-status").textContent = job.message;
  if (job.status === "done") {
    clearInterval(mInitPoll);
    mInitPoll = null;
    mState.inited = true;
    mState.account = (job.results && job.results[0] && job.results[0].account) || "";
    resetInitBtn();
    $("m-status").textContent = job.message;
    show($("m-body"), true);
    await loadContacts();       
    await refreshTimeline();
    loadContactsAll();   
  } else if (job.status === "error") {
    clearInterval(mInitPoll);
    mInitPoll = null;
    resetInitBtn();
    $("m-status").textContent = "";
    alert(job.message);
  }
}

function resetInitBtn() {
  $("m-init").disabled = false;
  $("m-init").textContent = mState.inited ? "重新解密" : "初始化解密";
}

async function loadContacts() {
  const res = await api("/api/moments/contacts", {});
  if (!res.ok) { enqueueBanner(res.msg || "好友列表加载失败"); return; }
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
$("fm-cancel").onclick = closeFilter;
$("fm-apply").onclick = closeFilter;
$("m-filter-overlay").addEventListener("click", (e) => {
  if (e.target === $("m-filter-overlay")) closeFilter();
});

async function refreshTimeline() {
  if (!mState.inited) return;
  const sv = $("m-start").value;
  const ev = $("m-end").value;
  if (sv && ev && ev < sv) {
    enqueueBanner("结束日期必须晚于开始日期");
    return;
  }
  const res = await api("/api/moments/timeline", {
    usernames: [...mState.picked],
    start: sv || "",
    end: ev || "",
  });
  if (!res.ok) {
    
    enqueueBanner(res.msg || "朋友圈列表加载失败，请稍后重试");
    if (/未初始化|未就绪|解密/.test(res.msg || "")) {
      mState.inited = false;
      show($("m-body"), false);
      resetInitBtn();
    }
    return;
  }
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
      ? "未选择任何好友，列表为空（点筛选按钮全选即可恢复）"
      : "该筛选条件下没有朋友圈";
  } else {
    $("m-list-count").textContent =
      `共 ${mState.posts.length} 条，勾选后可组合下载`;
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
  const btn = $("m-export");
  btn.disabled = n === 0;
  btn.textContent = `下载选中（${n} 条）`;
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
  if (!picked.length) { alert("所选条目无效，请重新勾选"); return; }
  const res = await api("/api/export", {
    type: "moments",
    picked,
    keep_interactions: $("m-keep").checked,
  });
  if (!res.ok) { alert(res.msg || "导出失败"); return; }
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
    enqueueBanner(res.msg || "好友列表加载失败，请先在朋友圈页解密封装");
    return;
  }
  cState.loaded = true;
  cState.contacts = sortContactsByPinyin(res.contacts || []);
  show($("c-body"), true);
  $("c-init").textContent = "重新解密";
  $("c-export").disabled = false;
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
            btn.textContent = "已复制";
            btn.classList.add("copied");
            setTimeout(() => { btn.textContent = "复制"; btn.classList.remove("copied"); }, 1500);
          } catch (e) {
            btn.textContent = "失败";
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

  $("c-count").textContent = `共 ${items.length} 位好友`;
  $("c-status").textContent = `共计 ${cState.contacts.length} 人`;
  renderPager($("c-pager"), items.length, size, cState.page,
    (p) => { cState.page = p; renderContactTable(); });
}

$("c-search").addEventListener("input", () => { cState.page = 0; renderContactTable(); });

$("c-export").onclick = async () => {
  const cols = cState.columns;
  if (!cols.length) { enqueueBanner("请至少勾选一列再导出"); return; }
  const res = await api("/api/contacts/export", { columns: cols });
  if (!res.ok) { enqueueBanner(res.msg || "导出失败"); return; }
  enqueueBanner(`好友列表已导出：${res.path}（${res.count} 人）`);
  $("c-export").disabled = false;
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
  stopJobPoll();
  $("btn-parse").disabled = true;
  $("btn-parse").textContent = "解析中…";
  try {
    const res = await api("/api/parse", { url });
    if (!res.ok) { alert(res.msg || "解析失败"); return; }
    state.article = res.article;
    state.biz = res.article.biz;
    renderArticle();
    hideAuthor();
    $("dl-banner").hidden = true;
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
  if (!res.ok) { alert(res.msg || "导出失败"); return; }
  startJob(res.job, state.article.title || "文章", res.dir || "");
};



$("btn-author").onclick = loadAuthor;

async function loadAuthor() {
  if (!state.article) return;
  stopWatch();
  state.knownKeys = new Set();
  state.listKey = "";
  show($("author"), true);
  $("author-name").textContent = state.article.account || "该公众号";
  $("author-count").textContent = "加载中…";
  $("list").innerHTML = "";
  show($("open-panel"), false);

  const res = await api("/api/author/articles", { url: state.article.url });
  if (!res.ok) {
    $("author-count").textContent = res.msg || "加载失败";
    return;
  }
  if (res.need_open) {
    $("author-count").textContent = "缓存里还没有文章记录";
    show($("open-panel"), true);
    $("open-status").textContent = res.msg;
    state.articles = [];
    startWatch();   
    return;
  }
  state.articles = res.articles || [];
  state.knownKeys = new Set(state.articles.map(keyOf));
  $("author-count").innerHTML =
    '<span class="live-dot"></span>正在监听微信 · 已收录 ' +
    state.articles.length + " 篇";
  show($("open-panel"), true);
  $("open-status").textContent =
    "继续在微信里点开还没收录的文章，列表会实时变多；点完直接勾选下载。";
  renderList();
  startWatch();
}



function startWatch() {
  if (!state.biz || state.watchPoll) return;
  api("/api/author/watch/start", { biz: state.biz }, true)
    .catch(() => {})
    .then(() => {
      if (state.watchPoll) return;
      state.watchPoll = setInterval(watchPoll, 3000);
    });
}

async function watchPoll() {
  if (!state.biz) return;
  let res;
  try {
    res = await api("/api/author/watch/status", { biz: state.biz }, true);
  } catch (e) {
    return;   
  }
  if (!res.ok) return;
  const n = res.total;

  
  const listening = res.running
    ? '<span class="live-dot"></span>正在监听微信'
    : "未在监听";
  let txt = `${listening} · 已收录 ${n} 篇`;
  if (res.filling_left > 0) txt += `，正在补齐标题/发表时间（剩 ${res.filling_left}）`;
  $("author-count").innerHTML = txt;

  
  (res.added || []).forEach((a) => {
    if (state.knownKeys.has(keyOf(a))) return;
    enqueueBanner(`成功读取《${a.title || `文章 ${a.mid}`}》`);
  });
  (res.articles || []).forEach((a) => state.knownKeys.add(keyOf(a)));

  
  const key = (res.articles || [])
    .map((a) => `${a.title || ""}|${a.time || ""}`).join("\n");
  if (key !== state.listKey) {
    const before = state.selected;
    state.articles = res.articles || [];
    state.selected = new Set(
      [...before].filter((i) => i < state.articles.length)
    );
    state.listKey = key;
    renderList();
  }
}

function stopWatch() {
  if (state.watchPoll) {
    clearInterval(state.watchPoll);
    state.watchPoll = null;
  }
  if (state.biz) api("/api/author/watch/stop", { biz: state.biz }, true).catch(() => {});
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
  document.querySelectorAll(".row").forEach((row) => {
    const i = Number(row.dataset.i);
    row.querySelector("input").checked = state.selected.has(i);
    row.classList.toggle("focus", state.focus === i);
  });
}
function focusRow(i) {
  const row = document.querySelector(`.row[data-i="${i}"]`);
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
  if (!res.ok) { alert(res.msg || "导出失败"); return; }
  startJob(res.job, `下载：${a.title || a.mid}`, res.dir || "");
}

$("btn-download-picked").onclick = async () => {
  const picked = [...state.selected].sort((x, y) => x - y).map((i) => state.articles[i]);
  if (!picked.length) return;
  const res = await api("/api/export", {
    type: "author", articles: picked,
    account: state.article.account, biz: state.biz, mode: "select",
  });
  if (!res.ok) { alert(res.msg || "导出失败"); return; }
  startJob(res.job, `下载选中的 ${picked.length} 篇`, res.dir || "");
};

$("btn-download-all").onclick = async () => {
  if (!state.articles.length) return;
  const res = await api("/api/export", {
    type: "author", articles: state.articles,
    account: state.article.account, biz: state.biz, mode: "all",
  });
  if (!res.ok) { alert(res.msg || "导出失败"); return; }
  startJob(res.job, `下载全部 ${state.articles.length} 篇`, res.dir || "");
};
