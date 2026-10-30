"""对手数据：全局对手库 players/opponents.db（SQLite，tools/oppdb.py）+ 每桌目录 tables/<桌>/。

- 对手库：每个对手一行，各项统计的计数器（手数、入池、收池、3B 机会/次数…）+ 最近一份画像摘要（取各桌 <opp>.md 里最新修改的）；
  每手结束直接把计数加进库，不读 jsonl。
- 桌目录 tables/<桌>/：table.md（桌信息）、hands.jsonl（本桌对局流水，只追加，每手一行，hid 唯一，含逐个动作的位置/注码）、
  msgs.jsonl（本桌模型对话，按 hid 关联；流水里 msgs 字段记文件路径）、<opp>.md（该对手在本桌的画像，ProfileUpdater 自动维护）。
  对局明细不进库，只在 rebuild（或库结构升级）时全量读一遍各桌 hands.jsonl 重算。

用法：
    python3 tools/players.py list                         # 各对手手数、收池/VPIP/PFR/3B/4B/cbet/AF/WTSD(%)、最近出现、画像（按最近出现倒序）
    python3 tools/players.py show little00                # 该对手跨桌统计 + 简述（就是 prompt 里的画像块）+ 最近一份画像
    python3 tools/players.py hands little00 [-n 20] [--table <桌>]   # 该对手最近几手（hid、桌、位置、牌、盈亏、行动线）
    python3 tools/players.py hand <hid>                   # 一手的逐动作明细 + 当时的模型对话（按 hid 关联）
    python3 tools/players.py import logs/2026-10-30/hands__*.jsonl --table 20261029_wepoker_practice [--opp little00]
                                                          # 回放手导入该桌流水（全局去重，只追加）
    python3 tools/players.py rebuild                      # 计数清零，从 tables/*/hands.jsonl 全量重算（画像保留）
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


def iter_hands(table=None):
    for f in P._hand_files():
        if table and f.parent.name != table: continue
        for l in f.read_text(encoding="utf-8").splitlines():
            try: yield json.loads(l)
            except ValueError: pass


def cmd_hands(a):
    hs = sorted((h for h in iter_hands(a.table) if P.same_opp(h.get("opp"), a.opp)), key=lambda h: h.get("t") or "")[-a.n:]
    if not hs: print(f"流水里没有 {a.opp} 的手"); return
    for h in hs:
        d = h.get("delta")
        line = " ".join(f"{x['pos']}{x['act']}" + (f" {x['add']:g}" if x["add"] else "") for x in h.get("acts") or [])
        print(f"{h.get('hid') or '-':28s} {h.get('t') or '-':19s} {h.get('table') or '-':26s} 我方{h.get('pos') or '?':3s} {' '.join(h.get('hero') or []):6s} "
              f"[{' '.join(h.get('board') or [])}]  {'?' if d is None else f'{d / 2:+g}'}BB  {line}")


def cmd_hand(a):
    h = next((h for h in iter_hands() if h.get("hid") == a.hid), None)
    if not h: print(f"流水里没有 {a.hid}"); return
    print(f"{h['hid']}  {h.get('t')}  桌 {h.get('table') or '-'}  对手 {h.get('opp') or '未识别'}  我方 {h.get('pos')} {' '.join(h.get('hero') or [])}"
          f"  公共牌 [{' '.join(h.get('board') or [])}]  {h.get('how')}  我方 {'?' if h.get('delta') is None else f'{h['delta'] / 2:+g}'}BB"
          + (f"  起始 {h['start_bb']}" if h.get("start_bb") else "") + (f"  来源 {h['src']}" if h.get("src") else ""))
    print(f"{'街':8s} {'谁':4s} {'位置':4s} {'动作':6s} {'投入':>5s} {'本街到':>6s} {'底池':>5s} {'剩余':>6s}")
    for x in h.get("acts") or []:
        f = lambda v: "-" if v is None else f"{v:g}"
        print(f"{x['st']:8s} {x['who']:4s} {x['pos']:4s} {x['act']:6s} {f(x['add']):>5s} {f(x['to']):>6s} {f(x['pot']):>5s} {f(x['left']):>6s}")
    m = h.get("msgs")
    if not m: print("\n（没有模型对话记录）"); return
    p = Path(m) if Path(m).is_absolute() else ROOT / m
    rec = None
    if p.exists():
        for l in p.read_text(encoding="utf-8").splitlines():
            if a.hid in l:
                r = json.loads(l)
                if r.get("hid") == a.hid: rec = r; break
    if not rec: print(f"\n模型对话 {m} 已清理或缺失"); return
    print(f"\n模型对话（{m}，{len(rec['msgs']) // 2} 次决策）：")
    for x in rec["msgs"]:
        print(("【牌桌】" if x["role"] == "user" else "【模型】") + "\n" + x["content"] + "\n")


def cmd_import(a):
    seen = {P.hand_key(h) for h in P.load_hands(limit=0)}
    new, skip = [], 0
    for f in a.files:
        for i, l in enumerate(open(f, encoding="utf-8")):
            try: h = json.loads(l)
            except ValueError: continue
            opp = a.opp or h.get("opp")
            if not opp or not h.get("ev"): skip += 1; continue
            k = P.hand_key(h)
            if k in seen: skip += 1; continue
            seen.add(k)
            h = P.hand_detail(dict(h, opp=opp, table=a.table, src=h.get("src") or Path(f).name,
                                   hid=h.get("hid") or f"imp-{Path(f).stem}-{i + 1:04d}"), msgs=h.get("msgs") if isinstance(h.get("msgs"), str) else None)
            new.append(h)
    if new:
        import bot_core
        out = bot_core.new_table(P.TABLES / a.table) / "hands.jsonl"
        with open(out, "a", encoding="utf-8") as fo:
            for h in new: fo.write(json.dumps(h, ensure_ascii=False) + "\n")
        print(f"新增 {len(new)} 手 -> {out.relative_to(ROOT)}")
    print(f"跳过（重复/无行动线/无对手）{skip} 条")
    d = db()
    if not d.synced: d.add_hands(new)      # 新导入的手直接加计数（新库已在打开时重建过）


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("list"); sp.add_parser("rebuild")
    s = sp.add_parser("hands"); s.add_argument("opp"); s.add_argument("-n", type=int, default=20); s.add_argument("--table", help="只看这张桌")
    s = sp.add_parser("hand"); s.add_argument("hid")
    s = sp.add_parser("show"); s.add_argument("opp")
    s = sp.add_parser("import"); s.add_argument("files", nargs="+"); s.add_argument("--table", required=True, help="导入到哪张桌（tables/<桌>/hands.jsonl，不存在就从模板建）")
    s.add_argument("--opp", help="覆盖记录里的对手昵称")
    a = ap.parse_args()
    {"list": cmd_list, "show": cmd_show, "hands": cmd_hands, "hand": cmd_hand, "import": cmd_import, "rebuild": cmd_rebuild}[a.cmd](a)
