"""对手库（SQLite）：每个对手一行——各项统计计数 + 画像摘要；识别到对手后按主键直接查库（约 0.06ms，不做热加载）做热/冷启动。

对局明细不进库，仍记在 jsonl（只追加）：tables/<桌>/bot_hands.jsonl（真机每手）、players/<opp>/hands.jsonl（tools/players.py 导入的回放手）。
库只存从这些明细累计出来的结果：
  players  每个对手一行：key(主键，state.name_key(昵称)，容忍 OCR 的 0/O、l/1) / name / first_seen / last_seen /
           统计计数列（session_prompt.STAT_FIELDS，n=累计手数；“X_opp”=机会数）/ profile(画像正文) / ptype(画像类型) / profile_mtime
  player_rates 视图：把计数换算成百分比（VPIP、PFR、3B、4B、cbet…），直接 SELECT 查看
  sources(path PK, offset) —— 每个 jsonl 已累计到的字节偏移：只读新追加的完整行，避免重扫 / 重复累计
  idx_players_last(last_seen) —— 按最近出现排序列对手
群体参考 = SUM(全体) − 该对手（计数可加减；合计第一次查询时 SUM 一次，之后随写入增量维护）。
真机每手结束：session 后台写线程先把这一手追加到 bot_hands.jsonl，再 sync 该文件（只读刚追加的那一行）。
表结构改了就升 VERSION：打开旧库时清空重建，再从 jsonl 全量累计（明细都在 jsonl 里，不丢数据）。
"""
import json, sqlite3, threading, time
from pathlib import Path

import state as wpstate

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "players" / "opponents.db"
VERSION = 2

# player_rates 视图：列名 -> (分子, 分母)
RATES = (
    ("vpip", "vp", "vp_n"), ("pfr", "pfr", "vp_n"), ("open", "open", "btn"), ("limp", "limp", "btn"),
    ("b3", "b3", "vs_open"), ("b4", "b4", "f3_opp"), ("bb_fold_vs_open", "bb_fold", "vs_open"),
    ("fold_to_3b", "f3_fold", "f3_opp"), ("fold_to_4b", "f4_fold", "f4_opp"), ("iso_vs_limp", "iso", "vs_limp"),
    ("cbet_flop", "cb", "cb_opp"), ("cbet_turn", "cb2", "cb2_opp"), ("fold_to_cbet", "fcb", "fcb_opp"),
    ("raise_cbet", "rcb", "fcb_opp"), ("check_raise", "xr", "xr_opp"), ("wtsd", "wtsd", "flop"), ("wsd", "wsd", "wtsd"),
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
        self._schema()
        self.synced, self.sync_ms = 0, None
        self._sum = None                      # 全体计数合计：第一次 pop() 时 SUM 一次，之后随写入增量维护（只这一组数，不是热加载）

    def _schema(self):
        if self.conn.execute("PRAGMA user_version").fetchone()[0] != VERSION:        # 旧结构：清空，之后 sync 从 jsonl 重建
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
CREATE TABLE IF NOT EXISTS sources(path TEXT PRIMARY KEY, offset INTEGER NOT NULL DEFAULT 0);
CREATE VIEW IF NOT EXISTS player_rates AS SELECT key, name, n AS hands, last_seen,
  {rates},
  ROUND(1.0 * agg / NULLIF(calls, 0), 2) AS af
FROM players;
PRAGMA user_version = {VERSION};""")

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

    def _put(self, d):
        if self._sum is not None:
            old = self._row(d["key"])
            self._sum = add_stats(sub_stats(self._sum, old["stats"]) if old else self._sum, d["stats"] or {})
        vals = dict(d, **{k: (d["stats"] or {}).get(k, 0) for k in self.keys})
        self.conn.execute(f"INSERT OR REPLACE INTO players({','.join(self.cols)}) VALUES({','.join('?' * len(self.cols))})",
                          tuple(vals[c] for c in self.cols))

    def _new(self, k, name):
        return {"key": k, "name": name, "first_seen": None, "last_seen": None, "stats": None,
                "profile": None, "ptype": None, "profile_mtime": 0}

    # ---- 写 ----
    def add_hands(self, hands):
        """把一批手（jsonl 记录）累计进各对手的行；没有 opp 的跳过。返回累计的手数"""
        P, groups = _P(), {}
        for h in hands:
            if h.get("opp"): groups.setdefault(wpstate.name_key(h["opp"]), []).append(h)
        for k, hs in groups.items():
            d = self._row(k) or self._new(k, hs[-1]["opp"])
            ts = [h.get("t") for h in hs if h.get("t")]
            if ts:
                d["first_seen"] = min([d["first_seen"]] + ts) if d["first_seen"] else min(ts)
                d["last_seen"] = max([d["last_seen"] or ""] + ts)
            d["stats"] = add_stats(d["stats"], P.opp_stats(hs))
            self._put(d)
        return sum(map(len, groups.values()))

    def sync(self, files):
        """增量累计：每个 jsonl 只读上次位置之后新追加的完整行（文件变短视为重写，从头读）。偏移与统计在同一事务里提交。"""
        n = 0
        for f in map(Path, files):
            if not f.exists(): continue
            key, size = str(f.resolve()), f.stat().st_size
            with self.lock:
                r = self.conn.execute("SELECT offset FROM sources WHERE path=?", (key,)).fetchone()
                off = r[0] if r else 0
                if size < off: off = 0
                if size == off: continue
                with open(f, "rb") as fh:
                    fh.seek(off); data = fh.read()
                end = data.rfind(b"\n") + 1               # 最后一行可能还在写，只取到最后一个换行
                if not end: continue
                rows = []
                for l in data[:end].decode("utf-8", "replace").splitlines():
                    try: rows.append(json.loads(l))
                    except ValueError: pass
                self.conn.execute("BEGIN")
                try:
                    n += self.add_hands(rows)
                    self.conn.execute("INSERT INTO sources(path,offset) VALUES(?,?) ON CONFLICT(path) DO UPDATE SET offset=excluded.offset",
                                      (key, off + end))
                    self.conn.execute("COMMIT")
                except Exception:
                    self.conn.execute("ROLLBACK"); self._sum = None; raise
        return n

    def sync_profiles(self, files):
        """画像 md（players/<opp>.md、tables/<桌>/<opp>.md）→ 行里的 profile/ptype；同一对手取最新修改的文件，未变化不重读"""
        P, best = _P(), {}
        for f in map(Path, files):
            if f.name in ("table.md", "README.md") or f.name.startswith("_") or not f.exists(): continue
            k, m = wpstate.name_key(f.stem), f.stat().st_mtime
            if k and (k not in best or m > best[k][1]): best[k] = (f, m)
        n = 0
        with self.lock:
            for k, (f, m) in best.items():
                d = self._row(k) or self._new(k, f.stem)
                if m <= d["profile_mtime"]: continue
                t = f.read_text(encoding="utf-8")
                d.update(profile=P.profile_text(t), ptype=P.profile_type(t), profile_mtime=m)
                self._put(d); n += 1
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
    """启动用：打开库 → 增量累计 jsonl 新增行、刷新画像（不做热加载，之后按需查库）"""
    t0 = time.time()
    db = OppDB(path)
    db.synced = db.sync(hand_files)
    db.sync_profiles(profile_files)
    db.sync_ms = (time.time() - t0) * 1000
    return db
