"""后台监视：持续截图解析，界面/牌/按钮变化时保存原始截图和文字状态。
python3 tools/watch.py [秒数]
"""
import json, shutil, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import state as S

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "model_test" / "live"
OUT.mkdir(parents=True, exist_ok=True)
dur = float(sys.argv[1]) if len(sys.argv) > 1 else 1200
t0, prev, n = time.time(), None, 0
log = open(OUT / "watch.log", "a")
while time.time() - t0 < dur:
    ts = time.time()
    src = S.capture("/tmp/watch.raw")
    st = S.parse(src)
    key = (st["screen"], tuple(st["hero"]), tuple(st["board"]), tuple(sorted(st["buttons"])),
           tuple(p["amt"] or "" for p in st["presets"]))
    if key != prev:
        n += 1
        name = f"{time.strftime('%H%M%S')}_{n:03d}_{st['screen']}"
        shutil.copy(src, OUT / f"{name}.raw")
        txt = S.to_text(st)
        log.write(f"==== {name}  ({time.time() - ts:.2f}s)\n{txt}\n"); log.flush()
        print(f"{name} hero={' '.join(st['hero'])} board={' '.join(st['board'])} btn={list(st['buttons'])}", flush=True)
        prev = key
    time.sleep(0.3)
