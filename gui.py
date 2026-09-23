"""TikTok Shop 热销榜采集工具 —— 图形界面入口（tkinter）。

双击启动（打包后为 exe），小白友好：
  1. 选择类别（或"全部"）
  2. 填销量下限（可留空）
  3. 填抓取数量（默认 15）
  4. 点「开始抓取」
遇到滑块验证时，会弹出可见浏览器窗口，请手动拖动滑块，
验证通过后程序自动继续。
"""

from __future__ import annotations

import logging
import queue
import sys
import threading
from pathlib import Path

import tkinter as tk
from tkinter import ttk, messagebox

import config  # noqa: E402
from excel_export import export_to_excel, timestamped_copy_path  # noqa: E402
from scraper import (  # noqa: E402
    NoProductFoundError,
    PageOpenError,
    ScraperError,
    SecurityCheckError,
    StopRequested,
    TikTokRankingScraper,
)

# 项目根目录（源码运行时=项目目录；打包后=exe 所在目录）
BASE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))

# 打包后（frozen）：所有输出/日志/浏览器用户目录都放到 exe 同级目录，
# 避免写进程序包内部；源码运行时保持项目目录不变。
if getattr(sys, "frozen", False):
    _exe_dir = Path(sys.executable).resolve().parent
    config.BASE_DIR = _exe_dir
    config.OUTPUT_DIR = _exe_dir / "output"
    config.DEBUG_DIR = _exe_dir / "debug"
    config.LOG_DIR = _exe_dir / "logs"
    config.PROFILE_DIR = _exe_dir / ".browser_profile"


# ---------------------------------------------------------------------------
# 日志：抓取线程 -> 队列 -> GUI 日志框
# ---------------------------------------------------------------------------
class QueueLogHandler(logging.Handler):
    def __init__(self, log_queue: "queue.Queue[str]") -> None:
        super().__init__()
        self.log_queue = log_queue
        fmt = logging.Formatter(
            fmt="%(asctime)s | %(levelname)-7s | %(message)s",
            datefmt="%H:%M:%S",
        )
        self.setFormatter(fmt)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.log_queue.put(self.format(record))
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------
class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("TikTok Shop 热销榜采集工具")
        self.geometry("860x620")
        self.minsize(760, 540)

        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self._task: threading.Thread | None = None
        #: 当前抓取任务对应的 scraper（供『停止抓取』按钮调用 request_stop）
        self._scraper: TikTokRankingScraper | None = None
        #: 停止请求事件：传给 scraper，点停止时 set()
        self._stop_event = threading.Event()

        # 把抓取日志同时送入 GUI 日志框
        qh = QueueLogHandler(self.log_queue)
        logging.getLogger().addHandler(qh)
        logging.getLogger().setLevel(getattr(logging, config.LOG_LEVEL, logging.INFO))

        self._build_ui()

        # 日志框轮询刷新
        self._poll_log()

    # -- 界面 --------------------------------------------------------------
    def _build_ui(self) -> None:
        pad = {"padx": 10, "pady": 6}

        top = ttk.LabelFrame(self, text="抓取设置")
        top.pack(fill="x", **pad)

        row1 = ttk.Frame(top)
        row1.pack(fill="x", padx=8, pady=6)
        ttk.Label(row1, text="类别：", width=8).pack(side="left")
        self.category_var = tk.StringVar()
        names = ["全部"] + list(config.list_category_names())
        self.category_combo = ttk.Combobox(
            row1, textvariable=self.category_var, values=names, state="readonly", width=28
        )
        self.category_combo.current(0)
        self.category_combo.pack(side="left")

        ttk.Label(row1, text="销量下限（≥，可留空）：", width=22).pack(side="left", padx=(16, 0))
        self.min_sold_var = tk.StringVar()
        ttk.Entry(row1, textvariable=self.min_sold_var, width=14).pack(side="left")

        row2 = ttk.Frame(top)
        row2.pack(fill="x", padx=8, pady=6)
        ttk.Label(row2, text="抓取数量：", width=8).pack(side="left")
        self.max_var = tk.StringVar(value="15")
        ttk.Entry(row2, textvariable=self.max_var, width=8).pack(side="left")

        self.headed_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            row2,
            text="显示浏览器窗口（滑块验证时需要，建议勾选）",
            variable=self.headed_var,
        ).pack(side="left", padx=(20, 0))

        row3 = ttk.Frame(top)
        row3.pack(fill="x", padx=8, pady=(0, 6))
        ttk.Label(row3, text="上架时间：", width=8).pack(side="left")
        ttk.Label(row3, text="只保留上架 ≤").pack(side="left")
        self.max_age_var = tk.StringVar()
        ttk.Entry(row3, textvariable=self.max_age_var, width=6).pack(side="left")
        ttk.Label(
            row3,
            text="天的商品（可留空不限；需在 CDP Chrome 里安装『Tiktok选品助手』插件）",
        ).pack(side="left")

        btn_row = ttk.Frame(self)
        btn_row.pack(fill="x", **pad)
        self.start_btn = ttk.Button(btn_row, text="开始抓取", command=self._on_start, width=16)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(
            btn_row, text="停止抓取", command=self._on_stop, width=16, state="disabled"
        )
        self.stop_btn.pack(side="left", padx=10)
        self.open_btn = ttk.Button(
            btn_row, text="打开输出文件夹", command=self._open_output, state="disabled", width=18
        )
        self.open_btn.pack(side="left", padx=10)
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(btn_row, textvariable=self.status_var, foreground="#666").pack(side="right")

        # 日志区
        log_frame = ttk.LabelFrame(self, text="运行日志")
        log_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.log_text = tk.Text(log_frame, height=18, state="disabled", wrap="word")
        scroll = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True, padx=(4, 0), pady=4)

        hint = (
            "使用说明：\n"
            "· 类别：选「全部」= 榜单总榜不过滤；选具体类别 = 只保留该类别商品（进入详情页按一级类别匹配）。\n"
            "· 销量下限：留空不限；填 490000 表示只保留销量 ≥ 490,000 的商品。\n"
            "· 上架时间：留空不限；填 30 表示只保留上架不超过 30 天的商品。\n"
            "  需要先把『Tiktok选品助手』插件安装到「启动Chrome.bat」启动的 Chrome 里，\n"
            "  否则读不到上架时间（未装插件时该筛选会跳过所有商品）。\n"
            "· 出现滑块验证时，请在弹出的浏览器窗口里手动拖动滑块；验证通过后程序自动继续。\n"
            "· 想中途停止：点「停止抓取」，已抓到的商品会照常导出。\n"
            "· 输出 Excel 在 exe 同目录的 output\\ 文件夹；文件被 WPS/Excel 占用时会自动另存副本。"
        )
        ttk.Label(self, text=hint, foreground="#555", justify="left").pack(fill="x", padx=10, pady=(0, 10))

    # -- 日志轮询 ----------------------------------------------------------
    def _poll_log(self) -> None:
        try:
            while True:
                msg = self.log_queue.get_nowait()
                if msg == "__DONE__":
                    self._on_task_done()
                    continue
                if msg.startswith("__OUT__"):
                    self._last_out = Path(msg[len("__OUT__"):])
                    continue
                self.log_text.configure(state="normal")
                self.log_text.insert("end", msg + "\n")
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(120, self._poll_log)

    # -- 动作 --------------------------------------------------------------
    def _validate_inputs(self) -> tuple[str | None, int | None, int, bool, int | None] | None:
        name = self.category_var.get().strip()
        category = None if name in ("", "全部") else name

        raw_sold = self.min_sold_var.get().strip()
        min_sold: int | None = None
        if raw_sold:
            try:
                min_sold = int(raw_sold.replace(",", ""))
                if min_sold < 0:
                    raise ValueError
            except ValueError:
                messagebox.showerror("输入错误", "销量下限必须是 ≥ 0 的整数，例如 490000")
                return None

        raw_max = self.max_var.get().strip()
        try:
            max_products = int(raw_max)
            if max_products < 1:
                raise ValueError
        except ValueError:
            messagebox.showerror("输入错误", "抓取数量必须是 ≥ 1 的整数")
            return None

        raw_age = self.max_age_var.get().strip()
        max_age_days: int | None = None
        if raw_age:
            try:
                max_age_days = int(raw_age)
                if max_age_days < 1:
                    raise ValueError
            except ValueError:
                messagebox.showerror("输入错误", "上架时间必须是 ≥ 1 的整数天数，例如 30")
                return None

        return category, min_sold, max_products, self.headed_var.get(), max_age_days

    def _on_start(self) -> None:
        if self._task and self._task.is_alive():
            messagebox.showinfo("提示", "抓取正在进行中，请稍候…")
            return
        parsed = self._validate_inputs()
        if parsed is None:
            return
        category, min_sold, max_products, headed, max_age_days = parsed

        self.start_btn.configure(state="disabled")
        self.open_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self._scraper = None
        self._stop_event = threading.Event()
        cond = []
        if category:
            cond.append(f"类别：{category}")
        if min_sold is not None:
            cond.append(f"销量 ≥ {min_sold:,}")
        if max_age_days is not None:
            cond.append(f"上架 ≤ {max_age_days} 天")
        cond.append(f"数量：{max_products}")
        self.status_var.set("抓取中…")
        self._append_log(f"\n===== 开始抓取（{'、'.join(cond)}）=====")

        self._task = threading.Thread(
            target=self._run_task,
            args=(category, min_sold, max_products, headed, max_age_days),
            daemon=True,
        )
        self._task.start()

    def _on_stop(self) -> None:
        """点击『停止抓取』：请求 scraper 在下一个安全点退出。"""
        if self._scraper is not None:
            try:
                self._scraper.request_stop()
            except Exception as exc:  # noqa: BLE001
                self._append_log(f"发送停止请求时出现问题：{exc}")
        self.stop_btn.configure(state="disabled")
        self.status_var.set("正在停止…")
        self._append_log("已请求停止抓取，等当前步骤结束后退出…")

    def _run_task(
        self,
        category: str | None,
        min_sold: int | None,
        max_products: int,
        headed: bool,
        max_age_days: int | None,
    ) -> None:
        out_path: Path | None = None
        try:
            log = logging.getLogger("gui")
            log.info("启动浏览器（headless=%s）…", not headed)
            self._scraper = TikTokRankingScraper(
                url=config.URL,
                max_products=max_products,
                headless=not headed,
                category=category,
                min_sold=min_sold,
                max_listing_age_days=max_age_days,
                wait_for_verify=True,
                use_profile=True,
                profile_dir=config.PROFILE_DIR,
                stop_event=self._stop_event,
            )
            with self._scraper as scraper:
                products = scraper.run()
            self._scraper = None

            if not products:
                if self._stop_event.is_set():
                    log.info("已停止，未抓到任何商品，不导出。")
                    self.log_queue.put("__DONE__")
                    return
                raise NoProductFoundError("没有符合条件的商品（可能被销量下限过滤掉了）")

            category_label = None
            if category:
                info = config.resolve_category(category)
                if info:
                    category_label = f"{info['name_zh']}（{info['name_en']}）"
            out_path = config.OUTPUT_DIR / config.EXCEL_FILENAME
            main_saved = False
            try:
                out_path = export_to_excel(products, str(out_path), category_label=category_label)
                main_saved = True
            except PermissionError:
                out_path = timestamped_copy_path(config.OUTPUT_DIR, config.EXCEL_FILENAME)
                log.warning("输出文件被占用（可能正被 Excel/WPS 打开），已另存：%s", out_path)
                out_path = export_to_excel(products, str(out_path), category_label=category_label)

            # 与命令行版 main.py 一致：主文件导出成功后，额外存一份带时间戳的历史副本
            if config.SAVE_TIMESTAMPED_COPY and main_saved:
                try:
                    copy_path = timestamped_copy_path(config.OUTPUT_DIR, config.EXCEL_FILENAME)
                    export_to_excel(products, str(copy_path), category_label=category_label)
                    log.info("历史副本已保存：%s", copy_path)
                except PermissionError:
                    log.warning("历史副本被占用，跳过保存：%s", copy_path)

            log.info("完成：导出 %d 条 → %s", len(products), out_path)
            self.log_queue.put("__DONE__")
        except StopRequested:
            self.log_queue.put("\n[已停止] 抓取已终止。")
            self.log_queue.put("__DONE__")
        except SecurityCheckError as exc:
            self.log_queue.put(f"\n[失败] 安全验证未通过：{exc}")
            self.log_queue.put("__DONE__")
        except (PageOpenError, NoProductFoundError, ScraperError) as exc:
            self.log_queue.put(f"\n[失败] {exc}")
            self.log_queue.put("__DONE__")
        except Exception as exc:  # noqa: BLE001
            self.log_queue.put(f"\n[失败] 未预期的错误：{exc}")
            self.log_queue.put("__DONE__")

        if out_path is not None:
            self.log_queue.put(f"__OUT__{out_path}")

    def _on_task_done(self) -> None:
        self.start_btn.configure(state="normal")
        self.open_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.status_var.set("就绪")
        self._append_log("===== 本次抓取结束 =====\n")

    def _append_log(self, msg: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _open_output(self) -> None:
        out_dir = config.OUTPUT_DIR
        out_dir.mkdir(parents=True, exist_ok=True)
        try:
            import os

            os.startfile(str(out_dir))  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("打开失败", str(exc))


def _packaged_smoke() -> int:
    """打包验证：exe 同级放一个 selftest.flag 后启动，会自动测试
    Playwright 驱动 + 系统浏览器是否可用，结果写入 smoke_result.txt，
    然后退出（不打开界面）。正常使用不受影响。"""
    try:
        from playwright.sync_api import sync_playwright

        result = []
        with sync_playwright() as p:
            browser = None
            last_err = ""
            for channel in ("chrome", "msedge"):
                try:
                    browser = p.chromium.launch(channel=channel, headless=True)
                    break
                except Exception as exc:  # noqa: BLE001
                    last_err = str(exc).splitlines()[0]
            if browser is None:
                raise RuntimeError(f"系统浏览器不可用：{last_err}")
            try:
                page = browser.new_page()
                page.goto("https://www.example.com", timeout=30000)
                result.append(f"title={page.title()!r}")
            finally:
                browser.close()
        (config.BASE_DIR / "smoke_result.txt").write_text(
            "OK " + " | ".join(result), encoding="utf-8"
        )
        return 0
    except Exception as exc:  # noqa: BLE001
        (config.BASE_DIR / "smoke_result.txt").write_text(
            f"FAIL {exc}", encoding="utf-8"
        )
        return 1


def main() -> int:
    if (config.BASE_DIR / "selftest.flag").exists():
        return _packaged_smoke()
    app = App()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
