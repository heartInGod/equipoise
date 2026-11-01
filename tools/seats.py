"""多人桌座位识别：座位布局（各座位头像中心，顺时针）+ 单帧逐座位解析 + D 位归属 + 开局模型校验。

坐标都是原图 1440x3200。布局来自 tools/layout_8max.json（`python3 tools/seats.py derive <帧目录>` 从录像推出；
开局由多模态模型校验一次，一致则在文件里记 verified）。每个座位以头像中心 (cx,cy) 为锚：
  昵称/动作标签 在 (cx, cy+name_dy)；“弃牌”状态 / 空座“+” 在头像中心；剩余筹码在 (cx, cy+stack_dy)；
  我方座位（hero 槽，底部中间）的剩余筹码在手牌下方，按 x≈cx、y∈(cy+300, cy+1100) 找最近的数字；
  本街下注额在座位旁（带筹码图标），归到最近的已入座座位；中间是总底池（含本街下注）和之前各街已收进的底池。
筹码图标常被 OCR 成 ②/◎（去掉即可）或一个前缀数字（"22" 实为 2、"277" 实为 77）：无图标的多位数给两个候选，
用 总底池 − 已收底池 = Σ各座位本街下注 选出唯一组合；选不出时留给 HandHist 按行动合理性挑。

用法:
  python3 tools/seats.py show <img.raw>            # 解析一帧，打印各座位
  python3 tools/seats.py derive <帧目录> [--save]   # 从录像各帧推座位布局（--save 写 tools/layout_8max.json）
  python3 tools/seats.py dump <帧目录> <out.json>   # 帧目录 -> OCR/读牌 fixture（回放测试用）
  python3 tools/seats.py check <img.raw> [--model REF]   # 发截图让模型校验座位分组/D 位（会调模型）
"""
import itertools, json, math, re, subprocess, sys, threading, time
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
LAYOUT_FILE = TOOLS / "layout_8max.json"
ICON = re.compile(r"^[①-⑳◎◉○●⊙@©®$]+\s*")
LABELS = ("弃牌", "让牌", "跟注", "下注", "加注", "全下", "ALL IN", "All in", "All In", "Allin")
EMPTY = ("+", "十", "＋")
CJK = re.compile(r"[A-Za-z\u4e00-\u9fff]")
NAME_DY, STACK_DY = -142, 145
# h49 回放（WePoker 8 人桌，2026-10）实测：我方 0 号槽，其余顺时针；与 layout_8max.json 相同，文件缺失时兜底
DEFAULT = {"slots": [[720, 2156], [139, 1902], [139, 1363], [139, 845], [718, 556], [1301, 849], [1301, 1363], [1301, 1902]],
           "hero": 0, "name_dy": NAME_DY, "stack_dy": STACK_DY, "pot": [720, 900], "pot_prev": [741, 1004], "src": "default"}
_LAYOUT = None


def load_layout(path=LAYOUT_FILE):
    global _LAYOUT
    if _LAYOUT is None:
        try: _LAYOUT = json.loads(Path(path).read_text())
        except Exception: _LAYOUT = dict(DEFAULT)
    return _LAYOUT


def save_layout(lay, path=LAYOUT_FILE):
    global _LAYOUT
    Path(path).write_text(json.dumps(lay, ensure_ascii=False, indent=1) + "\n")
    _LAYOUT = lay


def _num(t):
    return int(t) if re.fullmatch(r"\d{1,7}", t) else None


def bet_cands(o):
    """下注额 token -> 候选值（去筹码图标；无图标的多位数多给一个“首位其实是图标”的候选，低置信度时排前面）"""
    t = o["t"].strip().replace(",", "")
    s = ICON.sub("", t)
    v = _num(s)
    if v is None: return []
    if s != t or len(s) < 2: return [v]
    alt = int(s[1:])
    return [alt, v] if o.get("c", 1) < 0.5 else [v, alt]


def _near(o, x, y, dx, dy):
    return abs(o["x"] - x) <= dx and abs(o["y"] - y) <= dy


def _is_name(t, o):
    return len(t) >= 2 and len(t) <= 20 and CJK.search(t) and o.get("h", 40) >= 30 and not any(w in t for w in LABELS)


def read_table(toks, lay=None, dealer=None, rects=()):
    """单帧 -> {"slots":[{i,cx,cy,hero,empty,occ,name,label,folded,stack,bet,bet_c}], "n", "occ", "dealer_slot", "pot", "pot_prev", "anchors", "fit"}"""
    lay = lay or load_layout()
    ndy, sdy = lay.get("name_dy", NAME_DY), lay.get("stack_dy", STACK_DY)
    toks = [o for o in toks if o["y"] >= 160 and not any(a <= o["x"] <= c and b <= o["y"] <= d for a, b, c, d in rects)]
    used = set()
    slots = []
    for i, (cx, cy) in enumerate(lay["slots"]):
        s = {"i": i, "cx": cx, "cy": cy, "hero": i == lay.get("hero", 0), "empty": False, "name": None, "label": None,
             "folded": False, "stack": None, "bet": None, "bet_c": []}
        for k, o in enumerate(toks):
            t = o["t"].strip()
            if _near(o, cx, cy + ndy, 150, 45):
                if any(w == t or w in t for w in LABELS): s["label"] = next(w for w in LABELS if w in t); used.add(k)
                elif t in EMPTY: s["empty"] = True; used.add(k)
                elif _is_name(t, o): s["name"] = t; used.add(k)
            elif _near(o, cx, cy, 110, 60):
                if "弃牌" in t: s["folded"] = True; used.add(k)
                elif t in EMPTY: s["empty"] = True; used.add(k)
            elif not s["hero"] and _near(o, cx, cy + sdy, 110, 45) and _num(t) is not None:
                s["stack"] = _num(t); used.add(k)
        if s["hero"]:
            st = [(o["y"], k, _num(o["t"].strip())) for k, o in enumerate(toks)
                  if abs(o["x"] - cx) <= 100 and cy + 300 < o["y"] < cy + 1100 and _num(o["t"].strip()) is not None]
            if st:
                _, k, v = min(st); s["stack"] = v; used.add(k)
        s["occ"] = not s["empty"] and any(s[k] for k in ("name", "label", "folded", "stack"))
        slots.append(s)
    pot = pot_prev = None
    for k, o in enumerate(toks):
        if k in used: continue
        if _near(o, *lay["pot"], 110, 40) and _num(ICON.sub("", o["t"].strip())) is not None:
            pot = _num(ICON.sub("", o["t"].strip())); used.add(k)
        elif _near(o, *lay["pot_prev"], 110, 40) and _num(ICON.sub("", o["t"].strip())) is not None:
            pot_prev = _num(ICON.sub("", o["t"].strip())); used.add(k)
    occ = [s for s in slots if s["occ"]]
    for k, o in enumerate(toks):                    # 下注额：剩下的数字归最近的已入座座位
        if k in used or o["y"] > 2450 or o.get("h", 40) < 30: continue
        c = bet_cands(o)
        if not c or not occ: continue
        d, _, s = min((math.hypot(o["x"] - s["cx"], o["y"] - s["cy"]), s["i"], s) for s in occ)
        if d < 360 and (not s["bet_c"] or d < s["_bd"]):
            s["bet_c"], s["_bd"] = c, d
    for s in slots:
        s.pop("_bd", None)
        s["bet"] = s["bet_c"][0] if s["bet_c"] else None
    resolve_bets(slots, pot, pot_prev)
    ds = None
    if dealer and occ:
        dd = sorted((math.hypot(dealer[0] - s["cx"], dealer[1] - s["cy"]), s["i"]) for s in occ)
        if dd[0][0] < 300 and (len(dd) == 1 or dd[0][0] < 0.7 * dd[1][0]): ds = dd[0][1]
    return {"slots": slots, "n": len(occ), "occ": [s["i"] for s in occ], "dealer_slot": ds, "pot": pot, "pot_prev": pot_prev,
            "src": lay.get("src")}


def resolve_bets(slots, pot, pot_prev):
    """有歧义的下注额：用 总底池 − 已收底池 = Σ本街下注 选唯一组合（选不出就保持第一候选，bet_c 留给 HandHist）"""
    amb = [s for s in slots if len(s["bet_c"]) > 1]
    if not amb or pot is None: return
    fixed = sum(s["bet"] for s in slots if s["bet"] is not None and len(s["bet_c"]) <= 1)
    target = pot - (pot_prev or 0) - fixed
    ok = [combo for combo in itertools.product(*[s["bet_c"] for s in amb]) if sum(combo) == target]
    if len({c for c in ok}) == 1:
        for s, v in zip(amb, ok[0]): s["bet"], s["bet_c"] = v, [v]


# ---------------------------------------------------------------- 状态接入
def attach(st, toks, rects=()):
    """state.from_toks 末尾调用：st["table"]=read_table(...)；≥3 人入座时按座位改写 nums 的本街下注/需跟/底池/我方筹码"""
    try: tb = read_table(toks, dealer=st.get("dealer"), rects=rects)
    except Exception: return
    st["table"] = tb
    if tb["n"] < 3: return
    tb["toks"] = [o for o in toks if 300 <= o["y"] <= 2450]          # 开局模型校验用
    ok, miss = fits(load_layout(), toks)
    if not ok: tb["misfit"] = [[x, y] for _, x, y in miss]          # 布局外的座位锚点：要重新校验/推断布局
    n = st.setdefault("nums", {})
    hero = next(s for s in tb["slots"] if s["hero"])
    if tb["pot"] is not None: n["pot"] = tb["pot"]
    if tb["pot_prev"] is not None or tb["pot"] is not None: n["pot_prev"] = tb["pot_prev"]
    if hero["stack"] is not None: n["my_stack"] = hero["stack"]
    my = hero["bet"] or 0
    hi = max([s["bet"] or 0 for s in tb["slots"] if s["occ"] and not s["hero"]] or [0])
    n["my_bet"], n["opp_bet"] = my, hi
    b = st.get("buttons") or {}
    btn = n.get("to_call_btn")
    if "让牌" in b: n["to_call"] = 0
    elif isinstance(btn, int) and btn > 0: n["to_call"] = btn
    elif btn == "allin" or (n.get("to_call") == n.get("my_stack") and "跟注" not in b and ("All in" in b or "ALL IN" in b)): n["to_call"] = n.get("my_stack")
    else: n["to_call"] = max(hi - my, 0)


# ---------------------------------------------------------------- 布局推断
def anchors(toks):
    """单帧里能当座位锚的点（头像中心）：昵称+其下方筹码、“弃牌”状态、空座“+”、底部中间我方昵称"""
    out = []
    nums = [o for o in toks if _num(o["t"].strip()) is not None]
    for o in toks:
        t = o["t"].strip()
        if o["y"] < 300 or o["y"] > 2450: continue
        if _is_name(t, o) and any(abs(m["x"] - o["x"]) < 60 and 240 < m["y"] - o["y"] < 330 for m in nums):
            out.append(("name", o["x"], o["y"] - NAME_DY))
        elif t == "弃牌" and o.get("h", 50) >= 40:
            out.append(("fold", o["x"], o["y"]))
        elif t in EMPTY and o.get("h", 0) >= 70:
            out.append(("empty", o["x"], o["y"]))
        elif _is_name(t, o) and abs(o["x"] - 720) < 60 and 1700 < o["y"] < 2300:
            out.append(("hero", o["x"], o["y"] - NAME_DY))
    return out


def derive(frames_toks):
    """多帧 -> 布局：锚点聚类（<80px 合并）+ 左右镜像补全（WePoker 座位关于 x=720 对称），按顺时针排、我方槽放 0 号"""
    pts = [(x, y) for toks in frames_toks for _, x, y in anchors(toks)]
    cl = []
    for x, y in pts:
        c = next((c for c in cl if math.hypot(c[0] / c[2] - x, c[1] / c[2] - y) < 80), None)
        if c: c[0] += x; c[1] += y; c[2] += 1
        else: cl.append([x, y, 1])
    cen = [(round(a / k), round(b / k)) for a, b, k in cl if k >= 2 or len(frames_toks) == 1]
    for x, y in list(cen):
        if abs(x - 720) > 200:
            m = (1440 - x, y)
            if not any(math.hypot(m[0] - a, m[1] - b) < 80 for a, b in cen): cen.append(m)
    if len(cen) < 3: return None
    my = sum(y for _, y in cen) / len(cen)
    hero = max(cen, key=lambda p: (abs(p[0] - 720) < 100, p[1]))
    a0 = math.atan2(hero[1] - my, hero[0] - 720)
    cen.sort(key=lambda p: (math.atan2(p[1] - my, p[0] - 720) - a0) % (2 * math.pi))
    lay = dict(DEFAULT, slots=[list(p) for p in cen], hero=0, src="derived")
    return lay


def fits(lay, toks, tol=80):
    """当前帧的锚点是否都落在布局的座位上（出现布局外的锚点 = 换了桌型/布局，要重新推断和校验）"""
    a = anchors(toks)
    miss = [p for p in a if not any(math.hypot(p[1] - x, p[2] - y) < tol for x, y in lay["slots"])]
    return not miss, miss


def positions_of(tb, dealer_slot):
    """已入座座位按顺时针从 D 位下一家排：单挑 BB, BTN；多人 SB, BB, 然后 UTG…BTN（D 位）。返回 {槽号: 位置}"""
    import table as T
    occ = tb["occ"]
    if dealer_slot not in occ or len(occ) < 2: return {}
    k = occ.index(dealer_slot)
    seq = occ[k + 1:] + occ[:k + 1]
    n = len(seq)
    names = ["BB", "BTN"] if n == 2 else ["SB", "BB"] + T.positions(n)[:-2]
    return dict(zip(seq, names))


def dealer_from_blinds(tb):
    """D 标没识别到时按盲注反推：下注 1 的是 SB（单挑是 BTN），D 位是 SB 的上一家"""
    occ = tb["occ"]
    by = {s["i"]: s["bet"] for s in tb["slots"] if s["occ"]}
    sb = [i for i in occ if by.get(i) == 1]
    if len(sb) != 1 or 2 not in by.values(): return None
    if len(occ) == 2: return sb[0]
    return occ[occ.index(sb[0]) - 1]


# ---------------------------------------------------------------- 开局模型校验
CHECK_SYS = "你是牌桌截图分析助手，只输出 JSON。"
CHECK_Q = """这是 WePoker 德州扑克牌桌截图（原图 1440x3200，图已等比缩小），我方坐在底部中间。下面是 OCR 读到的文字和原图坐标：
{ocr}
请从我方开始按顺时针（我方→左下→左侧→左上→正上→右上→右侧→右下）列出桌上每个座位，空座（显示“+”或无人）也要列出。
昵称请照抄上面 OCR 的写法（读不到写 null）；dealer 标出庄家按钮“D”紧挨着的那个座位。只输出 JSON：
{{"seats":[{{"name":"昵称或null","empty":false,"dealer":false}}, ...]}}"""


def check_prompt(toks, jpg_b64):
    ocr = "；".join(f"{o['t']}@{o['x']},{o['y']}" for o in toks if 300 <= o["y"] <= 2450)
    return {"system": [{"type": "text", "text": CHECK_SYS}],
            "messages": [{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": jpg_b64}},
                {"type": "text", "text": CHECK_Q.format(ocr=ocr)}]}]}


def _nk(t):
    import unicodedata
    return re.sub(r"[\s_]+", "", unicodedata.normalize("NFKC", t or "").lower())


def _same(a, b):
    """昵称近似相同：归一化后相等，或等长且只差 1 个字（模型偶尔读错一个字）"""
    a, b = _nk(a), _nk(b)
    return a == b or (len(a) == len(b) >= 3 and sum(x != y for x, y in zip(a, b)) <= 1)


def compare(tb, ans, names=None):
    """代码结果 vs 模型结果 -> 差异列表（空=一致）。比：座位总数、入座人数、各座位昵称（按顺时针逐个）、D 位。
    names={槽号: 昵称}：本局累计看到的昵称（单帧昵称可能被动作标签盖住）"""
    seats = (ans or {}).get("seats") or []
    diff = []
    if len(seats) != len(tb["slots"]):
        return [f"座位数 代码{len(tb['slots'])} 模型{len(seats)}"]
    names = names or {}
    for s, m in zip(tb["slots"], seats):
        nm = names.get(s["i"]) or s["name"]
        if bool(m.get("empty")) != (not s["occ"]):
            diff.append(f"{s['i']}号座 代码{'有人' if s['occ'] else '空'} 模型{'空' if m.get('empty') else '有人'}")
        elif s["occ"] and not s["hero"] and nm and m.get("name") and not _same(nm, m["name"]):   # 我方昵称来自配置，不比
            diff.append(f"{s['i']}号座 昵称 代码{nm} 模型{m['name']}")
    md = next((i for i, m in enumerate(seats) if m.get("dealer")), None)
    if tb.get("dealer_slot") is not None and md is not None and md != tb["dealer_slot"]:
        diff.append(f"D 位 代码{tb['dealer_slot']}号座 模型{md}号座")
    return diff


def to_jpg_b64(src, width=720):
    import base64, tempfile
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "f.jpg"
        if str(src).endswith(".raw"):
            subprocess.run([sys.executable, str(TOOLS / "raw2jpg.py"), str(src), str(out), str(width)], capture_output=True)
        else:
            subprocess.run(["sips", "-s", "format", "jpeg", "--resampleWidth", str(width), str(src), "--out", str(out)], capture_output=True)
        return base64.b64encode(out.read_bytes()).decode()


def model_check(src, toks, tb, call, names=None, timeout=40):
    """发截图 + OCR 让模型给座位分组和 D 位，与代码结果比对。call(req, timeout=, max_tokens=) -> 文本。返回 (diff, ans)"""
    req = check_prompt(toks, to_jpg_b64(src))
    txt = call(req, timeout=timeout, max_tokens=900)
    m = re.search(r"\{.*\}", txt or "", re.S)
    ans = json.loads(m.group(0)) if m else None
    return (compare(tb, ans, names) if ans else ["模型输出无法解析"]), ans


class Guard:
    """开局校验：入座座位集合变化 / 出现布局外锚点 时在后台线程问一次模型（不阻塞决策）；
    一致 -> layout 文件记 verified；不一致 -> on_bad(why) 由调用方记日志、存截图。make_call=() -> call(req, timeout, max_tokens)"""
    def __init__(self, make_call, on_bad=None, log=None, save=True):
        self.make_call, self.on_bad, self.log, self.save = make_call, on_bad, log or (lambda m: None), save
        self.key, self.busy, self.last = None, False, None

    @staticmethod
    def _key(tb):
        return (tuple(tb["occ"]), bool(tb.get("misfit")))

    def need(self, st):
        tb = st.get("table") or {}
        if tb.get("n", 0) < 3 or self.busy: return False
        return self._key(tb) != self.key

    def maybe(self, src, st, names=None, on_bad=None):
        """src 须是不会被后续截图覆盖的帧文件（调用方先复制）"""
        if not self.need(st): return False
        tb = st["table"]
        toks = tb.get("toks") or []
        self.key, self.busy = self._key(tb), True
        on_bad = on_bad or self.on_bad
        def run():
            try:
                diff, ans = model_check(src, toks, tb, self.make_call(), names)
                self.last = (diff, ans)
                if tb.get("misfit"): diff = [f"布局外锚点 {tb['misfit']}"] + diff
                if diff:
                    self.log(f"[座位校验] 与模型不一致: {'; '.join(diff)}")
                    if on_bad: on_bad("座位校验不一致: " + "; ".join(diff))
                else:
                    self.log(f"[座位校验] 一致：{tb['n']} 人入座，D={tb.get('dealer_slot')}号座")
                    if self.save:
                        lay = dict(load_layout())
                        lay["verified"] = {"t": time.strftime("%Y-%m-%d %H:%M:%S"), "occ": tb["occ"],
                                           "names": [s["name"] for s in (ans or {}).get("seats", [])]}
                        save_layout(lay)
            except Exception as e:
                self.log(f"[座位校验] 失败: {e}")
                self.key = None                      # 下次再试
            finally:
                self.busy = False
        threading.Thread(target=run, daemon=True).start()
        return True


# ---------------------------------------------------------------- CLI
def _frames(d):
    return sorted(Path(d).glob("*.raw"))


def frame_fixture(src):
    """一帧 raw -> {toks, board, hero, rects, dealer}（回放 fixture；toks 只留 y>=160 的字段 t/x/y/w/h/c）"""
    import state as S, pix
    im = pix.load(str(src))
    board, hero, rects = S.read_cards(str(src), im)
    toks = S.full_ocr(str(src))
    return {"f": Path(src).name, "toks": [{k: o[k] for k in ("t", "x", "y", "w", "h", "c") if k in o} for o in toks if o["y"] >= 160],
            "board": board, "hero": hero, "rects": [list(r) for r in rects], "dealer": S.find_dealer(im, toks)}


def main(argv):
    if not argv or argv[0] not in ("show", "derive", "dump", "check"):
        print(__doc__); return
    cmd = argv[0]
    if cmd == "dump":
        fx = [frame_fixture(p) for p in _frames(argv[1])]
        Path(argv[2]).write_text(json.dumps(fx, ensure_ascii=False, separators=(",", ":")) + "\n")
        print(f"{len(fx)} 帧 -> {argv[2]}"); return
    if cmd == "derive":
        lay = derive([frame_fixture(p)["toks"] for p in _frames(argv[1])])
        print(json.dumps(lay, ensure_ascii=False))
        if lay and "--save" in argv: lay["src"] = f"derive {argv[1]}"; save_layout(lay); print("已写", LAYOUT_FILE)
        return
    fx = frame_fixture(argv[1])
    tb = read_table(fx["toks"], dealer=fx["dealer"], rects=fx["rects"])
    ok, miss = fits(load_layout(), fx["toks"])
    print(f"入座 {tb['n']} 人 D={tb['dealer_slot']}号座 底池={tb['pot']} 已收={tb['pot_prev']} 布局{'吻合' if ok else '有布局外锚点 ' + str(miss)}")
    for s in tb["slots"]:
        print(f"  {s['i']}号座{'(我方)' if s['hero'] else ''}: " + ("空" if not s["occ"] else
              f"{s['name']} 筹码={s['stack']} 下注={s['bet']}{'(候选' + str(s['bet_c']) + ')' if len(s['bet_c']) > 1 else ''}"
              + (f" 标签={s['label']}" if s['label'] else "") + (" 已弃牌" if s["folded"] else "")))
    if cmd == "check":
        sys.path.insert(0, str(TOOLS.parent))
        import bot_core as C
        ref = argv[argv.index("--model") + 1] if "--model" in argv else "mafia-anthropic/claude-opus-5"
        t0 = time.time()
        diff, ans = model_check(argv[1], fx["toks"], tb, C.make_model(ref))
        print(f"模型 {ref} {time.time() - t0:.1f}s: {json.dumps(ans, ensure_ascii=False)}")
        print("一致" if not diff else "不一致: " + "; ".join(diff))


if __name__ == "__main__":
    sys.path.insert(0, str(TOOLS))
    main(sys.argv[1:])
