"""WePoker 操作脚本（基于本地 OCR 定位按钮）
用法:
  python3 tools/act.py look                 # 截图+解析，打印文字状态
  python3 tools/act.py fold|check|call|allin [--nv]
  python3 tools/act.py raise 3BB            # 翻牌前：2BB 3BB 4BB 5BB 6BB
  python3 tools/act.py raise 1/2            # 翻牌后：1/3 1/2 2/3 1 1.2（池）
  python3 tools/act.py raise                # 只点 加注/下注 按钮本身
  python3 tools/act.py tap X Y              # 原始坐标点击（调试）
--nv: 不做操作后的复核截图（更快）

2026-10-30 真机实测结论（房间 537835，1/2 盲）：
- 我的回合 = 出现 弃牌 + (让牌|跟注) + (下注|加注)；对手回合只剩灰色预选 "让或弃/让牌/跟注"。
- 弃牌≈(374,2728)  让牌/跟注≈(1066,2730)（跟注上方会显示金额，按钮 y 可能到 2756）  下注/加注≈(720,2620)
  —— 坐标仅作参考，脚本每次按 OCR 到的按钮文字定位。
- 额度预设 5 个固定槽位 (304,2407) (511,2305) (720,2291) (927,2302) (1133,2407)：
  翻牌前标签 "BB" = 2/3/4/5/6BB；翻牌后标签 "底池" = 1/3、1/2、2/3、1、1.2 池；面对下注时同一组预设即加注额度。
- 点预设 = 直接下注/加注，无需确认（实测 3BB、5BB、6BB、1/3、1/2、2/3 均一次生效）。
- 灰色预设（2026-10-30 回放真机帧 000019/000059/000130 确认）：预设金额 < 最小下注(2) 时该档变灰，
  OCR 读不到金额（amt=None），点它不生效 = 实际让牌（换街后底池不变）。判定看档位下方金额区
  (sx±45, sy+70..sy+135) 最亮像素 min(R,G,B)：可用 ≥240，灰档约 120-135，滑条展开整屏变暗 <20；
  阈值 200（state.preset_off）。raise 命令与机器人都跳过灰档；全部灰且金额 ≥ 筹码时走全下。
- 行动倒计时约 12-15s，超时系统自动让牌/弃牌。
- 全下（2026-10-30 真机实测生效，6d9d 翻牌前 198 全下）：
  * 主动全下：界面没有独立"全下"按钮。点 加注(720,2620) -> 弹出竖向滑条（滑块初始≈(720,2432)，
    原加注按钮变成"确定"≈(720,2613)）-> 把滑块拖到顶 swipe 720,2432 -> 720,1420 (600ms)，
    气泡显示 "ALL IN / 100%" -> 点 确定。头像出现 "All in" 即生效。
  * 跟对手全下（00:34 实测生效，7sTc 跟对手 12 全下，双方亮牌 J6 vs 7T 进入发牌）：
    对手头像出现 "All in"（OCR 文本 "Allin" y<1600），我方仍是普通 "N 跟注" 按钮，点跟注即 call all in。
    若我方筹码 ≤ 跟注额，该按钮会变成蓝色 "All in"(≈1066,2730)，同样点它（ALIAS allin 已覆盖）。
  * 拖滑条和点确定必须一条命令连续完成（停下来看图会超时被自动弃牌，实测踩过两次）。
"""
import json, os, subprocess, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import state as S
import logroll

ROOT = Path(__file__).resolve().parent.parent
SNAP = ROOT / "model_test" / "act"


def swipe(x0, y0, x1, y1, ms):
    subprocess.run([S.ADB, "-s", S.DEV, "shell", "input", "swipe", *map(str, map(int, (x0, y0, x1, y1, ms)))], check=True)


def tap(x, y):
    subprocess.run([S.ADB, "-s", S.DEV, "shell", "input", "tap", str(int(x)), str(int(y))], check=True)


def snap(tag):
    SNAP.mkdir(parents=True, exist_ok=True)
    p = SNAP / f"{time.strftime('%H%M%S')}_{tag}.raw"
    S.capture(str(p))
    return str(p), S.parse(str(p))


def brief(st):
    return {"screen": st["screen"], "hero": " ".join(st["hero"]), "board": " ".join(st["board"]),
            "buttons": st["buttons"], "presets": [(p["frac"], p["amt"]) for p in st["presets"]]}


def log(rec):
    with open(logroll.path("act.jsonl"), "a") as f: f.write(json.dumps(rec, ensure_ascii=False) + "\n")


ALIAS = {"fold": ["弃牌"], "check": ["让牌"], "call": ["跟注"], "allin": ["全下", "ALL IN", "All in"]}


def allin_seq(st):
    """返回执行描述；有 All in 按钮直接点，否则 加注->拖滑条到顶->确定。"""
    b = st["buttons"]
    k = next((k for k in ALIAS["allin"] if k in b), None)
    if k:
        tap(*b[k]); return f"{k}@{b[k]}"
    k = next((k for k in ("加注", "下注") if k in b), None)
    if not k: return None
    x, y = b[k]
    tap(x, y); time.sleep(0.6)
    swipe(x, 2432, x, 1420, 600)
    time.sleep(0.3)
    tap(x, y - 7)   # 确定 按钮与原加注按钮同位置
    return f"{k}->滑条到顶->确定@{(x, y - 7)}"


def main():
    a = [x for x in sys.argv[1:] if not x.startswith("--")]
    nv = "--nv" in sys.argv
    if not a: print(__doc__); return
    cmd = a[0]
    t0 = time.time()
    if cmd == "tap":
        tap(a[1], a[2]); print("tapped"); return
    path, st = snap("pre")
    if cmd == "look":
        print(S.to_text(st)); print(f"[{time.time() - t0:.2f}s] {path}"); return
    b = st["buttons"]
    target = None
    if cmd == "allin":
        d = allin_seq(st)
        target = ("allin", d) if d else None
        if d:
            rec = {"t": time.strftime("%H:%M:%S"), "cmd": a, "do": d, "pre": brief(st), "t_act": round(time.time() - t0, 2)}
            if not nv:
                time.sleep(0.5); p2, st2 = snap("post")
                rec["post"] = brief(st2); rec["post_raw"] = p2
                rec["effect"] = "生效" if st2["screen"] != "my_turn" else "仍在我的回合"
            rec["t_total"] = round(time.time() - t0, 2); log(rec)
            print(json.dumps(rec, ensure_ascii=False)); return
    elif cmd in ALIAS:
        k = next((k for k in ALIAS[cmd] if k in b), None)
        if k: target = (k, b[k])
    elif cmd == "raise":
        want = a[1].replace(" ", "") if len(a) > 1 else ""
        p = next((p for p in st["presets"] if not p.get("off") and (p["frac"].replace(" ", "") == want or (p["amt"] or "") == want)), None)
        if p: target = (f"预设{p['frac']}({p['amt']})", p["xy"])
        elif want in ("", "btn"):
            k = next((k for k in ("加注", "下注") if k in b), None)
            if k: target = (k, b[k])
    if not target:
        print(json.dumps({"ok": False, "why": f"找不到 {a}", **brief(st)}, ensure_ascii=False)); return
    tap(*target[1])
    rec = {"t": time.strftime("%H:%M:%S"), "cmd": a, "tap": target, "pre": brief(st), "t_act": round(time.time() - t0, 2)}
    if not nv:
        time.sleep(0.5)
        p2, st2 = snap("post")
        rec["post"] = brief(st2); rec["post_raw"] = p2
        rec["effect"] = "生效(行动按钮已消失)" if st2["screen"] != "my_turn" else "仍在我的回合(可能需要确认/再点)"
    rec["t_total"] = round(time.time() - t0, 2)
    log(rec)
    print(json.dumps(rec, ensure_ascii=False))


if __name__ == "__main__":
    main()
