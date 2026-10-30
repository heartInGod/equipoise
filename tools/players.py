"""跨桌对手库：players/<opp>/hands.jsonl（导入的历史手）+ players/<opp>.md（全局画像，ProfileUpdater 自动维护）。

机器人真机 session 模式的手记在 tables/<桌>/bot_hands.jsonl；开新桌时 make_session 会按对手昵称跨桌汇总
（tables/*/bot_hands.jsonl + players/*/hands.jsonl，按 位置+手牌+公共牌+行动线 去重）做热启动。
这个工具用来查看对手库，以及把 logs/ 里的回放手牌记录（hands__*.jsonl）导入对手库。

用法：
    python3 tools/players.py list                         # 各对手手数、画像文件
    python3 tools/players.py show little00                # 该对手跨桌统计 + 简述（就是 prompt 里的画像块）
    python3 tools/players.py import logs/2026-10-30/hands__*.jsonl [--opp little00]   # 导入（去重，只追加）
"""
import argparse, json, sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import session_prompt as P


def cmd_list(a):
    hs = P.load_hands(limit=0)
    cnt = Counter(h.get("opp") or "?" for h in hs)
    names = set(cnt) | {p.stem for p in P.PLAYERS.glob("*.md")}
    if not names: print("对手库为空"); return
    for o in sorted(names):
        pp = P.profile_path(None, o)
        print(f"{o:20s} {cnt.get(o, 0):5d} 手  画像 {pp.relative_to(ROOT) if pp else '无'}")


def cmd_show(a):
    hist = P.load_hands(a.opp, limit=0)
    pop = P.load_hands(exclude=a.opp, limit=0)
    S = P.opp_stats(hist)
    print(f"对手 {a.opp}：跨桌 {len(hist)} 手（群体参考 {len(pop)} 手）")
    print("\n".join(P.profile_block(S, P.opp_desc(S, None, a.opp), len(hist), P.opp_stats(pop) if pop else None)))


def cmd_import(a):
    seen = {P.hand_key(h) for h in P.load_hands(limit=0)}
    new, skip = {}, 0
    for f in a.files:
        for l in open(f, encoding="utf-8"):
            try: h = json.loads(l)
            except ValueError: continue
            opp = a.opp or h.get("opp")
            if not opp or not h.get("ev"): skip += 1; continue
            h = {k: h.get(k) for k in ("t", "n", "opp", "pos", "hero", "board", "ev", "how", "delta")}
            h["opp"], h["src"] = opp, str(Path(f).name)
            k = P.hand_key(h)
            if k in seen: skip += 1; continue
            seen.add(k); new.setdefault(opp, []).append(h)
    for opp, hs in new.items():
        d = P.PLAYERS / P.safe_name(opp); d.mkdir(parents=True, exist_ok=True)
        with open(d / "hands.jsonl", "a", encoding="utf-8") as fo:
            for h in hs: fo.write(json.dumps(h, ensure_ascii=False) + "\n")
        print(f"{opp}: 新增 {len(hs)} 手 -> {(d / 'hands.jsonl').relative_to(ROOT)}")
    print(f"跳过（重复/无行动线）{skip} 条")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("list")
    s = sp.add_parser("show"); s.add_argument("opp")
    s = sp.add_parser("import"); s.add_argument("files", nargs="+"); s.add_argument("--opp", help="覆盖记录里的对手昵称")
    a = ap.parse_args()
    {"list": cmd_list, "show": cmd_show, "import": cmd_import}[a.cmd](a)
