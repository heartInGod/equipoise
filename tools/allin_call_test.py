"""等对手全下后立即跟注(call all in)，成功一次即退出。其余回合：能让则让，否则小额跟注(<=6)，否则弃牌。"""
import sys, time, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import state as S, act as A
T_END = time.time() + float(sys.argv[1] if len(sys.argv) > 1 else 900)
while time.time() < T_END:
    p = "/tmp/ac.raw"; S.capture(p); st = S.parse(p)
    if st["screen"] != "my_turn": time.sleep(0.3); continue
    b = st["buttons"]
    opp_allin = any(t[0].lower().replace(" ","") == "allin" and t[2] < 1600 for t in st["texts"])
    k = next((k for k in ("All in", "ALL IN", "全下") if k in b), None)
    if opp_allin or k:
        key = k or ("跟注" if "跟注" in b else None)
        if key:
            A.tap(*b[key]); time.sleep(1.0)
            S.capture("/Users/fox/modelProject/Equipoise/model_test/callallin_post.raw")
            import shutil; shutil.copy(p, "/Users/fox/modelProject/Equipoise/model_test/callallin_pre.raw")
            print(json.dumps({"t": time.strftime("%H:%M:%S"), "hero": st["hero"], "tap": [key, b[key]], "opp_allin_text": opp_allin}, ensure_ascii=False), flush=True)
            break
    if "让牌" in b: A.tap(*b["让牌"]); print("check", st["hero"], flush=True)
    elif "跟注" in b: A.tap(*b["跟注"]); print("call", st["hero"], flush=True)
    else: A.tap(*b["弃牌"]); print("fold", st["hero"], flush=True)
    time.sleep(1.0)
