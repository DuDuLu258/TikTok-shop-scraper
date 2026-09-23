"""页面结构诊断脚本（先跑这个，再跑 main.py）。

它做的事情：

1. 打开 TikTok Shop US 榜单页并截图；
2. 保存完整 HTML；
3. 统计候选容器的数量（含 product / card / item 等语义的 class）；
4. 自动推断商品卡片，打印前几条的文本、链接、图片；
5. 打印『查看更多 / View More』按钮的候选元素；
6. 导出页面里的 JSON 数据块（如果有）。

输出目录：``debug/``

用法：

    python inspect_page.py --headed
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import Counter
from datetime import datetime
from typing import Any

from bs4 import BeautifulSoup

import config
from page_parser import build_product_record
from scraper import JS_COLLECT_CARDS, JS_MARK_BY_TEXT, JS_PAGE_STATS, TikTokRankingScraper


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )


def section(title: str) -> None:
    print()
    print("-" * 72)
    print(title)
    print("-" * 72)


def analyze_html(html: str, base_url: str) -> dict[str, Any]:
    """对页面 HTML 做结构与文本统计。"""
    soup = BeautifulSoup(html, "lxml")

    # 1) 语义 class 统计：哪些容器看起来像商品卡片
    class_counter: Counter[str] = Counter()
    for node in soup.find_all(attrs={"class": True}):
        for name in node.get("class") or []:
            if re.search(r"product|card|item|goods|rank|list|grid", name, re.I):
                class_counter[name] += 1

    # 2) 商品链接
    product_anchors = [
        a.get("href", "")
        for a in soup.find_all("a", href=True)
        if re.search(r"/product|/view/product|/goods/|/p/\d", a.get("href", ""), re.I)
    ]

    # 3) 打分找卡片：含图片 + 有链接/文本，且文本里出现价格或 sold
    candidates: list[tuple[int, Any]] = []
    for node in soup.find_all(["div", "li", "article", "section"]):
        text = node.get_text(" ", strip=True)
        if not (10 <= len(text) <= 800):
            continue
        imgs = node.find_all("img")
        if not (1 <= len(imgs) <= 3):
            continue
        links = node.find_all("a", href=True)
        if len(links) > 4:
            continue

        score = 0
        if re.search(r"\$\s?\d", text):
            score += 2
        if re.search(r"\d\s*%|sold", text, re.I):
            score += 2
        if re.search(r"/product|/view/product|/goods/|/p/\d", " ".join(a.get("href", "") for a in links), re.I):
            score += 3
        classes = " ".join(node.get("class") or [])
        if re.search(r"product|card|item|goods", classes, re.I):
            score += 1
        if score >= 2:
            candidates.append((score, node))

    # 4) 内嵌 JSON 数据块
    json_blobs: list[str] = []
    for script in soup.find_all("script"):
        content = script.string or ""
        if not content:
            continue
        if any(key in content for key in ("SIGI_STATE", "__NEXT_DATA__", "UNIVERSAL_DATA", "productList", "ranking")):
            json_blobs.append(f"<script id={script.get('id')!r} type={script.get('type')!r} len={len(content)}>")

    return {
        "class_hits": class_counter.most_common(25),
        "product_anchor_count": len(product_anchors),
        "product_anchor_samples": product_anchors[:10],
        "card_candidates": candidates[:8],
        "candidate_count": len(candidates),
        "json_blobs": json_blobs[:20],
    }


def print_analysis(analysis: dict[str, Any], base_url: str) -> None:
    section("1. 看起来像商品卡片的 class（出现次数 Top 25）")
    if analysis["class_hits"]:
        for name, count in analysis["class_hits"]:
            print(f"  {count:>5}  {name}")
    else:
        print("  （没有匹配到 product/card/item 语义的 class）")

    section("2. 商品链接锚点")
    print(f"  命中数量：{analysis['product_anchor_count']}")
    for href in analysis["product_anchor_samples"]:
        print(f"    {href}")

    section(f"3. 候选商品卡片：{analysis['candidate_count']} 个（展示前 8 个）")
    for index, (score, node) in enumerate(analysis["card_candidates"], start=1):
        record = build_product_record(
            card_html=str(node),
            base_url=base_url,
            keywords=config.PRODUCT_URL_KEYWORDS,
            index=index - 1,
        )
        print(f"\n  [{index}] 评分 {score}")
        print(f"      product_name : {record['product_name'][:80]!r}")
        print(f"      shop_name    : {record['shop_name']!r}")
        print(f"      current_price: {record['current_price']}")
        print(f"      original_price: {record['original_price']}")
        print(f"      discount     : {record['discount']}")
        print(f"      sold_count   : {record['sold_count']} ({record['sold_text']!r})")
        print(f"      rating       : {record['rating']}")
        print(f"      image_url    : {record['image_url'][:90]!r}")
        print(f"      product_url  : {record['product_url'][:90]!r}")
        text = node.get_text(" | ", strip=True)
        print(f"      原始文本     : {text[:220]}")

    section("4. 页面里的 JSON 数据块")
    if analysis["json_blobs"]:
        for blob in analysis["json_blobs"]:
            print(f"  {blob}")
    else:
        print("  （没有发现已知的内嵌状态数据）")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="页面结构诊断工具")
    parser.add_argument("--url", default=config.URL)
    parser.add_argument("--headed", action="store_true", help="显示浏览器窗口")
    parser.add_argument("--scroll", type=int, default=4, help="滚动轮数（默认 4）")
    parser.add_argument(
        "--wait-verify",
        action="store_true",
        help="遇到滑块验证时等待手动通过（会自动切换到可见窗口，不设超时）",
    )
    parser.add_argument("--strict-verify", action="store_true", help="遇到验证直接报错退出")
    args = parser.parse_args(argv)

    setup_logging()
    log = logging.getLogger("inspect")

    if args.wait_verify and not args.headed:
        log.info("--wait-verify 需要可见窗口，已自动切换到 headed 模式。")
        args.headed = True

    out_dir = config.DEBUG_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    scraper = TikTokRankingScraper(
        url=args.url,
        max_products=10,
        headless=not args.headed,
        debug=True,
        wait_for_verify=args.wait_verify,
        strict_verify=args.strict_verify,
    )
    scraper._start_browser()
    try:
        page = scraper._prepare_verified_page()
        scraper._hide_mouse(page)

        # 先抓一次未滚动的信息
        stats_before = page.evaluate(JS_PAGE_STATS, scraper._section_args())
        log.info("初始统计：%s", stats_before)

        scraper._human_scroll(page, iterations=args.scroll)

        stats_after = page.evaluate(JS_PAGE_STATS, scraper._section_args())
        log.info("滚动后统计：%s", stats_after)

        html = page.content()
        html_file = out_dir / f"page_{stamp}.html"
        html_file.write_text(html, encoding="utf-8")
        log.info("HTML 已保存：%s", html_file)

        screenshot = out_dir / f"page_{stamp}.png"
        page.screenshot(path=str(screenshot), full_page=False)
        log.info("截图已保存：%s", screenshot)

        full_screenshot = out_dir / f"page_full_{stamp}.png"
        try:
            page.screenshot(path=str(full_screenshot), full_page=True)
            log.info("整页截图已保存：%s", full_screenshot)
        except Exception as exc:  # pragma: no cover
            log.warning("整页截图失败（页面很长时常见）：%s", exc)

        # JS 卡片采集结果
        cards = page.evaluate(JS_COLLECT_CARDS, scraper._section_args())
        cards_file = out_dir / f"cards_{stamp}.json"
        cards_file.write_text(
            json.dumps(cards, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        section(f"5. JS 卡片采集结果：{len(cards)} 张（明细见 {cards_file.name}）")
        for index, card in enumerate(cards[:5], start=1):
            print(f"\n  [{index}] url={card.get('url')}")
            print(f"      text={ (card.get('text') or '')[:200]!r}")

        # 结构分析
        analysis = analyze_html(html, args.url)
        print_analysis(analysis, args.url)

        section("6. 『查看更多 / View More』按钮探测")
        found = page.evaluate(
            JS_MARK_BY_TEXT,
            {
                "patterns": config.VIEW_MORE_TEXTS,
                "selector": 'button, a, [role="button"], [role="link"], div, span, p',
                "maxTextLen": 36,
            },
        )
        print(f"  找到的按钮文本：{found!r}")
        if found:
            element = page.query_selector('[data-tts-pick="1"]')
            if element:
                print(f"  外层 HTML：{element.evaluate('(el) => el.outerHTML.slice(0, 400)')}")
        else:
            print("  没有找到『查看更多』按钮（可能按钮在页面更下方、或已被滚动触发替换）")

        section("7. 商品解析抽样（用 JS 采集到的卡片）")
        for index, card in enumerate(cards[:5], start=1):
            record = build_product_record(
                card_html=card.get("html") or "",
                fallback_url=card.get("url") or "",
                base_url=args.url,
                keywords=config.PRODUCT_URL_KEYWORDS,
                index=index - 1,
            )
            print(f"  [{index}] {record['product_name'][:70]!r} | ${record['current_price']} | "
                  f"sold={record['sold_count']} | rating={record['rating']}")

        print()
        print("=" * 72)
        print(f"诊断完成。产物目录：{out_dir}")
        print("请把 debug/ 下的截图与上面的输出发我，用于确认 DOM 结构并微调解析规则。")
        print("=" * 72)
        return 0

    except Exception as exc:  # noqa: BLE001
        log.error("诊断失败：%s", exc)
        return 1
    finally:
        scraper.close()


if __name__ == "__main__":
    raise SystemExit(main())
