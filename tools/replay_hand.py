"""离线回放一手牌：按日志时间段取 my_turn 帧，历史按当时实际执行的动作构建，每帧用当前 prompt 重新问模型，对比旧决策。
用法: python3 tools/replay_hand.py 00:51:26 00:52:43 [--model M] [--show]"""
import argparse, json, sys, time
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tools"))
import poker_bot as PB
import state as S
import logroll

ap = argparse.ArgumentParser()
ap.add_argument("t0"); ap.add_argument("t1")
ap.add_argument("--model", default="mafia-anthropic/claude-opus-5"); ap.add_argument("--show", action="store_true")
ap.add_argument("--prompt", choices=["short", "long"], default="short")
ap.add_argument("--log", help="默认取 logs/ 下最近一天的 text__<model>.jsonl")
a = ap.parse_args()
if not a.log: a.log = str(logroll.latest(f"text__{a.model.replace('/', '__')}.jsonl"))

rows = [json.loads(l) for l in open(ROOT / a.log)]
rows = [r for r in rows if a.t0 <= r["t"] <= a.t1 and r["screen"] == "my_turn" and r["act"] not in ("wait",)]
hist = S.HandHist()
for r in rows:
    raw = ROOT / "model_test" / "live" / (r["t"].replace(":", "") + "_my_turn.raw")
    if not raw.exists(): print("缺帧", raw); continue
    st = S.parse(str(raw))
    hist.observe(st)
    import bot_core
    tpl, fn = bot_core.PROMPTS[a.prompt]
    txt = fn(st, hist)
    if a.show: print("-" * 60 + "\n" + txt)
    t = time.time()
    try:
        new = PB.parse_json(PB.call_model(a.model, tpl.format(state=txt), None, timeout=20, max_tokens=200))
    except Exception as e:
        new = {"err": str(e)[:100]}
    print(json.dumps({"t": r["t"], "hero": r["hero"], "board": r["board"], "old": r.get("dec"), "new": new,
                      "sec": round(time.time() - t, 1)}, ensure_ascii=False), flush=True)
    hist.mine({"act": PB.real_act(r.get("dec") or {}, r["act"])}, r["act"], st)   # 历史沿用当时真实动作
