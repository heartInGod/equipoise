"""从真实截图生成/刷新 golden 期望（tests/golden_frames.json）。识别逻辑有意改动后：重跑本脚本 -> 人工检查 diff -> 提交。
用法: python3 tests/make_golden.py [--check]   (--check 只打印与现有 golden 的差异)"""
import json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import state as S

GOLD = ROOT / "tests" / "golden_frames.json"


def pick():
    live = sorted((ROOT / "model_test/live").glob("*.raw"))
    out = [p for p in live if not p.name.split("_", 1)[1].startswith(("my_turn", "other"))]   # 稀有界面全收
    mt = [p for p in live if "_my_turn" in p.name]; ot = [p for p in live if "_other" in p.name]
    out += mt[:: max(1, len(mt) // 30)] + ot[:: max(1, len(ot) // 15)]
    out += sorted((ROOT / "model_test").glob("*.raw")) + sorted((ROOT / "model_test/unknown").glob("*.raw"))
    out += sorted((ROOT / "model_test/user_shots").glob("*.raw"))
    return out


def summary(st):
    n = st.get("nums") or {}
    return {"screen": st["screen"], "hero": " ".join(st["hero"]), "board": " ".join(st["board"]),
            "buttons": sorted(st["buttons"]), "presets": [p["frac"] + "=" + str(p["amt"]) for p in st["presets"]],
            "pot": n.get("pot"), "to_call": n.get("to_call"), "my_stack": n.get("my_stack"), "opp_stack": n.get("opp_stack"),
            "dealer": None if not st.get("dealer") else ("me" if st["dealer"][1] > 1600 else "opp")}


if __name__ == "__main__":
    old = json.loads(GOLD.read_text()) if GOLD.exists() else {}
    new = {}
    for p in pick():
        rel = str(p.relative_to(ROOT))
        if p.stat().st_size < 1000: continue
        new[rel] = summary(S.parse(str(p)))
        if old.get(rel) and old[rel] != new[rel]: print("DIFF", rel, old[rel], "->", new[rel])
    if "--check" not in sys.argv:
        GOLD.write_text(json.dumps(new, ensure_ascii=False, indent=0))
        print(f"写入 {len(new)} 帧 -> {GOLD}")
