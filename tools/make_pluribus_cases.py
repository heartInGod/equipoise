"""Pluribus 6 人桌牌谱 -> 模型评测用例（tools/cases_pluribus.json，model_eval --set pluribus 读）。

数据：Brown & Sandholm (2019) Pluribus 对职业玩家的 10,000 手 6-max NLHE（100BB，盲注 50/100），
PHH 格式，来自 uoftcprg/phh-dataset（Zenodo 10.5281/zenodo.10796886，CC BY 4.0）。
  下载：curl -L -C - -o z1.zip "https://zenodo.org/records/10796886/files/poker-hand-histories.zip?download=1" && unzip z1.zip
用法：python3 tools/make_pluribus_cases.py --src /tmp/plb/data/pluribus [--n 1000] [--seed 7]

每个用例 = Pluribus 的一个决策点：我方=Pluribus 的座位与手牌，ev=此前完整行动线（别人底牌不给）。
  座位：p1..p6 = SB BB UTG HJ CO BTN；金额：1BB=100 -> 项目筹码 1BB=2（四舍五入成整数筹码）；起始 200 筹码。
  ok   = Pluribus 实际动作（参考答案，混合策略局面只代表一次抽样 -> 结果叫“吻合率”，只做模型横向对比）
  bad  = 规则标出的明确严重错误（见 label_bad）；能免费让牌却弃牌由 model_eval 统一判严重。
分层：按 街×局面 配额抽样，同一类里单一动作最多占 60%，避免全是翻前弃牌。
"""
import argparse, glob, itertools, json, random, re, sys, tomllib
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tools")]
from make_preflop_rank import best7

POS = ["SB", "BB", "UTG", "HJ", "CO", "BTN"]
STREETS = ["翻前", "翻牌", "转牌", "河牌"]
QUOTA = {"翻前-未入池": 130, "翻前-面对开池": 150, "翻前-面对3bet+": 100, "翻前-面对平跟": 20,
         "翻牌-单挑": 150, "翻牌-多人": 100, "转牌-单挑": 110, "转牌-多人": 60, "河牌-单挑": 120, "河牌-多人": 60}
RK = "23456789TJQKA"


def chips(x): return int(x / 50 + 0.5)


def cards(s): return [s[i:i + 2] for i in range(0, len(s), 2)]


def ci(c): return RK.index(c[0]) * 4 + "cdhs".index(c[1])


def decision_points(d, fname):
    """一手牌 -> Pluribus 每个决策点 dict"""
    me = d["players"].index("Pluribus")
    hero_pos = POS[me]
    hole, board, ev, out = {}, [], [], []
    street = 0
    c = {i: 0 for i in range(6)}; c[0], c[1] = 1, 2           # 本街投入（筹码）
    tot = {i: 0 for i in range(6)}                           # 之前各街投入
    pre_raises, folded, acted = 1, set(), set()
    for a in d["actions"]:
        p = a.split()
        if p[0] == "d":
            if p[1] == "dh": hole[int(p[2][1:]) - 1] = cards(p[3])
            elif p[1] == "db":
                board += cards(p[2]); street += 1
                for i in c: tot[i] += c[i]; c[i] = 0
                pre_raises = 0
            continue
        i, op = int(p[0][1:]) - 1, p[1]
        if op == "sm": continue
        hi = max(c.values())
        name = "我方" if i == me else POS[i]
        to_call = hi - c[i]
        if i == me:
            active = [j for j in range(6) if j not in folded and j != me]
            amt = chips(int(p[2])) if op == "cbr" else None
            out.append(dict(hero=hole[me], pos=hero_pos, ev=list(ev), board=list(board), street=STREETS[street],
                            to_call=to_call, hi=hi, raises=pre_raises, active=active, plb=op, amt=amt,
                            allin=(op == "cbr" and chips(int(p[2])) + tot[i] >= 200) or (op == "cc" and tot[i] + hi >= 200),
                            pot=sum(tot.values()) + sum(c.values()), src=f"{fname}#hand{d['hand']}", stack=200 - tot[i] - c[i],
                            limped=street == 0 and hi == 2 and any(c[j] == 2 for j in range(6) if j not in (0, 1))))
        st = STREETS[street]
        if op == "f": ev.append(f"{st} {name} 弃牌"); folded.add(i)
        elif op == "cc":
            if to_call == 0: ev.append(f"{st} {name} 让牌")
            elif street == 0 and hi == 2 and i != 1: ev.append(f"{st} {name} 平跟(limp)")
            else: ev.append(f"{st} {name} 跟注")
            c[i] = hi
        elif op == "cbr":
            v = max(chips(int(p[2])), hi + 1)
            ev.append(f"{st} {name} {'加注到' if hi or street == 0 else '下注到'} {v}")
            c[i] = v; pre_raises += 1
        acted.add(i)
    return out


def scene(dp):
    st = dp["street"]
    if st == "翻前":
        if dp["raises"] >= 3: return "翻前-面对3bet+"
        if dp["raises"] == 2: return "翻前-面对开池"
        return "翻前-面对平跟" if dp["limped"] else "翻前-未入池"
    return f"{st}-{'单挑' if len(dp['active']) == 1 else '多人'}"


def plb_act(dp):
    op = dp["plb"]
    if op == "f": return "弃牌"
    if op == "cc": return "跟注" if dp["to_call"] else "让牌"
    if dp["allin"]: return "全下"
    return "加注" if (dp["street"] == "翻前" or dp["to_call"] or dp["hi"]) else "下注"


def ok_set(a, dp):
    if a == "全下": return ["全下", "加注" if (dp["street"] == "翻前" or dp["hi"]) else "下注"]
    if a in ("加注", "下注"): return [a, "全下"]
    return [a]


def equity(hero, board, nopp, n=400, rng=random.Random(1)):
    """对 nopp 个随机手牌的胜率（平分按份额），蒙特卡洛"""
    h, b = [ci(x) for x in hero], [ci(x) for x in board]
    deck = [x for x in range(52) if x not in h and x not in b]
    win = 0.0
    for _ in range(n):
        s = rng.sample(deck, 2 * nopp + 5 - len(b))
        bb = b + s[2 * nopp:]
        me = best7(h + bb)
        opp = [best7(s[2 * k:2 * k + 2] + bb) for k in range(nopp)]
        top = max(opp)
        if me > top: win += 1
        elif me == top: win += 1 / (1 + sum(o == top for o in opp))
    return win / n


def pre_top(hero):
    import state
    r = state.preflop_rank(hero)
    return r[1]["top"] if r else 50


def label_bad(dp, a):
    """明确的严重错误（与 Pluribus 动作不冲突才标）。返回 (bad, 说明, eq)"""
    bad, why, eq = [], [], None
    nopp = max(len(dp["active"]), 1)
    if dp["street"] == "翻前":
        top = pre_top(dp["hero"])
        if top <= 2 and dp["to_call"]:
            bad.append("弃牌"); why.append(f"前{top}%（AA/KK 级）面对加注弃牌")
        if top >= 60 and dp["to_call"] >= 0.5 * dp["stack"] and dp["to_call"] > 20:
            bad += ["跟注", "全下"]; why.append(f"前{top}% 垃圾牌跟/推大半筹码")
    else:
        eq = equity(dp["hero"], dp["board"], nopp)
        if dp["to_call"] and eq >= 0.85:
            bad.append("弃牌"); why.append(f"对{nopp}家随机牌胜率{eq:.0%}（接近坚果）面对下注弃牌")
        if dp["street"] == "河牌" and dp["to_call"] and eq <= 0.08 and dp["to_call"] * 2 >= dp["pot"] - dp["to_call"]:
            bad.append("跟注"); why.append(f"河牌对随机牌胜率{eq:.0%}（连诈唬都抓不到）跟注 ≥半池下注")
    bad = [x for x in dict.fromkeys(bad) if x not in ok_set(a, dp)]
    return bad, "；".join(why), eq


def to_case(k, dp):
    import model_eval as ME
    a = plb_act(dp)
    bad, bwhy, eq = label_bad(dp, a)
    sz = f" {dp['amt'] / 2:g}BB(到)" if dp["amt"] else ""
    c = dict(id=f"plb_{k:04d}", cat="PLB" + scene(dp), hero=dp["hero"], pos=dp["pos"], ev=[e.replace(" 我方 ", " 我方") for e in dp["ev"]], board=dp["board"],
             ok=ok_set(a, dp), bad=bad, size=None, stack=200, opp=None, modes=["session"], n=6, opps=None, r=None,
             why=f"Pluribus 实际：{a}{sz}" + (f"｜严重：{bwhy}" if bwhy else ""), src=dp["src"],
             plb=dict(act=a, amt=dp["amt"], eq=None if eq is None else round(eq, 3)))
    st, h = ME.build(c)                                   # 能被评测器正确还原才收
    n = st["nums"]
    assert abs(n["to_call"] - min(dp["to_call"], n["my_stack"])) <= 1, (n["to_call"], dp["to_call"])
    assert ("让牌" in st["buttons"]) == (dp["to_call"] == 0)
    return c


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", default="/tmp/plb/data/pluribus"); ap.add_argument("--n", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=7); ap.add_argument("--out", default=str(ROOT / "tools" / "cases_pluribus.json"))
    a = ap.parse_args()
    pool = defaultdict(list)
    fs = sorted(glob.glob(f"{a.src}/**/*.phh", recursive=True))
    for f in fs:
        d = tomllib.load(open(f, "rb"))
        for dp in decision_points(d, str(Path(f).relative_to(a.src))):
            pool[scene(dp)].append(dp)
    print(f"{len(fs)} 手，决策点：" + "，".join(f"{k} {len(v)}" for k, v in sorted(pool.items())))
    rng = random.Random(a.seed)
    scale = a.n / sum(QUOTA.values())
    cases, drop = [], Counter()
    for sc, q in QUOTA.items():
        q = round(q * scale)
        items = pool[sc][:]; rng.shuffle(items)
        cap, cnt, got = int(q * 0.6), Counter(), 0
        for dp in items:
            if got >= q: break
            act = plb_act(dp)
            if cnt[act] >= cap: continue
            try: c = to_case(0, dp)
            except Exception as e: drop[f"{sc}:{type(e).__name__}"] += 1; continue
            cnt[act] += 1; got += 1; cases.append(c)
        print(f"  {sc:<12} {got:>4}/{q}  " + " ".join(f"{k}{v}" for k, v in cnt.most_common()))
    for k, c in enumerate(cases): c["id"] = f"plb_{k:04d}"
    Path(a.out).write_text(json.dumps(cases, ensure_ascii=False, indent=0))
    print(f"写 {len(cases)} 例 -> {a.out}；丢弃（还原不一致）{sum(drop.values())}：{dict(drop)}")
    print(f"带严重错误标注 {sum(1 for c in cases if c['bad'])} 例")


if __name__ == "__main__":
    main()
