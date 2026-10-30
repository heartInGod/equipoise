"""模型决策评测用例（tools/model_eval.py 读）。每个用例是一个“轮到我方决策”的局面 + 可接受的动作。

写法：只写 手牌/公共牌/我方位置/本手行动线（HandHist 的原话，“街 事件”）/起始筹码，
底池、本街下注、剩余筹码、按钮、加注预设都由 model_eval.build() 按真机界面规律推出来。
  ev 词汇（与 tools/state.HandHist 一致，金额单位=筹码，1BB=2）：
    翻前 对手开局加注到 5 / 翻前 对手平跟(limp) / 翻前 我方加注到 5 / 翻前 对手加注到 16（3bet）/ 翻前 我方跟注到 16
    翻牌 对手让牌 / 翻牌 我方让牌 / 翻牌 对手下注 8（对手本街先下注）/ 翻牌 对手下注到 8（我方让牌后对手下注）
    翻牌 我方下注到 6 / 翻牌 对手跟注 / 转牌 对手加注到 45
  ok   = 正确动作（实际执行后的动作：弃牌/让牌/跟注/下注/加注/全下；跟注即全下时记“跟注”；翻前任何加注记“加注”）
  bad  = 严重错误（另外：能免费让牌却弃牌，一律算严重）
  size = 下注/加注时可接受的预设档位名（None=不检查）
  opp  = 对手画像（PROFILES 的 key），只有 session prompt 能看到画像，这类用例 modes=("session",)
答案只收“明确”的局面（教科书/求解器结论一致、不依赖细微频率），混合策略的局面把几种动作都放进 ok。
"""

# 常用行动线
SRP_BTN = ["翻前 我方加注到 5", "翻前 对手跟注"]                 # 我方按钮开 2.5BB，大盲跟：翻牌底池 5BB，各剩 97.5BB
SRP_BB = ["翻前 对手开局加注到 5", "翻前 我方跟注到 5"]          # 对手按钮开 2.5BB，我方大盲跟
P3B_BTN = ["翻前 我方加注到 5", "翻前 对手加注到 16（3bet）", "翻前 我方跟注到 16"]   # 3bet 底池 16BB


def C(id, cat, hero, pos, ev=(), board="", ok=(), bad=(), size=None, stack=200, opp=None, modes=None, why=""):
    return dict(id=id, cat=cat, hero=hero.split(), pos=pos, ev=list(ev), board=board.split(), ok=list(ok), bad=list(bad),
                size=size, stack=stack, opp=opp, modes=modes, why=why)


CASES = [
    # ---------------- 翻前 ----------------
    C("pre_btn_AA", "翻前", "Ah As", "BTN", ok=["加注"], bad=["弃牌"], size=["2BB", "3BB", "4BB"], why="按钮位 AA 必须开池加注（2~4BB）"),
    C("pre_btn_K7o", "翻前", "Kc 7d", "BTN", ok=["加注"], why="K7o 前33%，单挑按钮开池范围内"),
    C("pre_btn_72o", "翻前", "7c 2d", "BTN", ok=["弃牌"], why="72o 前94%，单挑按钮也该弃（开约80~85%）"),
    C("pre_bb_vs_open_72o", "翻前", "7c 2d", "BB", ["翻前 对手开局加注到 5"], ok=["弃牌"], why="大盲面对 2.5BB 开池，72o 弃"),
    C("pre_bb_vs_open_K9s", "翻前", "Kh 9h", "BB", ["翻前 对手开局加注到 5"], ok=["跟注", "加注"], bad=["弃牌"], why="K9s 大盲必防守（跟或 3bet）"),
    C("pre_bb_vs_open_QQ", "翻前", "Qs Qd", "BB", ["翻前 对手开局加注到 5"], ok=["加注"], bad=["弃牌"], why="QQ 大盲面对开池应 3bet 价值"),
    C("pre_bb_limp_83o", "翻前", "8c 3d", "BB", ["翻前 对手平跟(limp)"], ok=["让牌"], bad=["弃牌"], why="对手平跟，大盲弱牌免费看翻牌"),
    C("pre_bb_limp_AKo", "翻前", "Ac Kd", "BB", ["翻前 对手平跟(limp)"], ok=["加注"], bad=["弃牌"], why="AKo 对平跟要加注孤立"),
    C("pre_btn_vs_3bet_AKs", "翻前", "Ah Kh", "BTN", ["翻前 我方加注到 5", "翻前 对手加注到 16（3bet）"],
      ok=["加注", "跟注", "全下"], bad=["弃牌"], why="AKs 面对 3bet 4bet 或跟注，绝不弃"),
    C("pre_btn_vs_3bet_J4o", "翻前", "Jc 4d", "BTN", ["翻前 我方加注到 5", "翻前 对手加注到 16（3bet）"], ok=["弃牌"], why="J4o 面对 3bet 弃"),
    C("pre_bb_vs_4bet_AA", "翻前", "Ad Ac", "BB", ["翻前 对手开局加注到 5", "翻前 我方加注到 16（3bet）", "翻前 对手加注到 40（4bet）"],
      ok=["加注", "全下", "跟注"], bad=["弃牌"], why="AA 面对 4bet 继续（5bet/全下/跟）"),
    C("pre_bb_vs_4bet_A5o", "翻前", "Ah 5c", "BB", ["翻前 对手开局加注到 5", "翻前 我方加注到 16（3bet）", "翻前 对手加注到 40（4bet）"],
      ok=["弃牌"], why="轻 3bet 的 A5o 面对 4bet 弃"),
    C("pre_btn_10bb_A5o", "翻前", "Ah 5c", "BTN", stack=20, ok=["全下", "加注"], bad=["弃牌"], why="10BB 有效筹码，A5o 按钮推或开"),
    C("pre_bb_vs_shove15_KTo", "翻前", "Kd Ts", "BB", ["翻前 对手开局加注到 30"], stack=(200, 30), ok=["跟注"],
      why="对手 15BB 全下（范围很宽），KTo 胜率约 57% > 所需 47%，跟"),
    C("pre_bb_vs_shove100_72o", "翻前", "7c 2d", "BB", ["翻前 对手开局加注到 200"], ok=["弃牌"], bad=["跟注"], why="100BB 全下，72o 弃"),

    # ---------------- 规则 / 读牌陷阱 ----------------
    C("flop_free_check_air", "规则陷阱", "7c 2d", "BB", SRP_BB, "Ah Kd 9s", ok=["让牌"], bad=["弃牌"], why="没人下注时永远不弃牌"),
    C("flop_air_vs_pot", "规则陷阱", "7c 2d", "BB", SRP_BB + ["翻牌 我方让牌", "翻牌 对手下注到 10"], "Ah Kd 9s",
      ok=["弃牌"], why="7 高无听牌面对满池下注，弃（不能让牌）"),
    C("river_royal_vs_allin", "规则陷阱", "Ah Kh", "BTN",
      SRP_BTN + ["翻牌 对手让牌", "翻牌 我方让牌", "转牌 对手让牌", "转牌 我方让牌", "河牌 对手下注 195"], "Qh Jh Th 3c 2d",
      ok=["跟注"], bad=["弃牌"], why="皇家同花顺面对全下必跟（All in 按钮是我方的跟注）"),
    C("river_board_twopair_trap", "规则陷阱", "7c 6c", "BB",
      SRP_BB + ["翻牌 我方让牌", "翻牌 对手下注到 6", "翻牌 我方跟注到 6", "转牌 我方让牌", "转牌 对手让牌", "河牌 我方让牌", "河牌 对手下注到 22"],
      "Kd Kh 3s 3c Qd", ok=["弃牌"], bad=["跟注", "加注", "全下"], why="两对全在公共牌上、Q 踢脚压过 7，我方最多平分，跟注无利"),
    C("river_4flush_low_flush_vs_jam", "规则陷阱", "6h 3d", "BB",
      SRP_BB + ["翻牌 我方让牌", "翻牌 对手下注到 6", "翻牌 我方跟注到 6", "转牌 我方让牌", "转牌 对手让牌", "河牌 我方让牌", "河牌 对手下注到 189"],
      "Ah 9h 5h Kc 2h", ok=["弃牌"], why="公共牌四张同花，6 高同花面对全下，任何更大的红桃都赢我们"),
    C("river_air_vs_pot", "规则陷阱", "5h 4h", "BB",
      SRP_BB + ["翻牌 我方让牌", "翻牌 对手下注到 6", "翻牌 我方跟注到 6", "转牌 我方让牌", "转牌 对手让牌", "河牌 我方让牌", "河牌 对手下注到 22"],
      "Ah Kd 9h 2c Qs", ok=["弃牌"], bad=["跟注"], why="同花听牌没中，5 高连诈唬都抓不到"),

    # ---------------- 翻后价值 ----------------
    C("flop_TPTK_bet", "翻后价值", "Ad Kc", "BTN", SRP_BTN + ["翻牌 对手让牌"], "Ah 7c 2d", ok=["下注"], why="顶对顶踢脚干燥面，对手让牌，下注价值"),
    C("flop_set_wet_vs_bet", "翻后价值", "9s 9d", "BB", SRP_BB + ["翻牌 我方让牌", "翻牌 对手下注到 7"], "9h Th Jc",
      ok=["加注", "跟注", "全下"], bad=["弃牌"], why="湿润面三条，加注保护/价值，至少跟"),
    C("river_nutflush_value", "翻后价值", "Ah Qh", "BTN",
      SRP_BTN + ["翻牌 对手让牌", "翻牌 我方下注到 6", "翻牌 对手跟注", "转牌 对手让牌", "转牌 我方让牌", "河牌 对手让牌"], "Kh 8h 3c 2s 5h",
      ok=["下注", "全下"], size=["2/3", "1", "1.2"], why="河牌坚果同花，对手让牌，大额价值下注"),
    C("turn_nut_straight_vs_raise", "翻后价值", "Jc Qd", "BTN",
      SRP_BTN + ["翻牌 对手让牌", "翻牌 我方下注到 6", "翻牌 对手跟注", "转牌 对手让牌", "转牌 我方下注到 15", "转牌 对手加注到 45"], "8c 9d 2s Th",
      ok=["加注", "全下", "跟注"], bad=["弃牌"], why="转牌坚果顺子面对 check-raise，继续"),
    C("flop_spr2_top_pair_vs_jam", "翻后价值", "Ks Qd", "BTN", P3B_BTN + ["翻牌 对手下注 64"], "Kc 8d 3s", stack=80,
      ok=["跟注"], why="3bet 底池 SPR 2，顶对好踢脚面对全下，所需 40%，跟"),

    # ---------------- 听牌赔率 ----------------
    C("flop_nut_fd_small_bet", "听牌赔率", "Ah 5h", "BB", SRP_BB + ["翻牌 我方让牌", "翻牌 对手下注到 3"], "Kh 7h 2c",
      ok=["跟注", "加注"], bad=["弃牌"], why="坚果同花听+A 高面对 1/3 池，所需 19%，远够"),
    C("flop_combo_draw_vs_pot", "听牌赔率", "Jh Qh", "BB", SRP_BB + ["翻牌 我方让牌", "翻牌 对手下注到 10"], "9h Th 3c",
      ok=["跟注", "加注", "全下"], bad=["弃牌"], why="同花+两头顺听（约15outs，胜率>50%），绝不弃"),
    C("turn_gutshot_vs_pot", "听牌赔率", "Jc Td", "BB",
      SRP_BB + ["翻牌 我方让牌", "翻牌 对手下注到 6", "翻牌 我方跟注到 6", "转牌 我方让牌", "转牌 对手下注到 22"], "Kc 9d 4s 2h",
      ok=["弃牌"], why="卡顺 4outs≈9%，面对满池需 33%，弃"),
    C("turn_oesd_small_bet", "听牌赔率", "Tc Jh", "BB",
      SRP_BB + ["翻牌 我方让牌", "翻牌 对手下注到 6", "翻牌 我方跟注到 6", "转牌 我方让牌", "转牌 对手下注到 4"], "8c 9d 2s Kh",
      ok=["跟注", "加注"], why="两头顺 8outs≈17%，面对 1/5 池只需 14%，跟"),

    # ---------------- 对手画像（只有 session prompt 有画像） ----------------
    C("exploit_maniac_bluffcatch", "对手画像", "9c 8c", "BB",
      SRP_BB + ["翻牌 我方让牌", "翻牌 对手下注到 6", "翻牌 我方跟注到 6", "转牌 我方让牌", "转牌 对手下注到 15", "转牌 我方跟注到 15",
                "河牌 我方让牌", "河牌 对手下注到 52"], "Qs 8d 4c 2h 7s",
      ok=["跟注"], opp="maniac", modes=("session",), why="疯鱼三条街连打诈唬极多，中对抓诈唬"),
    C("exploit_nit_fold", "对手画像", "9c 8c", "BB",
      SRP_BB + ["翻牌 我方让牌", "翻牌 对手下注到 6", "翻牌 我方跟注到 6", "转牌 我方让牌", "转牌 对手下注到 15", "转牌 我方跟注到 15",
                "河牌 我方让牌", "河牌 对手下注到 52"], "Qs 8d 4c 2h 7s",
      ok=["弃牌"], opp="nit", modes=("session",), why="紧弱玩家三条街满池下注几乎全是价值，中对弃"),
    C("exploit_station_no_bluff", "对手画像", "Jh Th", "BTN",
      SRP_BTN + ["翻牌 对手让牌", "翻牌 我方下注到 6", "翻牌 对手跟注", "转牌 对手让牌", "转牌 我方下注到 15", "转牌 对手跟注", "河牌 对手让牌"],
      "Kd 9s 4c 2h 3d", ok=["让牌"], opp="station", modes=("session",), why="跟注站不弃牌，J 高空气不要诈唬"),
    C("exploit_station_thin_value", "对手画像", "9h 8h", "BTN",
      SRP_BTN + ["翻牌 对手让牌", "翻牌 我方让牌", "转牌 对手让牌", "转牌 我方让牌", "河牌 对手让牌"], "Kd 9s 4c 2h 3d",
      ok=["下注"], opp="station", modes=("session",), why="跟注站会用更差的牌跟，中对薄价值下注"),
]


def _stats(n, **kw):
    s = dict(n=n, vp_n=n); s.update(kw); return s


PROFILES = {
    "maniac": {
        "ptype": "疯鱼/松凶（maniac）：几乎每手入池加注，翻后连续下注、诈唬频率极高",
        "profile": "# 对手画像\n- 类型：疯鱼/松凶（maniac）\n- 翻前：VPIP 93%、PFR 80%，3bet 很多\n"
                   "- 翻后：翻牌 cbet 92%、转牌二次下注 84%，河牌经常满池甚至超池下注；摊牌时多次亮出空气或弱对\n"
                   "- 弱点：诈唬过多，面对跟注不会收手",
        "stats": _stats(150, vp=140, pfr=120, won=80, vp_won=75, net=300, btn=75, open=70, limp=3, vs_open=70, b3=35, bb_call=30, bb_fold=5,
                        f3_opp=20, f3_fold=2, b4=8, flop=120, cb_opp=60, cb=55, cb2_opp=50, cb2=42, fcb_opp=40, fcb=6, rcb=15,
                        xr_opp=30, xr=12, agg=300, calls=60, wtsd=50, wsd=18)},
    "nit": {
        "ptype": "紧弱（nit）：入池很少，翻后被动，下注基本是强牌",
        "profile": "# 对手画像\n- 类型：紧弱（nit）\n- 翻前：VPIP 30%、PFR 20%，大盲常弃\n"
                   "- 翻后：很少主动下注，面对 cbet 常弃；几次连续下注到河牌，摊牌都是两对以上\n"
                   "- 弱点：太紧，偷盲和 cbet 收益高；他的大注基本不是诈唬",
        "stats": _stats(150, vp=45, pfr=30, won=50, vp_won=28, net=-80, btn=75, open=25, limp=3, vs_open=70, b3=5, bb_call=15, bb_fold=50,
                        f3_opp=15, f3_fold=12, b4=1, flop=50, cb_opp=25, cb=12, cb2_opp=10, cb2=4, fcb_opp=25, fcb=18, rcb=1,
                        xr_opp=15, xr=1, agg=30, calls=40, wtsd=15, wsd=11)},
    "station": {
        "ptype": "跟注站（calling station）：松被动，几乎不弃牌，很少加注",
        "profile": "# 对手画像\n- 类型：跟注站（松被动）\n- 翻前：VPIP 90%、PFR 10%，常平跟\n"
                   "- 翻后：面对 cbet 只弃 8%，A 高、底对、听牌都会跟到河牌；摊牌率很高\n"
                   "- 弱点：不弃牌，诈唬无效；薄价值下注收益高",
        "stats": _stats(150, vp=135, pfr=15, won=60, vp_won=55, net=-250, btn=75, open=12, limp=60, vs_open=70, b3=2, bb_call=60, bb_fold=5,
                        f3_opp=5, f3_fold=1, b4=0, flop=130, cb_opp=20, cb=8, cb2_opp=8, cb2=2, fcb_opp=50, fcb=4, rcb=2,
                        xr_opp=40, xr=2, agg=20, calls=160, wtsd=70, wsd=28)},
}
