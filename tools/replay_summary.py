"""汇总回放日志：画面分布、报错/兜底、模型耗时、新旧决策差异。用法: python3 tools/replay_summary.py logs/<YYYY-MM-DD>/replay__XXXX.jsonl"""
import json, sys, collections
rows = [json.loads(l) for l in open(sys.argv[1])]
print("帧数", len(rows), "画面分布", dict(collections.Counter(r["screen"] for r in rows)))
dec = [r for r in rows if r.get("dec")]
llm = sorted(r["t_llm"] for r in dec if "t_llm" in r)
if llm: print(f"模型调用 {len(llm)} 次，耗时 中位 {llm[len(llm)//2]}s，P90 {llm[int(len(llm)*.9)]}s，最大 {llm[-1]}s")
err = [r for r in dec if r["dec"].get("act") == "ERR"]
fb = [r for r in rows if str(r.get("act", "")).startswith(("兜底", "noop"))]
retry = [r for r in rows if r.get("note") == "上次点击未生效，重试"]
print("模型报错", len(err), "兜底/无法执行", len(fb), "点击未生效重试", len(retry))
for r in err[:5] + fb[:10]: print("  !", r.get("frame"), r["dec"], r["act"])
print("新旧对比(动作不同):")
same = diff = 0
for r in dec:
    o = r.get("old")
    if not o or "ERR" in (o.get("act"), r["dec"].get("act")): continue
    if o.get("act") == r["dec"].get("act"): same += 1; continue
    diff += 1
    print(f"  {r['frame']} {r['hero']} | {r['board']} | 旧 {o.get('act')} {o.get('size') or ''}({o.get('why')}) -> 新 {r['dec'].get('act')} {r['dec'].get('size') or ''}({r['dec'].get('why')})")
print("动作相同", same, "不同", diff)
