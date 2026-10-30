# tables/ 对局数据目录

每张桌子一个子目录，存放该桌的手牌记录和每位玩家的画像。

## 目录结构

```
tables/
├── README.md                 # 本说明
├── _template/                # 模板，新开一张桌子时整个复制
│   ├── table.md              # 桌子基本信息
│   ├── player.log            # 手牌行动记录（格式示例）
│   └── _user_template.md     # 玩家画像模板
└── <table_id>/               # 一张桌子，例如 20261029_nl2_6max_A
    ├── table.md
    ├── player.log            # 本桌所有手牌，按时间追加
    ├── <user>.md             # 每位玩家一份画像，文件名 = 玩家昵称
    └── ...
```

## 命名规则

- `<table_id>`：`YYYYMMDD_<级别>_<桌型>_<标识>`，如 `20261029_nl2_6max_A`、`20261030_mtt_9max_main`。
- `<user>.md`：用玩家昵称，空格和特殊字符替换为 `_`，如 `Bob_88.md`。
- 自己（Hero）也可以建画像，用于复盘自身漏洞。

## 工作流程

1. **开桌**：复制 `_template/` 为 `<table_id>/`，填写 `table.md`。
2. **记录**：每打完一手，按 `player.log` 格式追加一个 HAND 块。
3. **更新画像**：根据新手牌更新对应 `<user>.md` 的计数器、统计数据和观察。
4. **偏移**：对照 `../gto.md` §8 节点表找偏离，按 `../technology.md` §6 写入画像的"偏移策略"。
5. **换桌 / 遇到老对手**：同一玩家在多张桌出现时，在画像里注明其他桌的文件路径，合并参考。

## player.log 格式说明

每手牌一个块，以 `=== HAND` 开头，以空行结束。

| 行 | 含义 | 必填 |
|---|---|---|
| `=== HAND #<编号> \| <时间> \| <级别> \| BTN=S<n> ===` | 手牌头 | 是 |
| `SEATS:` | 座位、昵称、起始筹码（BB 或金额，全桌统一） | 是 |
| `HERO:` | 自己的座位和手牌 | 是 |
| `POST:` | 盲注、前注、straddle | 是 |
| `PREFLOP:` | 翻前行动，按顺序，分号分隔 | 是 |
| `FLOP [..] pot=..:` | 翻牌及行动 | 到达才写 |
| `TURN [..] pot=..:` | 转牌及行动 | 到达才写 |
| `RIVER [..] pot=..:` | 河牌及行动 | 到达才写 |
| `SHOWDOWN:` | 亮出的手牌和成牌 | 有摊牌才写 |
| `RESULT:` | 赢家和赢得金额 | 是 |
| `TAGS:` | 本手牌触发的统计标签，供更新画像 | 建议 |
| `NOTES:` | 读牌、时间、尺度习惯等观察 | 可选 |

### 行动写法

```
S<座位>(<位置>) <动作> [金额]
```

- 动作：`fold` `check` `call <x>` `bet <x>` `raise <x>`（加注到的总额）`allin <x>` `post <x>` `muck` `show`
- 金额统一用 **BB** 或统一用**筹码数**，一张桌子内不要混用（在 `table.md` 中声明）。
- 牌面写法：点数 `A K Q J T 9..2` + 花色 `s h d c`，如 `Ah Kd`、`[Ks 7d 2c]`。
- 位置：`UTG HJ CO BTN SB BB`（9 人桌加 `UTG1 UTG2 LJ`）。
- 未知手牌写 `[?? ??]`；只亮一张写 `[Ah ??]`。

### TAGS 标签（用于累计画像统计）

格式 `S<座位>:<标签>`，逗号分隔。常用标签：

| 标签 | 含义 | 对应统计 |
|---|---|---|
| `vpip` | 主动入池 | VPIP |
| `pfr` | 翻前加注 | PFR |
| `3bet_opp` / `3bet` | 有 3-bet 机会 / 做了 3-bet | 3-bet% |
| `f3bet_opp` / `f3bet` | 面对 3-bet / 弃牌 | Fold to 3-bet |
| `cbet_opp` / `cbet` | 有 C-bet 机会 / 做了 C-bet | C-bet% |
| `fcbet_opp` / `fcbet` | 面对 C-bet / 弃牌 | Fold to C-bet |
| `steal_opp` / `steal` | 后位有偷盲机会 / 偷盲 | ATS |
| `bet` `raise` `call` | 翻后下注、加注、跟注次数 | AF = (bet+raise)/call |
| `sawflop` | 看到翻牌 | WTSD 分母 |
| `wtsd` / `wsd` | 到达摊牌 / 摊牌赢 | WTSD / W$SD |
| `bluff` | 摊牌确认的诈唬 | 诈唬倾向 |
| `limp` | 平跟入池 | 被动倾向 |
