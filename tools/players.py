"""跨桌对手库：players/opponents.db（SQLite，tools/oppdb.py）+ players/<opp>.md（全局画像，ProfileUpdater 自动维护）。

库里每个对手一行：各项统计的计数器（手数、入池、收池、3B 机会/次数…）+ 画像摘要；每手结束直接把计数加进库，不读 jsonl。
对局明细不进库，在 players/<opp>/hands.jsonl（只追加：真机 session 模式每手 + 本工具从回放日志导入；含逐个动作的位置/注码），
只在 rebuild（或库结构升级）时全量读一遍重算。
这个工具用来查看对手库，以及把 logs/ 里的回放手牌记录（hands__*.jsonl）导入对手库。

用法：
    python3 tools/players.py list                         # 各对手手数、VPIP/收池率/PFR/3B/4B/cbet/AF/WTSD(%)、最近出现、画像（按最近出现倒序）
    python3 tools/players.py show little00                # 该对手跨桌统计 + 简述（就是 prompt 里的画像块）
    python3 tools/players.py import logs/2026-10-30/hands__*.jsonl [--opp little00]   # 导入（去重，只追加）
    python3 tools/players.py rebuild                      # 计数清零，从 players/*/hands.jsonl 全量重算（画像保留）
"""
import argparse, json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import session_prompt as P
import oppdb


def db():
    d = oppdb.open_db(P._hand_files(), P._profile_files())
    print(f"[oppdb] {d.path.relative_to(ROOT)}：对手 {d.counts()[0]} 人 / 累计 {d.counts()[1]} 手" + (f"（新库，从 jsonl 重建 {d.synced} 手）" if d.synced else "") + f"，{d.sync_ms:.0f}ms")
    return d


def cmd_list(a):
    d = db()
    rows = d.rates()
    if not rows: print("对手库为空"); return
    prof = {r["key"]: r for r in d.rows()}
    f = lambda v: "-" if v is None else f"{v:g}"
    print(f"{'对手':18s} {'手数':>5s} {'收池':>5s} {'VPIP':>5s} {'PFR':>5s} {'3B':>5s} {'4B':>5s} {'cbet':>5s} {'AF':>5s} {'WTSD':>5s}  最近出现             画像")
    for r in rows:
        p = prof[r["key"]]
        print(f"{r['name']:18s} {r['hands']:5d} {f(r['won']):>5s} {f(r['vpip']):>5s} {f(r['pfr']):>5s} {f(r['b3']):>5s} {f(r['b4']):>5s} {f(r['cbet_flop']):>5s} "
              f"{f(r['af']):>5s} {f(r['wtsd']):>5s}  {r['last_seen'] or '-':19s}  {'有' + ('（' + p['ptype'] + '）' if p.get('ptype') else '') if p.get('profile') else '无'}")


def cmd_show(a):
    d = db()
    row = d.lookup(a.opp)
    name = row["name"] if row else a.opp
    S = row["stats"] if row and row["stats"] else P.opp_stats([])
    pop = d.pop(name)
    print(f"对手 {name}：跨桌 {S['n']} 手（群体参考 {pop['n'] if pop else 0} 手）")
    print("\n".join(P.profile_block(S, P.opp_desc(S, None, name, row and row["ptype"]), S["n"], pop)))
    if row and row.get("profile"): print("\n" + row["profile"])


def cmd_rebuild(a):
    t0 = __import__("time").time()
    d = oppdb.OppDB()
    n = d.rebuild(P._hand_files()); d.sync_profiles(P._profile_files())
    print(f"[oppdb] 重建：{n} 手 -> 对手 {d.counts()[0]} 人，{(__import__('time').time() - t0) * 1000:.0f}ms")


def cmd_import(a):
    seen = {P.hand_key(h) for h in P.load_hands(limit=0)}
    new, skip = {}, 0
    for f in a.files:
        for l in open(f, encoding="utf-8"):
            try: h = json.loads(l)
            except ValueError: continue
            opp = a.opp or h.get("opp")
            if not opp or not h.get("ev"): skip += 1; continue
            h = P.hand_detail(dict(h, opp=opp))
            h["src"] = str(Path(f).name)
            k = P.hand_key(h)
            if k in seen: skip += 1; continue
            seen.add(k); new.setdefault(opp, []).append(h)
    for opp, hs in new.items():
        d = P.PLAYERS / P.safe_name(opp); d.mkdir(parents=True, exist_ok=True)
        with open(d / "hands.jsonl", "a", encoding="utf-8") as fo:
            for h in hs: fo.write(json.dumps(h, ensure_ascii=False) + "\n")
        print(f"{opp}: 新增 {len(hs)} 手 -> {(d / 'hands.jsonl').relative_to(ROOT)}")
    print(f"跳过（重复/无行动线）{skip} 条")
    d = db()
    if not d.synced: d.add_hands([h for hs in new.values() for h in hs])      # 新导入的手直接加计数（新库已在打开时重建过）


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("list"); sp.add_parser("rebuild")
    s = sp.add_parser("show"); s.add_argument("opp")
    s = sp.add_parser("import"); s.add_argument("files", nargs="+"); s.add_argument("--opp", help="覆盖记录里的对手昵称")
    a = ap.parse_args()
    {"list": cmd_list, "show": cmd_show, "import": cmd_import, "rebuild": cmd_rebuild}[a.cmd](a)
