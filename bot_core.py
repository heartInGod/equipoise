"""WePoker 机器人核心（可注入：帧源 / 点击 IO / 模型 / 时钟），供 poker_bot.py tlive 与 tests/ 使用。

延迟设计（目标：画面出现 -> 点击 6~8s，最坏 ≤12s）：
- Grabber 后台线程连续截图（环形 6 个文件，os.replace 原子替换），主循环永远拿最新帧，不再串行等截图；
  点击后 busy_until+0.25s 之前开始的帧一律丢弃（那是点击前的旧画面）。
- 常驻 OCR 进程（state.OCRS/OCRC），parse ≈0.2s。
- AdbIO 常驻 `adb shell`：tap/全下序列写一行就返回，设备按顺序执行，不阻塞主循环。
- 模型：keep-alive 连接池 + 后台预热；HEDGE_AFTER 秒没返回就再发一个对冲请求，先到的有效结果胜出；
  到 截图时刻+BUDGET 仍无结果 -> rule_decide 本地规则兜底（不再超时被系统自动弃牌）。
- 日志 / 截图归档：hardlink（瞬时）+ AsyncWriter 线程写文件。
"""
import http.client, json, os, queue, re, ssl, subprocess, sys, threading, time
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "tools"))
import state as wpstate
import logroll

LOG = logroll.logger("bot")                               # 标准日志 INFO/WARN/ERROR；tlive 时同时写 logs/<日期>/ops.log

ACTS = ("弃牌", "让牌", "跟注", "下注", "加注", "全下")
ALLIN_KEYS = ("All in", "ALL IN", "全下")

# short 模式（备用：单帧调试 / 非 Anthropic 模型兜底 / session 出问题时对照；tlive 默认 session）：状态由 state.brief_text 生成，约 650 字
TPROMPT = """WePoker 无限注德州单挑，盲注1/2。下面的事实由代码算出，直接采用；「可选」是我方按钮。
翻前按代码给的「起手牌 前X%」决定（X越小越强）：按钮位前85%开2~2.5BB，其余弃；大盲面对2~3BB开局：前12% 3bet，前65%跟，其余弃；对手平跟(limp)：前30%加注3BB，其余让牌；面对3bet：前4% 4bet，前20%跟，其余弃；面对4bet+：仅前4%继续；面对全下或超过5BB的加注：按「翻前面对大注」结论，够跟就跟，否则弃。
翻后：顶对及以上价值下注/加注2/3池；≥8outs强听牌可半诈唬1/3~1/2池；翻前加注方在干燥牌面可1/3池持续下注；中等牌过牌控池；面对下注时用代码给的「听牌胜率」或成牌胜率对比「所需胜率」，不够就弃（差≤5%且有位置可跟），后门不算。
{state}
只输出JSON：{{"act":"弃牌|让牌|跟注|下注|加注|全下","size":"下注/加注填[]里的档位名如 3BB、1/2，否则null","why":"≤15字"}}"""
PROMPTS = {"short": (TPROMPT, lambda st, h: wpstate.brief_text(st, h))}


def parse_json(text):
    i = text.find("{")
    if i < 0: raise ValueError(f"无 JSON: {text[:200]}")
    return json.JSONDecoder().raw_decode(text, i)[0]   # 只取第一个对象，模型偶尔在后面多输出一段


# ======================= 点击 IO =======================
class AdbIO:
    """常驻 `adb shell`：每个动作写一行命令立即返回；设备端按顺序执行。
    busy_until = 预计设备执行完的时刻（用于丢弃点击前开始的截图）。常驻 shell 不可用时退回单次 adb 进程。"""
    EST = {"tap": 0.35, "allin": 2.3, "raise": 1.3}

    def __init__(self, adb=wpstate.ADB, dev=wpstate.DEV, persistent=True):
        self.adb, self.dev, self.p, self.busy_until = adb, dev, None, 0.0
        self.persistent = persistent and self._probe()

    def _start(self):
        self.p = subprocess.Popen([self.adb, "-s", self.dev, "shell"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True, bufsize=1)

    def _probe(self):
        """确认常驻 shell 真的会执行 stdin 里的命令（echo 回显）"""
        try:
            self._start(); self.p.stdin.write("echo __ready__\n"); self.p.stdin.flush()
            box = []
            th = threading.Thread(target=lambda: box.append(self.p.stdout.readline()), daemon=True); th.start(); th.join(3)
            if box and "__ready__" in box[0]:
                threading.Thread(target=lambda: [None for _ in self.p.stdout], daemon=True).start()   # 之后的输出丢弃
                return True
        except Exception:
            pass
        try: self.p and self.p.kill()
        except Exception: pass
        self.p = None
        return False

    def _sh(self, cmd, kind):
        t = time.time()
        if self.persistent:
            try:
                if self.p is None or self.p.poll() is not None: self._start()
                self.p.stdin.write(cmd + "\n"); self.p.stdin.flush()
            except Exception:
                self.p = None; self.persistent = False
        if not self.persistent:
            subprocess.Popen([self.adb, "-s", self.dev, "shell", cmd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.busy_until = max(self.busy_until, t) + self.EST[kind]

    def tap(self, x, y): self._sh(f"input tap {int(x)} {int(y)}", "tap")

    def allin(self, x, y):
        """主动全下：点加注 -> 滑条拖到顶 -> 点确定，必须一条命令连续完成（中途停下会超时被自动弃牌）"""
        x, y = int(x), int(y)
        self._sh(f"input tap {x} {y}; sleep 0.6; input swipe {x} 2432 {x} 1420 600; sleep 0.3; input tap {x} {y - 7}", "allin")

    def raise_min(self, x, y):
        """没有额度预设时：点加注(弹出滑条，默认=最小加注) -> 点确定，一条命令完成"""
        x, y = int(x), int(y)
        self._sh(f"input tap {x} {y}; sleep 0.6; input tap {x} {y - 7}", "raise")

    def capture(self, path):
        with open(path, "wb") as f:
            r = subprocess.run([self.adb, "-s", self.dev, "exec-out", "screencap"], stdout=f, stderr=subprocess.DEVNULL)
        return r.returncode == 0 and os.path.getsize(path) > 1000


class RecIO:
    """回放 / 测试用：只记录动作"""
    def __init__(self): self.taps, self.busy_until = [], 0.0
    def tap(self, x, y): self.taps.append((int(x), int(y)))
    def allin(self, x, y): self.taps.append(("allin", int(x), int(y)))
    def raise_min(self, x, y): self.taps.append(("raise_min", int(x), int(y)))


# ======================= 截图 / 写盘 =======================
class Grabber(threading.Thread):
    """后台连续截图。get(min_t) 返回 (path, t_start)：开始时间 ≥ min_t 的最新一帧。"""
    def __init__(self, io, ring_dir, n=6):
        super().__init__(daemon=True)
        self.io, self.ring, self.n = io, Path(ring_dir), n
        self.ring.mkdir(parents=True, exist_ok=True)
        self.cv, self.latest, self.stop, self.fails, self.periods = threading.Condition(), None, False, 0, []

    def run(self):
        i, tmp, last = 0, self.ring / "grab.tmp", None
        while not self.stop:
            t = time.time()
            try: ok = self.io.capture(str(tmp))
            except Exception: ok = False
            if not ok:
                self.fails += 1
                if self.fails % 5 == 1: LOG.warning("[grab] 设备无响应（%d 次），1s 后重试", self.fails)
                time.sleep(1); continue
            self.fails = 0
            slot = self.ring / f"{i % self.n}.raw"; i += 1
            try: os.replace(tmp, slot)
            except OSError: continue      # 新 inode：已 hardlink 走的旧帧不受影响；正在读旧帧的进程也不受影响
            if last: self.periods = (self.periods + [t - last])[-50:]
            last = t
            with self.cv:
                self.latest = (str(slot), t); self.cv.notify_all()

    def get(self, min_t, timeout=600):
        end = time.time() + timeout
        with self.cv:
            while self.latest is None or self.latest[1] < min_t:
                if not self.cv.wait(timeout=max(0.01, end - time.time())) and time.time() >= end:
                    raise RuntimeError("截图超时（设备掉线？）")
            return self.latest


class AsyncWriter(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.q = queue.Queue(); self.files = {}; self.start()

    def write(self, path, text): self.q.put(("a", str(path), text))
    def put_file(self, path, text): self.q.put(("w", str(path), text))

    def run(self):
        while True:
            kind, path, text = self.q.get()
            try:
                if kind == "a":
                    f = self.files.get(path)
                    if f is None:
                        parent = os.path.dirname(path)
                        for k in [k for k in self.files if os.path.dirname(k) != parent]:   # 跨天滚动：关掉旧日期目录的句柄
                            self.files.pop(k).close()
                        f = self.files[path] = open(path, "a")
                    f.write(text); f.flush()
                else:
                    with open(path, "w") as f: f.write(text)
            except Exception as e:
                LOG.error("[writer] 写 %s 失败: %s", path, e, exc_info=True)
            self.q.task_done()

    def flush(self): self.q.join()


def keep_frame(src, dst):
    """归档截图：同卷 hardlink（瞬时），失败才拷贝"""
    try: os.link(src, dst)
    except FileExistsError: pass
    except OSError:
        import shutil; shutil.copy(src, dst)


# ======================= 模型 =======================
class AnthropicKA:
    """Anthropic messages API 长连接池：后台保持 2 条已完成 TLS 握手的连接，空闲 40s 重建。"""
    def __init__(self, ref, warm=2, idle=40):
        from poker_bot import load_providers, protocol
        prov, self.model = ref.split("/", 1)
        p = load_providers()[prov]
        if protocol(p) != "anthropic": raise ValueError("非 anthropic 协议")
        u = urlparse(p["baseURL"].rstrip("/") + "/messages")
        self.host, self.port, self.path = u.hostname, u.port or 443, u.path
        key = p["apiKey"]
        self.H = {"Content-Type": "application/json", **p["headers"], "x-api-key": key, "Authorization": f"Bearer {key}",
                  "anthropic-version": "2023-06-01"}
        self.ctx, self.lock, self.pool, self.warm, self.idle = ssl.create_default_context(), threading.Lock(), [], warm, idle
        threading.Thread(target=self._keeper, daemon=True).start()

    def _new(self):
        c = http.client.HTTPSConnection(self.host, self.port, timeout=10, context=self.ctx); c.connect()
        return [c, time.time()]

    def _keeper(self):
        while True:
            with self.lock:
                old = [it for it in self.pool if time.time() - it[1] > self.idle]
                self.pool = [it for it in self.pool if it not in old]; n = len(self.pool)
            for it in old:
                try: it[0].close()
                except Exception: pass
            for _ in range(self.warm - n):
                try:
                    it = self._new()
                    with self.lock: self.pool.append(it)
                except Exception:
                    pass
            time.sleep(3)

    def __call__(self, prompt, timeout=12, max_tokens=200):
        """prompt: 文本，或 session 模式的 {"system": [...], "messages": [...]}"""
        if isinstance(prompt, dict):
            req = {"system": prompt["system"], "messages": prompt["messages"]}
        else:
            req = {"messages": [{"role": "user", "content": [{"type": "text", "text": prompt}]}]}
        body = json.dumps({"model": self.model, "max_tokens": max_tokens, "temperature": 0, "thinking": {"type": "disabled"}, **req})
        for attempt in range(2):
            with self.lock: it = self.pool.pop() if self.pool else None
            fresh = it is None
            c = (it or self._new())[0]
            try:
                c.sock.settimeout(timeout)
                c.request("POST", self.path, body, self.H); r = c.getresponse(); d = r.read()
                if r.status != 200:
                    c.close(); raise RuntimeError(f"HTTP {r.status}: {d[:200].decode(errors='replace')}")
                with self.lock: self.pool.append([c, time.time()])
                d = json.loads(d)
                return "".join(x.get("text", "") for x in d.get("content", []) if x.get("type") == "text")
            except (http.client.RemoteDisconnected, BrokenPipeError, ConnectionResetError, http.client.CannotSendRequest):
                c.close()
                if fresh or attempt: raise       # 复用的连接被服务端关了 -> 换新连接重试一次
            except Exception:
                c.close(); raise


def generic_model(ref):
    """非 anthropic provider：沿用 poker_bot.call_model（每次新建连接）"""
    from poker_bot import call_model
    from session_prompt import flatten
    return lambda prompt, timeout=12, max_tokens=200: call_model(ref, flatten(prompt), None, timeout=timeout, max_tokens=max_tokens)


def make_model(ref):
    try: return AnthropicKA(ref)
    except ValueError: return generic_model(ref)


class Decider:
    """对冲请求 + 硬 deadline。raw(prompt, timeout) -> 文本。ask() 返回 (dec 或 None, info)。"""
    def __init__(self, raw, hedge_after=3.0, max_hedge=1):
        self.raw, self.hedge_after, self.max_hedge = raw, hedge_after, max_hedge

    def ask(self, prompt, deadline):
        q, n = queue.Queue(), [0]
        t0 = time.time()

        def worker(k):
            t = time.time()
            try:
                dec = parse_json(self.raw(prompt, timeout=max(1.0, deadline - time.time() + 0.5)))
                if dec.get("act") not in ACTS: raise ValueError(f"非法 act: {dec.get('act')}")
                q.put((k, dec, None, time.time() - t))
            except Exception as e:
                q.put((k, None, f"{type(e).__name__}: {str(e)[:60]}", time.time() - t))

        def launch():
            threading.Thread(target=worker, args=(n[0],), daemon=True).start(); n[0] += 1

        launch(); errs, done = [], 0
        hedge_t = t0 + self.hedge_after
        while True:
            now = time.time()
            if now >= deadline:
                return None, {"timeout": True, "n_req": n[0], "errs": errs, "t": round(now - t0, 2)}
            can_hedge = n[0] < 1 + self.max_hedge
            try:
                k, dec, err, dt = q.get(timeout=max(0.005, min(deadline, hedge_t if can_hedge else deadline) - now))
            except queue.Empty:
                if can_hedge and time.time() >= hedge_t: launch()
                continue
            if dec is not None:
                return dec, {"winner": k, "n_req": n[0], "t": round(time.time() - t0, 2), "errs": errs}
            errs.append(err); done += 1
            if can_hedge: launch()                       # 出错立刻对冲，不等 hedge_after
            elif done >= n[0]:
                return None, {"timeout": False, "n_req": n[0], "errs": errs, "t": round(time.time() - t0, 2)}


# ======================= 本地规则（模型超时/出错兜底） =======================
_BW = "23456789TJQKA"


def _pre_class(hero):
    if len(hero) != 2 or any("?" in c for c in hero): return "unknown"
    r = sorted((_BW.index(c[:-1].replace("10", "T")) for c in hero), reverse=True)
    suited = hero[0][-1] == hero[1][-1]
    hi, lo = r
    if hi == lo: return "premium" if hi >= _BW.index("T") else "playable"
    if hi == 12 and lo >= _BW.index("Q"): return "premium"                         # AK AQ
    if hi == 12 or (hi >= _BW.index("J") and lo >= _BW.index("T")): return "playable"   # Ax、KQ KJ KT QJ QT JT
    if suited and hi - lo <= 2 and lo >= _BW.index("5"): return "playable"         # 同花连张/隔张
    if hi >= _BW.index("K") and suited: return "playable"
    return "trash"


def _pick_preset(st, want):
    ps = wpstate.usable_presets(st)
    for w in want:
        p = next((p for p in ps if p["frac"] == w), None)
        if p: return p["frac"]
    return ps[min(2, len(ps) - 1)]["frac"] if ps else None


def board_danger(board):
    """公共牌危险度：fl4=四张同花(一张牌成同花)、st4=一张牌就能成顺、fl3=三张同花、paired=公共牌有对子"""
    suits = [c[-1] for c in board]
    bv = [wpstate._rv(c) for c in board]
    mx = max((suits.count(s) for s in set(suits)), default=0)
    st4 = len(board) >= 4 and any(wpstate._straight_hi(bv + [r]) for r in range(2, 15))
    return {"fl4": mx >= 4, "st4": st4, "fl3": mx >= 3, "paired": len(set(bv)) < len(bv)}


def _flush_beaten(hero, board):
    """公共牌 ≥4 张同花时，未出现的同花色牌里比我方最大同花色手牌大的张数（越多越危险）"""
    suits = [c[-1] for c in board]
    s = max(set(suits), key=suits.count)
    mine = [wpstate._rv(c) for c in hero if c[-1] == s]
    if not mine: return 99
    sv = lambda cs: wpstate._straight_hi([wpstate._rv(c) for c in cs if c[-1] == s]) or 0
    if sv(board + list(hero)) > sv(board): return 0      # 我方组成同花顺（made_desc 不区分同花顺）
    seen = {wpstate._rv(c) for c in board + list(hero) if c[-1] == s}
    return sum(1 for r in range(max(mine) + 1, 15) if r not in seen)


def _downgrade(hero, board, md):
    """兜底规则里“强牌”在危险牌面降为中等牌（只过牌/便宜跟注，不主动打价值）：
    - 顺子/同花/葫芦/四条 不降；
    - 公共牌四张同花或一张成顺 -> 两对、三条、顶对、超对都降；
    - 公共牌三张同花 -> 一对类（顶对/超对）降（公共牌有对子时 made_desc 给的是“两对”，走下一条）；
    - 两对里有一对是公共牌对子且比我方配上的那对大 -> 实际只是一对，降。"""
    d = board_danger(board)
    if d["fl4"] and re.search(r"同花\(我方", md) and not re.search(r"葫芦|四条|同花顺", md) and _flush_beaten(hero, board) >= 2:
        return True                         # 四张同花公共牌上只用一张小牌成的同花：比它大的同花色牌还有 ≥2 张没出现
    if re.search(r"顺子|同花\(我方|葫芦|四条", md): return False
    if d["fl4"] or d["st4"]: return True
    if re.search(r"顶对|超对", md) and d["fl3"]: return True
    if "两对" in md and "其中用到我方的" in md:
        hv, bv = {wpstate._rv(c) for c in hero}, [wpstate._rv(c) for c in board]
        bp = max((v for v in set(bv) if bv.count(v) >= 2), default=0)
        mine = max((v for v in hv if v in bv), default=0)
        return mine < bp
    return False


def rule_decide(st, hist=None):
    """保守本地规则：免费就让牌；强牌下注 2/3 池；按所需胜率跟注；其余弃牌。"""
    b, n = st["buttons"], st.get("nums") or {}
    tc, pot = n.get("to_call"), n.get("pot") or 0
    tc = tc if isinstance(tc, int) else 0
    can_check, can_bet = "让牌" in b, any(k in b for k in ("下注", "加注"))
    if tc:
        p2, t2 = wpstate.cap_call(n); need = t2 / ((p2 or 0) + t2) * 100   # 对手超出我方筹码的部分不算赔率
    else: need = 0
    chk_fold = lambda why: {"act": "让牌" if can_check else "弃牌", "size": None, "why": "规则:" + why}
    call = lambda why: {"act": "跟注", "size": None, "why": "规则:" + why}
    bet = lambda size, why: {"act": "下注", "size": size, "why": "规则:" + why}
    if not st["board"]:
        c = _pre_class(st["hero"])
        lvl = hist.level if hist is not None and hist.street == "翻前" else (2 if tc > 2 else 1)
        pr = wpstate.preflop_rank(st["hero"])
        if pr and lvl <= 1 and tc == 1 and can_bet and wpstate.usable_presets(st) and pr[1]["top"] <= 85:   # 按钮位首次行动：开局
            return bet(_pick_preset(st, ["2BB", "3BB"]), f"按钮开局 前{pr[1]['top']:.0f}%")
        if pr and tc > 10:                   # 面对全下/大额加注：按对其范围（按加注层级/大小估宽度）的胜率对比赔率
            e, x = wpstate.vs_big_eq(pr[1], lvl, (n.get("opp_bet") or 0) / 2)
            if e >= need: return call(f"对前{x}%胜率{e:.0f}%>=需{need:.0f}%")
            return chk_fold(f"对前{x}%胜率{e:.0f}%<需{need:.0f}%")
        if c == "premium":
            if can_bet and lvl <= 2 and wpstate.usable_presets(st): return bet(_pick_preset(st, ["3BB", "1/2", "2/3"]), "强牌加注")
            return call("强牌跟注") if tc else chk_fold("强牌")
        if c == "playable":
            if tc == 0: return chk_fold("可玩牌")
            return call("可玩牌跟小注") if tc <= 6 else chk_fold("可玩牌面对大注")
        return chk_fold("翻前弱牌")
    md = " ".join(wpstate.made_desc(st["hero"], st["board"]))
    made = md.split("听牌:")[0]               # 强弱只看成牌部分：听牌行里有“顺子听牌/顺子补牌”字样，不能当成顺子
    strong = bool(re.search(r"两对|三条|顺子|同花\(|葫芦|四条|顶对|超对", made)) and not re.search(r"我方仅高牌|没用到手牌|公共牌三条", made)
    medium = bool(re.search(r"一对|口袋对", made)) and not strong
    if strong and _downgrade(st["hero"], st["board"], made):
        strong, medium = False, True
    outs = wpstate.draw_outs(md.split("听牌:")[-1]) if "听牌:" in md else 0
    if strong:
        if tc == 0 and can_bet: return bet(_pick_preset(st, ["2/3", "1/2"]), "强牌价值下注")
        return call("强牌跟注") if tc else chk_fold("强牌")
    if tc == 0: return chk_fold("免费看牌")
    if medium and need < 30: return call(f"中等牌 需{need:.0f}%")
    if outs:                                 # 翻牌圈按到河牌（与旧 outs×4 口径一致），转牌按下一张
        nxt, river = wpstate.draw_eq(outs, len(st["board"]))
        if (river if len(st["board"]) == 3 else nxt) >= need: return call(f"{outs}outs 需{need:.0f}%")
    return chk_fold(f"牌力不足 需{need:.0f}%")


# ======================= 执行 =======================
def fallback(st, io):
    b = st["buttons"]
    k = "让牌" if "让牌" in b else ("弃牌" if "弃牌" in b else None)
    if k: io.tap(*b[k]); return f"兜底{k}@{b[k]}"
    return "noop"


def allin_seq(st, io):
    b = st["buttons"]
    k = next((k for k in ALLIN_KEYS if k in b), None)
    if k: io.tap(*b[k]); return f"{k}@{b[k]}"
    k = next((k for k in ("加注", "下注") if k in b), None)
    if not k: return None
    x, y = b[k]; io.allin(x, y)
    return f"{k}->滑条到顶->确定@{(x, y - 7)}"


def _all_over_stack(st):
    """全部预设都是灰的，且读得到的金额都 ≥ 我方剩余筹码+本街已下（不是滑条展开导致的整屏变暗）"""
    n = st.get("nums") or {}
    cap = (n.get("my_stack") or 0) + (n.get("my_bet") or 0)
    amts = [float(p["amt"]) for p in st["presets"] if p.get("amt") and re.fullmatch(r"\d+(\.\d+)?", p["amt"])]
    return cap > 0 and bool(amts) and not wpstate.usable_presets(st) and min(amts) >= cap


def execute(st, dec, io):
    act, size = dec.get("act"), dec.get("size")
    b = st["buttons"]
    if act in ("下注", "加注") and "确定" in b:
        io.tap(*b["确定"]); return f"滑条确定(最小加注)@{b['确定']}"     # 加注滑条已展开（默认=最小加注额）
    ps = wpstate.usable_presets(st)
    if act in ("下注", "加注") and ps:
        w = str(size or "").replace(" ", "").replace("池", "")
        p = next((p for p in ps if p["frac"] == w), None)
        if not p:
            # 档位不在当前可用预设里：按金额取最接近的预设，绝不单点加注按钮(会弹滑条)
            m = re.fullmatch(r"(\d+(?:\.\d+)?)BB", w)
            want = float(m.group(1)) * 2 if m else (float(w) if re.fullmatch(r"\d+(\.\d+)?", w) else None)
            cand = [q for q in ps if q["amt"] and re.fullmatch(r"\d+(\.\d+)?", q["amt"])]
            grey = next((i for i, q in enumerate(st["presets"]) if q["frac"] == w), None)
            if grey is not None:       # 要的档是灰的（低于最小下注/加注或超过筹码）：按档位顺序取最近的可用档（"1"/"1.2" 是档位名，不是筹码数）
                p = min(ps, key=lambda q: abs(st["presets"].index(q) - grey))
            elif want is not None and cand:
                p = min(cand, key=lambda q: abs(float(q["amt"]) - want))
            else:
                p = ps[min(2, len(ps) - 1)]
        io.tap(*p["xy"]); return f"preset {p['frac']}={p['amt']}@{p['xy']}(要{size})"
    if act in ("下注", "加注") and st["presets"] and _all_over_stack(st):
        d = allin_seq(st, io)          # 所有档位都超过筹码（灰）：想下注就是全下
        if d: return d + "(预设都超过筹码)"
    if act in ("下注", "加注"):
        k = act if act in b else next((k for k in ("加注", "下注") if k in b), None)
        if k: io.raise_min(*b[k]); return f"{k}->确定(最小加注，无预设)@{b[k]}"
    if act == "全下":
        d = allin_seq(st, io)
        if d: return d
    if act in b:
        io.tap(*b[act]); return f"{act}@{b[act]}"
    if act == "跟注":
        k = next((k for k in ALLIN_KEYS if k in b), None)
        if k: io.tap(*b[k]); return f"{k}@{b[k]}(跟注=全下)"   # 对手下注 ≥ 我方筹码：跟注按钮变成 All in
    alt = {"跟注": "让牌", "下注": "加注", "加注": "下注"}.get(act)   # 想让牌但只能跟注 -> 不自动跟注
    if alt and alt in b:
        io.tap(*b[alt]); return f"{alt}@{b[alt]}(替代{act})"
    return fallback(st, io)


def my_equity(st):
    """发牌选择弹窗里我方胜率(%)：用底部我方昵称(y≈2470)匹配弹窗内同名列(y 1250-1350)；匹配不到时默认左列是我方"""
    tx = st["texts"]
    pct = [(float(t[0][:-1]), t[1]) for t in tx if re.fullmatch(r"\d+(\.\d+)?%", t[0]) and 1500 < t[2] < 1680]
    if not pct: return None
    me = next((t[0] for t in tx if 2420 < t[2] < 2520 and not t[0].isdigit()), None)
    col = next((t[1] for t in tx if me and t[0] == me and 1250 < t[2] < 1360), None)
    if col is None: col = min(x for _, x in pct)
    return min(pct, key=lambda p: abs(p[1] - col))[0]


def real_act(dec, act_str):
    """按实际执行结果（而非模型意图）归类动作，供行动历史使用"""
    s = str(act_str)
    if s.startswith(("preset", "滑条确定")): return dec.get("act") if dec.get("act") in ("下注", "加注") else "加注"
    if s.startswith("兜底"): return s[2:4]
    if "滑条到顶" in s or s.startswith(ALLIN_KEYS): return "全下"
    for k in ("弃牌", "让牌", "跟注", "下注", "加注"):
        if s.startswith(k): return k
    return dec.get("act")


# ======================= 主逻辑（每帧一次） =======================
class Bot:
    """handle(src, st, t_shot, wall0) 处理一帧并返回日志记录。
    t_shot = 帧时间（真机=截图开始的 wall time；回放=文件名时间）；wall0 = 该帧开始处理/截图的 wall time（算 deadline 用）。"""
    def __init__(self, io, decider=None, writer=None, log_path=None, live_dir=None, unk_dir=None, unk_log=None,
                 dry=False, stuck=25, budget=8.0, q_wait=2.0, replay=False, prompt="short", session=None, preflop_rule=False):
        self.io, self.decider, self.writer, self.log_path = io, decider, writer, log_path
        self.preflop_rule = preflop_rule  # True：翻前不问模型，直接本地规则（省一次模型往返）
        self.live_dir, self.unk_dir, self.unk_log = live_dir, unk_dir, unk_log
        self.dry, self.stuck, self.budget, self.q_wait, self.replay = dry, stuck, budget, q_wait, replay
        self.sess = session              # session_prompt.HandSession（--prompt session）
        self.prompt, self.state_fn = PROMPTS["short" if prompt == "session" else prompt]
        if prompt == "session" and self.sess is None:
            from session_prompt import HandSession
            self.sess = HandSession()
        self.prev_key = self.pending = self.last_act = self.saved_stuck = self.q_since = None
        self.key_since = None
        self.retries = 0                 # 同一画面“点击未生效”重试次数
        self.cards = None                # (hero, board, t)：最近一次读全的牌，滑条展开(画面变暗读不到牌)时沿用
        self.hist = wpstate.HandHist()

    # ---- 工具 ----
    def _now(self, t_shot, wall0):
        return t_shot if self.replay else t_shot + (time.time() - wall0)

    def save_unknown(self, src, st, why):
        if self.unk_dir is None: return f"(不存图: {why})"
        stem = Path(self.unk_dir) / f"{time.strftime('%m%d_%H%M%S')}_{st['screen']}"
        LOG.warning("[异常界面] %s（截图 %s.raw）", why, stem.relative_to(ROOT) if stem.is_relative_to(ROOT) else stem, extra={"file_only": True})
        keep_frame(src, f"{stem}.raw")
        if self.writer:
            self.writer.put_file(f"{stem}.txt", f"原因: {why}\n" + wpstate.to_text(st))
            if self.unk_log:
                self.writer.write(self.unk_log, json.dumps({"t": time.strftime("%H:%M:%S"), "why": why, "raw": f"{stem}.raw", "screen": st["screen"],
                                                            "btn": st["buttons"], "texts": [t[0] for t in st["texts"]][:40]}, ensure_ascii=False) + "\n")
        return str(stem)

    def decide(self, st, wall0):
        """模型（对冲+deadline）-> 失败用本地规则；preflop_rule 时翻前直接本地规则"""
        use_model = self.decider is not None and not (self.preflop_rule and not st["board"])
        if self.sess is not None:
            tb = time.time()
            try:
                req, txt = self.sess.build(st, self.hist)
            except Exception:                      # session 记录异常不能拖垮主循环：本次用本地规则
                LOG.error("session 构建 prompt 失败，本次用本地规则", exc_info=True)
                return rule_decide(st, self.hist), {"rule": True, "sess_err": True}, ""
            t_build = time.time() - tb
            dec, info = self.decider.ask(req, wall0 + self.budget) if use_model else (None, {})
            info["t_build"] = round(t_build, 4)
            if dec is None:
                dec = rule_decide(st, self.hist); info["rule"] = True
            self.sess.commit(txt, dec)
            return dec, info, txt
        tb = time.time()
        txt = self.state_fn(st, self.hist)
        info = {"t_build": round(time.time() - tb, 4)}
        dec = None
        if use_model:
            dec, info = self.decider.ask(self.prompt.format(state=txt), wall0 + self.budget)
        if dec is None:
            dec = rule_decide(st, self.hist); info["rule"] = True
        return dec, info, txt

    # ---- 每帧 ----
    def handle(self, src, st, t_shot, wall0=None):
        wall0 = wall0 or time.time()
        io, b = self.io, st["buttons"]
        dim = None
        if len(st["hero"]) == 2 and "?" not in "".join(st["hero"]):
            self.cards = (list(st["hero"]), list(st["board"]), t_shot)
        elif "确定" in b and not st["hero"] and self.cards and t_shot - self.cards[2] < 40:
            st["hero"], st["board"] = list(self.cards[0]), list(self.cards[1])   # 加注滑条展开，整屏变暗，读不到牌
            dim = "滑条展开(画面变暗)，沿用记忆的牌"
        if self.sess is not None and hasattr(self.sess, "see_opp"):
            self.sess.see_opp(st.get("opp_name"))          # 对手昵称投票 + 异步加载跨桌画像（不阻塞本帧）
        n = st.get("nums") or {}
        hero, board = " ".join(st["hero"]), " ".join(st["board"])
        rec = {"t": time.strftime("%H:%M:%S", time.localtime(t_shot)) if not self.replay else None, "screen": st["screen"],
               "hero": hero, "board": board, "btn": list(b), "t_parse": round(st.get("ms", 0) / 1000, 2)}
        key = (st["screen"], hero, board, tuple(sorted(b)), n.get("pot"), n.get("to_call"))
        now = lambda: self._now(t_shot, wall0)
        if key != self.prev_key:
            if self.live_dir is not None:
                keep_frame(src, Path(self.live_dir) / f"{time.strftime('%H%M%S', time.localtime(t_shot))}_{st['screen']}.raw")
            self.key_since = t_shot
        scr = st["screen"]
        tap = (lambda *xy: None) if self.dry else io.tap
        if scr == "profile":
            tap(720, 300); rec["act"] = "关闭资料卡"
        elif scr == "runit":
            # 全下后发牌次数：EV 相同只影响波动。领先(>50%)发两次降低被反超风险，落后/持平发一次保留翻盘机会
            eq = my_equity(st)
            want = "发两次" if eq is not None and eq > 50 else "直接发牌"
            k = want if want in b else next(iter(b.keys() & {"直接发牌", "发两次"}))
            tap(*b[k]); rec["act"] = k; rec["note"] = f"全下发牌选择 我方胜率={eq}"
        elif scr == "away":
            tap(*b["回到牌桌"]); rec["act"] = "回到牌桌"
        elif scr == "other" and t_shot - (self.key_since or t_shot) > self.stuck and self.saved_stuck != key:
            self.saved_stuck = key
            rec["act"] = "unknown"; rec["note"] = "画面卡住>%ds，已截图 %s" % (self.stuck, self.save_unknown(src, st, f"同一画面 {self.stuck}s 未变化"))
            k = next((k for k in ("关闭", "知道了", "取消", "继续", "确定") if k in b), None)
            if k: tap(*b[k]); rec["act"] = f"unknown->点{k}"
        elif scr == "can_start":
            tap(*b["开始游戏"]); rec["act"] = "开始游戏"
        elif scr == "my_turn" and self.pending and now() - self.pending[0] < 4 and self.pending[1] == (hero, board):
            # 上一步点了额度预设，界面仍在我的回合 -> 预设只填额度，补点确认
            k = next((k for k in ("确定", "确认", "加注", "下注") if k in b), None)
            rec["act"] = f"确认{k}@{b[k]}" if k else "noop"
            if k: tap(*b[k])
            rec["note"] = "预设后仍在我的回合"; self.pending = None
        elif scr == "my_turn" and "确定" in b and self.last_act and (self.last_act[2] or {}).get("act") in ("下注", "加注") and now() - self.last_act[1] < 10:
            tap(*b["确定"]); rec["act"] = f"确定@{b['确定']}"; rec["note"] = "加注滑条已展开，按原决策点确定(最小加注)"
            self.last_act = (key, now(), self.last_act[2])
        elif scr == "anim":
            rec["act"] = "wait"; rec["note"] = "按钮动画中，等下一帧"
        elif scr == "my_turn" and self.last_act and (now() - self.last_act[1] < 1.2 or (self.last_act[0] == key and now() - self.last_act[1] < 2.5)):
            rec["act"] = "wait"     # 刚对这个画面操作过，界面还没刷新，不要重复点
        elif scr == "my_turn" and self.last_act and self.last_act[0] == key and self.retries >= 3:
            rec["act"] = "wait"; rec["note"] = "同一画面已重试 3 次仍未生效（可能有系统浮层），不再点击"
        elif scr == "my_turn" and self.last_act and self.last_act[0] == key:
            dec = self.last_act[2]; rec["dec"] = dec; rec["note"] = "上次点击未生效，重试"   # 不再调模型
            rec["act"] = "dry" if self.dry else execute(st, dec, io)
            self.last_act = (key, now(), dec); self.retries += 1
            self.save_unknown(src, st, f"点击未生效重试 {dec}")
        elif scr == "my_turn" and ("?" in hero or "?" in board or len(st["board"]) in (1, 2)) and (self.q_since is None or t_shot - self.q_since < self.q_wait):
            if self.q_since is None: self.q_since = t_shot
            rec["act"] = "wait"; rec["note"] = "牌未识别完整，等下一帧" if len(st["board"]) not in (1, 2) else "公共牌只有 1~2 张（发牌动画中），等下一帧"
        elif scr == "my_turn":
            self.q_since = None; self.retries = 0
            th = time.time()
            self.hist.observe(st)
            tm = time.time()
            dec, info, txt = self.decide(st, wall0)
            rec.update(t_hist=round(tm - th, 4), t_llm=round(time.time() - tm, 2), dec=dec, llm=info, state=txt)
            te = time.time()
            rec["act"] = "dry" if self.dry else execute(st, dec, io)
            rec["t_exec"] = round(time.time() - te, 4)
            self.last_act = (key, now(), dec)
            if not self.dry: self.hist.mine({"act": real_act(dec, rec["act"])}, rec["act"], st)
            if str(rec["act"]).startswith(("兜底", "noop")):
                rec["note"] = "动作无法执行，已截图 " + self.save_unknown(src, st, f"决策 {dec} 但执行={rec['act']}")
            self.pending = (now(), (hero, board)) if str(rec["act"]).startswith("preset") else None
        else:
            rec["act"] = "wait"
            if self.pending and now() - self.pending[0] < 4: rec["note"] = "预设已直接生效"; self.pending = None
        if scr != "my_turn": self.q_since = None
        if dim: rec["note"] = (rec.get("note", "") + " " + dim).strip()
        self.prev_key = key
        rec["lat"] = round(time.time() - wall0, 2)        # 截图开始 -> 动作发出
        if self.writer and self.log_path: self.writer.write(self.log_path, json.dumps(rec, ensure_ascii=False) + "\n")
        return rec


# ======================= 运行 =======================
SCREEN_CN = {"my_turn": "我的回合", "anim": "动画", "runit": "发牌选择", "profile": "资料卡", "away": "离开", "can_start": "可开局",
             "need_sit": "需入座", "waiting": "等待", "other": "其他"}


def ops_line(rec, st):
    """ops.log 的一行消息（人看的实时操作，时间由日志格式给出）：画面 手牌 [公共牌] 底池/需跟 | 决策(来源 耗时) 理由 -> 点击 | 总延迟 | 备注"""
    n, d = st.get("nums") or {}, rec.get("dec")
    L = [SCREEN_CN.get(rec.get("screen"), rec.get("screen"))]
    if rec.get("hero"): L.append(rec["hero"])
    if rec.get("board"): L.append(f"[{rec['board']}]")
    if n.get("pot") is not None: L.append(f"池{n['pot'] / 2:g}BB" + (f" 需跟{n['to_call'] / 2:g}BB" if n.get("to_call") else ""))
    s = " ".join(L)
    if d:
        src = "规则" if (rec.get("llm") or {}).get("rule") else f"模型{rec['t_llm']}s" if rec.get("t_llm") is not None else "重试"
        s += f" | {d.get('act')}{' ' + str(d['size']) if d.get('size') else ''}（{src}）{str(d.get('why') or '')[:40]}"
    s += f" -> {rec.get('act')}"
    if rec.get("lat") is not None: s += f" | {rec['lat']}s"
    if rec.get("note"): s += f" | {rec['note']}"
    return s


def make_session(a, model=None, live=False, log=None):
    """--prompt session 时构造 HandSession。真机：每手写本桌 tables/<桌>/hands.jsonl（对局流水）+ msgs.jsonl（模型对话，hid 关联），
    计数直接加进全局对手库 players/opponents.db，并按 --profile-every 异步更新本桌画像 tables/<桌>/<opp>.md；
    回放：流水/对话写 logs/<日期>/hands__*.jsonl、msgs__*.jsonl，不写库、不写桌目录"""
    if getattr(a, "prompt", None) != "session": return None
    log = log or (lambda m: print(m, flush=True))
    from session_prompt import HandSession, ProfileUpdater, TABLES
    tdir = ROOT / a.table if getattr(a, "table", None) else (TABLES / time.strftime("%Y%m%d_auto") if live else None)
    if live: new_table(tdir)
    opp = getattr(a, "opp", None)
    ts = time.strftime('%m%d_%H%M')
    hl = tdir / "hands.jsonl" if live else logroll.path(f"hands__{ts}.jsonl")
    ml = tdir / "msgs.jsonl" if live else logroll.path(f"msgs__{ts}.jsonl")
    warm = getattr(a, "warm", None)
    warm = live if warm is None else warm          # 真机默认热启动；回放默认不载入（历史里可能就有被回放的手）
    # 对手昵称从截图识别（Bot.handle -> sess.see_opp），识别到/换人后查库取其跨桌统计 + 画像（本桌优先），下一手生效；--opp 只是可选初值
    store = None
    if warm:                                       # 对手库：每人一行计数+最近画像（新库时从各桌 hands.jsonl 重建），识别到对手后按主键查库
        try:
            import oppdb
            from session_prompt import _hand_files, _profile_files
            store = oppdb.open_db(_hand_files(), _profile_files())
            np_, nh = store.counts()
            log(f"[oppdb] 对手 {np_} 人 / 累计 {nh} 手" + (f"（新库，从 jsonl 重建 {store.synced} 手）" if store.synced else "") + f"，打开 {store.sync_ms:.0f}ms，识别到对手后按需查库")
        except Exception as e:
            LOG.error("[oppdb] 打开失败，退回扫 jsonl: %s: %s", type(e).__name__, e, exc_info=True)
    upd = ProfileUpdater(model, tdir, opp, every=a.profile_every, store=store) if live and model and a.profile_every > 0 else None
    return HandSession(tdir, opp, hands_log=hl, msgs_log=ml, on_hand_end=upd, warm=warm,
                       store=store, store_write=live, log=log,
                       on_summary=lambda line: log("[手牌结束] " + line))


def new_table(tdir):
    """桌目录不存在时建一个：从 tables/_template/table.md 复制桌信息（之后手工补平台/级别等）"""
    tdir = Path(tdir)
    if (tdir / "table.md").exists(): return tdir
    tdir.mkdir(parents=True, exist_ok=True)
    tpl = tdir.parent / "_template" / "table.md"
    if tpl.exists():
        (tdir / "table.md").write_text(tpl.read_text().replace("<table_id>", tdir.name).replace("- 日期：", f"- 日期：{time.strftime('%Y-%m-%d')}", 1))
    return tdir


def run_live(a):
    """真机：Grabber 连续截图 -> parse -> Bot.handle"""
    logroll.day_dir()                                   # 建今天的目录并清理 90 天前的日志
    logroll.ops_start()                                 # logs/<日期>/ops.log：所有 INFO/WARN/ERROR + 未捕获异常，tail -f 看
    live = ROOT / "model_test" / "live"; live.mkdir(parents=True, exist_ok=True)
    try:                                                # 帧滚动清理：保护 golden/测试帧，未保护帧超过上限(默认30GB)删最旧的
        import frameroll
        dels, _, after, _ = frameroll.cleanup(live)
        if dels: LOG.info("[frameroll] 删除最旧帧 %d 张，未保护帧剩 %.2fGB", len(dels), after / 1024 ** 3)
    except Exception as e:
        LOG.warning("[frameroll] 清理失败(忽略): %s", e)
    unk = ROOT / "model_test" / "unknown"; unk.mkdir(parents=True, exist_ok=True)
    io = AdbIO()
    LOG.info("[io] 常驻 adb shell: %s", io.persistent)
    grab = Grabber(io, logroll.LOGS / ".ring"); grab.start()
    w = AsyncWriter()
    say = lambda m: (LOG.error if "失败" in m else LOG.info)("%s", m)   # session 的消息；“…失败”记 ERROR
    say(f"===== tlive 启动 model={a.model} prompt={a.prompt} table={getattr(a, 'table', None)} dry={a.dry} =====")
    model = make_model(a.model)
    bot = Bot(io, Decider(model, hedge_after=a.hedge), w, logroll.DailyPath(f"text__{a.model.replace('/', '__')}.jsonl"), live, unk,
              logroll.DailyPath("unknown.jsonl"), dry=a.dry, stuck=a.stuck, budget=a.budget, prompt=a.prompt,
              session=make_session(a, model, live=not a.dry, log=say), preflop_rule=getattr(a, "preflop_rule", True))
    t0, min_t = time.time(), 0.0
    while time.time() - t0 < a.max_sec:
        src, t_shot = grab.get(min_t)
        min_t = max(t_shot + 1e-3, io.busy_until + 0.25)   # 每帧只处理一次；点击后丢弃点击前开始的帧
        st = wpstate.parse(src)
        rec = bot.handle(src, st, t_shot, wall0=t_shot)
        rec_p = {k: v for k, v in rec.items() if k != "state"}
        if rec["act"] != "wait" or rec.get("note"):
            rec_p["grab_period"] = round(sum(grab.periods) / len(grab.periods), 2) if grab.periods else None
            print(json.dumps(rec_p, ensure_ascii=False), flush=True)
            (LOG.warning if str(rec["act"]).startswith(("unknown", "兜底", "noop")) else LOG.info)("%s", ops_line(rec, st), extra={"file_only": True})
    if bot.sess: bot.sess.flush()
    say("===== tlive 结束 =====")
    grab.stop = True; w.flush(); logroll.ops_flush()


def run_replay(a):
    """离线回放：历史帧代替截图（时钟=文件名时间），动作只记录；可用真实模型或 --rule 只用本地规则"""
    frames = sorted(p for p in (ROOT / a.replay).glob("*.raw") if a.t0 <= p.name[:6] <= a.t1)
    old = {}
    for lp in logroll.find_all(f"text__{a.model.replace('/', '__')}.jsonl"):   # 按日期升序，同一时刻以较新的一天为准
        for l in open(lp):
            r = json.loads(l)
            if r.get("dec") and r.get("t"): old[r["t"].replace(":", "")] = r["dec"]
    io, w = RecIO(), AsyncWriter()
    decider = None if getattr(a, "rule", False) else Decider(make_model(a.model), hedge_after=a.hedge)
    bot = Bot(io, decider, w, logroll.path(f"replay__{time.strftime('%m%d_%H%M')}.jsonl"), replay=True, stuck=1e9, budget=a.budget, prompt=a.prompt,
              session=make_session(a), preflop_rule=getattr(a, "preflop_rule", True))
    for fp in frames:
        hh = fp.name[:6]
        clock = int(hh[:2]) * 3600 + int(hh[2:4]) * 60 + int(hh[4:6])
        io.taps.clear()
        st = wpstate.parse(str(fp))
        bot.writer = None                       # 先不写，补完字段再写
        rec = bot.handle(str(fp), st, clock, wall0=time.time())
        rec.update(t=f"{hh[:2]}:{hh[2:4]}:{hh[4:]}", frame=fp.name, taps=list(io.taps))
        if hh in old: rec["old"] = old[hh]
        w.write(bot.log_path, json.dumps(rec, ensure_ascii=False) + "\n")
        if rec["act"] != "wait" or rec.get("note"):
            print(json.dumps({k: v for k, v in rec.items() if k != "state"}, ensure_ascii=False), flush=True)
    w.flush()
    if bot.sess: bot.sess.flush()
