"""系统日志目录：logs/YYYY-MM-DD/<name>，按天滚动，只保留最近 KEEP_DAYS 天（默认 90，含今天）。

用法：
    import logroll
    logroll.path("act.jsonl")              # 今天的日志文件路径（目录自动创建）
    p = logroll.DailyPath("unknown.jsonl") # 长时间运行的进程用：每次 str(p) 都按"当前日期"解析，跨零点自动换到新目录
    logroll.latest("text__xxx.jsonl")      # 最近一天里存在的同名日志（分析/回放脚本的默认输入）
    logroll.find_all("text__xxx.jsonl")    # 所有日期里的同名日志，按日期升序

只清理名字是 YYYY-MM-DD 的子目录；logs/ 根下的其他文件（如 stack_cache.json、_backup/、.ring/）不受影响。
保留天数可用环境变量 EQUIPOISE_LOG_KEEP_DAYS 覆盖。
"""
import os, re, shutil, sys, threading, time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / "logs"
KEEP_DAYS = int(os.environ.get("EQUIPOISE_LOG_KEEP_DAYS", "90"))
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_lock = threading.Lock()
_cleaned_for = None          # 当天已清理过就不再扫目录


def today_str(t=None):
    return time.strftime("%Y-%m-%d", time.localtime(t))


def cleanup(keep=KEEP_DAYS, today=None):
    """删除早于"今天往前 keep-1 天"的日期目录，返回被删的目录名。"""
    today = today or date.today()
    cutoff = (today - timedelta(days=keep - 1)).isoformat()
    removed = []
    if not LOGS.is_dir(): return removed
    for d in LOGS.iterdir():
        if d.is_dir() and _DAY.match(d.name) and d.name < cutoff:
            try:
                shutil.rmtree(d); removed.append(d.name)
            except OSError as e:
                print(f"[logroll] 清理 {d} 失败: {e}", file=sys.stderr)
    return removed


def day_dir(t=None):
    global _cleaned_for
    name = today_str(t)
    d = LOGS / name
    d.mkdir(parents=True, exist_ok=True)
    if t is None and _cleaned_for != name:
        with _lock:
            if _cleaned_for != name:
                _cleaned_for = name
                cleanup()
    return d


def path(name, t=None):
    return day_dir(t) / name


class DailyPath(os.PathLike):
    """按写入时刻解析到当天目录的路径；可直接传给 open() / AsyncWriter。"""
    def __init__(self, name): self.name = name
    def __fspath__(self): return str(path(self.name))
    def __str__(self): return self.__fspath__()
    def __repr__(self): return f"DailyPath({self.name!r})"


def find_all(name):
    if not LOGS.is_dir(): return []
    return [d / name for d in sorted(LOGS.iterdir()) if d.is_dir() and _DAY.match(d.name) and (d / name).exists()]


def latest(name):
    found = find_all(name)
    return found[-1] if found else LOGS / today_str() / name
