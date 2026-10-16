"""Thu thập bài viết và bình luận qua phiên Chromium đang đăng nhập."""

import asyncio
import random
import re
import time
from .data import AppState, Comment, Post, Store, content_hash
from .extract import acceptable_image, cursor_has_next, extract_posts, merge_posts, parse_count, parse_json_payloads
from pathlib import Path
from playwright_stealth import Stealth
from typing import Any, Callable, Protocol
from urllib.parse import urljoin, urlsplit


_EXPAND_JS = r"""() => {
  const text = (node) => ((node.innerText || node.textContent || "") + "").replace(/\s+/g, " ").trim();
  const COMPOSER = /(viết|write|gửi|post|đăng)\s*(một\s*)?bình luận|bình luận bằng (file|ảnh|nhãn dán|biểu tượng|gif|sticker)|^(bình luận|comment)$/i;
  const safe = [...document.querySelectorAll('[role="button"], button')].filter((node) => {
    if (node.offsetParent === null) return false;
    if (node.tagName === "A") {
      const href = node.getAttribute("href") || "";
      if (href && href !== "#" && !href.startsWith("javascript:")) return false;
    }
    const t = text(node);
    return t && t.length < 60 && !COMPOSER.test(t);
  });
  const more = safe.filter((node) => /^(xem thêm|see more)$/i.test(text(node)));
  for (const node of more.sort(() => Math.random() - 0.5).slice(0, 4)) node.click();
}"""

_STEALTH = Stealth(
    navigator_languages=False,
    navigator_languages_override=None,
    navigator_platform=False,
    navigator_platform_override=None,
    webgl_vendor=False,
)

_LOCALE = "vi-VN"
_TIMEZONE = "Asia/Ho_Chi_Minh"
_ARGS = ['--disable-blink-features=AutomationControlled', f'--lang={_LOCALE}']

class PlaywrightSource:
    """Kết hợp dữ liệu phản hồi mạng và DOM trong cùng một phiên trình duyệt."""
    def __init__(self, profile: Path, headless: bool) -> None:
        self.profile = Path(profile)
        self.headless = headless
        self.queue: asyncio.Queue = asyncio.Queue()
        self._signal = asyncio.Event()
        self._playwright = None
        self.context = None
        self.page = None
        self.blocked = False
        self.feed_url = ""
        self.recovered_navigations = 0
        self.needs_login = False
        self._pending = 0

    async def start(self, url: str) -> None:
        from playwright.async_api import async_playwright

        self.profile.mkdir(parents=True, exist_ok=True)
        self._playwright = await async_playwright().start()
        self.context = await self._playwright.chromium.launch_persistent_context(
            str(self.profile),
            channel="chromium",
            headless=self.headless,
            viewport={"width": 1280, "height": 900},
            locale=_LOCALE,
            timezone_id=_TIMEZONE,
            args=_ARGS,
        )
        await _STEALTH.apply_stealth_async(self.context)
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        self.page.on("response", self._on_response)
        await self.page.goto(url, wait_until="domcontentloaded", timeout=45000)
        self.feed_url = self.page.url

    def _is_feed_page(self) -> bool:
        if self.page is None or not self.feed_url:
            return True
        try:
            here = _page_key(self.page.url)
            want = _page_key(self.feed_url)
        except Exception:
            return True
        return here == want

    async def ensure_feed(self) -> None:
        if self.page is None or not self.feed_url:
            return
        if self._is_feed_page() and not self._login_visible():
            self.needs_login = False
            return
        if self._login_visible():

            self.needs_login = True
            return
        try:
            self.recovered_navigations += 1
            if not self.page.url.startswith("about:blank"):
                try:
                    await self.page.go_back(wait_until="domcontentloaded", timeout=20000)
                    await self.page.wait_for_timeout(1500)
                    if self._is_feed_page() and not self._login_visible():
                        return
                except Exception:
                    pass
            await self.page.goto(self.feed_url, wait_until="domcontentloaded", timeout=45000)
            await self.page.wait_for_timeout(2500)
            if self._login_visible():
                self.needs_login = True
        except Exception:
            return

    def _login_visible(self) -> bool:
        if self.page is None:
            return False
        try:
            url = self.page.url or ""
        except Exception:
            return False
        if "/login" in url or "/checkpoint" in url:
            return True
        return False

    def _on_response(self, response: object) -> None:

        if self._pending >= 24:
            return
        self._pending += 1
        asyncio.create_task(self._read_response(response))

    async def _read_response(self, response: object) -> None:
        try:
            await self._read_response_inner(response)
        finally:
            self._pending -= 1

    async def _read_response_inner(self, response: object) -> None:
        try:
            if response.status == 429 or "/checkpoint/" in response.url:
                self.blocked = True
                return
            if response.status != 200:
                return
            if response.request.resource_type not in {"xhr", "fetch"}:
                return
            ctype = (response.headers.get("content-type") or "").lower()
            if not _maybe_json(ctype, response.url):
                return
            body = await response.body()
        except Exception:
            return

        try:
            posts, flag = await asyncio.to_thread(_extract_docs, body, response.url)
        except Exception:
            return
        if not posts and flag is None:
            return
        await self.queue.put((merge_posts(posts), flag))
        self._signal.set()

    async def session_hint(self) -> bool:
        if self.page is None:
            return False
        try:
            url = self.page.url
            selectors = [
                "[data-feed]",
                "[data-post-id]",
                "[role=feed]",
                "div[data-pagelet='GroupFeed']",
                "div[aria-label*='Feed']",
                "div[aria-label*='Bài viết']",
                "div[aria-label*='Posts']",
                "div[role=main] [role=article]",
            ]
            if "/groups/" in url:
                selectors = [
                    "[role=feed]",
                    "div[data-pagelet='GroupFeed']",
                    "div[aria-label*='Feed']",
                    "div[aria-label*='Bài viết']",
                    "div[aria-label*='Posts']",
                    "[data-feed]",
                    "[data-post-id]",
                    "div[role=main] [role=article]",
                    "[role=article]",
                ]
            selector_parts = []
            for s in selectors:
                safe = s.replace("\\", "\\\\").replace('"', '\\"')
                selector_parts.append(f'document.querySelector("{safe}")')
            selector = " || ".join(selector_parts)
            return bool(
                await self.page.evaluate(
                    """(sel) => {
                      const pw = document.querySelector("input[type=password]");
                      const loginVisible = !!(pw && pw.offsetParent !== null);
                      const feed = eval(sel);
                      return !loginVisible && !!feed;
                    }""",
                    selector,
                )
            )
        except Exception:
            return False

    async def prepare_round(self, timeout: float) -> bool:
        await self.ensure_feed()
        if self.page is not None and "/checkpoint/" in self.page.url:
            self.blocked = True
            return False
        if not self.queue.empty():
            return True
        self._signal.clear()
        if not self.queue.empty():
            return True
        try:
            await asyncio.wait_for(self._signal.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def expand(self) -> None:
        if self.page is None:
            return
        await self.ensure_feed()
        try:
            await self.page.evaluate(_EXPAND_JS)
        except Exception:
            return
        if not self._is_feed_page():
            await self.page.wait_for_timeout(1500)
            await self.ensure_feed()

    async def collect_batch(self) -> tuple[list[Post], bool | None]:
        await self.ensure_feed()
        posts: list[Post] = []
        flag: bool | None = None
        while not self.queue.empty():
            batch_posts, has_next = self.queue.get_nowait()
            posts.extend(batch_posts)
            if has_next is not None:
                flag = has_next
        posts.extend(await self._dom_posts())
        return merge_posts(posts), flag

    async def scroll(self) -> None:
        if self.page is None:
            return
        try:
            metrics = await self.page.evaluate(
                """() => {
                  const feed = document.querySelector("[data-feed]");
                  if (feed && feed.scrollHeight > feed.clientHeight + 10) {
                    return {feed: true, y: feed.scrollTop, h: feed.scrollHeight, vh: feed.clientHeight};
                  }
                  return {feed: false, y: window.scrollY, h: document.documentElement.scrollHeight, vh: window.innerHeight};
                }"""
            )
        except Exception:
            return
        remaining = metrics["h"] - metrics["y"] - metrics["vh"]
        if remaining <= 10:
            return
        if metrics["feed"]:
            delta = max(metrics["vh"] * 0.9, 400)
            await self._eval_scroll(metrics["feed"], metrics["y"] + delta)
            return
        steps = random.randint(2, 4)
        traveled = 0.0
        for index in range(steps):
            chunk = (remaining - traveled) / (steps - index)
            traveled += chunk * random.uniform(0.75, 1.0)
            await self._eval_scroll(False, metrics["y"] + min(traveled, remaining))
            await asyncio.sleep(random.uniform(0.35, 0.9))

            if not self.queue.empty():
                return
        if random.random() < 0.08:
            await asyncio.sleep(random.uniform(3.0, 7.0))

    async def _eval_scroll(self, is_feed: bool, top: float) -> None:
        if self.page is None:
            return
        try:
            if is_feed:
                await self.page.evaluate(
                    "(top) => { const feed = document.querySelector('[data-feed]'); if (feed) feed.scrollTo(0, top); }",
                    top,
                )
            else:
                await self.page.evaluate("(top) => window.scrollTo({top, behavior: 'smooth'})", top)
        except Exception:
            return

    async def fetch_text(self, url: str) -> str:
        if self.context is None:
            return ""
        page = await self.context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=15000)
            text = await page.evaluate(
                """() => {
                  const node = document.querySelector("[data-field=text], article");
                  return node ? node.innerText : "";
                }"""
            )
            return text or ""
        except Exception:
            return ""
        finally:
            await page.close()

    async def close(self) -> None:
        try:
            if self.context is not None:
                await self.context.close()
        except Exception:
            pass
        try:
            if self._playwright is not None:
                await self._playwright.stop()
        except Exception:
            pass

    async def _dom_posts(self) -> list[Post]:
        if self.page is None:
            return []
        try:
            await self.page.evaluate(
                """() => {
                  for (const img of document.querySelectorAll("[data-post-id] img[data-photo]")) {
                    img.scrollIntoView({block: "center"});
                  }
                }"""
            )
            raw = await self.page.evaluate(
                """() => {
                  const best = (img) => {
                    const srcset = img.getAttribute("srcset");
                    if (srcset) {
                      let bestUrl = "";
                      let bestW = -1;
                      for (const part of srcset.split(",")) {
                        const bits = part.trim().split(/\\s+/);
                        if (!bits[0]) continue;
                        const width = bits[1] && bits[1].endsWith("w") ? parseInt(bits[1], 10) : 0;
                        if (width >= bestW) {
                          bestW = width;
                          bestUrl = bits[0];
                        }
                      }
                      if (bestUrl) return bestUrl;
                    }
                    return img.currentSrc || img.src || img.getAttribute("data-src") || img.getAttribute("src") || "";
                  };
                  return [...document.querySelectorAll("[data-post-id]")].map((node) => ({
                    post_id: node.getAttribute("data-post-id") || "",
                    author: (node.querySelector("[data-field=author]") || {}).innerText || "",
                    created_at: (node.querySelector("time") || {}).getAttribute?.("datetime") || "",
                    time_label: (node.querySelector("[data-field=time]") || {}).innerText || "",
                    like_count: (node.querySelector("[data-field=likes]") || {}).innerText || "0",
                    comment_count: (node.querySelector("[data-field=comments]") || {}).innerText || "0",
                    text: (node.querySelector("[data-field=text]") || {}).innerText || "",
                    image_urls: [...node.querySelectorAll("img[data-photo]")].map(best).filter(Boolean),
                    permalink: (node.querySelector("a[data-permalink]") || {}).href || ""
                  }));
                }"""
            )
        except Exception:
            return []
        posts: list[Post] = []
        base = self.page.url if self.page is not None else ""
        for item in raw or []:
            post_id = str(item.get("post_id") or "").strip()
            if not post_id:
                continue
            created = str(item.get("created_at") or "").strip() or None
            label = str(item.get("time_label") or "").strip() or None
            posts.append(
                Post(
                    post_id=post_id,
                    author=str(item.get("author") or "").strip(),
                    created_at=created,
                    time_label=label,
                    like_count=parse_count(item.get("like_count")),
                    comment_count=parse_count(item.get("comment_count")),
                    text=str(item.get("text") or "").strip(),
                    image_urls=_unique_images(item.get("image_urls") or [], base),
                    permalink=_abs_or_none(str(item.get("permalink") or ""), base),
                )
            )
        return posts

def _extract_docs(body: bytes, base_url: str) -> tuple[list[Post], bool | None]:
    docs = parse_json_payloads(body)
    if not docs:
        return [], None
    posts: list[Post] = []
    flag: bool | None = None
    for doc in docs:
        found = extract_posts(doc, base_url=base_url)
        if not found:
            continue
        posts.extend(found)
        has_next = cursor_has_next(doc)
        if has_next is True:
            flag = True
        elif has_next is False and flag is None:
            flag = False
    return posts, flag

def _maybe_json(ctype: str, url: str) -> bool:
    if "json" in ctype:
        return True
    if "/api/graphql" in url:
        return True
    if "x-javascript" in ctype:
        return True
    return False

def _page_key(url: str) -> str:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return url
    path = parsed.path.rstrip("/") or "/"
    return f"{parsed.scheme}://{parsed.netloc}{path}"

def _unique_images(urls: list, base: str) -> list[str]:
    clean: list[str] = []
    seen: set[str] = set()
    for url in urls:
        absolute = urljoin(base, str(url)) if url else ""
        if not absolute or not acceptable_image(absolute) or absolute in seen:
            continue
        seen.add(absolute)
        clean.append(absolute)
    return clean

def _abs_or_none(url: str, base: str) -> str | None:
    text = url.strip()
    if not text:
        return None
    return urljoin(base, text)


MODAL = '[role="dialog"][aria-modal="true"]'

SCOPE = '[data-pc-scope="1"]'

SORT_CURRENT = ("Phù hợp nhất", "Mới nhất", "Tất cả bình luận", "Most relevant", "All comments")

_MARK = "Ẩn hoặc báo cáo bình luận này"

_HIDDEN = re.compile(r"bình luận này đã bị ẩn|comment này đã bị ẩn|nội dung này đã bị ẩn", re.I)

def _is_marked_js() -> str:
    return (
        "el => /"
        + _MARK
        + "/.test([...el.querySelectorAll('[aria-label]')]"
        ".map(x => x.getAttribute('aria-label') || '').join('|'))"
    )

async def page_wait(page: Any, ms: int) -> None:
    if page is None:
        await asyncio.sleep(ms / 1000)
        return
    try:
        await page.wait_for_timeout(ms)
    except Exception:
        await asyncio.sleep(ms / 1000)

_SORT_LABEL_JS = """(sel) => {
  const scope = document.querySelector(sel);
  const read = (root) => [...root.querySelectorAll('[role="button"]')]
    .map((b) => (b.innerText || '').trim())
    .filter((t) => /Phù hợp nhất|Tất cả bình luận|Mới nhất/.test(t));
  if (scope) {
    const hit = read(scope);
    if (hit.length) return hit[0];
  }
  const all = read(document);
  return all.length ? all[0] : '';
}"""

_SCOPE_JS = """() => {
  document.querySelectorAll('[data-pc-scope]').forEach((n) => n.removeAttribute('data-pc-scope'));
  const modal = document.querySelector('[role="dialog"][aria-modal="true"]');
  if (modal) {
    modal.setAttribute('data-pc-scope', '1');
    return {layout: 'modal', n: -1};
  }
  const marked = %s;
  const all = [...document.querySelectorAll('[role="article"]')].filter(marked);
  const leaves = all.filter((a) => ![...a.querySelectorAll('[role="article"]')].some(marked));
  if (leaves.length < 2) return {layout: '', n: leaves.length};
  let node = leaves[0];
  while (
    node.parentElement &&
    node.parentElement !== document.documentElement &&
    !leaves.every((l) => node.contains(l))
  ) {
    node = node.parentElement;
  }
  node.setAttribute('data-pc-scope', '1');
  return {layout: 'page', n: leaves.length};
}"""

_LIST_JS = """(sel) => {
  const dlg = document.querySelector(sel);
  if (!dlg) return {error: 'khong thay modal'};
  const marked = %s;
  const all = [...dlg.querySelectorAll('[role="article"]')].filter(marked);
  const leaves = all.filter(a => ![...a.querySelectorAll('[role="article"]')].some(marked));
  const dlgBox = dlg.getBoundingClientRect();

  const pickId = (a) => {
    for (const link of a.querySelectorAll('a[href]')) {
      const href = link.getAttribute('href') || '';
      if (href.includes('comment_id=')) {
        const clean = href.replace(/&__cft__.*/, '');
        try {
          return new URL(clean, location.origin).searchParams.get('comment_id') || '';
        } catch (e) { return ''; }
      }
    }
    return '';
  };

  const rows = leaves.map((a) => {
    const ariaAll = [...a.querySelectorAll('[aria-label]')].map(n => n.getAttribute('aria-label') || '');
    let when = ariaAll.find(l => / lúc \\d{1,2}:\\d{2}$/.test(l)) || '';
    let likes = 0;
    for (const l of ariaAll) {
      const m = l.match(/^(\\d+)\\s*cảm xúc/);
      if (m) { likes = parseInt(m[1], 10); break; }
    }
    const links = [...a.querySelectorAll('a[href]')];
    const timeLink = links.find(l => (l.getAttribute('href') || '').includes('comment_id='));
    const timeLabel = timeLink ? (timeLink.innerText || '').trim() : '';
    const authorLink = links.find(l => l !== timeLink && (l.innerText || '').trim());
    const author = authorLink ? (authorLink.innerText || '').trim() : '';
    if (!when) when = timeLabel;
    const clone = a.cloneNode(true);
    clone.querySelectorAll('[role="button"]').forEach(n => n.remove());
    clone.querySelectorAll('a[href]').forEach((l) => {
      const href = l.getAttribute('href') || '';
      const label = (l.innerText || '').trim();
      if (href.includes('comment_id=') || (author && label === author)) l.remove();
    });
    clone.querySelectorAll('[aria-label]').forEach(n => {
      const l = n.getAttribute('aria-label') || '';
      if (/ lúc \\d{1,2}:\\d{2}$/.test(l) || l.includes('báo cáo bình luận')) n.remove();
    });
    let text = (clone.innerText || '').replace(/\\s+/g, ' ').trim();

    const hidden = /bình luận này đã bị ẩn/i.test(a.innerText || '');
    const box = a.getBoundingClientRect();
    return {
      comment_id: pickId(a),
      author, text, created_at: when, like_count: likes, hidden,
      left: Math.round(box.left - dlgBox.left),
      has_replies: /^Xem \\d+ (câu trả lời|phản hồi)/i.test(a.innerText || ''),
      order: all.indexOf(a),
    };
  });
  return { rows, total_articles: all.length };
}"""

_BOTTOM_JS = """(sel) => {
  const scope = document.querySelector(sel);
  if (!scope) return true;
  let node = scope;
  while (node && node !== document.body) {
    if (node.scrollHeight > node.clientHeight + 40) {
      return node.scrollTop + node.clientHeight >= node.scrollHeight - 80;
    }
    node = node.parentElement;
  }
  const doc = document.scrollingElement || document.documentElement;
  return doc.scrollTop + doc.clientHeight >= doc.scrollHeight - 80;
}"""

_HAS_REPLY_BTN_JS = """(sel) => {
  const scope = document.querySelector(sel);
  if (!scope) return false;
  return [...scope.querySelectorAll('[role="button"]')].some(
    (b) => /^Xem \\d+ (câu trả lời|phản hồi)/i.test((b.innerText || '').trim()));
}"""

_OPEN_ALL_REPLIES_JS = """(sel) => {
  const dlg = document.querySelector(sel);
  if (!dlg) return 0;
  const btns = [...dlg.querySelectorAll('[role="button"]')]
    .filter((b) => /^Xem \\d+ (câu trả lời|phản hồi)/i.test((b.innerText || '').trim()));
  let n = 0;
  for (const b of btns) {
    try { b.click(); n++; } catch (e) {  }
  }
  return n;
}"""

class CommentCrawler:
    """Đọc bình luận trong tab riêng; gộp các vòng để giữ phần DOM đã bị gỡ."""

    def __init__(self, source: Any, *, max_rounds: int = 40) -> None:
        self.source = source
        self.max_rounds = max_rounds
        self._page: Any = None
        self.layout = ""
        self.scope_comments = 0

    @property
    def page(self) -> Any:
        return self._page

    async def _ensure_page(self) -> Any:
        if self._page is not None:
            return self._page
        context = getattr(self.source, "context", None)
        if context is None:
            self._page = getattr(self.source, "page", None)
        else:
            self._page = await context.new_page()
        return self._page

    async def close(self) -> None:
        if self._page is not None and self._page is not getattr(self.source, "page", None):
            try:
                await self._page.close()
            except Exception:
                pass
        self._page = None

    async def _mark_scope(self) -> str:
        page = self.page
        if page is None:
            return ""
        try:
            info = await page.evaluate(_SCOPE_JS % _is_marked_js())
        except Exception:
            return ""
        layout = str((info or {}).get("layout") or "")
        self.scope_comments = int((info or {}).get("n") or 0)
        if layout:
            self.layout = layout
        return layout

    async def open_post(self, permalink: str) -> bool:
        page = await self._ensure_page()
        if page is None:
            return False
        await page.goto(permalink, wait_until="domcontentloaded", timeout=60000)

        ready = False
        for _ in range(40):
            if await self._mark_scope():
                try:
                    await page.locator(SCOPE).wait_for(state="visible", timeout=700)
                    ready = True
                    break
                except Exception:
                    pass
            await self._pause(0.4, 0.8)
        if not ready:
            return False

        await self._switch_to_all_comments()

        await self._wait_sort_changed()

        await self._mark_scope()
        await self._focus_scope()
        return True

    async def _wait_sort_changed(self, timeout: float = 12.0) -> None:
        page = self.page
        if page is None:
            return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                text = await page.evaluate(_SORT_LABEL_JS, SCOPE)
            except Exception:
                text = ""
            if "Tất cả bình luận" in (text or ""):

                await self._pause(0.6, 1.2)
                return
            await self._pause(0.4, 0.9)

    async def _switch_to_all_comments(self) -> None:
        page = self.page
        if page is None:
            return
        try:
            label = await page.locator(SCOPE).get_by_text(SORT_CURRENT[0], exact=True).first.inner_text(timeout=6000)
        except Exception:

            try:
                label = await page.get_by_text(SORT_CURRENT[0], exact=True).first.inner_text(timeout=6000)
            except Exception:
                return
        if "Tất cả bình luận" in label:
            return
        try:
            try:
                await page.locator(SCOPE).get_by_text(SORT_CURRENT[0], exact=True).first.click(timeout=10000)
            except Exception:
                await page.get_by_text(SORT_CURRENT[0], exact=True).first.click(timeout=10000)
            await self._pause(0.8, 1.6)
            await page.get_by_text("Tất cả bình luận", exact=True).first.click(timeout=10000)
            await self._pause(1.5, 2.5)
        except Exception:
            return

    async def _focus_scope(self) -> None:
        page = self.page
        if page is None:
            return
        try:
            box = await page.locator(SCOPE).bounding_box(timeout=8000)
        except Exception:
            return
        if not box:
            return
        size = page.viewport_size or {"width": 1280, "height": 900}
        x = box["x"] + box["width"] / 2
        y = box["y"] + box["height"] / 2
        x = max(12, min(x, size["width"] - 12))
        y = max(12, min(y, size["height"] - 12))
        await page.mouse.move(x, y)

    async def _rows(self) -> list[dict]:
        page = self.page
        if page is None:
            return []
        try:
            data = await page.evaluate(_LIST_JS % _is_marked_js(), SCOPE)
        except Exception:
            return []
        rows = data.get("rows") or []
        return rows

    async def _scroll(self, steps: int = 6) -> None:
        page = self.page
        if page is None:
            return
        for index in range(steps):
            if index and await self._at_bottom():
                return
            await page.mouse.wheel(0, random.randint(560, 700))
            await page.wait_for_timeout(random.randint(240, 340))

    async def _open_all_replies(self) -> int:
        page = self.page
        if page is None:
            return 0
        try:
            clicked = int(await page.evaluate(_OPEN_ALL_REPLIES_JS, SCOPE))
        except Exception:
            return 0
        if clicked:
            await page.wait_for_timeout(random.randint(1200, 1600))
        return clicked

    async def _has_collapsed_replies(self) -> bool:
        page = self.page
        if page is None:
            return False
        try:
            return bool(await page.evaluate(_HAS_REPLY_BTN_JS, SCOPE))
        except Exception:
            return False

    async def _at_bottom(self) -> bool:
        page = self.page
        if page is None:
            return True
        try:
            return bool(await page.evaluate(_BOTTOM_JS, SCOPE))
        except Exception:
            return True

    async def collect(
        self,
        target: int = 0,
        on_progress: Callable[[int], None] | None = None,
        budget: float = 60.0,
    ) -> list[Comment]:
        last_count = -1
        idle = 0
        reported = -1
        merged: dict[str, dict] = {}
        order = {"n": 0}
        deadline = time.monotonic() + budget

        def absorb(rows: list[dict]) -> int:
            for row in rows:
                cid = row.get("comment_id") or ""
                if not cid:
                    continue
                seen = merged.get(cid)
                if seen is None:
                    item = dict(row)
                    item["order"] = order["n"]
                    order["n"] += 1
                    merged[cid] = item
                else:
                    if (row.get("text") or "") and not (seen.get("text") or ""):
                        seen["text"] = row["text"]
                    if not (seen.get("author") or "") and (row.get("author") or ""):
                        seen["author"] = row["author"]
                    if not (seen.get("created_at") or "") and (row.get("created_at") or ""):
                        seen["created_at"] = row["created_at"]

        for _ in range(self.max_rounds):

            if self.page is not None and await self.page.locator(SCOPE).count() == 0:
                await self._mark_scope()
                await self._focus_scope()
            await self._scroll()
            absorb(await self._rows())
            if on_progress is not None and len(merged) != reported:
                reported = len(merged)
                on_progress(reported)

            await self._open_all_replies()

            total = len(merged)
            if target and total >= target:
                break
            if total > last_count:
                last_count = total
                idle = 0

                if await self._at_bottom() and not await self._has_collapsed_replies():
                    break
                continue
            if await self._at_bottom():

                await self._pause(1.2, 2.4)
            idle += 1

            if idle >= 3:
                break
            if time.monotonic() >= deadline:
                break

        absorb(await self._rows())
        return self._to_comments(list(merged.values()))

    def _to_comments(self, rows: list[dict]) -> list[Comment]:
        rows = sorted(rows, key=lambda r: int(r.get("order") or 0))
        seen: set[str] = set()
        ordered: list[tuple[dict, str | None, int]] = []

        levels: list[int] = []
        for row in rows:
            left = int(row.get("left") or 0)
            if left not in levels:
                levels.append(left)
        levels.sort()

        last_at: dict[int, str] = {}
        for row in rows:
            cid = row.get("comment_id") or ""
            if not cid or cid in seen:
                continue
            left = int(row.get("left") or 0)
            level = levels.index(left)
            parent = last_at.get(level - 1) if level > 0 else None
            if parent is None and level > 0:

                for deeper in range(level - 1, -1, -1):
                    if deeper in last_at:
                        parent = last_at[deeper]
                        break
            seen.add(cid)
            last_at[level] = cid
            ordered.append((row, parent, level))

        out: list[Comment] = []
        for row, parent, depth in ordered:
            if row.get("hidden"):
                continue

            text = (row.get("text") or "").strip().lstrip("·•-–— ").strip()
            out.append(
                Comment(
                    comment_id=row["comment_id"],
                    post_id="",
                    parent_id=parent,
                    depth=depth,
                    author=(row.get("author") or "").strip(),
                    text=text,
                    created_at=(row.get("created_at") or "").strip(),
                    like_count=int(row.get("like_count") or 0),
                )
            )
        return out

    async def _pause(self, low: float, high: float) -> None:
        await asyncio.sleep(random.uniform(low, high))

GAP_OK = (2.0, 4.0)
GAP_SUSPECT = (8.0, 14.0)
GAP_BAD = (20.0, 35.0)

async def crawl_comments(
    source: Any,
    store: Any,
    *,
    collection_id: int,
    min_comments: int = 2,
    limit: int | None = None,
    retries: int = 2,
    idle_timeout: float = 8.0,
    stop: Callable[[], bool] | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> int:
    pending = store.list_posts_needing_comments(collection_id, min_comments=min_comments, limit=limit)
    if not pending:
        return 0

    crawler = CommentCrawler(source)
    total = len(pending)
    done = 0
    if on_progress:

        approx = total * 25 / 60
        het = f" (ước khoảng {approx:.0f} phút)" if approx >= 1 else ""
        on_progress(f"còn {total} bài chưa lấy bình luận, sẽ xử lý hết{het}. "
                    f"Dừng giữa chừng thì chạy lại tiếp tục từ bài kế tiếp.")

    for index, post in enumerate(pending, start=1):
        if source.blocked:
            if on_progress:
                on_progress("Facebook đang chặn — dừng lại. Thử lại sau.")
            break
        if stop is not None and stop():
            if on_progress:
                on_progress("đã dừng theo yêu cầu — chạy lại sẽ tiếp tục từ bài kế tiếp")
            break
        if on_progress:
            on_progress(
                f"[{index}/{total}] mở bài của {post['author'][:30]} "
                f"({post['comment_count']} bình luận)"
            )
        permalink = post["permalink"]
        target = int(post["comment_count"] or 0)
        items: list[Comment] = []
        merged: dict[str, Comment] = {}
        gained = 0
        failure = ""
        attempts = max(1, retries + 1)

        for attempt in range(1, attempts + 1):
            try:
                opened = await crawler.open_post(permalink)
            except Exception as exc:
                opened = False
                if on_progress:
                    on_progress(f"  lỗi mở bài: {exc}")
            if not opened:
                failure = "không mở được trang bài"
                await crawler.close()
                if attempt < attempts:
                    await asyncio.sleep(random.uniform(1.5, 3.0))
                continue

            seen = {"n": 0}

            def report(count: int) -> None:
                seen["n"] = count
                if on_progress:
                    on_progress(f"  đang lấy bình luận: {count}")

            try:
                items = await crawler.collect(target=target, on_progress=report)
                failure = ""
            except Exception as exc:
                failure = str(exc)
                if on_progress:
                    on_progress(f"  lỗi lấy bình luận: {exc}")
                await crawler.close()
                if attempt < attempts:
                    await asyncio.sleep(random.uniform(1.5, 3.0))
                continue

            before = len(merged)
            for item in items:
                merged.setdefault(item.comment_id, item)
            gained = len(merged) - before
            if not target or len(merged) >= target:
                break

            if gained == 0:
                if on_progress:
                    on_progress(
                        f"  lấy được {len(merged)}/{target} — Facebook không hiện thêm, "
                        "coi như đủ"
                    )
                break
            if attempt < attempts:
                if on_progress:
                    on_progress(
                        f"  mới lấy {len(merged)}/{target} (+{gained}) — tải lại trang bài thử lại"
                    )
                await crawler.close()
                await asyncio.sleep(random.uniform(1.5, 3.0))

        items = list(merged.values())
        if failure:
            if items:
                store.save_comments(collection_id, post["post_id"], items)
            store.mark_comments_error(collection_id, post["post_id"], failure)
            if on_progress:
                on_progress(f"  chưa hoàn tất: {failure} — có thể chạy lại")
        else:
            saved = store.save_comments(collection_id, post["post_id"], items)
            done += 1
            if on_progress:
                reply_n = sum(1 for c in items if c.depth > 0)
                on_progress(f"  xong bài này: lưu {saved} bình luận ({reply_n} trả lời)")

        if source.blocked:
            if on_progress:
                on_progress("Facebook đang phản ứng — nghỉ lâu trước khi tiếp tục")
            gap = GAP_BAD
        elif gained > 0 or not target:
            gap = GAP_OK
        else:
            gap = GAP_SUSPECT
        await crawler.close()
        await asyncio.sleep(random.uniform(*gap))

    await crawler.close()
    if on_progress:
        on_progress(f"xong {done}/{total} bài trong collection")
    return done


class FeedSource(Protocol):
    async def prepare_round(self, timeout: float) -> bool: ...

    async def expand(self) -> None: ...

    async def collect_batch(self) -> tuple[list[Post], bool | None]: ...

    async def scroll(self) -> None: ...

    async def fetch_text(self, url: str) -> str: ...

async def crawl_feed(
    source: FeedSource,
    store: Store,
    state: AppState,
    *,
    idle_timeout: float = 8.0,
    stale_limit: int = 3,
) -> None:
    seen: set[str] = set()
    stale = 0
    state.set_status("running")
    while not state.stop_event.is_set():
        if getattr(source, "blocked", False):
            state.stop_reason = "rate_limited"
            state.set_status("done")
            state.log("bị giới hạn tốc độ — nghỉ một lát rồi chạy lại")
            return
        if getattr(source, "needs_login", False):
            _need_login(state)
            return
        ready = await source.prepare_round(idle_timeout)
        if getattr(source, "needs_login", False):
            _need_login(state)
            return
        if state.stop_event.is_set():
            break
        if not ready:
            state.set_status("stalled")
        await source.expand()
        posts, has_next = await source.collect_batch()
        state.rounds += 1
        fresh: list[Post] = []
        for post in posts:
            if post.post_id in seen:
                continue
            seen.add(post.post_id)
            fresh.append(post)
        if fresh:
            stale = 0
            state.set_status("running")
        else:
            stale += 1
        for post in fresh:
            if not post.text and post.permalink:
                filled = (await source.fetch_text(post.permalink)).strip()
                if filled:
                    post.text = filled
            _persist(store, state, post)
        state.touch_run(store)
        if has_next is False:
            state.stop_reason = "cursor_empty"
            state.set_status("done")
            return
        if stale >= stale_limit:
            state.stop_reason = "no_new_posts"
            state.set_status("done")
            return
        await source.scroll()
        await asyncio.sleep(0.2)
    state.stop_reason = state.stop_reason or "user_stop"
    state.set_status("done")

def _need_login(state: AppState) -> None:
    state.stop_reason = "login_required"
    state.set_status("waiting_login")
    state.log("Facebook đưa ra trang đăng nhập — đăng nhập lại rồi chạy tiếp")

def _persist(store: Store, state: AppState, post: Post) -> None:
    existing = store.get_post(post.post_id, collection_id=state.collection_id)
    digest = content_hash(post)
    if existing is not None and (not state.refresh or existing["content_hash"] == digest):
        state.skip_count += 1
        state.emit({"type": "post", "post": _public(existing), "skipped": True, "updated": False})
        state.log(f"skip {post.post_id}")
        return
    saved = store.upsert(state.page_url, post, collection_id=state.collection_id)
    updated = existing is not None
    if updated:
        state.updated_count += 1
    else:
        state.new_count += 1
    state.emit({"type": "post", "post": _public(saved), "skipped": False, "updated": updated})
    state.log(("cập nhật " if updated else "lưu ") + post.post_id)

def _public(row: dict) -> dict:
    keys = ("post_id", "author", "created_at", "time_label", "like_count",
            "comment_count", "text", "image_urls")
    return {**{key: row[key] for key in keys}, "permalink": row.get("permalink"),
            "collection_id": row.get("collection_id", 1)}
