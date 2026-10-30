"""今晚会话分析：模型错误率/超时率、每个 action 耗时拆解、按手统计盈亏(bb/100)。
用法: python3 tools/analyze_session.py [logs/<YYYY-MM-DD>/text__mafia-anthropic__claude-opus-5.jsonl]（默认取最近一天）"""
import json, os, re, sys, statistics as stt
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import logroll
LOG = sys.argv[1] if len(sys.argv) > 1 else str(logroll.latest("text__mafia-anthropic__claude-opus-5.jsonl"))
rows = [json.loads(l) for l in open(LOG)]
sec = lambda t: int(t[:2]) * 3600 + int(t[3:5]) * 60 + int(t[6:8])


def pct(xs, q): xs = sorted(xs); return xs[min(len(xs) - 1, int(len(xs) * q))] if xs else None


# ---------- 模型错误/超时 ----------
dec = [r for r in rows if r.get("dec") and "t_llm" in r]
def is_err(r):
    d = r["dec"]; return d.get("act") == "ERR" or "ERR" in str(d.get("why", ""))[:4]
def is_timeout(r):
    return is_err(r) and re.search(r"time(d)? ?out|超时", str(r["dec"].get("why", "")), re.I) is not None
errs = [r for r in dec if is_err(r)]
tos = [r for r in dec if is_timeout(r)]
bad = [r for r in dec if not is_err(r) and str(r.get("act", "")).startswith(("兜底", "noop"))]
retry = [r for r in rows if r.get("note") == "上次点击未生效，重试"]
print(f"## 模型调用 {len(dec)} 次")
print(f"  调用失败(ERR) {len(errs)} = {len(errs) / len(dec):.1%}；其中超时 {len(tos)} = {len(tos) / len(dec):.1%}")
print(f"  返回了但动作无法执行(走兜底) {len(bad)} = {len(bad) / len(dec):.1%}；点击未生效重试 {len(retry)}")
for r in errs: print(f"    ERR {r['t']} {r['t_llm']}s {str(r['dec'].get('why'))[:70]}")
for r in bad: print(f"    兜底 {r['t']} {r['dec']} -> {r['act']}")

# ---------- 耗时 ----------
print("\n## 每个 action 耗时(秒)：截图 t_cap / 识别 t_parse / 模型 t_llm / 本轮合计 total / 估计体感(含发现延迟)")
acts = []
for i, r in enumerate(rows):
    if r["screen"] != "my_turn" or "t_llm" not in r: continue
    prev = rows[i - 1] if i else None
    # 我方回合在上一轮截图之后某时刻出现：平均发现延迟≈上一轮周期/2；新街时还有 0.5s 等动画+重截
    cyc = (prev.get("total", 2) + 0.3) if prev else 2
    street_wait = 0
    if prev and prev.get("board") != r.get("board") and prev["screen"] != "my_turn":
        street_wait = 0  # 旧版只有 my_turn 帧公共牌变化才重截；无法从日志区分，保守不计
    feel = cyc / 2 + r["total"] + street_wait
    kind = (r["dec"].get("act") if not is_err(r) else "ERR")
    acts.append(dict(kind=kind, cap=r["t_cap"], parse=r["t_parse"], llm=r["t_llm"], total=r["total"], feel=feel,
                     exe=r["total"] - r["t_cap"] - r["t_parse"] - r["t_llm"], t=r["t"]))
def line(name, xs):
    if not xs: return
    f = lambda k: stt.mean(a[k] for a in xs)
    print(f"  {name:6s} n={len(xs):3d}  cap {f('cap'):.2f}  parse {f('parse'):.2f}  llm {f('llm'):.2f}(P90 {pct([a['llm'] for a in xs], .9):.1f})"
          f"  exec {f('exe'):.2f}  total {f('total'):.2f}  体感 {f('feel'):.2f}(P90 {pct([a['feel'] for a in xs], .9):.1f}, max {max(a['feel'] for a in xs):.1f})")
line("全部", acts)
for k in sorted({a["kind"] for a in acts}, key=str): line(str(k), [a for a in acts if a["kind"] == k])
over = [a for a in acts if a["feel"] > 10]
print(f"  体感>10s: {len(over)}/{len(acts)} = {len(over) / max(1, len(acts)):.0%}；>12s: {sum(a['feel'] > 12 for a in acts)}")
# 我方回合真正持续时间：同一手同一街连续 my_turn 帧的跨度(含重复点击/重试)
print(f"  非我回合轮询周期(截图+识别+等待)均值 {stt.mean(r.get('total', 0) for r in rows if r['screen'] != 'my_turn') + 0.3:.2f}s")

# ---------- 盈亏 ----------
cache = logroll.LOGS / "stack_cache.json"
C = json.loads(cache.read_text()) if cache.exists() else {}
import state as S
live = ROOT / "model_test/live"
frames = sorted(p for p in live.glob("*_my_turn.raw"))
hands, cur = [], None
for p in frames:
    k = p.name
    if k not in C:
        try:
            st = S.parse(str(p)); n = st["nums"]
            C[k] = dict(hero=st["hero"], board=st["board"], my=n.get("my_stack"), my_bet=n.get("my_bet"), opp=n.get("opp_stack"),
                        opp_bet=n.get("opp_bet"), pot=n.get("pot"), pot_prev=n.get("pot_prev"), dealer=st.get("dealer"))
        except Exception as e: C[k] = {"err": str(e)}
    c = C[k]
    if not c.get("hero") or len(c["hero"]) != 2 or "?" in "".join(c["hero"]): continue
    if cur is None or tuple(c["hero"]) != cur["hero"] or (not c["board"] and cur["last_board"]):
        cur = dict(hero=tuple(c["hero"]), first=k, frames=[], last_board=None); hands.append(cur)
    cur["frames"].append(c); cur["last_board"] = c["board"]
cache.write_text(json.dumps(C, ensure_ascii=False))
# 每手开始时我方总筹码 = 首帧(翻前) 剩余+本街已下
def start_chips(h):
    f = h["frames"][0]
    if f["board"] or f["my"] is None: return None
    return f["my"] + (f["my_bet"] or 0), (f["opp"] or 0) + (f["opp_bet"] or 0)
seq = [(h["first"][:6], start_chips(h)) for h in hands]
seq = [(t, s) for t, s in seq if s]
print(f"\n## 盈亏：识别到 {len(hands)} 手(有我方行动的)，其中 {len(seq)} 手首帧可读筹码")
if seq:
    # 逐手：我方真实输赢 = 我方筹码变化 - 我方补码。总筹码增加时先归给对手的增量(对手补码)，剩余算我方补码；
    # 总筹码减少(有人离桌/重开)且我方新筹码=200(默认买入) -> 视为我方输光后重新买入
    tot, rebuy, rows_ = 0, 0, []
    for (t0, a), (t1, b) in zip(seq, seq[1:]):
        dme, dopp = b[0] - a[0], b[1] - a[1]; dsum = dme + dopp; add = 0
        if dsum > 20: add = max(0, dsum - max(0, dopp))
        elif dsum < -20 and b[0] == 200: add = 200; dme = 200 - a[0]
        real = dme - add; rebuy += add; tot += real; rows_.append((real, t0))
    n = len(seq) - 1
    print(f"  起始 {seq[0][1][0]}（{seq[0][0]}）→ 末手开始 {seq[-1][1][0]}（{seq[-1][0]}），期间我方补码 {rebuy}")
    print(f"  {n} 手累计 {tot:+d} 筹码 = {tot / 2:+.1f} bb，≈ {tot / 2 / max(n, 1) * 100:+.0f} bb/100（对手直接弃牌、我方无需行动的手没计入手数，实际 bb/100 绝对值更小）")
    rows_.sort()
    print("  单手最大输:", rows_[:6]); print("  单手最大赢:", rows_[-5:][::-1])
