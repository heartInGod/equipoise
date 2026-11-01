"""对手库（SQLite）：每个对手一行——各项统计的计数器（分子、分母分开存）+ 画像摘要；识别到对手后按主键直接查库（不做热加载）。

  players  每个对手一行：key(主键，state.name_key(昵称)，容忍 OCR 的 0/O、l/1) / name / first_seen / last_seen /
           计数列（session_prompt.STAT_FIELDS：n 手数、vp_n/vp 入池机会/入池、won 收池、net 净赢、b3/vs_open、cb/cb_opp…；
           “X_opp”=机会数）/ profile(画像正文) / ptype(画像类型) / profile_mtime
  player_rates 视图：计数换算成百分比（VPIP、PFR、收池率、3B、4B、cbet…），直接 SELECT 查看
  idx_players_last(last_seen) —— 按最近出现排序列对手
每手结束（session 后台写线程）：add_hands([rec]) 把这一手算出的计数直接加到该对手那一行
（INSERT … ON CONFLICT DO UPDATE SET n=n+1, vp=vp+…），不读 jsonl、不扫全表。
对局明细不进库：各桌对局流水 tables/<桌>/hands.jsonl（只追加，每手一行：hid + 行动线 + 逐个动作的位置/类型/注码，见 session_prompt.hand_detail；
模型对话在 tables/<桌>/msgs.jsonl，按 hid 关联）。
jsonl 只在重建时读：表结构改了就升 VERSION，打开旧库（或库文件丢了）时清空，open_db 再从 jsonl 全量重算。
群体参考 = SUM(全体) − 该对手（计数可加减；合计第一次查询时 SUM 一次，之后随写入增量维护）。
"""
import json, sqlite3, threading, time
from pathlib import Path

import state as wpstate

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "players" / "opponents.db"
VERSION = 4                  # 4：加多人桌口径（按位置开池、多人 3bet/4bet/5bet、领打、河牌下注/加注）

# player_rates 视图：列名 -> (分子, 分母)
RATES = (
    ("won", "won", "n"), ("vp_won", "vp_won", "vp"), ("vpip", "vp", "vp_n"), ("pfr", "pfr", "vp_n"), ("open", "open", "btn"), ("limp", "limp", "btn"),
    ("b3", "b3", "vs_open"), ("b4", "b4", "f3_opp"), ("bb_fold_vs_open", "bb_fold", "vs_open"),
    ("fold_to_3b", "f3_fold", "f3_opp"), ("fold_to_4b", "f4_fold", "f4_opp"), ("iso_vs_limp", "iso", "vs_limp"),
    ("cbet_flop", "cb", "cb_opp"), ("cbet_turn", "cb2", "cb2_opp"), ("fold_to_cbet", "fcb", "fcb_opp"),
    ("raise_cbet", "rcb", "fcb_opp"), ("check_raise", "xr", "xr_opp"), ("wtsd", "wtsd", "flop"), ("wsd", "wsd", "wtsd"),
    ("rfi_ep", "rfi_ep", "rfi_ep_opp"), ("rfi_hj", "rfi_hj", "rfi_hj_opp"), ("rfi_co", "rfi_co", "rfi_co_opp"),
    ("rfi_btn", "rfi_bt", "rfi_bt_opp"), ("rfi_sb", "rfi_sb", "rfi_sb_opp"), ("mw_3b", "m3", "m3_opp"), ("mw_call_open", "mc", "m3_opp"),
    ("mw_fold_to_3b", "mf3", "mf3_opp"), ("mw_4b", "m4", "m4_opp"), ("mw_5b", "m5", "m5_opp"),
    ("donk", "ld", "ld_opp"), ("river_bet", "rv", "rv_opp"), ("river_raise", "rr", "rr_opp"),
)


def _P():
    import session_prompt as P          # 延迟导入：统计/画像解析逻辑和 prompt 共用一份
    return P


def add_stats(a, b):
    if not a: return dict(b) if b else None
    if not b: return dict(a)
    return {k: a.get(k, 0) + b.get(k, 0) for k in set(a) | set(b)}


def sub_stats(a, b):
    return {k: a[k] - (b or {}).get(k, 0) for k in a}


class OppDB:
    def __init__(self, path=DB):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.keys = _P().STAT_KEYS
        self.cols = ("key", "name", "first_seen", "last_seen") + self.keys + ("profile", "ptype", "profile_mtime")
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self.conn.execute("PRAGMA journal_mode=WAL"); self.conn.execute("PRAGMA synchronous=NORMAL")
        self.fresh = self._schema()           # True=新建/升级后的空库，open_db 会从 jsonl 重建
        self.synced, self.sync_ms = 0, None
        self._sum = None                      # 全体计数合计：第一次 pop() 时 SUM 一次，之后随写入增量维护（只这一组数，不是热加载）

    def _schema(self):
        fresh = self.conn.execute("PRAGMA user_version").fetchone()[0] != VERSION
        if fresh:                                                                     # 新库/旧结构：清空，之后从 jsonl 重建
            self.conn.executescript("DROP VIEW IF EXISTS player_rates; DROP TABLE IF EXISTS players; "
                                    "DROP TABLE IF EXISTS agg; DROP TABLE IF EXISTS sources;")
        desc = dict(_P().STAT_FIELDS)
        stat_cols = "\n  ".join(f"{k} INTEGER NOT NULL DEFAULT 0,  -- {desc[k]}" for k in self.keys)
        rates = ",\n  ".join(f"ROUND(100.0 * {a} / NULLIF({b}, 0), 1) AS {n}" for n, a, b in RATES)
        self.conn.executescript(f"""
CREATE TABLE IF NOT EXISTS players(
  key TEXT PRIMARY KEY,      -- state.name_key(昵称)
  name TEXT NOT NULL,        -- 显示名
  first_seen TEXT, last_seen TEXT,
  {stat_cols}
  profile TEXT,              -- 画像 md 正文（去掉更新日志）
  ptype TEXT,                -- 画像「类型：」一行
  profile_mtime REAL NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_players_last ON players(last_seen);
CREATE VIEW IF NOT EXISTS player_rates AS SELECT key, name, n AS hands, last_seen,
  {rates},
  ROUND(1.0 * agg / NULLIF(calls, 0), 2) AS af,
  ROUND(50.0 * net / NULLIF(n, 0), 1) AS bb100
FROM players;
PRAGMA user_version = {VERSION};""")
        return fresh

    def close(self):
        with self.lock: self.conn.close()

    # ---- 行 <-> dict ----
    def _to_dict(self, r):
        d = dict(zip(self.cols, r))
        d["stats"] = {k: d.pop(k) for k in self.keys}
        d["n_hands"] = d["stats"]["n"]
        return d

    def _row(self, k):
        r = self.conn.execute(f"SELECT {','.join(self.cols)} FROM players WHERE key=?", (k,)).fetchone()
        return self._to_dict(r) if r else None

    def _ensure(self, k, name):
        self.conn.execute("INSERT OR IGNORE INTO players(key, name) VALUES(?, ?)", (k, name))

    # ---- 写 ----
    def add_hands(self, hands):
        """把一批手（记录：opp/pos/ev/board/how/delta/t）的计数加到各对手那一行（一条 UPSERT，不读旧值）；没有 opp 的跳过。返回手数"""
        P, groups = _P(), {}
        for h in hands:
            if (h.get("np") or 2) > 2:                  # 多人桌：每个入座对手各算一份（opp_pos=他的位置）
                for pos, name in (h.get("seats") or {}).items():
                    if name and pos != h.get("pos"): groups.setdefault(wpstate.name_key(name), []).append(dict(h, opp=name, opp_pos=pos))
            elif h.get("opp"): groups.setdefault(wpstate.name_key(h["opp"]), []).append(h)
        if not groups: return 0
        cols = ("key", "name", "first_seen", "last_seen") + self.keys
        sql = (f"INSERT INTO players({','.join(cols)}) VALUES({','.join('?' * len(cols))}) ON CONFLICT(key) DO UPDATE SET "
               "name=excluded.name, "
               "first_seen=COALESCE(MIN(first_seen, excluded.first_seen), first_seen, excluded.first_seen), "
               "last_seen=COALESCE(MAX(last_seen, excluded.last_seen), last_seen, excluded.last_seen), "
               + ", ".join(f"{k}={k}+excluded.{k}" for k in self.keys))
        with self.lock:
            own = not self.conn.in_transaction
            if own: self.conn.execute("BEGIN")
            try:
                for k, hs in groups.items():
                    S, ts = P.opp_stats(hs), [h["t"] for h in hs if h.get("t")]
                    self.conn.execute(sql, (k, hs[-1]["opp"], min(ts) if ts else None, max(ts) if ts else None) + tuple(S[c] for c in self.keys))
                    if self._sum is not None: self._sum = add_stats(self._sum, S)
                if own: self.conn.execute("COMMIT")
            except Exception:
                if own: self.conn.execute("ROLLBACK")
                self._sum = None; raise
        return sum(map(len, groups.values()))

    def rebuild(self, files):
        """清空所有计数，从明细 jsonl 全量重算（jsonl 每行一手，导入时已去重）；画像列保留。返回累计的手数"""
        rows = []
        for f in map(Path, files):
            if not f.exists(): continue
            for l in f.read_text(encoding="utf-8").splitlines():
                try: rows.append(json.loads(l))
                except ValueError: pass
        rows.sort(key=lambda h: h.get("t") or "")
        with self.lock:
            self.conn.execute("BEGIN")
            try:
                self.conn.execute("UPDATE players SET first_seen=NULL, last_seen=NULL, " + ", ".join(f"{k}=0" for k in self.keys))
                self._sum = None
                n = self.add_hands(rows)
                self.conn.execute("DELETE FROM players WHERE n=0 AND profile IS NULL")
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK"); self._sum = None; raise
        self.fresh = False
        return n

    def sync_profiles(self, files):
        """各桌画像 tables/<桌>/<opp>.md → 行里的 profile/ptype（该对手最近一份画像，供没有本桌画像时作先验）；同一对手取最新修改的文件，未变化不重读"""
        P, best = _P(), {}
        for f in map(Path, files):
            if f.name in ("table.md", "README.md") or f.name.startswith("_") or not f.exists(): continue
            k, m = wpstate.name_key(f.stem), f.stat().st_mtime
            if k and (k not in best or m > best[k][1]): best[k] = (f, m)
        n = 0
        with self.lock:
            for k, (f, m) in best.items():
                r = self.conn.execute("SELECT profile_mtime FROM players WHERE key=?", (k,)).fetchone()
                if r and m <= r[0]: continue
                t = f.read_text(encoding="utf-8")
                self._ensure(k, f.stem)
                self.conn.execute("UPDATE players SET profile=?, ptype=?, profile_mtime=? WHERE key=?",
                                  (P.profile_text(t), P.profile_type(t), m, k))
                n += 1
        return n

    # ---- 读（直接查库，主键索引） ----
    def lookup(self, name):
        """昵称（可含 OCR 误差）→ 该对手行（stats 为各项计数）；没见过返回 None"""
        with self.lock: return self._row(wpstate.name_key(name))

    def pop(self, name=None):
        """群体参考 = 全体合计 − 该对手；没有其他对手的样本返回 None"""
        with self.lock:
            if self._sum is None:
                r = self.conn.execute(f"SELECT {','.join(f'COALESCE(SUM({k}),0)' for k in self.keys)} FROM players").fetchone()
                self._sum = dict(zip(self.keys, r))
            d = self._row(wpstate.name_key(name)) if name else None
            S = sub_stats(self._sum, d["stats"] if d else None)
        return S if S["n"] else None

    def rows(self):
        """所有对手，按最近出现倒序"""
        with self.lock:
            return [self._to_dict(r) for r in self.conn.execute(f"SELECT {','.join(self.cols)} FROM players ORDER BY last_seen DESC")]

    def rates(self, name=None):
        """player_rates 视图（百分比）；name 给定时只取该对手"""
        with self.lock:
            cur = self.conn.execute("SELECT * FROM player_rates" + (" WHERE key=?" if name else "")
                                    + " ORDER BY last_seen DESC", (wpstate.name_key(name),) if name else ())
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, r)) for r in cur]

    def counts(self):
        with self.lock:
            n, h = self.conn.execute("SELECT COUNT(*), COALESCE(SUM(n),0) FROM players").fetchone()
        return n, h

    def known_names(self):
        with self.lock: return [r[0] for r in self.conn.execute("SELECT name FROM players")]


def open_db(hand_files=(), profile_files=(), path=DB):
    """启动用：打开库（新库/结构升级时从明细 jsonl 重建，平时不读 jsonl）→ 刷新画像（不做热加载，之后按需查库）"""
    t0 = time.time()
    db = OppDB(path)
    db.synced = db.rebuild(hand_files) if db.fresh else 0
    db.sync_profiles(profile_files)
    db.sync_ms = (time.time() - t0) * 1000
    return db
