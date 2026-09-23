"""TikTok Shop 榜单采集核心模块。

职责：

* 浏览器控制（Playwright / Chromium，正常浏览模式）
* 页面访问、等待、拟人滚动
* 定位并点击『查看更多 / View More』
* 抓取商品卡片并交给 :mod:`page_parser` 解析

定位策略刻意避开固定的 CSS 选择器：商品锚点是**含有商品链接语义的 a 标签**，
卡片边界由**结构关系**（最近的、含图片且文本量合理的祖先节点）推断，
按钮则完全依靠**可见文本**匹配。
"""

from __future__ import annotations

import json
import logging
logging.getLogger('PIL').setLevel(logging.WARNING)
import random
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Locator,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

import config
from config import TIMING, resolve_category
from page_parser import (
    assign_ranks,
    build_product_record,
    dedupe_records,
    extract_category_from_pdp,
    extract_product_description,
    extract_product_image_from_pdp,
    is_meaningful,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------
class ScraperError(RuntimeError):
    """采集过程中的可预期错误（会被 main.py 捕获并给出友好提示）。"""


class PageOpenError(ScraperError):
    """页面打不开 / 超时 / 返回错误状态码。"""


class NoProductFoundError(ScraperError):
    """页面打开了，但一个商品都没解析出来。"""


class SecurityCheckError(ScraperError):
    """页面被 TikTok 的安全验证（滑块 / 人机校验）拦截。"""


class StopRequested(ScraperError):
    """用户请求终止抓取（GUI 点击『停止』时抛出）。"""


# ---------------------------------------------------------------------------
# 注入脚本：弱化自动化特征
# ---------------------------------------------------------------------------
STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
window.chrome = window.chrome || { runtime: {} };
"""

# ---------------------------------------------------------------------------
# 页面内脚本
# ---------------------------------------------------------------------------
#: 收集商品卡片：先定位"畅销商品"栏目容器（按标题文本 + 商品链接数判定），
#: 只在该容器内收集；定位失败时回退全页收集（保证不会抓空）。
#: 参数 args.sectionTitles：栏目标题文本列表（空 = 全页收集，兼容榜单页模式）。
JS_COLLECT_CARDS = r"""
(args) => {
  const SECTION_TITLES = (args && args.sectionTitles || []).map(s => (s + '').replace(/\s+/g, ' ').trim().toLowerCase());
  const PRODUCT_RE = /\/pdp\/|\/product|\/view\/product|\/goods\/|\/p\/\d/i;
  const out = [];
  const seen = new Set();

  const abs = (u) => { try { return new URL(u, location.href).href; } catch (e) { return ''; } };
  const txt = (el) => ((el.innerText || el.textContent || '') + '').trim();
  const norm = (s) => ((s || '') + '').replace(/\s+/g, ' ').trim().toLowerCase();

  const isProductLink = (a) => a && PRODUCT_RE.test(a.getAttribute('href') || '');
  const linkCountIn = (root) => {
    if (!root) return 0;
    let n = 0;
    for (const a of root.querySelectorAll('a[href]')) {
      if (isProductLink(a)) n++;
      if (n >= 4) break;
    }
    return n;
  };

  // 定位"畅销商品"栏目内容容器
  const findSection = () => {
    if (!SECTION_TITLES.length) return null;
    for (const el of Array.from(document.querySelectorAll('h1,h2,h3,h4,div,span,p'))) {
      if (el.children.length > 3) continue;
      const t = norm(txt(el));
      if (!t || SECTION_TITLES.indexOf(t) < 0) continue;
      // 1) 标题的下一个兄弟节点（首页实测：h2「畅销商品」-> div.w-full 商品区）
      let sib = el.nextElementSibling;
      for (let d = 0; sib && d < 3; d++) {
        if (linkCountIn(sib) >= 4) return sib;
        sib = sib.nextElementSibling;
      }
      // 2) 兜底：向上找包含商品链接的祖先容器
      let node = el;
      for (let i = 0; i < 6 && node.parentElement; i++) {
        node = node.parentElement;
        if (linkCountIn(node) >= 4) return node;
      }
    }
    return null;
  };

  const section = findSection();
  const roots = section ? [section] : [document];

  const cardOf = (start) => {
    let node = start;
    let best = null;
    for (let i = 0; i < 8 && node && node.parentElement; i++) {
      node = node.parentElement;
      const t = txt(node);
      const imgs = node.querySelectorAll('img').length;
      const links = node.querySelectorAll('a[href]').length;
      const ok = imgs >= 1 && imgs <= 4 && t.length >= 12 && t.length <= 1200 && links <= 4;
      if (ok) { best = node; } else if (best) { break; }
    }
    return best || start;
  };

  const push = (el, href) => {
    if (!el) return;
    const url = href ? abs(href) : '';
    const key = (url || txt(el).slice(0, 200));
    if (!key || seen.has(key)) return;
    seen.add(key);
    out.push({
      url: url,
      html: (el.outerHTML || '').slice(0, 20000),
      text: txt(el).slice(0, 1200)
    });
  };

  for (const root of roots) {
    const anchors = Array.from(root.querySelectorAll('a[href]'))
      .filter(a => isProductLink(a));

    for (const a of anchors) {
      push(cardOf(a), a.getAttribute('href') || '');
    }

    // 兜底：某些版本卡片外层不是链接，只有图片 + 点击事件
    if (out.length < 2) {
      for (const img of Array.from(root.querySelectorAll('img'))) {
        const r = img.getBoundingClientRect();
        if (r.width < 40 || r.height < 40) continue;
        const card = cardOf(img);
        const a = card.querySelector('a[href]');
        push(card, a ? a.getAttribute('href') : '');
      }
    }
  }

  return out;
}
"""

#: 页面计数：商品锚点数 / 有效图片数 / 页面高度（用于判断是否有新内容加载）。
#: 与 JS_COLLECT_CARDS 一样支持按"畅销商品"栏目限定统计。
JS_PAGE_STATS = r"""
(args) => {
  const SECTION_TITLES = (args && args.sectionTitles || []).map(s => (s + '').replace(/\s+/g, ' ').trim().toLowerCase());
  const PRODUCT_RE = /\/pdp\/|\/product|\/view\/product|\/goods\/|\/p\/\d/i;

  const txt = (el) => ((el.innerText || el.textContent || '') + '').trim();
  const norm = (s) => ((s || '') + '').replace(/\s+/g, ' ').trim().toLowerCase();
  const isProductLink = (a) => a && PRODUCT_RE.test(a.getAttribute('href') || '');
  const linkCountIn = (root) => {
    if (!root) return 0;
    let n = 0;
    for (const a of root.querySelectorAll('a[href]')) {
      if (isProductLink(a)) n++;
      if (n >= 4) break;
    }
    return n;
  };

  const findSection = () => {
    if (!SECTION_TITLES.length) return null;
    for (const el of Array.from(document.querySelectorAll('h1,h2,h3,h4,div,span,p'))) {
      if (el.children.length > 3) continue;
      const t = norm(txt(el));
      if (!t || SECTION_TITLES.indexOf(t) < 0) continue;
      let sib = el.nextElementSibling;
      for (let d = 0; sib && d < 3; d++) {
        if (linkCountIn(sib) >= 4) return sib;
        sib = sib.nextElementSibling;
      }
      let node = el;
      for (let i = 0; i < 6 && node.parentElement; i++) {
        node = node.parentElement;
        if (linkCountIn(node) >= 4) return node;
      }
    }
    return null;
  };

  const root = findSection() || document;

  let anchors = 0;
  for (const a of root.querySelectorAll('a[href]')) {
    if (isProductLink(a)) anchors++;
  }
  let imgs = 0;
  for (const img of root.querySelectorAll('img')) {
    const r = img.getBoundingClientRect();
    if (r.width >= 40 && r.height >= 40) imgs++;
  }
  const doc = document.scrollingElement || document.documentElement;
  return { anchors: anchors, imgs: imgs, height: doc ? doc.scrollHeight : 0 };
}
"""

#: 按可见文本定位可点击元素，并打上 data-tts-pick 标记（随后用真实鼠标点击）。
#: 支持 args.sectionTitles：非空时先在栏目容器内查找（如"畅销商品"的"查看更多"），
#: 并且只认**视口内**可见的元素——避免点到其它栏目同名的按钮（例如首页
#: Recommended for you 的"查看更多"会跳转页面，不能点）。
JS_MARK_BY_TEXT = r"""
(args) => {
  const patterns = (args.patterns || []).map(p => ((p || '') + '').toLowerCase());
  const selector = args.selector || 'button, a, [role="button"], [role="link"], div, span, p';
  const maxTextLen = args.maxTextLen || 40;
  const maxArea = args.maxArea || 0;
  const exact = !!args.exact;
  const SECTION_TITLES = (args.sectionTitles || []).map(s => (s + '').replace(/\s+/g, ' ').trim().toLowerCase());
  const norm = (s) => ((s || '') + '').replace(/\s+/g, ' ').trim().toLowerCase();
  const vh = window.innerHeight || 0;

  for (const el of Array.from(document.querySelectorAll('[data-tts-pick="1"]'))) {
    el.removeAttribute('data-tts-pick');
  }

  const isProductLink = (a) => a && /\/pdp\/|\/product|\/view\/product|\/goods\/|\/p\/\d/i.test(a.getAttribute('href') || '');
  const linkCountIn = (root) => {
    if (!root) return 0;
    let n = 0;
    for (const a of root.querySelectorAll('a[href]')) {
      if (isProductLink(a)) n++;
      if (n >= 4) break;
    }
    return n;
  };
  const hasMatchText = (root) => {
    if (!root) return false;
    for (const el of Array.from(root.querySelectorAll(selector))) {
      const t = norm(el.innerText || el.textContent);
      if (!t || t.length > maxTextLen) continue;
      if (exact ? patterns.some(p => t === p) : patterns.some(p => t === p || t.indexOf(p) !== -1)) return true;
    }
    return false;
  };

  // 定位栏目容器（如"畅销商品"）：优先向上找同时含目标文本与商品链接的祖先，
  // 兜底用标题的下一个兄弟（商品网格）。
  let section = null;
  if (SECTION_TITLES.length) {
    for (const el of Array.from(document.querySelectorAll('h1,h2,h3,h4,div,span,p'))) {
      if (el.children.length > 3) continue;
      const t = norm(el.innerText || el.textContent);
      if (!t || SECTION_TITLES.indexOf(t) < 0) continue;
      let node = el;
      for (let i = 0; i < 8 && node.parentElement; i++) {
        node = node.parentElement;
        if (hasMatchText(node) && linkCountIn(node) >= 4) { section = node; break; }
      }
      if (!section) {
        let sib = el.nextElementSibling;
        for (let d = 0; sib && d < 3; d++) {
          if (linkCountIn(sib) >= 4) { section = sib; break; }
          sib = sib.nextElementSibling;
        }
      }
      if (section) break;
    }
  }

  let best = null, bestArea = 0, bestDepth = -1;
  for (const el of Array.from(document.querySelectorAll(selector))) {
    if (section && !section.contains(el)) continue;
    const t = norm(el.innerText || el.textContent);
    if (!t || t.length > maxTextLen) continue;
    const matched = exact ? patterns.some(p => t === p)
                          : patterns.some(p => t === p || t.indexOf(p) !== -1);
    if (!matched) continue;

    const r = el.getBoundingClientRect();
    if (r.width < 12 || r.height < 12) continue;
    // 只认视口内可见的元素（避免点到其它栏目滚出屏的"查看更多"）
    if (!(r.bottom > 0 && r.top < vh)) continue;
    const st = window.getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none') continue;
    if (parseFloat(st.opacity || '1') < 0.1) continue;
    if (el.closest('[aria-hidden="true"]')) continue;

    const area = r.width * r.height;
    if (maxArea && area > maxArea) continue;

    let depth = 0, p = el;
    while (p && p.parentElement) { p = p.parentElement; depth++; }

    if (best === null || area < bestArea || (area === bestArea && depth > bestDepth)) {
      best = el; bestArea = area; bestDepth = depth;
    }
  }

  if (!best) return null;
  best.setAttribute('data-tts-pick', '1');
  try { best.scrollIntoView({ block: 'center', inline: 'center' }); } catch (e) {}
  return norm(best.innerText || best.textContent);
}
"""

#: 检测"真正可见"的安全验证遮罩。
#: 只认在视口内、有实际面积、没有被隐藏的元素，避免页面上残留的
#: 隐藏节点造成误判（"其实已经通过验证，程序却还在等"）。
JS_SECURITY_MARKER = r"""
(args) => {
  const markers = (args.markers || []).map(m => ((m || '') + '').toLowerCase()).filter(Boolean);
  const vh = window.innerHeight || 0;
  const vw = window.innerWidth || 0;

  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 60 || r.height < 40) return null;
    const st = window.getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden') return null;
    if (parseFloat(st.opacity || '1') < 0.1) return null;
    if (!(r.bottom > 0 && r.top < vh && r.right > 0 && r.left < vw)) return null;
    return r;
  };

  for (const el of Array.from(document.querySelectorAll('div, section, span, p, h1, h2, h3, button, a'))) {
    let text = '';
    try { text = ((el.innerText || el.textContent || '') + '').toLowerCase(); } catch (e) { continue; }
    if (!text || text.length > 600) continue;
    if (!markers.some(m => text.indexOf(m) !== -1)) continue;
    if (!visible(el)) continue;
    return text.replace(/\s+/g, ' ').slice(0, 120);
  }

  // 有些验证是整页 iframe
  for (const frame of Array.from(document.querySelectorAll('iframe'))) {
    const src = (frame.getAttribute('src') || '').toLowerCase();
    if (!/captcha|verify|secsdk|unisec/.test(src)) continue;
    const r = visible(frame);
    if (!r) continue;
    if (r.width * r.height > 0.35 * vh * vw) return 'fullscreen-iframe:' + src.slice(0, 80);
  }
  return null;
}
"""


#: 在列表页 DOM 里读所有插件卡片的上架时间。
#: 插件给每个商品卡片注入 <div id="goods_card_<商品ID>">，直接遍历读取，
#: 不需要 hover（hover 会被插件 overlay 层拦截）。
#: 返回 {商品ID: {listing_time, listing_time_label}}。
JS_READ_ALL_LISTING_TIMES = r"""
() => {
  const result = {};
  // 直接遍历商品卡片（grid 子元素），在卡片内部找插件浮层
  const cardContainers = document.querySelectorAll('.grid > div');
  cardContainers.forEach((card) => {
    const link = card.querySelector('a[href*="/pdp/"]');
    if (!link) return;
    const href = link.getAttribute('href') || '';
    const m = href.match(/\/(\d+)(?:$|\?)/);
    if (!m) return;
    const pid = m[1];
    const overlay = card.querySelector('[id^="goods_card_"]');
    if (!overlay) return;
    const labels = overlay.querySelectorAll('span.label, span[class*="label"]');
    for (const el of labels) {
      if (!(el.textContent || '').includes('上架时间')) continue;
      let value = el.nextElementSibling;
      if (!value || !(value.textContent || '').trim()) {
        const v = overlay.querySelector('span.value, [class*="value"]');
        if (v) value = v;
      }
      if (!value) continue;
      const vtxt = (value.textContent || '').trim();
      if (!vtxt) continue;
      result[pid] = { listing_time_label: vtxt };
      break;
    }
  });
  return result;
}
"""


# ---------------------------------------------------------------------------
# 采集器
# ---------------------------------------------------------------------------
class TikTokRankingScraper:
    """TikTok Shop US 热销榜采集器。"""

    def __init__(
        self,
        url: str = config.URL,
        max_products: int = config.MAX_PRODUCTS,
        headless: bool = config.HEADLESS,
        debug: bool = False,
        category: str | None = None,
        min_sold: int | None = None,
        max_listing_age_days: int | None = None,
        min_listing_age_days: int | None = None,
        wait_for_verify: bool = False,
        strict_verify: bool = False,
        use_profile: bool = config.USE_PERSISTENT_PROFILE,
        profile_dir: Path | str = config.PROFILE_DIR,
        debug_dir: Path | str = config.DEBUG_DIR,
        cdp_url: str | None = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        self.url = url
        self.max_products = max(1, int(max_products))
        self.headless = headless
        self.debug = debug
        self.category = category
        #: 类别解析结果：用于在榜单页抓取时按 PDP 一级类别过滤（不跳转类目页）
        self.category_info = resolve_category(category) if category else None
        #: 销量下限过滤：只保留销量 >= min_sold 的商品（None 表示不过滤）
        self.min_sold = int(min_sold) if min_sold else None
        #: 上架时间过滤（天）：只保留上架不超过 max_listing_age_days 天的商品
        self.max_listing_age_days = int(max_listing_age_days) if max_listing_age_days else None
        self.min_listing_age_days = int(min_listing_age_days) if min_listing_age_days else None
        self.wait_for_verify = wait_for_verify
        self.strict_verify = strict_verify
        self.use_profile = use_profile
        self.profile_dir = Path(profile_dir)
        self.debug_dir = Path(debug_dir)
        #: CDP 模式：连接用户手动启动的真人 Chrome（None 时用 config.CDP_URL；
        #: 传空字符串可强制关闭 CDP 模式，回到程序自己启动浏览器）
        self.cdp_url: str | None = config.CDP_URL if cdp_url is None else cdp_url
        self._cdp_mode: bool = False
        #: 停止请求事件：GUI 点击『停止』时 set()，抓取循环在安全点退出
        self._stop_event = stop_event or threading.Event()

        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        #: 缓存每轮读到的商品上架时间 {商品ID: {listing_time, listing_time_label}}
        self._listing_time_cache: dict[str, dict[str, str]] = {}

    # -- 生命周期 ---------------------------------------------------------
    def __enter__(self) -> "TikTokRankingScraper":
        self._start_browser()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _start_browser(self) -> None:
        # 允许 __enter__ 和 run() 都调用，但只真正启动一次
        if self._context is not None:
            log.debug("浏览器已经启动，跳过重复初始化。")
            return

        # CDP 模式：连接用户手动启动的真人 Chrome（首页 /us 的整页安全验证
        # 只拦截自动化浏览器；真人 Chrome 可以正常访问，程序借它的身份抓取）
        if self.cdp_url:
            self._connect_cdp()
            return

        log.info("启动 Chromium（headless=%s）...", self.headless)
        self._playwright = sync_playwright().start()

        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        context_kwargs: dict[str, Any] = {
            "viewport": config.VIEWPORT,
            "user_agent": config.USER_AGENT,  # None = 使用浏览器真实 UA
            "locale": config.LOCALE,
            "timezone_id": config.TIMEZONE,
            "device_scale_factor": 1,
        }

        channels = list(config.BROWSER_CHANNELS)
        if None in channels and not self._bundled_chromium_available():
            channels.remove(None)
            log.debug("未安装 Playwright 自带 Chromium，跳过该项。")

        errors: list[str] = []
        # 第一轮用固定用户目录（保留 Cookie / 验证状态）；如果目录被占用或不可写，
        # 自动退化为临时目录，保证程序至少能跑起来。
        for use_profile in ([True, False] if self.use_profile else [False]):
            profile_locked = False
            for channel in channels:
                label = channel or "Playwright Chromium"
                try:
                    if use_profile:
                        self.profile_dir.mkdir(parents=True, exist_ok=True)
                        self._context = self._playwright.chromium.launch_persistent_context(
                            user_data_dir=str(self.profile_dir),
                            channel=channel,
                            headless=self.headless,
                            args=launch_args,
                            **context_kwargs,
                        )
                        self._browser = None
                        log.info("浏览器就绪：%s（用户目录 %s）", label, self.profile_dir)
                    else:
                        self._browser = self._playwright.chromium.launch(
                            headless=self.headless,
                            channel=channel,
                            args=launch_args,
                        )
                        self._context = self._browser.new_context(**context_kwargs)
                        log.info("浏览器就绪：%s（临时会话，不保留 Cookie）", label)
                    break
                except PlaywrightError as exc:
                    message = str(exc)
                    if use_profile and self._is_profile_lock_error(message):
                        profile_locked = True
                    errors.append(f"{label}{'' if use_profile else '(临时目录)'} -> {message.splitlines()[0]}")
                    log.debug("启动 %s 失败，尝试下一个：%s", label, exc)
                    self._context = None
                    self._browser = None

            if self._context is not None:
                break
            if use_profile and profile_locked:
                log.warning(
                    "浏览器用户目录 %s 被占用或不可写（常见原因：上次的浏览器进程没退干净、"
                    "或目录权限不足）。本次改用临时目录，Cookie 不会保留。",
                    self.profile_dir,
                )
                continue

        if self._context is None:
            self.close()
            detail = "\n".join(f"  - {item}" for item in errors)
            raise ScraperError(
                "浏览器启动失败。已尝试：\n"
                f"{detail}\n\n"
                "解决办法（任选其一）：\n"
                "  1) 安装 Playwright 自带 Chromium：python -m playwright install chromium\n"
                "  2) 安装 Google Chrome 或 Microsoft Edge，程序会自动使用它；\n"
                "  3) 如果提示用户目录被占用，请在任务管理器里结束残留的 chrome.exe，\n"
                "     或删除 .browser_profile 目录后重试。"
            )

        self._context.set_default_timeout(TIMING.element_wait_ms)
        self._context.add_init_script(STEALTH_JS)

    def _connect_cdp(self) -> None:
        """连接用户手动启动的真人 Chrome（CDP 模式）。

        背景：TikTok 对首页 /us 会整页拦截所有自动化浏览器（连滑块拖对都不放行），
        而用户手动启动的 Chrome 是"真人浏览器身份"，可以正常访问。
        程序通过 Chrome 的调试端口（--remote-debugging-port=9222）连接它，
        以真人浏览器身份打开首页并抓取，TikTok 无法区分。

        注意：CDP 模式下 close() 只断开连接，不会关闭用户的 Chrome 和标签页。
        """
        self._cdp_mode = True
        log.info("CDP 模式：连接已有 Chrome（%s）...", self.cdp_url)
        if self._playwright is None:
            self._playwright = sync_playwright().start()
        try:
            self._browser = self._playwright.chromium.connect_over_cdp(self.cdp_url)
        except PlaywrightError as exc:
            self.close()
            raise ScraperError(
                f"无法连接 Chrome（{self.cdp_url}）：{exc}\n\n"
                "请先用「启动Chrome.bat」或命令行启动带调试端口的 Chrome：\n"
                '  chrome.exe --remote-debugging-port=9222 --user-data-dir="<项目目录>\\.cdp_profile"\n'
                "等 Chrome 窗口出现后，再重新运行抓取程序。"
            ) from exc
        contexts = self._browser.contexts
        self._context = contexts[0] if contexts else self._browser.new_context()
        self._context.set_default_timeout(TIMING.element_wait_ms)
        log.info(
            "已连接 Chrome（%d 个浏览器上下文）。页面会在你的 Chrome 里新开标签页打开，"
            "请留意浏览器窗口。",
            len(self._browser.contexts),
        )

    @staticmethod
    def _is_profile_lock_error(message: str) -> bool:
        low = (message or "").lower()
        return any(
            key in low
            for key in ("processsingleton", "lock file", "profile directory", "拒绝访问", "access is denied")
        )

    def _bundled_chromium_available(self) -> bool:
        """Playwright 自带的 Chromium 是否已经下载好。"""
        if self._playwright is None:
            return False
        try:
            return Path(self._playwright.chromium.executable_path).exists()
        except Exception:  # pragma: no cover - 不同版本行为略有差异
            return False

    def close(self) -> None:
        if self._cdp_mode:
            # CDP 模式：只断开连接，绝不关闭用户的 Chrome 和标签页
            if self._playwright is not None:
                try:
                    self._playwright.stop()
                except Exception as exc:  # pragma: no cover
                    log.debug("停止 playwright 时出现问题：%s", exc)
            self._playwright = None
            self._browser = None
            self._context = None
            log.debug("CDP 连接已断开（未关闭用户的 Chrome）。")
            return

        for resource, name in ((self._context, "context"), (self._browser, "browser")):
            if resource is None:
                continue
            try:
                resource.close()
            except Exception as exc:  # pragma: no cover - 关闭失败不该影响主流程
                log.debug("关闭 %s 时出现问题：%s", name, exc)
        self._context = None
        self._browser = None

        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception as exc:  # pragma: no cover
                log.debug("停止 playwright 时出现问题：%s", exc)
            self._playwright = None

    # -- 小工具 -----------------------------------------------------------
    @staticmethod
    def _sleep(seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)

    @staticmethod
    def _rand_seconds(bounds: tuple[float, float]) -> float:
        return random.uniform(bounds[0], bounds[1])

    @staticmethod
    def _rand_int(bounds: tuple[int, int]) -> int:
        return random.randint(bounds[0], bounds[1])

    def request_stop(self) -> None:
        """请求终止抓取：GUI 点击『停止』时调用。

        只会让抓取循环在下一个安全点退出（不会立刻打断正在进行的
        页面请求），最终返回已抓到的部分结果。
        """
        self._stop_event.set()

    def _check_stop(self) -> None:
        """在抓取循环的安全点检查是否被要求停止，是则抛出 StopRequested。"""
        if self._stop_event.is_set():
            raise StopRequested("用户已请求终止抓取")

    def _save_debug_artifacts(self, page: Page, tag: str) -> None:
        """保存截图 / HTML 快照，便于排查页面结构变化。"""
        if not self.debug:
            return
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        screenshot = self.debug_dir / f"{tag}_{stamp}.png"
        html_file = self.debug_dir / f"{tag}_{stamp}.html"
        try:
            page.screenshot(path=str(screenshot), full_page=False)
            html_file.write_text(page.content(), encoding="utf-8")
            log.info("调试文件已保存：%s / %s", screenshot.name, html_file.name)
        except Exception as exc:  # pragma: no cover
            log.warning("保存调试文件失败：%s", exc)

    # -- 页面打开 ---------------------------------------------------------
    def _prepare_page(self, page: Page) -> None:
        """修正 UA 等细节：无头模式下把 HeadlessChrome 换成真实 Chrome 标识。"""
        try:
            current = page.evaluate("() => navigator.userAgent") or ""
        except PlaywrightError:
            return

        if config.USER_AGENT:
            target = config.USER_AGENT
        elif "HeadlessChrome" in current:
            target = current.replace("HeadlessChrome", "Chrome")
        else:
            return

        if not target or target == current:
            return
        try:
            session = page.context.new_cdp_session(page)
            session.send("Network.setUserAgentOverride", {"userAgent": target})
            log.debug("已修正 User-Agent：%s", target)
        except PlaywrightError as exc:
            log.debug("修正 User-Agent 失败（不影响主流程）：%s", exc)

    def _disable_cache_for_page(self, page: Page) -> None:
        """禁用当前标签页的 HTTP 缓存，保证每次抓取都向服务器请求最新数据。

        新开标签页不会继承之前的缓存禁用状态，所以每个页面都要单独设置；
        设置失败不影响主流程（只少了一重"防旧数据"保险）。
        """
        try:
            session = page.context.new_cdp_session(page)
            session.send("Network.enable")
            session.send("Network.setCacheDisabled", {"cacheDisabled": True})
        except PlaywrightError:
            pass

    def _security_check_signature(self, page: Page) -> str | None:
        """判断当前是否被安全验证拦住，返回命中的特征描述。

        注意：只有在**视口内真正可见**的验证层才算数 —— TikTok 在验证完成后
        有时会把节点留在 DOM 里，只看 innerText 会误判成"还没通过"。
        """
        try:
            title = (page.title() or "").strip().lower()
        except PlaywrightError:
            return None

        for marker in config.SECURITY_CHECK_TITLES:
            if marker in title:
                return f"页面标题命中「{marker}」"

        try:
            hit = page.evaluate(JS_SECURITY_MARKER, {"markers": list(config.SECURITY_CHECK_TEXTS)})
        except PlaywrightError:
            return None
        if hit:
            return f"可见遮罩命中「{str(hit)[:60]}」"
        return None

    @staticmethod
    def _stdin_watcher() -> dict[str, Any]:
        """后台线程监听回车键，返回一个共享状态字典。

        用"已读到的行数"计数而不是布尔标志：连续按两次回车也不会丢事件
        （布尔标志会被 clear() 吃掉，导致程序一直干等）。
        stdin 不可读（重定向 / 非交互）时线程自动退出，计数保持为 0。
        """
        state: dict[str, Any] = {"lines": 0, "eof": False}

        def reader() -> None:
            while True:
                try:
                    line = sys.stdin.readline()
                except Exception:
                    state["eof"] = True
                    return
                if line == "":  # EOF
                    state["eof"] = True
                    return
                state["lines"] += 1

        threading.Thread(target=reader, daemon=True, name="stdin-watcher").start()
        return state

    def _wait_for_manual_verification(self, page: Page, signature: str) -> None:
        """等待人工通过验证：不设超时，并支持按回车强制继续。"""
        log.warning("检测到 TikTok 安全验证（%s）。", signature)
        print()
        print("=" * 72)
        print("需要你手动完成一次人机验证（程序不会绕过验证）：")
        print("  1) 切到浏览器窗口，把滑块拼图拖到缺口处；")
        print("  2) 验证通过后程序会自动继续（每 2 秒检测一次）；")
        print("  3) 如果明明已经通过、程序还在等，直接在控制台按一次回车即可继续。")
        print("  默认不设超时，不会自动关闭浏览器；想中止请按 Ctrl+C。")
        print("=" * 72)
        print()

        stdin_state = self._stdin_watcher()

        deadline: float | None = None
        if config.VERIFY_WAIT_SECONDS and config.VERIFY_WAIT_SECONDS > 0:
            deadline = time.time() + config.VERIFY_WAIT_SECONDS

        confirmed = 0
        last_reminder = time.time()
        while True:
            self._sleep(config.VERIFY_POLL_INTERVAL_S)

            if self._security_check_signature(page) is None:
                log.info("验证已通过，继续抓取。")
                self._sleep(self._rand_seconds(TIMING.after_load_wait_s))
                return

            if stdin_state["lines"] > confirmed:
                confirmed = stdin_state["lines"]
                log.warning(
                    "按你的确认继续（页面上仍能看到验证层；"
                    "如果验证其实没完成，后面的请求仍可能被拦截）。"
                )
                return

            if deadline is not None and time.time() > deadline:
                raise SecurityCheckError(
                    f"等待人工验证超时（{config.VERIFY_WAIT_SECONDS} 秒）。\n"
                    "把 config.VERIFY_WAIT_SECONDS 设为 0 可以不限时等待，"
                    "或在验证通过后直接按回车强制继续。"
                )

            if time.time() - last_reminder >= 60:
                log.warning("仍在等待人工验证……（通过后程序会自动继续，也可以按回车强制继续）")
                last_reminder = time.time()

    def _handle_security_check(self, page: Page) -> None:
        """处理安全验证页：要么等待人工通过，要么抛出带指引的错误。"""
        signature = self._security_check_signature(page)
        if not signature:
            return

        self._save_debug_artifacts(page, "security_check")

        if not self.wait_for_verify or self.headless:
            raise SecurityCheckError(
                "被 TikTok 的安全验证拦住了（{}）。\n"
                "这是人机校验，不是代码 bug，无法用程序绕过。请这样处理：\n"
                "  1) 用可见浏览器运行：python main.py --headed --wait-verify\n"
                "  2) 在弹出的浏览器窗口里手动拖动滑块完成验证；\n"
                "  3) 程序会自动继续抓取，而且这次验证状态会保存在 {} 目录里，\n"
                "     以后运行通常不用再验证。".format(signature, self.profile_dir)
            )

        self._wait_for_manual_verification(page, signature)

    def _prepare_verified_page(self) -> Page:
        """打开页面并确保通过安全验证；无头模式被拦时自动切到可见浏览器。"""
        for attempt in range(2):
            page = self._open_page()
            self._save_debug_artifacts(page, "loaded")
            try:
                self._handle_security_check(page)
            except SecurityCheckError:
                can_escalate = (
                    attempt == 0
                    and self.headless
                    and not self.strict_verify
                    and config.AUTO_HEADED_ON_VERIFY
                )
                if not can_escalate:
                    raise
                log.warning(
                    "无头模式被安全验证拦住了。现在自动打开浏览器窗口，"
                    "请手动完成一次验证（程序会一直等你，不会自动关窗口）。"
                )
                self.close()
                self.headless = False
                self.wait_for_verify = True
                self._start_browser()
                continue
            return page
        raise SecurityCheckError("无法通过安全验证，请稍后重试。")

    def _open_page(self) -> Page:
        assert self._context is not None, "浏览器上下文尚未初始化"

        page = self._context.new_page()
        page.set_default_timeout(TIMING.element_wait_ms)
        self._prepare_page(page)
        self._disable_cache_for_page(page)
        log.info("打开页面：%s", self.url)

        try:
            response = page.goto(
                self.url,
                wait_until="domcontentloaded",
                timeout=TIMING.page_load_timeout_ms,
            )
        except PlaywrightTimeoutError as exc:
            self._save_debug_artifacts(page, "open_timeout")
            raise PageOpenError(
                f"打开页面超时（{TIMING.page_load_timeout_ms / 1000:.0f} 秒）：{self.url}\n"
                "请检查网络、代理或稍后重试。"
            ) from exc
        except PlaywrightError as exc:
            raise PageOpenError(f"打开页面失败：{self.url}\n原始错误：{exc}") from exc

        if response is not None and response.status >= 400:
            self._save_debug_artifacts(page, f"http_{response.status}")
            raise PageOpenError(
                f"页面返回 HTTP {response.status}：{self.url}\n"
                "可能是地区限制、页面改版或触发了风控。建议加 --headed 参数亲眼看看浏览器里的情况。"
            )

        try:
            page.wait_for_load_state("networkidle", timeout=TIMING.network_idle_timeout_ms)
        except PlaywrightTimeoutError:
            log.debug("networkidle 等待超时（页面有长连接，通常属正常），继续执行。")

        # 模拟真人打开页面后的阅读停顿
        self._sleep(self._rand_seconds(TIMING.after_load_wait_s))
        log.info("页面标题：%s", (page.title() or "").strip() or "(空)")
        return page

    # -- 页面交互 ---------------------------------------------------------
    def _mark_by_text(
        self,
        page: Page,
        patterns: list[str],
        selector: str | None = None,
        max_text_len: int = 40,
        max_area: int = 0,
        exact: bool = False,
        section_titles: list[str] | None = None,
    ) -> str | None:
        """按文本标记一个可见可点击元素，返回其文本；找不到返回 None。

        section_titles 非空时，只在对应栏目容器内查找（如首页"畅销商品"）。
        """
        args: dict[str, Any] = {
            "patterns": patterns,
            "maxTextLen": max_text_len,
            "maxArea": max_area,
            "exact": exact,
            "sectionTitles": section_titles or [],
        }
        if selector:
            args["selector"] = selector
        try:
            return page.evaluate(JS_MARK_BY_TEXT, args)
        except PlaywrightError as exc:
            log.debug("文本定位失败：%s", exc)
            return None

    def _human_click(self, page: Page, locator: Locator) -> None:
        """在元素内随机位置用真实鼠标点击（带移动轨迹与停顿）。"""
        box = locator.bounding_box()
        if not box:
            locator.click(timeout=TIMING.element_wait_ms)
            return
        x = box["x"] + box["width"] * random.uniform(0.30, 0.70)
        y = box["y"] + box["height"] * random.uniform(0.30, 0.70)
        page.mouse.move(x, y, steps=random.randint(8, 20))
        self._sleep(random.uniform(0.15, 0.45))
        page.mouse.click(x, y)

    def _hide_mouse(self, page: Page) -> None:
        """把鼠标放到视口中间，保证滚轮事件落在页面上。"""
        try:
            page.mouse.move(config.VIEWPORT["width"] / 2, config.VIEWPORT["height"] / 2, steps=5)
        except PlaywrightError:
            pass

    def _section_args(self) -> dict[str, Any]:
        """传给卡片收集 / 页面统计 JS 的参数：限定"畅销商品"栏目时传标题文本。"""
        if config.SOURCE_TYPE == "homepage":
            return {"sectionTitles": list(config.SECTION_TITLE_TEXTS)}
        return {"sectionTitles": []}

    def _page_stats(self, page: Page) -> dict[str, int]:
        try:
            return page.evaluate(JS_PAGE_STATS, self._section_args())
        except PlaywrightError:
            return {"anchors": 0, "imgs": 0, "height": 0}

    def _at_bottom(self, page: Page) -> bool:
        try:
            return bool(
                page.evaluate(
                    "() => { const d = document.scrollingElement || document.documentElement;"
                    " return d.scrollTop + window.innerHeight >= d.scrollHeight - 20; }"
                )
            )
        except PlaywrightError:
            return False

    def _human_scroll(self, page: Page, iterations: int = 3, until_bottom: bool = False) -> None:
        """缓慢向下滚动，模拟真人浏览。"""
        self._hide_mouse(page)
        limit = config.MAX_SCROLL_STEPS_PER_ROUND if until_bottom else max(1, iterations)
        last_height = self._page_stats(page)["height"]
        stable_rounds = 0

        for _ in range(limit):
            page.mouse.wheel(0, self._rand_int(TIMING.scroll_step_px))
            self._sleep(self._rand_seconds(TIMING.scroll_pause_s))

            if random.random() < TIMING.back_scroll_chance:
                page.mouse.wheel(0, -self._rand_int(TIMING.back_scroll_px))
                self._sleep(random.uniform(0.3, 0.9))

            if not until_bottom:
                continue

            stats = self._page_stats(page)
            if self._at_bottom(page) and stats["height"] == last_height:
                stable_rounds += 1
                if stable_rounds >= 2:
                    break
            else:
                stable_rounds = 0
            last_height = stats["height"]

    def _dismiss_overlays(self, page: Page) -> None:
        """尝试关掉 Cookie / 登录提示等遮挡层，失败也无所谓。"""
        for _ in range(2):
            text = self._mark_by_text(
                page,
                config.DISMISS_TEXTS,
                selector='button, a, [role="button"], [role="link"]',
                max_text_len=24,
                max_area=60_000,  # 只点小按钮，避免误点整块横幅
            )
            if not text:
                return
            try:
                log.info("关闭页面弹窗：%s", text)
                self._human_click(page, page.locator('[data-tts-pick="1"]').first)
                self._sleep(random.uniform(0.6, 1.4))
            except PlaywrightError as exc:
                log.debug("弹窗点击失败：%s", exc)
                return

    def _select_category(self, page: Page) -> None:
        """按类别抓取。

        主路径：类别能映射成类目页 URL 时（config.CATEGORIES），页面打开时
        已经是该类目页（见 __init__），这里只打日志；
        兜底路径：类别无法映射（自定义标签）时，尝试点击页面上的分类标签。
        """
        if not self.category:
            return
        if self.category_info:
            log.info(
                "按类别抓取：%s（%s），页面 %s",
                self.category_info["name_zh"],
                self.category_info["name_en"],
                self.url,
            )
            return
        text = self._mark_by_text(
            page,
            [self.category],
            selector='button, a, [role="button"], [role="tab"], div, span',
            max_text_len=30,
            max_area=40_000,
        )
        if not text:
            log.warning("没有找到分类标签「%s」，将按默认榜单抓取。", self.category)
            return
        try:
            log.info("切换到分类：%s", text)
            self._human_click(page, page.locator('[data-tts-pick="1"]').first)
            self._sleep(self._rand_seconds(TIMING.after_click_wait_s))
        except PlaywrightError as exc:
            log.warning("切换分类失败，改抓默认榜单：%s", exc)

    def _find_view_more(self, page: Page) -> Locator | None:
        """寻找『查看更多 / View More』按钮，返回可点击的定位器。

        首页模式（SOURCE_TYPE == "homepage"）下，只在"畅销商品"栏目容器内查找，
        避免点到页面其它栏目（如 Recommended for you）的同名按钮 —— 那些按钮
        点击后会跳转到新页面，而不是在栏目内加载更多商品。
        """
        section_titles = (
            list(config.SECTION_TITLE_TEXTS) if config.SOURCE_TYPE == "homepage" else None
        )
        # 第一次找：在 section 内
        text = self._mark_by_text(
            page,
            config.VIEW_MORE_TEXTS,
            selector='button, a, [role="button"], [role="link"], div, span, p',
            max_text_len=36,
            section_titles=section_titles,
        )
        if not text:
            # 滚动到底部等2秒再试一次（按钮可能在新加载商品下方）
            try:
                self._human_scroll(page, until_bottom=True)
                self._sleep(2.0)
            except Exception:
                pass
            text = self._mark_by_text(
                page,
                config.VIEW_MORE_TEXTS,
                selector='button, a, [role="button"], [role="link"], div, span, p',
                max_text_len=36,
                section_titles=section_titles,
            )
        if not text:
            # 去掉 section 限制，全局找一次
            text = self._mark_by_text(
                page,
                config.VIEW_MORE_TEXTS,
                selector='button, a, [role="button"], [role="link"], div, span, p',
                max_text_len=36,
                section_titles=None,
            )
        if not text:
            log.debug("未找到『查看更多』文本（已滚动重试+全局搜索）")
            return None
        locator = page.locator('[data-tts-pick="1"]').first
        if locator.count() == 0:
            for t in config.VIEW_MORE_TEXTS:
                fallback = page.get_by_text(t, exact=False).first
                if fallback.count() > 0:
                    log.info("找到『查看更多』按钮（文本匹配）：%s", text)
                    return fallback
            log.debug("找到『查看更多』文本但 data-tts-pick 元素不存在，回退也失败")
            return None
        log.info("找到『查看更多』按钮：%s", text)
        return locator

    def _wait_for_growth(
        self,
        page: Page,
        before: dict[str, int],
        timeout_ms: int | None = None,
    ) -> bool:
        """点击后等待出现新商品（锚点数或图片数增加）。"""
        deadline = time.time() + (timeout_ms or TIMING.after_click_poll_ms) / 1000
        while time.time() < deadline:
            self._sleep(TIMING.poll_interval_ms / 1000)
            stats = self._page_stats(page)
            if stats["anchors"] > before["anchors"] or stats["imgs"] > before["imgs"]:
                return True
        log.debug("点击后未检测到新商品（可能已到榜单末尾）。")
        return False

    # -- 抓取 -------------------------------------------------------------
    def _collect_cards(self, page: Page) -> list[dict[str, Any]]:
        try:
            cards = page.evaluate(JS_COLLECT_CARDS, self._section_args())
        except PlaywrightError as exc:
            log.warning("读取页面商品卡片失败：%s", exc)
            return []
        if not isinstance(cards, list):
            return []
        log.debug("本轮读取到 %d 张卡片", len(cards))
        return cards

    @staticmethod
    def _card_key(card: dict[str, Any]) -> str:
        url = (card.get("url") or "").strip()
        if url:
            return url.split("?")[0].rstrip("/").lower()
        return (card.get("text") or "").strip()[:200].lower()

    # -- 商品详情页（PDP）相关 ------------------------------------------------
    def _read_pdp_details(self, product_url: str) -> dict[str, str] | None:
        """打开 PDP 读详情；遇 TikTok 限流自动重试（最多 3 次，间隔递增）。"""
        if self._context is None:
            return None
        for attempt in range(3):
            result = self._open_pdp_once(product_url)
            if result is not None:
                return result
            if attempt < 2:
                wait_s = 6 * (attempt + 1)
                log.warning(
                    "PDP 第 %d/3 次失败/限流，等 %d 秒重试：%s",
                    attempt + 1, wait_s, product_url[:80],
                )
                self._sleep(wait_s)
        log.warning("PDP 多次失败，放弃该商品：%s", product_url[:80])
        return None

    def _open_pdp_once(self, product_url: str) -> dict[str, str] | None:
        """单次打开 PDP 并读取；被限流/页面异常时返回 None。"""
        pdp_page: Page | None = None
        try:
            pdp_page = self._context.new_page()
            self._disable_cache_for_page(pdp_page)
            pdp_page.goto(
                product_url,
                wait_until="domcontentloaded",
                timeout=TIMING.page_load_timeout_ms,
            )
            try:
                pdp_page.wait_for_load_state(
                    "networkidle", timeout=TIMING.network_idle_timeout_ms
                )
            except PlaywrightError:
                pass
            html_text = ""
            blocked = False
            for _ in range(14):
                html_text = pdp_page.content()
                lower = html_text.lower()
                if "request blocked" in lower or "unusual activity" in lower:
                    blocked = True
                    break
                if extract_product_description(html_text):
                    break
                self._sleep(0.6)
            if blocked:
                log.debug("PDP 命中限流提示：%s", product_url[:80])
                return None
            self._sleep(self._rand_seconds(TIMING.pdp_wait_s))
            html_text = pdp_page.content()
            result: dict[str, str] = {
                "description": extract_product_description(html_text),
                "image_url": extract_product_image_from_pdp(html_text),
            }
            category = extract_category_from_pdp(html_text)
            if category:
                result["category_id"] = category["category_id"]
                result["category_name"] = category["category_name"]
            return result
        except PlaywrightError as exc:
            log.warning("打开商品详情页失败（%s）：%s", product_url[:100], exc)
            return None
        finally:
            if pdp_page is not None:
                try:
                    pdp_page.close()
                except PlaywrightError:
                    pass

    def _listing_time_filter(self, listing_time: str, listing_label: str = "") -> tuple[bool, str]:
        """上架时间筛选。

        列表层只有相对文本（如"1年前收录"、"2月前收录"），PDP 层才有精确时间戳。
        优先用精确时间；没有精确时间时解析相对文本粗判。
        """
        import re as _re
        # 优先：精确时间戳
        if listing_time:
            try:
                listed_at = datetime.strptime(listing_time.strip(), "%Y-%m-%d %H:%M:%S")
                age_days = (datetime.now() - listed_at).total_seconds() / 86400.0
            except ValueError:
                age_days = None
            if age_days is not None:
                if self.max_listing_age_days is not None and age_days > self.max_listing_age_days:
                    return False, f"上架 {age_days:.0f} 天 > {self.max_listing_age_days} 天上限"
                if self.min_listing_age_days is not None and age_days < self.min_listing_age_days:
                    return False, f"上架 {age_days:.0f} 天 < {self.min_listing_age_days} 天下限"
                return True, ""
        # 兜底：解析相对文本。支持"X天/周/月/年前收录"约X天；"X天/周/月/年内收录"=不到X天
        label = (listing_label or "").strip()
        if not label:
            return False, "插件未注入上架时间（确认 CDP Chrome 已装『Tiktok选品助手』）"
        m_before = _re.match(r"([\d.]+)\s*(?:个)?(天|周|月|年)前", label)
        m_within = _re.match(r"([\d.]+)\s*(?:个)?(天|周|月|年)内", label)
        if not m_before and not m_within:
            return False, f"上架时间格式无法解析：{label}"
        factor = {"天": 1, "周": 7, "月": 30, "年": 365}
        if m_before:
            num = float(m_before.group(1))
            unit = m_before.group(2)
            age_days = num * factor[unit]
            age_desc = f"约{age_days:.0f}天"
        else:
            num = float(m_within.group(1))
            unit = m_within.group(2)
            age_days = num * factor[unit]
            age_desc = f"{age_days:.0f}天内"
        if self.max_listing_age_days is not None and age_days > self.max_listing_age_days:
            return False, f"上架{age_desc}（{label}）> {self.max_listing_age_days} 天上限"
        if self.min_listing_age_days is not None and age_days < self.min_listing_age_days:
            return False, f"上架{age_desc}（{label}）< {self.min_listing_age_days} 天下限"
        return True, ""

    def _read_all_listing_times(self, page: Page) -> dict[str, dict[str, str]]:
        """一次性读列表页所有插件卡片的上架时间。

        插件给每个商品卡片注入 <div id="goods_card_<商品ID>">，
        直接遍历读取，不需要 hover（hover 会被 overlay 层拦截）。
        插件注入是异步的，最多等 10 秒重试 5 次。
        """
        data: dict[str, dict[str, str]] = {}
        attempt = 0
        for attempt in range(5):
            try:
                result = page.evaluate(JS_READ_ALL_LISTING_TIMES)
                # debug：dump goods_card 元素总数和第一个的 HTML
                if self.debug and attempt == 0:
                    info = page.evaluate(
                        """() => {
                          const cards = document.querySelectorAll('[id^="goods_card_"]');
                          let sample = '';
                          if (cards.length > 0) sample = cards[0].outerHTML.slice(0, 6000);
                          return {count: cards.length, sample: sample,
                                  firstId: cards.length > 0 ? cards[0].id : ''};
                        }"""
                    )
                    if isinstance(info, dict):
                        log.debug(
                            "goods_card 元素: 总数=%d, 第一个id=%s",
                            info.get("count", 0), info.get("firstId", ""),
                        )
                        if info.get("sample"):
                            self.debug_dir.mkdir(parents=True, exist_ok=True)
                            fn = self.debug_dir / (
                                "goodscard_" + datetime.now().strftime("%H%M%S_%f") + ".html"
                            )
                            fn.write_text(info["sample"], encoding="utf-8")
                            log.debug("goods_card 样本已保存：%s", fn.name)
            except PlaywrightError as exc:
                if self.debug:
                    log.debug("批量读上架时间失败：%s", exc)
                result = {}
            if isinstance(result, dict) and len(result) > 0:
                data = result
                break
            if attempt < 4:
                self._sleep(2.0)  # 等插件异步注入
        if self.debug:
            log.debug("批量读到 %d 个商品的上架时间（重试 %d 次）", len(data), attempt + 1)
        return data

    def _category_matches(self, category: dict[str, str]) -> bool:
        """判断 PDP 读到的一级类别是否与用户设定的类别一致。

        依次比较 category_id、英文名、中文名，任一匹配即视为一致。
        """
        if not self.category_info:
            return True
        expected = self.category_info
        category_id = str(category.get("category_id") or "")
        category_name = str(category.get("category_name") or "").strip()
        if category_id and category_id == expected["category_id"]:
            return True
        if category_name and category_name.lower() == expected["name_en"].lower():
            return True
        if category_name and category_name == expected["name_zh"]:
            return True
        return False

    def _harvest(self, page: Page) -> list[dict[str, Any]]:
        """滚动 + 查看更多循环，收集原始卡片。

        所有抓取都会逐个进入 PDP 读取『商品详情介绍』（以及一级类别）；
        指定类别时只保留匹配类别的商品，直到集满 max_products 条或列表到底。
        """
        cards: list[dict[str, Any]] = []
        seen: set[str] = set()
        crawl_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        no_growth_rounds = 0
        view_more_clicks = 0
        processed = 0
        results: list[dict[str, Any]] = []
        target = self.max_products
        filter_by_category = self.category_info is not None
        if self.min_sold is not None:
            log.info(
                "销量过滤：仅保留销量 ≥ %d 的商品%s。",
                self.min_sold,
                f"（类别：{self.category_info['name_zh']}）" if filter_by_category else "",
            )

        def merge(batch: list[dict[str, Any]]) -> int:
            added = 0
            for card in batch:
                key = self._card_key(card)
                if not key or key in seen:
                    continue
                seen.add(key)
                cards.append(card)
                added += 1
            return added

        listing_pending: dict[str, int] = {}
        def inspect_new_cards() -> None:
            """对未处理的候选卡片逐个进 PDP 读取详情介绍（与一级类别）。"""
            nonlocal processed
            while processed < len(cards) and len(results) < target:
                self._check_stop()
                card = cards[processed]
                processed += 1
                record = build_product_record(
                    card_html=card.get("html") or "",
                    fallback_url=card.get("url") or "",
                    base_url=self.url,
                    keywords=config.PRODUCT_URL_KEYWORDS,
                    crawl_time=crawl_time,
                )
                if not is_meaningful(record):
                    continue
                # 销量过滤：卡片上就有销量，未达标的不进 PDP（省时间）
                if self.min_sold is not None:
                    sold = record.get("sold_count")
                    if not isinstance(sold, (int, float)) or int(sold) < self.min_sold:
                        if self.debug:
                            log.debug(
                                "销量不达标，跳过：%s（销量 %s < %d）",
                                (record.get("product_name") or "")[:40],
                                sold,
                                self.min_sold,
                            )
                        continue
                product_url = record.get("product_url")
                if not product_url:
                    continue
                # 上架时间筛选（查本轮批量读到的 cache）：不满足的不进 PDP
                listing_filter_on = (
                    self.max_listing_age_days is not None
                    or self.min_listing_age_days is not None
                )
                if listing_filter_on:
                    pid = product_url.rstrip("/").split("/")[-1]
                    card_listing = self._listing_time_cache.get(pid) or {}
                    if self.debug:
                        log.debug(
                            "上架时间查询: pid=%s, cache命中=%s, 值=%s",
                            pid, bool(card_listing), card_listing,
                        )
                    if not card_listing:
                        waited = listing_pending.get(pid, 0)
                        if waited < 3:
                            listing_pending[pid] = waited + 1
                            processed -= 1
                            if self.debug and waited == 0:
                                cache_keys = list(self._listing_time_cache.keys())[:5]
                                log.debug(
                                    "上架时间未加载，等待下一轮：%s | 当前pid=%s | cache有=%s",
                                    (record.get("product_name") or "")[:40],
                                    pid,
                                    cache_keys,
                                )
                            return
                        if self.debug:
                            log.debug(
                                "上架时间等了%d轮仍未加载，按无数据跳过：%s",
                                waited + 1,
                                (record.get("product_name") or "")[:40],
                            )
                        continue
                    record["listing_time"] = card_listing.get("listing_time") or ""
                    record["listing_time_label"] = card_listing.get("listing_time_label") or ""
                    keep, reason = self._listing_time_filter(
                        record.get("listing_time", ""),
                        record.get("listing_time_label", ""),
                    )
                    if self.debug:
                        log.debug(
                            "上架时间筛选结果: %s -> %s (%s)",
                            record.get("listing_time") or "(空)", keep, reason,
                        )
                    if not keep:
                        if self.debug:
                            log.debug(
                                "上架时间不满足，跳过（不进 PDP）：%s（%s）",
                                (record.get("product_name") or "")[:40],
                                reason,
                            )
                        continue
                details = self._read_pdp_details(product_url)
                if not details:
                    log.warning("商品详情页读取失败，跳过该商品：%s", product_url[:90])
                    self._sleep(self._rand_seconds(TIMING.pdp_interval_s))
                    continue
                record["description"] = details.get("description") or config.DESCRIPTION_IMAGE_FALLBACK
                # 图片兜底：列表页卡片因懒加载没解析到主图时，用 PDP 的主图补上
                if not record.get("image_url"):
                    pdp_image = details.get("image_url") or ""
                    if pdp_image:
                        record["image_url"] = pdp_image
                        log.debug(
                            "列表页未取到图片，已用 PDP 主图兜底：%s",
                            (record.get("product_name") or "")[:40],
                        )
                if filter_by_category:
                    category = {
                        "category_id": details.get("category_id") or "",
                        "category_name": details.get("category_name") or "",
                    }
                    if category.get("category_id") and self._category_matches(category):
                        record["category_id"] = category["category_id"]
                        record["category_name"] = category["category_name"]
                        results.append(record)
                        log.info(
                            "命中类别「%s」（%s），已匹配 %d / %d",
                            category["category_name"],
                            category["category_id"],
                            len(results),
                            target,
                        )
                    elif self.debug:
                        log.debug(
                            "类别不符，跳过：%s（期望 %s）",
                            category["category_name"],
                            self.category_info["name_zh"] if self.category_info else "",
                        )
                else:
                    results.append(record)
                    log.info(
                        "已读取商品详情 %d / %d（%s）",
                        len(results),
                        target,
                        (record.get("product_name") or "")[:40],
                    )
                self._sleep(self._rand_seconds(TIMING.pdp_interval_s))

        def done() -> bool:
            return len(results) >= target

        # 上架时间筛选开启时：先等插件注入完浮层数据，再开始处理第一批卡片
        listing_filter_on = (
            self.max_listing_age_days is not None
            or self.min_listing_age_days is not None
        )
        if listing_filter_on:
            log.info("等待插件注入上架时间数据...")
            for wait_attempt in range(10):
                # 每2轮缓慢滚动一次，触发插件懒加载上架时间
                if wait_attempt > 0 and wait_attempt % 2 == 0:
                    try:
                        self._human_scroll(page, until_bottom=False)
                        self._sleep(1.0)
                    except Exception:
                        pass
                merge(self._collect_cards(page))
                self._listing_time_cache.update(self._read_all_listing_times(page))
                if len(self._listing_time_cache) > 0:
                    break
                self._sleep(2.0)
            log.info("插件初始缓存：%d 个商品有上架时间", len(self._listing_time_cache))
            if self.debug and self._listing_time_cache:
                sample_keys = list(self._listing_time_cache.keys())[:5]
                log.debug("cache 里的 PID 样本: %s", sample_keys)

        try:
            for round_index in range(1, config.MAX_SCROLL_ROUNDS + 1):
                self._check_stop()
                # 验证随时可能在翻页过程中重新弹出
                if self._security_check_signature(page):
                    self._handle_security_check(page)

                merge(self._collect_cards(page))
                # 每轮读一次插件注入的上架时间缓存（供卡片层筛选用）
                if listing_filter_on:
                    self._listing_time_cache.update(self._read_all_listing_times(page))
                inspect_new_cards()
                log.info(
                    "第 %d 轮：候选 %d 个，已获取 %d / 目标 %d",
                    round_index,
                    len(cards),
                    len(results),
                    target,
                )

                if done():
                    break

                before_count = len(cards)
                clicked = False

                # 1) 继续向下浏览，触发懒加载
                self._check_stop()
                self._human_scroll(page, until_bottom=True)
                merge(self._collect_cards(page))
                inspect_new_cards()

                # 2) 找『查看更多』并点击
                if view_more_clicks < config.MAX_VIEW_MORE_CLICKS and not done():
                    self._check_stop()
                    self._sleep(1.0)  # 等按钮渲染
                    before_stats = self._page_stats(page)
                    locator = self._find_view_more(page)
                    if locator is not None:
                        try:
                            self._sleep(self._rand_seconds(TIMING.before_click_wait_s))
                            self._human_click(page, locator)
                            clicked = True
                            view_more_clicks += 1
                            self._wait_for_growth(page, before_stats)
                            self._sleep(self._rand_seconds(TIMING.after_click_wait_s))
                        except PlaywrightError as exc:
                            log.warning("点击『查看更多』失败（会继续尝试其它方式）：%s", exc)
                        merge(self._collect_cards(page))
                        inspect_new_cards()

                if done():
                    break

                gained = len(cards) - before_count
                if gained > 0:
                    no_growth_rounds = 0
                else:
                    no_growth_rounds += 1
                    log.debug("本轮没有新增商品（连续 %d 轮）", no_growth_rounds)

                if no_growth_rounds >= config.NO_GROWTH_LIMIT:
                    log.info("页面已无更多商品（连续 %d 轮没有新增）。", no_growth_rounds)
                    break

                self._sleep(self._rand_seconds(TIMING.between_rounds_s))
            else:
                log.warning("达到最大轮次上限（%d 轮），停止滚动。", config.MAX_SCROLL_ROUNDS)
        except StopRequested:
            log.info("已收到停止请求，结束抓取（已获取 %d / %d）。", len(results), target)

        log.info(
            "抓取完成：候选 %d 个，最终 %d 条（目标 %d）%s。",
            len(cards),
            len(results),
            target,
            f"（类别：{self.category_info['name_zh']}）" if filter_by_category else "",
        )
        if len(results) < target:
            log.warning(
                "数量不足：只拿到 %d 条（目标 %d），可能榜单已到底或该类别商品较少。",
                len(results),
                target,
            )
        assign_ranks(results)
        return results[:target]

    # -- 对外入口 ---------------------------------------------------------
    def run(self) -> list[dict[str, Any]]:
        """执行一次完整抓取，返回商品记录列表。"""
        self._start_browser()
        try:
            page = self._prepare_verified_page()
            self._dismiss_overlays(page)
            self._select_category(page)

            products = self._harvest(page)

            if self.debug:
                self._save_debug_artifacts(page, "final")
                self.debug_dir.mkdir(parents=True, exist_ok=True)
                raw_file = self.debug_dir / (
                    f"products_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
                )
                raw_file.write_text(
                    json.dumps(products, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                log.info("调试数据已保存：%s", raw_file.name)

            if not products:
                self._save_debug_artifacts(page, "no_product")
                raise NoProductFoundError(
                    "页面已打开，但没有解析到任何商品。\n"
                    "建议执行：python inspect_page.py --headed\n"
                    "它会截图、保存 HTML 并打印页面结构，便于确认商品卡片长什么样。"
                )

            if len(products) < self.max_products:
                log.warning(
                    "只抓到 %d 个商品（目标 %d 个）。可能是榜单已到底、懒加载较慢，或页面结构有变化。",
                    len(products),
                    self.max_products,
                )
            return products
        finally:
            self.close()


def scrape_products(
    url: str = config.URL,
    max_products: int = config.MAX_PRODUCTS,
    headless: bool = config.HEADLESS,
    debug: bool = False,
    category: str | None = None,
    wait_for_verify: bool = False,
    strict_verify: bool = False,
) -> list[dict[str, Any]]:
    """便捷函数：一次性完成抓取。"""
    with TikTokRankingScraper(
        url=url,
        max_products=max_products,
        headless=headless,
        debug=debug,
        category=category,
        wait_for_verify=wait_for_verify,
        strict_verify=strict_verify,
    ) as scraper:
        return scraper.run()
