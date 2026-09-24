"""Offline, read-only monitor for ZCode's local model_usage SQLite table."""
from __future__ import annotations

import json
import os
import queue
import sqlite3
import threading
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime, time as day_time, timedelta
from pathlib import Path
from tkinter import filedialog, ttk

APP_TITLE = "ZCode 本机用量监控器"
DEFAULT_DB = Path.home() / ".zcode" / "cli" / "db" / "db.sqlite"
CONFIG_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ZCodeTokenMonitor"
CONFIG_PATH = CONFIG_DIR / "settings.json"
POLL_SECONDS = 3.0
QUERY_TIMEOUT = 0.25
REQUIRED_COLUMNS = {
    "id", "provider_id", "model_id", "query_source", "status", "started_at",
    "input_tokens", "output_tokens", "reasoning_tokens",
    "cache_creation_input_tokens", "cache_read_input_tokens", "computed_total_tokens",
}
SOURCE_LABELS = {
    "main_turn": "主任务", "session_title": "标题生成", "subagent": "子智能体",
}
SOURCE_LABELS_ORDERED = [("全部来源", "*"), ("主任务", "main_turn"), ("标题生成", "session_title"), ("子智能体", "subagent")]
PERIODS = [("今日", "today"), ("最近 7 天", "7d"), ("全部历史", "all")]
BG = "#191a1e"
PANEL = "#24262b"
FG = "#e6e6e7"
MUTED = "#96999f"
ACCENT = "#72a7f8"


def local_path(path: Path) -> bool:
    """Reject UNC and Windows drive paths that resolve to network providers."""
    try:
        text = str(path.resolve(strict=False))
        if text.startswith("\\\\") or text.startswith("//"):
            return False
        if os.name == "nt":
            import ctypes
            drive = Path(text).drive
            if drive:
                get_drive_type = ctypes.windll.kernel32.GetDriveTypeW
                get_drive_type.argtypes = [ctypes.c_wchar_p]
                get_drive_type.restype = ctypes.c_uint
                if get_drive_type(drive + "\\") == 4:  # DRIVE_REMOTE
                    return False
        return True
    except (OSError, ValueError):
        return False


def file_signature(path: Path) -> tuple | None:
    try:
        db = path.stat()
        wal = Path(str(path) + "-wal")
        wal_stat = wal.stat() if wal.exists() else None
        return (
            db.st_mtime_ns, db.st_size,
            wal_stat.st_mtime_ns if wal_stat else None,
            wal_stat.st_size if wal_stat else None,
        )
    except OSError:
        return None


def query_usage(path: Path) -> tuple[list[dict], tuple]:
    if not path.is_file():
        raise FileNotFoundError(f"找不到数据库：{path}")
    if not local_path(path):
        raise ValueError("只支持本机磁盘上的数据库文件，不读取网络共享路径。")
    uri = path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=QUERY_TIMEOUT)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
        connection.execute(f"PRAGMA busy_timeout = {int(QUERY_TIMEOUT * 1000)}")
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "model_usage" not in tables:
            raise ValueError("该 SQLite 文件没有 model_usage 表；请选用当前 ZCode 的 CLI 用量数据库。")
        columns = {row[1] for row in connection.execute("PRAGMA table_info(model_usage)")}
        missing = REQUIRED_COLUMNS - columns
        if missing:
            raise ValueError("model_usage 表结构不兼容，缺少字段：" + ", ".join(sorted(missing)))
        sql = """SELECT id, provider_id, model_id, query_source, status, started_at,
                        input_tokens, output_tokens, reasoning_tokens,
                        cache_creation_input_tokens, cache_read_input_tokens,
                        computed_total_tokens
                 FROM model_usage ORDER BY started_at DESC"""
        rows = [dict(row) for row in connection.execute(sql)]
        schema = tuple(sorted(columns))
        return rows, schema
    finally:
        connection.close()


def format_num(value) -> str:
    return "未知" if value is None else f"{int(value):,}"


def period_start(period: str) -> int | None:
    now = datetime.now().astimezone()
    today_start = datetime.combine(now.date(), day_time.min).astimezone()
    if period == "today":
        return int(today_start.timestamp() * 1000)
    if period == "7d":
        week_start = datetime.combine(now.date() - timedelta(days=6), day_time.min).astimezone()
        return int(week_start.timestamp() * 1000)
    return None


@dataclass(frozen=True)
class OfficialSnapshot:
    """Only populated by a future verified local official-usage data source."""
    deducted: int | float | None = None
    unit: str | None = None
    scope: str | None = None
    period_start: datetime | None = None
    period_end: datetime | None = None
    reset_at: datetime | None = None
    observed_at: datetime | None = None


def official_value_text(value: int | float | None, unit: str | None) -> str:
    if value is None:
        return "未获取"
    return f"{value:,}" + (f" {unit}" if unit else "（原始单位未知）")


def official_time_text(value: datetime | None) -> str:
    if value is None or value.tzinfo is None or value.utcoffset() is None:
        return "未获取"
    source_zone = value.strftime("%z") or "时区未知"
    return f"{value.astimezone().strftime('%Y-%m-%d %H:%M:%S')}（来源 {source_zone}）"


def official_snapshot_status(snapshot: OfficialSnapshot, now: datetime | None = None) -> str:
    if snapshot.reset_at is None or snapshot.reset_at.tzinfo is None or snapshot.reset_at.utcoffset() is None:
        return "官方状态：未获取"
    if snapshot.observed_at is None or snapshot.observed_at.tzinfo is None or snapshot.observed_at.utcoffset() is None:
        return "官方状态：未获取"
    current = now or datetime.now().astimezone()
    if current >= snapshot.reset_at and snapshot.observed_at < snapshot.reset_at:
        return "官方状态：等待官方更新"
    return "官方状态：已获取"


def official_period_local_tokens(rows: list[dict], snapshot: OfficialSnapshot) -> int | None:
    """Return local recorded tokens for an explicitly supplied [start, end) interval."""
    if (snapshot.period_start is None or snapshot.period_end is None
            or snapshot.period_start.tzinfo is None or snapshot.period_end.tzinfo is None
            or snapshot.period_start.utcoffset() is None or snapshot.period_end.utcoffset() is None
            or snapshot.period_end <= snapshot.period_start):
        return None
    start_ms = int(snapshot.period_start.timestamp() * 1000)
    end_ms = int(snapshot.period_end.timestamp() * 1000)
    matching = [row for row in rows if start_ms <= row["started_at"] < end_ms]
    if any(row["computed_total_tokens"] is None for row in matching):
        return None
    return sum(int(row["computed_total_tokens"]) for row in matching)


class MonitorBackend:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("1000x700")
        self.root.minsize(800, 560)
        self.root.configure(bg=BG)
        self.db_path = self._load_path()
        self.period = tk.StringVar(value="today")
        self.source = tk.StringVar(value="*")
        self.rows: list[dict] = []
        self.official = OfficialSnapshot()
        self.last_success: datetime | None = None
        self.last_signature = None
        self.last_error: str | None = None
        self.stop_event = threading.Event()
        self.refresh_event = threading.Event()
        self.messages: queue.Queue = queue.Queue()
        self.worker = threading.Thread(target=self._worker_loop, name="zcode-local-usage-reader", daemon=True)
        self._build_style()
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.worker.start()
        self.root.after(100, self._poll_messages)

    def _load_path(self) -> Path:
        try:
            configured = Path(json.loads(CONFIG_PATH.read_text(encoding="utf-8"))["database"])
            if local_path(configured):
                return configured
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return DEFAULT_DB

    def _save_path(self):
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            CONFIG_PATH.write_text(json.dumps({"database": str(self.db_path)}, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            self.last_error = f"无法保存监控器设置：{exc}"

    def _schedule_local_midnight_refresh(self):
        now = datetime.now().astimezone()
        tomorrow = datetime.combine(now.date() + timedelta(days=1), day_time.min).astimezone()
        delay_ms = max(1000, int((tomorrow - now).total_seconds() * 1000) + 100)
        self.root.after(delay_ms, self._on_local_midnight)

    def _on_local_midnight(self):
        if self.stop_event.is_set():
            return
        self._render()
        self._schedule_local_midnight_refresh()

    def _period_changed(self, index: int):
        self.period.set(PERIODS[index][1])
        self._render()

    def _source_changed(self, index: int):
        self.source.set(SOURCE_LABELS_ORDERED[index][1])
        self._render()

    def choose_database(self):
        chosen = filedialog.askopenfilename(title="选择 ZCode 本地数据库", initialdir=str(self.db_path.parent), filetypes=[("SQLite 数据库", "*.sqlite *.db"), ("所有文件", "*.*")])
        if not chosen:
            return
        candidate = Path(chosen)
        if not local_path(candidate):
            self.status_label.configure(text="只支持本机磁盘上的数据库文件，不读取网络共享路径。", fg="#f0a565")
            return
        self.db_path = candidate
        self.path_label.configure(text=f"数据库：{self.db_path}")
        self._save_path()
        self.last_signature = None
        self.messages.put(("refresh", None))

    def _worker_loop(self):
        backoff = POLL_SECONDS
        first_pass = True
        while not self.stop_event.is_set():
            if not first_pass:
                self.refresh_event.wait(backoff)
                self.refresh_event.clear()
                if self.stop_event.is_set():
                    break
            first_pass = False
            path = self.db_path
            signature = file_signature(path)
            if signature is None:
                self.messages.put(("error", (f"数据库不存在或暂不可访问：{path}", None)))
                backoff = min(15.0, max(POLL_SECONDS, backoff * 1.5))
                continue
            if signature != self.last_signature:
                try:
                    rows, schema = query_usage(path)
                    signature_after = file_signature(path)
                    if path != self.db_path:
                        continue
                    self.last_signature = signature_after or signature
                    backoff = POLL_SECONDS
                    self.messages.put(("data", (rows, datetime.now(), signature_after, schema)))
                except sqlite3.OperationalError as exc:
                    message = "数据库正被 ZCode 写入，稍后自动重试。" if "locked" in str(exc).lower() or "busy" in str(exc).lower() else f"读取数据库失败：{exc}"
                    self.messages.put(("error", (message, None)))
                    backoff = min(5.0, max(0.5, backoff * 1.5))
                except (OSError, ValueError, sqlite3.DatabaseError) as exc:
                    self.messages.put(("error", (str(exc), None)))
                    backoff = min(15.0, max(POLL_SECONDS, backoff * 1.5))
    
    def _poll_messages(self):
        if self.stop_event.is_set():
            return
        try:
            while True:
                kind, payload = self.messages.get_nowait()
                if kind == "data":
                    self.rows, self.last_success, self.last_signature, _schema = payload
                    self.last_error = None
                    self._render()
                elif kind == "error":
                    message, _ = payload
                    self.last_error = message
                    self._set_status()
                elif kind == "refresh":
                    self.last_signature = None
                    self.refresh_event.set()
        except queue.Empty:
            pass
        self.root.after(150, self._poll_messages)

    def _set_status(self):
        if self.last_error:
            text = f"采集状态：{self.last_error}"
            if self.last_success:
                text += f"  ·  上次成功刷新：{self.last_success.astimezone().strftime('%Y-%m-%d %H:%M:%S')}（数据可能过期）"
            color = "#f0a565"
        elif self.last_success:
            text = f"采集状态：正常 · 上次成功刷新：{self.last_success.astimezone().strftime('%Y-%m-%d %H:%M:%S')} · 每 3 秒检测数据库变化"
            color = "#85c995"
        else:
            text = "采集状态：等待首次读取本地数据库"
            color = ACCENT
        self.status_label.configure(text=text, fg=color)

    def close(self):
        self.stop_event.set()
        self.refresh_event.set()
        self.root.destroy()


if __name__ == "__main__":
    from dashboard import main
    main()
