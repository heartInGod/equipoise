# tables/ 每桌数据目录

一张桌一个子目录。机器人真机运行时（`tlive --prompt session --table tables/<table_id>`）自动往里写；
所有对手的跨桌累计统计在全局对手库 `players/opponents.db`，和桌内画像并存。

## 目录结构

```
tables/
├── README.md                 # 本说明
├── _template/                # 模板：新桌由 bot_core.new_table() 自动复制 table.md
│   ├── table.md              # 桌信息模板
│   └── _user_template.md     # 对手画像模板（ProfileUpdater 按它生成 <opp>.md）
└── <table_id>/               # 一张桌，例如 20261029_wepoker_practice
    ├── table.md              # 桌信息：平台、级别、金额单位、Hero 昵称（session prompt 读前言 bullet）
    ├── hands.jsonl           # 本桌对局流水：每手一行，只追加（git 跟踪）
    ├── msgs.jsonl            # 本桌模型对话：每手一行 {hid,t,opp,msgs}，按 hid 与流水关联（不进 git）
    └── <opp>.md              # 该对手在本桌的画像（同一人在不同桌可以不同；更新前的旧版备份为 .md.bak）
```

## 命名

- `<table_id>`：`YYYYMMDD_<平台/级别>_<标识>`，如 `20261029_wepoker_practice`。真机没指定 `--table` 时自动建 `YYYYMMDD_auto`。
- `<opp>.md`：对手昵称，空格和特殊字符替换为 `_`；匹配时按 `name_key` 归一化（容忍 OCR 的 0/O、l/1）。

## hands.jsonl 每行字段（`session_prompt.hand_detail`）

| 字段 | 含义 |
|---|---|
| `hid` | 手牌 id：`<运行开始时间>-<4位随机>-<手序号>`；导入的回放手为 `imp-<来源文件>-<序号>`，全局唯一 |
| `t` / `n` | 结束时间 / 本次运行的第几手 |
| `opp` | 对手昵称（整局都没识别出来时为 null，不计入对手库） |
| `pos` / `hero` / `board` | 我方位置 BTN/BB、手牌、公共牌 |
| `ev` | 原始行动线 `[[街, "我方加注到 6"], ...]`（筹码单位，1BB = 2 筹码），重建统计只用它 |
| `how` / `delta` | 结束方式、我方盈亏（筹码） |
| `table` | 桌 id |
| `start_bb` | 本手开始时双方总筹码（BB） |
| `acts` | 逐个动作：`st` 街、`who` me/opp、`pos`、`act` fold/check/limp/call/bet/raise/allin、`add` 本次投入、`to` 本街累计到、`pot` 动作前底池、`left` 动作后剩余（BB） |
| `msgs` | 该手模型对话所在文件（相对仓库根，如 `tables/<table_id>/msgs.jsonl`） |
| `src` | 导入来源（仅导入的手） |

## 数据流

1. **每手结束**（后台写线程，主线程只入队）：追加 `msgs.jsonl` → 追加 `hands.jsonl` → 计数 UPSERT 进 `players/opponents.db` → 每 N 手（`--profile-every`）异步让模型更新本桌 `<opp>.md`，写完同步进库。
2. **画像优先级**：本桌 `<opp>.md` > 对手库里该对手最近一份画像（来自其他桌，标注“仅作先验”）> 无画像按 GTO 基线。
3. **统计**：prompt 里的统计块是全局累计（库里计数 + 本局），不分桌。
4. **回放**（`--replay`）不写桌目录、不写库：流水/对话写 `logs/<日期>/hands__*.jsonl`、`msgs__*.jsonl`，需要时用 `python3 tools/players.py import <文件> --table <table_id>` 导入。
5. **查看**：`players.py list | show <opp> | hands <opp> [--table X] | hand <hid> | rebuild`（rebuild 从各桌 `hands.jsonl` 全量重算计数）。
6. **偏移**：对照 `../gto.md` §8 节点表找偏离，按 `../technology.md` §6 写入画像的“偏移策略”。
