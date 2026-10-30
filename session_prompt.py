"""session 模式 prompt：开局一次长 system（背景/规则/盲注/策略，可缓存）+ 每手注入对手画像与近况 + 每次决策一条本手完整记录。

请求格式（交给 AnthropicKA / generic_model）：
    {"system": [静态块(cache_control), 动态块], "messages": [user, assistant, ..., user]}
- 静态块：整局不变，走 prompt caching（实测 5k token 命中缓存后耗时与短 prompt 相同）。
- 动态块：每手开始时刷新一次（对手画像 <opp>.md + 最近几手结果摘要），手内不变。
- messages：只有一条 user。每次决策发本手完整记录（手牌编号、当前手牌、未弃牌对手画像、逐个动作：位置[用户名] 动作，剩余 BB）
  + 当前局面快照；不带之前的模型回复（我方动作已在行动线里），每次都以代码记录和当前帧为准。
- 手结束（检测到新手牌）时生成结果摘要（行动线、盈亏、结束方式），写 <table>/bot_hands.jsonl，
  并可触发 ProfileUpdater 异步更新画像（不在决策路径上，不受倒计时约束）。
"""
import json, re, threading, time
from pathlib import Path

import state as wpstate

SYS_STATIC = """你是无限注德州扑克单挑(HU)职业牌手，正在 WePoker 手机 App 上用练习币实战，目标是长期赢最多筹码。
每到我方行动，代码会把截图识别成文字发给你；你只负责决定动作，点击由代码完成。

## 牌桌
{table}

## 规则要点
- 单挑：按钮位=小盲(1)，翻前先行动、翻后最后行动（有位置）；大盲(2)翻前后行动、翻后先行动。
- 金额单位一律是 BB（大盲，1BB=2 筹码）。
- 「可选」里 加注/下注[...] 的档位是界面预设按钮（xBB 档=加注到的总额，1/3池、1/2池… 是底池比例）；只能从中选，没有合适档位就选最接近的。
- 「全下N」= 押上我方全部筹码 N；跟注额 ≥ 我方筹码时跟注即全下。
- 每条消息是本手到此刻的完整记录：手牌编号、当前手牌，紧接着是未弃牌对手的画像（本局统计 次数/机会=频率 + 一句简述），
  然后是每条街每个动作（位置[用户名] 动作，动作后剩余筹码），最后是「当前局面」快照。
- 所有数字（剩余筹码、底池、SPR、所需胜率、听牌胜率、起手牌排名、牌力、本局统计）都由代码算出，直接采用，不要自己重算或怀疑。
- 本局统计看不到对手在按钮位直接弃牌的手，开池率会偏高；样本少时统计只作参考。
- 收到消息就说明本手还在进行、轮到我方行动（之前的点击可能没生效），必须从「可选」里选一个动作。

## 策略基线（GTO 近似，再按对手画像偏移）
翻前按代码给的「起手牌 前X%」（X越小越强）：
- 按钮位首次行动：前85%开 2~2.5BB，其余弃。
- 大盲面对 2~3BB 开局：前12% 3bet，前65%跟，其余弃；对手平跟(limp)：前30%加注3BB，其余让牌。
- 面对3bet：前4% 4bet，前20%跟，其余弃；面对4bet及以上：只有前4%继续。
- 面对全下或超过 5BB 的大额加注：直接按代码给的「翻前面对大注」结论：够跟就跟（全下），不够就弃。
翻后：
- 顶对及以上价值下注/加注约2/3池；≥8 outs 强听牌可半诈唬 1/3~1/2 池。
- 翻前加注方在干燥牌面可 1/3 池持续下注；中等牌力过牌控池；无牌力无听牌放弃。
- 面对下注：用「听牌胜率」或成牌实际胜率对比「所需胜率」，不够就弃（差≤5%且有位置可跟）；后门听牌不算。
- 非坚果听牌、对子面的同花/顺子听牌要打折。
偏移：只在「对手画像」有证据的漏洞上偏移（本局统计对照下面「单挑统计参考值」判断偏高/偏低），样本少（<100 手）时只做小幅偏移、贴近基线。
{exploit}{reference}

## 输出
只输出一行 JSON，不要其他文字：
{{"act":"弃牌|让牌|跟注|下注|加注|全下","size":"下注/加注填[]里=号前的档位名，去掉“池”字，如 3BB、1/2，否则null","why":"≤15字"}}"""


def _exploit_table():
    """technology.md §6.1 + §6.2 偏移操作表（按对手类型 / 统计漏洞）"""
    p = Path(__file__).with_name("technology.md")
    if not p.exists(): return ""
    m = re.search(r"### 6\.1.*?(?=### 6\.3)", p.read_text(), re.S)
    return ("\n## 偏移操作表\n" + m.group(0).replace("### ", "")).rstrip() if m else ""


def _sec(name, start, end, drop=()):
    """从 gto.md / technology.md 截取 start..end 之间的一节；去掉分隔线、引用说明和含 drop 关键词的行"""
    p = Path(__file__).with_name(name)
    if not p.exists(): return ""
    m = re.search(start + r".*?(?=" + end + ")", p.read_text(), re.S)
    if not m: return ""
    L = [l for l in m.group(0).splitlines()[1:]          # 去掉原标题（外面另起标题）
         if l.strip() != "---" and not l.startswith(">") and not any(d in l for d in drop)]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(L)).strip()


def _reference():
    """参考资料（单挑深筹码适用的部分）：gto.md §8.1/§2.5/§2.6/§6.2/§7，technology.md §2.4/§4.1/§4.2"""
    parts = [
        ("单挑统计参考值（对照「对手画像」本局统计判断偏离方向）", _sec("gto.md", r"### 8\.1[^\n]*\n", r"\n---|\n## 9")),
        ("筹码深度", _sec("technology.md", r"### 2\.4[^\n]*\n", r"\n---|\n## 3")),
        ("读牌面", _sec("technology.md", r"### 4\.1[^\n]*\n", r"### 4\.2")),
        ("SPR（代码在「当前局面」给出）", _sec("technology.md", r"### 4\.2[^\n]*\n", r"### 4\.3")),
        ("翻后下注尺度", _sec("gto.md", r"### 6\.2[^\n]*\n", r"### 6\.3")),
        ("下注尺度速查（所需胜率已由代码算出；MDF / α 用来判断诈唬和防守）", _sec("gto.md", r"### 2\.5[^\n]*\n", r"### 2\.6")),
        ("公式的适用边界", _sec("gto.md", r"### 2\.6[^\n]*\n", r"\n---|\n## 3")),
        ("Solver 通用结论", re.sub(r"(?m)^\d+\. ", "- ", re.sub(r"；对 UTG[^。\n]*", "", _sec("gto.md", r"## 7\.[^\n]*\n", r"\n---|\n## 8", drop=("SB 对 BB", "多人底池"))))),
    ]
    body = "\n\n".join(f"### {t}\n{s}" for t, s in parts if s)
    return "\n\n## 参考资料（来自 gto.md / technology.md，只摘单挑适用的部分）\n" + body if body else ""


def _table_brief(table_dir):
    """table.md 里的前言 bullet（平台、级别、单位、Hero 昵称）"""
    if not table_dir or not (Path(table_dir) / "table.md").exists():
        return "- WePoker 练习局，盲注 1/2（筹码），单挑。"
    L = [l for l in (Path(table_dir) / "table.md").read_text().splitlines() if l.startswith("- ") and "：" in l]
    return "\n".join(l for l in L if re.match(r"- (平台|游戏|级别|金额单位|Hero)", l)) or "- WePoker 练习局，盲注 1/2（筹码），单挑。"


def _profile_brief(table_dir, opp, cap=2500):
    """对手画像：<opp>.md 去掉更新日志，截断到 cap 字；没有画像时给出提示"""
    p = Path(table_dir) / f"{opp}.md" if table_dir and opp else None
    if not p or not p.exists():
        return f"对手 {opp or '未知'}：暂无画像（样本不足），按 GTO 基线打，不做偏移。"
    t = p.read_text()
    t = re.split(r"\n## 7\. 更新日志", t)[0]
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    return t[:cap] + ("…(截断)" if len(t) > cap else "")


ORDER = ["翻前", "翻牌", "转牌", "河牌"]
EN = {"翻前": "preflop", "翻牌": "flop", "转牌": "turn", "河牌": "river"}
OTHER = {"BTN": "BB", "BB": "BTN"}


def bb(x):
    """筹码 -> BB 文本（1BB=2 筹码）"""
    return "?" if x is None else f"{round(x / 2, 1):g}"


def _pct(a, b):
    return f"{a}/{b}={a / b * 100:.0f}%" if b else "无样本"


def opp_stats(hands):
    """从本局已结束的手（rec: pos=我方位置 BTN/BB, ev, board, how）统计对手数据。看不到对手按钮位直接弃牌的手。"""
    S = dict(n=len(hands), btn=0, open=0, limp=0, vs_open=0, b3=0, bb_call=0, bb_fold=0, f3_opp=0, f3_fold=0,
             vp=0, vp_n=0, pfr=0, cb_opp=0, cb=0, fcb_opp=0, fcb=0, agg=0, calls=0, flop=0, wtsd=0)
    for h in hands:
        ev, pos, board = h.get("ev") or [], h.get("pos"), h.get("board") or []
        pre = [e for s, e in ev if s == "翻前"]
        last = ev[-1][1] if ev else ""
        if pos == "BB":                                   # 对手在按钮位
            k = next((i for i, e in enumerate(pre) if e.startswith("对手开局加注") or "对手平跟" in e), None)
            if k is not None:
                S["btn"] += 1; S["vp"] += 1; S["vp_n"] += 1
                if "平跟" in pre[k]: S["limp"] += 1
                else:
                    S["open"] += 1; S["pfr"] += 1
                    if any(e.startswith("我方加注") or e.startswith("我方全下") for e in pre[k + 1:]):
                        S["f3_opp"] += 1
                        if not board and (last.startswith("我方加注") or last.startswith("我方全下")): S["f3_fold"] += 1
        elif pos == "BTN":                                # 对手在大盲
            k = next((i for i, e in enumerate(pre) if e.startswith("我方")), None)
            if k is not None and (pre[k].startswith("我方加注") or pre[k].startswith("我方全下")):
                S["vs_open"] += 1; S["vp_n"] += 1
                rest = pre[k + 1:]
                if any(e.startswith("对手加注") for e in rest): S["b3"] += 1; S["vp"] += 1; S["pfr"] += 1
                elif board or any(e.startswith("对手跟注") for e in rest): S["bb_call"] += 1; S["vp"] += 1
                elif last.startswith("我方"): S["bb_fold"] += 1
        # 翻前最后的进攻方
        agg = next((("opp" if e.startswith("对手") else "me") for e in reversed(pre) if re.search(r"加注|全下", e)), None)
        if len(board) >= 3:
            S["flop"] += 1
            if "摊牌" in (h.get("how") or ""): S["wtsd"] += 1
            fl = [e for s, e in ev if s == "翻牌"]
            first = next((e for e in fl if e.startswith("对手") or re.match(r"我方(下注|加注|全下)", e)), None)
            if agg == "opp" and first and first.startswith("对手"):
                S["cb_opp"] += 1; S["cb"] += bool(re.match(r"对手(下注|加注)", first))
            if agg == "me" and first and first.startswith("我方"):
                S["fcb_opp"] += 1
                after = fl[fl.index(first) + 1:]
                if not any(re.match(r"对手(跟注|加注)", e) for e in after) and len(board) == 3 and last == first: S["fcb"] += 1
        for s, e in ev:
            if s == "翻前": continue
            if re.match(r"对手(下注|加注)", e): S["agg"] += 1
            elif e.startswith("对手跟注"): S["calls"] += 1
    return S


def _type_line(table_dir, opp):
    """<opp>.md 里「类型：」一行（模板占位不算）"""
    p = Path(table_dir) / f"{opp}.md" if table_dir and opp else None
    if not p or not p.exists(): return None
    m = re.search(r"^- 类型[：:]\s*(.+)$", p.read_text(), re.M)
    t = m.group(1).strip() if m else ""
    return None if not t or "/" in t else t[:30]


def opp_desc(S, table_dir=None, opp=None):
    """一句话描述：优先用画像文件的类型判断，没有就按本局统计启发式生成"""
    t = _type_line(table_dir, opp)
    if t: return t
    if S["vp_n"] < 4: return "样本太少，按基线打"
    vp, pfr = S["vp"] / S["vp_n"], S["pfr"] / S["vp_n"]
    d = ("松凶，常主动抢池" if vp >= .6 and pfr >= .4 else "松被动，爱跟注" if vp >= .6 else
         "紧凶" if pfr >= .25 else "偏紧偏被动")
    if S["cb_opp"] >= 3 and S["cb"] / S["cb_opp"] >= .7: d += "，持续下注多"
    if S["fcb_opp"] >= 3 and S["fcb"] / S["fcb_opp"] >= .6: d += "，面对cbet常弃"
    if S["calls"] >= 3 and S["agg"] / S["calls"] < 1: d += "，翻后偏被动"
    elif S["calls"] >= 3 and S["agg"] / S["calls"] >= 2: d += "，翻后下注加注很多"
    if S["vs_open"] >= 3 and S["bb_fold"] / S["vs_open"] >= .6: d += "，大盲常弃"
    return ("初判（样本少）" if S["n"] < 30 else "") + d


def profile_block(S, desc):
    """手牌下方的对手画像：本局全部统计 + 简短描述（后续动作行不再重复）"""
    if not S["n"]: return [f"  本局还没有已结束的手，无统计；简述：{desc}"]
    af = f"{S['agg'] / S['calls']:.1f}" if S["calls"] else "—"
    return [f"  翻前：本局{S['n']}手 开池{_pct(S['open'], S['btn'])}、limp{_pct(S['limp'], S['btn'])}、3B{_pct(S['b3'], S['vs_open'])}、"
            f"大盲面对开池弃{_pct(S['bb_fold'], S['vs_open'])}、面对3B弃{_pct(S['f3_fold'], S['f3_opp'])}、VPIP{_pct(S['vp'], S['vp_n'])}",
            f"  翻牌：cbet{_pct(S['cb'], S['cb_opp'])}、面对cbet弃{_pct(S['fcb'], S['fcb_opp'])}、看翻牌{S['flop']}手",
            f"  转牌/河牌：翻后下注加注{S['agg']}次/跟注{S['calls']}次（AF {af}）、到摊牌{_pct(S['wtsd'], S['flop'])}",
            f"  简述：{desc}"]


def walk(ev, hero_pos):
    """按事件逐步累计投入。返回 (steps, prior)：steps=[(街, who, 动作文本(BB), 该玩家本手累计投入)]；
    prior[街]={'me','opp'} 为该街开始前的累计投入（筹码）。"""
    me_btn = hero_pos == "BTN"
    c = {"me": 1 if me_btn else 2, "opp": 2 if me_btn else 1}     # 本街累计
    base = {"me": 0, "opp": 0}
    prior, steps, cur = {"翻前": dict(base)}, [], "翻前"
    for s, e in ev:
        if s != cur:
            while cur != s and ORDER.index(cur) < ORDER.index(s):
                hi = max(c.values())
                base = {k: base[k] + hi for k in c}
                cur = ORDER[ORDER.index(cur) + 1]
                prior[cur] = dict(base); c = {"me": 0, "opp": 0}
        who = "me" if e.startswith("我方") else "opp"
        o = "opp" if who == "me" else "me"
        body = e[2:]
        lv = re.search(r"(\dbet)", body)
        lv = f"（{lv.group(1)}）" if lv and s == "翻前" else ""
        if "全下" in body:
            m = re.search(r"共 ?(\d+)", body)
            if m: c[who] = int(m.group(1))
            txt = "全下" + (f" {bb(int(m.group(1)))}BB" if m else "")
        elif "平跟" in body:
            c[who] = 2; txt = "平跟(limp) 0.5BB"
        elif "跟注" in body:
            m = re.search(r"到 ?(\d+)", body)
            if m: c[o] = max(c[o], int(m.group(1)))      # “跟注到 X”：对方至少下到了 X
            amt = max(c[o] - c[who], 0); c[who] = c[o]; txt = f"跟注 {bb(amt)}BB"
        elif re.search(r"到 ?(\d+)", body):
            v = int(re.search(r"到 ?(\d+)", body).group(1)); c[who] = v
            verb = "加注到" if "加注" in body else "下注"
            txt = f"{verb} {bb(v)}BB{lv}"
        elif re.search(r"下注 ?(\d+)", body):
            v = int(re.search(r"下注 ?(\d+)", body).group(1)); c[who] = v; txt = f"下注 {bb(v)}BB"
        elif "?" in body:
            txt = re.sub(r"（.*", "", body).replace("?", "?BB") + lv
        else:
            txt = body
        steps.append((s, who, txt, base[who] + c[who]))
    return steps, prior, cur, c, base


def snapshot_bb(st, hist):
    """当前局面快照（BB 单位），只放决策需要的代码计算结果"""
    n, b = st.get("nums") or {}, st["buttons"]
    hero, board = st["hero"], st["board"]
    L = []
    pr = wpstate.preflop_rank(hero) if not board else None
    if pr: L.append(f"起手牌 {pr[0]} 前{pr[1]['top']:.0f}%（单挑对随机牌胜率 {pr[1]['eq']:.0f}%）")
    md = wpstate.made_desc(hero, board)
    if md: L.append("牌力 " + md[0].split(": ", 1)[1] + ("；" + md[1].replace(": ", " ") if len(md) > 1 else ""))
    pot, tc, ms, os_ = n.get("pot"), n.get("to_call"), n.get("my_stack"), n.get("opp_stack")
    if pot is not None:
        ep = wpstate.cap_call(n)[0]
        L.append(f"底池 {bb(pot)}BB（本街 对手 {bb(n.get('opp_bet') or 0)} / 我方 {bb(n.get('my_bet') or 0)}）"
                 + (f"；对手超出我方筹码的部分会退回，有效底池 {bb(ep)}BB" if ep is not None and ep < pot else ""))
    if ms is not None and os_ is not None:
        L.append(f"有效筹码 {bb(min(ms + (n.get('my_bet') or 0), os_ + (n.get('opp_bet') or 0)))}BB")
        ep = wpstate.cap_call(n)[0] if pot is not None else None
        if board and ep:                       # SPR = 双方剩余较小者 / 当前（有效）底池
            L.append(f"SPR {min(ms, os_) / ep:.1f}")
    if tc:
        pot, tc = wpstate.cap_call(n)          # 对手下注超过我方筹码时，多出的部分会退回，不算赔率
        need = tc / ((pot or 0) + tc) * 100
        L.append(f"需跟注 {bb(tc)}BB，所需胜率 {need:.0f}%" + ("（跟注即全下）" if ms is not None and tc >= ms else ""))
        if pr and tc > 10:
            e = wpstate.vs_big_eq(pr[1]["eq"])
            L.append(f"翻前面对大注 对其范围胜率约 {e:.0f}% {'≥' if e >= need else '<'} 所需 {need:.0f}%（{'够跟' if e >= need else '不够'}）")
    if md and len(board) in (3, 4):
        outs = sum(int(x) for x in re.findall(r"(\d+) outs", md[-1])) if md[-1].startswith("听牌") else 0
        if outs:
            o = min(outs, 15)
            L.append(f"听牌胜率 下一张成牌约 {o * 2}%" + (f"，到河牌约 {min(o * 4, 60)}%（仅跟注即全下时可用）" if len(board) == 3 else "（只剩一张）")
                     + ("；非坚果听牌要打折" if "非坚果" in md[-1] else ""))
    opts = []
    for k in b:
        if k == "跟注": opts.append(f"跟注 {bb(tc)}BB" if tc else "跟注")
        elif k in ("All in", "ALL IN", "全下"): opts.append(f"全下 {bb((ms or 0) + (n.get('my_bet') or 0))}BB")
        elif k in ("弃牌", "让牌"): opts.append(k)
        elif k in ("下注", "加注"):
            ps = []
            for p in wpstate.usable_presets(st):
                a = float(p["amt"]) if p.get("amt") else None
                if p["frac"].endswith("BB"): ps.append(p["frac"] if a is None or abs(a / 2 - float(p["frac"][:-2])) < .01 else f"{p['frac']}={bb(a)}BB")
                else: ps.append(f"{p['frac']}池" + (f"={bb(a)}BB" if a is not None else ""))
            opts.append(f"{k}[{'加注到 ' if tc and ps else ''}{'、'.join(ps)}]" if ps else k)
    L.append("可选 " + " / ".join(opts))
    return L


class HandSession:
    """维护 session 模式的请求。build() 生成本次请求（本手完整记录，单条 user）；commit() 记下模型回复。"""
    def __init__(self, table_dir=None, opp=None, keep_hands=6, hands_log=None, on_hand_end=None, hero_name=None):
        self.table_dir, self.opp, self.keep = table_dir, opp, keep_hands
        self.static = SYS_STATIC.format(exploit=_exploit_table(), reference=_reference(), table=_table_brief(table_dir) + (f"\n- 当前对手：{opp}" if opp else ""))
        self.hands_log, self.on_hand_end = hands_log, on_hand_end
        self.hero_name = hero_name or self._hero_name(table_dir)
        self.recent, self.hands = [], []     # 最近几手摘要（文本） / 本局已结束手的结构化记录（算统计）
        self.msgs, self.ev_ref = [], None
        self.cur = None                      # 当前手：{"hero","board","pos","start_total","ev","start"}
        self.stats = opp_stats([]); self.desc = opp_desc(self.stats, table_dir, opp)
        self.dynamic = self._dyn()
        self.n_hands = 0

    @staticmethod
    def _hero_name(table_dir):
        p = Path(table_dir) / "table.md" if table_dir else None
        m = re.search(r"Hero 昵称[^：]*：\s*([^\s/]+)", p.read_text()) if p and p.exists() else None
        return m.group(1) if m else "乌贼69"

    def _dyn(self):
        L = ["## 对手画像（手与手之间由代码更新）", _profile_brief(self.table_dir, self.opp)]
        L += ["", "## 最近几手（代码记录，单位 BB）"] + (self.recent[-self.keep:] or ["(本局刚开始)"])
        return "\n".join(L)

    @staticmethod
    def _total(st):
        n = st.get("nums") or {}
        ms = n.get("my_stack")
        return None if ms is None else ms + (n.get("my_bet") or 0)

    def _name(self, who, pos):
        return f"{pos or '?'}[{self.hero_name if who == 'me' else (self.opp or '对手')}]"

    def lines(self, ev, pos, board, start, cur_street=None):
        """逐街行动线（位置[用户名] 动作，剩余 BB）。start={'me','opp'} 本手开始时总筹码。"""
        steps, prior, last, _, _ = walk(ev, pos)
        streets = ORDER[:ORDER.index(cur_street) + 1] if cur_street in ORDER else ORDER[:ORDER.index(last) + 1]
        mp, op = pos, OTHER.get(pos)
        out = []
        for s in streets:
            if s == "翻前": head = f"{EN[s]}（盲注 {'我方' if mp == 'BTN' else '对手'} BTN 0.5BB / {'对手' if mp == 'BTN' else '我方'} BB 1BB）:"
            else:
                cards = {"翻牌": board[:3], "转牌": board[3:4], "河牌": board[4:5]}[s]
                pr = prior.get(s)
                head = f"{EN[s]} [{' '.join(cards)}]" + (f" 底池 {bb(pr['me'] + pr['opp'])}BB" if pr else "") + ":"
            acts = []
            for s2, who, txt, tot in steps:
                if s2 != s: continue
                rem = None if start.get(who) is None else max(start[who] - tot, 0)
                tag = "(我方)" if who == "me" else ""
                acts.append(f"{self._name(who, mp if who == 'me' else op)}{tag} {txt}，剩余 {bb(rem)}BB" + ("（全下）" if rem == 0 else ""))
            if s == cur_street:
                acts.append(f"轮到 {self._name('me', mp)}(我方)")
            out.append(head + " " + (" -> ".join(acts) if acts else "(无动作)"))
        return out

    def _close(self, st_new):
        """上一手结束：生成摘要。盈亏 = 新一手开始时我方总筹码 − 上一手开始时总筹码（含期间未观察到的手）"""
        c = self.cur
        if not c: return
        tot = self._total(st_new)
        delta = None if tot is None or c["start_total"] is None else tot - c["start_total"]
        ev = c["ev"]
        last_me = next((e for _, e in reversed(ev) if e.startswith("我方")), "")
        if "弃牌" in last_me: how = "我方弃牌"
        elif re.search(r"我方(下注|加注|全下)", last_me) and (delta or 0) > 0 and ev and not ev[-1][1].startswith("对手"): how = "对手弃牌（推断）"
        elif delta is not None and abs(delta) > 0 and len(c["board"]) == 5: how = "摊牌或河牌后结束"
        else: how = "结束方式未知"
        line = (f"手牌 #{self.n_hands} 我方{c['pos'] or '?'} {' '.join(c['hero'])} | "
                + " ".join(self.lines(ev, c["pos"], c["board"], c.get("start") or {}))
                + f" | {how}，我方 {'?' if delta is None else ('+' if delta > 0 else '') + bb(delta)}BB")
        if delta is not None and abs(delta) > 400: line += "（变化过大，可能有补码或漏观察的手）"
        self.recent.append(line)
        rec = {"t": time.strftime("%Y-%m-%d %H:%M:%S"), "n": self.n_hands, "opp": self.opp, "pos": c["pos"], "hero": c["hero"],
               "board": c["board"], "ev": ev, "how": how, "delta": delta, "msgs": self.msgs}
        self.hands.append(rec)
        if self.hands_log:
            with open(self.hands_log, "a") as f: f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if self.on_hand_end:
            try: self.on_hand_end(rec, self)
            except Exception: pass

    def user_text(self, st, hist):
        c, n = self.cur, st.get("nums") or {}
        pos, street = c["pos"], wpstate.STREETS.get(len(st["board"]), "翻前")
        # 以当前帧为准反推本手开始时的总筹码：start = 当前剩余 + 之前各街投入 + 本街已下
        _, prior, _, _, _ = walk(hist.ev, pos)
        pr = prior.get(street) or {"me": 0, "opp": 0}
        for who, sk, bk in (("me", "my_stack", "my_bet"), ("opp", "opp_stack", "opp_bet")):
            if n.get(sk) is not None: c["start"][who] = n[sk] + pr[who] + (n.get(bk) or 0)
        s = c["start"]
        rk = wpstate.preflop_rank(st["hero"])
        L = [f"手牌 #{self.n_hands}",
             f"当前手牌 {' '.join(st['hero'])}（{wpstate.hand_desc(st['hero'])}" + (f"，{rk[0]} 前{rk[1]['top']:.0f}%" if rk else "") + "）；"
             f"我方 {self._name('me', pos)}，对手 {self._name('opp', OTHER.get(pos))}"]
        if not any(e.startswith("对手弃牌") for _, e in hist.ev):      # 只写未弃牌的对手（单挑时就是唯一的对手）
            L += [f"对手画像 {self._name('opp', OTHER.get(pos))}（本局统计，次数/机会=频率）"] + profile_block(self.stats, self.desc)
        L += [f"起始筹码 我方 {bb(s.get('me'))}BB / 对手 {bb(s.get('opp'))}BB"]
        L += self.lines(hist.ev, pos, st["board"], s, cur_street=street)
        L += ["当前局面（代码计算）"] + snapshot_bb(st, hist)
        L.append("现在根据用户行为、画像和相关数据进行决策行动")
        return "\n".join(L)

    def build(self, st, hist):
        """返回 (请求 dict, 本条 user 文本)"""
        if hist.ev is not self.ev_ref:            # HandHist.reset() 会换新的 ev 列表 => 新的一手
            self._close(st)
            self.n_hands += 1
            self.ev_ref, self.msgs = hist.ev, []
            self.cur = {"hero": list(st["hero"]), "board": [], "pos": hist.pos, "start_total": self._total(st), "ev": hist.ev, "start": {}}
            self.stats = opp_stats(self.hands); self.desc = opp_desc(self.stats, self.table_dir, self.opp)
            self.dynamic = self._dyn()           # 画像/近况/统计只在手开始时刷新，手内 system 保持不变
        self.cur["board"] = list(st["board"])
        if self.cur["pos"] is None: self.cur["pos"] = hist.pos
        user = self.user_text(st, hist)
        req = {"system": [{"type": "text", "text": self.static, "cache_control": {"type": "ephemeral"}},
                          {"type": "text", "text": self.dynamic}],
               "messages": [{"role": "user", "content": user}]}
        return req, user

    def commit(self, user, dec):
        self.msgs = self.msgs + [{"role": "user", "content": user},
                                 {"role": "assistant", "content": json.dumps(dec, ensure_ascii=False)}]


def flatten(req):
    """非 anthropic provider 用：把 session 请求拼成单个文本 prompt"""
    if isinstance(req, str): return req
    parts = [b["text"] for b in req["system"]]
    for m in req["messages"]:
        parts.append(("【我方决策】" if m["role"] == "assistant" else "【牌桌】") + "\n" + m["content"])
    return "\n\n".join(parts)


PROFILE_PROMPT = """你在维护德州扑克对手画像。下面是模板、旧画像（可能为空）和机器人最近记录的手牌（代码自动记录：行动线、结束方式、我方盈亏；
看不到对手底牌，除非注明）。请输出完整的新画像 Markdown（沿用模板结构），要求：
- 统计计数器按这些手牌能确认的数据累加到旧数据上，看不出来的不要编；
- 样本少（<100 手）时类型判断写“初判”，可信度标低，偏移策略要保守；
- “已确认漏洞”每条要写证据（第几手/统计）；
- “最后更新”写到最新手的序号与时间。
只输出 Markdown，不要解释。对手昵称：{opp}；桌：{table}（单挑，我方=乌贼69）。

### 模板
{tpl}

### 旧画像
{old}

### 最近手牌
{hands}"""


class ProfileUpdater:
    """每 every 手异步调一次模型更新 <table>/<opp>.md（不在决策路径上）。replay 时不要启用。"""
    def __init__(self, model, table_dir, opp, every=10):
        self.model, self.dir, self.opp, self.every = model, Path(table_dir), opp, every
        self.buf, self.busy = [], False

    def __call__(self, rec, sess):
        self.buf.append({k: rec[k] for k in ("n", "t", "pos", "hero", "board", "ev", "how", "delta")})
        if self.every <= 0 or len(self.buf) < self.every or self.busy: return
        hands, self.buf, self.busy = self.buf, [], True
        threading.Thread(target=self._run, args=(hands,), daemon=True).start()

    def _run(self, hands):
        try:
            p = self.dir / f"{self.opp}.md"
            tpl = (self.dir.parent / "_template" / "_user_template.md")
            prompt = PROFILE_PROMPT.format(opp=self.opp, table=self.dir.name, tpl=tpl.read_text() if tpl.exists() else "", old=p.read_text() if p.exists() else "(无)",
                                           hands="\n".join(json.dumps(h, ensure_ascii=False) for h in hands))
            md = self.model(prompt, timeout=180, max_tokens=4000).strip()
            md = re.sub(r"^```(?:markdown)?\n|\n```$", "", md)
            if md.startswith("# "):
                if p.exists(): p.with_suffix(".md.bak").write_text(p.read_text())
                p.write_text(md + "\n")
        except Exception as e:
            print(f"[profile] 更新失败: {type(e).__name__}: {str(e)[:80]}", flush=True)
        finally:
            self.busy = False
