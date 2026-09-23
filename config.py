"""TikTok Shop 美国站热销榜采集工具 —— 全局配置。

所有可调参数集中在这里，修改后重新执行 ``python main.py`` 即可生效。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"   # Excel 输出目录
DEBUG_DIR = BASE_DIR / "debug"     # 调试截图 / HTML 快照目录
LOG_DIR = BASE_DIR / "logs"        # 运行日志目录

# ---------------------------------------------------------------------------
# 目标页面与抓取数量
# ---------------------------------------------------------------------------
#: 数据源：TikTok Shop 美区首页（"畅销商品"栏目）
#: 使用"从榜单页面包屑进入"的合法入口链接（enter_method=ranking_list_breadcrumb），
#: 比直接访问 /us 更容易通过风控（直接访问 /us 会被整页 Security Check 拦截）。
URL = "https://shop.tiktok.com/us?source=ecommerce_rankinglist&enter_method=ranking_list_breadcrumb&first_entrance_tt_scene=seo&btm_pre=a18064.b0.c0.d0&btm_pre_show_id=8fc1f5a9-b9b2-4880-ad67-8e53717029a4&btm_transfer_id=2892d93a-269c-46e9-a4aa-8d19de13b57b"

#: 数据源类型：homepage = 首页（只抓"畅销商品/Best Sellers"栏目），ranking = 榜单页（全页）
SOURCE_TYPE = "homepage"

#: "畅销商品"栏目标题文本（中英文都覆盖；页面语言由 URL 里的 lang 决定）
SECTION_TITLE_TEXTS = ["畅销商品", "best sellers", "best seller", "bestseller"]

# ---------------------------------------------------------------------------
# CDP 模式：连接真人 Chrome（用于访问首页 /us，绕过整页安全验证）
# ---------------------------------------------------------------------------
#: 连接地址。非空时程序不再自己启动浏览器，而是连接你手动启动的 Chrome
#: （需先用「启动Chrome.bat」或命令行带 --remote-debugging-port=9222 启动）。
#: 因为 TikTok 对首页 /us 会整页拦截所有自动化浏览器，只有真人浏览器能进；
#: 程序通过调试端口控制你的 Chrome，以真人浏览器身份抓取。
#: 置空字符串可关闭 CDP 模式，回到程序自己启动浏览器的旧逻辑。
CDP_URL: str | None = "http://127.0.0.1:9222"

#: 默认抓取 Top 100，命令行 ``--max`` 可覆盖
MAX_PRODUCTS = 100

#: 是否无头运行。首次运行 / 调试页面结构时建议改成 False，可以直接看浏览器。
HEADLESS = True

#: 浏览器视口
VIEWPORT = {"width": 1440, "height": 900}

#: 尽量贴近美国真实用户
LOCALE = "en-US"
TIMEZONE = "America/Los_Angeles"

#: 浏览器内核的尝试顺序：
#:   None    = Playwright 自带的 Chromium（需要先执行 playwright install chromium）
#:   "chrome"= 使用系统已安装的 Google Chrome
#:   "msedge"= 使用系统已安装的 Microsoft Edge
#: 按顺序尝试，第一个能启动成功的就用。这样即使没下载 Chromium 也能跑起来。
BROWSER_CHANNELS: tuple[str | None, ...] = (None, "chrome", "msedge")

#: User-Agent。留空（None）表示使用浏览器真实 UA —— 这是最安全的做法，
#: 因为写死的 UA 一旦和真实浏览器版本对不上，反而容易被风控识别。
#: 无头模式下程序会自动把 UA 里的 "HeadlessChrome" 修正成 "Chrome"。
USER_AGENT: str | None = None

# ---------------------------------------------------------------------------
# 浏览器用户目录（很关键）
# ---------------------------------------------------------------------------
#: 使用固定的用户数据目录：Cookie、验证状态会保留下来。
#: 手动通过一次安全验证后，后续运行通常就不再需要验证了。
USE_PERSISTENT_PROFILE = True
PROFILE_DIR = BASE_DIR / ".browser_profile"

# ---------------------------------------------------------------------------
# 安全验证（滑块 / 人机校验）处理
# ---------------------------------------------------------------------------
#: 命中这些标题时，认为页面被安全验证拦截
SECURITY_CHECK_TITLES = (
    "security check",
    "verify",
    "just a moment",
    "attention required",
)

#: 命中这些正文关键词时，认为页面被安全验证拦截
SECURITY_CHECK_TEXTS = (
    "verify to continue",
    "drag the puzzle piece",
    "are you a robot",
    "unusual traffic",
    "安全验证",
    "拖动滑块",
)

#: 等待人工完成验证的最长时间（秒）。
#: 0 = 一直等，直到验证通过或你自己按回车确认（推荐，避免你还在划滑块时窗口被关掉）。
VERIFY_WAIT_SECONDS = 0

#: 验证状态的轮询间隔（秒）
VERIFY_POLL_INTERVAL_S = 2.0

#: 无头模式遇到安全验证时，是否自动切换成可见浏览器让你手动通过。
#: True = 直接开窗口给你过（推荐，防止"再运行一次还是被拦"）
AUTO_HEADED_ON_VERIFY = True

# ---------------------------------------------------------------------------
# 循环与停止条件
# ---------------------------------------------------------------------------
#: "查看更多"按钮最多点击多少次（防止页面故障时无限循环）
MAX_VIEW_MORE_CLICKS = 40

#: 连续 N 轮既没有点出更多商品、滚动也加载不出新商品时，判定为抓取结束
NO_GROWTH_LIMIT = 3

#: 页面最多迭代多少轮"滚动 + 收集"（安全上限）
MAX_SCROLL_ROUNDS = 60

#: 单轮"滚动到底部"最多滚多少步（防止长页面把一轮拖到几分钟）
MAX_SCROLL_STEPS_PER_ROUND = 25


# ---------------------------------------------------------------------------
# 时间参数（毫秒 / 秒，都取区间随机值，避免机器味过重）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Timing:
    #: 打开页面的超时
    page_load_timeout_ms: int = 60_000
    #: 等待 DOM 就绪
    dom_ready_timeout_ms: int = 45_000
    #: 等待网络空闲（超时不报错，仅用于"尽量等满"）
    network_idle_timeout_ms: int = 25_000
    #: 单个元素等待
    element_wait_ms: int = 15_000

    #: 页面打开后的额外等待
    after_load_wait_s: tuple[float, float] = (2.5, 4.5)
    #: 每轮滚动的像素步长
    scroll_step_px: tuple[int, int] = (320, 720)
    #: 每次滚动后的停顿
    scroll_pause_s: tuple[float, float] = (1.1, 2.6)
    #: 偶发向上回滚（更像真人）
    back_scroll_px: tuple[int, int] = (40, 140)
    back_scroll_chance: float = 0.25

    #: 点击"查看更多"之前先停顿
    before_click_wait_s: tuple[float, float] = (0.8, 1.8)
    #: 点击之后的等待
    after_click_wait_s: tuple[float, float] = (2.0, 3.5)
    #: 点击后轮询"是否出现新商品"的总时长
    after_click_poll_ms: int = 15_000
    #: 轮询间隔
    poll_interval_ms: int = 600

    #: 每轮之间的随机思考时间
    between_rounds_s: tuple[float, float] = (1.5, 3.2)

    #: PDP 页面加载完成后的额外等待
    pdp_wait_s: tuple[float, float] = (0.8, 1.8)
    #: 两次 PDP 访问之间的随机间隔（拉长降低连续访问被风控限流的概率）
    pdp_interval_s: tuple[float, float] = (2.5, 5.0)


TIMING = Timing()

# ---------------------------------------------------------------------------
# "查看更多"按钮文本（覆盖中英文、不同写法；用包含匹配，不要求完全相等）
# ---------------------------------------------------------------------------
VIEW_MORE_TEXTS = [
    "view more",
    "viewmore",
    "see more",
    "seemore",
    "show more",
    "load more",
    "more products",
    "查看更多",
    "查看更多商品",
    "加载更多",
    "更多商品",
    "view all",
]

# ---------------------------------------------------------------------------
# 页面弹窗 / Cookie 提示的关闭按钮文本
# ---------------------------------------------------------------------------
DISMISS_TEXTS = [
    "accept all",
    "accept cookies",
    "accept",
    "agree",
    "got it",
    "allow all",
    "close",
    "no thanks",
    "not now",
    "later",
    "接受全部",
    "同意",
    "我知道了",
    "关闭",
]

# ---------------------------------------------------------------------------
# 商品类别（按类别抓取）
# ---------------------------------------------------------------------------
#: 每个类别以 URL slug 为键：(category_id, 英文名, 中文名)
#: 类目页 URL 格式：https://shop.tiktok.com/us/c/<slug>/<category_id>
#: 数据来源：TikTok Shop 公开类目结构（category_id 为平台一级类目 ID，长期稳定）
#: 榜单页分类导航的全部一级类别：
#: slug -> (category_id, 英文名, 中文名)
#: category_id 来自类目页 URL（https://shop.tiktok.com/us/c/<slug>/<id>），
#: 与 PDP 内嵌 JSON 里 level==1 的 category_id 一致，用于按类别过滤。
CATEGORIES: dict[str, tuple[str, str, str]] = {
    "beauty-personal-care": ("601450", "Beauty & Personal Care", "美妆个护"),
    "womenswear-underwear": ("601152", "Women's Wear & Underwear", "女装和内衣"),
    "menswear-underwear": ("824328", "Men's Wear & Underwear", "男装与男士内衣"),
    "phones-electronics": ("601739", "Phones & Electronics", "手机和电子产品"),
    "fashion-accessories": ("605248", "Fashion Accessories", "时尚配饰"),
    "collectibles": ("951432", "Collectibles", "收藏品"),
    "home-supplies": ("600001", "Home Supplies", "家居用品"),
    "kitchenware": ("600024", "Kitchenware", "厨具"),
    "shoes": ("601352", "Shoes", "鞋靴"),
    "sports-outdoor": ("603014", "Sports & Outdoor", "运动与户外"),
    "luggage-bags": ("824584", "Luggage & Bags", "箱包"),
    "toys-hobbies": ("604206", "Toys & Hobbies", "玩具和爱好"),
    "automotive-motorcycle": ("605196", "Automotive & Motorcycle", "汽车和摩托车相关"),
    "kids-fashion": ("802184", "Kids' Fashion", "儿童时尚"),
    "computers-office-equipment": ("601755", "Computers & Office Equipment", "电脑和办公设备"),
    "baby-maternity": ("602284", "Baby & Maternity", "母婴用品"),
    "tools-hardware": ("604579", "Tools & Hardware", "五金工具"),
    "textiles-soft-furnishings": ("600154", "Textiles & Soft Furnishings", "家纺布艺"),
    "pet-supplies": ("602118", "Pet Supplies", "宠物用品"),
    "home-improvement": ("604968", "Home Improvement", "家居装修"),
    "food-beverages": ("700437", "Food & Beverages", "食品饮料"),
    "modest-fashion": ("601303", "Modest Fashion", "穆斯林时尚"),
    "books-magazines-audio": ("801928", "Books, Magazines & Audio", "图书、杂志和音频"),
    "household-appliances": ("600942", "Household Appliances", "家电"),
    "health": ("700645", "Health", "保健"),
    "furniture": ("604453", "Furniture", "家具"),
    "jewelry-accessories-derivatives": ("953224", "Jewelry, Accessories & Derivatives", "珠宝与衍生品"),
    "pre-owned": ("856720", "Pre-owned", "二手"),
}

#: 类目页所在站点（当前工具只针对美国站）
CATEGORY_REGION = "us"


def category_page_url(slug: str, region: str = CATEGORY_REGION) -> str:
    """根据 slug 生成类目页 URL；slug 不存在时返回空串。"""
    info = CATEGORIES.get(slug)
    if not info:
        return ""
    return f"https://shop.tiktok.com/{region}/c/{slug}/{info[0]}"


#: 常见误写别名 → slug（避免输入笔误或旧叫法导致匹配不到）
CATEGORY_ALIASES: dict[str, str] = {
    "美妆个人": "beauty-personal-care",
    "女装": "womenswear-underwear",
    "男装": "menswear-underwear",
    "运动户外": "sports-outdoor",
    "健康": "health",
    "图书杂志和音频": "books-magazines-audio",
}


def resolve_category(name: str | None) -> dict[str, str] | None:
    """把用户输入的类别（中文名 / 英文名 / slug）解析成类目信息。

    返回 {"slug", "category_id", "name_en", "name_zh", "url"}；
    解析不到时返回 None（调用方可以退化为点击页面分类标签）。
    """
    if not name:
        return None
    key = str(name).strip().lower()
    if key in CATEGORY_ALIASES:
        key = CATEGORY_ALIASES[key]
    for slug, (category_id, name_en, name_zh) in CATEGORIES.items():
        aliases = {slug, slug.replace("-", " "), name_en.lower(), name_zh.lower()}
        if key in aliases:
            return {
                "slug": slug,
                "category_id": category_id,
                "name_en": name_en,
                "name_zh": name_zh,
                "url": category_page_url(slug),
            }
    return None


def list_categories() -> list[str]:
    """返回「中文名 (英文名 / slug)」形式的类别列表，用于命令行帮助。"""
    result = []
    for slug, (_category_id, name_en, name_zh) in CATEGORIES.items():
        result.append(f"{name_zh} ({name_en} / {slug})")
    return result


def list_category_names() -> list[str]:
    """返回纯中文类别名列表，用于图形界面下拉框。"""
    return [name_zh for (_category_id, _name_en, name_zh) in CATEGORIES.values()]


# ---------------------------------------------------------------------------
# 商品链接的识别规则（TikTok Shop 不同版本 URL 形态不同，多留几种）
# ---------------------------------------------------------------------------
PRODUCT_URL_KEYWORDS = ("/pdp/", "/product", "/view/product", "/goods/", "/p/")

# ---------------------------------------------------------------------------
# 最终 Excel 的列定义：(中文表头, 字段名, 列宽)
# ---------------------------------------------------------------------------
EXCEL_COLUMNS: list[tuple[str, str, int]] = [
    ("商品名称", "product_name", 56),
    ("商品销量", "sold_count", 12),
    ("商品上架时间", "listing_time_label", 20),
    ("商品链接", "product_url", 60),
    ("商品照片", "image_url", 38),
    ("商品详情介绍", "description", 80),
]

EXCEL_FILENAME = "TikTok_US_Ranking.xlsx"

#: 商品描述区只有图片、没有文字时，「商品详情介绍」列填写的占位文本
DESCRIPTION_IMAGE_FALLBACK = "（描述为图片，无文字）"

#: 是否把商品照片下载并内嵌到 Excel 单元格（缩略图，点击打开原图）。
#: 下载失败的商品自动退回「图片链接文本 + 超链接」。
EMBED_PRODUCT_IMAGES = True

#: 除主文件外，是否再按时间戳存一份历史副本（存到 output/ 目录）
SAVE_TIMESTAMPED_COPY = True

#: 日志级别：DEBUG / INFO / WARNING
LOG_LEVEL = "INFO"
