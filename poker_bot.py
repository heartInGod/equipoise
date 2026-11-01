#!/usr/bin/env python3
"""WePoker 直连模型操作脚本：截图 -> 模型（直连 API，单图）-> 解析 JSON -> adb 点击。

用法（机器人主模式 tlive 的核心逻辑在 bot_core.py）：
  python3 poker_bot.py probe  --model mafia-anthropic/claude-sonnet-5-5 [--img model_test/state1.jpg]
      只调模型不点手机，测连通 / 延迟 / 读图（初筛用）
  python3 poker_bot.py live   --model <p/m> [--steps 6] [--max-sec 120] [--dry]
      真机：循环 截图->决策->点击，每步打印耗时，日志写 logs/<YYYY-MM-DD>/
  python3 poker_bot.py list   列出配置里的所有模型

只用标准库（本机无 httpx / PyYAML）。密钥从 ~/.minimax/config.yaml 读取，不打印。
"""
import argparse, base64, json, os, re, subprocess, sys, time, urllib.request, urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CFG = Path(os.environ.get("MAVIS_CONFIG", "/Users/fox/.minimax/config.yaml"))
DEV = os.environ.get("ADB_DEVICE", "59a4a919")
ADB = "/opt/homebrew/bin/adb"
IMG_W, IMG_H, PHONE_W, PHONE_H = 540, 1200, 1440, 3200
SCALE = PHONE_W / IMG_W  # 2.6667

# ---------------- 配置解析（极简，只取需要的字段） ----------------
def load_providers():
    lines = CFG.read_text().splitlines()
    provs, cur, sect, curmodel = {}, None, None, None
    for l in lines:
        if l.startswith("provider:") or l.startswith("custom_provider:"):
            sect = "provider"; continue
        if sect != "provider":
            continue
        if re.match(r"^\S", l):
            sect = None; continue
        m = re.match(r"^  ([A-Za-z0-9_.-]+):\s*$", l)
        if m:
            cur = m.group(1); provs[cur] = {"models": [], "headers": {}}; curmodel = None; continue
        if cur is None:
            continue
        p = provs[cur]
        for key in ("npm", "api"):
            m = re.match(rf"^    {key}:\s*\"?([^\"]+)\"?\s*$", l)
            if m: p[key] = m.group(1)
        m = re.match(r"^      (apiKey|baseURL):\s*(\S+)", l)
        if m: p[m.group(1)] = m.group(2).strip('"')
        m = re.match(r"^        ([A-Za-z0-9-]+):\s*(\S+)", l)
        if m and p.get("_in_headers"): p["headers"][m.group(1)] = m.group(2).strip('"')
        if re.match(r"^      headers:\s*$", l): p["_in_headers"] = True
        elif re.match(r"^      \S", l) or re.match(r"^    \S", l): p["_in_headers"] = False
        if re.match(r"^    models:\s*$", l): p["_in_models"] = True; continue
        if re.match(r"^    \S", l): p["_in_models"] = False
        if p.get("_in_models"):
            m = re.match(r"^      ([^ :#][^:]*):\s*$", l)
            if m: p["models"].append(m.group(1))
    return provs

def protocol(p):
    api, npm = p.get("api", ""), p.get("npm", "")
    if api == "openai-responses": return "responses"
    if api == "anthropic-messages" or "anthropic" in npm: return "anthropic"
    if "google" in npm: return "gemini"
    return "chat"

# ---------------- 模型调用 ----------------
def http_post(url, headers, body, timeout):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode(errors='replace')[:300]}")

def call_model(ref, prompt, jpg_bytes, timeout=60, max_tokens=400):
    prov_name, model = ref.split("/", 1)
    p = load_providers()[prov_name]
    proto, base, key = protocol(p), p["baseURL"].rstrip("/"), p["apiKey"]
    b64 = base64.b64encode(jpg_bytes).decode() if jpg_bytes else None
    h = {"Content-Type": "application/json", **p["headers"]}
    if proto == "anthropic":
        h.update({"x-api-key": key, "Authorization": f"Bearer {key}", "anthropic-version": "2023-06-01"})
        body = {"model": model, "max_tokens": max_tokens, "temperature": 0,
                "messages": [{"role": "user", "content": ([
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}}] if b64 else []) + [
                    {"type": "text", "text": prompt}]}]}
        body["thinking"] = {"type": "disabled"}
        try:
            d = http_post(f"{base}/messages", h, body, timeout)
        except RuntimeError as e:
            if "thinking" not in str(e) and "HTTP 400" not in str(e): raise
            body.pop("thinking"); body["max_tokens"] = 2000; d = http_post(f"{base}/messages", h, body, timeout)
        txt = "".join(c.get("text", "") for c in d.get("content", []) if c.get("type") == "text")
        if not txt.strip() and d.get("stop_reason") == "max_tokens":  # 仍在思考里耗尽，放宽重试一次
            body.pop("thinking", None); body["max_tokens"] = 2000; d = http_post(f"{base}/messages", h, body, timeout)
            txt = "".join(c.get("text", "") for c in d.get("content", []) if c.get("type") == "text")
        return txt
    if proto == "responses":
        h["Authorization"] = f"Bearer {key}"
        body = {"model": model, "max_output_tokens": max_tokens, "reasoning": {"effort": "low"},
                "input": [{"role": "user", "content": ([
                    {"type": "input_image", "image_url": f"data:image/jpeg;base64,{b64}"}] if b64 else []) + [
                    {"type": "input_text", "text": prompt}]}]}
        d = None
        for eff in ("none", "low", None):
            if eff: body["reasoning"] = {"effort": eff}
            else: body.pop("reasoning", None); body["max_output_tokens"] = 2000
            try:
                d = http_post(f"{base}/responses", h, body, timeout); break
            except RuntimeError as e:
                if "HTTP 400" not in str(e) or eff is None: raise
        if d.get("output_text"): return d["output_text"]
        return "".join(c.get("text", "") for o in d.get("output", []) for c in (o.get("content") or []) if c.get("type") in ("output_text", "text"))
    if proto == "gemini":
        h["x-goog-api-key"] = key
        body = {"contents": [{"role": "user", "parts": [
                    {"inline_data": {"mime_type": "image/jpeg", "data": b64}}, {"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": 2000, "temperature": 0}}
        d = http_post(f"{base}/models/{model}:generateContent", h, body, timeout)
        return "".join(pt.get("text", "") for c in d.get("candidates", []) for pt in c.get("content", {}).get("parts", []))
    h["Authorization"] = f"Bearer {key}"
    body = {"model": model, "max_tokens": max_tokens, "temperature": 0,
            "messages": [{"role": "user", "content": ([
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}] if b64 else []) + [
                {"type": "text", "text": prompt}]}]}
    d = http_post(f"{base}/chat/completions", h, body, timeout)
    return d["choices"][0]["message"].get("content") or ""

def parse_json(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m: raise ValueError(f"无 JSON: {text[:200]}")
    return json.loads(m.group(0))

# ---------------- 提示词 ----------------
PROMPT = """你在用 ADB 操作一台安卓手机玩 WePoker（无限注德州，盲注1/2，练习币）。图片是手机截图，已缩放到 540x1200。我方 Hero=乌贼69，固定在屏幕底部。目标：尽可能赢筹码。
已知界面（540x1200 坐标，按钮会随状态出现/消失，以图为准）：
- 等人：中间"邀请好友"按钮=没对手，等待；中间出现"开始游戏"按钮(270,580)=点它开局。
- 底部座位是"+"=我不在座，点(270,984)坐下。
- 我的回合：弃牌(140,1023) 下注/加注(270,984) 让牌或跟注(398,1023)；尺度预设 1/3池(114,894) 1/2池(192,858) 2/3池(270,855) 1池(348,858) 1.2池(426,894)。
- 只出现"让或弃""自动让牌"灰色按钮、或对手头像有倒计时 = 对手回合，等待。
- 弹窗/聊天框挡住时用 back。
牌力策略：单挑范围宽。翻前强牌/A高/对子/同花连张可加注或跟注，垃圾牌面对加注弃；翻后有顶对以上或强听牌(≥8 outs)下注/跟注，空气面对大注弃，没人下注就让牌。
只输出一行 JSON，不要其他文字：
{"screen":"lobby|waiting|need_sit|can_start|my_turn|opp_turn|popup|other","hero":"如 Ah Kd 或空","board":"如 Ts Jc 8s 或空","pot":数字或null,"op":"tap|wait|back","x":整数或null,"y":整数或null,"label":"点的是什么","why":"不超过20字"}"""

# ---------------- 设备 ----------------
def capture():
    t = time.time()
    png = subprocess.run([ADB, "-s", DEV, "exec-out", "screencap", "-p"], capture_output=True, check=True).stdout
    tmp_png, tmp_jpg = Path("/tmp/wp_bot.png"), Path("/tmp/wp_bot.jpg")   # 旧看图模式的单帧临时文件
    tmp_png.write_bytes(png)
    subprocess.run(["sips", "-s", "format", "jpeg", "-s", "formatOptions", "65", "--resampleWidth", str(IMG_W),
                    str(tmp_png), "--out", str(tmp_jpg)], capture_output=True, check=True)
    return tmp_jpg.read_bytes(), time.time() - t

def do(dec):
    if dec.get("op") == "tap" and dec.get("x") is not None:
        x, y = int(dec["x"] * SCALE), int(dec["y"] * SCALE)
        subprocess.run([ADB, "-s", DEV, "shell", "input", "tap", str(x), str(y)], check=True)
        return f"tap({x},{y})"
    if dec.get("op") == "back":
        subprocess.run([ADB, "-s", DEV, "shell", "input", "keyevent", "KEYCODE_BACK"], check=True)
        return "back"
    return "wait"

# ---------------- 命令 ----------------
def cmd_list(_):
    for name, p in load_providers().items():
        for m in p["models"]:
            print(f"{name}/{m}\t{protocol(p)}")

def cmd_probe(a):
    img = (ROOT / a.img).read_bytes()
    t = time.time()
    try:
        out = call_model(a.model, PROMPT, img, timeout=a.timeout)
        dt = time.time() - t
        try:
            dec = parse_json(out); ok = True
        except Exception as e:
            dec, ok = {"raw": out[:200], "err": str(e)}, False
        print(json.dumps({"model": a.model, "ok": ok, "sec": round(dt, 1), "dec": dec}, ensure_ascii=False))
    except Exception as e:
        print(json.dumps({"model": a.model, "ok": False, "sec": round(time.time() - t, 1), "error": str(e)[:300]}, ensure_ascii=False))

def cmd_live(a):
    import logroll
    logdir = logroll.day_dir()
    tag = a.model.replace("/", "__")
    log = open(logdir / f"{tag}.jsonl", "a")
    t0 = time.time()
    for i in range(a.steps):
        if time.time() - t0 > a.max_sec: break
        ts = time.time()
        img, t_cap = capture()
        (logdir / f"{tag}_{i}.jpg").write_bytes(img)
        try:
            tm = time.time(); out = call_model(a.model, PROMPT, img, timeout=a.timeout); t_llm = time.time() - tm
            dec = parse_json(out)
        except Exception as e:
            rec = {"step": i, "error": str(e)[:300], "t_cap": round(t_cap, 1), "total": round(time.time() - ts, 1)}
            print(json.dumps(rec, ensure_ascii=False), flush=True); log.write(json.dumps(rec, ensure_ascii=False) + "\n"); continue
        done = "dry" if a.dry else do(dec)
        rec = {"step": i, "screen": dec.get("screen"), "hero": dec.get("hero"), "board": dec.get("board"),
               "op": dec.get("op"), "label": dec.get("label"), "act": done, "why": dec.get("why"),
               "t_cap": round(t_cap, 1), "t_llm": round(t_llm, 1), "total": round(time.time() - ts, 1)}
        print(json.dumps(rec, ensure_ascii=False), flush=True); log.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if dec.get("op") == "wait": time.sleep(a.wait)
        else: time.sleep(0.6)
    log.close()

# ---------------- 文本模式：本地 OCR -> 文字 -> 模型 ----------------
sys.path.insert(0, str(ROOT / "tools"))
import state as wpstate
# 机器人核心逻辑在 bot_core.py（可注入 IO/模型，tests/ 直接测）；这里保留旧名字供 tools/replay_hand.py 等使用
from bot_core import TPROMPT, real_act, my_equity, rule_decide  # noqa: F401


def cmd_tprobe(a):
    st = wpstate.parse(str(ROOT / a.img))
    txt = wpstate.to_text(st)
    t = time.time()
    try:
        out = call_model(a.model, TPROMPT.format(state=txt), None, timeout=a.timeout, max_tokens=200)
        dec = parse_json(out); ok = True
    except Exception as e:
        dec, ok = {"err": str(e)[:200]}, False
    print(json.dumps({"model": a.model, "ok": ok, "sec": round(time.time() - t, 1), "dec": dec}, ensure_ascii=False), flush=True)


def cmd_tlive(a):
    import bot_core
    (bot_core.run_replay if a.replay else bot_core.run_live)(a)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("list")
    p1 = sp.add_parser("probe"); p1.add_argument("--model", required=True); p1.add_argument("--img", default="model_test/state1.jpg"); p1.add_argument("--timeout", type=float, default=60)
    p2 = sp.add_parser("live"); p2.add_argument("--model", required=True); p2.add_argument("--steps", type=int, default=6)
    p2.add_argument("--max-sec", type=float, default=120); p2.add_argument("--timeout", type=float, default=30)
    p2.add_argument("--wait", type=float, default=1.5); p2.add_argument("--dry", action="store_true")
    p3 = sp.add_parser("tprobe"); p3.add_argument("--model", required=True); p3.add_argument("--img", default="model_test/state1.raw"); p3.add_argument("--timeout", type=float, default=30)
    p4 = sp.add_parser("tlive"); p4.add_argument("--model", required=True); p4.add_argument("--max-sec", type=float, default=300)
    p4.add_argument("--budget", type=float, default=8.0, help="截图开始后多少秒内必须出决策，超时用本地规则")
    p4.add_argument("--hedge", type=float, default=4.5, help="模型多少秒没返回就发对冲请求（模型中位约 3.6s，3.0 时几乎每次都对冲、请求量翻倍）")
    p4.add_argument("--dry", action="store_true")
    p4.add_argument("--replay", help="离线回放目录(如 model_test/live)，不连手机"); p4.add_argument("--t0", default="000000"); p4.add_argument("--t1", default="999999")
    p4.add_argument("--rule", action="store_true", help="回放时不调模型，只用本地规则")
    p4.add_argument("--preflop-rule", action=argparse.BooleanOptionalAction, default=False, help="翻前不问模型，全部用本地规则（单挑写的规则，多人桌不适用；默认关：翻前也问模型，3bet/4bet/画像调整由模型做）")
    p4.add_argument("--trash-fold", action=argparse.BooleanOptionalAction, default=True, help="翻前无人入池、我方非大盲、明确的垃圾牌（非同花非对子，排名在该位置开池基线+25%%之外）直接弃，不问模型（默认开）")
    p4.add_argument("--prompt", choices=["short", "session"], default="session",
                    help="决策 prompt：session=开局长system(缓存)+对手画像+手内多轮短消息(默认) short=单帧精简 prompt(备用：调试/非 Anthropic 模型兜底/对照)")
    p4.add_argument("--table", default="tables/20261029_wepoker_practice", help="session 模式：桌目录（读 table.md / <opp>.md；真机写 hands.jsonl / msgs.jsonl；不存在就从模板建；传空串自动建 tables/YYYYMMDD_auto）")
    p4.add_argument("--opp", default=None, help="session 模式：对手昵称初值（可选；默认从截图识别，桌上换人自动切换）")
    p4.add_argument("--warm", action=argparse.BooleanOptionalAction, default=None,
                    help="session 模式：载入该对手跨桌历史手（热启动）+ 其他对手群体参考（冷启动）。默认真机开、回放关")
    p4.add_argument("--profile-every", type=int, default=10, help="session 真机模式：每 N 手异步更新一次对手画像，0=不更新")
    p4.add_argument("--seat-check", action=argparse.BooleanOptionalAction, default=True,
                    help="多人桌：入座变化时后台发截图让模型校验座位分组/D 位（一致记入 tools/layout_8max.json，不一致存截图）；--no-seat-check 关闭")
    p4.add_argument("--stuck", type=float, default=25, help="非我回合画面不变超过N秒视为未知界面并截图")
    p4.add_argument("--capture", choices=["stream", "screencap"], default="stream",
                    help="截图方式：stream=screenrecord 视频流+VideoToolbox 解码(默认，需先编译 tools/vstream；不可用时自动退回 screencap) screencap=每帧 adb screencap(~0.95s/帧)")
    a = ap.parse_args()
    {"list": cmd_list, "probe": cmd_probe, "live": cmd_live, "tprobe": cmd_tprobe, "tlive": cmd_tlive}[a.cmd](a)
