"""离线生成 169 种起手牌的单挑胜率（对随机手牌）与排名百分位 -> tools/preflop_rank.json
用法: python3 tools/make_preflop_rank.py [N=4000]   （纯标准库，约 1~2 分钟）"""
import itertools, json, random, sys
from collections import Counter
from pathlib import Path

R = "23456789TJQKA"


def score5(cs):
    v = sorted((c // 4 for c in cs), reverse=True)
    fl = len({c % 4 for c in cs}) == 1
    u = sorted(set(v), reverse=True)
    st = None
    if len(u) == 5 and u[0] - u[4] == 4: st = u[0]
    elif u == [12, 3, 2, 1, 0]: st = 3
    cnt = sorted(Counter(v).items(), key=lambda x: (-x[1], -x[0]))
    shape, ks = [c for _, c in cnt], [r for r, _ in cnt]
    if st is not None and fl: return (8, st)
    if shape[0] == 4: return (7, *ks)
    if shape[:2] == [3, 2]: return (6, *ks)
    if fl: return (5, *v)
    if st is not None: return (4, st)
    if shape[0] == 3: return (3, *ks)
    if shape[:2] == [2, 2]: return (2, *ks)
    if shape[0] == 2: return (1, *ks)
    return (0, *v)


def best7(cs): return max(score5(c) for c in itertools.combinations(cs, 5))


def hands169():
    for i in range(12, -1, -1):
        for j in range(i, -1, -1):
            if i == j: yield R[i] * 2, (i * 4, j * 4 + 1)
            else:
                yield R[i] + R[j] + "s", (i * 4, j * 4)
                yield R[i] + R[j] + "o", (i * 4, j * 4 + 1)


def main(n):
    rng = random.Random(1)
    deck = list(range(52))
    eq, combos = {}, {}
    for name, h in hands169():
        rest = [c for c in deck if c not in h]
        w = 0.0
        for _ in range(n):
            s = rng.sample(rest, 7)
            a, b = best7(list(h) + s[2:]), best7(s[:2] + s[2:])
            w += 1 if a > b else 0.5 if a == b else 0
        eq[name] = w / n
        combos[name] = 6 if len(name) == 2 else 4 if name[2] == "s" else 12
        print(name, f"{eq[name]:.3f}", file=sys.stderr)
    order = sorted(eq, key=lambda k: -eq[k])
    out, acc = {}, 0
    for k in order:
        acc += combos[k]
        out[k] = {"eq": round(eq[k] * 100, 1), "top": round(acc / 1326 * 100, 1)}
    Path(__file__).with_name("preflop_rank.json").write_text(json.dumps(out, ensure_ascii=False, indent=0))


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 4000)
