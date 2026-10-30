# 机器人场景清单与测试覆盖

运行：`python3 tests/test_scenarios.py`（约 40s，不连手机、不调真实模型）；`SKIP_GOLDEN=1` 跳过真实截图（约 10s）。
识别逻辑有意改动后：`python3 tests/make_golden.py --check` 看差异 → 人工确认 → `python3 tests/make_golden.py` 刷新。

| # | 场景 | 机器人应做 | 测试 |
|---|------|-----------|------|
| **界面** ||||
| 1 | 我的回合（弃牌 + 让/跟 + 下注/加注 稳定在底部） | 问模型 → 执行 | TestMyTurnFlow.test_decide_and_execute，golden |
| 2 | 行动按钮弹出动画（按钮缩小、偏中间，弃牌 w<90 或 x 不在 320–430） | 等下一帧，不点（坐标不可信） | TestScreens.test_anim_waits，golden（003720/003809/000017/004801） |
| 3 | 对手回合（只有灰色 让或弃 / 让牌） | wait | test_waiting_and_opp_turn_wait，golden |
| 4 | 等人（邀请好友） | wait | 同上，golden 004017 |
| 5 | 可以开局（开始游戏） | 点开始游戏 | test_can_start，golden 004031 |
| 6 | 误点头像弹出资料卡（总手数/入池率/摊牌胜率） | 点 (720,300) 关闭 | test_profile_closes，golden 004351/allin_done |
| 7 | 超时被判离开（回到牌桌） | 点回到牌桌 | test_away_back_to_table，golden 003711 |
| 8 | 双方全下后选发牌次数 | 我方胜率 >50% 发两次，否则/读不到 直接发牌 | test_runit_*（领先/落后/未知），golden 4 帧 |
| 9 | 未知界面且 25s 不变 | 存图到 model_test/unknown，若有 关闭/知道了/取消/继续/确定 点一次；同一画面只处理一次 | test_stuck_other_saves_once_and_taps_close |
| 10 | 系统浮层（如 MIUI 截图预览）挡住，点击无效 | 同一画面最多重试 3 次，之后不再点 | test_retry_when_tap_not_effective_then_cap（来源：00:59 连续 8 次重试） |
| **我的回合细节** ||||
| 11 | 刚点完 1.2s 内画面未刷新 | 不重复点 | test_no_double_tap_right_after_action |
| 12 | 点击没生效（画面/金额不变 >2.5s） | 按原决策用新坐标重试，不再调模型 | test_retry_…，HandHist.test_tap_not_effective_undo |
| 13 | 对手再加注后按钮相同但金额不同 | 视为新局面，重新问模型 | test_new_situation_same_buttons_calls_model_again |
| 14 | 牌点数未识别（?） | 等下一帧，超过 2s 照常决策 | test_unknown_card_waits_then_proceeds |
| 15 | 点预设后仍在我的回合 | 补点 确定/确认 | test_preset_then_still_my_turn_confirms |
| 16 | 加注滑条展开（整屏变暗，读不到牌；来源 00:43:39/00:45:18 模型拿空手牌决策） | 若刚决定加注 → 直接点确定；否则沿用记忆的本手牌问模型 | test_slider_dim_after_raise_taps_confirm / test_slider_dim_uses_remembered_cards |
| 17 | --dry | 任何界面都不点 | test_dry_mode_never_taps |
| **动作映射** ||||
| 18 | 下注/加注 指定预设名（含"池"后缀） | 点该预设 | TestExecute.test_preset_by_name / _with_pool_suffix |
| 19 | 要 xBB 但预设是底池比例（面对加注时） | 按金额取最接近预设 | test_bb_size_when_presets_are_pot_fractions |
| 20 | size 非法 | 取中间档预设，绝不单点加注 | test_illegal_size_uses_middle_preset |
| 21 | 没有预设 | 点加注+确定一条命令完成（最小加注） | test_raise_without_presets_is_atomic_min_raise |
| 22 | 滑条已展开 | 点确定 | test_slider_already_open |
| 23 | 全下：有 All in 按钮 / 没有 | 点 All in / 加注→拖滑条到顶→确定（一条命令） | test_allin_button / test_allin_via_slider |
| 24 | 要跟注但只有 All in（对手下注 ≥ 我方筹码） | 点 All in（旧版会兜底弃牌） | test_call_when_only_allin |
| 25 | 要让牌但只能跟注 | 不自动跟注 → 弃牌 | test_check_never_becomes_call |
| **模型延迟 / 异常**（时间 1/10 缩放，硬上限 12s） ||||
| 26 | 模型 3s 正常 | 单请求 | TestLatency.test_fast_3s |
| 27 | 模型 6s：对冲请求更快 / 不更快 | 3.8s 发第二个请求，先到先用 | test_slow_6s_hedge_wins / _no_faster_hedge |
| 28 | 模型 9s / 15s / 挂死 | 截图后 8s 本地规则出手 | test_slow_9s_deadline_rule / test_hang_15s / test_real_scale_hang（真实时间） |
| 29 | SSL 握手超时等报错 | 立刻发对冲请求 | test_error_then_hedge_immediately |
| 30 | 两个请求都报错 / 非 JSON / 非法 act | 立刻本地规则 | test_both_error_rule_fast / test_bad_json / test_illegal_act |
| **牌局知识** ||||
| 31 | 行动历史：开局→3bet→4bet、换街、新手牌重置、点击无效撤销 | 文字正确 | TestHandHist |
| 32 | 牌力：顶对/超对/两对/公共牌对子/暗三条/同花听(坚果/非坚果)/两头/卡顺/顺子/公共牌成顺/同花/葫芦 | 文字正确 | TestMadeDesc（15 例） |
| 33 | 本地规则兜底 | 免费让牌；premium 加注；垃圾牌弃；强牌 2/3 池；按 outs 和所需胜率跟注；公共牌对子不算强牌 | TestRules |
| **基础设施** ||||
| 34 | 截图线程：拿到更新的帧；环形覆盖不影响已归档帧（hardlink） | | TestInfra |
| 35 | 日志异步写入不阻塞 | 200 行 <50ms | test_async_writer |
| 36 | 识别回归（132 帧真实截图）+ parse <0.6s | | TestGoldenFrames |
| 37 | ops.log 标准日志格式 / 级别 / 未捕获异常 | | TestLogroll |
| 38 | 模型评测用例自洽（正确动作可执行、prompt 含手牌/画像）、判分归一化（跟注即全下、翻后“加注”=下注、面对下注“让牌”=兜底弃牌） | 模型真实正确率用 `tools/model_eval.py` 另跑（调模型） | TestModelEval |

## 尚未覆盖（需要真机）
- 截图提速（设备端裁剪 / 压缩）与常驻 `adb shell` 点击：手机连上后先跑 `AdbIO()._probe()` 和截图计时。
- 网络断线、手机掉线重连（Grabber 会每 1s 重试，未实测）。
