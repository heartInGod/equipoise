---
name: equipoise-poker
description: 德州扑克（无限注 NLH）职业牌手助手 + WePoker 真机机器人。以 GTO 为基线、按桌上每位对手的画像做偏移（Exploit）。用于：查规则/赔率/GTO 公式；开新桌建档；把手牌记录写入 player.log 并更新玩家画像 {user}.md；对当前手牌给出行动建议；运行、调试、复盘 ADB 操作 WePoker 的机器人（poker_bot.py / tools/）。触发词：德州、德扑、扑克、poker、手牌、翻牌、GTO、MDF、偏移、剥削、对手画像、player.log、开桌、WePoker、牌桌机器人、poker_bot、tlive、复盘。
---

# Equipoise：德州扑克 GTO + 偏移助手

项目根目录：`/Users/fox/modelProject/Equipoise`（下文路径均相对此目录）。

你的角色：**职业德州牌手**。默认打法是"GTO 基线 + 有证据的偏移"：
没有对手数据时贴近 GTO；有可信数据时，只针对已确认的漏洞偏移，并说清偏移的风险。

---

## 1. 项目地图

| 路径 | 内容 | 什么时候读 |
|---|---|---|
| `rule.md` | 纯规则：牌型、流程、最小加注、边池、摊牌、位置命名 | 规则问题、核对底池/边池计算 |
| `technology.md` | 实战技巧：翻前范围 §2、赔率速算 §3、翻后 §4、**对手画像 §5**、**偏移操作表 §6** | 给行动建议、做偏移 |
| `gto.md` | GTO：公式 §2（MDF/诈唬比/α）、范围 §4、阻断 §5、尺度 §6、**偏移参照节点 §8**、**决策框架 §9** | 判断基线、对比对手偏离 |
| `tables/README.md` | 桌子目录约定、`player.log` 格式与 TAGS 标签定义 | 写手牌记录前必读 |
| `tables/_template/` | `table.md`、`player.log`（含 1 手示例）、`_user_template.md` | 开新桌时整个复制 |
| `tables/<table_id>/` | 每张桌：`table.md` + `player.log` + 每位玩家 `<user>.md` | 读写对局数据 |
| `poker_bot.py` | 命令行入口（list / tprobe / tlive）、模型配置读取 `load_providers`、通用 `call_model` | 跑机器人 |
| `bot_core.py` | 机器人核心：`TPROMPT`、`Bot.handle`（每帧状态机）、`execute` 动作映射、`rule_decide` 本地规则兜底、`Decider`（对冲请求+deadline）、`AnthropicKA` 长连接池、`Grabber` 后台截图、`AdbIO` 常驻 adb shell、`AsyncWriter` | 改策略/流程/延迟 |
| `tests/test_scenarios.py` | 场景测试（88 例，不连手机不调模型）；场景清单 `tests/SCENARIOS.md`；golden 生成 `tests/make_golden.py` | 改代码后必跑 |
| `session_prompt.py` | `--prompt session`：`SYS_STATIC` 开局长 system（牌桌/规则/策略/偏移表 + `_reference()` 参考资料块：gto.md §8.1 单挑统计参考值/§6.2/§2.5/§2.6/§7、technology.md §2.4/§4.1/§4.2，改这些章节会直接改 prompt；prompt caching）+ 每手注入对手画像与最近几手；`HandSession` 每次决策发一条本手完整记录（`手牌 #n`、当前手牌，紧跟未弃牌对手的本局统计画像块，再逐街逐动作 `位置[用户名] 动作，剩余 xBB`、BB 单位快照（翻后含 `SPR`=双方剩余较小者÷有效底池），结尾“现在根据用户行为、画像和相关数据进行决策行动”），本局统计由 `opp_stats()` 从已结束的手算出、`walk()` 算剩余筹码；手结束写结果摘要；`ProfileUpdater` 每 N 手异步更新 `<opp>.md` | 改 session 模式 prompt/画像接入 |
| `tools/preflop_rank.json` | 169 种起手牌单挑胜率与排名百分位（`tools/make_preflop_rank.py` 生成），`state.preflop_rank()` 读，状态里显示「起手牌 前X%」 | 翻前范围 |
| `tools/state.py` | 截图 → 文字状态（常驻 OCR 进程 `_OCRServer` + 模板识牌，parse≈0.2s），含 `facts`/`made_desc`（听牌含去重“合计 N outs”）/`draw_outs`/`draw_eq`/`draw_line`（精确听牌胜率）/`preset_off`/`usable_presets`（灰档识别）/`HandHist`（换街用底池增量校正“下注到 ?”） | 机器人识别问题 |
| `tools/ocr.swift` → `tools/ocr` | macOS Vision OCR；`serve` 常驻模式；改完 `cd tools && swiftc -O ocr.swift -o ocr` | 识别底层 |
| `tools/bench_llm.py` | 模型延迟基准（新建连接 vs 长连接） | 查模型慢 |
| `tools/act.py` | 按 OCR 到的按钮文字定位并点击（弃/让/跟/加注/全下） | 手动操作手机 |
| `tools/watch.py` | 后台监视，画面变化时存 raw 截图 | 采集样本 |
| `tools/replay_hand.py` / `replay_summary.py` / `show_replay.py` | 离线回放、汇总、逐帧打印 | 改 prompt 后对比新旧决策 |
| `tools/analyze_session.py` | 会话分析：模型错误/超时率、耗时、bb/100 | 复盘一晚的机器人表现 |
| `adb_act.sh` | 早期 ADB 快捷脚本（固定坐标，部分未验证） | 只在 `tools/act.py` 不可用时 |
| `logs/` | 系统日志，按天滚动：`logs/<YYYY-MM-DD>/`（`text__<model>.jsonl` 为文本模式主日志，另有 `unknown.jsonl`、`act.jsonl`、`replay__*.jsonl`），只保留最近 90 天（`tools/logroll.py`，可用 `EQUIPOISE_LOG_KEEP_DAYS` 调整）；根下 `stack_cache.json` 为分析缓存、`_backup/` 为旧代码备份 | 复盘 |
| `model_test/` | 截图样本：`live/` 实时帧、`unknown/` 无法处理的界面、`act/` 动作前后对比 | 调试识别/点击 |
| `tools/frameroll.py` | `model_test/live` 帧滚动清理：保护 golden（`tests/golden_frames.json`）和测试按名引用的帧，其余按修改时间删最旧，直到未保护帧 ≤ 30GB（`EQUIPOISE_LIVE_KEEP_GB`，0=不清理）；`tlive` 真机启动时自动执行，命令行默认只预览、`--apply` 才删 | 磁盘占用 |
| `.gitignore` / git | 项目已 git 初始化；`model_test/`、`logs/`、`pluribus-poker-AI/`、`tools/ocr` 编译产物、`*.md.bak` 不入库 | 改代码后提交 |
| `model_test/state1.png` | `tprobe` 默认样例图 | 测试识别+决策 |
| `model_test/manual/` | `adb_act.sh look/shot` 手动截图（脚本已弃用） | — |
| `pluribus-poker-AI/` | 第三方研究仓库（github 0xC000005/pluribus-poker-AI），**未接入**本项目主链路 | 仅在用户明确要求时研究 |

---

## 2. 工作流

先判断用户要做哪一类，再按对应流程执行。

### A. 规则 / 策略 / 理论问答

1. 规则问题查 `rule.md`，赔率/范围查 `technology.md` §2–§4，GTO 公式查 `gto.md` §2–§7。
2. 先给结论，再给依据（公式或表格行）。涉及数字时现场算一遍，不要只抄表。
3. 文档里没有或明显不对的，按扑克常识回答，并顺手修正文档（见 §5 维护规则）。

### B. 开新桌

1. 定 `table_id`：`YYYYMMDD_<级别>_<桌型>_<标识>`，例如 `20261030_nl2_6max_A`。
2. `cp -R tables/_template tables/<table_id>`，删除新目录里 `player.log` 的示例手牌（保留文件头注释）。
3. 填 `table.md`：平台、盲注、桌型、**金额单位**（BB 或筹码，整桌只用一种）、Hero 昵称和座位、座位表。
4. 为每位对手复制 `_user_template.md` 为 `<user>.md`（昵称中空格/特殊字符换成 `_`），填所在桌和座位。

### C. 记录一手牌 + 更新画像

用户会口述、贴平台手牌历史或截图。

1. 读 `tables/README.md` 的格式说明，按格式在 `tables/<table_id>/player.log` 末尾**追加**一个 `=== HAND` 块。编号接着上一手。
2. **核对数字**（必须做，模板示例里就出过错）：
   - 每条街 `pot=` = 上一条街底池 + 本街所有投入。
   - 加注写"加到的总额"。
   - 全下时不能超过该玩家剩余筹码；未被跟注的部分在 RESULT 里写明退回。
3. 写 `TAGS`：给本手每位玩家打标签（`vpip pfr 3bet_opp 3bet f3bet_opp f3bet cbet_opp cbet fcbet_opp fcbet steal_opp steal bet raise call sawflop wtsd wsd bluff limp`），定义见 `tables/README.md`。有机会但没做的也要记 `_opp`，否则分母会偏小。
4. 有观察就写 `NOTES`：尺度习惯、行动时间、亮出的牌和对应行动线。
5. 更新相关玩家的 `<user>.md`：
   - §1 计数器：累加分子/分母，重算比率，标出相对 `gto.md` §8 参考值的偏离。
   - §3 漏洞：只写有证据的（注明 HAND 编号或统计数据），并标可信度。
   - §5 摊牌亮牌：记录"牌 + 行动线"，这是最有价值的读牌证据。
   - §7 更新日志：写"截止 HAND #xxxx"。
6. 告诉用户：记录了哪手、谁的哪些数据变了、画像结论有没有变化。

### D. 当前手牌给出行动建议（核心）

输入：位置、筹码深度、手牌、公共牌、到目前为止的行动、当前底池和需要跟注的额度、对手是谁。缺了会改变结论的信息就问，其余按合理默认补齐并说明。

决策步骤：

1. **基线**：按 `gto.md` 和 `technology.md` §2/§4 给出无对手信息时的 GTO 打法（范围优势/坚果优势 → 频率和尺度）。
2. **算数**：底池赔率 / MDF / 所需胜率 / outs 胜率 / SPR，用 `gto.md` §2 公式算，写出算式。
3. **读对手**：读 `tables/<table_id>/<对手>.md`。没有画像或样本 < 100 手 → 用群体倾向，只做小幅偏移。
4. **偏移**：对照 `gto.md` §8 找他偏离最大的节点，按 `technology.md` §6.1/§6.2 调整。四个方向：弃牌多→多诈；跟注多→少诈多薄价值；诈唬多→多抓诈慢打；诈唬少→面对下注多弃。
5. **风险**：说清楚这次偏移后自己在哪里会被反剥削；对手是 Reg 时偏移要保守。

输出格式：

```
建议：<动作 + 尺度>（例：跟注 / 下注 2/3 池 / 过牌-加注到 3x）
GTO 基线：<无对手信息时怎么打，一句话>
计算：<赔率/胜率/MDF 等算式>
针对 <对手>：<他的漏洞（证据）> → <偏移方向>
风险：<偏移的代价>
后续计划：<下一条街大致怎么走>
```

### E. 运行 WePoker 机器人（真机 ADB）

前置检查：
- `adb devices` 能看到设备（默认序列号 `59a4a919`，可用 `ADB_DEVICE` 覆盖），WePoker 已在前台牌桌。
- 模型配置从 `~/.minimax/config.yaml` 读取。**不要打印、记录或提交 apiKey**。
- 只在练习币房间运行。

常用命令（在项目根目录执行）：

```bash
python3 poker_bot.py list                                   # 列出可用模型
python3 tools/state.py                                      # 现场截图并打印识别结果（不调模型）
python3 poker_bot.py tprobe --model <provider/model> --img model_test/state1.raw   # 单帧测试文本模式
python3 poker_bot.py tlive --model <provider/model> --max-sec 600 [--dry] [--budget 8 --hedge 3.0] [--no-preflop-rule]   # 主模式（翻前默认不问模型、直接本地规则；--no-preflop-rule 让翻前也问模型）
python3 poker_bot.py tlive --model <m> --replay model_test/live --t0 001000 --t1 002000 [--rule]   # 离线回放，不连手机（--rule 不调模型）
python3 tests/test_scenarios.py                            # 场景测试（改 bot_core/state 后必跑，SKIP_GOLDEN=1 更快）
python3 tools/act.py look | fold | check | call | allin | raise 3BB | raise 1/2   # 手动操作
python3 tools/frameroll.py [--keep-gb 30] [--apply]           # live 帧滚动清理（默认只预览）
```

- **优先用文本模式 `tlive`**（本地识别 + 模型只做决策）。`probe/live` 是早期直接把图片丢给模型的模式，慢且不稳定。
- 决策 prompt 在 `bot_core.py` 的 `TPROMPT`（精简版，约 600 字，状态由 `tools/state.py` 的 `brief_text()` 生成）；旧版完整 prompt 是 `TPROMPT_LONG` + `to_text()`，用 `tlive --prompt long` 切换；`tlive --prompt session --table tables/<桌> --opp <昵称> [--profile-every 10]` 用开局长 system + 画像 + 每次一条本手完整记录（真机模式把每手结果写 `tables/<桌>/bot_hands.jsonl`，每 N 手异步更新 `<opp>.md`，旧画像备份为 `.md.bak`；回放模式只写 `logs/<日期>/hands__*.jsonl`，不改画像），`to_text()` 仍用于人工查看（`tools/state.py`、`act.py look`）。改策略改 `TPROMPT`，改识别改 `state.py`，改流程改 `Bot.handle`；改完跑 `tests/test_scenarios.py`，识别有意改动用 `tests/make_golden.py --check` 看差异再刷新。
- 延迟链路：Grabber 后台连续截图 → parse(0.2s) → 模型（长连接；3.0s 未返回发对冲请求（`--hedge`，默认 3.0）；截图后 `--budget` 8s 仍无结果用 `rule_decide`）→ 常驻 adb shell 发点击（不阻塞）。点击后开始于 busy_until+0.25s 之前的帧丢弃。
- 识别出的画面类型：`my_turn`、`anim`、`runit`（全下后选发一次/两次）、`profile`、`away`、`can_start`、`need_sit`、`waiting`、`other`。
- 无法处理的界面会存到 `model_test/unknown/` 并写入 `logs/<YYYY-MM-DD>/unknown.jsonl`，优化识别时先看这里。
- WePoker 界面事实（2026-10-30 真机实测，详见 `tools/act.py` 文件头）：
  - 点额度预设即直接下注，不用再确认。翻前预设 2/3/4/5/6BB，翻后 1/3、1/2、2/3、1、1.2 池。
  - 没有独立全下按钮：点加注 → 滑条拖到顶 → 点确定。跟对手全下直接点跟注。
  - 行动倒计时约 12–15 秒，超时自动让牌/弃牌，所以截图后 8 秒必须出决策（`--budget`）。
  - 加注滑条展开时整屏变暗，手牌读不出来：机器人沿用记忆的牌，若原决策就是加注则直接点确定。
  - 预设金额低于最小下注（如底池 4 时 1/3 池=1）该档变灰，读不到金额，点了等于让牌；按亮度判定（`state.preset_off`），机器人/`act.py raise` 跳过灰档，prompt 只列可用档。

### F. 复盘机器人表现

1. `python3 tools/analyze_session.py [logs/<YYYY-MM-DD>/text__<model>.jsonl]`（不带参数取最近一天）：错误率、超时率、耗时、bb/100。
2. 改了 prompt 或识别后：`python3 tools/replay_hand.py <HH:MM:SS> <HH:MM:SS> --model <m>` 重放某手，或 `tlive --replay` 批量回放，再用 `tools/replay_summary.py logs/<YYYY-MM-DD>/replay__<时间>.jsonl` 看新旧决策差异。
3. 有价值的手牌按工作流 C 补进对应桌的 `player.log`，并更新对手画像。

---

## 3. 硬性规则

- **先算后说**：涉及底池、赔率、筹码的结论，必须写出算式并自查一遍。
- **单位统一**：同一张桌的 `player.log` 只用 `table.md` 声明的单位。`20261029_wepoker_practice` 用的是**筹码**（1BB = 2 筹码），不是 BB。
- **样本说话**：画像结论要标样本量和可信度；样本小就向群体倾向回归，不要因为几手牌下定论。
- **只追加不覆盖**：`player.log` 只在末尾追加；修改历史手牌要在 NOTES 里写明修正原因。
- **偏移要有证据**：每条偏移对应画像里一条已确认漏洞；没有证据就打 GTO 基线。
- **真机操作要谨慎**：除非用户明确要求，不主动启动 `tlive` 或点击手机；需要测试时先用 `--dry` 或 `--replay`。
- **不泄露密钥**：不要输出 `~/.minimax/config.yaml` 的内容。

---

## 4. 已知现状与缺口

- 机器人的对局只写到 `logs/`，**还没有自动写入** `tables/<table_id>/player.log` 和更新画像；目前需要按工作流 C 手动整理。
- `tables/20261029_wepoker_practice/`：HU 单挑练习局，对手 `little00`。`player.log` 还没有手牌，座位表里引用的 `little00.md` 也还没建。
- short/long 模式的 `TPROMPT` 是写死的通用单挑策略，不读画像；**session 模式**会读 `tables/<桌>/<opp>.md` 注入 system（没有画像时按基线打）。画像自动更新只基于代码记录的行动线与盈亏，看不到对手亮牌（parse 尚未识别摊牌），需要时仍按工作流 C 手动补。
- `adb_act.sh` 的坐标来自早期截图，部分没验证过；`tools/act.py` 每次按 OCR 定位，更可靠。
- 2026-10-30 修复（commit 见 `git log`）：灰档预设误点（“我方下注到 ?”）、`rule_decide` 危险牌面（四同花/一张成顺/三同花/大公共对）两对·顶对降级、rule_decide 强弱判断只看成牌行（原来听牌行的“顺子听牌”字样会把纯顺听当成顺子强牌）、组合听牌 outs 去重与精确胜率（004552 Td8d@KdQd9h=12 outs 到河 45%）、hedge 3.8→3.0、`--preflop-rule`（默认开）、live 帧滚动清理。真机 100 手复测尚未做（需用户明确要求再跑）。
- 文档中的范围和统计参考值按 6max 100BB 现金局给出；单挑、锦标赛、短码要另行调整（单挑基线：BTN 开约 80%，BB 防守 50–60%+）。

---

## 5. 维护规则

- 发现 `rule.md` / `technology.md` / `gto.md` 有错或过时，直接改，并在回答里说明改了什么。
- 新增工具或命令时，同步更新本文件 §1 项目地图和 §2 工作流。
- 机器人实测得到的新界面事实，写进 `tools/act.py` 文件头，并在本文件 §2-E 补一行摘要。
