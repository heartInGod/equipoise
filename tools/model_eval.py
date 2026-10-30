"""模型决策评测：用 tools/model_cases.py 里的局面问模型，按“实际会执行的动作”判分，统计各类局面的正确率。

出题和判分都走生产代码：局面 -> prompt（short=TPROMPT+brief_text / long / session=HandSession.build，与 tlive 完全一致）
-> 模型 -> parse_json -> execute(RecIO，不点手机) -> real_act 归类。
判定：对 / 尺度偏（动作对、下注档位不在可接受范围）/ 错 / 严重（用例 bad 里的动作，或能免费让牌却弃牌）/ 无效（解析失败/非法 act/超时）。

用法：
  python3 tools/model_eval.py --model rule                         # 本地规则基线（不调模型，也用来检查用例）
  python3 tools/model_eval.py --model mafia-anthropic/claude-opus-5 --prompt session [-n 3] [-j 4]
  python3 tools/model_eval.py --model <m> --cat 翻前 --case pre_btn_AA,pre_btn_72o   # 只跑部分
  python3 tools/model_eval.py --list                               # 列用例
  python3 tools/model_eval.py --show river_board_twopair_trap --prompt session     # 打印发给模型的 prompt（不调模型）
结果：控制台汇总 + logs/<日期>/model_eval__<模型>__<prompt>.jsonl（每次调用一行：用例、动作、判定、回复、耗时、prompt）。
"""
import argparse, json, re, sys, tempfile, time
from fractions import Fraction
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tools")]
import bot_core as C
import state as S
import session_prompt as SP
import logroll
from model_cases import CASES, PROFILES

FOLD, CALL, CHECK, RAISE = [374, 2728], [1066, 2756], [1066, 2730], [720, 2620]
XY = [[304, 2407], [511, 2305], [720, 2291], [927, 2302], [1133, 2407]]
BB_NAMES, POT_NAMES = ["2BB", "3BB", "4BB", "5BB", "6BB"], ["1/3", "1/2", "2/3", "1", "1.2"]
VERDICTS = ["对", "尺度偏", "错", "严重", "无效"]


def build(c):
    """用例 -> (st, hist)：按 walk() 累计投入推出底池/本街下注/剩余筹码，按真机规律给按钮和预设"""
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
    if to_call and to_call >= my_stack: b = {"弃牌": FOLD, "All in": CALL}
    elif to_call: b = {"弃牌": FOLD, "跟注": CALL, **({"加注": RAISE} if opp_stack > 0 else {})}
    else: b = {"弃牌": FOLD, "让牌": CHECK, ("加注" if pre else "下注"): RAISE}
    presets = []
    if ("加注" in b or "下注" in b):
        cap, lo = my_stack + my_bet, (opp_bet + max(to_call, 2) if to_call or opp_bet else 2)
        if pre and opp_bet <= 2:                                   # 未开池/对手平跟：2BB~6BB（加注到）
            amts = [int(x[:-2]) * 2 for x in BB_NAMES]; names = BB_NAMES
        else:                                                      # 面对加注、翻后：底池比例
            names = POT_NAMES
            amts = [round(float(Fraction(f)) * pot) if not to_call else opp_bet + round(float(Fraction(f)) * (pot + to_call)) for f in POT_NAMES]
        for f, a, xy in zip(names, amts, XY):
            presets.append({"frac": f, "amt": str(a), "xy": xy, **({"off": True} if a < lo or a > cap else {})})
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


def prompt_for(c, st, h, mode):
    """与 tlive 相同的 prompt。session：新建 HandSession（无历史手），对手画像用例注入统计计数 + 画像"""
    if mode == "session":
        prof = PROFILES.get(c["opp"]) if c["opp"] else None
        tmp = Path(tempfile.mkdtemp(prefix="model_eval_"))
        sess = SP.HandSession(None, "对手A", hands_log=tmp / "hands.jsonl", log=lambda m: None,
                              hist_stats=dict(dict.fromkeys(SP.STAT_KEYS, 0), **prof["stats"]) if prof else None)
        if prof: sess.prof = {"profile": prof["profile"], "ptype": prof["ptype"], "src": "本桌"}
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


def grade(c, st, eff, frac):
    if eff in c["ok"]:
        if c["size"] and eff in ("下注", "加注") and frac not in c["size"]: return "尺度偏"
        return "对"
    if eff in c["bad"] or (eff == "弃牌" and not st["nums"]["to_call"]): return "严重"
    return "错"


def run_one(c, mode, model, timeout):
    st, h = build(c)
    pr = prompt_for(c, st, h, mode)
    t0, raw, err = time.time(), None, None
    try:
        if model == "rule":
            dec = C.rule_decide(st, h)
        else:
            for k in range(6):                                   # 429 限流：退避重试（不计入耗时）
                t1 = time.time()
                try: raw = model(pr, timeout=timeout, max_tokens=300); break
                except RuntimeError as e:
                    if "429" not in str(e) or k == 5: raise
                    time.sleep(5 * (k + 1)); t0 += time.time() - t1
            dec = C.parse_json(raw)
            if dec.get("act") not in C.ACTS: raise ValueError(f"非法 act: {dec.get('act')}")
    except Exception as e:
        dec, err = None, f"{type(e).__name__}: {str(e)[:120]}"
    lat = round(time.time() - t0, 2)
    if dec is None:
        return dict(id=c["id"], cat=c["cat"], verdict="无效", err=err, raw=raw, lat=lat, prompt=_ptxt(pr))
    eff, frac, act_str = effective(st, dec)
    return dict(id=c["id"], cat=c["cat"], verdict=grade(c, st, eff, frac), eff=eff, frac=frac, act_str=act_str, dec=dec,
                ok=c["ok"], size=c["size"], why=c["why"], raw=raw, lat=lat, prompt=_ptxt(pr))


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
    bad = [r for r in res if r["verdict"] != "对"]
    if bad:
        print("\n不正确的：")
        for r in sorted(bad, key=lambda r: (VERDICTS.index(r["verdict"]), r["id"])):
            if r["verdict"] == "无效": print(f"  [无效] {r['id']}: {r['err']}"); continue
            d = r["dec"]
            print(f"  [{r['verdict']}] {r['id']}: 做了 {r['eff']}{' ' + r['frac'] if r['frac'] else ''}（{d.get('why')}）"
                  f"；应 {'/'.join(r['ok'])}{' ' + '/'.join(r['size']) if r['size'] else ''} —— {r['why']}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default="rule", help="provider/model，或 rule=本地规则基线")
    ap.add_argument("--prompt", choices=["short", "long", "session"], default="session")
    ap.add_argument("-n", type=int, default=1, help="每个用例问几次（看稳定性）")
    ap.add_argument("-j", type=int, default=2, help="并发请求数（接口有 rpm 限流，429 会退避重试）")
    ap.add_argument("--timeout", type=float, default=30)
    ap.add_argument("--cat"); ap.add_argument("--case")
    ap.add_argument("--list", action="store_true"); ap.add_argument("--show")
    a = ap.parse_args()
    if a.list:
        for c in CASES: print(f"{c['cat']:<6} {c['id']:<32} 应 {'/'.join(c['ok']):<10} {c['why']}" + (f"（仅 {','.join(c['modes'])}）" if c["modes"] else ""))
        return
    if a.show:
        c = next(c for c in CASES if c["id"] == a.show); st, h = build(c); pr = prompt_for(c, st, h, a.prompt)
        print(pr if isinstance(pr, str) else SP.flatten(pr) if a.prompt == "session" and "--full" in sys.argv else _ptxt(pr))
        return
    cs = select(a)
    model = "rule" if a.model == "rule" else C.make_model(a.model)
    jobs = [c for c in cs for _ in range(a.n)]
    print(f"评测 {len(cs)} 个用例 × {a.n} 次，model={a.model} prompt={a.prompt}")
    with ThreadPoolExecutor(max_workers=1 if model == "rule" else a.j) as ex:
        res = list(ex.map(lambda c: run_one(c, a.prompt, model, a.timeout), jobs))
    out = logroll.path(f"model_eval__{a.model.replace('/', '__')}__{a.prompt}.jsonl")
    with open(out, "a", encoding="utf-8") as f:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        for r in res: f.write(json.dumps(dict(r, t=ts, model=a.model, mode=a.prompt), ensure_ascii=False) + "\n")
    summary(res)
    print(f"\n明细：{out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
