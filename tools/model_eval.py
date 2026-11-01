"""模型决策评测：用 tools/model_cases.py 里的局面问模型，按“实际会执行的动作”判分，统计各类局面的正确率。

出题和判分都走生产代码：局面 -> prompt（short=TPROMPT+brief_text / session=HandSession.build，与 tlive 完全一致）
-> 模型 -> parse_json -> execute(RecIO，不点手机) -> real_act 归类。
判定：对 / 尺度偏（动作对、下注档位不在可接受范围）/ 错 / 严重（用例 bad 里的动作，或能免费让牌却弃牌）/ 无效（解析失败/非法 act/超时）。
session 模式与真机一致：代码用随机数 R 按模型给的 mix 频率选最终动作（bot_core.apply_mix）。R 不给模型看；
用例没写 r= 时按 id 固定种子生成（可复现），判分按 mix：ok 动作合计频率 ≥50% 为对、bad ≥50% 为严重；
写了 r= 的成对频率用例（freq_*）按实际执行的动作判，用来检查 R 确实落到了低频动作上。

用法：
  python3 tools/model_eval.py --model rule                         # 本地规则基线（不调模型，也用来检查用例）
  python3 tools/model_eval.py --model mafia-anthropic/claude-opus-5 --prompt session [-n 3] [-j 4]
  python3 tools/model_eval.py --model <m> --cat 翻前 --case pre_btn_AA,pre_btn_72o   # 只跑部分
  python3 tools/model_eval.py --list                               # 列用例
  python3 tools/model_eval.py --show river_board_twopair_trap --prompt session     # 打印发给模型的 prompt（不调模型）
结果：控制台汇总 + logs/<日期>/model_eval__<模型>__<prompt>.jsonl（每次调用一行：用例、动作、判定、回复、耗时、prompt）。
"""
import argparse, json, random, re, sys, tempfile, time
from fractions import Fraction
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tools")]
import bot_core as C
import state as S
import session_prompt as SP
import table as T
import logroll
from model_cases import CASES, PROFILES

FOLD, CALL, CHECK, RAISE = [374, 2728], [1066, 2756], [1066, 2730], [720, 2620]
XY = [[304, 2407], [511, 2305], [720, 2291], [927, 2302], [1133, 2407]]
BB_NAMES, POT_NAMES = ["2BB", "3BB", "4BB", "5BB", "6BB"], ["1/3", "1/2", "2/3", "1", "1.2"]
VERDICTS = ["对", "尺度偏", "错", "严重", "无效"]


def build(c):
    """用例 -> (st, hist)：按 walk() 累计投入推出底池/本街下注/剩余筹码，按真机规律给按钮和预设"""
    if (c.get("n") or 2) > 2: return build_multi(c)
    pos, board = c["pos"], c["board"]
    ev = [tuple(x.split(" ", 1)) for x in c["ev"]]
    street = S.STREETS[len(board)]
    _, _, cur, cc, base = SP.walk(ev, pos)
    if SP.ORDER.index(street) > SP.ORDER.index(cur):          # 行动线停在上一街（本街我方先动）：上一街已平
        hi = max(cc.values()); base = {k: base[k] + hi for k in cc}; cc = {"me": 0, "opp": 0}
    sm, so = c["stack"] if isinstance(c["stack"], (tuple, list)) else (c["stack"], c["stack"])
    my_stack, opp_stack = sm - base["me"] - cc["me"], so - base["opp"] - cc["opp"]
    assert my_stack >= 0 and opp_stack >= 0, (c["id"], my_stack, opp_stack)
    my_bet, opp_bet = cc["me"], cc["opp"]
    to_call = min(max(opp_bet - my_bet, 0), my_stack)
    pot_prev = base["me"] + base["opp"]; pot = pot_prev + my_bet + opp_bet
    pre = street == "翻前"
    b, presets = _buttons(pre, to_call, my_stack, my_bet, opp_bet, opp_stack, pot)
    st = {"screen": "my_turn", "hero": list(c["hero"]), "board": list(board), "buttons": b, "presets": presets,
          "nums": {"pot": pot, "pot_prev": pot_prev, "opp_bet": opp_bet, "my_bet": my_bet, "to_call": to_call,
                   "my_stack": my_stack, "opp_stack": opp_stack},
          "texts": [], "dealer": [720, 2000] if pos == "BTN" else [720, 900], "ms": 0}
    h = S.HandHist()
    h.hero, h.pos, h.ev, h.street = tuple(c["hero"]), pos, list(ev), street
    raises = sum(1 for s, e in ev if s == street and re.search(r"加注|全下|下注", e))
    h.level = (1 + raises) if pre else raises
    h.my_tot, h.opp_tot = my_bet, opp_bet
    return st, h


def _buttons(pre, to_call, my_stack, my_bet, opp_bet, opp_stack, pot):
    if to_call and to_call >= my_stack: b = {"弃牌": FOLD, "All in": CALL}
    elif to_call: b = {"弃牌": FOLD, "跟注": CALL, **({"加注": RAISE} if opp_stack > 0 else {})}
    else: b = {"弃牌": FOLD, "让牌": CHECK, ("加注" if pre else "下注"): RAISE}
    presets = []
    if ("加注" in b or "下注" in b):
        cap, lo = my_stack + my_bet, (opp_bet + max(to_call, 2) if to_call or opp_bet else 2)
        if pre and opp_bet <= 2:                                   # 未开池/只有平跟：2BB~6BB（加注到）
            amts = [int(x[:-2]) * 2 for x in BB_NAMES]; names = BB_NAMES
        else:                                                      # 面对加注、翻后：底池比例
            names = POT_NAMES
            amts = [round(float(Fraction(f)) * pot) if not to_call else opp_bet + round(float(Fraction(f)) * (pot + to_call)) for f in POT_NAMES]
        for f, a, xy in zip(names, amts, XY):
            presets.append({"frac": f, "amt": str(a), "xy": xy, **({"off": True} if a < lo or a > cap else {})})
    return b, presets


def seat_name(p):
    return f"玩家{p}"


def build_multi(c):
    """多人桌用例（n=3~8）：ev 里对手用位置前缀（“翻前 CO 加注到 5”），stack=每人起始筹码（数值或 {位置: 筹码}）。
    nums 另给 stacks={位置: 剩余}、street_bets=本街各人下注之和；opp_bet=本街最高注，opp_stack=在局对手里最深的剩余"""
    pos, board, np_ = c["pos"], c["board"], c["n"]
    ev = [tuple(x.split(" ", 1)) for x in c["ev"]]
    street = S.STREETS[len(board)]
    w = T.walk_full(ev, pos, np_)
    cur, cc, base, folded = w["cur"], dict(w["c"]), dict(w["base"]), set(w["folded"])
    while SP.ORDER.index(street) > SP.ORDER.index(cur):       # 行动线停在上一街：上一街已平（翻前没动作的人视为弃牌）
        hi = max(cc.values())
        if cur == "翻前": folded |= {k for k in cc if k not in w["active"]}
        base = {k: base[k] + (cc[k] if k in folded else hi) for k in cc}; cc = {k: 0 for k in cc}
        cur = SP.ORDER[SP.ORDER.index(cur) + 1]
    pos_of = w["pos_of"]
    stk = c["stack"]
    start = {k: (stk.get(pos_of[k], 200) if isinstance(stk, dict) else stk) for k in cc}
    rem = {k: start[k] - base[k] - cc[k] for k in cc}
    assert all(v >= 0 for v in rem.values()), (c["id"], rem)
    active = [k for k in w["active"] if k != "me" and k not in folded]
    my_stack, my_bet = rem["me"], cc["me"]
    opp_bet = max(cc[k] for k in cc if k != "me")
    opp_stack = max([rem[k] for k in active] or [0])
    to_call = min(max(opp_bet - my_bet, 0), my_stack)
    pot_prev = sum(base.values()); pot = pot_prev + sum(cc.values())
    pre = street == "翻前"
    b, presets = _buttons(pre, to_call, my_stack, my_bet, opp_bet, opp_stack, pot)
    st = {"screen": "my_turn", "hero": list(c["hero"]), "board": list(board), "buttons": b, "presets": presets,
          "nums": {"pot": pot, "pot_prev": pot_prev, "opp_bet": opp_bet, "my_bet": my_bet, "to_call": to_call,
                   "my_stack": my_stack, "opp_stack": opp_stack, "street_bets": sum(cc.values()),
                   "stacks": {pos_of[k]: rem[k] for k in cc if k != "me"}},
          "texts": [], "dealer": [720, 2000] if pos == "BTN" else [720, 900], "ms": 0}
    h = S.HandHist()
    h.hero, h.pos, h.ev, h.street = tuple(c["hero"]), pos, list(ev), street
    h.np, h.seats = np_, {p: seat_name(p) for p in T.positions(np_) if p != pos}
    raises = sum(1 for s, e in ev if s == street and re.search(r"加注|全下|下注", e))
    h.level = (1 + raises) if pre else raises
    h.my_tot, h.opp_tot = my_bet, opp_bet
    return st, h


def _prof_stats(prof, multi=False):
    d = dict(dict.fromkeys(SP.STAT_KEYS, 0), **prof["stats"])
    if multi: d.update(prof.get("mw") or {})
    return d


def case_r(c):
    """用例的随机数 R：显式 r= 用例按指定值，其余按 id 固定种子（结果可复现）"""
    return c["r"] if c.get("r") is not None else random.Random(c["id"]).randint(0, 99)


def prompt_for(c, st, h, mode, explain=False):
    """与 tlive 相同的 prompt。session：新建 HandSession（无历史手），对手画像用例注入统计计数 + 画像"""
    if mode == "session" and (c.get("n") or 2) > 2:
        tmp = Path(tempfile.mkdtemp(prefix="model_eval_"))
        sess = SP.HandSession(None, None, hands_log=tmp / "hands.jsonl", log=lambda m: None, explain=explain)
        for p, key in (c.get("opps") or {}).items():
            pf = PROFILES[key]
            sess.add_opp(seat_name(p), _prof_stats(pf, True), {"profile": pf["profile"], "ptype": pf["ptype"], "src": "本桌"})
        for p in h.seats:
            if seat_name(p) not in sess.book: sess.add_opp(seat_name(p))
        sess.fixed_r = case_r(c)
        req, _ = sess.build(st, h)
        return req
    if mode == "session":
        prof = PROFILES.get(c["opp"]) if c["opp"] else None
        tmp = Path(tempfile.mkdtemp(prefix="model_eval_"))
        sess = SP.HandSession(None, "对手A", hands_log=tmp / "hands.jsonl", log=lambda m: None, explain=explain,
                              hist_stats=dict(dict.fromkeys(SP.STAT_KEYS, 0), **prof["stats"]) if prof else None)
        if prof: sess.prof = {"profile": prof["profile"], "ptype": prof["ptype"], "src": "本桌"}
        sess.fixed_r = case_r(c)
        req, _ = sess.build(st, h)
        return req
    tpl, fn = C.PROMPTS[mode]
    return tpl.format(state=fn(st, h))


def effective(st, dec):
    """模型决策 -> 实际会执行的动作（与真机一致），以及点到的预设档位"""
    act_str = C.execute(st, dec, C.RecIO())
    a = C.real_act(dec, act_str)
    n = st["nums"]
    if a == "全下" and n["to_call"] and n["to_call"] >= n["my_stack"]: a = "跟注"     # 跟注即全下
    if a in ("下注", "加注"): a = "加注" if (not st["board"] or n["to_call"]) else "下注"
    m = re.search(r"preset (\S+?)=", act_str)
    return a, (m.group(1) if m else None), act_str


def mix_eff(st, parts):
    """mix 各段 -> [(实际动作, 尺度, 频率)]（与 effective 同口径：全下=跟注、翻后无人下注加注=下注、能让牌弃牌=让牌）"""
    n, out = st["nums"], []
    for a, sz, f in parts:
        if a == "全下" and n["to_call"] and n["to_call"] >= n["my_stack"]: a = "跟注"
        if a in ("下注", "加注"): a = "加注" if (not st["board"] or n["to_call"]) else "下注"
        if a == "弃牌" and not n["to_call"]: a = "让牌"
        if a == "让牌" and n["to_call"]: a = "弃牌"
        out.append((a, sz, f))
    return out


def grade_mix(c, st, dec):
    """按 mix 判：ok 动作合计频率 ≥50% 为对（主 ok 动作是下注/加注时再看尺度）；bad 动作 ≥50% 为严重。mix 解析不了返回 None"""
    parts = mix_eff(st, C.mix_parse(dec.get("mix")))
    if not parts: return None, None
    tot = sum(f for *_, f in parts)
    w = lambda acts: sum(f for a, _, f in parts if a in acts) * 100 / tot
    okw = round(w(c["ok"]))
    if okw >= 50:
        a, sz, _ = max((p for p in parts if p[0] in c["ok"]), key=lambda p: p[2])
        if c["size"] and a in ("下注", "加注"):
            frac = effective(st, {"act": a, "size": sz or dec.get("size")})[1]
            if frac not in c["size"]: return "尺度偏", okw
        return "对", okw
    if w(c["bad"]) >= 50: return "严重", okw
    return "错", okw


def grade(c, st, eff, frac):
    if eff in c["ok"]:
        if c["size"] and eff in ("下注", "加注") and frac not in c["size"]: return "尺度偏"
        return "对"
    if eff in c["bad"] or (eff == "弃牌" and not st["nums"]["to_call"]): return "严重"
    return "错"


def run_one(c, mode, model, timeout, explain=False):
    st, h = build(c)
    pr = prompt_for(c, st, h, mode, explain)
    t0, raw, err = time.time(), None, None
    try:
        if model == "rule":
            dec = C.rule_decide(st, h)
        else:
            for k in range(6):                                   # 429 限流：退避重试（不计入耗时）
                t1 = time.time()
                try: raw = model(pr, timeout=timeout, max_tokens=1500 if explain else 300); break
                except RuntimeError as e:
                    if "429" not in str(e) or k == 5: raise
                    time.sleep(5 * (k + 1)); t0 += time.time() - t1
            dec = C.parse_json(raw)
            if dec.get("act") not in C.ACTS: raise ValueError(f"非法 act: {dec.get('act')}")
            if mode == "session": dec = C.apply_mix(dec, case_r(c), st["nums"]["to_call"])    # 与真机一致：代码按 mix+R 选动作
    except Exception as e:
        dec, err = None, f"{type(e).__name__}: {str(e)[:120]}"
    lat = round(time.time() - t0, 2)
    if dec is None:
        return dict(id=c["id"], cat=c["cat"], verdict="无效", err=err, raw=raw, lat=lat, prompt=_ptxt(pr))
    eff, frac, act_str = effective(st, dec)
    vm, okw = grade_mix(c, st, dec) if model != "rule" and c.get("r") is None else (None, None)
    return dict(id=c["id"], cat=c["cat"], verdict=vm or grade(c, st, eff, frac), eff=eff, frac=frac, act_str=act_str, dec=dec,
                ok=c["ok"], size=c["size"], why=c["why"], raw=raw, lat=lat, prompt=_ptxt(pr), r=dec.get("r"), okw=okw,
                by="mix" if vm else "act", nomix=model != "rule" and mode == "session" and not C.mix_parse(dec.get("mix")))


def _ptxt(pr):
    return pr if isinstance(pr, str) else pr["messages"][-1]["content"]     # session 只存 user（system 很长且固定）


def select(a):
    cs = [c for c in CASES if not c["modes"] or a.prompt in c["modes"]]
    if a.cat: cs = [c for c in cs if c["cat"] in a.cat.split(",")]
    if a.case: cs = [c for c in cs if c["id"] in a.case.split(",")]
    return cs


def summary(res):
    by = defaultdict(Counter)
    for r in res: by[r["cat"]][r["verdict"]] += 1; by["合计"][r["verdict"]] += 1
    w = max(len(k) for k in by) + 2
    print(f"\n{'类别':<{w}}{'次数':>5}" + "".join(f"{v:>6}" for v in VERDICTS) + f"{'正确率':>8}{'方向对':>8}")
    for k, cnt in sorted(by.items(), key=lambda kv: kv[0] == "合计"):
        n = sum(cnt.values())
        print(f"{k:<{w}}{n:>6}" + "".join(f"{cnt[v]:>7}" for v in VERDICTS)
              + f"{cnt['对'] / n * 100:>9.0f}%{(cnt['对'] + cnt['尺度偏']) / n * 100:>8.0f}%")
    lats = [r["lat"] for r in res if r["verdict"] != "无效"]
    if lats and max(lats) > 0.05:
        lats.sort(); print(f"\n耗时 中位 {lats[len(lats) // 2]:.1f}s / P90 {lats[int(len(lats) * .9)]:.1f}s / 最大 {lats[-1]:.1f}s")
    ov = [r for r in res if (r.get("dec") or {}).get("act_model")]
    if ov:
        print(f"\n代码按 mix+R 改写了模型 act {len(ov)} 次（R 落在非最高频动作上）："
              + "".join(f"\n  {r['id']}: R={r['r']} mix={r['dec'].get('mix')} 模型={r['dec']['act_model']} -> 执行={r['dec']['act']}" for r in ov))
    nomix = sum(1 for r in res if r.get("nomix"))
    if nomix: print(f"mix 缺失或解析不了 {nomix} 次")
    bad = [r for r in res if r["verdict"] != "对"]
    if bad:
        print("\n不正确的：")
        for r in sorted(bad, key=lambda r: (VERDICTS.index(r["verdict"]), r["id"])):
            if r["verdict"] == "无效": print(f"  [无效] {r['id']}: {r['err']}"); continue
            d = r["dec"]
            how = f"mix={d.get('mix')}（ok 动作 {r['okw']}%）" if r.get("by") == "mix" else f"做了 {r['eff']}{' ' + r['frac'] if r['frac'] else ''}"
            print(f"  [{r['verdict']}] {r['id']}: {how}（{d.get('why')}）"
                  f"；应 {'/'.join(r['ok'])}{' ' + '/'.join(r['size']) if r['size'] else ''} —— {r['why']}")


def dump_explain(res):
    """explain 模式：逐例打印模型的范围推演和理论依据"""
    for r in res:
        d = r.get("dec") or {}
        print(f"\n=== [{r['verdict']}] {r['id']}  做 {r.get('eff')}{' ' + r['frac'] if r.get('frac') else ''}"
              f"（应 {'/'.join(r.get('ok') or [])}）{r['lat']}s")
        if not d: print(f"  {r.get('err')}  raw={str(r.get('raw'))[:300]}"); continue
        for k in ("range", "hero", "theory", "mix", "why"): print(f"  {k}: {d.get(k)}")
        if r.get("r") is not None: print(f"  R={r['r']} 执行={d.get('act')}" + (f"（模型 act={d['act_model']}）" if d.get("act_model") else "") + f" 判分按{r.get('by')}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default="rule", help="provider/model，或 rule=本地规则基线")
    ap.add_argument("--prompt", choices=["short", "session"], default="session")
    ap.add_argument("-n", type=int, default=1, help="每个用例问几次（看稳定性）")
    ap.add_argument("-j", type=int, default=2, help="并发请求数（接口有 rpm 限流，429 会退避重试）")
    ap.add_argument("--timeout", type=float, default=30)
    ap.add_argument("--cat"); ap.add_argument("--case")
    ap.add_argument("--list", action="store_true"); ap.add_argument("--show")
    ap.add_argument("--explain", action="store_true", help="测试模式输出：逐街范围推演 hero/theory（仅 session；max_tokens 1500）")
    ap.add_argument("--full", action="store_true", help="--show 时连 system 一起打印")
    a = ap.parse_args()
    if a.list:
        for c in CASES: print(f"{c['cat']:<6} {c['id']:<32} 应 {'/'.join(c['ok']):<10} {c['why']}" + (f"（仅 {','.join(c['modes'])}）" if c["modes"] else ""))
        return
    if a.show:
        c = next(c for c in CASES if c["id"] == a.show); st, h = build(c); pr = prompt_for(c, st, h, a.prompt, a.explain)
        print(pr if isinstance(pr, str) else SP.flatten(pr) if a.full else _ptxt(pr))
        return
    cs = select(a)
    model = "rule" if a.model == "rule" else C.make_model(a.model)
    jobs = [c for c in cs for _ in range(a.n)]
    if a.explain and a.timeout == 30: a.timeout = 90
    print(f"评测 {len(cs)} 个用例 × {a.n} 次，model={a.model} prompt={a.prompt}{' explain' if a.explain else ''}")
    with ThreadPoolExecutor(max_workers=1 if model == "rule" else a.j) as ex:
        res = list(ex.map(lambda c: run_one(c, a.prompt, model, a.timeout, a.explain), jobs))
    out = logroll.path(f"model_eval__{a.model.replace('/', '__')}__{a.prompt}{'_explain' if a.explain else ''}.jsonl")
    with open(out, "a", encoding="utf-8") as f:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        for r in res: f.write(json.dumps(dict(r, t=ts, model=a.model, mode=a.prompt, explain=a.explain), ensure_ascii=False) + "\n")
    if a.explain: dump_explain(res)
    summary(res)
    print(f"\n明细：{out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
