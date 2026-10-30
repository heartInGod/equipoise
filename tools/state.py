"""WePoker 截图 -> 文字状态（不调用模型）。
用法: python3 tools/state.py [img.raw|img.png]   # 不给参数则现场 adb 截图
"""
import json, os, re, subprocess, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pix

TOOLS = Path(__file__).resolve().parent
OCR = str(TOOLS / "ocr")
ADB = "/opt/homebrew/bin/adb"
DEV = os.environ.get("ADB_DEVICE", "59a4a919")

ACTION_WORDS = ["弃牌", "让牌", "跟注", "下注", "加注", "全下", "ALL IN", "All in", "确定", "确认"]
OTHER_BTNS = ["坐下", "开始游戏", "邀请好友", "让或弃", "自动让牌", "跟任何注", "买入", "带入", "继续", "取消",
              "返回", "关闭", "知道了", "准备", "站起", "离开", "回到牌桌", "直接发牌", "发两次"]
NOISE = re.compile(r"WePoker|wpk|练习币|延迟看牌|^[\W_]*$|^\d{1,2}:\d{2}$|\d{6,}")  # 6位以上=房间号/时间行；5位筹码要保留


def capture(path="/tmp/wp.raw", wait_sec=600):
    """截图；设备掉线/返回空数据时每 2s 重试，最多等 wait_sec 秒（避免一次掉线就崩掉整个机器人）"""
    t0 = time.time()
    while True:
        with open(path, "wb") as f:
            r = subprocess.run([ADB, "-s", DEV, "exec-out", "screencap"], stdout=f, stderr=subprocess.DEVNULL)
        if r.returncode == 0 and os.path.getsize(path) > 1000:
            return path
        if time.time() - t0 > wait_sec:
            raise RuntimeError(f"adb 设备 {DEV} 掉线超过 {wait_sec}s")
        print(f"[capture] 设备 {DEV} 无响应，2s 后重试", file=sys.stderr, flush=True)
        time.sleep(2)


# ---------- 牌 ----------
def is_white(p):
    return p[0] > 225 and p[1] > 225 and p[2] > 225


def find_cards(im, y_lo, y_hi):
    """在 y_lo..y_hi 区间找白色牌面上沿，返回 [(x_left, top)]"""
    best = []
    for y in range(y_lo, y_hi, 3):
        runs, s = [], None
        for x in range(0, im.w, 2):
            w = is_white(im.px(x, y))
            if w and s is None: s = x
            if not w and s is not None:
                if 125 <= x - s <= 175: runs.append(s)
                s = None
        if len(runs) > len(best):
            best = [(x, y) for x in runs]
    if not best: return []
    # 往上找真正的上沿
    out = []
    for x, y in best:
        t = y
        while t > y_lo - 20 and is_white(im.px(x + 40, t - 1)): t -= 1
        out.append((x, t))
    return out


def ink(p):
    r, g, b = p
    if r > 80 and r > 1.8 * g and r > 1.4 * b: return "r"
    if r + g + b < 300: return "k"
    return None


def suit_of(im, x0, top):
    rows, colors, started = [], {"k": 0, "r": 0}, False
    y0 = top + 64
    while y0 < top + 90 and any(ink(im.px(x, y0)) for x in range(x0 + 3, x0 + 60, 2)): y0 += 1  # 跳过点数尾巴
    for y in range(y0, top + 135):
        segs, cur, n = 0, False, 0
        xs = []
        for x in range(x0 + 3, x0 + 60):
            c = ink(im.px(x, y))
            if c:
                colors[c] += 1; xs.append(x)
                if not cur: segs += 1
            cur = bool(c)
        if xs:
            started = True; rows.append((max(xs) - min(xs) + 1, segs))
        elif started:
            break
    if len(rows) < 15: return "?"
    red = colors["r"] > colors["k"]
    ws = [w for w, _ in rows]; n = len(ws); mw = max(ws)
    arg = ws.index(mw) / n
    if red:
        notch = any(s >= 2 for _, s in rows[: max(2, n // 6)])
        return "h" if notch or arg < 0.42 else "d"
    # 黑：梅花上瓣后有收窄
    a, b = int(n * 0.05), int(n * 0.3)
    lobe = max(ws[a:b + 1])
    neck = min(ws[b:int(n * 0.55) + 1])
    return "c" if neck < lobe - 2 else "s"


TPL = TOOLS / "rank_tpl.json"


def load_tpl():
    try:
        return json.loads(TPL.read_text())
    except Exception:
        return {}


def save_tpl(t):
    TPL.write_text(json.dumps(t))


def glyph(im, x0, y0, w=75, h=72, gw=15, gh=18):
    """点数字形 -> gw*gh 的 0/1 串（墨迹占比>0.3 记 1）"""
    out = []
    for j in range(gh):
        for i in range(gw):
            n = c = 0
            for yy in range(y0 + j * h // gh, y0 + (j + 1) * h // gh, 2):
                for xx in range(x0 + i * w // gw, x0 + (i + 1) * w // gw, 2):
                    n += 1; c += ink(im.px(xx, yy)) is not None
            out.append("1" if n and c / n > 0.3 else "0")
    return "".join(out)


def match_tpl(tpl, g):
    best, br = 10 ** 9, "?"
    for r, gs in tpl.items():
        for s in gs:
            d = sum(a != b for a, b in zip(s, g))
            if d < best: best, br = d, r
    return br if best < len(g) * 0.12 else "?"


def read_cards(src, im):
    board = find_cards(im, 1400, 1480)
    hero = find_cards(im, 2730, 2810) or find_cards(im, 2270, 2340)  # 实战 / 回放
    allc = [("b", x, t) for x, t in board] + [("h", x, t) for x, t in hero]
    if not allc: return [], [], []
    rects = [f"{x + 2},{t + 3},75,72" for _, x, t in allc]
    seqs = []
    for g in ("15", "22", "30"):   # 依次尝试不同间距，读出张数对了就停
        out = OCRC.submit("\t".join(["C", os.path.abspath(src), g, *rects]), [OCR, src, "crops", *rects], {**os.environ, "OCR_GAP": g})()
        lines = sorted((int(l.split("\t")[1]), l.split("\t", 2)[2].rstrip("\n")) for l in out.splitlines() if l.startswith("L\t"))
        seqs.append(re.findall(r"10|[2-9AJQK]", re.sub(r"[Oo]", "0", "".join(t for _, t in lines)).upper()))
        if len(seqs[-1]) == len(allc): break
    seq = next((q for q in seqs if len(q) == len(allc)), seqs[0])
    tpl = load_tpl()
    ranks = {}
    for i, (_, x, t) in enumerate(allc):
        g = glyph(im, x + 2, t + 3)
        if len(seq) == len(allc):
            ranks[i] = "T" if seq[i] == "10" else seq[i]
            tpl.setdefault(ranks[i], []).append(g)
            tpl[ranks[i]] = tpl[ranks[i]][-5:]
        else:
            ranks[i] = match_tpl(tpl, g)
    save_tpl(tpl)
    b, h = [], []
    for i, (k, x, t) in enumerate(allc):
        c = ranks.get(i, "?") + suit_of(im, x, t)
        (b if k == "b" else h).append(c)
    return b, h, [(x, t, x + 155, t + 235) for _, x, t in allc]


# ---------- 全屏 OCR ----------
class _OCRServer:
    """常驻 OCR 进程（tools/ocr serve），每个请求一行、响应以 \x01END 结尾。
    省掉每次进程启动 + Vision 初始化（整图 0.5s -> 0.18s，读牌 3 次 crops 0.43s -> ~0.1s）。异常时退回单次进程。"""
    def __init__(self): self.p = None
    def _start(self):
        self.p = subprocess.Popen([OCR, "serve"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
    def submit(self, line, fallback_argv=None, env=None):
        try:
            if self.p is None or self.p.poll() is not None: self._start()
            self.p.stdin.write(line + "\n"); self.p.stdin.flush()
            return self._read
        except Exception:
            self.p = None
            proc = subprocess.Popen(fallback_argv, stdout=subprocess.PIPE, text=True, env=env)
            return lambda: proc.communicate()[0]
    def _read(self):
        out = []
        while True:
            l = self.p.stdout.readline()
            if not l: self.p = None; break
            if l.startswith("\x01END"): break
            out.append(l)
        return "".join(out)


OCRS, OCRC = _OCRServer(), _OCRServer()   # 整图 / 读牌 crops 各一个进程，可并行


def full_ocr(src):
    p = subprocess.run([OCR, src], capture_output=True, text=True)
    try:
        return json.loads(p.stdout)
    except Exception:
        return []


def parse(src):
    t0 = time.time()
    got = OCRS.submit(os.path.abspath(src), [OCR, src])   # OCR 在常驻进程里跑，同时本进程读牌
    im = pix.load(src)
    board, hero, rects = read_cards(src, im)
    t_cards = time.time() - t0
    try: toks = json.loads(got() or "[]")
    except Exception: toks = []
    st = {"board": board, "hero": hero, "buttons": {}, "presets": [], "texts": []}
    for o in toks:
        t, x, y = re.sub(r"^[①-⑳◎◉○●⊙@©®]+\s*", "", o["t"].strip()), o["x"], o["y"]
        if y < 160 or NOISE.search(t): continue
        if any(a <= x <= c and b <= y <= d for a, b, c, d in rects):  # 牌面上的字
            continue
        hit = next((w for w in ACTION_WORDS + OTHER_BTNS if w in t), None)
        if not hit and t.replace(" ", "").lower() == "allin" and y > 2450:
            hit = "All in"   # 按钮大小写不固定：All in / All In / ALL IN
        if hit and y > 2450 or hit in OTHER_BTNS:
            st["buttons"][hit] = [int(x), int(y)]
            if hit == "弃牌": st["fold_w"] = o.get("w", 0)
            continue
        st["texts"].append([t, int(x), int(y)])
    # 下注额度预设：5 个固定槽位；标签 "BB"(翻牌前) 或 "底池"(翻牌后)
    tx = st["texts"]
    SLOTS = [(304, 2407), (511, 2305), (720, 2291), (927, 2302), (1133, 2407)]
    labels = [a for a in tx if a[0] in ("底池", "BB") and 2150 < a[2] < 2450]
    used = {id(a) for a in labels}
    if labels:
        kind = "BB" if sum(a[0] == "BB" for a in labels) >= sum(a[0] == "底池" for a in labels) else "底池"
        names = ["2BB", "3BB", "4BB", "5BB", "6BB"] if kind == "BB" else ["1/3", "1/2", "2/3", "1", "1.2"]
        for (sx, sy), nm in zip(SLOTS, names):
            amt = None
            for a in tx:
                if abs(a[1] - sx) < 60 and 2200 < a[2] < 2560 and re.fullmatch(r"[\d./]+", a[0]):
                    if 60 < a[2] - sy < 140 and re.fullmatch(r"\d+(\.\d+)?", a[0]): amt = a[0]
                    used.add(id(a))
            st["presets"].append({"frac": nm, "amt": amt, "xy": [sx, sy], "off": preset_off(im, sx, sy)})
    st["texts"] = [a for a in tx if id(a) not in used]
    st["dealer"] = find_dealer(im, toks)
    b = st["buttons"]
    st["nums"] = table_nums(st)
    if "直接发牌" not in b: b.pop("发两次", None)   # 桌面信息行"延迟看牌 发两次"是房间设置文字，不是按钮
    if "弃牌" in b and any(k in b for k in ("让牌", "跟注", "下注", "加注", "全下", "All in", "ALL IN")):
        # 行动按钮弹出动画中按钮缩小并偏向中间（实测 弃牌 w=60,x=541；稳定后 w≈107,x≈374），此时坐标不可信
        fx, fw = b["弃牌"][0], st.get("fold_w", 107)
        st["screen"] = "anim" if (fw < 90 or not 320 < fx < 430) else "my_turn"
    elif "直接发牌" in b or "发两次" in b:
        st["screen"] = "runit"        # 双方全下后的 发一次/发两次 选择弹窗（不选则倒计时~12s后默认发一次）
    elif any(t[0] in ("总手数", "入池率", "摊牌胜率") for t in st["texts"]):
        st["screen"] = "profile"      # 误点头像弹出的资料卡：点卡片外关闭
    elif "回到牌桌" in b:
        st["screen"] = "away"         # 超时被判离开：点 回到牌桌
    elif "开始游戏" in b:
        st["screen"] = "can_start"
    elif "坐下" in b:
        st["screen"] = "need_sit"
    elif "邀请好友" in b:
        st["screen"] = "waiting"
    else:
        st["screen"] = "other"
    st["opp_name"] = opp_name(st) if st["screen"] in ("my_turn", "other", "anim") else None
    st["ms"] = int((time.time() - t0) * 1000); st["ms_cards"] = int(t_cards * 1000)
    return st


# 单挑桌对手昵称：顶部中央座位 (720,416) 下方的名字行（2026-10-30 真机帧统计：little00 出现在 x≈715,y≈416；
# 对手行动时该位置被“加注/跟注/下注/让牌”等动作标签盖住；OCR 常把 0 读成 O，如 little0O）
OPP_NAME_BOX = (560, 360, 880, 470)
_NOT_NAME = re.compile(r"^[+\-−]?[\d.,]+$|密码局|底池|All ?in|ALL ?IN|等待|思考|离开|留座|补码|买入|超时")


def opp_name(st):
    """当前帧的对手昵称原文（可能有 OCR 误差），读不到返回 None。用 name_key() 归一化后再比较。"""
    x0, y0, x1, y1 = OPP_NAME_BOX
    for t, x, y in st.get("texts") or []:
        if not (x0 <= x <= x1 and y0 <= y <= y1): continue
        t = t.strip()
        if len(t) < 2 or len(t) > 20 or _NOT_NAME.search(t): continue
        if any(w in t for w in ACTION_WORDS + OTHER_BTNS): continue
        if not re.search(r"[A-Za-z\u4e00-\u9fff]", t): continue
        return t
    return None


def name_key(t):
    """昵称归一化键：NFKC、小写、去空白和下划线，易混字符 o→0、l/i/|→1（只用于比较，不用于显示）"""
    import unicodedata
    k = unicodedata.normalize("NFKC", t or "").lower()
    k = re.sub(r"[\s_]+", "", k)          # 文件名里空格会被换成 _
    return k.translate(str.maketrans({"o": "0", "l": "1", "i": "1", "|": "1"}))


def preset_off(im, sx, sy):
    """额度预设是否不可用（灰色）：金额低于最小下注/最小加注、或超过筹码时按钮变灰，点了不会下注（2026-10-30 实测：
    底池 4 时 1/3=1 是灰的，点后等于让牌）。判据：档位下方金额文字的最亮像素 —— 可用档纯白(≥240)，灰档≈120~135，
    滑条展开整屏变暗时 <20（同样不可用）。用 min(R,G,B) 排除绿色桌面。"""
    m = 0
    for y in range(sy + 70, min(im.h, sy + 135), 2):
        for x in range(max(0, sx - 45), min(im.w, sx + 46), 2):
            v = min(im.px(x, y))
            if v > m:
                m = v
                if m >= 200: return False
    return True


def usable_presets(st):
    """可点的额度预设（排除灰档）"""
    return [p for p in st.get("presets") or [] if not p.get("off")]


def _num(t):
    d = re.sub(r"[^\d]", "", t)
    return int(d) if d and len(d) <= 7 else None


def table_nums(st):
    """从固定区域读筹码/下注/底池（1440x3200 实测布局，见 act.py 文档）并消费掉这些文字。
    双方本街下注优先用 底池总额-上街底池 与 需跟注额 联立反推（筹码图标常被 OCR 成数字前缀，如 ②1→91）。"""
    tx, b = st["texts"], st["buttons"]
    def grab(x0, x1, y0, y1):
        for a in tx:
            if x0 <= a[1] <= x1 and y0 <= a[2] <= y1 and _num(a[0]) is not None:
                tx.remove(a); return _num(a[0])
        return None
    n = {"opp_stack": grab(600, 840, 670, 730), "opp_bet_ocr": grab(600, 860, 740, 800),
         "pot": grab(600, 840, 870, 930), "pot_prev": grab(600, 900, 975, 1035),
         "my_bet_ocr": grab(780, 1060, 2520, 2600), "my_stack": grab(600, 840, 3105, 3165)}
    call_btn = b.get("跟注")
    n["to_call"] = grab(call_btn[0] - 90, call_btn[0] + 90, call_btn[1] - 90, call_btn[1] - 15) if call_btn else None
    if n["to_call"] is None and ("All in" in b or "ALL IN" in b) and "跟注" not in b and "让牌" not in b:
        n["to_call"] = "allin"  # 对手下注≥我方剩余筹码：只能 弃牌 或 All in 跟
    pot, prev = n["pot"], n["pot_prev"] or 0
    opp = my = None
    if pot is not None and isinstance(n["to_call"], int):
        sb = pot - prev
        if sb >= n["to_call"] and (sb + n["to_call"]) % 2 == 0:
            opp, my = (sb + n["to_call"]) // 2, (sb - n["to_call"]) // 2
    if opp is None and ("让牌" in b) and "弃牌" in b:
        opp = my = (pot - prev) // 2 if pot is not None else 0   # 能让牌=双方本街下注相等
    if opp is None:
        opp = n["opp_bet_ocr"] or 0
        my = (pot - prev - opp) if pot is not None and pot - prev - opp >= 0 else (n["my_bet_ocr"] or 0)
    if isinstance(n["to_call"], int) and n["to_call"] > 0 and (opp or 0) - (my or 0) != n["to_call"]:
        my = my or 0; opp = my + n["to_call"]          # 底池数字动画未刷新时，以跟注按钮上的金额为准
        if pot is not None and pot < prev + opp + my: n["pot"] = prev + opp + my
    n["opp_bet"], n["my_bet"] = opp, my
    if n["to_call"] == "allin":
        n["to_call"] = n["my_stack"]
    elif n["to_call"] is None:
        n["to_call"] = 0 if "让牌" in b else (max(opp - my, 0) if opp is not None else None)
    return n


def hand_desc(hero):
    if len(hero) != 2 or any("?" in c for c in hero): return "未识别完整"
    r1, r2 = hero[0][:-1], hero[1][:-1]
    if r1 == r2: return "对子"
    return "同花" if hero[0][-1] == hero[1][-1] else "不同花"


RANKS = "23456789TJQKA"
RNAME = {"T": "10", "J": "J", "Q": "Q", "K": "K", "A": "A"}


def _rv(c):
    r = c[:-1].replace("10", "T")
    return RANKS.index(r) + 2 if r in RANKS else None


def _straight_hi(vals):
    v = set(vals) | ({1} if 14 in vals else set())
    for hi in range(14, 4, -1):
        if all(x in v for x in range(hi - 4, hi + 1)): return hi
    return None


def made_desc(hero, board):
    """代码算牌力：成牌类别+细节、听牌 outs。返回若干行文字；手牌/公共牌未识别完整时返回 []"""
    cs = list(hero) + list(board)
    if len(hero) != 2 or not board or any("?" in c or _rv(c) is None for c in cs): return []
    hv, bv = [_rv(c) for c in hero], [_rv(c) for c in board]
    allv = hv + bv
    nm = lambda v: RNAME.get(RANKS[v - 2], RANKS[v - 2])
    suits = [c[-1] for c in cs]
    cnt = {v: allv.count(v) for v in set(allv)}
    bcnt = {v: bv.count(v) for v in set(bv)}
    L = []
    # 同花/顺子
    fl = next((s for s in "shdc" if suits.count(s) >= 5), None)
    st_hi, st_b = _straight_hi(allv), _straight_hi(bv)
    quads = [v for v, c in cnt.items() if c == 4]
    trips = sorted([v for v, c in cnt.items() if c == 3], reverse=True)
    pairs = sorted([v for v, c in cnt.items() if c == 2], reverse=True)
    bmax = max(bv)
    if quads: made = f"四条{nm(quads[0])}" + ("" if quads[0] in hv else f"（公共牌自身四条，比踢脚，我方最大 {nm(max(hv))}）")
    elif trips and (len(trips) > 1 or pairs): made = "葫芦"
    elif fl:
        mine = [hv[i] for i in range(2) if hero[i][-1] == fl]
        made = "同花" + (f"(我方用到 {'、'.join(nm(v) for v in sorted(mine, reverse=True))} 的同花牌)" if mine else "(公共牌自身成同花，我方没用到手牌)")
    elif st_hi:
        made = f"顺子(到{nm(st_hi)})" + ("(公共牌自身成顺，我方没用到手牌)" if st_b == st_hi else "")
    elif trips:
        t = trips[0]
        made = (f"暗三条{nm(t)}" if hv[0] == hv[1] == t else (f"三条{nm(t)}" if t in hv else f"公共牌三条{nm(t)}，我方仅高牌"))
    elif len(pairs) >= 2:
        mine = [p for p in pairs if p in hv]
        made = f"两对({nm(pairs[0])}和{nm(pairs[1])})" + ("" if mine else "，全在公共牌上，我方仅高牌") + \
               (f"，其中用到我方的 {'、'.join(nm(p) for p in mine)}" if mine and len(mine) < len(pairs[:2]) else "")
    elif pairs:
        p = pairs[0]
        if hv[0] == hv[1] == p:
            higher = len({v for v in bv if v > p})
            made = f"口袋对{nm(p)}" + ("（超对，大于所有公共牌）" if higher == 0 else f"（有 {higher} 种公共牌比它大）")
        elif p in hv:
            order = sorted(set(bv), reverse=True)
            pos = ["顶对", "第二对", "第三对"][order.index(p)] if order.index(p) < 3 else "小对"
            kick = [v for v in hv if v != p][0]
            made = f"一对{nm(p)}（{pos}，踢脚 {nm(kick)}）"
        else:
            made = f"公共牌对子{nm(p)}，我方仅高牌（最大 {nm(max(hv))}）"
    else:
        made = f"高牌（最大 {nm(max(hv))}，没有成对）"
    L.append(f"我方牌力(代码计算，可信): {made}")
    if len(board) < 5 and not fl and not (st_hi and st_b != st_hi):
        draws, fsuit = [], None
        for s in "shdc":
            if suits.count(s) == 4 and any(c[-1] == s for c in hero):
                fsuit = s
                mine = max(hv[i] for i in range(2) if hero[i][-1] == s)
                higher = [v for v in range(mine + 1, 15) if not any(_rv(c) == v and c[-1] == s for c in board)]
                draws.append(f"同花听牌 9 outs（我方最大同花牌 {nm(mine)}" + ("，坚果同花听）" if not higher else f"，非坚果，上面还有 {len(higher)} 张更大的同花牌）"))
        comp = [r for r in range(2, 15) if r not in allv and _straight_hi(allv + [r]) and _straight_hi(allv + [r]) != _straight_hi(bv + [r])]
        if len(comp) >= 2: draws.append(f"两头/双卡顺子听牌 {4 * len(comp)} outs")
        elif len(comp) == 1: draws.append("卡顺听牌 4 outs")
        if fsuit and comp:      # 同花色的顺子补牌在两种听牌里都算了一次，去重
            draws.append(f"合计 {9 + 4 * len(comp) - len(comp)} outs（去重，{len(comp)} 张同花色顺子补牌只算一次）")
        L.append("听牌: " + ("；".join(draws) if draws else "无（后门不算）"))
    return L


def draw_outs(line):
    """从 made_desc 的听牌行取 outs 总数：有“合计”用合计（已去重），否则把各项相加"""
    m = re.search(r"合计 (\d+) outs", line or "")
    return int(m.group(1)) if m else sum(int(x) for x in re.findall(r"(\d+) outs", line or ""))


def draw_eq(outs, n_board):
    """听牌成牌概率(%)：(下一张, 到河牌)。翻牌剩 47 张未知牌，转牌剩 46 张；到河牌按精确组合算（outs×4 在 outs>8 时高估）"""
    if n_board == 3:
        return outs / 47 * 100, (1 - (47 - outs) * (46 - outs) / (47 * 46)) * 100
    if n_board == 4:
        return outs / 46 * 100, outs / 46 * 100
    return 0.0, 0.0


def draw_line(line, n_board, head="听牌胜率"):
    """made_desc 听牌行 → 给 prompt 的胜率文字（0 或 1 行）。outs 已去重，概率按剩余牌精确计算"""
    if not (line or "").startswith("听牌") or n_board not in (3, 4):
        return []
    outs = draw_outs(line)
    if not outs:
        return []
    nxt, river = draw_eq(outs, n_board)
    s = f"{head} {outs} outs 下一张成牌约 {nxt:.0f}%"
    s += f"，到河牌约 {river:.0f}%（仅跟注即全下时可用）" if n_board == 3 else "（只剩一张）"
    if "非坚果" in line:
        s += "；同花非坚果：对手持更大同花牌时同花 outs 会输，胜率略低于上述值"
    return [s]
    return 0.0, 0.0


def facts(st):
    """把可由代码确定的事实算好，避免模型自己猜（位置/同花/跟注额/底池赔率/可选动作）"""
    n, b, L = st.get("nums", {}), st["buttons"], []
    d = st.get("dealer")
    if d:
        me_btn = d[1] > 1600
        L.append("我方位置: " + ("按钮位=小盲（翻前先行动，翻后最后行动/有位置）" if me_btn else "大盲位（翻前后行动，翻后先行动/无位置）"))
    L.append(f"手牌类型: {hand_desc(st['hero'])}" + ("" if not st["hero"] else f"（{'、'.join(c[:-1] + {'s': '黑桃', 'h': '红桃', 'd': '方块', 'c': '梅花'}.get(c[-1], '?') for c in st['hero'])}）"))
    L += made_desc(st["hero"], st["board"])
    if n.get("pot") is not None:
        L.append(f"底池(含本街下注): {n['pot']}；本街开始前底池: {n.get('pot_prev') or 0}；对手本街已下: {n.get('opp_bet')}；我方本街已下: {n.get('my_bet')}")
    L.append(f"筹码: 我方剩余 {n.get('my_stack')}，对手剩余 {n.get('opp_stack')}" +
             (f"，有效筹码 {min(n['my_stack'] + (n.get('my_bet') or 0), n['opp_stack'] + (n.get('opp_bet') or 0))}" if n.get("my_stack") and n.get("opp_stack") else ""))
    tc = n.get("to_call")
    if tc:
        pot, tc = cap_call(n); pot = pot or 0
        L.append(f"需跟注: {tc}（跟 {tc} 去赢 {pot}，底池赔率要求胜率 ≥ {tc / (pot + tc) * 100:.1f}%）" +
                 ("；跟注即全下" if n.get("my_stack") is not None and tc >= n["my_stack"] else ""))
    elif tc == 0:
        L.append("需跟注: 0（可以免费让牌）")
    opts = []
    for k in b:
        if k == "跟注" and tc: opts.append(f"跟注{tc}")
        elif k in ("All in", "ALL IN", "全下"): opts.append(f"全下(共{(n.get('my_stack') or 0) + (n.get('my_bet') or 0)})")
        elif k in ("弃牌", "让牌", "跟注", "下注", "加注"): opts.append(k)
    if opts: L.append("我方可选动作(这些是我方按钮，不代表对手动作): " + " / ".join(opts))
    return L


_TR = bytes((ord("g") if v < 70 else (ord("w") if v > 195 else ord("m"))) for v in range(256))
_DPAT = re.compile(rb"gm{0,3}(w{4,18}m{0,3}g{3,14}m{0,3}w{5,24}m{0,3}g{3,14}m{0,3}w{3,18})m{0,3}g")


def find_dealer(im, toks=None):
    """红色通道逐行正则匹配 D 圆盘，返回圆心坐标"""
    hits = []
    stride = im.w * im.bpp
    for y in range(250, min(im.h, 3150), 3):
        row = bytes(im.d[y * stride:(y + 1) * stride])[0::im.bpp].translate(_TR)
        red = None
        for m in _DPAT.finditer(row):
            if 34 <= m.end(1) - m.start(1) <= 60:
                if red is None: red = bytes(im.d[y * stride:(y + 1) * stride])[0::im.bpp]
                dark = [v for v in red[m.start(1):m.end(1)] if v < 70]
                if dark and 20 <= sum(dark) / len(dark) <= 90:  # D 字母是深灰(≈41)，桌面/横幅底色≈0
                    hits.append(((m.start(1) + m.end(1)) // 2, y))
    best = None
    for x, y in hits:
        grp = [(a, b) for a, b in hits if abs(a - x) < 12 and abs(b - y) < 30]
        if len(grp) < 3: continue
        cx = sum(a for a, _ in grp) // len(grp)
        top, bot = min(b for _, b in grp), max(b for _, b in grp)
        cy = (top + bot) // 2
        if im.px(cx, max(0, cy - 34))[0] > 120 or im.px(cx, min(im.h - 1, cy + 34))[0] > 120:
            continue  # 上下不是桌面背景 -> 牌面字形等误报
        if best is None or len(grp) > best[2]: best = (cx, cy, len(grp))
    return None if best is None else [best[0], best[1]]


STREETS = {0: "翻前", 3: "翻牌", 4: "转牌", 5: "河牌"}
LEVEL = {1: "盲注", 2: "开局加注(2bet)", 3: "3bet", 4: "4bet", 5: "5bet"}


class HandHist:
    """跨帧维护本手行动历史。每到我方回合 observe() 一次（推断对手自上次以来的动作），我方执行后 mine() 记录我方动作。
    金额均为“本街累计下注到 X”。新手牌（手牌变化）时重置。"""
    def __init__(self): self.reset(None)

    def reset(self, hero):
        self.hero, self.pos, self.street, self.ev, self.level = hero, None, None, [], 1
        self.my_tot = self.opp_tot = 0; self.last_me = None; self._snap = None
        self.pot0 = None                     # 当前街开始时的底池（换街时用底池增量校正我方下注额）

    _ORDER = ("翻前", "翻牌", "转牌", "河牌")

    def _fix_unknown_bet(self, new_street, pot_prev):
        """上一街我方下注额记成 '?'（预设金额没读到）且对手跟注结束了这一街：用底池增量反推。
        底池没涨 -> 那次点击没有下注（例如点了灰档），改记为让牌。只在相邻两街之间校正。"""
        if self.pot0 is None or pot_prev is None or self.last_me not in ("加注", "下注"): return
        o = self._ORDER
        if self.street not in o or new_street not in o or o.index(new_street) - o.index(self.street) != 1: return
        i = next((i for i in range(len(self.ev) - 1, -1, -1) if self.ev[i][1].startswith("我方")), None)
        if i is None or self.ev[i][0] != self.street or "到 ?" not in self.ev[i][1]: return
        delta = pot_prev - self.pot0
        if delta <= 0:
            self.ev[i] = (self.street, "我方让牌"); self.last_me = "让牌"
        elif delta % 2 == 0:
            self.ev[i] = (self.street, self.ev[i][1].replace("到 ?", f"到 {delta // 2}", 1)); self.my_tot = delta // 2

    def observe(self, st):
        hero = tuple(st["hero"])
        street = STREETS.get(len(st["board"]), "?")
        full = len(hero) == 2 and "?" not in "".join(hero)
        if full and (hero != self.hero or (street == "翻前" and self.street not in (None, "翻前"))):
            self.reset(hero)
        d = st.get("dealer")
        if d and self.pos is None: self.pos = "BTN" if d[1] > 1600 else "BB"
        n = st.get("nums", {})
        opp, my = n.get("opp_bet") or 0, n.get("my_bet") or 0
        if self.pos is None and street == "翻前" and opp >= 2:   # 庄家标识缺失时按盲注推断
            self.pos = "BTN" if my == 1 and opp == 2 else "BB" if my == 2 else None
        if street != self.street:
            if self.street is not None:   # 上一街如何结束
                self._fix_unknown_bet(street, n.get("pot_prev"))
                if self.last_me in ("加注", "下注", "全下"): self.ev.append((self.street, "对手跟注"))
                elif self.last_me == "让牌" and self.pos == "BB": self.ev.append((self.street, "对手让牌"))
            self.street, self.level, self.last_me = street, (1 if street == "翻前" else 0), None
            self.pot0 = 0 if street == "翻前" else n.get("pot_prev")
            if street == "翻前":
                if self.pos == "BB" and opp > 2: self.ev.append((street, f"对手开局加注到 {opp}")); self.level = 2
                elif self.pos == "BB" and opp == 2: self.ev.append((street, "对手平跟(limp)"))
            elif opp > 0:
                self.ev.append((street, f"对手下注 {opp}")); self.level = 1
            elif self.pos == "BTN":
                self.ev.append((street, "对手让牌"))
        elif self.last_me:
            lm = self.last_me
            prev_my = self._snap[2] if self._snap else 0          # 我方动作前本街已下
            # 点击没生效：我方本街下注没变且对手也没新动作（预设金额与实际 OCR 可能差 1~2，不能拿预设额比较）
            failed = lm == "弃牌" or (lm in ("跟注", "加注", "下注", "全下") and my <= prev_my and opp <= self.opp_tot) or (lm == "让牌" and opp <= self.opp_tot)
            if failed and self._snap:   # 仍是我方回合且状态没变 -> 上次点击没生效，撤销记录
                k, self.level, self.my_tot, self.last_me = self._snap
                del self.ev[k:]; self._snap = None
                self.my_tot, self.opp_tot = my, opp
                return
            if lm in ("加注", "下注") and my > 0 and my != self.my_tot:   # 用实际下注额修正预设额
                for i in range(len(self.ev) - 1, -1, -1):
                    if self.ev[i][1].startswith("我方") and "到 " in self.ev[i][1]:
                        self.ev[i] = (self.ev[i][0], re.sub(r"到 (\d+|\?)", f"到 {my}", self.ev[i][1], count=1)); break
                self.my_tot = my
            if opp > self.my_tot:
                was_bet = self.level >= (2 if street == "翻前" else 1)
                self.level += 1
                self.ev.append((street, f"对手{'加注' if was_bet else '下注'}到 {opp}" + (f"（{LEVEL.get(self.level, f'{self.level}bet')}）" if street == '翻前' else "")))
        self.my_tot, self.opp_tot = my, opp

    def mine(self, dec, act_str, st):
        self._snap = (len(self.ev), self.level, self.my_tot, self.last_me)
        a = dec.get("act")
        n = st.get("nums", {})
        if a == "跟注" and "All in" in st["buttons"] and n.get("to_call") and n.get("my_stack") and n["to_call"] >= n["my_stack"]: a = "全下"
        m = re.search(r"preset (\S+?)=(\d+)", str(act_str))
        if a in ("加注", "下注"):
            a = "加注" if self.street == "翻前" or self.level >= 1 else "下注"
            tot = int(m.group(2)) if m else None
            self.level += 1
            self.ev.append((self.street, f"我方{a}到 {tot if tot else '?'}" + (f"（{LEVEL.get(self.level, f'{self.level}bet')}）" if self.street == '翻前' else "")))
            if tot: self.my_tot = tot
        elif a == "跟注": self.ev.append((self.street, f"我方跟注到 {n.get('opp_bet')}")); self.my_tot = n.get("opp_bet") or self.my_tot
        elif a == "全下": self.ev.append((self.street, f"我方全下(共 {(n.get('my_stack') or 0) + (n.get('my_bet') or 0)})")); self.level += 1
        elif a in ("让牌", "弃牌"): self.ev.append((self.street, f"我方{a}"))
        self.last_me = a

    def text(self):
        L = []
        if self.pos: L.append("本手我方位置: " + ("按钮位/小盲" if self.pos == "BTN" else "大盲"))
        cur = None
        for s, e in self.ev:
            if s != cur: L.append(f"  {s}: {e}"); cur = s
            else: L[-1] += f" → {e}"
        if self.street:
            lv = self.level
            desc = {0: "本街尚无人下注", 1: "本街面对首次下注" if self.street != "翻前" else "只有盲注、无人加注"}.get(lv)
            if desc is None: desc = (LEVEL.get(lv, f"{lv}bet") + " 局面") if self.street == "翻前" else f"本街已发生 {lv - 1} 次加注"
            L.append(f"当前({self.street}): {desc}")
        return ["本手行动历史(代码记录，可信):"] + (L or ["  (无)"])


_PFR = None


def preflop_rank(hero):
    """('Q7o', {'eq': 胜率%, 'top': 前X%}) ；表由 tools/make_preflop_rank.py 生成"""
    global _PFR
    if len(hero) != 2 or "?" in "".join(hero): return None
    if _PFR is None:
        p = Path(__file__).with_name("preflop_rank.json")
        _PFR = json.loads(p.read_text()) if p.exists() else {}
    o = "23456789TJQKA"
    (r1, s1), (r2, s2) = [(c[:-1].replace("10", "T"), c[-1]) for c in hero]
    if r1 not in o or r2 not in o: return None
    if o.index(r1) < o.index(r2): r1, r2 = r2, r1
    k = r1 + r2 if r1 == r2 else r1 + r2 + ("s" if s1 == s2 else "o")
    return (k, _PFR[k]) if k in _PFR else None


def vs_big_eq(eq):
    """翻前面对全下/大额加注时对其（较窄）范围的估计胜率：弱牌打 7 折，强牌最多扣 12 个点"""
    return max(eq * 0.7, eq - 12)


def cap_call(n):
    """(有效底池, 有效跟注额)：对手下注超出我方全部筹码的部分会退回，不进入赔率"""
    pot, tc, ms, mb, ob = n.get("pot"), n.get("to_call"), n.get("my_stack"), n.get("my_bet") or 0, n.get("opp_bet") or 0
    if pot is None or not tc or ms is None: return pot, tc
    extra = max(ob - (mb + ms), tc - ms, 0)       # 对手下注里超出我方全部筹码、会退回的部分
    return pot - extra, min(tc, ms)


def brief_text(st, hist=None, hist_line=True):
    """精简版牌桌状态（给模型决策用，约 300 字）：只留决策需要的事实，一行一类，不含原始 OCR 文字"""
    n, b = st.get("nums") or {}, st["buttons"]
    hero, board = st["hero"], st["board"]
    street = STREETS.get(len(board), "?")
    d = st.get("dealer")
    pos = None if not d else ("按钮位(翻前先动，翻后有位置)" if d[1] > 1600 else "大盲(翻前后动，翻后无位置)")
    if pos is None and street == "翻前":   # 庄家标识没识别到时按盲注推断（单挑小盲=按钮位）
        mb, ob = n.get("my_bet"), n.get("opp_bet")
        if mb == 1 and ob == 2: pos = "按钮位(翻前先动，翻后有位置)"
        elif mb == 2 and (ob or 0) >= 2: pos = "大盲(翻前后动，翻后无位置)"
    if pos is None and hist is not None and hist.pos:
        pos = "按钮位(翻前先动，翻后有位置)" if hist.pos == "BTN" else "大盲(翻前后动，翻后无位置)"
    hd = hand_desc(hero)
    L = [f"手牌 {' '.join(hero) or '?'}（{hd}）；{street}" + (f" {' '.join(board)}" if board else "") + (f"；我方{pos}" if pos else "")]
    pr = preflop_rank(hero) if not board else None
    if pr:
        L.append(f"起手牌 {pr[0]} 前{pr[1]['top']:.0f}%（单挑对随机牌胜率 {pr[1]['eq']:.0f}%）")
    md = made_desc(hero, board)
    if md:
        L.append("牌力 " + md[0].split(": ", 1)[1] + ("；" + md[1].replace(": ", " ") if len(md) > 1 else ""))
    pot, tc = n.get("pot"), n.get("to_call")
    if pot is not None:
        L.append(f"底池 {pot}（本街前 {n.get('pot_prev') or 0}，本街对手 {n.get('opp_bet')} / 我方 {n.get('my_bet')}）")
    ms, os_ = n.get("my_stack"), n.get("opp_stack")
    if ms is not None or os_ is not None:
        L.append(f"筹码 我方 {ms} / 对手 {os_}" + (f"，有效 {min(ms + (n.get('my_bet') or 0), os_ + (n.get('opp_bet') or 0))}" if ms and os_ else ""))
    if tc:
        pot, tc = cap_call(n)
        L.append(f"需跟注 {tc}，所需胜率 {tc / ((pot or 0) + tc) * 100:.0f}%" + ("（跟注即全下）" if ms is not None and tc >= ms else ""))
        if pr and tc > 10:        # 翻前面对全下/大额加注：对手范围变窄，用 vs_big_eq 估计胜率
            e, need = vs_big_eq(pr[1]["eq"]), tc / ((pot or 0) + tc) * 100
            L.append(f"翻前面对大注 对其范围胜率约 {e:.0f}% {'≥' if e >= need else '<'} 所需 {need:.0f}%（{'够跟' if e >= need else '不够'}）")
    if md and len(board) in (3, 4):
        L.extend(draw_line(md[-1], len(board), "听牌胜率(代码算)"))
    if hist_line and hist is not None and hist.ev:
        segs, cur = [], None
        for s, e in hist.ev:
            if s != cur: segs.append(f"{s}: {e}"); cur = s
            else: segs[-1] += f" → {e}"
        L.append("历史 " + "；".join(segs))
    if hist is not None and hist.street == "翻前" and hist.level >= 2:
        L.append(f"当前 翻前{LEVEL.get(hist.level, f'{hist.level}bet')}局面")
    opts = []
    for k in b:
        if k == "跟注": opts.append(f"跟注{tc}" if tc else "跟注")
        elif k in ("All in", "ALL IN", "全下"): opts.append(f"全下{(ms or 0) + (n.get('my_bet') or 0)}")
        elif k in ("弃牌", "让牌"): opts.append(k)
        elif k in ("下注", "加注"):
            ps = "、".join(f"{p['frac']}={p['amt']}" for p in usable_presets(st))
            opts.append(f"{k}[{('加注到 ' if tc else '') + ps}]" if ps else k)
    L.append("可选 " + " / ".join(opts))
    return "\n".join(L)


def to_text(st, hist=None, raw_texts=True):
    L = [f"界面判定: {st['screen']}",
         f"我的手牌: {' '.join(st['hero']) or '无'}",
         f"公共牌: {' '.join(st['board']) or '无'}"]
    if st["screen"] == "my_turn": L += facts(st)
    if hist is not None and st["screen"] == "my_turn": L += hist.text()
    elif st["buttons"]: L.append("可点按钮: " + "，".join(f"{k}" for k in st["buttons"]))
    if st["presets"]: L.append("加注/下注额度预设(点一下即按该额度下注；灰档不可用): " + "，".join(
        (f"{p['frac']}={p['amt']}" if p['frac'].endswith("BB") else f"{p['frac']}池={p['amt']}") + ("(灰,不可用)" if p.get("off") else "")
        for p in st["presets"]))
    tx = st["texts"]
    if not raw_texts and st["screen"] == "my_turn":
        # 精简：数字/位置/按钮已由代码算好，只留桌面区域里的少量非数字文字（如对手头像上的 Allin、昵称、提示）
        tx = [t for t in tx if 250 < t[2] < 2450 and not re.fullmatch(r"[\d.,:/]+|D|十|\+|总底池|底池|下注", t[0])][:12]
        if tx: L.append("其余桌面文字(文字@x,y；y<1600 属对手区域): " + "；".join(f"{t}@{x},{y}" for t, x, y in tx))
        return "\n".join(L)
    L.append("其余屏幕文字(文字@x,y，屏幕1440x3200，我方在底部y≈3100，对手在上方):")
    L.append("  " + "；".join(f"{t}@{x},{y}" for t, x, y in tx))
    return "\n".join(L)


if __name__ == "__main__":
    t = time.time()
    src = sys.argv[1] if len(sys.argv) > 1 else capture()
    tc = time.time() - t
    st = parse(src)
    print(to_text(st))
    print(f"\n[capture {tc:.2f}s, parse {st['ms']/1000:.2f}s]")
