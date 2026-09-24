"""Light, local-only dashboard for ZCode's recorded model usage."""
from __future__ import annotations

import math
import tkinter as tk
from collections import defaultdict
from datetime import date, datetime, timedelta
from tkinter import ttk

from monitor import (
    MonitorBackend, PERIODS, SOURCE_LABELS, SOURCE_LABELS_ORDERED,
    format_num, official_period_local_tokens, official_snapshot_status,
    official_time_text, official_value_text, period_start,
)

PAGE = "#ffffff"
CARD = "#f5f6f8"
LINE = "#e8eaee"
TEXT = "#20242a"
SUBTEXT = "#69717e"
BLUE = "#3478f6"
BLUE_PALE = "#e6efff"
PALETTE = ("#3478f6", "#5ca88a", "#9879d1", "#f3a85d", "#69a6c6")


def compact(value: int | None) -> str:
    if value is None:
        return "未知"
    if abs(value) >= 10_000:
        return f"{value / 10_000:,.1f}万"
    return f"{value:,}"


def token_sum(rows: list[dict], field: str) -> int | None:
    values = [row[field] for row in rows]
    if any(value is None for value in values):
        return None
    return sum(int(value) for value in values)


class DashboardApp(MonitorBackend):
    def _build_style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Light.Treeview", background=PAGE, fieldbackground=PAGE, foreground=TEXT,
                        rowheight=31, borderwidth=0, font=("Microsoft YaHei UI", 9))
        style.configure("Light.Treeview.Heading", background=CARD, foreground=SUBTEXT,
                        font=("Microsoft YaHei UI", 9, "bold"), relief="flat")
        style.map("Light.Treeview", background=[("selected", BLUE_PALE)], foreground=[("selected", TEXT)])
        style.configure("Light.TCombobox", fieldbackground=PAGE, background=PAGE, foreground=TEXT,
                        arrowcolor=SUBTEXT, bordercolor=LINE, lightcolor=LINE, darkcolor=LINE)
        style.map("Light.TCombobox", fieldbackground=[("readonly", PAGE)],
                  selectbackground=[("readonly", PAGE)], selectforeground=[("readonly", TEXT)])

    def _label(self, parent, text, size=10, color=TEXT, weight="normal", **kwargs):
        return tk.Label(parent, text=text, bg=parent.cget("bg"), fg=color,
                        font=("Microsoft YaHei UI", size, weight), **kwargs)

    def _section(self, parent, title: str):
        background = tk.Canvas(parent, bg=PAGE, bd=0, highlightthickness=0, height=80)
        background.pack(fill="x", pady=(0, 16))
        frame = tk.Frame(background, bg=CARD, padx=10, pady=7)
        window_id = background.create_window((8, 8), window=frame, anchor="nw")

        def draw_card(_event=None):
            width = max(20, background.winfo_width())
            background.delete("card-bg")
            radius = 15
            background.create_rectangle(radius, 0, width - radius, background.winfo_height(),
                                        fill=CARD, outline="", tags="card-bg")
            background.create_rectangle(0, radius, width, background.winfo_height() - radius,
                                        fill=CARD, outline="", tags="card-bg")
            for x, y, start in ((0, 0, 90), (width - 2 * radius, 0, 0),
                                (0, background.winfo_height() - 2 * radius, 180),
                                (width - 2 * radius, background.winfo_height() - 2 * radius, 270)):
                background.create_arc(x, y, x + 2 * radius, y + 2 * radius,
                                      start=start, extent=90, style="pieslice", fill=CARD,
                                      outline="", tags="card-bg")
            background.tag_lower("card-bg")
            background.itemconfigure(window_id, width=max(10, width - 16))

        def size_card(event):
            target = event.height + 16
            if background.winfo_height() != target:
                background.configure(height=target)

        background.bind("<Configure>", draw_card)
        frame.bind("<Configure>", size_card)
        header = tk.Frame(frame, bg=CARD)
        header.pack(fill="x", pady=(0, 10))
        self._label(header, title, 11, TEXT, "bold").pack(side="left")
        return frame, header

    def _button(self, parent, text, command):
        button = tk.Button(parent, text=text, command=command, bg=PAGE, fg=TEXT,
                           activebackground=BLUE_PALE, activeforeground=TEXT,
                           relief="flat", bd=0, padx=12, pady=7,
                           font=("Microsoft YaHei UI", 9), cursor="hand2")
        return button

    def _build(self):
        self.root.title("ZCode 使用统计 · 本机")
        self.root.geometry("1100x760")
        self.root.minsize(900, 620)
        self.root.configure(bg=PAGE)
        self.activity_rows: list[dict] = []
        self.model_rows: list[dict] = []
        self.trend_days = 30
        self._initial_positioned = False
        self._trend_hitboxes = []
        self.period_buttons = []
        self.trend_buttons = []

        self.viewport = tk.Canvas(self.root, bg=PAGE, bd=0, highlightthickness=0)
        scrollbar = ttk.Scrollbar(self.root, orient="vertical", command=self.viewport.yview)
        self.viewport.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.viewport.pack(side="left", fill="both", expand=True)
        body = tk.Frame(self.viewport, bg=PAGE, padx=28, pady=22)
        window_id = self.viewport.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>", lambda _e: self.viewport.configure(scrollregion=self.viewport.bbox("all")))
        self.viewport.bind("<Configure>", lambda e: self.viewport.itemconfigure(window_id, width=e.width))
        self.viewport.bind("<MouseWheel>", lambda e: self.viewport.yview_scroll(int(-e.delta / 120), "units"))

        heading = tk.Frame(body, bg=PAGE)
        heading.pack(fill="x", pady=(0, 19))
        self._label(heading, "使用统计", 23, TEXT, "bold").pack(side="left")
        self._label(heading, "当前用户 · 本机 ZCode 记录", 10, SUBTEXT).pack(side="left", padx=(16, 0), pady=(12, 0))
        self.record_range_label = self._label(body, "正在核对本地记录时间…", 9, SUBTEXT, anchor="w")
        self.record_range_label.pack(fill="x", pady=(0, 13))

        controls = tk.Frame(body, bg=PAGE)
        controls.pack(fill="x", pady=(0, 17))
        self._label(controls, "统计范围", 9, SUBTEXT).pack(side="left", padx=(0, 8))
        period_pill = tk.Frame(controls, bg=CARD, padx=3, pady=3)
        period_pill.pack(side="left", padx=(0, 17))
        for index, (title, _value) in enumerate(PERIODS):
            button = self._button(period_pill, title, lambda i=index: self._choose_period(i))
            button.configure(padx=10, pady=4)
            button.pack(side="left")
            self.period_buttons.append(button)
        self._update_pills(self.period_buttons, 0)
        self._label(controls, "来源", 9, SUBTEXT).pack(side="left", padx=(0, 7))
        source_box = ttk.Combobox(controls, state="readonly", width=13, style="Light.TCombobox",
                                  values=[label for label, _ in SOURCE_LABELS_ORDERED])
        source_box.current(0)
        source_box.pack(side="left")
        source_box.bind("<<ComboboxSelected>>", lambda _e: self._source_changed(source_box.current()))
        self._button(controls, "选择数据库", self.choose_database).pack(side="right")
        self._button(controls, "刷新", lambda: self.messages.put(("refresh", None))).pack(side="right", padx=(0, 7))

        stats, stats_header = self._section(body, "本机记录用量")
        self.stats_title = stats_header.winfo_children()[0]
        strip = tk.Frame(stats, bg=CARD)
        strip.pack(fill="x", pady=(2, 2))
        self.metrics: dict[str, tuple[tk.Label, tk.Label]] = {}
        for index, (key, title) in enumerate((("total", "总 Token"), ("input", "输入 Token"),
                                               ("output", "输出 Token"), ("models", "使用模型"))):
            cell = tk.Frame(strip, bg=CARD)
            cell.pack(side="left", fill="x", expand=True)
            if index:
                tk.Frame(cell, bg="#dfe2e8", width=1, height=42).pack(side="left", padx=(0, 13))
            value = self._label(cell, "—", 17, TEXT)
            value.pack(anchor="w")
            caption = self._label(cell, title, 9, SUBTEXT)
            caption.pack(anchor="w", pady=(2, 0))
            self.metrics[key] = value, caption
        self.detail_label = self._label(stats, "缓存读取 —  ·  缓存写入 —  ·  推理 —", 9, SUBTEXT, anchor="w")
        self.detail_label.pack(fill="x", pady=(15, 0))

        activity, _ = self._section(body, "Token 活动")
        self.heatmap = tk.Canvas(activity, bg=CARD, height=140, bd=0, highlightthickness=0)
        self.heatmap.pack(fill="x")
        self.activity_info = self._label(activity, "每格代表一天 · 蓝色越深，用量越高", 9, SUBTEXT, anchor="w")
        self.activity_info.pack(fill="x", pady=(5, 0))
        self.heatmap.bind("<Configure>", lambda _e: self._draw_heatmap())
        self.heatmap.bind("<Motion>", self._hover_heatmap)
        self.heatmap.bind("<Leave>", lambda _e: self.activity_info.configure(text="每格代表一天 · 蓝色越深，用量越高"))

        trend, trend_header = self._section(body, "每日 Token 趋势")
        trend_pill = tk.Frame(trend_header, bg="#e9ebef", padx=3, pady=3)
        trend_pill.pack(side="right")
        for index, days in enumerate((7, 30)):
            button = self._button(trend_pill, f"近 {days} 天", lambda n=days: self._choose_trend(n))
            button.configure(padx=10, pady=3)
            button.pack(side="left")
            self.trend_buttons.append(button)
        self._update_pills(self.trend_buttons, 1)
        self.trend = tk.Canvas(trend, bg=CARD, height=194, bd=0, highlightthickness=0)
        self.trend.pack(fill="x")
        self.trend_info = self._label(trend, "按本地自然日统计", 9, SUBTEXT, anchor="w")
        self.trend_info.pack(fill="x", pady=(4, 0))
        self.trend.bind("<Configure>", lambda _e: self._draw_trend())
        self.trend.bind("<Motion>", self._hover_trend)
        self.trend.bind("<Leave>", lambda _e: self.trend_info.configure(text="按本地自然日统计"))

        model_section, _ = self._section(body, "模型用量分布 · 全部历史")
        self.model_chart = tk.Canvas(model_section, bg=CARD, height=140, bd=0, highlightthickness=0)
        self.model_chart.pack(fill="x")
        self.model_chart.bind("<Configure>", lambda _e: self._draw_models())

        official, _ = self._section(body, "官方套餐扣减")
        row = tk.Frame(official, bg=CARD)
        row.pack(fill="x")
        self.official_amount = self._label(row, "未获取", 16, TEXT)
        self.official_amount.pack(side="left")
        self.official_period_tokens = self._label(row, "官方周期内本机 Token：未获取", 9, SUBTEXT)
        self.official_period_tokens.pack(side="right")
        self.official_meta = self._label(official, "没有可靠的本地官方结算数据", 9, SUBTEXT,
                                         anchor="w", justify="left", wraplength=940)
        self.official_meta.pack(fill="x", pady=(7, 0))

        details, _ = self._section(body, "模型明细")
        columns = ("provider", "model", "source", "status", "input", "output", "total", "cache_read", "cache_write", "requests")
        self.tree = ttk.Treeview(details, columns=columns, show="headings", height=6, style="Light.Treeview")
        headers = ("供应商", "模型", "来源", "状态", "输入", "输出", "总 Token", "缓存读", "缓存写", "请求")
        widths = (113, 125, 95, 87, 95, 95, 105, 90, 90, 58)
        for key, title, width in zip(columns, headers, widths):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, minwidth=52, anchor="w" if key in columns[:4] else "e")
        self.empty_hint = self._label(details, "当前范围暂无用量记录", 10, SUBTEXT, anchor="w", pady=12)
        self.empty_hint.pack(fill="x")

        footer = tk.Frame(body, bg=PAGE)
        footer.pack(fill="x", pady=(0, 4))
        self.path_label = self._label(footer, f"数据源：{self.db_path}", 8, SUBTEXT, anchor="w")
        self.path_label.pack(fill="x")
        self.status_label = self._label(footer, "正在读取本地数据库…", 8, BLUE, anchor="w", justify="left", wraplength=1020)
        self.status_label.pack(fill="x", pady=(4, 0))
        self._schedule_local_midnight_refresh()

    def _update_pills(self, buttons, selected):
        for index, button in enumerate(buttons):
            button.configure(bg=PAGE if index == selected else CARD,
                             fg=TEXT if index == selected else SUBTEXT)

    def _choose_period(self, index):
        self._update_pills(self.period_buttons, index)
        self._period_changed(index)

    def _choose_trend(self, days):
        self.trend_days = days
        self._update_pills(self.trend_buttons, 0 if days == 7 else 1)
        self._draw_trend()

    def _source_rows(self):
        selected = self.source.get()
        return [row for row in self.rows if selected == "*" or row["query_source"] == selected]

    def _render(self):
        source_rows = self._source_rows()
        lower = period_start(self.period.get())
        filtered = [row for row in source_rows if lower is None or row["started_at"] >= lower]
        titles = {"today": "今日的本机 Token", "7d": "最近 7 天的本机 Token", "all": "全部历史的本机 Token"}
        self.stats_title.configure(text=titles[self.period.get()])
        if self.rows:
            times = [row["started_at"] for row in self.rows]
            first_day = datetime.fromtimestamp(min(times) / 1000).strftime("%Y-%m-%d")
            last_time = datetime.fromtimestamp(max(times) / 1000).strftime("%Y-%m-%d %H:%M")
            self.record_range_label.configure(text=f"数据库记录范围：{first_day} 至 {last_time[:10]}  ·  最近一次模型请求：{last_time}  ·  官方套餐扣减另列")
        else:
            self.record_range_label.configure(text="数据库中暂无模型用量记录 · 官方套餐扣减另列")
        groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for row in filtered:
            groups[(row["provider_id"] or "未知", row["model_id"] or "未知")].append(row)

        self.metrics["total"][0].configure(text=compact(token_sum(filtered, "computed_total_tokens")))
        self.metrics["input"][0].configure(text=compact(token_sum(filtered, "input_tokens")))
        self.metrics["output"][0].configure(text=compact(token_sum(filtered, "output_tokens")))
        self.metrics["models"][0].configure(text=str(len(groups)))
        self.detail_label.configure(text=(
            f"缓存读取 {format_num(token_sum(filtered, 'cache_read_input_tokens'))}  ·  "
            f"缓存写入 {format_num(token_sum(filtered, 'cache_creation_input_tokens'))}  ·  "
            f"推理 {format_num(token_sum(filtered, 'reasoning_tokens'))}"
        ))

        for item in self.tree.get_children():
            self.tree.delete(item)
        for (provider, model), rows in sorted(groups.items(),
                                               key=lambda pair: token_sum(pair[1], "computed_total_tokens") or 0,
                                               reverse=True):
            sources = ", ".join(sorted({SOURCE_LABELS.get(row["query_source"], row["query_source"]) for row in rows}))
            statuses = {row["status"] for row in rows}
            status = "运行中" if "running" in statuses else ("含失败" if "error" in statuses else ("含取消" if "cancelled" in statuses else "已完成"))
            self.tree.insert("", "end", values=(
                provider, model, sources, status,
                format_num(token_sum(rows, "input_tokens")),
                format_num(token_sum(rows, "output_tokens")),
                format_num(token_sum(rows, "computed_total_tokens")),
                format_num(token_sum(rows, "cache_read_input_tokens")),
                format_num(token_sum(rows, "cache_creation_input_tokens")),
                len(rows),
            ))
        if groups:
            self.empty_hint.pack_forget()
            self.tree.pack(fill="x")
        else:
            self.tree.pack_forget()
            self.empty_hint.pack(fill="x")

        snapshot = self.official
        self.official_amount.configure(text=official_value_text(snapshot.deducted, snapshot.unit))
        interval_total = official_period_local_tokens(source_rows, snapshot)
        self.official_period_tokens.configure(text=f"官方周期内本机 Token：{format_num(interval_total) if interval_total is not None else '未获取'}")
        if snapshot.deducted is None and snapshot.period_start is None:
            official_line = "当前没有可靠的本地官方结算数据 · 套餐扣减不根据 Token 估算"
        else:
            official_line = (f"{official_snapshot_status(snapshot)}  ·  范围：{snapshot.scope or '未获取'}  ·  "
                             f"区间：{official_time_text(snapshot.period_start)} — {official_time_text(snapshot.period_end)}  ·  "
                             f"下次重置：{official_time_text(snapshot.reset_at)}  ·  数据时间：{official_time_text(snapshot.observed_at)}")
        self.official_meta.configure(text=official_line)

        self.activity_rows = source_rows
        self.model_rows = source_rows
        self._draw_heatmap()
        self._draw_trend()
        self._draw_models()
        self._set_status()
        if not self._initial_positioned:
            self._initial_positioned = True
            self.root.after_idle(lambda: self.viewport.yview_moveto(0))

    def _day_totals(self, rows):
        totals = defaultdict(int)
        for row in rows:
            value = row["computed_total_tokens"]
            if value is not None:
                day = datetime.fromtimestamp(row["started_at"] / 1000).date()
                totals[day] += int(value)
        return totals

    def _draw_heatmap(self):
        canvas = self.heatmap
        canvas.delete("all")
        width = max(canvas.winfo_width(), 900)
        today = date.today()
        start = today - timedelta(days=364)
        start -= timedelta(days=start.weekday())
        weeks = math.ceil(((today - start).days + 1) / 7)
        step = min(18, max(10, (width - 44) // weeks))
        cell = step - 3
        totals = self._day_totals(self.activity_rows)
        peak = max(totals.values(), default=0)
        x0, y0 = 14, 28
        last_month = None
        for week in range(weeks):
            for weekday in range(7):
                day = start + timedelta(days=week * 7 + weekday)
                if day > today:
                    continue
                value = totals.get(day, 0)
                if value == 0 or peak == 0:
                    color = "#e7e9ed"
                else:
                    strength = (value / peak) ** 0.45
                    color = ("#d8e6ff", "#abc9ff", "#77a8fa", BLUE)[min(3, int(strength * 4))]
                x, y = x0 + week * step, y0 + weekday * step
                canvas.create_rectangle(x, y, x + cell, y + cell, fill=color, outline="", tags=(f"day:{day.isoformat()}:{value}",))
                if weekday == 0 and day.month != last_month:
                    canvas.create_text(x, 9, anchor="nw", text=f"{day.month}月", fill=SUBTEXT,
                                       font=("Microsoft YaHei UI", 8))
                    last_month = day.month

    def _hover_heatmap(self, _event):
        item = self.heatmap.find_withtag("current")
        if item:
            tags = self.heatmap.gettags(item[0])
            for tag in tags:
                if tag.startswith("day:"):
                    _prefix, day, value = tag.split(":")
                    self.activity_info.configure(text=f"{day} · 本机记录 {int(value):,} Token")
                    return

    def _draw_trend(self):
        canvas = self.trend
        canvas.delete("all")
        width = max(canvas.winfo_width(), 700)
        left, right, top, bottom = 72, width - 15, 15, 165
        totals = self._day_totals(self.activity_rows)
        today = date.today()
        days = [today - timedelta(days=i) for i in range(self.trend_days - 1, -1, -1)]
        values = [totals.get(day, 0) for day in days]
        maximum = max(values, default=0)
        for fraction in (0, .5, 1):
            y = bottom - (bottom - top) * fraction
            canvas.create_line(left, y, right, y, fill=LINE)
            canvas.create_text(left - 6, y, anchor="e", text=compact(round(maximum * fraction)),
                               fill=SUBTEXT, font=("Microsoft YaHei UI", 8))
        self._trend_hitboxes = []
        slot = (right - left) / len(days)
        bar = min(34, slot * .58)
        for index, (day, value) in enumerate(zip(days, values)):
            x = left + (index + .5) * slot
            height = (bottom - top) * value / maximum if maximum else 0
            if value:
                canvas.create_rectangle(x - bar / 2, bottom - height, x + bar / 2, bottom,
                                        fill=BLUE, outline="")
            if self.trend_days == 7 or index % 5 == 0 or index == len(days) - 1:
                canvas.create_text(x, bottom + 12, text=f"{day.month}/{day.day}",
                                   fill=SUBTEXT, font=("Microsoft YaHei UI", 8))
            self._trend_hitboxes.append((x - slot / 2, x + slot / 2, day, value))
        if maximum == 0:
            canvas.create_text((left + right) / 2, (top + bottom) / 2, text="这段时间暂无本机记录",
                               fill=SUBTEXT, font=("Microsoft YaHei UI", 10))

    def _hover_trend(self, event):
        for left, right, day, value in self._trend_hitboxes:
            if left <= event.x < right:
                self.trend_info.configure(text=f"{day.isoformat()} · 本机记录 {value:,} Token")
                return

    def _draw_models(self):
        canvas = self.model_chart
        canvas.delete("all")
        groups = defaultdict(int)
        for row in self.model_rows:
            value = row["computed_total_tokens"]
            if value is not None:
                groups[(row["provider_id"] or "未知", row["model_id"] or "未知")] += int(value)
        entries = sorted(groups.items(), key=lambda item: item[1], reverse=True)
        canvas.configure(height=max(110, min(260, len(entries) * 42 + 18)))
        width = max(canvas.winfo_width(), 700)
        total = sum(groups.values())
        if not entries:
            canvas.create_text(width / 2, 55, text="当前范围暂无模型用量", fill=SUBTEXT,
                               font=("Microsoft YaHei UI", 10))
            return
        for index, ((provider, model), value) in enumerate(entries[:6]):
            y = 16 + index * 42
            canvas.create_text(2, y, anchor="nw", text=model, fill=TEXT,
                               font=("Microsoft YaHei UI", 9, "bold"))
            canvas.create_text(width - 5, y, anchor="ne", text=f"{compact(value)} Token  ·  {value / total:.1%}" if total else "0 Token",
                               fill=SUBTEXT, font=("Microsoft YaHei UI", 9))
            canvas.create_rectangle(2, y + 23, width - 5, y + 31, fill="#e6e9ef", outline="")
            if value and total:
                canvas.create_rectangle(2, y + 23, 2 + (width - 7) * value / total, y + 31,
                                        fill=PALETTE[index % len(PALETTE)], outline="")

    def _set_status(self):
        if self.last_error:
            text = f"读取状态：{self.last_error}"
            if self.last_success:
                text += f" · 上次成功：{self.last_success.strftime('%Y-%m-%d %H:%M:%S')}（数据可能过期）"
            color = "#ad6a16"
        elif self.last_success:
            text = f"本地数据库读取正常 · 上次读取 {self.last_success.strftime('%Y-%m-%d %H:%M:%S')} · 每 3 秒检查变化"
            color = "#397b59"
        else:
            text = "正在读取本地数据库…"
            color = BLUE
        self.status_label.configure(text=text, fg=color)


def main():
    root = tk.Tk()
    DashboardApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
