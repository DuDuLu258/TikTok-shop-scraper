"""TikTok Shop US 热销商品采集工具 —— 命令行入口。

用法：

    python main.py                      # 抓 Top 100（无头浏览器）
    python main.py --headed             # 显示浏览器窗口，方便观察
    python main.py --max 50             # 只抓 50 个
    python main.py --debug              # 额外保存截图 / HTML / JSON
    python main.py --category Beauty    # 先切到某个分类标签
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

import config
from excel_export import export_to_excel, timestamped_copy_path
from scraper import (
    NoProductFoundError,
    PageOpenError,
    ScraperError,
    SecurityCheckError,
    TikTokRankingScraper,
)


def setup_logging(level: str, log_dir: Path) -> Path:
    """同时输出到控制台和日志文件，返回日志文件路径。"""
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"scrape_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.addHandler(console)
    root.addHandler(file_handler)
    return log_file


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="TikTok Shop US 热销榜采集工具（Playwright + BeautifulSoup + openpyxl）",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("--url", default=config.URL, help=f"目标页面（默认 {config.URL}）")
    parser.add_argument(
        "--max",
        dest="max_products",
        type=int,
        default=config.MAX_PRODUCTS,
        help=f"最多抓取多少个商品（默认 {config.MAX_PRODUCTS}）",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="显示浏览器窗口（调试页面结构时推荐）",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="保存截图 / HTML / JSON 到 debug/ 目录，并输出更详细的日志",
    )
    parser.add_argument(
        "--category",
        default=None,
        help=(
            "可选：只抓指定类别的商品（一次一个类别）。\n"
            "支持中文名 / 英文名 / slug，例如：\n"
            "  --category 美妆个护\n"
            "  --category \"Beauty & Personal Care\"\n"
            "  --category beauty-personal-care\n"
            "可用类别：\n  - " + "\n  - ".join(config.list_categories())
        ),
    )
    parser.add_argument(
        "--min-sold",
        type=int,
        default=None,
        help=(
            "可选：只保留销量不低于该数值的商品（销量下限过滤）。\n"
            "可与 --category 组合使用，例如：\n"
            "  --category 美妆个护 --min-sold 490000\n"
            "表示：只抓『美妆个护』类别下销量 ≥ 490000 的商品。\n"
            "榜单卡片解析不到销量的商品会被跳过。"
        ),
    )
    parser.add_argument(
        "--max-age-days",
        type=int,
        default=None,
        help=(
            "可选：只保留上架不超过 N 天的商品（上架时间筛选）。\n"
            "数据来自『Tiktok选品助手』插件在商品详情页注入的预估上架时间，\n"
            "需要先把该插件安装到「启动Chrome.bat」启动的 Chrome 里。\n"
            "示例：--max-age-days 30 表示只抓近 30 天上架的商品；\n"
            "插件未注入上架时间的商品会被跳过。"
        ),
    )
    parser.add_argument(
        "--min-age-days",
        type=int,
        default=None,
        help=(
            "可选：只保留上架不少于 N 天的商品（与 --max-age-days 组合成区间）。\n"
            "示例：--min-age-days 30 --max-age-days 180 表示只抓上架 30~180 天的商品。"
        ),
    )
    parser.add_argument(
        "--wait-verify",
        action="store_true",
        help="遇到滑块安全验证时，等待你在浏览器里手动通过（会自动切换到可见窗口，不设超时）",
    )
    parser.add_argument(
        "--strict-verify",
        action="store_true",
        help="遇到安全验证不要自动打开浏览器窗口，直接报错退出（默认会自动切到可见窗口）",
    )
    parser.add_argument(
        "--no-profile",
        action="store_true",
        help="不使用固定浏览器用户目录（Cookie 不保留，一般不建议）",
    )
    parser.add_argument(
        "--profile-dir",
        default=str(config.PROFILE_DIR),
        help="浏览器用户目录（默认 .browser_profile）",
    )
    parser.add_argument(
        "--out",
        default=str(config.OUTPUT_DIR / config.EXCEL_FILENAME),
        help="Excel 输出路径",
    )
    parser.add_argument("--no-copy", action="store_true", help="不额外保存带时间戳的历史副本")
    return parser


def print_summary(
    products: list[dict],
    excel_path: Path,
    elapsed: float,
    category_label: str | None = None,
) -> None:
    print()
    print("=" * 68)
    title = f"抓取完成：{len(products)} 个商品，用时 {elapsed:.1f} 秒"
    if category_label:
        title += f"（类别：{category_label}）"
    print(title)
    print(f"Excel 文件：{excel_path}")
    print("=" * 68)

    preview = products[:5]
    if not preview:
        return
    print("前 5 条预览：")
    for item in preview:
        price = item.get("current_price")
        price_text = f"${price:.2f}" if isinstance(price, (int, float)) else "-"
        sold = item.get("sold_count")
        sold_text = f"{sold:,}" if isinstance(sold, int) else "-"
        print(
            f"  #{item.get('rank'):>3} {str(item.get('product_name'))[:44]:<44} "
            f"{price_text:>9}  销量 {sold_text:>7}"
        )
    print()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    level = "DEBUG" if args.debug else config.LOG_LEVEL
    log_file = setup_logging(level, config.LOG_DIR)
    log = logging.getLogger("main")

    log.info("TikTok Shop US 热销榜采集开始（目标 %d 个商品）", args.max_products)
    log.info("日志文件：%s", log_file)

    if args.wait_verify and not args.headed:
        log.info("--wait-verify 需要可见的浏览器窗口，已自动切换到 headed 模式。")
        args.headed = True

    if args.no_profile:
        args.profile_dir = None

    started = time.time()

    category_label: str | None = None
    if args.category:
        info = config.resolve_category(args.category)
        category_label = (
            f"{info['name_zh']}（{info['name_en']}）" if info else str(args.category)
        )
        if info:
            log.info("目标类别：%s（%s）", info["name_zh"], info["name_en"])
        else:
            log.warning(
                "类别「%s」不在内置映射中，无法按类别过滤，"
                "将按全站榜单抓取（并尝试点击页面上的同名分类标签）。"
                "可运行 python main.py --help 查看支持的类别。",
                args.category,
            )
    if args.min_sold is not None:
        log.info("销量下限：%s", f"{args.min_sold:,}")
    if args.max_age_days is not None:
        log.info("上架时间上限：不超过 %d 天", args.max_age_days)
    if args.min_age_days is not None:
        log.info("上架时间下限：不少于 %d 天", args.min_age_days)

    try:
        with TikTokRankingScraper(
            url=args.url,
            max_products=args.max_products,
            headless=not args.headed,
            debug=args.debug,
            category=args.category,
            min_sold=args.min_sold,
            max_listing_age_days=args.max_age_days,
            min_listing_age_days=args.min_age_days,
            wait_for_verify=args.wait_verify,
            strict_verify=args.strict_verify,
            use_profile=not args.no_profile,
            profile_dir=args.profile_dir or config.PROFILE_DIR,
        ) as scraper:
            products = scraper.run()

        # 写主文件；若文件正被 Excel / WPS 打开导致占用，自动改存到带时间戳的新文件
        excel_path = args.out
        main_saved = False
        try:
            excel_path = export_to_excel(products, args.out, category_label=category_label)
            main_saved = True
        except PermissionError:
            fallback = timestamped_copy_path(Path(args.out).parent, Path(args.out).name)
            log.warning(
                "输出文件被占用（可能正被 Excel / WPS 打开）：%s\n自动改存为：%s",
                args.out,
                fallback,
            )
            excel_path = export_to_excel(products, fallback, category_label=category_label)

        if config.SAVE_TIMESTAMPED_COPY and not args.no_copy and main_saved:
            copy_path = timestamped_copy_path(Path(args.out).parent, Path(args.out).name)
            try:
                export_to_excel(products, copy_path, category_label=category_label)
                log.info("历史副本已保存：%s", copy_path)
            except PermissionError:
                log.warning("历史副本被占用，跳过保存：%s", copy_path)

        print_summary(
            products,
            excel_path,
            time.time() - started,
            category_label=category_label,
        )
        return 0

    except PageOpenError as exc:
        log.error("页面打开失败：\n%s", exc)
        return 2
    except NoProductFoundError as exc:
        log.error("没有抓到商品：\n%s", exc)
        return 3
    except SecurityCheckError as exc:
        log.error("安全验证未通过：\n%s", exc)
        return 5
    except ScraperError as exc:
        log.error("采集过程出错：\n%s", exc)
        return 4
    except KeyboardInterrupt:
        log.warning("用户中断，程序退出。")
        return 130
    except Exception as exc:  # noqa: BLE001 - 兜底，保证错误信息清晰
        log.exception("未预期的错误：%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
