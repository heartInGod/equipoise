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
HEDGE, BUDGET = 3.8 * K, 8.0 * K
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
        r, dt = self.run_bot(fixed(OK_CALL, 3 * K)); self.assertEqual(r["llm"]["n_req"], 1); self.assertLess(dt, 3.5 * K)

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


# ---------------------------------------------------------------- 7. 基础设施
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
        recs = [json.loads(l) for l in self.log.read_text().splitlines()]
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["delta"], 10); self.assertEqual(recs[0]["hero"], ["Ah", "Kd"])
        self.assertIn("手牌 #1", self.sess.recent[0])
        self.assertIn("手牌 #1", self.model.calls[1]["system"][1]["text"])
        self.assertIn("手牌 #2", self.model.calls[1]["messages"][0]["content"])   # 近况注入下一手的 system

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
        self.assertIn('"act"', C.TPROMPT_LONG.format(state=S.to_text(st, S.HandHist())))

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
