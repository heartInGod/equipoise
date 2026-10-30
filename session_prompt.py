"""session 模式 prompt：开局一次长 system（背景/规则/盲注/策略，可缓存）+ 每手注入对手画像与近况 + 每次决策一条本手完整记录。

请求格式（交给 AnthropicKA / generic_model）：
    {"system": [静态块(cache_control), 动态块], "messages": [user, assistant, ..., user]}
- 静态块：整局不变，走 prompt caching（实测 5k token 命中缓存后耗时与短 prompt 相同）。
- 动态块：每手开始时刷新一次（对手画像：本桌 tables/<桌>/<opp>.md 优先，其次对手库里最近一份 + 最近几手结果摘要），手内不变。
- messages：只有一条 user。每次决策发本手完整记录（手牌编号、当前手牌、未弃牌对手画像、逐个动作：位置[用户名] 动作，剩余 BB）
  + 当前局面快照；不带之前的模型回复（我方动作已在行动线里），每次都以代码记录和当前帧为准。
- 手结束（检测到新手牌）时生成结果摘要（行动线、盈亏、结束方式）。后台线程把这一手写进本桌对局流水 tables/<桌>/hands.jsonl
  （每手一行，含逐动作明细，hid 唯一），模型对话另写本桌 tables/<桌>/msgs.jsonl（同一个 hid，流水里 msgs 字段指向该文件），
  计数 UPSERT 进全局对手库 players/opponents.db，并可触发 ProfileUpdater 异步更新本桌画像（不在决策路径上，不受倒计时约束）。
"""
import json, os, queue, re, secrets, threading, time
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
- 「听牌胜率」已按去重 outs 精确计算，直接用；只有公共牌成对时同花/顺子听要明显打折（对手可能已成葫芦），非坚果同花听只需小幅打折。
- 翻牌圈 12+ outs（同花+顺子组合听）面对全下，到河牌胜率接近所需胜率时倾向跟注。
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


ROOT = Path(__file__).resolve().parent
TABLES = ROOT / "tables"            # 每桌一个目录 tables/<桌>/：table.md、hands.jsonl（对局流水）、msgs.jsonl（模型对话）、<opp>.md（本桌画像）
PLAYERS = ROOT / "players"          # 全局对手库 players/opponents.db（所有对手的累计计数 + 最近一份画像）
HIST_LIMIT = 1000                   # 热启动最多载入最近多少手
COLD_N = 30                         # 对手累计少于这么多手时附“群体参考”


def safe_name(opp):
    """昵称 -> 文件名（空格/特殊字符换成 _）"""
    return re.sub(r"[^\w.-]", "_", opp or "").strip("._") or "unknown"


def same_opp(a, b):
    """两个昵称是否同一人（按 name_key 归一化，容忍 OCR 的 0/O、l/1 混淆）"""
    return bool(a) and bool(b) and wpstate.name_key(a) == wpstate.name_key(b)


def display_name(raw, known=()):
    """OCR 原文 -> 显示用昵称：对手库里已有同键的名字就用它；否则把数字旁边的 O 改回 0"""
    for k in known:
        if same_opp(k, raw): return k
    return re.sub(r"(?<=\d)[Oo]|[Oo](?=\d)", "0", raw.strip())


def known_names(tables=None):
    """出现过的昵称（各桌画像文件名 + 各桌流水里的 opp）；无对手库时兜底用"""
    names = {p.stem for p in _profile_files(tables)}
    names |= {h.get("opp") for h in load_hands(limit=0, tables=tables) if h.get("opp")}
    return sorted(names)


def profile_path(table_dir, opp):
    """本桌画像 tables/<桌>/<opp>.md（文件名按 name_key 匹配，容忍 OCR 误差）；没有返回 None。
    同一个人在不同桌的画像各自独立；本桌没有时由调用方回退到对手库里最近一份画像"""
    if not opp or not table_dir or not Path(table_dir).is_dir(): return None
    c = [p for p in _profile_files_in(Path(table_dir)) if same_opp(p.stem, opp)]
    return max(c, key=lambda p: p.stat().st_mtime) if c else None


def hand_key(h):
    """同一手牌的去重键（回放会把同一批帧重复记录多次，时间戳不同但牌和行动线相同）"""
    return json.dumps([h.get("pos"), h.get("hero"), h.get("board"), h.get("ev")], ensure_ascii=False)


def _hand_files(tables=None):
    """各桌对局流水 tables/*/hands.jsonl（真机每手 + 导入的回放手），只用于重建对手库 / 无库时兜底 / 按 hid 查手"""
    return sorted(Path(tables or TABLES).glob("*/hands.jsonl"))


def rel(p):
    """路径 -> 相对仓库根的字符串（不在仓库里就用绝对路径）"""
    p = Path(os.fspath(p)).resolve()
    try: return str(p.relative_to(ROOT))
    except ValueError: return str(p)


def _profile_files_in(d):
    return [f for f in sorted(Path(d).glob("*.md")) if f.name not in ("table.md", "README.md") and not f.name.startswith("_")]


def _profile_files(tables=None):
    """各桌画像 tables/<桌>/<opp>.md（排除 table.md / README / _ 开头；_template 等 _ 开头的目录也排除）"""
    return [f for d in sorted(Path(tables or TABLES).iterdir()) if d.is_dir() and not d.name.startswith("_")
            for f in _profile_files_in(d)] if Path(tables or TABLES).is_dir() else []


def load_hands(opp=None, exclude=None, tables=None, limit=HIST_LIMIT):
    """跨桌读对局流水（tables/*/hands.jsonl），去重后按时间升序取最近 limit 手。
    opp 给定时只要该对手；exclude 给定时排除该对手（算群体参考）。"""
    seen, out = set(), []
    for f in _hand_files(tables):
        for l in f.read_text(encoding="utf-8").splitlines():
            try: h = json.loads(l)
            except ValueError: continue
            o = h.get("opp")
            if (opp is not None and not same_opp(o, opp)) or (exclude is not None and same_opp(o, exclude)): continue
            k = hand_key(h)
            if k in seen: continue
            seen.add(k); out.append({k2: h.get(k2) for k2 in ("hid", "t", "n", "opp", "pos", "hero", "board", "ev", "how", "delta")})
    out.sort(key=lambda h: h.get("t") or "")
    return out[-limit:] if limit else out


def profile_text(t, cap=2500):
    """画像 md 正文：去掉更新日志，截断到 cap 字"""
    t = re.split(r"\n## 7\. 更新日志", t)[0]
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    return t[:cap] + ("…(截断)" if len(t) > cap else "")


def profile_type(t):
    """画像 md 里「类型：」一行（模板占位不算）"""
    m = re.search(r"^- 类型[：:]\s*(.+)$", t, re.M)
    t = m.group(1).strip() if m else ""
    return None if not t or "/" in t else t[:30]


def _profile_brief(table_dir, opp, cap=2500, prof=None):
    """对手画像：prof=后台查好的画像（本桌 <opp>.md 优先，其次对手库里最近一份）；否则读本桌 <opp>.md；都没有时给出提示"""
    if prof: return prof
    p = profile_path(table_dir, opp)
    if not p:
        return f"对手 {opp or '未知'}：暂无画像（样本不足），按 GTO 基线打，不做偏移。"
    return profile_text(p.read_text(), cap)


def find_profile(table_dir, opp, row=None):
    """（后台线程用）对手画像 {"profile","ptype","src"}：本桌 <opp>.md 优先；没有就用对手库行里最近一份（来自其他桌，作先验）；都没有 None"""
    p = profile_path(table_dir, opp)
    if p:
        t = p.read_text(encoding="utf-8")
        return {"profile": profile_text(t), "ptype": profile_type(t), "src": "本桌"}
    if row and row.get("profile"):
        return {"profile": "（本桌还没有画像，以下是该对手在其他桌的最近一份画像，仅作先验）\n" + row["profile"], "ptype": row.get("ptype"), "src": "他桌"}
    return None


ORDER = ["翻前", "翻牌", "转牌", "河牌"]
EN = {"翻前": "preflop", "翻牌": "flop", "转牌": "turn", "河牌": "river"}
OTHER = {"BTN": "BB", "BB": "BTN"}


def bb(x):
    """筹码 -> BB 文本（1BB=2 筹码）"""
    return "?" if x is None else f"{round(x / 2, 1):g}"


def _pct(a, b):
    return f"{a}/{b}={a / b * 100:.0f}%" if b else "无样本"


# 对手统计计数器（全是可加的计数；“X_opp”=机会数，配对的计数=做了几次）。对手库 players 表每项一列，player_rates 视图换算成百分比
STAT_FIELDS = (
    ("n", "累计手数"),
    ("vp_n", "翻前有主动决策的手（VPIP/PFR 分母）"), ("vp", "主动入池 VPIP（入池率）"), ("pfr", "翻前加注 PFR"),
    ("won", "收池（对手赢下底池）"), ("vp_won", "主动入池后收池"), ("net", "对手净赢筹码（1BB=2，可为负）"),
    ("btn", "按钮位有行动"), ("open", "按钮位开池加注"), ("limp", "按钮位平跟 limp"),
    ("vs_open", "大盲面对我方开池"), ("b3", "3bet"), ("bb_call", "大盲跟注开池"), ("bb_fold", "大盲弃牌"),
    ("vs_limp", "大盲面对我方 limp"), ("iso", "对 limp 加注"),
    ("f3_opp", "开池后面对我方 3bet"), ("f3_fold", "面对 3bet 弃牌"), ("b4", "4bet"),
    ("f4_opp", "3bet 后面对我方 4bet"), ("f4_fold", "面对 4bet 弃牌"),
    ("flop", "看到翻牌"),
    ("cb_opp", "翻牌 cbet 机会"), ("cb", "翻牌 cbet"),
    ("cb2_opp", "转牌二次 cbet 机会"), ("cb2", "转牌二次 cbet"),
    ("fcb_opp", "面对我方翻牌 cbet"), ("fcb", "面对 cbet 弃牌"), ("rcb", "面对 cbet 加注"),
    ("xr_opp", "翻牌让牌后面对我方下注"), ("xr", "翻牌 check-raise"),
    ("agg", "翻后下注/加注次数"), ("calls", "翻后跟注次数"),
    ("wtsd", "到摊牌"), ("wsd", "摊牌赢（我方亏）"),
)
STAT_KEYS = tuple(k for k, _ in STAT_FIELDS)
_RAISE = re.compile(r"加注|全下|下注到")


def opp_stats(hands):
    """从已结束的手（rec: pos=我方位置 BTN/BB, ev, board, how, delta）统计对手计数（字段见 STAT_FIELDS）。
    看不到对手按钮位直接弃牌的手（这类手不进记录），所以按钮位的分母是“有行动”的手。"""
    S = dict.fromkeys(STAT_KEYS, 0); S["n"] = len(hands)
    opp_r = lambda e: e.startswith("对手") and bool(_RAISE.search(e))
    me_r = lambda e: e.startswith("我方") and bool(_RAISE.search(e))
    nxt = lambda seq, i, who: next((j for j in range(i, len(seq)) if seq[j].startswith(who)), None)
    for h in hands:
        ev, pos, board = h.get("ev") or [], h.get("pos"), h.get("board") or []
        vp0 = S["vp"]
        pre = [e for s, e in ev if s == "翻前"]
        last = ev[-1][1] if ev else ""
        folded_pre = not board and last.startswith("我方")          # 翻前以我方动作收尾 = 对手弃牌
        if pos == "BB":                                               # 对手在按钮位（先行动）
            k = nxt(pre, 0, "对手")
            if k is not None and ("平跟" in pre[k] or opp_r(pre[k])):
                S["btn"] += 1; S["vp"] += 1; S["vp_n"] += 1
                if "平跟" in pre[k]: S["limp"] += 1
                else:
                    S["open"] += 1; S["pfr"] += 1
                    j = nxt(pre, k + 1, "我方")
                    if j is not None and me_r(pre[j]):               # 我方 3bet
                        S["f3_opp"] += 1
                        r = nxt(pre, j + 1, "对手")
                        if r is None and folded_pre: S["f3_fold"] += 1
                        elif r is not None and opp_r(pre[r]): S["b4"] += 1
        elif pos == "BTN":                                            # 对手在大盲
            k = nxt(pre, 0, "我方")
            if k is not None and me_r(pre[k]):                        # 我方开池
                S["vs_open"] += 1; S["vp_n"] += 1
                r = nxt(pre, k + 1, "对手")
                if r is not None and opp_r(pre[r]):                   # 对手 3bet
                    S["b3"] += 1; S["vp"] += 1; S["pfr"] += 1
                    j = nxt(pre, r + 1, "我方")
                    if j is not None and me_r(pre[j]):               # 我方 4bet
                        S["f4_opp"] += 1
                        if nxt(pre, j + 1, "对手") is None and folded_pre: S["f4_fold"] += 1
                elif board or (r is not None and "跟注" in pre[r]): S["bb_call"] += 1; S["vp"] += 1
                elif folded_pre: S["bb_fold"] += 1
            elif k is not None and ("跟注" in pre[k] or "平跟" in pre[k]):   # 我方 limp
                S["vs_limp"] += 1; S["vp_n"] += 1
                r = nxt(pre, k + 1, "对手")
                if r is not None and opp_r(pre[r]): S["iso"] += 1; S["vp"] += 1; S["pfr"] += 1
        d = h.get("delta")                                            # 我方盈亏：负 = 对手收池
        if d is not None:
            S["net"] -= d
            if d < 0: S["won"] += 1; S["vp_won"] += S["vp"] > vp0
        agg = next((("opp" if e.startswith("对手") else "me") for e in reversed(pre) if _RAISE.search(e)), None)   # 翻前最后的进攻方
        if len(board) >= 3:
            S["flop"] += 1
            fl = [e for s, e in ev if s == "翻牌"]
            tu = [e for s, e in ev if s == "转牌"]
            bet = lambda e, who: bool(re.match(who + r"(下注|加注|全下)", e))
            def first_act(seq, who, other):
                """who 本街第一个动作（之前 other 没有下注才算有 cbet 机会）；返回 (下标, 是否有机会)"""
                i = next((n for n, e in enumerate(seq) if e.startswith(who)), None)
                return i, i is not None and not any(bet(e, other) for e in seq[:i])
            o, ok = first_act(fl, "对手", "我方")
            opp_cb = False
            if agg == "opp" and ok:                                   # 对手翻牌 cbet 机会（我方没有先下注）
                S["cb_opp"] += 1; opp_cb = bet(fl[o], "对手"); S["cb"] += opp_cb
            if opp_cb and len(board) >= 4:                           # 转牌二次 cbet
                o2, ok2 = first_act(tu, "对手", "我方")
                if ok2: S["cb2_opp"] += 1; S["cb2"] += bet(tu[o2], "对手")
            m, okm = first_act(fl, "我方", "对手")
            if agg == "me" and okm and bet(fl[m], "我方"):             # 我方 cbet
                S["fcb_opp"] += 1
                r = next((e for e in fl[m + 1:] if e.startswith("对手")), None)
                if r and bet(r, "对手"): S["rcb"] += 1
                elif r is None and len(board) == 3 and last == fl[m]: S["fcb"] += 1
            i = next((n for n, e in enumerate(fl) if e.startswith("对手")), None)     # 翻牌 check-raise
            if i is not None and fl[i].startswith("对手让牌"):
                j = next((n for n in range(i + 1, len(fl)) if re.match(r"我方(下注|加注|全下)", fl[n])), None)
                if j is not None:
                    S["xr_opp"] += 1
                    r = next((e for e in fl[j + 1:] if e.startswith("对手")), None)
                    S["xr"] += bool(r and re.match(r"对手(加注|全下)", r))
            if "摊牌" in (h.get("how") or ""):
                S["wtsd"] += 1; S["wsd"] += (h.get("delta") or 0) < 0
        for s, e in ev:
            if s == "翻前": continue
            if re.match(r"对手(下注|加注|全下)", e): S["agg"] += 1
            elif e.startswith("对手跟注"): S["calls"] += 1
    return S


def _type_line(table_dir, opp):
    """<opp>.md 里「类型：」一行（模板占位不算）"""
    p = profile_path(table_dir, opp)
    return profile_type(p.read_text()) if p else None


def opp_desc(S, table_dir=None, opp=None, typ=None):
    """一句话描述：优先用画像的类型判断（typ=对手库里查到的；否则读画像文件），没有就按统计启发式生成"""
    t = typ or _type_line(table_dir, opp)
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


def profile_block(S, desc, n_hist=0, pop=None):
    """手牌下方的对手画像：统计（有历史时为累计=历史+本局）+ 简短描述（后续动作行不再重复）；
    pop=群体统计（对手样本少时作冷启动参考）"""
    tail = []
    if pop and pop["n"] and S["n"] < COLD_N:
        tail = [f"  群体参考（其他对手共{pop['n']}手，该对手样本少时向它回归）："] + ["  " + l for l in _stat_lines(pop, "群体")]
    if not S["n"]: return [f"  本局还没有已结束的手，无统计；简述：{desc}"] + tail
    scope = f"累计{S['n']}手(此前{n_hist}+本局{S['n'] - n_hist})" if n_hist else f"本局{S['n']}手"
    return _stat_lines(S, scope) + [f"  简述：{desc}"] + tail


def _stat_lines(S, scope):
    S = dict(dict.fromkeys(STAT_KEYS, 0), **S)
    af = f"{S['agg'] / S['calls']:.1f}" if S["calls"] else "—"
    return [f"  盈亏：收池{_pct(S['won'], S['n'])}、入池后收池{_pct(S['vp_won'], S['vp'])}、对手净赢 {bb(S['net'])}BB"
            + (f"（{S['net'] / 2 / S['n'] * 100:+.0f}BB/100手）" if S["n"] else ""),
            f"  翻前：{scope} VPIP{_pct(S['vp'], S['vp_n'])}、PFR{_pct(S['pfr'], S['vp_n'])}、开池{_pct(S['open'], S['btn'])}、limp{_pct(S['limp'], S['btn'])}、"
            f"3B{_pct(S['b3'], S['vs_open'])}、4B{_pct(S['b4'], S['f3_opp'])}、大盲面对开池弃{_pct(S['bb_fold'], S['vs_open'])}、"
            f"面对3B弃{_pct(S['f3_fold'], S['f3_opp'])}、面对4B弃{_pct(S['f4_fold'], S['f4_opp'])}、对limp加注{_pct(S['iso'], S['vs_limp'])}",
            f"  翻牌：cbet{_pct(S['cb'], S['cb_opp'])}、面对cbet弃{_pct(S['fcb'], S['fcb_opp'])}/加注{_pct(S['rcb'], S['fcb_opp'])}、"
            f"check-raise{_pct(S['xr'], S['xr_opp'])}、看翻牌{S['flop']}手",
            f"  转牌/河牌：转牌二次cbet{_pct(S['cb2'], S['cb2_opp'])}、翻后下注加注{S['agg']}次/跟注{S['calls']}次（AF {af}）、"
            f"到摊牌{_pct(S['wtsd'], S['flop'])}、摊牌赢{_pct(S['wsd'], S['wtsd'])}"]


def walk(ev, hero_pos):
    """按事件逐步累计投入。返回 (steps, prior)：steps=[(街, who, 动作文本(BB), 该玩家本手累计投入)]；
    prior[街]={'me','opp'} 为该街开始前的累计投入（筹码）。"""
    me_btn = hero_pos == "BTN"
    c = {"me": 1 if me_btn else 2, "opp": 2 if me_btn else 1}     # 本街累计
    base = {"me": 0, "opp": 0}
    prior, steps, cur = {"翻前": dict(base)}, [], "翻前"
    for s, e in ev:
        if s not in ORDER: continue               # 异常街名（如动画帧的 '?'）跳过，不中断结算
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


ACT = (("弃牌", "fold"), ("让牌", "check"), ("平跟", "limp"), ("跟注", "call"), ("全下", "allin"), ("加注", "raise"), ("下注", "bet"))


def hand_detail(rec, table=None, msgs=None):
    """一手的对局流水记录（写 tables/<桌>/hands.jsonl）：hid + 原始记录（重建统计用）+ 逐个动作的位置/类型/注码（BB）；
    msgs=该手模型对话所在文件（tables/<桌>/msgs.jsonl，按 hid 关联）。
    acts[i] = {st 街, who me/opp, pos BTN/BB, act fold/check/limp/call/bet/raise/allin, add 本次投入, to 本街累计到,
               pot 动作前底池, left 动作后剩余筹码（起始筹码未知时为 None）}"""
    pos = rec.get("pos")
    steps, prior, *_ = walk(rec.get("ev") or [], pos)
    me_btn = pos == "BTN"
    last = {"me": 1 if me_btn else 2, "opp": 2 if me_btn else 1}            # 盲注
    start = rec.get("start") or {}
    acts = []
    for (s, who, txt, tot), (_, e) in zip(steps, rec.get("ev") or []):
        a = next((v for k, v in ACT if k in e), "?")
        street_base = (prior.get(s) or {}).get(who, 0)
        acts.append({"st": EN[s], "who": who, "pos": (pos if who == "me" else OTHER.get(pos)) or "?", "act": a,
                     "add": round((tot - last[who]) / 2, 1), "to": round((tot - street_base) / 2, 1),
                     "pot": round((last["me"] + last["opp"]) / 2, 1),
                     "left": None if start.get(who) is None else round(max(start[who] - tot, 0) / 2, 1)})
        last[who] = max(last[who], tot)
    d = {k: rec.get(k) for k in ("hid", "t", "n", "opp", "pos", "hero", "board", "ev", "how", "delta")}
    if table or rec.get("table"): d["table"] = table or rec.get("table")
    if start: d["start_bb"] = {k: round(v / 2, 1) for k, v in start.items() if v is not None}
    d["acts"] = acts
    if msgs: d["msgs"] = msgs
    if rec.get("src"): d["src"] = rec["src"]
    return d


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
            lv = hist.level if hist is not None and getattr(hist, "street", None) == "翻前" else 2
            L.append(wpstate.big_line(pr[1], need, lv, (n.get("opp_bet") or 0) / 2))
    if md and len(board) in (3, 4):
        L.extend(wpstate.draw_line(md[-1], len(board)))
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
    def __init__(self, table_dir=None, opp=None, keep_hands=6, hands_log=None, on_hand_end=None, hero_name=None,
                 history=None, population=None, warm=False, votes=3, log=print, store=None, store_write=False, hist_stats=None,
                 msgs_log=None, on_summary=None):
        """history=该对手此前（跨桌）已结束的手 / hist_stats=其累计统计计数，热启动；population=其他对手的手，对手样本少时作群体参考（冷启动）。
        对手昵称由 see_opp() 从截图识别（多帧投票），识别到/换人后 warm=True 时后台加载该对手跨桌历史，下一手开始生效；
        opp 参数只是初始值（可为 None）。
        每手记录：hands_log=对局流水文件（真机：tables/<桌>/hands.jsonl；回放：logs/<日期>/hands__*.jsonl）；
        msgs_log=模型对话文件（真机：tables/<桌>/msgs.jsonl；按 hid 关联，流水里 msgs 字段记它的路径）。
        on_summary=每手结束的一行摘要回调（真机写 ops.log）。
        store=对手库 OppDB：有它时历史统计/画像/群体参考按主键查库；store_write=True（仅真机）时每手把计数直接加进库（不读 jsonl）。"""
        self.table_dir, self.opp, self.keep = table_dir, opp, keep_hands
        self.hist_S = hist_stats or (opp_stats(history) if history else None)
        self.prof = None                     # 对手画像 {"profile","ptype","src"}：本桌 <opp>.md 优先，其次对手库里最近一份
        self.store, self.store_write = store, store_write
        self.msgs_log, self.on_summary = msgs_log, on_summary
        self.sid = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)      # 本次运行的 id；hid = sid-手序号
        self.pop = opp_stats(population) if population else (store.pop(opp) if store and warm else None)
        self.warm, self.need_votes, self.log = warm, votes, log
        self._votes, self._pending, self._loading, self._thread = [], None, None, None
        self._unwritten = []                 # 对手还没识别出来时结束的手，识别后补上昵称再写
        self._wq, self._writer, self._prof_new = queue.Queue(), None, None   # 每手记录的后台写线程（jsonl/对手库/画像回调都不在主线程）
        self._static()
        self.hands_log, self.on_hand_end = hands_log, on_hand_end
        if opp and warm and history is None and hist_stats is None: self._start_load(opp)
        self.hero_name = hero_name or self._hero_name(table_dir)
        self.recent, self.hands = [], []     # 最近几手摘要（文本） / 本局已结束手的结构化记录（算统计）
        self.msgs, self.ev_ref = [], None
        self.cur = None                      # 当前手：{"hero","board","pos","start_total","ev","start"}
        self._restat()
        self.dynamic = self._dyn()
        self.n_hands = 0

    def _static(self):
        self.static = SYS_STATIC.format(exploit=_exploit_table(), reference=_reference(),
                                        table=_table_brief(self.table_dir) + (f"\n- 当前对手：{self.opp}" if self.opp else ""))

    # ---- 对手识别 / 异步加载 ----
    def see_opp(self, raw):
        """每帧调用：raw=state.opp_name 读到的昵称原文。最近 8 次读数里同一个键 ≥votes 次且与当前对手不同 → 后台加载"""
        if not raw: return
        k = wpstate.name_key(raw)
        self._votes = (self._votes + [(k, raw)])[-8:]
        if sum(v[0] == k for v in self._votes) < self.need_votes: return
        if same_opp(k, self.opp if self._pending is None else self._pending[0]) or same_opp(k, self._loading): return
        raws = [r for kk, r in self._votes if kk == k]
        self._start_load(max(set(raws), key=raws.count))

    def _start_load(self, raw):
        self._loading = raw
        def run():
            try:
                if not self.warm:
                    name = display_name(raw); self._pending = (name, None, None, find_profile(self.table_dir, name))
                elif self.store:                    # 对手库：按主键查库
                    d = self.store.lookup(raw)
                    name = d["name"] if d else display_name(raw)
                    self._pending = (name, d and d["stats"], self.store.pop(name), find_profile(self.table_dir, name, d))
                else:                               # 无库：扫各桌 jsonl
                    name = display_name(raw, known_names())
                    hist, pop = load_hands(name), load_hands(exclude=name)
                    self._pending = (name, opp_stats(hist) if hist else None, opp_stats(pop) if pop else None, find_profile(self.table_dir, name))
            except Exception as e:
                self.log(f"[session] 加载对手 {raw} 失败: {type(e).__name__}: {e}")
                self._pending = (display_name(raw), None, None, None)
            finally:
                self._loading = None
        self._thread = threading.Thread(target=run, daemon=True); self._thread.start()

    def _apply_pending(self):
        """手与手之间切换到识别出的对手：此前对手未知 → 已结束的手补上昵称；换了人 → 本局统计/近况清空"""
        if self._pending is None: return
        name, hist, pop, prof = self._pending; self._pending = None
        if same_opp(name, self.opp):                 # --opp 初值就是这个人：补上历史/画像（本局还没打过手时才并入，免得和本局重复计）
            self.opp = name
            if not self.hands and self.hist_S is None: self.hist_S, self.pop = hist, pop or self.pop
            if prof and not self.prof: self.prof = prof
            return
        if self.opp is None:
            for h in self.hands: h["opp"] = name
        else:
            self.hands, self.recent = [], []
        self.opp, self.hist_S, self.pop = name, hist, pop
        self.prof = prof
        self._static()
        for rec in self._unwritten:
            rec["opp"] = name; self._emit(rec)
        self._unwritten = []
        self.log(f"[session] 识别到对手 {name}：跨桌历史 {hist['n'] if hist else 0} 手，群体参考 {pop['n'] if pop else 0} 手"
                 + (f"，已载入画像（{self.prof['src']}）" if self.prof else ""))

    def _emit(self, rec):
        """一手结束：交给后台写线程（按顺序：追加 jsonl → 累计进对手库 → 画像回调 → 取最新画像），主线程只入队"""
        if self._writer is None:
            self._writer = threading.Thread(target=self._write_loop, daemon=True); self._writer.start()
        self._wq.put(dict(rec))

    def _write_loop(self):
        while True:
            rec = self._wq.get()
            try: self._write(rec)
            except Exception as e: self.log(f"[session] 写手牌记录失败: {type(e).__name__}: {e}")
            finally: self._wq.task_done()

    def _write(self, rec):
        msgs = None
        if self.msgs_log is not None and rec.get("msgs"):                  # 模型对话单独存，按 hid 关联
            p = Path(os.fspath(self.msgs_log)); p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a", encoding="utf-8") as f:
                f.write(json.dumps({"hid": rec.get("hid"), "t": rec.get("t"), "opp": rec.get("opp"), "msgs": rec["msgs"]}, ensure_ascii=False) + "\n")
            msgs = rel(p)
        p = self.hands_log
        if p:
            p = Path(os.fspath(p)); p.parent.mkdir(parents=True, exist_ok=True)
            tb = Path(self.table_dir).name if self.table_dir else None
            with open(p, "a", encoding="utf-8") as f: f.write(json.dumps(hand_detail(rec, tb, msgs), ensure_ascii=False) + "\n")
        if self.store is not None and self.store_write and rec.get("opp"):
            try: self.store.add_hands([rec])            # 这一手的计数直接加到该对手那一行（UPDATE n=n+1, vp=vp+…）
            except Exception as e: self.log(f"[session] 写对手库失败: {type(e).__name__}: {e}")
        if self.on_hand_end:
            try: self.on_hand_end(rec, self)
            except Exception: pass
        if rec.get("opp"):    # 画像可能被 ProfileUpdater 更新过：查好放着，下一手开始时直接换上（本桌 md 优先，其次库里最近一份）
            d = self.store.lookup(rec["opp"]) if self.store is not None else None
            pf = find_profile(self.table_dir, rec["opp"], d)
            if pf: self._prof_new = (rec["opp"], pf)

    def flush(self):
        """等后台写完（退出前 / 测试用）；对手一直没识别出来的手也写进流水（opp=null，不计入对手库）"""
        for rec in self._unwritten: self._emit(rec)
        self._unwritten = []
        self._wq.join()

    def _refresh_prof(self):
        """每手开始换上后台写线程查好的最新画像（主线程不查库）"""
        p, self._prof_new = self._prof_new, None
        if p and same_opp(p[0], self.opp): self.prof = p[1]

    @property
    def n_hist(self):
        return self.hist_S["n"] if self.hist_S else 0

    def _restat(self):
        cur = opp_stats(self.hands)
        self.stats = {k: v + (self.hist_S or {}).get(k, 0) for k, v in cur.items()}
        typ = self.prof and self.prof["ptype"]
        self.desc = opp_desc(self.stats, self.table_dir, self.opp, typ)
        if self.stats["n"] < 4 and self.pop and self.pop["vp_n"] >= 4 and not (typ or _type_line(self.table_dir, self.opp)):
            self.desc = "新对手无样本，先按群体倾向（" + opp_desc(self.pop).replace("初判（样本少）", "") + "）小幅偏移、贴近基线"

    @staticmethod
    def _hero_name(table_dir):
        p = Path(table_dir) / "table.md" if table_dir else None
        m = re.search(r"Hero 昵称[^：]*：\s*([^\s/]+)", p.read_text()) if p and p.exists() else None
        return m.group(1) if m else "乌贼69"

    def _dyn(self):
        L = ["## 对手画像（手与手之间由代码更新）", _profile_brief(self.table_dir, self.opp, prof=self.prof and self.prof["profile"])]
        if self.n_hist and not self.recent:
            L += ["", f"## 与该对手的历史（此前 {self.n_hist} 手，跨桌累计；统计已并入手牌下方的对手画像）"]
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
        if self.on_summary:
            try: self.on_summary(line)
            except Exception: pass
        rec = {"hid": f"{self.sid}-{self.n_hands:04d}", "t": time.strftime("%Y-%m-%d %H:%M:%S"), "n": self.n_hands, "opp": self.opp, "pos": c["pos"], "hero": c["hero"],
               "board": c["board"], "ev": ev, "how": how, "delta": delta, "start": dict(c.get("start") or {}), "msgs": self.msgs}
        self.hands.append(rec)
        if self.opp is None: self._unwritten.append(rec)       # 对手未识别：等识别出来再写（补上 opp）
        else: self._emit(rec)

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
            L += [f"对手画像 {self._name('opp', OTHER.get(pos))}（" + ("跨桌累计统计" if self.n_hist else "本局统计") + "，次数/机会=频率）"] + profile_block(self.stats, self.desc, self.n_hist, self.pop)
        L += [f"起始筹码 我方 {bb(s.get('me'))}BB / 对手 {bb(s.get('opp'))}BB"]
        L += self.lines(hist.ev, pos, st["board"], s, cur_street=street)
        L += ["当前局面（代码计算）"] + snapshot_bb(st, hist)
        L.append("现在根据用户行为、画像和相关数据进行决策行动")
        return "\n".join(L)

    def build(self, st, hist):
        """返回 (请求 dict, 本条 user 文本)"""
        if hist.ev is not self.ev_ref:            # HandHist.reset() 会换新的 ev 列表 => 新的一手
            self._close(st)
            self._apply_pending()
            self._refresh_prof()
            self.n_hands += 1
            self.ev_ref, self.msgs = hist.ev, []
            self.cur = {"hero": list(st["hero"]), "board": [], "pos": hist.pos, "start_total": self._total(st), "ev": hist.ev, "start": {}}
            self._restat()
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
- 这是本桌画像（同一人在不同桌可能打法不同）：统计计数器按这些手牌能确认的数据累加到旧画像上，看不出来的不要编；
  旧画像若注明来自其他桌，只作先验，计数从本桌手牌重新累计，“其他出现过的桌”里写上来源桌；
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
    """每 every 手异步调一次模型更新本桌画像 tables/<桌>/<opp>.md（不在决策路径上），写完同步进对手库。replay 时不要启用。"""
    def __init__(self, model, table_dir, opp, every=10, store=None):
        self.model, self.dir, self.opp, self.every, self.store = model, Path(table_dir), opp, every, store
        self.buf, self.busy = [], False

    def __call__(self, rec, sess):
        opp = rec.get("opp")
        if not opp: return
        if not same_opp(opp, self.opp): self.opp, self.buf = opp, []      # 换对手：缓冲清空，画像写到新对手
        self.buf.append({k: rec[k] for k in ("n", "t", "pos", "hero", "board", "ev", "how", "delta")})
        if self.every <= 0 or len(self.buf) < self.every or self.busy: return
        hands, self.buf, self.busy = self.buf, [], True
        threading.Thread(target=self._run, args=(hands, self.opp), daemon=True).start()

    def _run(self, hands, opp=None):
        opp = opp or self.opp
        try:
            old = profile_path(self.dir, opp)                          # 旧画像：本桌的；本桌没有就用库里最近一份（他桌）作先验
            if old: old_t = old.read_text()
            else:
                d = self.store.lookup(opp) if self.store is not None else None
                old_t = f"（来自其他桌，仅作先验）\n{d['profile']}" if d and d.get("profile") else "(无)"
            p = old or self.dir / f"{safe_name(opp)}.md"
            tpl = (self.dir.parent / "_template" / "_user_template.md")
            prompt = PROFILE_PROMPT.format(opp=opp, table=self.dir.name, tpl=tpl.read_text() if tpl.exists() else "", old=old_t,
                                           hands="\n".join(json.dumps(h, ensure_ascii=False) for h in hands))
            md = self.model(prompt, timeout=180, max_tokens=4000).strip()
            md = re.sub(r"^```(?:markdown)?\n|\n```$", "", md)
            if md.startswith("# "):
                p.parent.mkdir(parents=True, exist_ok=True)
                if p.exists(): p.with_suffix(".md.bak").write_text(p.read_text())
                p.write_text(md + "\n")
                if self.store is not None: self.store.sync_profiles([p])     # 画像进库，下一手开始生效
        except Exception as e:
            import logroll
            logroll.logger("session").error("[profile] 更新 %s 画像失败: %s: %s", opp, type(e).__name__, str(e)[:120], exc_info=True)
        finally:
            self.busy = False
