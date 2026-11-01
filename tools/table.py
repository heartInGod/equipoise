"""N 人桌（2~8 人）：位置、行动线解析（N 个参与者）、翻前范围基线（按人数×位置）和按对手统计收缩的范围表。

行动线 ev = [(街, 事件)]，事件以行动者开头：
  我方…                      我方（hero）
  对手…                      单挑时唯一的对手（旧写法，HandHist / 单挑用例都用它）
  UTG/UTG+1/MP/HJ/CO/BTN/SB/BB …   多人桌按位置标识对手，如 "CO 加注到 5"、"BB 跟注"、"UTG 弃牌"
动作词与单挑一致：弃牌 / 让牌 / 平跟(limp) / 跟注[到 X] / 下注 X / 下注到 X / 加注到 X / 全下(共 X)；金额单位=筹码（1BB=2）。

范围用「前 X%」表示（按单挑对随机牌胜率排序的起手牌前 X%，与 preflop_rank 的 top 同一口径）；
“前X% 去掉前Y%” = 封顶范围（最强的那部分已经用加注打掉了）。基线来自 technology.md §2.1~2.3 / gto.md §7~8（6max 100BB 近似），
按身后还有几人未行动外推到 2~8 人；对手有统计时按 (次数 + K×基线)/(机会 + K) 收缩（样本少贴近基线）。
"""
import re

NONBLIND = ("UTG", "UTG+1", "MP", "HJ", "CO", "BTN")
POS_RE = re.compile(r"^(UTG\+1|UTG|MP|HJ|CO|BTN|SB|BB)\s*")
STREETS = ("翻前", "翻牌", "转牌", "河牌")
HU_OTHER = {"BTN": "BB", "BB": "BTN"}
K = 20                                   # 统计收缩强度：样本 20 次机会时基线和实际各占一半
RFI_BEHIND = {1: 40, 2: 45, 3: 28, 4: 21, 5: 17, 6: 14, 7: 12}   # 身后还有几人 -> 开池约前 X%（1=SB 只剩大盲）
HU_RFI = 80                              # 单挑按钮位开池


def positions(n):
    """翻前行动顺序的位置名：8 人 UTG, UTG+1, MP, HJ, CO, BTN, SB, BB；6 人 UTG, HJ, CO, BTN, SB, BB；单挑 BTN, BB"""
    if n <= 2: return ["BTN", "BB"]
    m = n - 2
    nb = list(NONBLIND[-m:]) if m < 4 else ["UTG"] + list(NONBLIND[-(m - 1):])
    return nb + ["SB", "BB"]


def post_order(n):
    """翻后行动顺序：单挑 BB 先；多人 SB, BB, 然后 UTG…BTN"""
    p = positions(n)
    return ["BB", "BTN"] if n <= 2 else ["SB", "BB"] + p[:-2]


def behind(pos, n):
    """翻前身后还有几人（含盲注）"""
    p = positions(n)
    return len(p) - 1 - p.index(pos) if pos in p else 0


def rfi(pos, n):
    """开池基线（前 X%）"""
    if n <= 2: return HU_RFI if pos == "BTN" else 0
    return RFI_BEHIND.get(behind(pos, n), 12)


def group(pos, n):
    """开池统计分组：sb / bt(BTN) / co / hj / ep(更早)；单挑按钮位单独算（HU 统计的 open/btn）"""
    if n <= 2: return "hu"
    return {1: "sb", 2: "bt", 3: "co", 4: "hj"}.get(behind(pos, n), "ep")


def _bb(x):
    return "?" if x is None else f"{round(x / 2, 1):g}"


# ---------------------------------------------------------------- 行动线
ACT = (("弃牌", "fold"), ("让牌", "check"), ("平跟", "limp"), ("跟注", "call"), ("全下", "allin"), ("加注", "raise"), ("下注", "bet"))


def actor(e, hero_pos, n):
    """事件 -> (行动者 key, 动作文本)。key：'me' / 'opp'（单挑或旧写法）/ 位置名"""
    if e.startswith("我方"): return "me", e[2:]
    if e.startswith("对手"): return "opp", e[2:]
    m = POS_RE.match(e)
    if m:
        p = m.group(1)
        if n <= 2: return ("me" if p == hero_pos else "opp"), e[m.end():]
        return ("me" if p == hero_pos else p), e[m.end():]
    return "opp", e


def players(hero_pos, n):
    """本手参与者 key（翻前顺序）和 key->位置"""
    if n <= 2:
        keys = ["me", "opp"] if hero_pos == "BTN" else ["opp", "me"]
        return keys, {"me": hero_pos, "opp": HU_OTHER.get(hero_pos, "?")}
    pos = positions(n)
    keys = ["me" if p == hero_pos else p for p in pos]
    return keys, {k: (hero_pos if k == "me" else k) for k in keys}


def blinds(hero_pos, n=2):
    """本手开始时各 key 的盲注投入（筹码）"""
    keys, pos_of = players(hero_pos, n)
    return {k: 1 if pos_of[k] == ("BTN" if n <= 2 else "SB") else 2 if pos_of[k] == "BB" else 0 for k in keys}


def walk_full(ev, hero_pos, n=2):
    """按事件逐步累计投入（N 人通用；n<=2 与旧单挑 walk 完全一致）。返回 dict：
    steps=[(街, key, 动作文本(BB), 该玩家本手累计投入)]，prior[街]={key: 该街开始前累计投入}，cur=最后一街，c=本街投入，base=之前各街投入，
    acts=[{st, key, pos, typ(fold/check/limp/call/bet/raise/allin), agg(是否抬高了最高注), to(本街累计到), lv(翻前加注层级 2=开局 3=3bet…)}]，
    folded=已弃牌的 key，active=仍在牌局里的 key（翻前一个动作都没有的非我方玩家视为已弃牌），keys/pos_of。"""
    keys, pos_of = players(hero_pos, n)
    hu = n <= 2
    c = blinds(hero_pos, n)
    base = {k: 0 for k in c}
    prior, steps, acts, cur = {"翻前": dict(base)}, [], [], "翻前"
    folded, acted_pre = set(), set()
    last_agg = next(k for k in c if c[k] == max(c.values()))
    lv = 1
    for s, e in ev:
        if s not in STREETS: continue                 # 异常街名（如动画帧的 '?'）跳过，不中断结算
        if s != cur:
            while cur != s and STREETS.index(cur) < STREETS.index(s):
                hi = max(c.values())
                if hu: base = {k: base[k] + hi for k in c}
                else:
                    if cur == "翻前": folded |= {k for k in c if k != "me" and k not in acted_pre}
                    base = {k: base[k] + (c[k] if k in folded else hi) for k in c}
                cur = STREETS[STREETS.index(cur) + 1]
                prior[cur] = dict(base); c = {k: 0 for k in c}
        who, body = actor(e, hero_pos, n)
        if who not in c: c[who] = 0; base[who] = 0; pos_of.setdefault(who, "?")
        if s == "翻前": acted_pre.add(who)
        hi0 = max(c.values())
        o = "opp" if hu and who == "me" else "me" if hu else last_agg
        tag = re.search(r"(\dbet)", body)
        tag = f"（{tag.group(1)}）" if tag and s == "翻前" else ""
        typ = next((v for k, v in ACT if k in body), "?")
        if "全下" in body:
            m = re.search(r"共 ?(\d+)", body)
            if m: c[who] = int(m.group(1))
            txt = "全下" + (f" {_bb(int(m.group(1)))}BB" if m else "")
        elif "平跟" in body:
            c[who] = max(hi0, 2); txt = "平跟(limp) 0.5BB" if hu else "平跟(limp) 1BB"
        elif "跟注" in body:
            m = re.search(r"到 ?(\d+)", body)
            tgt = c[o] if hu else hi0
            if m:
                x = int(m.group(1))
                if hu: c[o] = max(c[o], x); tgt = c[o]
                elif x > hi0: c[last_agg] = x; tgt = x
            amt = max(tgt - c[who], 0); c[who] = max(c[who], tgt); txt = f"跟注 {_bb(amt)}BB"
        elif re.search(r"到 ?(\d+)", body):
            v = int(re.search(r"到 ?(\d+)", body).group(1)); c[who] = v
            txt = f"{'加注到' if '加注' in body else '下注'} {_bb(v)}BB{tag}"
        elif re.search(r"下注 ?(\d+)", body):
            v = int(re.search(r"下注 ?(\d+)", body).group(1)); c[who] = v; txt = f"下注 {_bb(v)}BB"
        elif "?" in body:
            txt = re.sub(r"（.*", "", body).replace("?", "?BB") + tag
        else:
            txt = body
        if typ == "fold": folded.add(who)
        agg = c[who] > hi0 and typ in ("bet", "raise", "allin")
        if "?" in body and typ in ("bet", "raise"): agg = True          # 金额没读到（"加注到 ?"）也算进攻
        if agg:
            last_agg = who
            if s == "翻前": lv += 1
        acts.append({"st": s, "key": who, "pos": pos_of.get(who, "?"), "typ": typ, "agg": agg, "to": c[who], "lv": lv if s == "翻前" else None})
        steps.append((s, who, txt, base[who] + c[who]))
    active = [k for k in c if k not in folded and (hu or cur != "翻前" or k in acted_pre or k == "me" or not _passed(k, acts, keys))]
    return {"steps": steps, "prior": prior, "cur": cur, "c": c, "base": base, "acts": acts, "folded": folded,
            "active": active, "keys": keys, "pos_of": pos_of}


def _passed(k, acts, keys):
    """翻前：行动已经轮过 k（他后面的人有动作）而他没有动作 → 视为已弃牌"""
    if k not in keys: return False
    i = keys.index(k)
    return any(a["key"] in keys[i + 1:] for a in acts if a["st"] == "翻前")


def to_act(w, hero_pos, n, street):
    """本街我方行动之后还要表态的未弃牌对手（位置名，按本街行动顺序从我方之后排）：
    本街还没动过的，或最后一次动作在最近一次加注之前（要面对加注）的；最近一次加注者本人除外"""
    keys, pos_of = w["keys"], w["pos_of"]
    po = post_order(n)
    order = list(keys) if street == "翻前" else sorted(keys, key=lambda k: po.index(pos_of[k]) if pos_of[k] in po else 0)
    if "me" not in order: return []
    acts = [(i, a) for i, a in enumerate(w["acts"]) if a["st"] == street]
    agg = [i for i, a in acts if a["agg"]]
    li = agg[-1] if agg else -1
    lk = w["acts"][li]["key"] if agg else None
    if street == "翻前" and not agg: lk = None
    last = {}
    for i, a in acts: last[a["key"]] = i
    me = order.index("me")
    seq = order[me + 1:] + order[:me]
    return [pos_of[k] for k in seq if k != "me" and k in w["active"] and k != lk and (k not in last or last[k] < li)]


# ---------------------------------------------------------------- 范围基线与对手范围表
def shrink(base, cnt=0, opp=0, k=K):
    """(次数 + k×基线)/(机会 + k)，返回百分比"""
    return (cnt + k * base / 100) / (opp + k) * 100


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def _fmt(x):
    return f"{x:.1f}".rstrip("0").rstrip(".") if x < 5 else f"{x:.0f}"


def _rate(S, num, den):
    """统计里的 (次数, 机会)；没有该项返回 (0, 0)"""
    if not S: return 0, 0
    return S.get(num, 0) or 0, S.get(den, 0) or 0


def b3_base(opener_pos, n):
    """面对开局加注的 3bet 基线：开局者越宽 3bet 越宽（单挑大盲 12%）"""
    return _clamp(0.25 * rfi(opener_pos, n), 4, 12)


def defend_base(pos, opener_pos, n):
    """面对开局加注的继续总量（3bet + 跟注）基线：大盲最宽（单挑 65%；对 BTN 约 54%、对 UTG 约 29%），小盲多 3bet 少跟，其他位置冷跟窄"""
    r, b3 = rfi(opener_pos, n), b3_base(opener_pos, n)
    if pos == "BB": return min(65, 0.75 * r + 20)
    if pos == "SB": return b3 + 0.15 * r
    return b3 + 0.35 * r


def b4_base(pos, n, opener=True):
    return _clamp(1.5 + 0.03 * rfi(pos, n), 2.5, 5) if opener else 2.5


def ft3_base(n):
    return 0.65 if n <= 2 else 0.5


def _adj(base, S, num, den, hu_num=None, hu_den=None, n=3):
    """按对手统计收缩；返回 (估计值, 说明)"""
    a, b = _rate(S, hu_num or num, hu_den or den) if n <= 2 and hu_num else _rate(S, num, den)
    if not b: return base, f"基线{_fmt(base)}%"
    x = shrink(base, a, b)
    return x, f"基线{_fmt(base)}%，他{a}/{b}"


def preflop_ranges(w, n, stats_of=lambda k: None, hero="me"):
    """翻前各对手（未弃牌）的起点范围。返回 {key: {"pos","label","x","excl","why"}}；没有翻前动作的（还没轮到）不给"""
    acts = [a for a in w["acts"] if a["st"] == "翻前"]
    pos_of = w["pos_of"]
    out, opener, callers, raiser3 = {}, None, [], None
    hi_lv = 1
    for a in acts:
        k, p, t, lv = a["key"], a["pos"], a["typ"], a["lv"]
        S = stats_of(k) if k != hero else None
        info = None
        if a["agg"]:
            if lv == 2:                                         # 开局加注
                opener = k
                big = a["to"] / 2
                if n <= 2:
                    base, why = _adj(rfi(p, n) or HU_RFI, S, "", "", "open", "btn", n)
                else:
                    g = group(p, n)
                    base, why = _adj(rfi(p, n), S, f"rfi_{g}", f"rfi_{g}_opp", n=n)
                if big >= 6:                                    # 开局大注/全下：按尺度收紧（旧 big_range 口径）
                    x2 = 50 if big <= 12 else 35 if big <= 25 else 20 if big <= 50 else 10
                    if x2 < base: base, why = x2, why + f"；开局 {big:g}BB 大注按尺度收紧"
                info = ("开局加注" if t != "allin" else "开局全下", base, 0, why + "；线性，含全部强牌")
            elif lv == 3:                                       # 3bet / squeeze
                op = pos_of.get(opener, "BTN")
                base = b3_base(op, n) * (0.75 if callers else 1)
                x, why = _adj(base, S, "m3", "m3_opp", "b3", "vs_open", n)
                raiser3 = k
                info = ("挤压 squeeze" if callers else "3bet", x, 0, why + "；两极化：价值+阻断诈唬")
            elif lv == 4:
                base = b4_base(p, n, opener=(k == opener))
                x, why = _adj(base, S, "m4", "m4_opp", "b4", "f3_opp", n)
                info = ("4bet", x, 0, why + "；QQ+/AK 为主，少量 A5s 类诈唬")
            else:
                info = (f"{lv}bet/全下", 2.5, 0, "QQ+/AK")
            hi_lv, callers = lv, []
        elif t in ("call", "limp", "allin"):
            if hi_lv == 1:                                      # 平跟
                vb, pb = (70, 55) if n <= 2 else (25, 18)
                vx, _ = _adj(vb, S, "vp", "vp_n", n=n)
                px, _ = _adj(pb, S, "pfr", "vp_n", n=n)
                info = ("平跟(limp)", max(vx, px + 5), min(px, 30) * 0.5, "强牌多已加注；被动型 limp 更宽、偶尔慢打大牌")
            elif hi_lv == 2:
                op = pos_of.get(opener, "BTN")
                base, b3 = defend_base(p, op, n) * (0.8 if callers else 1), b3_base(op, n)
                a_, b_ = ((S.get("bb_call", 0) + S.get("b3", 0), S.get("vs_open", 0)) if n <= 2 else
                          (S.get("mc", 0) + S.get("m3", 0), S.get("m3_opp", 0))) if S else (0, 0)
                tot, why = (shrink(base, a_, b_), f"基线{_fmt(base)}%，他继续{a_}/{b_}") if b_ else (base, f"基线{_fmt(base)}%")
                lab = "大盲跟注" if p == "BB" else "冷跟(cold call)"
                info = (lab, tot, b3, why + "；封顶：最强牌多已 3bet" + ("；多人底池跟注" if callers else ""))
                callers.append(k)
            elif hi_lv == 3:
                if k == opener:
                    ft, why = _adj(ft3_base(n) * 100, S, "mf3", "mf3_opp", "f3_fold", "f3_opp", n)
                    o_x = out.get(k, {}).get("x") or rfi(p, n) or HU_RFI
                    info = ("跟注 3bet", o_x * (1 - ft / 100), b4_base(p, n), f"弃3bet率{why}；封顶中强牌")
                else:
                    info = ("冷跟 3bet", 6, 2.5, "很窄：中大对子、AQ/AK 类")
            else:
                info = (f"跟注 {hi_lv}bet", 4, 2, "AA/KK 多已再加注")
        elif t == "check" and hi_lv == 1:
            info = ("大盲过牌", 100, 12 if n > 2 else 30, "强牌多会加注孤立")
        if info and k != hero:
            lab, x, excl, why = info
            out[k] = {"pos": p, "label": lab, "x": _clamp(x, 0.5, 100), "excl": excl, "why": why}
        if t == "fold": out.pop(k, None)
    return out


def range_text(r):
    x, e = r["x"], r["excl"]
    rng = f"约前{_fmt(x)}%" + (f" 去掉前{_fmt(e)}%" if e and e < x else "")
    return f"{r['label']} → {rng}（{r['why']}）"


def hero_baseline(w, n, hero_pos):
    """我方当前翻前节点的位置基线（还没算对手画像偏移）"""
    acts = [a for a in w["acts"] if a["st"] == "翻前"]
    pos_of = w["pos_of"]
    aggs = [a for a in acts if a["agg"]]
    lv = aggs[-1]["lv"] if aggs else 1
    me_opened = any(a["key"] == "me" and a["agg"] and a["lv"] == 2 for a in acts)
    limpers = [a for a in acts if a["typ"] in ("limp", "call") and not aggs and a["key"] != "me"]
    hu = n <= 2
    if lv == 1:
        if not limpers:
            if hero_pos == "BB": return "无人加注：大盲可免费看翻牌，强牌（约前12%）加注"
            r = rfi(hero_pos, n)
            return (f"首次入池（身后{behind(hero_pos, n)}人）：开池约前{_fmt(r)}%，2~2.5BB" + ("（SB 对 BB 可 3BB）" if hero_pos == "SB" else "")
                    if not hu else f"单挑按钮位首次行动：开池约前{_fmt(r)}~85%，2~2.5BB")
        iso = 30 if hu else max(8, rfi(hero_pos, n) * 0.6) if hero_pos != "BB" else 12
        return f"前面有{len(limpers)}人平跟：约前{_fmt(iso)}% 加注孤立（3~4BB + 每个 limper 1BB），其余能免费就让牌、否则弃"
    if lv == 2:
        op = pos_of.get(next(a["key"] for a in aggs), "BTN")
        callers = [a for a in acts if a["typ"] == "call" and a["key"] != "me"]
        b3, tot = b3_base(op, n), defend_base(hero_pos, op, n)
        if callers: b3, tot = b3 * 0.75, tot * 0.8
        return (f"面对 {op} 开局加注" + (f"（已有{len(callers)}人跟注，挤压更紧、跟注更紧）" if callers else "")
                + f"：3bet 约前{_fmt(b3)}%，继续（3bet+跟注）约前{_fmt(tot)}%，其余弃")
    if lv == 3:
        if me_opened:
            b4 = b4_base(hero_pos, n)
            cont = (rfi(hero_pos, n) or HU_RFI) * (1 - ft3_base(n))
            return f"面对 3bet：4bet 约前{_fmt(b4)}%，继续（4bet+跟注）约前{_fmt(cont)}%，其余弃"
        return "面对开局+3bet（我方未入池）：4bet 约前2.5%，冷跟约前6%，其余弃"
    return "面对 4bet 及以上：只有约前3%（QQ+/AK）继续"


# ---------------------------------------------------------------- 胜率
def vs_eq(info, x):
    """对前 X% 范围的胜率（在 preflop_rank vs 表 5/10/20/35/50 之间线性插值；>50% 向对随机牌胜率插值）"""
    vs = info.get("vs") or {}
    if not vs: return max(info["eq"] * 0.7, info["eq"] - 12)
    pts = sorted((int(k), v) for k, v in vs.items()) + [(100, info["eq"])]
    if x <= pts[0][0]: return pts[0][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x <= x1: return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return info["eq"]


def mw_eq(info, k):
    """对 k 个随机对手的胜率（preflop_rank mw 表；k=1 用 eq）"""
    if k <= 1: return info["eq"]
    mw = info.get("mw") or {}
    return mw.get(str(min(k, 7)))
