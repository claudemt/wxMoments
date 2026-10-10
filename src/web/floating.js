// Shared floating windows for messages and work in progress.
const floating = (() => {
  const entries = new Map();
  const durations = { success: 3000, info: 4000, warning: 8000, error: 6000 };

  function close(key) {
    const entry = entries.get(key);
    if (!entry) return;
    clearTimeout(entry.timer);
    entries.delete(key);
    entry.card.remove();
    entry.onClose?.();
  }

  function resume(entry) {
    if (entry.hovered || entry.focused || !entry.remaining) return;
    entry.started = Date.now();
    entry.timer = setTimeout(() => close(entry.key), entry.remaining);
  }

  function pause(entry) {
    if (!entry.timer) return;
    clearTimeout(entry.timer);
    entry.timer = null;
    entry.remaining = Math.max(1, entry.remaining - (Date.now() - entry.started));
  }

  function finish(entry, duration = 4000) {
    clearTimeout(entry.timer);
    entry.timer = null;
    entry.remaining = duration;
    entry.closeButton.hidden = false;
    resume(entry);
  }

  function open(key, title, level = "info") {
    let entry = entries.get(key);
    if (entry) {
      entry.title.textContent = title;
      entry.card.dataset.level = level;
      return entry;
    }
    const card = document.createElement("div");
    card.className = "floating-window";
    card.dataset.level = level;
    card.setAttribute("role", level === "error" ? "alert" : "status");
    const head = document.createElement("div");
    head.className = "floating-head";
    const heading = document.createElement("strong");
    heading.textContent = title;
    const closeButton = document.createElement("button");
    closeButton.className = "popup-close";
    closeButton.textContent = "✕";
    closeButton.setAttribute("aria-label", "关闭浮窗");
    closeButton.hidden = true;
    closeButton.onclick = () => close(key);
    const body = document.createElement("div");
    body.className = "floating-body";
    head.append(heading, closeButton);
    card.append(head, body);
    entry = { key, card, title: heading, body, closeButton,
      timer: null, remaining: 0, hovered: false, focused: false, onClose: null };
    card.addEventListener("mouseenter", () => { entry.hovered = true; pause(entry); });
    card.addEventListener("mouseleave", () => { entry.hovered = false; resume(entry); });
    card.addEventListener("focusin", () => { entry.focused = true; pause(entry); });
    card.addEventListener("focusout", (event) => {
      entry.focused = card.contains(event.relatedTarget);
      if (!entry.focused) resume(entry);
    });
    entries.set(key, entry);
    // Keep active work visible; bound bursts of informational messages.
    const notices = [...entries.values()].filter((item) => item.remaining > 0);
    for (const item of notices.slice(0, Math.max(0, notices.length - 4))) close(item.key);
    document.getElementById("floating-windows").append(card);
    return entry;
  }

  function message(text, level = "info", title = "") {
    const key = `notice:${level}:${text}`;
    if (entries.has(key)) return entries.get(key);
    const entry = open(key, title || ({error: "操作失败", warning: "需要注意", success: "已完成"}[level] || "提示"), level);
    entry.body.textContent = text;
    finish(entry, durations[level] || durations.info);
    return entry;
  }

  return { open, close, finish, message };
})();
