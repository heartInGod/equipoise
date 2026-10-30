"""对手库（SQLite）：每个对手一行——累计统计 + 画像摘要；启动时整表热加载进内存，识别到对手后 O(1) 取出做热/冷启动。

对局明细不进库，仍记在 jsonl（只追加）：tables/<桌>/bot_hands.jsonl（真机每手）、players/<opp>/hands.jsonl（tools/players.py 导入的回放手）。
库只存从这些明细累计出来的结果：
  players(key PK, name, first_seen, last_seen, n_hands, tables, stats, profile, ptype, profile_mtime)
    key   = state.name_key(昵称)，容忍 OCR 的 0/O、l/1 误差；主键即查找索引
    stats = session_prompt.opp_stats 的计数（JSON，全是可加的计数：库里累加，群体参考 = 全体合计 − 该对手）
    profile/ptype = players/<opp>.md（或桌内 <opp>.md，取较新）去掉更新日志的正文 / 「类型：」一行
  idx_players_last(last_seen)  —— 按最近出现排序列对手
  sources(path PK, offset)     —— 每个 jsonl 已累计到的字节偏移：只读新追加的完整行，避免重扫 / 重复累计
真机每手结束：session 先把这一手追加到 bot_hands.jsonl，再 sync 该文件（只读刚追加的那一行）。
"""
import json, sqlite3, threading, time
from pathlib import Path

import state as wpstate

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "players" / "opponents.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS players(
  key TEXT PRIMARY KEY, name TEXT NOT NULL, first_seen TEXT, last_seen TEXT,
  n_hands INTEGER NOT NULL DEFAULT 0, tables TEXT NOT NULL DEFAULT '', stats TEXT,
  profile TEXT, ptype TEXT, profile_mtime REAL NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_players_last ON players(last_seen);
CREATE TABLE IF NOT EXISTS sources(path TEXT PRIMARY KEY, offset INTEGER NOT NULL DEFAULT 0);
"""
COLS = ("key", "name", "first_seen", "last_seen", "n_hands", "tables", "stats", "profile", "ptype", "profile_mtime")


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
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self.conn.execute("PRAGMA journal_mode=WAL"); self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self.players, self.total = {}, None     # 内存：{key: 行 dict(stats 已解析)}、全体 stats 合计
        self.load_ms, self.synced = None, 0

    def close(self):
        with self.lock: self.conn.close()

    # ---- 写 ----
    def _row(self, k):
        r = self.conn.execute(f"SELECT {','.join(COLS)} FROM players WHERE key=?", (k,)).fetchone()
        if not r: return None
        d = dict(zip(COLS, r)); d["stats"] = json.loads(d["stats"]) if d["stats"] else None
        return d

    def _put(self, d):
        self.conn.execute(f"INSERT OR REPLACE INTO players({','.join(COLS)}) VALUES({','.join('?' * len(COLS))})",
                          tuple(json.dumps(d[c]) if c == "stats" and d[c] is not None else d[c] for c in COLS))
        old = self.players.get(d["key"])               # 同步内存（行 + 全体合计）
        if old and old["stats"] and self.total: self.total = sub_stats(self.total, old["stats"])
        self.total = add_stats(self.total, d["stats"])
        self.players[d["key"]] = dict(d)

    def _new(self, k, name):
        return {"key": k, "name": name, "first_seen": None, "last_seen": None, "n_hands": 0, "tables": "",
                "stats": None, "profile": None, "ptype": None, "profile_mtime": 0}

    def add_hands(self, hands, table_id=None):
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
            d["n_hands"] += len(hs)
            d["stats"] = add_stats(d["stats"], P.opp_stats(hs))
            if table_id and table_id not in d["tables"].split(","):
                d["tables"] = ",".join(filter(None, [d["tables"], table_id]))
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
                    n += self.add_hands(rows, f.parent.name if f.name == "bot_hands.jsonl" else None)
                    self.conn.execute("INSERT INTO sources(path,offset) VALUES(?,?) ON CONFLICT(path) DO UPDATE SET offset=excluded.offset",
                                      (key, off + end))
                    self.conn.execute("COMMIT")
                except Exception:
                    self.conn.execute("ROLLBACK"); self.load(); raise
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

    # ---- 读 ----
    def load(self):
        """热加载：整张 players 表进内存，并算好全体统计合计"""
        t0 = time.time()
        with self.lock:
            ps = {}
            for r in self.conn.execute(f"SELECT {','.join(COLS)} FROM players"):
                d = dict(zip(COLS, r)); d["stats"] = json.loads(d["stats"]) if d["stats"] else None
                ps[d["key"]] = d
            total = None
            for d in ps.values(): total = add_stats(total, d["stats"])
            self.players, self.total = ps, total
        self.load_ms = (time.time() - t0) * 1000
        return self

    def lookup(self, name):
        """昵称（可含 OCR 误差）→ 该对手行的副本（stats 为累计计数）；没见过返回 None"""
        with self.lock:
            d = self.players.get(wpstate.name_key(name))
            return dict(d, stats=dict(d["stats"]) if d["stats"] else None) if d else None

    def pop(self, name=None):
        """群体参考 = 全体合计 − 该对手；没有其他对手的样本返回 None"""
        with self.lock:
            if not self.total: return None
            d = self.players.get(wpstate.name_key(name)) if name else None
            S = sub_stats(self.total, d["stats"] if d else None)
        return S if S["n"] else None

    def counts(self):
        return len(self.players), sum(d["n_hands"] for d in self.players.values())

    def known_names(self):
        return [d["name"] for d in self.players.values()]


def open_db(hand_files=(), profile_files=(), path=DB):
    """启动用：打开库 → 增量累计 jsonl、刷新画像 → 整表热加载进内存"""
    db = OppDB(path).load()
    db.synced = db.sync(hand_files)
    db.sync_profiles(profile_files)
    return db.load()
