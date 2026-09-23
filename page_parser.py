"""页面解析工具：把商品卡片 HTML 解析成结构化字段。

设计原则（对应需求里的"稳定性要求"）：

* 不依赖某个具体的 class 名或 CSS 路径（TikTok 的类名是哈希过的，比如 ``item-ZxfZxl``）；
* 优先使用**语义结构**（商品链接、图片、aria-label、字段之间的相邻关系）
  和**文本特征**（``$``、``sold``、``%``、``Rating: x out of 5``）来定位；
* 任何单个字段解析失败都不影响其它字段，也不影响整条记录的输出。

实测的 TikTok Shop US 榜单卡片结构（2026-09 快照）大致是：

.. code-block:: html

    <div class="relative w-full cursor-pointer">          <!-- 卡片 -->
      <picture><img src="产品主图" alt="商品名称"></picture>
      <div>Free shipping</div>                            <!-- 角标 -->
      <div>店铺名称</div>                                  <!-- 店铺 -->
      <a href="https://shop.tiktok.com/us/pdp/<slug>/<id>">  <!-- 商品链接 -->
        <h3 title="商品名称">商品名称</h3>
      </a>
      <span>4.6</span><div aria-label="Rating: 4.6 out of 5 stars">
      <div>2.1M sold</div>                                <!-- 销量 -->
      <div> - 66% </div>                                  <!-- 折扣 -->
      <div>$16.97</div><span class="line-through">$49.97</span>   <!-- 现价 / 原价 -->
    </div>
"""

from __future__ import annotations

import html as html_lib
import json
import re
from datetime import datetime
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

# ---------------------------------------------------------------------------
# 正则
# ---------------------------------------------------------------------------
#: 价格：$12.99 / US$1,299.00
PRICE_RE = re.compile(r"(?:US\s*)?\$\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)")

#: 数字 token：1,234 / 1.2K / 3.5M / 3200
_NUMBER = r"[0-9][0-9,]*(?:\.[0-9]+)?"
_UNIT = r"[ \t]*[KkMm万]?"

#: 销量：1.2K sold / 1,234+ sold / Sold 5.6K / 已售 3200
#: 分隔符只用空格 / 制表符，避免把上一行的价格当成销量数字。
SOLD_RES = [
    re.compile(rf"({_NUMBER}{_UNIT})[ \t]*\+?[ \t]*(?:sold|sales|pcs|pieces)", re.I),
    re.compile(rf"(?:sold|sales)[ \t]*[:：]?[ \t]*({_NUMBER}{_UNIT})", re.I),
    re.compile(rf"({_NUMBER}{_UNIT})[ \t]*\+?[ \t]*(?:已售|销量|件)"),
]

#: 折扣：-66% / 20% off / Save $5
DISCOUNT_RES = [
    re.compile(r"-?\s*([0-9]{1,2}(?:\.[0-9])?)\s*%", re.I),
    re.compile(r"(?:save|省)\s*(?:US\s*)?\$\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", re.I),
]

#: 评分：aria-label 里的 "Rating: 4.6 out of 5 stars" 最可靠
RATING_LABEL_RES = [
    re.compile(r"rating[^0-9]{0,12}([0-5](?:\.[0-9])?)", re.I),
    re.compile(r"([0-5](?:\.[0-9])?)\s*(?:out of|/)\s*5", re.I),
]

#: 评分：4.7★ / ★4.7 / 4.7 (1234) / 4.7 分
RATING_RES = [
    re.compile(r"([0-5](?:[.,][0-9])?)\s*(?:★|⭐|\u2b50|stars?\b|分)"),
    re.compile(r"(?:★|⭐|\u2b50)\s*([0-5](?:[.,][0-9])?)"),
    re.compile(r"\b([0-5]\.[0-9])\s*\(\s*[0-9][0-9,\.]*\s*[KkMm]?\s*\)"),
]

#: 评分兜底：独立成行的 4.6 这种写法（TikTok 榜单页就是这样）
STANDALONE_RATING_RE = re.compile(r"^([0-5]\.[0-9])$")

#: 倒计时（闪购剩余时间），不能当店铺名
TIME_RE = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?$")

#: 噪声词：这些词单独作为单词出现时，说明这一行不是商品名 / 店铺名
NOISE_WORD_RE = re.compile(
    r"\b(?:ad|ads|sponsored|top|rank|ranking|deal|deals|sale|sold|off|shipping|"
    r"rating|ratings|reviews?|coupon|free)\b",
    re.I,
)

#: 噪声短语（角标、促销文案）
NOISE_PHRASES = (
    "free shipping",
    "limited time",
    "flash sale",
    "just for you",
    "verified by",
    "已售",
    "销量",
    "包邮",
    "广告",
    "限时",
)

#: 明显是图标 / 角标的图片，不要当作商品主图
BADGE_IMAGE_HINTS = (
    "store_blue_v",
    "verified",
    "badge",
    "/icon",
    "icon_",
    "logo_small",
    "sprite",
    # "Stock Up Deals" 促销标签图（被多个商品共用的同一张图）
    "7b539533779d4ae0a1229552c7e",
)


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def clean_text(value: str | None) -> str:
    """压缩空白，去掉零宽字符。"""
    if not value:
        return ""
    value = str(value).replace("\u200b", "").replace("\xa0", " ")
    return re.sub(r"[ \t\r\f\v]+", " ", value).strip()


def text_lines(text: str) -> list[str]:
    """把卡片文本拆成干净的文本行。"""
    lines: list[str] = []
    for raw in (text or "").split("\n"):
        line = clean_text(raw)
        if line:
            lines.append(line)
    return lines


def to_float(token: str) -> float | None:
    """``"1,299.00"`` -> ``1299.0``"""
    try:
        return float(str(token).replace(",", ""))
    except (TypeError, ValueError):
        return None


def to_count(token: str) -> int | None:
    """把销量文本转成整数：``"1.2K"`` -> 1200，``"3.4万"`` -> 34000。"""
    if token is None:
        return None
    token = clean_text(str(token)).replace(",", "")
    match = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*([KkMm万]?)$", token)
    if not match:
        return None
    number = float(match.group(1))
    unit = match.group(2)
    factor = {"": 1, "k": 1_000, "m": 1_000_000, "万": 10_000}.get(unit.lower(), 1)
    return int(round(number * factor))


def abs_url(url: str | None, base: str = "") -> str:
    if not url:
        return ""
    url = str(url).strip()
    if url.startswith(("data:", "blob:", "javascript:")):
        return ""
    try:
        return urljoin(base, url) if base else url
    except ValueError:
        return url


def _unique(values: list[float]) -> list[float]:
    result: list[float] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


def _looks_like_noise(line: str) -> bool:
    """判断一行文本是不是角标 / 价格 / 评分这类"非名称"内容。"""
    if not line:
        return True
    if PRICE_RE.search(line):
        return True
    if TIME_RE.match(line):
        return True
    low = line.lower()
    if any(phrase in low for phrase in NOISE_PHRASES):
        return True
    if NOISE_WORD_RE.search(line):
        return True
    if re.fullmatch(r"[\d\s\.,%+\-★⭐]+", line):
        return True
    return False


def _looks_like_shop_name(line: str, title: str) -> bool:
    """店铺名的兜底判定：短、含字母、不是标题 / 角标 / 价格 / 倒计时。"""
    if not line or not (2 <= len(line) <= 40):
        return False
    if line == title or _looks_like_noise(line):
        return False
    # 至少要有一个字母或汉字，纯数字 / 符号不算
    if not re.search(r"[A-Za-z\u4e00-\u9fff]{2}", line):
        return False
    return True


# ---------------------------------------------------------------------------
# 字段提取
# ---------------------------------------------------------------------------
def extract_prices(text: str) -> list[float]:
    """按出现顺序取出所有价格。"""
    values: list[float] = []
    for match in PRICE_RE.finditer(text or ""):
        value = to_float(match.group(1))
        if value is not None and 0 < value < 1_000_000:
            values.append(value)
    return values


def extract_sold(text: str) -> tuple[int | None, str]:
    """返回 ``(销量数字, 原始文本)``。"""
    # 先把价格片段去掉，避免 "$15.49 / Sold 3.4K" 里的 15.49 被当成销量
    cleaned = PRICE_RE.sub(" ", text or "")
    for regex in SOLD_RES:
        match = regex.search(cleaned)
        if not match:
            continue
        raw = clean_text(match.group(1))
        # 两位小数更像价格而不是销量，直接跳过
        if re.fullmatch(r"[0-9][0-9,]*\.[0-9]{2}", raw):
            continue
        count = to_count(raw)
        if count is not None:
            return count, raw
    return None, ""


def extract_rating(text: str, card: Tag | None = None, lines: list[str] | None = None) -> float | None:
    """提取评分，按可靠性从高到低尝试。

    1. ``aria-label="Rating: 4.6 out of 5 stars"``（TikTok 榜单页就是这样写的）
    2. ``4.7★`` / ``★4.7`` 这类带星号的文本
    3. 紧挨着"xx sold"上一行的独立数字行（4.6）
    """
    if card is not None:
        for node in card.find_all(attrs={"aria-label": True}):
            label = node.get("aria-label") or ""
            for regex in RATING_LABEL_RES:
                match = regex.search(label)
                if not match:
                    continue
                value = to_float(match.group(1))
                if value is not None and 0 < value <= 5:
                    return round(value, 1)

    for regex in RATING_RES:
        match = regex.search(text or "")
        if not match:
            continue
        value = to_float(match.group(1).replace(",", "."))
        if value is not None and 0 < value <= 5:
            return round(value, 1)

    # 兜底：找独立成行的评分数字，优先取"xx sold"上一行
    if lines:
        for index, line in enumerate(lines):
            if not re.search(r"sold|[0-9]\s*[KkMm]?\s*sales", line, re.I):
                continue
            for offset in (1, 2):
                if index - offset < 0:
                    break
                match = STANDALONE_RATING_RE.match(lines[index - offset])
                if match:
                    value = to_float(match.group(1))
                    if value is not None and 0 < value <= 5:
                        return round(value, 1)

        for line in lines:
            match = STANDALONE_RATING_RE.match(line)
            if match:
                value = to_float(match.group(1))
                if value is not None and 0 < value <= 5:
                    return round(value, 1)
    return None


def extract_discount(
    text: str,
    current_price: float | None,
    original_price: float | None,
) -> float | None:
    """折扣百分比（正数表示降价幅度）。

    优先读页面文案（如 ``-66%``），读不到就用当前价 / 原价反算。
    """
    for regex in DISCOUNT_RES:
        match = regex.search(text or "")
        if not match:
            continue
        value = to_float(match.group(1))
        if value is None:
            continue
        if "%" in match.group(0):
            if 0 < value < 100:
                return round(value, 1)
        elif original_price and original_price > 0:
            # "Save $5" 这类写法换算成折扣比例
            ratio = value / (original_price + value)
            if 0 < ratio < 1:
                return round(ratio * 100, 1)

    if current_price and original_price and original_price > current_price > 0:
        return round((1 - current_price / original_price) * 100, 1)
    return None


def _strikethrough_prices(card: Tag) -> list[float]:
    """找出划线原价：``<del>`` / ``<s>`` / ``line-through`` 样式或 class。"""
    values: list[float] = []
    for node in card.find_all(["del", "s", "strike"]):
        values.extend(extract_prices(node.get_text(" ")))
    if values:
        return _unique(values)

    for node in card.find_all(style=True):
        if "line-through" in (node.get("style") or "").lower():
            values.extend(extract_prices(node.get_text(" ")))

    for node in card.find_all(attrs={"class": True}):
        classes = " ".join(node.get("class") or []).lower()
        if any(
            key in classes
            for key in ("original", "line-through", "strikethrough", "old-price", "del-price", "compare")
        ):
            values.extend(extract_prices(node.get_text(" ")))

    return _unique(values)


def _is_badge_image(img: Tag) -> bool:
    url = " ".join(
        str(img.get(attr) or "")
        for attr in ("src", "data-src", "srcset", "data-srcset", "class")
    ).lower()
    return any(hint in url for hint in BADGE_IMAGE_HINTS)


def extract_image_url(card: Tag, base_url: str = "", title: str = "") -> str:
    """取商品主图。优先和商品名一致的大图，跳过图标 / 认证角标。"""
    candidates: list[tuple[int, str]] = []

    def add(url: str, score: int) -> None:
        if url:
            candidates.append((score, url))

    for position, img in enumerate(card.find_all("img")):
        score = 0
        if _is_badge_image(img):
            score -= 10
        alt = clean_text(img.get("alt"))
        img_title = clean_text(img.get("title"))
        if title and (alt == title or img_title == title):
            score += 5
        elif len(alt) >= 12:
            score += 2
        if position == 0:
            score += 1

        for attr in ("src", "data-src", "data-lazy-src", "data-original", "data-image", "data-echo", "file"):
            url = abs_url(img.get(attr), base_url)
            if url:
                add(url, score)
                break
        else:
            for attr in ("srcset", "data-srcset"):
                srcset = img.get(attr)
                if not srcset:
                    continue
                parts = [piece.strip().split(" ")[0] for piece in srcset.split(",") if piece.strip()]
                for candidate in reversed(parts):
                    url = abs_url(candidate, base_url)
                    if url:
                        add(url, score)
                        break
                break
            else:
                match = re.search(r"url\((['\"]?)(.*?)\1\)", img.get("style") or "")
                if match:
                    add(abs_url(match.group(2), base_url), score)

    if candidates:
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1]

    for node in card.find_all(style=True):
        match = re.search(r"url\((['\"]?)(.*?)\1\)", node.get("style") or "")
        if match:
            url = abs_url(match.group(2), base_url)
            if url:
                return url
    return ""


def extract_product_url(card: Tag, base_url: str, keywords: Iterable[str]) -> str:
    """在卡片里找商品详情链接。"""
    keywords = tuple(str(keyword).lower() for keyword in keywords)
    for anchor in card.find_all("a", href=True):
        href = (anchor.get("href") or "").strip()
        if any(keyword in href.lower() for keyword in keywords):
            return abs_url(href, base_url)

    for anchor in card.find_all("a", href=True):
        href = abs_url(anchor.get("href"), base_url)
        if href and "tiktok.com" in urlparse(href).netloc:
            return href
    return ""


def extract_title(card: Tag, lines: list[str], product_url: str = "") -> str:
    """商品名称，按可靠性从高到低尝试。"""
    # 1) 商品详情链接里的标题（<h3 title="..."> 或 <a title="...">）
    if product_url:
        for anchor in card.find_all("a", href=True):
            href = abs_url(anchor.get("href"), "https://shop.tiktok.com")
            if href and href == product_url:
                for node in [anchor, *anchor.find_all(attrs={"title": True})]:
                    candidate = clean_text(node.get("title"))
                    if len(candidate) >= 6:
                        return candidate[:300]
                text = clean_text(anchor.get_text(" "))
                if len(text) >= 6:
                    return text[:300]

    # 2) 图片 alt / title
    for img in card.find_all("img"):
        for attr in ("alt", "title"):
            candidate = clean_text(img.get(attr))
            if len(candidate) >= 8 and not _looks_like_noise(candidate):
                return candidate[:300]

    # 3) 任意带 title 的元素
    for node in card.find_all(attrs={"title": True}):
        candidate = clean_text(node.get("title"))
        if len(candidate) >= 8 and not _looks_like_noise(candidate):
            return candidate[:300]

    # 4) 最长的有效文本行
    candidates = [line for line in lines if not _looks_like_noise(line) and len(line) <= 260]
    if candidates:
        return max(candidates, key=len)[:300]
    return ""


def extract_shop(card: Tag, lines: list[str], title: str, product_url: str = "") -> str:
    """店铺名称。

    实测结构里，店铺名是"商品链接所在容器的上一个兄弟节点"里的文本，
    所以优先用这种**结构关系**定位，其次才用 class 语义和文本兜底。
    """
    # 1) 结构关系：从商品链接往上找，看每一层的上一个兄弟节点
    if product_url:
        for anchor in card.find_all("a", href=True):
            href = abs_url(anchor.get("href"), "https://shop.tiktok.com")
            if href != product_url:
                continue
            node: Tag | None = anchor
            for _ in range(4):
                if node is None:
                    break
                sibling = node.find_previous_sibling()
                hops = 0
                while sibling is not None and hops < 3:
                    name = clean_text(sibling.get_text(" "))
                    if 1 < len(name) <= 60 and name != title and not _looks_like_noise(name):
                        return name[:80]
                    sibling = sibling.find_previous_sibling()
                    hops += 1
                node = node.parent

    # 2) 店铺链接 / 语义 class
    for anchor in card.find_all("a", href=True):
        href = (anchor.get("href") or "").lower()
        if (
            any(key in href for key in ("/shop/", "/store/", "/seller/", "/brand/"))
            or href.startswith("@")
            or "/@" in href
        ):
            name = clean_text(anchor.get_text(" "))
            if 1 < len(name) <= 60:
                return name[:80]

    for node in card.find_all(attrs={"class": True}):
        classes = " ".join(node.get("class") or []).lower()
        if any(
            key in classes
            for key in ("shop-name", "shopname", "store-name", "seller-name", "brand-name")
        ):
            name = clean_text(node.get_text(" "))
            if 1 < len(name) <= 60 and name != title:
                return name[:80]

    # 3) 文本兜底：只看"价格之前"的文本（店铺名在价格上方，倒计时 / 价格在下方）
    price_index = next(
        (index for index, line in enumerate(lines) if PRICE_RE.search(line)),
        len(lines),
    )
    above_price = lines[:price_index]

    # 3a) 标题上方的短行（最常见的布局：店铺名 → 商品名）
    title_index = above_price.index(title) if title in above_price else len(above_price)
    for line in above_price[:title_index]:
        if _looks_like_shop_name(line, title):
            return line[:80]

    # 3b) 标题下方、价格上方的短行（另一种布局）
    for line in above_price[title_index + 1 :]:
        if _looks_like_shop_name(line, title):
            return line[:80]
    return ""


# ---------------------------------------------------------------------------
# 组装记录
# ---------------------------------------------------------------------------
def build_product_record(
    card_html: str,
    fallback_url: str = "",
    base_url: str = "",
    keywords: Iterable[str] = (),
    index: int = 0,
    crawl_time: str | None = None,
) -> dict[str, Any]:
    """把一张商品卡片的 HTML 解析成一条记录。"""
    card = BeautifulSoup(card_html or "", "lxml")
    node: Tag = card.body or card

    # 分行文本用于标题 / 店铺 / 销量 / 评分；不分行文本用于价格
    # （TikTok 会把 "$16.97" 拆成 $ / 16 / . / 97 四个节点）
    line_text = node.get_text("\n")
    flat_text = node.get_text("")
    lines = text_lines(line_text)

    product_url = extract_product_url(node, base_url, keywords) or abs_url(fallback_url, base_url)
    title = extract_title(node, lines, product_url)

    all_prices = extract_prices(flat_text)
    struck = _strikethrough_prices(node)

    current_price: float | None = None
    original_price: float | None = None
    if struck:
        original_price = max(struck)
        others = [price for price in all_prices if price != original_price]
        if others:
            current_price = min(others)
    elif len(all_prices) >= 2:
        current_price, original_price = min(all_prices), max(all_prices)
        if current_price == original_price:
            original_price = None
    elif all_prices:
        current_price = all_prices[0]

    sold_count, sold_raw = extract_sold(line_text)

    return {
        "rank": index + 1,
        "product_name": title,
        "shop_name": extract_shop(node, lines, title, product_url),
        "product_url": product_url,
        "image_url": extract_image_url(node, base_url, title),
        "rating": extract_rating(line_text, card=node, lines=lines),
        "sold_count": sold_count,
        "sold_text": sold_raw,
        "current_price": current_price,
        "original_price": original_price,
        "discount": extract_discount(flat_text, current_price, original_price),
        "crawl_time": crawl_time or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ---------------------------------------------------------------------------
# 排名与去重
# ---------------------------------------------------------------------------
def product_key(record: dict[str, Any]) -> str:
    """去重键：优先商品 ID（URL），其次商品名。"""
    url = record.get("product_url") or ""
    if url:
        path = urlparse(url).path.rstrip("/")
        if path:
            return path.lower()
    name = clean_text(record.get("product_name") or "")
    return name.lower() if name else ""


def dedupe_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按出现顺序去重，保留第一条。"""
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for record in records:
        key = product_key(record)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(record)
    return result


def assign_ranks(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按页面出现顺序重新编号 1..N。"""
    for position, record in enumerate(records, start=1):
        record["rank"] = position
    return records


def is_meaningful(record: dict[str, Any]) -> bool:
    """至少有名字或链接、并且有图片或价格，才算一条有效商品记录。"""
    has_identity = bool(clean_text(record.get("product_name")) or record.get("product_url"))
    has_detail = bool(record.get("image_url") or record.get("current_price"))
    return has_identity and has_detail


# ---------------------------------------------------------------------------
# PDP（商品详情页）解析：读取商品一级类别
# ---------------------------------------------------------------------------
_MODERN_ROUTER_DATA_RE = re.compile(
    r'<script[^>]*id="__MODERN_ROUTER_DATA__"[^>]*>(.*?)</script>',
    re.S,
)

#: og:image meta（属性顺序不定，两个方向都匹配）
_OG_IMAGE_PROP_FIRST_RE = re.compile(
    r'<meta[^>]*?property=["\']og:image["\'][^>]*?content=["\']([^"\']+)["\']',
    re.I,
)
_OG_IMAGE_CONTENT_FIRST_RE = re.compile(
    r'<meta[^>]*?content=["\']([^"\']+)["\'][^>]*?property=["\']og:image["\']',
    re.I,
)


def _first_image_url(product_info: dict) -> str:
    """从 product_info 里按常见字段名取第一张商品主图 URL。"""
    if not isinstance(product_info, dict):
        return ""
    for key in ("images", "image", "main_image", "product_images", "media", "image_list"):
        value = product_info.get(key)
        if not value:
            continue
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    candidate = (
                        item.get("url")
                        or item.get("main_url")
                        or item.get("src")
                        or item.get("image_url")
                        or item.get("main_image_url")
                    )
                else:
                    candidate = item
                if isinstance(candidate, str) and candidate.startswith("http"):
                    return candidate
        elif isinstance(value, str) and value.startswith("http"):
            return value
    return ""


def extract_product_image_from_pdp(html_text: str) -> str:
    """从 PDP 页面 HTML 提取商品主图 URL（列表页懒加载缺图时兜底）。

    优先读 og:image meta；读不到再依次从 __MODERN_ROUTER_DATA__ 的
    global_data.product_info 与 components_map 的 product_model 里取第一张图。
    返回 URL，读不到返回空字符串。
    """
    if not html_text:
        return ""
    text = html_lib.unescape(html_text)
    for pattern in (_OG_IMAGE_PROP_FIRST_RE, _OG_IMAGE_CONTENT_FIRST_RE):
        for match in pattern.finditer(text):
            url = match.group(1).strip()
            if url.startswith("http"):
                return url
    match = _MODERN_ROUTER_DATA_RE.search(text)
    if not match:
        return ""
    try:
        data = json.loads(match.group(1))
    except Exception:
        return ""
    loader = data.get("loaderData") or {}
    for route, page in loader.items():
        if not isinstance(page, dict) or "pdp" not in route:
            continue
        try:
            global_data = (page.get("page_config") or {}).get("global_data") or {}
            url = _first_image_url(global_data.get("product_info") or {})
            if url:
                return url
        except AttributeError:
            pass
        # components_map 里的 product_model.images 也存主图
        try:
            components = (page.get("page_config") or {}).get("components_map") or []
        except AttributeError:
            continue
        if isinstance(components, dict):
            components = list(components.values())
        if not isinstance(components, list):
            continue
        for comp in components:
            if not isinstance(comp, dict):
                continue
            try:
                product_info = (comp.get("component_data") or {}).get("product_info") or {}
                pm = product_info.get("product_model") or {}
            except AttributeError:
                continue
            url = _first_image_url(pm) or _first_image_url(product_info)
            if url:
                return url
    return ""


def extract_category_from_pdp(html_text: str) -> dict[str, str] | None:
    """从 PDP 页面 HTML 里读取一级商品类别。

    数据源：__MODERN_ROUTER_DATA__ 内嵌 JSON 的
    global_data.product_info.categories（分类树按 level 递增排列），
    取 level == 1（或 parent_id == "0"）的一级类目。

    返回 {"category_id": str, "category_name": str}；
    解析不到返回 None（调用方自行兜底）。
    """
    if not html_text:
        return None
    text = html_lib.unescape(html_text)
    match = _MODERN_ROUTER_DATA_RE.search(text)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except Exception:
        return None
    loader = data.get("loaderData") or {}
    for route, page in loader.items():
        if not isinstance(page, dict) or "pdp" not in route:
            continue
        try:
            global_data = (page.get("page_config") or {}).get("global_data") or {}
            categories = (global_data.get("product_info") or {}).get("categories") or []
        except AttributeError:
            continue
        if not isinstance(categories, list):
            continue
        for cat in categories:
            if not isinstance(cat, dict):
                continue
            category_id = str(cat.get("category_id") or "")
            level = str(cat.get("level") or "")
            parent_id = str(cat.get("parent_id") or "")
            if level == "1" or parent_id == "0":
                if category_id:
                    return {
                        "category_id": category_id,
                        "category_name": str(cat.get("category_name") or ""),
                    }
    return None


#: 详情介绍区域的标题（覆盖英文与中文界面）
_DESCRIPTION_TITLES = (
    "product description",
    "商品描述",
    "商品详情介绍",
    "商品详情",
    "description",
)

#: 介绍容器里可能出现的其它区块标题（提取正文时一并去掉）
_DESCRIPTION_NOISE = (
    "about this product",
    "details",
    "safety & compliance",
    "shipping & returns",
    "select options",
    "关于本产品",
    "商品详情",
    "安全与合规",
)


def _extract_description_from_ssr(html_text: str) -> str:
    """从 __MODERN_ROUTER_DATA__ 的 components_map 读完整 product_model.description。

    description 是 JSON 字符串数组，元素类型：text（{"text": "..."}）、
    ul（{"content": ["...", ...]}）、image（无文字，跳过）。
    只拼接 text 与 ul 的文字，多行文本返回；读不到返回空字符串。
    """
    text = html_lib.unescape(html_text)
    match = _MODERN_ROUTER_DATA_RE.search(text)
    if not match:
        return ""
    try:
        data = json.loads(match.group(1))
    except Exception:
        return ""
    loader = data.get("loaderData") or {}
    for route, page in loader.items():
        if not isinstance(page, dict) or "pdp" not in route:
            continue
        try:
            components = (page.get("page_config") or {}).get("components_map") or []
        except AttributeError:
            continue
        if isinstance(components, dict):
            components = list(components.values())
        if not isinstance(components, list):
            continue
        for comp in components:
            if not isinstance(comp, dict):
                continue
            try:
                product_info = (comp.get("component_data") or {}).get("product_info") or {}
                pm = product_info.get("product_model") or {}
            except AttributeError:
                continue
            desc_raw = pm.get("description")
            if not desc_raw:
                continue
            if isinstance(desc_raw, str):
                try:
                    desc_raw = json.loads(desc_raw)
                except Exception:
                    continue
            if not isinstance(desc_raw, list):
                continue
            lines: list[str] = []
            for item in desc_raw:
                if not isinstance(item, dict):
                    continue
                kind = item.get("type")
                if kind == "text":
                    piece = item.get("text")
                    if isinstance(piece, str) and piece.strip():
                        lines.append(piece.strip())
                elif kind == "ul":
                    content = item.get("content")
                    if isinstance(content, list):
                        for li in content:
                            if isinstance(li, str) and li.strip():
                                lines.append("・" + li.strip())
            joined = "\n".join(lines).strip()
            if joined:
                return joined
    return ""


def extract_product_description(html_text: str) -> str:
    """从 PDP 页面 HTML 提取『商品详情介绍』长文本。

    优先读 SSR 内嵌 JSON（components_map 里 product_model.description 的文字与
    列表项，不依赖异步渲染）；读不到时退回 DOM 解析：找 "Product description"
    （中文界面“商品描述”）标题，取该标题之后、同一容器内的正文文本。
    找不到时返回空字符串。
    """
    if not html_text:
        return ""
    ssr_desc = _extract_description_from_ssr(html_text)
    if ssr_desc:
        return ssr_desc
    soup = BeautifulSoup(html_text, "lxml")
    candidates: list[str] = []
    for node in soup.find_all(["h1", "h2", "h3", "h4", "div", "span", "p"]):
        title_text = clean_text(node.get_text(" ", strip=True))
        if not title_text:
            continue
        lower_title = title_text.lower()
        if lower_title not in _DESCRIPTION_TITLES and not lower_title.startswith(
            "product description"
        ):
            continue
        container = node.parent
        if container is None:
            continue
        full = clean_text(container.get_text(" ", strip=True))
        if not full:
            continue
        pos = full.lower().find(lower_title)
        body = full[pos + len(lower_title) :] if pos >= 0 else full
        for noise in _DESCRIPTION_NOISE:
            body = body.replace(noise, "")
        body = clean_text(body)
        if body:
            candidates.append(body)
    if not candidates:
        return ""
    # 取最长的候选（最可能是完整介绍段落）
    return max(candidates, key=len)


# ---------------------------------------------------------------------------
# PDP（商品详情页）解析：读取『Tiktok选品助手』插件注入的上架时间
# ---------------------------------------------------------------------------
#: 插件浮层的根节点 id（扩展「Tiktok选品助手」/ tiktokshuju.com 在 PDP 注入）
LISTING_TIME_ROOT_ID = "tiktokshuju-goods-detail-root"


def extract_listing_time(html_text: str) -> dict[str, str] | None:
    """从 PDP 页面 HTML 读取『Tiktok选品助手』插件浮层注入的上架时间。

    插件在商品详情页注入的浮层结构（根节点 ``div#tiktokshuju-goods-detail-root``），
    其中「上架时间」一行为::

        <span class="label" title="商品预估上架时间">上架时间:</span>
        <span class="value" title="2025-08-09 15:50:59">1年前</span>

    —— 可见文本是相对表述（如"1年前"），精确时间（预估）在 value 的
    ``title`` 属性里。本函数返回::

        {"listing_time": "2025-08-09 15:50:59", "listing_time_label": "1年前"}

    找不到插件浮层、或其中没有「上架时间」条目时返回 None（调用方自行兜底）。
    """
    if not html_text:
        return None
    soup = BeautifulSoup(html_text, "lxml")
    root = soup.find(id=LISTING_TIME_ROOT_ID)
    if root is None:
        return None

    for label in root.find_all("span", attrs={"title": True}):
        label_title = clean_text(label.get("title") or "")
        label_text = clean_text(label.get_text(" ", strip=True))
        if "上架时间" not in (label_title + label_text):
            continue

        # value 优先取 label 的下一个兄弟 span；否则在 label 父节点内找 class 含 value 的 span
        value_node: Tag | None = label.find_next_sibling("span")
        if value_node is None and label.parent is not None:
            value_node = label.parent.find("span", class_=re.compile(r"value"))
        if value_node is None:
            return None

        result: dict[str, str] = {}
        value_title = clean_text(value_node.get("title"))
        if value_title:
            result["listing_time"] = value_title
        value_text = clean_text(value_node.get_text(" ", strip=True))
        if value_text:
            result["listing_time_label"] = value_text
        return result or None
    return None
