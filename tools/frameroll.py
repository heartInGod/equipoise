"""model_test/live 真机帧滚动清理：每帧 .raw 约 17.6MB，不清理会无限增长。

规则：
- 受保护（永不删）：tests/golden_frames.json 里的帧 + tests/*.py 里按名字引用的帧（如 005212_my_turn）。
- 其余 .raw 按修改时间从旧到新删，直到“未保护帧”总大小 ≤ 上限。
- 上限默认 30GB，环境变量 EQUIPOISE_LIVE_KEEP_GB 覆盖；设为 0 表示不清理。
- 只动 .raw；watch.log 等其他文件不碰。

用法：
    python3 tools/frameroll.py            # 只预览：会删哪些、删后多大
    python3 tools/frameroll.py --apply    # 真删
    python3 tools/frameroll.py --keep-gb 2 --apply
run_live 启动时自动调用 cleanup()（真删，按默认上限）。
"""
import argparse, json, os, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIVE = ROOT / "model_test" / "live"
KEEP_GB = float(os.environ.get("EQUIPOISE_LIVE_KEEP_GB", "30"))
_REF = re.compile(r"\b(\d{6}(?:_\d{3})?_[a-z_]+?)(?:\.raw)?\b")


def protected(root=ROOT):
    """golden + 测试引用的帧文件名（不含目录）"""
    names = set()
    gj = root / "tests" / "golden_frames.json"
    if gj.exists():
        names |= {Path(k).name for k in json.load(open(gj))}
    for py in (root / "tests").glob("*.py"):
        for m in _REF.finditer(py.read_text(encoding="utf-8")):
            names.add(m.group(1) + ".raw")
    return names


def plan(live=LIVE, keep_gb=KEEP_GB, root=ROOT):
    """返回 (要删的 Path 列表, 删前未保护字节数, 删后未保护字节数, 受保护帧数)"""
    if keep_gb <= 0 or not Path(live).is_dir():
        return [], 0, 0, 0
    prot = protected(root)
    frames = [(p.stat().st_mtime, p.name, p) for p in Path(live).glob("*.raw")]
    free = sorted((f for f in frames if f[1] not in prot), key=lambda f: (f[0], f[1]))
    sizes = {f[2]: f[2].stat().st_size for f in free}
    total = sum(sizes.values()); cap = keep_gb * 1024 ** 3
    out, left = [], total
    for _, _, p in free:
        if left <= cap: break
        out.append(p); left -= sizes[p]
    return out, total, left, len(frames) - len(free)


def cleanup(live=LIVE, keep_gb=KEEP_GB, apply=True, root=ROOT):
    dels, before, after, n_prot = plan(live, keep_gb, root)
    if apply:
        for p in dels:
            try: p.unlink()
            except OSError as e: print(f"[frameroll] 删除 {p} 失败: {e}", file=sys.stderr)
    return dels, before, after, n_prot


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep-gb", type=float, default=KEEP_GB)
    ap.add_argument("--apply", action="store_true", help="真删（默认只预览）")
    ap.add_argument("--dir", default=str(LIVE))
    a = ap.parse_args()
    dels, before, after, n_prot = cleanup(Path(a.dir), a.keep_gb, a.apply)
    G = 1024 ** 3
    print(f"受保护帧 {n_prot} 张；未保护 {before / G:.2f}GB → {after / G:.2f}GB（上限 {a.keep_gb}GB），"
          f"{'已删' if a.apply else '将删'} {len(dels)} 张")
    for p in dels[:5]: print("  ", p.name)
    if len(dels) > 5: print(f"   ... 共 {len(dels)} 张")
