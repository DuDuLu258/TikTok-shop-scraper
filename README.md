# TikTok Shop US 热销商品采集工具

本地运行的 Python 命令行工具：用 Playwright 打开 TikTok Shop 美国站排行榜页面，
模拟真人浏览（等待、缓慢滚动、点击 View more），把商品信息导出成 Excel。

* 目标页面：<https://shop.tiktok.com/us/events/ranking-list>
* 不登录账号、不调用官方 API、不使用付费第三方服务
* 仅用于个人数据研究与选品分析

---

## 一、目录结构

```
TikTokScraper/
├── main.py            # 程序入口（命令行参数、日志、流程编排、异常兜底）
├── scraper.py         # 浏览器控制 / 页面访问 / 商品抓取 / View more 循环 / 安全验证处理
├── page_parser.py     # 卡片解析：把商品卡 HTML 变成结构化字段
├── excel_export.py    # 生成 Excel（openpyxl）
├── config.py          # URL、最大数量、等待时间、按钮文案等全部配置
├── inspect_page.py    # 页面结构诊断脚本（先跑它，确认 DOM 再正式抓）
├── selftest.py        # 离线自检（不联网，验证依赖 / 解析 / 导出是否正常）
├── 启动Chrome.bat     # 一键启动带调试端口的 Chrome（CDP 模式用）
├── requirements.txt
├── .gitignore
└── README.md
```

运行时自动生成：

```
output/            # Excel 输出（主文件 + 带时间戳的历史副本）
debug/             # 截图 / HTML 快照 / 解析结果 JSON
logs/              # 每次运行的日志
.browser_profile/  # 浏览器用户目录（Cookie、验证状态）
```

---

## 二、安装

```bash
cd TikTokScraper

python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

浏览器内核有两种选择：

1. **用系统已装的 Chrome / Edge（推荐，零下载）**：什么都不用做，
   程序会按 `Playwright Chromium → 系统 Chrome → 系统 Edge` 的顺序自动挑一个能用的。
2. **用 Playwright 自带 Chromium**：
   ```bash
   python -m playwright install chromium
   ```

装完依赖后先跑一次离线自检，确认环境没问题：

```bash
python selftest.py
```

---

## 三、运行

第一步，先确认页面结构（推荐，能看到浏览器窗口）：

```bash
python inspect_page.py --headed
```

它会截图、保存 HTML，并打印：像商品卡片的 class、商品链接、候选卡片的解析结果、
`View more` 按钮候选、内嵌 JSON 状态。

确认无误后正式抓取：

```bash
python main.py
```

### CDP 模式：连接已有 Chrome（推荐）

脚本默认会自己启动一个浏览器。如果你想用自己手动开的 Chrome（比如已经登录了 TikTok、装了插件），用 CDP 模式：

1. 双击运行 `启动Chrome.bat`，会打开一个带调试端口的 Chrome
2. 在这个 Chrome 里手动完成安全验证、安装好「Tiktok选品助手」插件
3. 保持这个 Chrome 窗口开着
4. 另开终端运行 `python main.py`，脚本会自动连接到这个 Chrome

这样插件注入的上架时间数据才能被读到。

| 参数 | 说明 |
| --- | --- |
| `--max 50` | 只抓 50 个商品（默认 100） |
| `--headed` | 显示浏览器窗口，便于观察页面 |
| `--debug` | 保存截图 / HTML / JSON，并输出 DEBUG 日志 |
| `--wait-verify` | 遇到滑块验证时等你手动通过：不设超时、不会自动关窗口，你也可以在控制台按一次回车强制继续 |
| `--strict-verify` | 遇到验证不要自动开窗口，直接报错退出（默认会自动切到可见窗口） |
| `--category 美妆个护` | 只抓指定类别的商品（一次一个类别）。支持中文名 / 英文名 / URL slug，例如 `--category 美妆个护`、`--category "Beauty & Personal Care"`、`--category beauty-personal-care`；不传则抓默认总榜 |
| `--min-sold 490000` | 销量下限过滤：只保留销量 ≥ 该数值的商品，可与 `--category` 组合使用。榜单卡片解析不到销量的商品会被跳过 |
| `--no-profile` | 不使用固定浏览器用户目录（Cookie 不保留，一般不建议） |
| `--profile-dir D:\ttprofile` | 自定义浏览器用户目录位置 |
| `--out D:\data\rank.xlsx` | 自定义 Excel 输出路径 |
| `--no-copy` | 不额外保存带时间戳的历史副本 |

### 按类别抓取

榜单页默认展示全站近 30 天热销品。如果你只想保留某个类目（例如商品详情页里显示的
「美妆个护」）的商品，用 `--category` 指定即可：

```bash
python main.py --category 美妆个护 --max 15
```

* 程序仍然只打开榜单页（`/us/events/ranking-list`）抓取，不会跳到类目页；
* 每收集一批商品卡片，就**逐个进入商品详情页（PDP）读取详情介绍与一级类别**——详情
  介绍来自 PDP 的 "Product description" 区域；一级类别来自 PDP 内嵌 JSON
  （`__MODERN_ROUTER_DATA__` → `global_data.product_info.categories`，取
  `level == 1` 的类目），页面被浏览器翻译成中文也不影响，因为读到的是英文名和
  category_id；

### 类别 + 销量组合筛选

```bash
python main.py --category 美妆个护 --min-sold 490000 --max 15
```

表示：在榜单页检索，只保留**一级类别为「美妆个护」且销量 ≥ 490000** 的商品。
销量直接在榜单卡片上就能读到，所以销量不达标（或解析不到销量）的商品**不会**
进入商品详情页，抓取更快。
* 只有一级类别与 `--category` 设定一致的商品才会保留，一直抓到你设定的数量
  （上面的例子是 15 条）或榜单到底为止；默认 `--max` 仍是 100，最终交付数
  = 匹配类别的条数；
* 类别支持三种写法：中文名（`美妆个护`）、英文名（`Beauty & Personal Care`）、
  URL slug（`beauty-personal-care`），大小写不敏感；已内置 12 个一级类目
  （见 `python main.py --help` 或 `config.CATEGORIES`），需要其它类目时在
  `config.py` 里加一行即可；
* 每访问一个 PDP 会等 1~2.5 秒随机间隔，以降低连续访问被风控的概率；抓到的
  Excel 会在「抓取说明」表里记录本次的抓取类别；
* 若传入的类别不在内置映射里，程序退回"点击页面上分类标签"的老逻辑，且不做
  类别过滤。

输出文件：

```
output/TikTok_US_Ranking.xlsx                    # 每次覆盖的主文件
output/TikTok_US_Ranking_20260915_1333.xlsx      # 历史副本（每次运行一份）
```

---

## 四、安全验证（重要，请先读这一段）

TikTok Shop 页面在检测到"像机器人的访问"时，会弹出一个滑块验证页
（标题是 `Security Check`，提示 `Verify to continue: Drag the puzzle piece into place`）。

**这是人机校验，不是代码 bug，程序不会去绕过它。** 程序的应对方式是：

* 自动识别验证页，并且每次翻页都重新检测一次；
* **只认"视口内真正可见"的验证层**：TikTok 验证通过后有时会把节点留在 DOM 里，
  只看文本会误判成"还没通过"，所以隐藏节点一律不算（这点有自检覆盖）；
* 无头模式一旦被拦，**自动打开浏览器窗口**让你手动过，而不是直接报错退出；
* 等待人工验证**默认不超时、不会自动关窗口**，你慢慢划；
* 你在控制台**按一次回车**就能强制继续（如果显示已经通过、程序还在等，用这招）；
* 想回到"被拦就报错退出"的老行为：加 `--strict-verify`（退出码 5）。

推荐流程：

```bash
python main.py --headed --wait-verify --max 100
```

在弹出的窗口里手动拖一次滑块即可。程序会把这次验证状态保存在
`.browser_profile/` 目录里，**后续运行通常就不需要再验证了**，那时可以去掉 `--headed`。

如果 `python main.py`（无头）再次被拦，也不要紧：程序会自动把窗口打开给你，
你过一次滑块后它会自己继续抓取，不需要你再改命令。

**为什么无头模式还会被拦？** 验证状态是跟"浏览器会话 + 指纹"绑定的，
无头浏览器的指纹和可见窗口不完全一样，所以 TikTok 有可能再验一次。
如果它总是反复出现，把 `config.py` 里的 `HEADLESS` 改成 `False`（等于长期用可见窗口），
或者换一个美国节点的网络环境——出口 IP 的地区是这套风控里权重最大的因素。

**关于"滑块划好几次都不通过"**：这是账号/环境被标记时的典型表现，
滑块本身并不是必过题。有效的做法依次是：换美国节点网络 → 别删 `.browser_profile`
（Cookie 越老越好）→ 换成可见窗口慢慢划 → 隔一段时间再试。
程序侧只能做到"不加剧"：随机等待、低速滚动、真实鼠标事件，这些都已经做了。

影响验证频率的三个因素：

1. **网络出口地区**：从中国大陆直接访问美国站，风控概率明显更高。
   如果你有美国节点的网络环境，成功率会高很多。
2. **浏览器用户目录**：程序默认复用 `.browser_profile`，Cookie 越"老"越不容易被拦。
   不要频繁删除这个目录。
3. **操作节奏**：程序已经做了随机等待和低速滚动，不建议把 `config.py` 里的时间调得太激进。

另外，验证有时不是打开页面时出现，而是在点击 `View more` 之后才出现——
这也是正常的，程序在每一轮循环里都会重新检测。

---

## 五、抓取逻辑

1. 启动浏览器：按 `Playwright Chromium → 系统 Chrome → 系统 Edge` 顺序尝试，
   使用固定用户目录（Cookie 复用），设置 `en-US`、美西时区，
   并注入脚本弱化自动化特征；无头模式下会把 UA 里的 `HeadlessChrome` 修正成 `Chrome`。
2. 打开排行榜页，等 `domcontentloaded`，再尽量等 `networkidle`，
   然后随机停顿 2.5~4.5 秒（模拟真人打开页面后的阅读）。
3. 检测并处理安全验证（见第四节）：无头被拦就自动切到可见窗口等你过，
   然后尝试关闭 Cookie / 登录提示等遮挡层。
4. 循环执行：
   * 以 320~720 像素的随机步长缓慢滚动，每次停顿 1.1~2.6 秒；
   * 偶发向上回滚，避免节奏过于机械；
   * 收集商品卡片（按商品链接去重）；
   * 按可见文本查找 `View more / 查看更多 / See more…`，用真实鼠标事件点击，
     然后轮询等待新商品出现（比较商品链接数与图片数的变化）。
5. 停止条件（任一满足即结束）：
   * 已达到 `--max` 数量；
   * 连续 3 轮既没点出新商品、滚动也没加载出新内容；
   * 页面上已经没有 `View more` 按钮；
   * 达到最大轮次 / 最大点击次数上限（防止死循环）。
6. 解析 → 去重 → 重新编号 → 写 Excel → 打印前 5 条预览。

---

## 六、异常处理

| 情况 | 行为 |
| --- | --- |
| 页面打开超时（默认 60 秒） | 明确提示，退出码 2，`--debug` 时保存截图 |
| 页面返回 HTTP 4xx / 5xx | 提示可能是地区限制 / 改版 / 风控，退出码 2 |
| 无头模式遇到滑块验证 | 自动打开浏览器窗口等你手动通过，然后继续（`--strict-verify` 可改成直接报错，退出码 5） |
| 等待人工验证 | 默认不限时、不自动关窗口；按回车可强制继续 |
| 浏览器用户目录被占用 / 不可写 | 自动改用临时目录并提示（Cookie 不保留）；同时提示结束残留 chrome.exe |
| 一个商品都没解析出来 | 提示运行 `python inspect_page.py --headed`，退出码 3 |
| 找不到 `View more` 按钮 | 记录日志并正常结束（属于正常终止条件） |
| 点击按钮失败 | 记录警告，继续用滚动方式尝试，不中断程序 |
| 浏览器内核全部不可用 | 提示安装 Chromium 或装 Chrome / Edge |
| 抓到的数量少于目标 | 打印警告，照常导出已抓到的数据 |
| Ctrl+C | 干净退出，退出码 130 |

日志同时写入 `logs/scrape_YYYYmmdd_HHMMSS.log`，方便事后排查。

---

## 七、常见问题

**Q：卡在滑块验证 / 划好几次不通过 / 窗口突然关了？**

先确认你用的是最新代码（这三个问题都已修）：

* 窗口"自动关闭"是老版本 300 秒超时导致的，现在**默认不限时**，不会自己关；
* 划了好几次终于通过、程序却没反应 → 现在**按一次回车**就让程序继续；
* 无头模式反复被拦 → 现在会自动开窗口让你过，不用再手动加参数。

建议命令仍然是：

```bash
python main.py --headed --wait-verify --max 100
```

**Q：抓到 0 个商品怎么办？**

先跑 `python inspect_page.py --headed`，看 `debug/` 里的截图和打印出来的卡片样例。
页面改版时，按"第七节"的规则调整 `page_parser.py` 里的启发式判断即可。

**Q：只抓到 15 个（或几十个）就停了？**

15 是首屏卡片数。如果点了 `View more` 却没加载出新商品，通常是触发了风控，
或者页面加载变慢：可以把 `config.Timing` 里的等待时间调大、
把 `NO_GROWTH_LIMIT` 调到 4~5，或者用 `--headed` 观察点击后到底发生了什么。

**Q：能跑得更快吗？**

可以改小 `Timing` 里的区间（例如 `scroll_pause_s=(0.5, 1.0)`），
但不建议太激进：越像真人越不容易被拦。

**Q：需要登录吗？**

不需要，公开榜单页无需登录。

---

## 八、后续可以扩展的方向

* GUI 界面（PySide6 / Tkinter）
* 自动定时运行（Windows 任务计划程序 / cron）
* AI 选品分析（利润测算、评论情感、竞品对比）
* TEMU 等其它平台的数据整合

第一版保持命令行 + Excel 的极简形态，先保证抓得稳、数据对。

---

## 九、免责声明

本工具仅用于个人数据研究与选品分析。请遵守目标网站的服务条款、
robots 协议与所在地法律法规，不要用于高频、大规模或商业性的数据抓取。
程序不会绕过任何安全验证，遇到人机校验会停下来交由使用者本人处理，本质上还是人工访问。
因使用本工具产生的任何后果由使用者自行承担。
