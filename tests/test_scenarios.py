"""机器人场景测试（不连手机、不调真实模型）。
用法:  python3 tests/test_scenarios.py            # 全部（约 1 分钟，含 1 个真实时间尺度的 deadline 用例）
       python3 tests/test_scenarios.py -k Latency # 只跑某类
       SKIP_GOLDEN=1 python3 tests/test_scenarios.py   # 跳过真实截图 golden（快）
场景清单见 tests/SCENARIOS.md。
"""
import json, os, sys, tempfile, threading, time, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tools")]
import bot_core as C
import state as S

FOLD, CALL, CHECK, RAISE = [374, 2728], [1066, 2756], [1066, 2730], [720, 2620]
BB_PRE = [{"frac": f, "amt": str(a), "xy": xy} for f, a, xy in zip(["2BB", "3BB", "4BB", "5BB", "6BB"], [4, 6, 8, 10, 12],
                                                                   [[304, 2407], [511, 2305], [720, 2291], [927, 2302], [1133, 2407]])]
POT_PRE = [{"frac": f, "amt": str(a), "xy": xy} for f, a, xy in zip(["1/3", "1/2", "2/3", "1", "1.2"], [4, 6, 8, 12, 14],
                                                                    [[304, 2407], [511, 2305], [720, 2291], [927, 2302], [1133, 2407]])]


def mk(screen="my_turn", hero=("Ah", "Kd"), board=(), buttons=None, presets=None, nums=None, texts=None, dealer=(720, 2000)):
    b = buttons if buttons is not None else {"弃牌": FOLD, "让牌": CHECK, "加注": RAISE}
    n = {"pot": 4, "pot_prev": 0, "opp_bet": 2, "my_bet": 2, "to_call": 0, "my_stack": 196, "opp_stack": 196}
    n.update(nums or {})
    return {"screen": screen, "hero": list(hero), "board": list(board), "buttons": dict(b), "presets": list(presets or []),
            "nums": n, "texts": list(texts or []), "dealer": list(dealer) if dealer else None, "ms": 0}


def fixed(dec_text, delay=0.0, err=None):
    """mock 模型：sleep delay 后返回 dec_text / 抛 err"""
    calls = []
    def raw(prompt, timeout=12, max_tokens=200):
        calls.append(time.time()); time.sleep(delay)
        if err: raise err
        return dec_text
    raw.calls = calls
    return raw


def seq_model(*specs):
    """第 i 次调用用 specs[i] = (delay, text|Exception)"""
    calls = []
    def raw(prompt, timeout=12, max_tokens=200):
        i = len(calls); calls.append(prompt)
        d, out = specs[min(i, len(specs) - 1)]
        time.sleep(d)
        if isinstance(out, Exception): raise out
        return out
    raw.calls = calls
    return raw


OK_CALL = '{"act":"跟注","size":null,"why":"ok"}'
OK_RAISE = '{"act":"加注","size":"3BB","why":"ok"}'


def bot(decider=None, **kw):
    io = C.RecIO()
    return C.Bot(io, decider, replay=True, stuck=kw.pop("stuck", 25), budget=kw.pop("budget", 8.0), **kw), io


# ---------------------------------------------------------------- 1. 真实截图 golden
@unittest.skipIf(os.environ.get("SKIP_GOLDEN"), "SKIP_GOLDEN")
class TestGoldenFrames(unittest.TestCase):
    """真实截图 -> 界面/手牌/公共牌/按钮/金额 与 golden 一致（识别逻辑回归）"""
    def test_frames(self):
        sys.path.insert(0, str(ROOT / "tests"))
        import make_golden as G
        gold = json.loads(G.GOLD.read_text())
        bad, n = [], 0
        for rel, exp in gold.items():
            p = ROOT / rel
            if not p.exists(): continue
            got = G.summary(S.parse(str(p))); n += 1
            if got != exp: bad.append((rel, {k: (exp[k], got[k]) for k in exp if exp[k] != got[k]}))
        self.assertGreater(n, 50)
        self.assertEqual(bad, [], f"{len(bad)}/{n} 帧与 golden 不一致")

    def test_parse_speed(self):
        f = ROOT / "model_test/live/005212_my_turn.raw"
        if not f.exists(): self.skipTest("no frame")
        S.parse(str(f))       # 预热常驻 OCR
        t = time.time()
        for _ in range(5): S.parse(str(f))
        self.assertLess((time.time() - t) / 5, 0.6, "parse 应 <0.6s（常驻 OCR）")

    def test_opp_name_from_screen(self):
        """顶部中央座位的对手昵称（OCR 常把 0 读成 O）；对手行动标签盖住名字时读不到，不误报"""
        names = {f: S.parse(str(ROOT / f"model_test/live/{f}.raw")).get("opp_name") for f in ("000010_001_other", "000016_003_my_turn")}
        self.assertEqual(S.name_key(names["000010_001_other"]), S.name_key("little00"))
        self.assertIsNone(names["000016_003_my_turn"])                     # 该帧名字位置是“跟注”标签
        self.assertIsNone(S.opp_name({"texts": [["加注", 720, 416], ["+14", 720, 430], ["密码局", 700, 400]]}))
        self.assertEqual(S.opp_name({"texts": [["牌桌大王", 720, 416]]}), "牌桌大王")

    def test_grey_preset_detected(self):
        """真实帧：底池 4 时 1/3=1 是灰档；002620 整体偏暗但 5 档都可用；001202 只有 1/3 可用"""
        cases = {"000019_005_my_turn": [True, False, False, False, False], "002620_my_turn": [False] * 5,
                 "001202_my_turn": [False, True, True, True, True], "004114_my_turn": [True, False, False, False, False]}
        n = 0
        for f, exp in cases.items():
            p = ROOT / f"model_test/live/{f}.raw"
            if not p.exists(): continue
            self.assertEqual([q["off"] for q in S.parse(str(p))["presets"]], exp, f); n += 1
        if not n: self.skipTest("no frame")


# ---------------------------------------------------------------- 2. 各界面流程
class TestScreens(unittest.TestCase):
    def test_profile_closes(self):
        b, io = bot(); r = b.handle("x", mk("profile", buttons={}), 100)
        self.assertEqual(io.taps, [(720, 300)]); self.assertEqual(r["act"], "关闭资料卡")

    def test_away_back_to_table(self):
        b, io = bot(); b.handle("x", mk("away", buttons={"回到牌桌": [720, 2386], "离开": [718, 2591]}), 100)
        self.assertEqual(io.taps, [(720, 2386)])

    def test_can_start(self):
        b, io = bot(); b.handle("x", mk("can_start", buttons={"开始游戏": [720, 1560]}), 100)
        self.assertEqual(io.taps, [(720, 1560)])

    def test_waiting_and_opp_turn_wait(self):
        b, io = bot()
        for scr, bt in (("waiting", {"邀请好友": [720, 1500]}), ("other", {"让或弃": [376, 2730], "让牌": [1064, 2735]})):
            self.assertEqual(b.handle("x", mk(scr, buttons=bt), 100)["act"], "wait")
        self.assertEqual(io.taps, [])

    def test_anim_waits(self):
        b, io = bot(fixed(OK_CALL))
        r = b.handle("x", mk("anim"), 100)
        self.assertEqual(r["act"], "wait"); self.assertEqual(io.taps, [])

    def test_stuck_other_saves_once_and_taps_close(self):
        b, io = bot(stuck=25)
        st = mk("other", buttons={"关闭": [1300, 400]})
        self.assertEqual(b.handle("x", st, 100)["act"], "wait")
        self.assertEqual(b.handle("x", mk("other", buttons={"关闭": [1300, 400]}), 120)["act"], "wait")
        r = b.handle("x", mk("other", buttons={"关闭": [1300, 400]}), 126)
        self.assertEqual(r["act"], "unknown->点关闭"); self.assertEqual(io.taps, [(1300, 400)])
        self.assertEqual(b.handle("x", mk("other", buttons={"关闭": [1300, 400]}), 130)["act"], "wait")   # 只处理一次

    def test_runit_lead_runs_twice(self):
        tx = [["乌贼69", 900, 1300], ["little00", 500, 1300], ["72.5%", 900, 1600], ["27.5%", 500, 1600], ["乌贼69", 720, 2470]]
        b, io = bot(); r = b.handle("x", mk("runit", buttons={"直接发牌": [460, 2051], "发两次": [978, 2051]}, texts=tx), 100)
        self.assertEqual(r["act"], "发两次"); self.assertEqual(io.taps, [(978, 2051)])

    def test_runit_behind_runs_once(self):
        tx = [["乌贼69", 900, 1300], ["little00", 500, 1300], ["27.5%", 900, 1600], ["72.5%", 500, 1600], ["乌贼69", 720, 2470]]
        b, io = bot(); r = b.handle("x", mk("runit", buttons={"直接发牌": [460, 2051], "发两次": [978, 2051]}, texts=tx), 100)
        self.assertEqual(r["act"], "直接发牌")

    def test_runit_unknown_equity_runs_once(self):
        b, io = bot(); r = b.handle("x", mk("runit", buttons={"直接发牌": [460, 2051], "发两次": [978, 2051]}), 100)
        self.assertEqual(r["act"], "直接发牌")


# ---------------------------------------------------------------- 3. 我的回合流程
class TestMyTurnFlow(unittest.TestCase):
    def test_decide_and_execute(self):
        b, io = bot(C.Decider(fixed(OK_CALL)))
        r = b.handle("x", mk(buttons={"弃牌": FOLD, "跟注": CALL, "加注": RAISE}, nums={"to_call": 4, "opp_bet": 6}), 100)
        self.assertEqual(io.taps, [tuple(CALL)]); self.assertEqual(r["dec"]["act"], "跟注")

    def test_no_double_tap_right_after_action(self):
        b, io = bot(C.Decider(fixed(OK_CALL)))
        st = lambda: mk(buttons={"弃牌": FOLD, "跟注": CALL}, nums={"to_call": 4, "opp_bet": 6})
        b.handle("x", st(), 100); b.handle("x", st(), 101)       # 1s 后同画面：界面还没刷新，不点
        self.assertEqual(len(io.taps), 1)

    def test_retry_when_tap_not_effective_then_cap(self):
        m = fixed(OK_CALL)
        b, io = bot(C.Decider(m))
        st = lambda: mk(buttons={"弃牌": FOLD, "跟注": CALL}, nums={"to_call": 4, "opp_bet": 6})
        b.handle("x", st(), 100)
        for t in (103, 106, 109, 112, 115):
            b.handle("x", st(), t)
        self.assertEqual(len(m.calls), 1, "重试不应再调模型")
        self.assertEqual(len(io.taps), 1 + 3, "最多重试 3 次")

    def test_new_situation_same_buttons_calls_model_again(self):
        m = fixed(OK_CALL)
        b, io = bot(C.Decider(m))
        b.handle("x", mk(buttons={"弃牌": FOLD, "跟注": CALL}, nums={"to_call": 4, "pot": 8}), 100)
        b.handle("x", mk(buttons={"弃牌": FOLD, "跟注": CALL}, nums={"to_call": 20, "pot": 40}), 103)   # 对手再加注
        self.assertEqual(len(m.calls), 2)

    def test_unknown_card_waits_then_proceeds(self):
        m = fixed('{"act":"让牌","size":null,"why":"x"}')
        b, io = bot(C.Decider(m))
        self.assertEqual(b.handle("x", mk(hero=("?s", "8h")), 100)["act"], "wait")
        self.assertEqual(b.handle("x", mk(hero=("?s", "8h")), 101)["act"], "wait")
        r = b.handle("x", mk(hero=("?s", "8h")), 102.5)       # 超过 2s 照常决策
        self.assertTrue(r["act"].startswith("让牌"))

    def test_partial_board_waits_and_not_recorded(self):
        """2026-10-30 22:48 真机：翻牌动画中只有 2 张公共牌就决策，HandHist 记了 '?' 街，下一手结算 walk() 崩溃退出"""
        m = fixed('{"act":"让牌","size":null,"why":"x"}')
        b, io = bot(C.Decider(m))
        self.assertEqual(b.handle("x", mk(board=("8d", "Ah")), 100)["act"], "wait")
        self.assertEqual(io.taps, [])
        h = S.HandHist(); h.observe(mk(hero=("8c", "Qh"), nums={"opp_bet": 2, "my_bet": 1}))
        h.observe(mk(hero=("8c", "Qh"), board=("8d", "Ah")))
        self.assertTrue(all(s != "?" for s, _ in h.ev)); self.assertEqual(h.street, "翻前")

    def test_session_walk_skips_unknown_street(self):
        import session_prompt as SP
        steps, prior, last, _, _ = SP.walk([("翻前", "我方跟注到 2"), ("?", "我方下注到 2"), ("翻牌", "对手让牌")], "BTN")
        self.assertEqual(last, "翻牌")

    def test_session_build_error_falls_back_to_rule(self):
        b, io = bot(C.Decider(fixed(OK_CALL)))
        class Bad:
            def build(self, st, hist): raise ValueError("boom")
        b.sess = Bad()
        r = b.handle("x", mk(buttons={"弃牌": FOLD, "跟注": CALL}, nums={"to_call": 4, "opp_bet": 6}), 100)
        self.assertTrue(r["dec"]["why"].startswith("规则:"), r)

    def test_preset_then_still_my_turn_confirms(self):
        b, io = bot(C.Decider(fixed(OK_RAISE)))
        b.handle("x", mk(presets=BB_PRE), 100)
        self.assertEqual(io.taps, [(511, 2305)])
        r = b.handle("x", mk(presets=BB_PRE, buttons={"弃牌": FOLD, "让牌": CHECK, "确定": [720, 2613]}), 101.5)
        self.assertTrue(r["act"].startswith("确认确定")); self.assertEqual(io.taps[-1], (720, 2613))

    def test_slider_dim_after_raise_taps_confirm(self):
        """加注滑条展开 -> 整屏变暗读不到牌：按原决策点确定，不重新问模型"""
        m = fixed('{"act":"加注","size":"9BB","why":"x"}')
        b, io = bot(C.Decider(m))
        b.handle("x", mk(), 100)                    # 无预设 -> raise_min 序列
        self.assertEqual(io.taps, [("raise_min", 720, 2620)])
        r = b.handle("x", mk(hero=(), buttons={"弃牌": FOLD, "确定": [720, 2613], "跟注": CALL}), 103)
        self.assertEqual(io.taps[-1], (720, 2613)); self.assertEqual(len(m.calls), 1)
        self.assertIn("滑条", r["note"])

    def test_slider_dim_uses_remembered_cards(self):
        m = seq_model((0, OK_CALL))
        b, io = bot(C.Decider(m))
        b.handle("x", mk("other", hero=("7s", "Ac")), 100)
        r = b.handle("x", mk(hero=(), buttons={"弃牌": FOLD, "确定": [720, 2613], "跟注": CALL}, nums={"to_call": 19, "opp_bet": 27}), 101)
        self.assertIn("手牌 7s Ac", m.calls[0])
        self.assertIn("沿用记忆的牌", r["note"])

    def test_model_error_uses_rule_not_timeout(self):
        b, io = bot(C.Decider(fixed("", err=RuntimeError("HTTP 500"))))
        r = b.handle("x", mk(hero=("2c", "7d"), buttons={"弃牌": FOLD, "跟注": CALL}, nums={"to_call": 4, "opp_bet": 6}), 100)
        self.assertTrue(r["llm"].get("rule")); self.assertEqual(io.taps, [tuple(FOLD)])

    def test_dry_mode_never_taps(self):
        b, io = bot(C.Decider(fixed(OK_CALL)), dry=True)
        b.handle("x", mk(buttons={"弃牌": FOLD, "跟注": CALL}, nums={"to_call": 4}), 100)
        b.handle("x", mk("profile"), 105); b.handle("x", mk("can_start", buttons={"开始游戏": [1, 1]}), 106)
        self.assertEqual(io.taps, [])


# ---------------------------------------------------------------- 4. 动作映射
class TestExecute(unittest.TestCase):
    def ex(self, st, dec):
        io = C.RecIO(); out = C.execute(st, dec, io); return out, io.taps

    def test_preset_by_name(self):
        out, t = self.ex(mk(presets=POT_PRE), {"act": "下注", "size": "2/3"}); self.assertEqual(t, [(720, 2291)])

    def test_preset_name_with_pool_suffix(self):
        out, t = self.ex(mk(presets=POT_PRE), {"act": "下注", "size": "1/2池"}); self.assertEqual(t, [(511, 2305)])

    def test_bb_size_when_presets_are_pot_fractions(self):
        out, t = self.ex(mk(presets=POT_PRE), {"act": "加注", "size": "6BB"})    # 12 筹码 -> 最接近 "1"=12
        self.assertEqual(t, [(927, 2302)])

    def test_illegal_size_uses_middle_preset(self):
        out, t = self.ex(mk(presets=POT_PRE), {"act": "加注", "size": "大一点"}); self.assertEqual(t, [(720, 2291)])

    def test_grey_preset_never_tapped(self):
        """1/3 档低于最小下注是灰的（点了不下注）：改点最近的可用档 1/2"""
        ps = [dict(p) for p in POT_PRE]; ps[0].update(amt=None, off=True)
        out, t = self.ex(mk(presets=ps), {"act": "下注", "size": "1/3"}); self.assertEqual(t, [(511, 2305)])

    def test_grey_large_presets_pick_largest_usable(self):
        """大档超过筹码变灰：要 1.2 池时点最大的可用档"""
        ps = [dict(p) for p in POT_PRE]; ps[3]["off"] = ps[4]["off"] = True
        out, t = self.ex(mk(presets=ps), {"act": "下注", "size": "1.2"}); self.assertEqual(t, [(720, 2291)])

    def test_all_presets_over_stack_goes_allin(self):
        ps = [dict(p, off=True) for p in POT_PRE]
        out, t = self.ex(mk(presets=ps, nums={"my_stack": 3, "my_bet": 0}), {"act": "下注", "size": "1/2"})
        self.assertEqual(t, [("allin", 720, 2620)])

    def test_grey_presets_hidden_from_prompt(self):
        ps = [dict(p) for p in POT_PRE]; ps[0].update(amt=None, off=True)
        txt = S.brief_text(mk(board=("4d", "Ad", "Kd"), presets=ps, buttons={"弃牌": FOLD, "让牌": CHECK, "下注": RAISE}))
        self.assertIn("下注[1/2=6", txt); self.assertNotIn("1/3", txt)

    def test_raise_without_presets_is_atomic_min_raise(self):
        out, t = self.ex(mk(), {"act": "加注", "size": "3BB"}); self.assertEqual(t, [("raise_min", 720, 2620)])

    def test_slider_already_open(self):
        out, t = self.ex(mk(buttons={"弃牌": FOLD, "确定": [720, 2613]}), {"act": "加注", "size": "3BB"}); self.assertEqual(t, [(720, 2613)])

    def test_allin_button(self):
        out, t = self.ex(mk(buttons={"弃牌": FOLD, "All in": [1064, 2733]}), {"act": "全下"}); self.assertEqual(t, [(1064, 2733)])

    def test_allin_via_slider(self):
        out, t = self.ex(mk(buttons={"弃牌": FOLD, "跟注": CALL, "加注": RAISE}), {"act": "全下"}); self.assertEqual(t, [("allin", 720, 2620)])

    def test_call_when_only_allin(self):
        out, t = self.ex(mk(buttons={"弃牌": FOLD, "All in": [1064, 2733]}), {"act": "跟注"}); self.assertEqual(t, [(1064, 2733)])

    def test_check_never_becomes_call(self):
        out, t = self.ex(mk(buttons={"弃牌": FOLD, "跟注": CALL}), {"act": "让牌"}); self.assertEqual(t, [tuple(FOLD)])

    def test_call_when_only_check(self):
        out, t = self.ex(mk(), {"act": "跟注"}); self.assertEqual(t, [tuple(CHECK)])

    def test_real_act(self):
        self.assertEqual(C.real_act({"act": "加注"}, "加注->滑条到顶->确定@(1,2)"), "全下")
        self.assertEqual(C.real_act({"act": "跟注"}, "All in@[1,2](跟注=全下)"), "全下")
        self.assertEqual(C.real_act({"act": "下注"}, "preset 1/2=6@[1,2]"), "下注")
        self.assertEqual(C.real_act({"act": "跟注"}, "兜底弃牌@[1,2]"), "弃牌")


# ---------------------------------------------------------------- 5. 延迟 / 模型异常（时间按 1/10 缩放：0.1s = 实际 1s）
K = 0.1
HEDGE, BUDGET = 3.0 * K, 8.0 * K     # 与 Decider / --hedge 默认值一致
LIMIT = 12 * K          # 截图开始 -> 动作发出 的硬上限


class TestLatency(unittest.TestCase):
    def run_bot(self, raw):
        b, io = bot(C.Decider(raw, hedge_after=HEDGE), budget=BUDGET)
        w0 = time.time()
        r = b.handle("x", mk(buttons={"弃牌": FOLD, "跟注": CALL}, nums={"to_call": 4, "opp_bet": 6}), 100, wall0=w0)
        dt = time.time() - w0
        self.assertLessEqual(dt, LIMIT); self.assertEqual(len(io.taps), 1, "必须有动作")
        return r, dt

    def test_fast_3s(self):
        r, dt = self.run_bot(fixed(OK_CALL, 2.5 * K)); self.assertEqual(r["llm"]["n_req"], 1); self.assertLess(dt, 3 * K)

    def test_default_hedge(self):
        self.assertEqual(C.Decider(None).hedge_after, 3.0)

    def test_preflop_rule_skips_model(self):
        raw = fixed(OK_RAISE, 0)
        b, io = bot(C.Decider(raw, hedge_after=HEDGE), budget=BUDGET, preflop_rule=True)
        r = b.handle("x", mk(buttons={"弃牌": FOLD, "跟注": CALL, "加注": RAISE}, nums={"to_call": 1, "opp_bet": 2, "my_bet": 1}), 100, wall0=time.time())
        self.assertEqual(raw.calls, [], "翻前不应调用模型"); self.assertTrue(r["llm"].get("rule")); self.assertEqual(len(io.taps), 1)
        r = b.handle("x", mk(board=("Ac", "7s", "2d"), buttons={"弃牌": FOLD, "让牌": CHECK, "加注": RAISE}), 110, wall0=time.time())
        self.assertEqual(len(raw.calls), 1, "翻后照常问模型")

    def test_slow_6s_hedge_wins(self):
        raw = seq_model((6 * K, OK_CALL), (1.5 * K, OK_CALL))
        r, dt = self.run_bot(raw)
        self.assertEqual(r["llm"]["winner"], 1); self.assertLess(dt, 5.8 * K)

    def test_slow_6s_no_faster_hedge(self):
        r, dt = self.run_bot(fixed(OK_CALL, 6 * K)); self.assertEqual(r["llm"]["n_req"], 2); self.assertLess(dt, 6.6 * K)

    def test_slow_9s_deadline_rule(self):
        r, dt = self.run_bot(fixed(OK_CALL, 9 * K)); self.assertTrue(r["llm"].get("rule")); self.assertLess(dt, BUDGET + 0.5 * K)

    def test_hang_15s_deadline_rule(self):
        r, dt = self.run_bot(fixed(OK_CALL, 15 * K)); self.assertTrue(r["llm"].get("timeout"))

    def test_error_then_hedge_immediately(self):
        raw = seq_model((0.5 * K, RuntimeError("SSL handshake timed out")), (2 * K, OK_CALL))
        r, dt = self.run_bot(raw)
        self.assertEqual(r["llm"]["winner"], 1); self.assertLess(dt, 3 * K)

    def test_both_error_rule_fast(self):
        r, dt = self.run_bot(fixed("", 0.2 * K, err=RuntimeError("HTTP 529")))
        self.assertTrue(r["llm"].get("rule")); self.assertLess(dt, 1 * K)

    def test_bad_json(self):
        r, dt = self.run_bot(fixed("我觉得应该跟注")); self.assertTrue(r["llm"].get("rule"))

    def test_illegal_act(self):
        r, dt = self.run_bot(fixed('{"act":"raise","size":"3BB"}')); self.assertTrue(r["llm"].get("rule"))

    def test_real_scale_hang(self):
        """真实时间尺度：模型挂死 -> 8s 内本地规则出手（≤12s 硬上限）"""
        b, io = bot(C.Decider(fixed(OK_CALL, 30), hedge_after=3.8), budget=8.0)
        w0 = time.time()
        r = b.handle("x", mk(buttons={"弃牌": FOLD, "跟注": CALL}, nums={"to_call": 4, "opp_bet": 6}), 100, wall0=w0 - 1.2)  # 截图+parse 已用 1.2s
        self.assertLess(time.time() - (w0 - 1.2), 8.3); self.assertEqual(len(io.taps), 1)


# ---------------------------------------------------------------- 6. 行动历史 / 牌力 / 规则
class TestFrameRoll(unittest.TestCase):
    def test_keeps_protected_and_newest(self):
        import frameroll as F
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); (root / "tests").mkdir(); live = root / "live"; live.mkdir()
            (root / "tests" / "golden_frames.json").write_text(json.dumps({"model_test/live/000001_my_turn.raw": {}}))
            (root / "tests" / "t.py").write_text('f = "000002_005_other"')
            for i in range(1, 7):
                p = live / f"00000{i}_my_turn.raw" if i != 2 else live / "000002_005_other.raw"
                p.write_bytes(b"x" * 1000); os.utime(p, (i, i))
            (live / "watch.log").write_text("log")
            dels, before, after, n_prot = F.cleanup(live, keep_gb=2500 / 1024 ** 3, root=root)
            self.assertEqual(n_prot, 2); self.assertEqual(before, 4000); self.assertEqual(after, 2000)
            self.assertEqual(sorted(p.name for p in dels), ["000003_my_turn.raw", "000004_my_turn.raw"])
            left = sorted(p.name for p in live.iterdir())
            self.assertEqual(left, ["000001_my_turn.raw", "000002_005_other.raw", "000005_my_turn.raw", "000006_my_turn.raw", "watch.log"])
            self.assertEqual(F.cleanup(live, keep_gb=0, root=root)[0], [])


class TestHandHist(unittest.TestCase):
    def st(self, opp, my, hero=("Ah", "Kd"), board=(), dealer=(720, 900)):
        return mk(hero=hero, board=board, dealer=dealer, nums={"opp_bet": opp, "my_bet": my})

    def test_bb_3bet_then_4bet(self):
        h = S.HandHist()
        st = self.st(6, 2); h.observe(st)
        h.mine({"act": "加注"}, "preset 1=18@[927,2302]", st)
        h.observe(self.st(50, 18))
        txt = "\n".join(h.text())
        self.assertIn("对手开局加注到 6", txt); self.assertIn("我方加注到 18（3bet）", txt); self.assertIn("对手加注到 50（4bet）", txt)

    def test_tap_not_effective_undo(self):
        h = S.HandHist()
        st = self.st(6, 2); h.observe(st); n0 = len(h.ev)
        h.mine({"act": "跟注"}, "跟注@[1,2]", st)
        h.observe(self.st(6, 2))                 # 仍是我的回合、金额没变 -> 撤销
        self.assertEqual(len(h.ev), n0)

    def test_new_hand_resets(self):
        h = S.HandHist(); h.observe(self.st(6, 2)); h.mine({"act": "弃牌"}, "弃牌@[1,2]", self.st(6, 2))
        h.observe(self.st(2, 1, hero=("7c", "7d"), dealer=(720, 2000)))
        self.assertNotIn("我方弃牌", "\n".join(h.text()))

    def test_street_change_records_call(self):
        h = S.HandHist(); st = self.st(2, 1, dealer=(720, 2000)); h.observe(st)
        h.mine({"act": "加注"}, "preset 3BB=6@[1,2]", st)
        h.observe(self.st(0, 0, board=("2c", "7d", "9h"), dealer=(720, 2000)))
        self.assertIn("对手跟注", "\n".join(h.text()))

    def test_call_odds_capped_by_my_stack(self):
        """对手全下 1602、我方只剩 196：所需胜率按有效筹码算（≈49%），不是 11%"""
        st = mk(buttons={"弃牌": FOLD, "All in": CALL}, nums={"pot": 1604, "opp_bet": 1602, "my_bet": 2, "to_call": 1600, "my_stack": 196, "opp_stack": 0})
        self.assertEqual(S.cap_call(st["nums"]), (200, 196))
        self.assertIn("所需胜率 49%", S.brief_text(st))
        # 跟注额已被界面封顶成我方筹码时同样要扣掉超出部分
        self.assertEqual(S.cap_call({"pot": 1604, "opp_bet": 1602, "my_bet": 2, "to_call": 196, "my_stack": 196}), (200, 196))

    def test_bet_amount_misread_then_raise(self):
        """预设额 5 但 OCR 读到我方下注 4、对手加注到 20：不能当成点击失败撤销，且用实际额修正"""
        fl = ("5c", "8s", "Th"); d = (720, 2000)
        h = S.HandHist(); h.observe(self.st(2, 1, dealer=d)); h.mine({"act": "跟注"}, "跟注@[1,2]", self.st(2, 1, dealer=d))
        st = self.st(0, 0, board=fl, dealer=d); h.observe(st)
        h.mine({"act": "下注"}, "preset 2/3=5@[1,2]", st)
        h.observe(self.st(20, 4, board=fl, dealer=d))
        txt = "\n".join(h.text())
        self.assertIn("我方下注到 4", txt); self.assertIn("对手加注到 20", txt)

    def _flop_bet_unknown(self, turn_prev):
        """BB 翻前让牌到翻牌（底池 4），翻牌点了金额读不到的预设（记成 '到 ?'），转牌开始时底池 = turn_prev"""
        d = (720, 900); fl = ("4d", "Ad", "Kd")
        h = S.HandHist()
        st = mk(board=(), dealer=d, nums={"opp_bet": 2, "my_bet": 2, "pot": 4, "pot_prev": 0}); h.observe(st)
        h.mine({"act": "让牌"}, "让牌@[1,2]", st)
        st = mk(board=fl, dealer=d, nums={"opp_bet": 0, "my_bet": 0, "pot": 4, "pot_prev": 4}); h.observe(st)
        h.mine({"act": "下注"}, "preset 1/3=None@[304,2407](要1/3)", st)
        self.assertIn("我方下注到 ?", "\n".join(h.text()))
        h.observe(mk(board=fl + ("Kc",), dealer=d, nums={"opp_bet": 0, "my_bet": 0, "pot": turn_prev, "pot_prev": turn_prev}))
        return "\n".join(h.text())

    def test_unknown_bet_fixed_by_pot_growth(self):
        """对手跟注后换街：底池 4 -> 8，反推我方下注到 2"""
        txt = self._flop_bet_unknown(8)
        self.assertIn("我方下注到 2", txt); self.assertIn("对手跟注", txt); self.assertNotIn("?", txt)

    def test_unknown_bet_without_pot_growth_is_check(self):
        """点了灰档（没下注）：换街后底池没涨 -> 改记为 我方让牌 → 对手让牌（2026-10-30 回放 000019 实例）"""
        txt = self._flop_bet_unknown(4)
        self.assertIn("翻牌: 我方让牌 → 对手让牌", txt); self.assertNotIn("对手跟注", txt.split("翻牌:")[1])


class TestMadeDesc(unittest.TestCase):
    CASES = [
        (("Ah", "Kd"), ("Ac", "7s", "2d"), "顶对"), (("Qh", "Qd"), ("Jc", "7s", "2d"), "超对"),
        (("9h", "9d"), ("Jc", "Ts", "2d"), "有 2 种"), (("Ah", "7d"), ("Ac", "7s", "2d"), "两对"),
        (("Ah", "Kd"), ("7c", "7s", "2d"), "公共牌对子"), (("7h", "7d"), ("7c", "Ks", "2d"), "暗三条"),
        (("Ah", "2h"), ("Kh", "9h", "4c"), "坚果同花听"), (("8h", "2h"), ("Kh", "9h", "4c"), "非坚果"),
        (("9c", "8d"), ("7h", "6s", "2c"), "两头"), (("9c", "5d"), ("7h", "6s", "2c"), "卡顺"),
        (("Ah", "Kd"), ("Qc", "Js", "Td"), "顺子"), (("2h", "3d"), ("Ac", "Ks", "Qd", "Jh", "Tc"), "没用到手牌"),
        (("Ah", "Kh"), ("Qh", "7h", "2h"), "同花("), (("Kc", "Kd"), ("Ks", "7c", "7d"), "葫芦"),
        (("Ts", "8h"), ("9h", "Ah", "5h"), "非坚果"),
    ]

    def test_cases(self):
        for hero, board, want in self.CASES:
            with self.subTest(hero=hero, board=board):
                self.assertIn(want, " ".join(S.made_desc(list(hero), list(board))))

    def test_combo_draw_outs_dedup(self):
        """004552：Td8d 在 Kd Qd 9h 上 = 同花 9 + 卡J 4 − Jd 重复 = 12 outs；到河牌精确约 45%（旧 outs×4 写成 52%+打折）"""
        md = S.made_desc(["Td", "8d"], ["Kd", "Qd", "9h"])
        self.assertIn("合计 12 outs", md[-1])
        self.assertEqual(S.draw_outs(md[-1]), 12)
        nxt, river = S.draw_eq(12, 3)
        self.assertAlmostEqual(nxt, 25.5, delta=0.2)
        self.assertAlmostEqual(river, 45.0, delta=0.2)
        line = S.draw_line(md[-1], 3)[0]
        self.assertIn("12 outs", line); self.assertIn("到河牌约 45%", line); self.assertNotIn("要打折", line)
        self.assertEqual(S.draw_outs("听牌: 两头/双卡顺子听牌 8 outs"), 8)
        self.assertEqual(S.draw_line("听牌: 无（后门不算）", 3), [])
        self.assertIn("只剩一张", S.draw_line("听牌: 卡顺听牌 4 outs", 4)[0])

    def test_incomplete_returns_empty(self):
        self.assertEqual(S.made_desc(["?s", "8h"], ["9h", "Ah", "5h"]), [])


class TestRules(unittest.TestCase):
    def act(self, **kw): return C.rule_decide(mk(**kw))

    def test_free_check(self): self.assertEqual(self.act(hero=("7c", "2d"))["act"], "让牌")
    def test_premium_raises_preflop(self):
        d = self.act(hero=("Ah", "Kd"), presets=BB_PRE); self.assertEqual((d["act"], d["size"]), ("下注", "3BB"))
    def test_trash_folds_to_raise(self):
        self.assertEqual(self.act(hero=("7c", "2d"), buttons={"弃牌": FOLD, "跟注": CALL}, nums={"to_call": 4})["act"], "弃牌")
    def test_playable_calls_small(self):
        self.assertEqual(self.act(hero=("Ah", "5d"), buttons={"弃牌": FOLD, "跟注": CALL}, nums={"to_call": 4})["act"], "跟注")
    def test_playable_folds_big(self):
        self.assertEqual(self.act(hero=("Qh", "5d"), buttons={"弃牌": FOLD, "跟注": CALL}, nums={"to_call": 40, "pot": 50})["act"], "弃牌")
    def test_ak_calls_shove_capped_odds(self):
        """对手全下 801BB、我方 99BB：按有效筹码算所需 49%，AKo 估计 54% -> 跟"""
        d = self.act(hero=("Kc", "Ad"), buttons={"弃牌": FOLD, "跟注": CALL}, nums={"to_call": 1600, "pot": 1604, "opp_bet": 1602, "my_bet": 2, "my_stack": 196})
        self.assertEqual(d["act"], "跟注")
    def test_strong_bets_two_thirds(self):
        d = self.act(hero=("Ah", "Kd"), board=("Ac", "7s", "2d"), presets=POT_PRE, buttons={"弃牌": FOLD, "让牌": CHECK, "下注": RAISE})
        self.assertEqual((d["act"], d["size"]), ("下注", "2/3"))
    def test_draw_with_odds_calls(self):
        d = self.act(hero=("Ah", "2h"), board=("Kh", "9h", "4c"), buttons={"弃牌": FOLD, "跟注": CALL}, nums={"to_call": 10, "pot": 30})
        self.assertEqual(d["act"], "跟注")
    def test_air_folds(self):
        d = self.act(hero=("7c", "2d"), board=("Kh", "9h", "4c"), buttons={"弃牌": FOLD, "跟注": CALL}, nums={"to_call": 10, "pot": 30})
        self.assertEqual(d["act"], "弃牌")
    def test_board_pair_is_not_strong(self):
        d = self.act(hero=("Ah", "Kd"), board=("7c", "7s", "2d"), buttons={"弃牌": FOLD, "跟注": CALL}, nums={"to_call": 20, "pot": 30})
        self.assertEqual(d["act"], "弃牌")

    def test_draw_text_not_mistaken_for_straight(self):
        """听牌行含“顺子听牌/顺子补牌”字样，旧正则把纯听牌当成顺子强牌（价值下注、跟全下）"""
        self.assertEqual(self._bet_or_check(("9c", "8d"), ("7h", "6s", "2c")), "让牌")          # 两头顺听，无成牌
        self.assertEqual(self._bet_or_check(("Td", "8d"), ("Kd", "Qd", "9h")), "让牌")          # 004552 组合听
        d = self.act(hero=("Td", "8d"), board=("Kd", "Qd", "9h"), buttons={"弃牌": FOLD, "All in": [1064, 2733]},
                     nums={"pot": 486, "pot_prev": 24, "opp_bet": 462, "my_bet": 0, "to_call": 314, "my_stack": 314})
        self.assertEqual(d["act"], "弃牌", d)                                                  # 12 outs 到河 45% < 所需 48%
        d = self.act(hero=("Td", "8d"), board=("Kd", "Qd", "9h"), buttons={"弃牌": FOLD, "跟注": CALL},
                     nums={"pot": 60, "pot_prev": 24, "opp_bet": 36, "my_bet": 0, "to_call": 36, "my_stack": 300})
        self.assertEqual(d["act"], "跟注", d)                                                  # 需 37.5%，45% 够

    def _bet_or_check(self, hero, board):
        return self.act(hero=hero, board=board, presets=POT_PRE, buttons={"弃牌": FOLD, "让牌": CHECK, "下注": RAISE})["act"]

    def test_two_pair_on_four_straight_board_not_value_bet(self):
        """2026-10-30 回放 005050：QJ 在 Ac Js 8c Qd Kc（一张 T 成顺、三张梅花）上不能当强牌打价值"""
        self.assertEqual(self._bet_or_check(("Jd", "Qh"), ("Ac", "Js", "8c", "Qd", "Kc")), "让牌")

    def test_top_pair_on_monotone_board_checks(self):
        self.assertEqual(self._bet_or_check(("Ah", "Kd"), ("Ac", "7c", "2c")), "让牌")
        self.assertEqual(self._bet_or_check(("Ah", "Kd"), ("Ac", "7s", "7d")), "下注")    # 两对(A和7)，A 是我方的大对：照常打价值

    def test_two_pair_with_bigger_board_pair_is_one_pair(self):
        self.assertEqual(self._bet_or_check(("5h", "6d"), ("Kc", "Ks", "5d")), "让牌")

    def test_nut_hands_still_value_bet(self):
        self.assertEqual(self._bet_or_check(("Kh", "6d"), ("Kc", "6s", "6c")), "下注")    # 葫芦
        self.assertEqual(self._bet_or_check(("Ah", "5h"), ("Kh", "7h", "2h")), "下注")    # 坚果同花
        self.assertEqual(self._bet_or_check(("Ah", "Kd"), ("Ac", "7s", "2d")), "下注")    # 干燥面顶对

    def test_danger_two_pair_still_calls_small_bet(self):
        d = self.act(hero=("Jd", "Qh"), board=("Ac", "Js", "8c", "Qd", "Kc"), buttons={"弃牌": FOLD, "跟注": CALL}, nums={"to_call": 5, "pot": 30})
        self.assertEqual(d["act"], "跟注")

    def test_low_flush_on_four_flush_board_folds_to_jam(self):
        """模型评测 river_4flush_low_flush_vs_jam：公共牌四张红桃，6 高同花面对全下，旧规则当强牌跟注"""
        jam = dict(buttons={"弃牌": FOLD, "All in": [1064, 2733]}, nums={"pot": 205, "opp_bet": 189, "my_bet": 0, "to_call": 189, "my_stack": 189})
        self.assertEqual(self.act(hero=("6h", "3d"), board=("Ah", "9h", "5h", "Kc", "2h"), **jam)["act"], "弃牌")
        self.assertEqual(self.act(hero=("Kh", "3d"), board=("Ah", "9h", "5h", "Kc", "2h"), **jam)["act"], "跟注")   # 坚果同花照跟
        self.assertEqual(self._bet_or_check(("6h", "7h"), ("8h", "9h", "Th", "2h")), "下注")                       # 同花顺不降级
        self.assertEqual(self._bet_or_check(("6h", "3d"), ("Ah", "9h", "5h", "2h")), "让牌")                       # 小同花不打价值

    def test_preflop_vs_4bet_uses_narrow_range(self):
        """模型评测 pre_bb_vs_4bet_A5o：旧 vs_big_eq 不看加注层级，A5o 面对 4bet 估 46%“够跟”"""
        h = S.HandHist(); h.street, h.level = "翻前", 4
        st = mk(hero=("Ah", "5c"), buttons={"弃牌": FOLD, "跟注": CALL},
                nums={"pot": 56, "opp_bet": 40, "my_bet": 16, "to_call": 24, "my_stack": 184, "opp_stack": 160})
        self.assertEqual(C.rule_decide(st, h)["act"], "弃牌")
        self.assertIn("前5%", S.brief_text(st, h)); self.assertIn("不够", S.brief_text(st, h))
        info = S.preflop_rank(("Ac", "Ad"))[1]
        self.assertGreaterEqual(S.vs_big_eq(info, 4)[0], 75)                                  # AA 对任何范围都够
        self.assertEqual([S.big_range(2, r) for r in (10, 20, 40, 100)], [50, 35, 20, 10])
        self.assertEqual((S.big_range(3), S.big_range(4), S.big_range(5)), (10, 5, 5))


# ---------------------------------------------------------------- 7. 基础设施
class TestModelEval(unittest.TestCase):
    """tools/model_eval.py：用例能构造出合法局面、正确答案点得到；判分按实际执行的动作归一化"""
    @classmethod
    def setUpClass(cls):
        import model_eval as E
        cls.E = E

    def test_cases_consistent(self):
        E = self.E
        self.assertEqual(len({c["id"] for c in E.CASES}), len(E.CASES))
        for c in E.CASES:
            st, h = E.build(c); n, b = st["nums"], st["buttons"]
            self.assertEqual(n["pot"], n["pot_prev"] + n["opp_bet"] + n["my_bet"], c["id"])
            for a in c["ok"] + c["bad"]:
                legal = {"弃牌": True, "让牌": not n["to_call"], "跟注": bool(n["to_call"]),
                         "下注": bool(st["board"]) and not n["to_call"], "加注": bool(E.S.usable_presets(st)) and (not st["board"] or bool(n["to_call"])),
                         "全下": n["my_stack"] > n["to_call"]}[a]
                self.assertTrue(legal, f"{c['id']}: {a} 在该局面不可执行（to_call={n['to_call']} 按钮={list(b)}）")
            for m in (c["modes"] or ("short", "session")):
                pr = E.prompt_for(c, st, h, m)
                txt = pr if isinstance(pr, str) else pr["messages"][-1]["content"] + pr["system"][1]["text"]
                self.assertIn(" ".join(c["hero"]), txt)
                if c["opp"]: self.assertIn(E.PROFILES[c["opp"]]["ptype"].split("：")[0], txt)

    def test_effective_and_grade(self):
        E = self.E
        get = lambda i: next(c for c in E.CASES if c["id"] == i)
        c = get("river_royal_vs_allin"); st, _ = E.build(c)
        self.assertEqual(list(st["buttons"]), ["弃牌", "All in"])
        for act in ("跟注", "全下"):                                   # 跟注即全下 -> 记“跟注”
            self.assertEqual(E.effective(st, {"act": act})[0], "跟注")
        self.assertEqual(E.grade(c, st, "弃牌", None), "严重")
        c = get("flop_free_check_air"); st, _ = E.build(c)
        self.assertEqual(E.effective(st, {"act": "加注", "size": "1/2"})[:2], ("下注", "1/2"))   # 翻后没人下注时“加注”=下注
        self.assertEqual(E.grade(c, st, "弃牌", None), "严重")             # 能免费让牌却弃牌
        c = get("flop_air_vs_pot"); st, _ = E.build(c)
        self.assertEqual(E.effective(st, {"act": "让牌"})[0], "弃牌")       # 面对下注想让牌 -> 实际兜底弃牌
        c = get("river_nutflush_value"); st, _ = E.build(c)
        self.assertEqual(E.grade(c, st, "下注", "1/3"), "尺度偏"); self.assertEqual(E.grade(c, st, "下注", "1"), "对")
        r = E.run_one(get("pre_btn_AA"), "short", lambda p, timeout=0, max_tokens=0: "好的 {\"act\":\"加注\",\"size\":\"3BB\",\"why\":\"x\"}", 5)
        self.assertEqual((r["verdict"], r["frac"]), ("对", "3BB"))
        r = E.run_one(get("pre_btn_AA"), "short", lambda p, timeout=0, max_tokens=0: "不知道", 5)
        self.assertEqual(r["verdict"], "无效")


class TestLogroll(unittest.TestCase):
    def test_ops_log_format_levels_uncaught(self):
        """标准日志：时间.毫秒 级别 [线程] 模块 - 消息；INFO->stdout、WARN/ERROR->stderr；file_only 只进文件；
        ops_start 后写 logs/<日期>/ops.log，子线程未捕获异常记 ERROR + traceback（子进程跑，不污染本进程的 hook）"""
        import subprocess
        with tempfile.TemporaryDirectory() as d:
            code = f"""
import sys, threading; sys.path.insert(0, {str(ROOT / 'tools')!r})
from pathlib import Path
import logroll; logroll.LOGS = Path({d!r})
log = logroll.logger("bot"); log.info("启动前只打印")
logroll.ops_start()
log.info("动作 %s", "跟注"); log.warning("画面卡住"); log.error("写失败", extra={{"file_only": True}})
th = threading.Thread(target=lambda: 1 / 0, name="grab"); th.start(); th.join()
logroll.ops_flush()
print(logroll.path("ops.log"))
"""
            r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
            out, err = r.stdout.splitlines(), r.stderr
            lines = Path(out[-1]).read_text().splitlines()
        pat = r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3} "
        self.assertRegex(out[0], pat + r"INFO  \[MainThread\] bot - 启动前只打印$")
        self.assertRegex(out[1], pat + r"INFO  \[MainThread\] bot - 动作 跟注$")
        self.assertIn("WARN  [MainThread] bot - 画面卡住", err); self.assertNotIn("写失败", err + r.stdout)   # file_only 不上控制台
        self.assertFalse(any("启动前只打印" in l for l in lines))
        self.assertRegex(lines[0], pat + r"INFO  \[MainThread\] bot - 动作 跟注$")
        self.assertIn("WARN  [MainThread] bot - 画面卡住", lines[1]); self.assertIn("ERROR [MainThread] bot - 写失败", lines[2])
        self.assertIn("ERROR [grab] uncaught - 未捕获异常（线程 grab）: ZeroDivisionError", lines[3])
        self.assertIn("Traceback", "\n".join(lines[4:])); self.assertIn("ZeroDivisionError: division by zero", lines[-1])


class TestSession(unittest.TestCase):
    """--prompt session：开局长 system(缓存) + 手内多轮；新手牌时结算上一手"""
    def setUp(self):
        import session_prompt as P
        self.P, self.tmp = P, tempfile.mkdtemp()
        self.log = Path(self.tmp) / "hands.jsonl"
        self.model = seq_model((0, OK_CALL))
        self.sess = P.HandSession(None, "opp", hands_log=self.log)
        self.b, self.io = bot(C.Decider(self.model), prompt="session", session=self.sess)

    def pre(self, hero=("Ah", "Kd"), stack=199):
        return mk(hero=hero, buttons={"弃牌": FOLD, "跟注": CHECK, "加注": RAISE},
                  nums={"pot": 3, "opp_bet": 2, "my_bet": 1, "to_call": 1, "my_stack": stack})

    def test_request_structure_and_turns(self):
        self.b.handle("x", self.pre(), 100)
        req = self.model.calls[0]
        self.assertIsInstance(req, dict)
        self.assertEqual(req["system"][0].get("cache_control"), {"type": "ephemeral"})
        self.assertIn("暂无画像", req["system"][1]["text"])
        self.assertEqual(len(req["messages"]), 1)
        u = req["messages"][0]["content"]
        self.assertIn("手牌 #1", u); self.assertIn("AKo", u); self.assertIn("起始筹码 我方 100BB", u)
        self.assertIn("轮到 BTN[乌贼69](我方)", u); self.assertIn("对手画像 BB[opp]", u); self.assertIn("简述：", u)
        self.assertLess(u.index("对手画像"), u.index("起始筹码"))          # 画像紧跟在当前手牌下面
        self.assertTrue(u.rstrip().endswith("现在根据用户行为、画像和相关数据进行决策行动"))
        flop = mk(board=("2c", "7d", "9h"), nums={"pot": 4, "opp_bet": 0, "my_bet": 0, "to_call": 0, "my_stack": 196})
        self.b.handle("x", flop, 110)
        req2 = self.model.calls[1]
        self.assertEqual(len(req2["messages"]), 1)                      # 每次一条完整记录
        u2 = req2["messages"][0]["content"]
        self.assertIn("手牌 #1", u2)
        self.assertIn("BTN[乌贼69](我方) 跟注 0.5BB，剩余 98BB", u2)      # 剩余筹码按 BB 计
        self.assertIn("flop [2c 7d 9h] 底池 2BB:", u2)
        self.assertEqual(u2.count("对手画像"), 1)                             # 动作行不再重复画像

    def test_new_hand_closes_previous(self):
        self.b.handle("x", self.pre(), 100)
        self.b.handle("x", self.pre(hero=("7c", "2d"), stack=209), 130)    # 新手牌：总筹码 200 -> 210
        self.assertEqual(len(self.model.calls[1]["messages"]), 1)          # 手内对话已清空
        self.sess.flush(); recs = [json.loads(l) for l in self.log.read_text().splitlines()]
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["delta"], 10); self.assertEqual(recs[0]["hero"], ["Ah", "Kd"])
        self.assertIn("手牌 #1", self.sess.recent[0])
        self.assertIn("手牌 #1", self.model.calls[1]["system"][1]["text"])
        self.assertIn("手牌 #2", self.model.calls[1]["messages"][0]["content"])   # 近况注入下一手的 system

    def test_warm_start_from_other_table(self):
        """热启动：对手在别的桌的历史手并入统计；冷启动：新对手给群体参考；同一手多次记录只算一次"""
        P = self.P
        root = Path(self.tmp); tables = root / "tables"
        (tables / "t1").mkdir(parents=True); (tables / "t2").mkdir()
        h_open = {"t": "2026-10-29 10:00:00", "opp": "villain", "pos": "BB", "hero": ["7c", "2d"], "board": [],
                  "ev": [["翻前", "对手开局加注到 4"], ["翻前", "我方弃牌"]], "how": "我方弃牌", "delta": -2}
        rows = [dict(h_open, hero=[f"{r}c", "2d"]) for r in "3456789"] + [dict(h_open, hero=["3c", "2d"], t="x")]   # 最后一条与第一条重复
        rows.append(dict(h_open, opp="other"))
        (tables / "t1" / "hands.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows[:4]))
        (tables / "t2" / "hands.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows[4:]))   # 跨两张桌
        hist = P.load_hands("villain", tables=tables)
        pop = P.load_hands(exclude="villain", tables=tables)
        self.assertEqual(len(hist), 7); self.assertEqual(len(pop), 1)
        sess = P.HandSession(None, "villain", history=hist, population=pop)
        req, u = sess.build(self.pre(), S.HandHist())
        self.assertIn("累计7手(此前7+本局0)", u); self.assertIn("开池7/7=100%", u)
        self.assertIn("此前 7 手", req["system"][1]["text"])
        self.assertIn("群体参考（其他对手共1手", u)                       # 样本 < 30 手仍附群体参考
        new = P.HandSession(None, "newbie", population=hist)
        _, u2 = new.build(self.pre(), S.HandHist())
        self.assertIn("新对手无样本，先按群体倾向", u2); self.assertIn("群体参考（其他对手共7手", u2)
        self.assertNotIn("群体参考", P.HandSession(None, "x", history=hist * 5).build(self.pre(), S.HandHist())[1])  # ≥30 手不再附

    def test_profile_per_table(self):
        """画像按桌：只找本桌 <opp>.md（name_key 匹配）；本桌没有时 find_profile 回退到对手库里最近一份（标注来自其他桌）"""
        P = self.P
        root = Path(self.tmp); t1, t2 = root / "t1", root / "t2"
        t1.mkdir(); t2.mkdir()
        (t1 / "vil lain.md").write_text("# t1\n- 类型：松凶\n本桌画像\n"); (t2 / "vil_lain.md").write_text("# t2")
        self.assertEqual(P.profile_path(t1, "vil lain"), t1 / "vil lain.md")
        self.assertEqual(P.profile_path(t2, "vil lain"), t2 / "vil_lain.md")          # 同一人不同桌各一份
        self.assertIsNone(P.profile_path(root / "nope", "vil lain")); self.assertIsNone(P.profile_path(t1, "nobody"))
        row = {"profile": "库里的画像", "ptype": "跟注站"}
        f = P.find_profile(t1, "vil lain", row); self.assertEqual((f["src"], f["ptype"]), ("本桌", "松凶")); self.assertIn("本桌画像", f["profile"])
        f = P.find_profile(root / "t3", "vil lain", row)
        self.assertEqual((f["src"], f["ptype"]), ("他桌", "跟注站")); self.assertIn("其他桌", f["profile"]); self.assertIn("库里的画像", f["profile"])
        self.assertIsNone(P.find_profile(root / "t3", "vil lain"))

    def test_profile_updater_writes_table(self):
        """ProfileUpdater 写本桌 <opp>.md；旧画像优先本桌，本桌没有时用库里最近一份（他桌）作先验；写完同步进库"""
        import oppdb
        P = self.P
        root = Path(self.tmp); tdir = root / "tables" / "t1"; tdir.mkdir(parents=True)
        db = oppdb.OppDB(root / "o.db")
        other = root / "tables" / "t0" / "little00.md"; other.parent.mkdir(); other.write_text("# 他桌画像\n- 类型：紧弱\n"); db.sync_profiles([other])
        prompts = []
        def model(prompt, **kw): prompts.append(prompt); return "# 新画像\n- 类型：松凶\n"
        u = P.ProfileUpdater(model, tdir, None, every=1, store=db)
        u._run([{"n": 1}], "little00")
        self.assertIn("来自其他桌", prompts[0]); self.assertIn("他桌画像", prompts[0])
        self.assertEqual((tdir / "little00.md").read_text(), "# 新画像\n- 类型：松凶\n")
        self.assertEqual(db.lookup("little00")["ptype"], "松凶")                         # 库里换成最新一份
        u._run([{"n": 2}], "little00")
        self.assertIn("# 新画像", prompts[1].split("### 旧画像")[1]); self.assertTrue((tdir / "little00.md.bak").exists())
        self.assertEqual(other.read_text(), "# 他桌画像\n- 类型：紧弱\n")               # 他桌画像不动
        db.close()

    def test_detect_opp_async_and_switch(self):
        """不靠 --opp：截图读到昵称（多帧投票、容忍 0/O）→ 后台加载跨桌历史 → 下一手生效；之前的手补上昵称再写；换人重置本局统计"""
        P = self.P
        root = Path(self.tmp); tables = root / "tables"
        (tables / "t0").mkdir(parents=True)
        h = {"t": "2026-10-29 10:00:00", "opp": "little00", "pos": "BB", "hero": ["7c", "2d"], "board": [],
             "ev": [["翻前", "对手开局加注到 4"], ["翻前", "我方弃牌"]], "how": "我方弃牌", "delta": -2}
        (tables / "t0" / "hands.jsonl").write_text("".join(json.dumps(dict(h, hero=[f"{r}c", "2d"]), ensure_ascii=False) + "\n" for r in "3456789"))
        old = P.TABLES
        P.TABLES = tables
        try:
            logs = []
            sess = P.HandSession(None, None, hands_log=self.log, warm=True, log=logs.append)
            b, io = bot(C.Decider(seq_model((0, OK_CALL))), prompt="session", session=sess)
            st = self.pre(); st["opp_name"] = "little0O"
            b.handle("x", st, 100)                                          # 第 1 手：只读到 1 次，未确认
            self.assertIsNone(sess.opp); self.assertIsNone(sess._thread)
            for _ in range(2): sess.see_opp("little0O")
            sess._thread.join(2)
            self.assertEqual(sess._pending[0], "little00")                  # 对手库里已有同键名字，用它显示
            self.assertIsNone(sess.opp)                                     # 手内不切换
            st2 = self.pre(hero=("7c", "2d"), stack=209); st2["opp_name"] = "little00"
            b.handle("x", st2, 130)                                         # 第 2 手开始：生效
            self.assertEqual(sess.opp, "little00"); self.assertEqual(sess.n_hist, 7)
            self.assertIn("此前7+本局1", sess.user_text(st2, b.hist))
            sess.flush(); recs = [json.loads(l) for l in self.log.read_text().splitlines()]
            self.assertEqual([r["opp"] for r in recs], ["little00"])         # 识别前结束的第 1 手补写
            self.assertIn("当前对手：little00", sess.static)
            self.assertTrue(any("识别到对手 little00" in m for m in logs))
            for _ in range(3): sess.see_opp("NewGuy")                       # 换人
            sess._thread.join(2)
            b.handle("x", self.pre(hero=("9c", "9d"), stack=199), 160)
            self.assertEqual(sess.opp, "NewGuy"); self.assertEqual(sess.n_hist, 0); self.assertEqual(sess.hands, [])
            self.assertIn("当前对手：NewGuy", sess.static)
        finally:
            P.TABLES = old

    def test_unknown_opp_hands_written_on_flush(self):
        """对手一直没识别出来：退出 flush 时这些手照样写进流水（opp=null），不计入对手库"""
        import oppdb
        db = oppdb.OppDB(Path(self.tmp) / "o.db")
        sess = self.P.HandSession(None, None, hands_log=self.log, store=db, store_write=True, log=lambda m: None)
        sess.build(self.pre(), S.HandHist()); sess.build(self.pre(hero=("7c", "2d"), stack=209), S.HandHist())
        sess.flush()
        recs = [json.loads(l) for l in self.log.read_text().splitlines()]
        self.assertEqual([(r["opp"], r["hero"]) for r in recs], [(None, ["Ah", "Kd"])]); self.assertTrue(recs[0]["hid"])
        self.assertEqual(db.counts(), (0, 0)); db.close()

    def test_hand_write_is_async(self):
        """每手记录（jsonl/对手库/画像回调）在后台线程写，写慢也不阻塞主线程出下一手的请求"""
        gate = threading.Event()
        sess = self.P.HandSession(None, "opp", hands_log=self.log, on_hand_end=lambda rec, s: gate.wait(5))
        sess.build(self.pre(), S.HandHist())
        t0 = time.time()
        sess.build(self.pre(hero=("7c", "2d"), stack=209), S.HandHist())       # 上一手结束 -> 入队
        sess.build(self.pre(hero=("9c", "9d"), stack=199), S.HandHist())
        self.assertLess(time.time() - t0, 0.5)
        self.assertNotEqual(sess._writer.ident, threading.get_ident())
        gate.set(); sess.flush()
        self.assertEqual([json.loads(l)["hero"] for l in self.log.read_text().splitlines()], [["Ah", "Kd"], ["7c", "2d"]])   # 顺序不变

    def test_ops_line_and_summary(self):
        """ops.log：每个动作一行中文（画面/牌/底池/决策来源/点击/延迟/备注）；每手结束回调一行摘要"""
        st = {"nums": {"pot": 12, "to_call": 4}}
        rec = {"t": "18:20:01", "screen": "my_turn", "hero": "Ah Kd", "board": "Qs 7d 2c", "dec": {"act": "加注", "size": "2/3", "why": "顶对强踢脚价值"},
               "t_llm": 2.5, "llm": {}, "act": "preset 2/3@[1,2]", "lat": 3.1}
        self.assertEqual(C.ops_line(rec, st), "我的回合 Ah Kd [Qs 7d 2c] 池6BB 需跟2BB | 加注 2/3（模型2.5s）顶对强踢脚价值 -> preset 2/3@[1,2] | 3.1s")
        r2 = {"t": "18:20:09", "screen": "other", "hero": "", "board": "", "act": "unknown", "note": "画面卡住>20s"}
        self.assertEqual(C.ops_line(r2, {}), "其他 -> unknown | 画面卡住>20s")
        self.assertIn("（规则）", C.ops_line(dict(rec, llm={"rule": True}), st))
        got = []
        sess = self.P.HandSession(None, "opp", hands_log=self.log, on_summary=got.append, log=lambda m: None)
        sess.build(self.pre(), S.HandHist()); self.assertEqual(got, [])
        sess.build(self.pre(hero=("7c", "2d"), stack=209), S.HandHist()); sess.flush()
        self.assertEqual(len(got), 1); self.assertTrue(got[0].startswith("手牌 #1 我方")); self.assertIn("Ah Kd", got[0])

    def test_flatten_for_generic(self):
        req, _ = self.sess.build(self.pre(), S.HandHist())
        t = self.P.flatten(req)
        self.assertIn("策略基线", t); self.assertIn("【牌桌】", t)

    def test_reference_and_spr(self):
        s = self.sess.static
        for k in ("单挑统计参考值", "60–75%", "SPR（代码", "下注尺度速查", "Solver 通用结论"):
            self.assertIn(k, s)
        self.assertNotIn("UTG", s); self.assertNotIn("多人底池", s); self.assertNotIn("### 8.1", s)   # 不带 6max 内容、不重复标题
        st = {"hero": ["Ah", "Kd"], "board": ["2c", "7d", "Qs"], "buttons": ["弃牌", "跟注"], "presets": [],
              "nums": {"pot": 40, "to_call": 20, "my_stack": 380, "opp_stack": 360, "my_bet": 0, "opp_bet": 20}}
        self.assertIn("SPR 9.0", self.P.snapshot_bb(st, []))                # min(380,360)/40
        st.update(board=[]); self.assertFalse(any(l.startswith("SPR") for l in self.P.snapshot_bb(st, [])))   # 翻前不给


class TestOppDB(unittest.TestCase):
    """对手库 SQLite（players 表：累计统计+画像，按需查库）：增量累计/不重复计数/OCR 容错/群体统计减法/画像/接入 HandSession"""
    def setUp(self):
        import oppdb, session_prompt as P
        self.O, self.P, self.tmp = oppdb, P, Path(tempfile.mkdtemp())
        self.db = oppdb.OppDB(self.tmp / "o.db")

    def tearDown(self): self.db.close()

    def h(self, opp, t, hero=("7c", "2d"), pos="BB"):
        return {"t": f"2026-10-29 10:00:{t:02d}", "opp": opp, "pos": pos, "hero": list(hero), "board": [],
                "ev": [["翻前", "对手开局加注到 4"], ["翻前", "我方弃牌"]], "how": "我方弃牌", "delta": -2}

    def jl(self, f, rows, mode="a"):
        f.parent.mkdir(parents=True, exist_ok=True)
        with open(f, mode) as fo: fo.write("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))

    def test_schema_and_add_per_hand(self):
        cols = {r[1] for r in self.db.conn.execute("PRAGMA table_info(players)")}
        self.assertTrue({"key", "n", "vp", "vp_n", "won", "vp_won", "net", "b3", "b4", "cb", "cb_opp", "profile", "ptype", "last_seen"} <= cols)
        self.assertFalse({"tables", "stats"} & cols)                                          # 统计拆成列，不记桌
        self.assertIn("idx_players_last", {r[1] for r in self.db.conn.execute("PRAGMA index_list(players)")})
        tabs = {r[0] for r in self.db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(tabs, {"players"})                                                   # 明细不进库，也不再记 jsonl 偏移
        self.assertTrue(self.db.fresh)
        for i in (0, 1, 2): self.db.add_hands([self.h("villain", i)])                        # 一手一手加
        self.db.add_hands([self.h("other", 9), self.h("vi11ain", 5), self.h("villain", 6)])
        self.db.close(); self.db = self.O.OppDB(self.tmp / "o.db")                            # 重开后直接查库
        self.assertFalse(self.db.fresh)
        v = self.db.lookup("vi11ain")                                                         # OCR l→1 容错
        self.assertEqual((v["n_hands"], v["first_seen"][-2:], v["last_seen"][-2:]), (5, "00", "06"))
        self.assertEqual(v["stats"], self.P.opp_stats([self.h("villain", i) for i in (0, 1, 2, 5, 6)]))
        self.assertEqual((v["stats"]["won"], v["stats"]["vp_won"], v["stats"]["net"]), (5, 5, 10))   # 每手对手收池 2 筹码
        self.assertIsNone(self.db.lookup("nobody"))
        r = self.db.rates("villain")[0]                                                       # 视图：百分比
        self.assertEqual((r["hands"], r["vpip"], r["won"], r["open"], r["b3"], r["bb100"]), (5, 100.0, 100.0, 100.0, None, 100.0))

    def test_rebuild_from_jsonl(self):
        """新库 / 表结构升级：open_db 从明细 jsonl 全量重建（画像保留）；平时打开不读 jsonl"""
        f = self.tmp / "players" / "villain" / "hands.jsonl"
        self.jl(f, [self.h("villain", i) for i in range(3)])
        db = self.O.open_db([f], path=self.tmp / "n.db")
        self.assertEqual((db.synced, db.lookup("villain")["n_hands"]), (3, 3))
        self.jl(f, [self.h("villain", 7)]); db.close()
        db = self.O.open_db([f], path=self.tmp / "n.db")
        self.assertEqual((db.synced, db.lookup("villain")["n_hands"]), (0, 3))                # 平时不读 jsonl
        db.conn.execute("PRAGMA user_version = 1"); db.close()
        db = self.O.open_db([f], path=self.tmp / "n.db")
        self.assertEqual((db.synced, db.lookup("villain")["n_hands"]), (4, 4))
        g = self.tmp / "villain.md"; g.write_text("# 画像\n- 类型：松凶\n")
        db.sync_profiles([g]); self.assertEqual(db.rebuild([f]), 4)
        self.assertEqual((db.lookup("villain")["n_hands"], db.lookup("villain")["ptype"]), (4, "松凶"))
        db.close()

    def test_hand_detail(self):
        """明细 jsonl：每个动作的位置 / 类型 / 投入 / 本街累计到 / 动作前底池 / 剩余（BB）"""
        rec = {"t": "x", "opp": "v", "pos": "BTN", "hero": ["Ah", "Kd"], "board": ["2c", "7d", "9h"], "how": "", "delta": 6,
               "start": {"me": 200, "opp": 200},
               "ev": [["翻前", "我方加注到 4（开局加注(2bet)）"], ["翻前", "对手跟注"], ["翻牌", "对手让牌"], ["翻牌", "我方下注到 3"], ["翻牌", "对手弃牌"]]}
        d = self.P.hand_detail(rec, "t1")
        self.assertEqual(d["table"], "t1"); self.assertEqual(d["start_bb"], {"me": 100.0, "opp": 100.0})
        A = [(a["st"], a["who"], a["pos"], a["act"], a["add"], a["to"], a["pot"], a["left"]) for a in d["acts"]]
        self.assertEqual(A, [("preflop", "me", "BTN", "raise", 1.5, 2.0, 1.5, 98.0), ("preflop", "opp", "BB", "call", 1.0, 2.0, 3.0, 98.0),
                             ("flop", "opp", "BB", "check", 0.0, 0.0, 4.0, 98.0), ("flop", "me", "BTN", "bet", 1.5, 1.5, 4.0, 96.5),
                             ("flop", "opp", "BB", "fold", 0.0, 0.0, 5.5, 98.0)])

    def test_stat_counters(self):
        """新增统计口径：4B / 面对4B弃 / 对limp加注 / 我方cbet（对手先让牌）/ 转牌二次cbet / check-raise / 摊牌赢"""
        P = self.P
        H = lambda pos, ev, board=(), how="", delta=0: {"pos": pos, "ev": ev, "board": list(board), "how": how, "delta": delta}
        B = ("2c", "7d", "9h", "Ks", "3d")
        S = P.opp_stats([
            H("BB", [["翻前", "对手开局加注到 4"], ["翻前", "我方加注到 12（3bet）"], ["翻前", "对手加注到 30（4bet）"], ["翻前", "我方弃牌"]]),
            H("BTN", [["翻前", "我方加注到 4（开局加注(2bet)）"], ["翻前", "对手加注到 12（3bet）"], ["翻前", "我方加注到 30（4bet）"]]),
            H("BTN", [["翻前", "我方跟注到 2"], ["翻前", "对手加注到 6"], ["翻前", "我方弃牌"]]),
            H("BTN", [["翻前", "我方加注到 4（开局加注(2bet)）"], ["翻前", "对手跟注"], ["翻牌", "对手让牌"], ["翻牌", "我方下注到 3"]], B[:3]),
            H("BB", [["翻前", "对手开局加注到 4"], ["翻前", "我方跟注到 4"], ["翻牌", "我方让牌"], ["翻牌", "对手下注到 3"], ["翻牌", "我方跟注到 3"],
                     ["转牌", "我方让牌"], ["转牌", "对手下注到 8"], ["转牌", "我方跟注到 8"], ["河牌", "我方让牌"], ["河牌", "对手让牌"]],
              B, "摊牌或河牌后结束", -10),
            H("BTN", [["翻前", "我方加注到 4（开局加注(2bet)）"], ["翻前", "对手跟注"], ["翻牌", "对手让牌"], ["翻牌", "我方下注到 3"],
                      ["翻牌", "对手加注到 10"], ["翻牌", "我方弃牌"]], B[:3]),
        ])
        got = {k: S[k] for k in ("b4", "f3_opp", "f4_opp", "f4_fold", "b3", "vs_limp", "iso", "fcb_opp", "fcb", "rcb",
                                 "cb_opp", "cb", "cb2_opp", "cb2", "xr_opp", "xr", "wtsd", "wsd")}
        self.assertEqual(got, dict(b4=1, f3_opp=1, f4_opp=1, f4_fold=1, b3=1, vs_limp=1, iso=1, fcb_opp=2, fcb=1, rcb=1,
                                   cb_opp=1, cb=1, cb2_opp=1, cb2=1, xr_opp=2, xr=1, wtsd=1, wsd=1))
        txt = "\n".join(P._stat_lines(S, "x"))
        for k in ("PFR", "4B1/1", "面对4B弃1/1", "对limp加注1/1", "转牌二次cbet1/1", "check-raise1/2", "摊牌赢1/1"): self.assertIn(k, txt)

    def test_population_subtract(self):
        P = self.P
        self.db.add_hands([self.h("villain", i) for i in range(4)] + [self.h("other", i, pos="BTN") for i in range(3)])
        self.assertEqual(self.db.pop("villain"), P.opp_stats([self.h("other", i, pos="BTN") for i in range(3)]))   # 群体 = SUM − 该对手
        self.assertEqual(self.db.pop()["n"], 7); self.assertEqual(self.db.pop("newbie")["n"], 7)
        self.db.add_hands([self.h("other", 30)])                                              # 合计随写入更新
        self.assertEqual(self.db.pop("villain")["n"], 4); self.assertEqual(self.db.counts(), (2, 8))
        self.assertIsNone(self.O.OppDB(self.tmp / "empty.db").pop("x"))

    def test_profiles(self):
        tables = self.tmp / "tables"
        for d in ("t1", "t2", "_template"): (tables / d).mkdir(parents=True)
        (tables / "t1" / "little00.md").write_text("# 旧桌\n- 类型：紧弱\n")
        g = tables / "t2" / "little00.md"; g.write_text("# 画像\n- 类型：松凶\n正文\n\n## 7. 更新日志\n- 旧\n")
        os.utime(tables / "t1" / "little00.md", (1, 1))
        for f in ("table.md", "_x.md"): (tables / "t2" / f).write_text("# x")
        (tables / "_template" / "y.md").write_text("# y")
        files = self.P._profile_files(tables)
        self.assertEqual(sorted(str(f.relative_to(tables)) for f in files), ["t1/little00.md", "t2/little00.md"])   # 不含 table.md/_ 开头/模板
        self.assertEqual(self.db.sync_profiles(files), 1); self.assertEqual(self.db.sync_profiles(files), 0)   # 同一人取最新一桌；未修改不重读
        d = self.db.lookup("little0O")
        self.assertEqual(d["ptype"], "松凶"); self.assertIn("正文", d["profile"]); self.assertNotIn("更新日志", d["profile"])
        self.assertEqual(d["n_hands"], 0)

    def test_session_with_store(self):
        """有库时：识别昵称 → 从内存取累计统计/画像/群体参考 → 下一手生效；真机每手追加 jsonl 后累计进库"""
        P = self.P
        self.db.add_hands([self.h("little00", i) for i in range(5)] + [self.h("other", i) for i in range(6)])
        g = self.tmp / "tables" / "t0" / "little00.md"; g.parent.mkdir(parents=True); g.write_text("# 画像\n- 类型：跟注站\n库里的画像\n"); self.db.sync_profiles([g])
        tdir = self.tmp / "tables" / "t1"; tdir.mkdir()
        hl, ml = tdir / "hands.jsonl", tdir / "msgs.jsonl"
        sess = P.HandSession(tdir, None, warm=True, store=self.db, store_write=True, hands_log=hl, msgs_log=ml, log=lambda m: None)
        self.assertEqual(sess.pop["n"], 11)                                      # 未识别前用全体做群体参考
        _, u = sess.build(self.pre(), S.HandHist())
        self.assertIn("群体参考（其他对手共11手", u)
        sess.commit(u, {"action": "跟注"})                                       # 这手有一次模型决策
        for _ in range(3): sess.see_opp("little0O")
        sess._thread.join()
        req, u = sess.build(self.pre(hero=("7c", "2d"), stack=209), S.HandHist())
        self.assertEqual(sess.opp, "little00"); self.assertEqual(sess.n_hist, 5); self.assertEqual(sess.pop["n"], 6)
        self.assertIn("累计6手(此前5+本局1)", u); self.assertIn("群体参考（其他对手共6手", u)
        self.assertIn("简述：跟注站", u); self.assertIn("库里的画像", req["system"][1]["text"])
        self.assertIn("其他桌", req["system"][1]["text"])                         # 本桌还没画像：用他桌最近一份作先验
        sess.flush()
        self.assertEqual(self.db.lookup("little00")["n_hands"], 6)               # 识别前结束的那手补上昵称，计数直接加进库
        det = [json.loads(l) for l in hl.read_text().splitlines()]                 # 本桌流水
        self.assertEqual(len(det), 1); self.assertIn("acts", det[0]); self.assertEqual(det[0]["opp"], "little00"); self.assertEqual(det[0]["table"], "t1")
        self.assertRegex(det[0]["hid"], r"^\d{8}-\d{6}-[0-9a-f]{4}-0001$")
        m = [json.loads(l) for l in ml.read_text().splitlines()]                    # 对话单独存，按 hid 双向关联
        self.assertEqual((len(m), m[0]["hid"]), (1, det[0]["hid"])); self.assertTrue(m[0]["msgs"])
        self.assertEqual(Path(det[0]["msgs"]).resolve() if Path(det[0]["msgs"]).is_absolute() else P.ROOT / det[0]["msgs"], ml.resolve())
        (tdir / "little00.md").write_text("# 本桌\n- 类型：松凶\n本桌新画像\n")      # 本桌有了画像（ProfileUpdater 写的）→ 下一手换上
        sess.build(self.pre(hero=("9c", "9d"), stack=199), S.HandHist()); sess.flush()
        req, u = sess.build(self.pre(hero=("8c", "8d"), stack=199), S.HandHist())
        self.assertIn("本桌新画像", req["system"][1]["text"]); self.assertNotIn("其他桌", req["system"][1]["text"]); self.assertIn("简述：松凶", u)

    def test_initial_opp_warm(self):
        """--opp 初值 + 热启动：后台查到的就是同一人时也要并入跨桌统计和画像"""
        self.db.add_hands([self.h("little00", i) for i in range(5)])
        sess = self.P.HandSession(None, "little00", warm=True, store=self.db, log=lambda m: None)
        sess._thread.join()
        _, u = sess.build(self.pre(), S.HandHist())
        self.assertEqual(sess.n_hist, 5); self.assertIn("此前5+本局0", u)

    def pre(self, hero=("Ah", "Kd"), stack=199):
        return mk(hero=hero, buttons={"弃牌": FOLD, "跟注": CHECK, "加注": RAISE},
                  nums={"pot": 3, "opp_bet": 2, "my_bet": 1, "to_call": 1, "my_stack": stack})

    def test_query_speed(self):
        """1 万个对手 / 20 万手：重建耗时；逐手写入耗时；不热加载，直接查库 lookup+群体参考的耗时"""
        f = self.tmp / "players" / "big" / "hands.jsonl"
        self.jl(f, [dict(self.h(f"p{i % 10000}", i % 60), n=i) for i in range(200000)], "w")
        t0 = time.time(); db = self.O.open_db([f], path=self.tmp / "big.db"); t_rb = time.time() - t0
        self.assertEqual(db.counts(), (10000, 200000))
        db.close(); t0 = time.time(); db = self.O.open_db([f], path=self.tmp / "big.db"); t_open = (time.time() - t0) * 1000
        db.pop("x")
        t0 = time.time()
        for i in range(2000): db.add_hands([self.h(f"p{i}", 1)])
        t_add = (time.time() - t0) / 2000 * 1000
        t0 = time.time()
        for i in range(10000): db.lookup(f"p{i}"); db.pop(f"p{i}")
        t_q = (time.time() - t0) / 10000 * 1000
        print(f"\n[oppdb] 20万手重建 {t_rb:.1f}s，平时打开 {t_open:.1f}ms，每手写入 {t_add:.2f}ms，查库 lookup+群体参考 {t_q:.3f}ms/次", file=sys.stderr)
        self.assertEqual(db.counts(), (10000, 202000)); self.assertLess(t_q, 5); self.assertLess(t_add, 20)
        self.assertEqual(db.pop("p1")["n"], 202000 - 21)
        db.close()


class FakeCapIO:
    def __init__(self, period): self.period, self.n = period, 0
    def capture(self, path):
        time.sleep(self.period); self.n += 1
        Path(path).write_bytes(str(self.n).encode() * 600); return True


class TestInfra(unittest.TestCase):
    def test_grabber_returns_fresh_frame(self):
        with tempfile.TemporaryDirectory() as d:
            g = C.Grabber(FakeCapIO(0.05), d); g.start()
            p1, t1 = g.get(0)
            p2, t2 = g.get(t1 + 1e-3)
            self.assertGreater(t2, t1); g.stop = True; g.join(1)

    def test_grabber_frame_survives_ring_overwrite_via_hardlink(self):
        with tempfile.TemporaryDirectory() as d:
            g = C.Grabber(FakeCapIO(0.02), Path(d) / "ring", n=2); g.start()
            p, t = g.get(0); keep = Path(d) / "keep.raw"; C.keep_frame(p, keep)
            data = keep.read_bytes()
            g.get(t + 0.2); g.stop = True; g.join(1)
            self.assertEqual(keep.read_bytes(), data)

    def test_async_writer(self):
        with tempfile.TemporaryDirectory() as d:
            w = C.AsyncWriter(); t = time.time()
            for i in range(200): w.write(Path(d) / "a.jsonl", f"{i}\n")
            self.assertLess(time.time() - t, 0.05, "写日志不应阻塞主循环")
            w.flush(); self.assertEqual(len((Path(d) / "a.jsonl").read_text().splitlines()), 200)

    def test_prompt_formats(self):
        st = mk(presets=BB_PRE)
        txt = C.TPROMPT.format(state=S.brief_text(st, S.HandHist()))
        self.assertIn('"act"', txt); self.assertIn("2BB=4", txt)
        self.assertLess(len(txt), 800, "精简 prompt 应 <800 字")
        self.assertEqual(set(C.PROMPTS), {"short"}, "long 已删除；session 走 HandSession")

    def test_brief_text_facts(self):
        st = mk(hero=("Ts", "8h"), board=("9h", "Ah", "5h"), buttons={"弃牌": FOLD, "跟注": CALL, "加注": RAISE}, presets=POT_PRE,
                nums={"pot": 200, "pot_prev": 120, "opp_bet": 80, "my_bet": 0, "to_call": 80, "my_stack": 366, "opp_stack": 1234})
        txt = S.brief_text(st, S.HandHist())
        for s in ("不同花", "翻牌 9h Ah 5h", "非坚果", "需跟注 80，所需胜率 29%", "加注到 1/3=4", "跟注80", "有效 366"):
            self.assertIn(s, txt)

    def test_brief_text_allin_only(self):
        st = mk(hero=("3c", "7h"), buttons={"弃牌": FOLD, "All in": [1064, 2733]}, nums={"to_call": 358, "my_stack": 358, "my_bet": 2, "pot": 642})
        txt = S.brief_text(st, S.HandHist())
        self.assertIn("跟注即全下", txt); self.assertIn("全下360", txt)


if __name__ == "__main__":
    unittest.main(verbosity=1)
