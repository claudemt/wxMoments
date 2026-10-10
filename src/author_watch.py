"""One stoppable cache-harvest worker per author; snapshots are thread-safe."""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from wechat_download import harvest

logger = logging.getLogger(__name__)


class AuthorWatch:
    def __init__(self, biz: str, load_article):
        self.biz = biz
        self.load_article = load_article
        self.items: dict[str, dict] = {}
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.filling = False
        self.error = ""
        self.retries: dict[str, tuple[int, float]] = {}
        self.last_seen = time.monotonic()

    def start(self) -> bool:
        with self.lock:
            if self.thread and self.thread.is_alive():
                return not self.stop_event.is_set()
            self.stop_event.clear()
            self.last_seen = time.monotonic()
            self.retries.clear()
            self.thread = threading.Thread(target=self._run, daemon=True,
                                           name=f"author-{self.biz}")
            self.thread.start()
            return True

    def stop(self) -> None:
        self.stop_event.set()

    def snapshot(self) -> dict:
        with self.lock:
            self.last_seen = time.monotonic()
            articles = []
            for d in self.items.values():
                h = harvest.Harvested(url=d["url"], mid=d["mid"], idx=d["idx"],
                                      publish_ts=d.get("publish_ts", 0))
                articles.append({"title": d.get("title", ""), "url": h.url,
                                 "mid": h.mid, "idx": h.idx, "time": h.time_text,
                                 "date": h.date_text, "dt": h.publish_ts})
            articles.sort(key=lambda d: int(d["mid"] or 0), reverse=True)
            return {"ok": True,
                    "running": bool(self.thread and self.thread.is_alive()
                                    and not self.stop_event.is_set()),
                    "filling": self.filling, "msg": self.error,
                    "filling_left": sum(not d.get("title") and self.retries.get(key, (0, 0))[0] < 3
                                        for key, d in self.items.items()),
                    "failed_titles": sum(not d.get("title") and self.retries.get(key, (0, 0))[0] >= 3
                                         for key, d in self.items.items()),
                    "total": len(articles), "articles": articles}

    def _fill(self, key: str) -> None:
        with self.lock:
            url = self.items[key]["url"]
        try:
            art = self.load_article(url, self.biz)
            if not art.title:
                raise ValueError("文章未返回标题")
            with self.lock:
                self.items[key].update(title=art.title, publish_ts=art.publish_ts)
                self.retries.pop(key, None)
        except Exception as exc:
            with self.lock:
                attempts = self.retries.get(key, (0, 0))[0] + 1
                self.retries[key] = (attempts, time.monotonic() + 60 * attempts)
            logger.warning("文章信息补全失败（第 %s 次）：%s", attempts, exc)

    def _run(self) -> None:
        tick = 0
        try:
            with ThreadPoolExecutor(max_workers=5) as pool:
                while not self.stop_event.is_set():
                    # A closed browser cannot send stop; release idle workers too.
                    with self.lock:
                        if time.monotonic() - self.last_seen > 90:
                            break
                    try:
                        found = harvest.harvest_articles(self.biz, use_pages=tick % 20 == 0)
                        with self.lock:
                            for h in found:
                                slot = self.items.setdefault(h.key, {})
                                slot.update(url=h.url, mid=h.mid, idx=h.idx)
                                if h.title:
                                    slot["title"] = h.title
                                if h.publish_ts:
                                    slot["publish_ts"] = h.publish_ts
                            self.error = ""
                    except Exception as exc:
                        with self.lock:
                            message = f"公众号缓存读取失败：{exc}"
                            if message != self.error:
                                logger.warning(message)
                            self.error = message
                    with self.lock:
                        todo = [key for key, d in self.items.items()
                                if not d.get("title") and d.get("url")
                                and self.retries.get(key, (0, 0))[1] <= time.monotonic()
                                and self.retries.get(key, (0, 0))[0] < 3]
                        self.filling = bool(todo)
                    futures = []
                    for key in todo:
                        if self.stop_event.is_set():
                            break
                        futures.append(pool.submit(self._fill, key))
                    for future in futures:
                        if self.stop_event.is_set():
                            for pending in futures:
                                pending.cancel()
                            break
                        future.result()
                    with self.lock:
                        self.filling = False
                    tick += 1
                    self.stop_event.wait(3.5)
        except Exception as exc:
            with self.lock:
                self.error = f"公众号收录已停止：{exc}"
            logger.exception(self.error)
        finally:
            with self.lock:
                self.filling = False
