"""模型延迟基准：对比 每次新建连接(urllib) vs 长连接(http.client keep-alive)、完整 prompt vs 精简 prompt、max_tokens。
用法: python3 tools/bench_llm.py [N]"""
import json, sys, time, http.client, ssl, statistics as stt
from pathlib import Path
from urllib.parse import urlparse
ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tools")]
import poker_bot as PB, state as S

N = int(sys.argv[1]) if len(sys.argv) > 1 else 4
MODEL = "mafia-anthropic/claude-opus-5"
frames = ["005140", "005212", "005236", "004922"]
sts = []
for f in frames:
    p = ROOT / f"model_test/live/{f}_my_turn.raw"
    if p.exists(): sts.append(S.parse(str(p)))
h = S.HandHist()
full = [PB.TPROMPT.format(state=S.to_text(st, h)) for st in sts]
slim = [PB.TPROMPT.format(state=S.to_text(st, h, raw_texts=False)) for st in sts] if "raw_texts" in S.to_text.__code__.co_varnames else full
print("prompt 字符数 full", [len(x) for x in full], "slim", [len(x) for x in slim])

prov = PB.load_providers()["mafia-anthropic"]
u = urlparse(prov["baseURL"].rstrip("/") + "/messages")
key = prov["apiKey"]
H = {"Content-Type": "application/json", **prov["headers"], "x-api-key": key, "Authorization": f"Bearer {key}", "anthropic-version": "2023-06-01"}
conn = [None]


def ka_post(body, timeout=20):
    for _ in range(2):
        if conn[0] is None: conn[0] = http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=timeout, context=ssl.create_default_context())
        try:
            conn[0].request("POST", u.path, json.dumps(body), H); r = conn[0].getresponse(); d = r.read()
            return json.loads(d)
        except Exception:
            conn[0].close(); conn[0] = None
    raise RuntimeError("ka fail")


def body(prompt, mt): return {"model": "claude-opus-5", "max_tokens": mt, "temperature": 0, "thinking": {"type": "disabled"},
                               "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}]}]}


def run(name, fn):
    ts, outs = [], []
    for i in range(N):
        p = (full if "full" in name else slim)[i % len(full)]
        t = time.time(); o = fn(p); ts.append(time.time() - t); outs.append(o)
    print(f"{name:28s} mean {stt.mean(ts):.2f} p50 {sorted(ts)[len(ts)//2]:.2f} max {max(ts):.2f}  sample={str(outs[-1])[:70]}", flush=True)


run("urllib full mt200", lambda p: PB.call_model(MODEL, p, None, timeout=20, max_tokens=200))
ka_post(body("hi", 5))  # 预热连接
run("keepalive full mt200", lambda p: "".join(c.get("text", "") for c in ka_post(body(p, 200))["content"]))
run("keepalive slim mt200", lambda p: "".join(c.get("text", "") for c in ka_post(body(p, 200))["content"]))
run("keepalive slim mt60", lambda p: "".join(c.get("text", "") for c in ka_post(body(p, 60))["content"]))
d = ka_post(body(slim[0], 200)); print("usage", d.get("usage"))
