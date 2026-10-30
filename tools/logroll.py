"""系统日志目录：logs/YYYY-MM-DD/<name>，按天滚动，只保留最近 KEEP_DAYS 天（默认 90，含今天）。

用法：
    import logroll
    logroll.path("act.jsonl")              # 今天的日志文件路径（目录自动创建）
    p = logroll.DailyPath("unknown.jsonl") # 长时间运行的进程用：每次 str(p) 都按"当前日期"解析，跨零点自动换到新目录
    logroll.latest("text__xxx.jsonl")      # 最近一天里存在的同名日志（分析/回放脚本的默认输入）
    logroll.find_all("text__xxx.jsonl")    # 所有日期里的同名日志，按日期升序
    log = logroll.logger("bot")            # 标准后端日志：log.info / log.warning / log.error(..., exc_info=True)
                                           # 格式 "2026-10-30 18:20:01.123 INFO  [MainThread] bot - 消息"
    logroll.ops_start()                    # tlive 真机：所有 logger 同时写 logs/<日期>/ops.log（后台线程，跨零点换文件），含未捕获异常 traceback

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


_ROOT_LOG = "equipoise"
_FMT = "%(asctime)s.%(msecs)03d %(levelname)-5s [%(threadName)s] %(mod)s - %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"
_listener = None


def _formatter():
    import logging
    f = logging.Formatter(_FMT, _DATEFMT)
    _fmt = f.format
    def fmt(r):                                     # WARNING -> WARN（后端常见写法，对齐 5 列）；模块名去掉 equipoise. 前缀
        if r.levelno == logging.WARNING: r.levelname = "WARN"
        r.mod = r.name.removeprefix(_ROOT_LOG + ".")
        return _fmt(r)
    f.format = fmt
    return f


def logger(name):
    """标准后端日志：logroll.logger("bot").info/warning/error(...)。
    格式：2026-10-30 18:20:01.123 INFO  [MainThread] bot - 消息；INFO 打到 stdout，WARN/ERROR 打到 stderr。
    extra={"file_only": True} 的行只进 ops.log 不打控制台（如每个动作一行，控制台已有 JSON）"""
    import logging
    root = logging.getLogger(_ROOT_LOG)
    if not root.handlers:
        root.setLevel(logging.INFO); root.propagate = False
        keep = lambda r: not getattr(r, "file_only", False)
        out = logging.StreamHandler(sys.stdout); out.addFilter(lambda r: keep(r) and r.levelno < logging.WARNING)
        err = logging.StreamHandler(sys.stderr); err.addFilter(lambda r: keep(r) and r.levelno >= logging.WARNING)
        for h in (out, err): h.setFormatter(_formatter()); root.addHandler(h)
    return logging.getLogger(f"{_ROOT_LOG}.{name}")


class _DailyFileHandler:
    """logging handler：写 logs/<当天>/<name>，跨零点自动换目录"""
    def __new__(cls, name):
        import logging
        class H(logging.Handler):
            def __init__(self):
                super().__init__(); self.p, self.cur, self.f = DailyPath(name), None, None
            def emit(self, r):
                try:
                    if str(self.p) != self.cur:
                        if self.f: self.f.close()
                        self.cur = str(self.p); self.f = open(self.cur, "a", encoding="utf-8")
                    self.f.write(self.format(r) + "\n"); self.f.flush()
                except Exception:
                    self.handleError(r)
        return H()


def ops_start(name="ops.log"):
    """开启 logs/<日期>/<name>：之后所有 logroll.logger(...) 的日志同时写进文件（QueueListener 后台线程写，不阻塞主线程）；
    未捕获的异常（主线程和子线程）以 ERROR + traceback 写入。只在 tlive 里调用，测试/回放/工具脚本只打印"""
    global _listener
    if _listener is not None: return
    import logging, logging.handlers, queue
    logger("ops")                                   # 确保控制台 handler 已装
    q = queue.Queue()
    fh = _DailyFileHandler(name); fh.setFormatter(_formatter())
    _listener = logging.handlers.QueueListener(q, fh, respect_handler_level=False)
    logging.getLogger(_ROOT_LOG).addHandler(logging.handlers.QueueHandler(q))
    _listener.start()
    log = logger("uncaught")
    old = sys.excepthook
    def hook(tp, v, tb):
        log.error("未捕获异常（主线程）: %s: %s", tp.__name__, v, exc_info=(tp, v, tb), extra={"file_only": True}); ops_flush(); old(tp, v, tb)
    sys.excepthook = hook
    old_t = threading.excepthook
    def thook(a):
        log.error("未捕获异常（线程 %s）: %s: %s", a.thread.name if a.thread else "?", a.exc_type.__name__, a.exc_value,
                  exc_info=(a.exc_type, a.exc_value, a.exc_traceback), extra={"file_only": True}); ops_flush(); old_t(a)
    threading.excepthook = thook


def ops_flush():
    """把队列里的日志写完（tlive 结束/进程退出前调用）。QueueListener.stop 会排空队列，然后重启以便继续写"""
    global _listener
    if _listener is not None:
        _listener.stop(); _listener.start()


def find_all(name):
    if not LOGS.is_dir(): return []
    return [d / name for d in sorted(LOGS.iterdir()) if d.is_dir() and _DAY.match(d.name) and (d / name).exists()]


def latest(name):
    found = find_all(name)
    return found[-1] if found else LOGS / today_str() / name
