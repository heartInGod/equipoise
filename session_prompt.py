"""session 模式 prompt：开局一次长 system（背景/规则/盲注/策略，可缓存）+ 每手注入对手画像与近况 + 每次决策一条本手完整记录。

请求格式（交给 AnthropicKA / generic_model）：
    {"system": [静态块(cache_control), 动态块], "messages": [user, assistant, ..., user]}
- 静态块：整局不变，走 prompt caching（实测 5k token 命中缓存后耗时与短 prompt 相同）。
- 动态块：每手开始时刷新一次（对手画像：本桌 tables/<桌>/<opp>.md 优先，其次对手库里最近一份 + 最近几手结果摘要），手内不变。
- messages：只有一条 user。每次决策发本手完整记录（手牌编号、当前手牌、未弃牌对手画像、逐个动作：位置[用户名] 动作，剩余 BB）
  + 当前局面快照；不带之前的模型回复（我方动作已在行动线里），每次都以代码记录和当前帧为准。
- 手结束（检测到新手牌）时生成结果摘要（行动线、盈亏、结束方式）。后台线程把这一手写进本桌对局流水 tables/<桌>/hands.jsonl
  （每手一行，含逐动作明细，hid 唯一），模型对话另写本桌 tables/<桌>/msgs.jsonl（同一个 hid，流水里 msgs 字段指向该文件），
  计数 UPSERT 进全局对手库 players/opponents.db，并可触发 ProfileUpdater 异步更新本桌画像（不在决策路径上，不受倒计时约束）。
"""
import json, os, queue, random, re, secrets, threading, time
from pathlib import Path

import state as wpstate
import table as T

SYS_STATIC = """你是无限注德州扑克职业牌手（2~8 人桌，单挑和多人桌都打），正在 WePoker 手机 App 上用练习币实战，目标是长期赢最多筹码。
每到我方行动，代码会把截图识别成文字发给你；你只负责决定动作，点击由代码完成。

## 牌桌
{table}

## 规则要点
- 位置：多人桌翻前顺序 UTG → UTG+1 → MP → HJ → CO → BTN → SB → BB，翻后 SB 先、BTN 最后；单挑时按钮位=小盲(0.5BB)，翻前先行动、翻后最后行动，大盲翻后先行动。
- 金额单位一律是 BB（大盲，1BB=2 筹码）。
- 「可选」里 加注/下注[...] 的档位是界面预设按钮（xBB 档=加注到的总额，1/3池、1/2池… 是底池比例）；只能从中选，没有合适档位就选最接近的。
- 「全下N」= 押上我方全部筹码 N；跟注额 ≥ 我方筹码时跟注即全下。
- 每条消息是本手到此刻的完整记录：手牌编号、当前手牌，然后是每个未弃牌对手的画像（统计 次数/机会=频率 + 一句简述）、
  代码给的「对手翻前范围」、每条街每个动作（位置[用户名] 动作，动作后剩余筹码），最后是「当前局面」快照。
- 所有数字（剩余筹码、底池、SPR、所需胜率、听牌胜率、起手牌排名、对随机牌/对范围胜率、牌力、统计、范围百分比）都由代码算出，直接采用，不要自己重算或怀疑。
- 多人桌「本街 最高注」= 本街目前最大的一注；「身后未行动」= 我方动作后还要表态的人（他们还可能加注，跟注前要考虑被挤压）。
- 统计看不到对手在按钮位直接弃牌的手，开池率可能偏高；样本少时统计只作参考。
- 收到消息就说明本手还在进行、轮到我方行动（之前的点击可能没生效），必须从「可选」里选一个动作。

## 策略基线（GTO 近似，再按对手画像偏移）
翻前：按代码给的「我方位置基线」（已按桌上人数和位置算好的前X%，X越小越强）和「起手牌 前X%」比较，再按对手画像偏移：
- 混合只发生在基线边界附近（相差约 5 个百分点以内）；「起手牌 前X%」明显比基线差的是纯弃牌，明显好的是纯加注/继续。
- 首次入池：只开池加注不平跟（limp），位置越靠前越紧；前面有人平跟：强牌加注孤立，其余能免费就让牌。
- 面对开局加注：3bet 两极化（价值 + A5s 类阻断诈唬）；冷跟只在有位置、牌可玩性好时，前面已有人跟注时挤压（squeeze）更紧；大盲有赔率优势，防守最宽。
- 面对 3bet / 4bet：按基线的 4bet / 继续比例，被压制的高张（KJo、AJo 对前位 3bet）弃。
- 翻前加注尺度：3bet 有位置约为开池的 3 倍、无位置约 4 倍，每多一个已跟注者再加约 1 个开池；4bet 有位置约为对方 3bet/squeeze 的 2.2–2.5 倍、无位置约 2.5–3 倍；翻前面对加注时「可选」档位后括号里是「对方本街注额的倍数」，按倍数对照上面的基线取最接近的档位（宁大勿小），不要按池分数凭感觉选；加注后剩余筹码不到底池时直接全下。多人桌翻前判断有无位置以代码给的「翻后行动顺序」为准。
- 面对全下或超过 5BB 的大额加注：看代码给的「翻前面对大注 对其范围胜率 vs 所需胜率」；多人在局时实际胜率更低，要留余量。
翻后：
- 顶对及以上价值下注/加注约2/3池；≥8 outs 强听牌可半诈唬 1/3~1/2 池。
- 翻前加注方在干燥牌面可小注持续下注；中等牌力过牌控池；无牌力无听牌放弃。
- 面对下注：用「听牌胜率」或我方对他剩余范围的胜率对比「所需胜率」；后门听牌不算。
- 「听牌胜率」已按去重 outs 精确计算，直接用；只有公共牌成对时同花/顺子听要明显打折（对手可能已成葫芦），非坚果同花听只需小幅打折。
- 翻牌圈 12+ outs（同花+顺子组合听）面对全下，到河牌胜率接近所需胜率时倾向跟注。
多人底池（翻后 ≥3 人在局，或翻前身后还有多人未行动）：
- 诈唬成功要所有人都弃，成功率按人数相乘下降 → 纯诈唬大幅减少，半诈唬只用强听牌；价值下注门槛提高（两人以上跟注时顶对弱踢脚多为控池）。
- 面对下注 + 加注（或下注 + 跟注后再有人加注）：范围远强于单人下注，中等牌基本弃；要考虑身后还没表态的人。
- 偏移以池里「最弱一环」为主要目标（池里有跟注站 → 整体减少诈唬，价值加宽）。
偏移：只在「对手画像」有证据的漏洞上偏移（统计对照下面「统计参考值」判断偏高/偏低），样本少（<100 手）时只做小幅偏移、贴近基线。

## 对手范围推演（每次决策的第一步，必须先做）
按范围思考，不按单手牌：对手不是“一手牌”，而是一个牌的集合；他的每个动作都会删掉其中一部分。按街从宽到窄推，再拿我方牌去比。多人桌对每个未弃牌对手分别推，重点是最后进攻的人。
1. 翻前起点：直接用代码给的「对手翻前范围（表）」——已按桌上人数×位置给出基线，并用他的开池/3bet/跟注统计做了收缩（样本少时贴近基线）。含义：
- 开局加注：线性（含全部强牌），位置越靠前越窄；开局尺度明显偏大且画像显示他尺度随牌力变，偏强。
- 平跟(limp)：多为弱到中等牌（强牌通常加注）；被动型对手 limp 更宽、偶尔慢打大牌。
- 大盲跟注 / 冷跟：封顶（AA/KK/QQ/AK 大多已 3bet），中等牌、同花连张、小对子多。
- 3bet / squeeze：两极化；跟 3bet：封顶的中强牌；4bet 及以上：QQ+/AK 为主。
2. 翻后逐个动作删牌（先看牌面对他范围的命中，见「读牌面」：高张/连张/同花面对谁更有利）
- 小注（≤1/2池）：线性、范围宽，中等牌/听牌/空气都有；cbet 频率高的对手更宽。
- 大注（≥2/3池）/超池：两极化 = 强成牌 + 听牌/空气，中等牌少；AF 低、很少下注的人大注偏强（但不是只有坚果，见下「行动线冲突与封顶」）。
- 过牌：翻前加注者过牌 → 顶端被削弱，多为中等牌或放弃牌（留意慢打）；有位置过牌回(check back) → 封顶，几乎没有强价值牌。
- 跟注：中等成牌 + 听牌，去掉了空气，也基本去掉坚果（坚果多会加注）；连续跟两条街 → 以中等对子、强听牌为主。
- 过牌-加注 / 加注：两对以上 + 强听牌半诈唬；越晚的街诈唬越少，河牌加注以强牌为主。
- 转牌/河牌完成听牌（第三张同花、连成顺子）：他前面一路跟注的听牌现在成了；他前面跟注、这时突然领打大注，多半成了。
- 河牌：他前面跟注的听牌没中的部分只能弃或诈唬，所以下注范围 = 价值 + 没中的听牌；过牌范围多为摊牌价值牌。
3. 画像校正：诈唬多（AF 高、cbet/二次下注高、河牌主动下注多）→ 下注范围里空气更多；跟注多（WTSD 高、fold to cbet 低）→ 跟注范围更宽、含更弱的牌；样本少按基线。
4. 定位我方：我方牌在他剩余范围中领先哪些、落后哪些、大约各占多少，再定动作。拿「所需胜率」去比的是我对他**剩余范围**的胜率，不是牌力本身（顶对若在他剩余范围里大多落后，赔率再好也弃）；动作必须和你写的范围结论一致：
- 价值下注：他的跟注范围里有足够多比我差的牌（对封顶范围可薄价值）；
- 诈唬：他的范围封顶/弱牌多且会弃，优先用阻断他跟注牌、不阻断他弃牌的牌；多人底池少诈唬；
- 控池（过牌/跟注）：领先他的弱牌、落后他的强牌，下注时更差的不跟、更好的不弃。

## 抓诈：由你权衡，不是死规则
面对下注时要不要用中等牌抓诈，综合下面几项自己判断，并在 range 里写出结论：
- 他这条行动线下注范围里诈唬（没中的听牌、空气、被反超的牌）大约占多少，对比「所需胜率」/ MDF（尺度越大，他需要的价值比例越高）；
- 统计证据和样本量：河牌主动下注率、AF、摊牌亮出过什么；样本少时向基线回归，不要因一两手就极端偏移；
- 我方牌的阻断：是否阻断他的价值牌（好），是否阻断他的诈唬牌（坏）；
- 剩余对手数：多人底池、身后还有人没表态时，抓诈价值明显下降；
- 我方牌在他范围里的位置：能赢他全部诈唬、还能赢一部分薄价值的牌最适合抓诈。

## 行动线冲突与封顶（规则写在这里，局面里出现时由你判断）
- 范围要前后一致：他每条街的动作都删掉了一部分牌。如果翻牌、转牌的行动已经把他的顶端范围删掉（例如：翻前加注者翻牌、转牌连续过牌；有位置一路过牌回；转牌跟注后河牌才突然大注），
  那他到河牌就不可能经常持有顶端范围——此时他河牌的大注里诈唬/薄价值比例更高：**提高抓诈频率**；反过来他示弱封顶时，我方**提高诈唬（下注/加注）频率**。
- 冲突的识别：后面街的动作代表的牌力，和他前面街的动作（以及他的统计习惯）对不上 → 优先相信“前面街已删掉的牌不会回来”，只给慢打留小比例；画像显示他常慢打时才放宽。
- 被动型对手不例外：AF 低、很少加注的人突然加注/大注时，他的范围确实偏强，但不是 100% 坚果——按 GTO 基线向“更强”偏移后，**仍按频率混合**（例如主要弃牌、少量跟注抓诈），不要整类直接弃或直接跟。
- 偏移只改变频率不改变结构：对任何类型的对手，都先想 GTO 下这个局面的混合频率，再按画像证据把频率往一边推。

## 混合策略与随机数 R
- 很多局面 GTO 是混合策略（两个动作 EV 接近）。用 mix 写出你的频率，例如 "跟注60 弃牌40"、"下注1/2 30 让牌70"（下注/加注在动作后写档位名）；纯策略写 "弃牌100"。
- 代码抽随机数 R（0~99），按 mix 的书写顺序累计频率选出最终执行的动作——低频动作也会被真正执行，这正是混合策略的意义。
  所以 mix 要写你真实认为的频率：不该做的动作就写 0 或不写，不要为“稳妥”给明显错误的动作留频率。
- act 写 mix 里频率最高的动作（mix 解析不了时代码按 act 执行）。
{exploit}{reference}

## 输出
{output}"""

OUT_LIVE = """只输出一行 JSON，不要其他文字（先写对手范围，再写频率，最后给动作）：
{"range":"≤40字：按行动线推出的对手当前范围（如：封顶中对+听牌，无两对以上）","mix":"动作+频率，如 跟注60 弃牌40、下注1/2 30 让牌70；纯策略写 弃牌100","act":"弃牌|让牌|跟注|下注|加注|全下（mix 里频率最高的）","size":"下注/加注填[]里=号前的档位名，去掉“池”字，如 3BB、1/2，否则null","why":"≤20字"}"""

OUT_EXPLAIN = """只输出一个 JSON 对象，不要其他文字，字段按顺序：
{"range":"逐街推演：翻前起点 → 每条街他的每个动作删掉了什么、剩下什么（每街一句，带大致比例或代表牌）；多人桌按人分别写",
 "hero":"我方牌在他剩余范围中的位置：领先哪些、落后哪些、大约各占多少；与所需胜率对比",
 "theory":"理论依据：点名用到的 GTO / 技术手册概念及它在这里的结论（如 范围优势/坚果优势、两极化/封顶、行动线冲突、MDF/所需胜率、阻断牌、多人底池、偏移表某条）",
 "mix":"动作+频率，如 跟注60 弃牌40、下注1/2 30 让牌70；纯策略写 弃牌100",
 "act":"弃牌|让牌|跟注|下注|加注|全下（mix 里频率最高的）","size":"下注/加注填[]里=号前的档位名，去掉“池”字，如 3BB、1/2，否则null","why":"≤20字结论"}"""


def _exploit_table():
    """technology.md §6.1 + §6.2 偏移操作表（按对手类型 / 统计漏洞）"""
    p = Path(__file__).with_name("technology.md")
    if not p.exists(): return ""
    m = re.search(r"### 6\.1.*?(?=### 6\.3)", p.read_text(), re.S)
    return ("\n## 偏移操作表\n" + m.group(0).replace("### ", "")).rstrip() if m else ""


def _sec(name, start, end, drop=()):
    """从 gto.md / technology.md 截取 start..end 之间的一节；去掉分隔线、引用说明和含 drop 关键词的行"""
    p = Path(__file__).with_name(name)
    if not p.exists(): return ""
    m = re.search(start + r".*?(?=" + end + ")", p.read_text(), re.S)
    if not m: return ""
    L = [l for l in m.group(0).splitlines()[1:]          # 去掉原标题（外面另起标题）
         if l.strip() != "---" and not l.startswith(">") and not any(d in l for d in drop)]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(L)).strip()


def _reference():
    """参考资料：gto.md §3/§4.1-4.3/§5/§8/§8.1/§2.5/§2.6/§6.2/§7，technology.md §1/§2.1-2.4/§4.1-4.4/§6.4"""
    parts = [
        ("核心原则", _sec("technology.md", r"## 1\.[^\n]*\n", r"\n---|\n## 2")),
        ("开池范围参考（6max 100BB；人数不同时以代码给的「我方位置基线」为准）", _sec("technology.md", r"### 2\.1[^\n]*\n", r"### 2\.2")),
        ("3-bet", _sec("technology.md", r"### 2\.2[^\n]*\n", r"### 2\.3")),
        ("大盲防守", _sec("technology.md", r"### 2\.3[^\n]*\n", r"### 2\.4")),
        ("筹码深度", _sec("technology.md", r"### 2\.4[^\n]*\n", r"\n---|\n## 3")),
        ("统计参考值（多人桌，对照「对手画像」判断偏离方向）", _sec("gto.md", r"## 8\.[^\n]*\n", r"### 8\.1")),
        ("单挑统计参考值", _sec("gto.md", r"### 8\.1[^\n]*\n", r"\n---|\n## 9")),
        ("读牌面", _sec("technology.md", r"### 4\.1[^\n]*\n", r"### 4\.2")),
        ("SPR（代码在「当前局面」给出）", _sec("technology.md", r"### 4\.2[^\n]*\n", r"### 4\.3")),
        ("下注的目的", _sec("technology.md", r"### 4\.3[^\n]*\n", r"### 4\.4")),
        ("转牌和河牌", _sec("technology.md", r"### 4\.4[^\n]*\n", r"\n---|\n## 5")),
        ("多人底池中的偏移", _sec("technology.md", r"### 6\.4[^\n]*\n", r"\n---|\n## 7")),
        ("范围理论", re.sub(r"(?m)^### 4\.\d+ ", "#### ", _sec("gto.md", r"### 4\.1[^\n]*\n", r"\n---|\n## 5"))),
        ("阻断牌", _sec("gto.md", r"## 5\.[^\n]*\n", r"\n---|\n## 6")),
        ("无差异原则与混合策略（执行频率用代码给的随机数 R）", _sec("gto.md", r"## 3\.[^\n]*\n", r"\n---|\n## 4", drop=("手表秒针",))),
        ("翻后下注尺度", _sec("gto.md", r"### 6\.2[^\n]*\n", r"### 6\.3")),
        ("下注尺度速查（所需胜率已由代码算出；MDF / α 用来判断诈唬和防守）", _sec("gto.md", r"### 2\.5[^\n]*\n", r"### 2\.6")),
        ("公式的适用边界", _sec("gto.md", r"### 2\.6[^\n]*\n", r"\n---|\n## 3")),
        ("Solver 通用结论", re.sub(r"(?m)^\d+\. ", "- ", _sec("gto.md", r"## 7\.[^\n]*\n", r"\n---|\n## 8"))),
    ]
    body = "\n\n".join(f"### {t}\n{s}" for t, s in parts if s)
    return "\n\n## 参考资料（来自 gto.md / technology.md）\n" + body if body else ""


def _table_brief(table_dir):
    """table.md 里的前言 bullet（平台、级别、单位、Hero 昵称）"""
    if not table_dir or not (Path(table_dir) / "table.md").exists():
        return "- WePoker 练习局，盲注 1/2（筹码），单挑。"
    L = [l for l in (Path(table_dir) / "table.md").read_text().splitlines() if l.startswith("- ") and "：" in l]
    return "\n".join(l for l in L if re.match(r"- (平台|游戏|级别|金额单位|Hero)", l)) or "- WePoker 练习局，盲注 1/2（筹码），单挑。"


ROOT = Path(__file__).resolve().parent
TABLES = ROOT / "tables"            # 每桌一个目录 tables/<桌>/：table.md、hands.jsonl（对局流水）、msgs.jsonl（模型对话）、<opp>.md（本桌画像）
PLAYERS = ROOT / "players"          # 全局对手库 players/opponents.db（所有对手的累计计数 + 最近一份画像）
HIST_LIMIT = 1000                   # 热启动最多载入最近多少手
COLD_N = 30                         # 对手累计少于这么多手时附“群体参考”


def safe_name(opp):
    """昵称 -> 文件名（空格/特殊字符换成 _）"""
    return re.sub(r"[^\w.-]", "_", opp or "").strip("._") or "unknown"


def same_opp(a, b):
    """两个昵称是否同一人（按 name_key 归一化，容忍 OCR 的 0/O、l/1 混淆）"""
    return bool(a) and bool(b) and wpstate.name_key(a) == wpstate.name_key(b)


def display_name(raw, known=()):
    """OCR 原文 -> 显示用昵称：对手库里已有同键的名字就用它；否则把数字旁边的 O 改回 0"""
    for k in known:
        if same_opp(k, raw): return k
    return re.sub(r"(?<=\d)[Oo]|[Oo](?=\d)", "0", raw.strip())


def known_names(tables=None):
    """出现过的昵称（各桌画像文件名 + 各桌流水里的 opp）；无对手库时兜底用"""
    names = {p.stem for p in _profile_files(tables)}
    names |= {h.get("opp") for h in load_hands(limit=0, tables=tables) if h.get("opp")}
    return sorted(names)


def profile_path(table_dir, opp):
    """本桌画像 tables/<桌>/<opp>.md（文件名按 name_key 匹配，容忍 OCR 误差）；没有返回 None。
    同一个人在不同桌的画像各自独立；本桌没有时由调用方回退到对手库里最近一份画像"""
    if not opp or not table_dir or not Path(table_dir).is_dir(): return None
    c = [p for p in _profile_files_in(Path(table_dir)) if same_opp(p.stem, opp)]
    return max(c, key=lambda p: p.stat().st_mtime) if c else None


def hand_key(h):
    """同一手牌的去重键（回放会把同一批帧重复记录多次，时间戳不同但牌和行动线相同）"""
    return json.dumps([h.get("pos"), h.get("hero"), h.get("board"), h.get("ev")], ensure_ascii=False)


def _hand_files(tables=None):
    """各桌对局流水 tables/*/hands.jsonl（真机每手 + 导入的回放手），只用于重建对手库 / 无库时兜底 / 按 hid 查手"""
    return sorted(Path(tables or TABLES).glob("*/hands.jsonl"))


def rel(p):
    """路径 -> 相对仓库根的字符串（不在仓库里就用绝对路径）"""
    p = Path(os.fspath(p)).resolve()
    try: return str(p.relative_to(ROOT))
    except ValueError: return str(p)


def _profile_files_in(d):
    return [f for f in sorted(Path(d).glob("*.md")) if f.name not in ("table.md", "README.md") and not f.name.startswith("_")]


def _profile_files(tables=None):
    """各桌画像 tables/<桌>/<opp>.md（排除 table.md / README / _ 开头；_template 等 _ 开头的目录也排除）"""
    return [f for d in sorted(Path(tables or TABLES).iterdir()) if d.is_dir() and not d.name.startswith("_")
            for f in _profile_files_in(d)] if Path(tables or TABLES).is_dir() else []


def load_hands(opp=None, exclude=None, tables=None, limit=HIST_LIMIT):
    """跨桌读对局流水（tables/*/hands.jsonl），去重后按时间升序取最近 limit 手。
    opp 给定时只要该对手；exclude 给定时排除该对手（算群体参考）。"""
    seen, out = set(), []
    for f in _hand_files(tables):
        for l in f.read_text(encoding="utf-8").splitlines():
            try: h = json.loads(l)
            except ValueError: continue
            o = h.get("opp")
            if (opp is not None and not same_opp(o, opp)) or (exclude is not None and same_opp(o, exclude)): continue
            k = hand_key(h)
            if k in seen: continue
            seen.add(k); out.append({k2: h.get(k2) for k2 in ("hid", "t", "n", "opp", "pos", "hero", "board", "ev", "how", "delta")})
    out.sort(key=lambda h: h.get("t") or "")
    return out[-limit:] if limit else out


def profile_text(t, cap=2500):
    """画像 md 正文：去掉更新日志，截断到 cap 字"""
    t = re.split(r"\n## 7\. 更新日志", t)[0]
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    return t[:cap] + ("…(截断)" if len(t) > cap else "")


def profile_type(t):
    """画像 md 里「类型：」一行（模板占位不算）"""
    m = re.search(r"^- 类型[：:]\s*(.+)$", t, re.M)
    t = m.group(1).strip() if m else ""
    return None if not t or "/" in t else t[:30]


def _profile_brief(table_dir, opp, cap=2500, prof=None):
    """对手画像：prof=后台查好的画像（本桌 <opp>.md 优先，其次对手库里最近一份）；否则读本桌 <opp>.md；都没有时给出提示"""
    if prof: return prof
    p = profile_path(table_dir, opp)
    if not p:
        return f"对手 {opp or '未知'}：暂无画像（样本不足），按 GTO 基线打，不做偏移。"
    return profile_text(p.read_text(), cap)


def find_profile(table_dir, opp, row=None):
    """（后台线程用）对手画像 {"profile","ptype","src"}：本桌 <opp>.md 优先；没有就用对手库行里最近一份（来自其他桌，作先验）；都没有 None"""
    p = profile_path(table_dir, opp)
    if p:
        t = p.read_text(encoding="utf-8")
        return {"profile": profile_text(t), "ptype": profile_type(t), "src": "本桌"}
    if row and row.get("profile"):
        return {"profile": "（本桌还没有画像，以下是该对手在其他桌的最近一份画像，仅作先验）\n" + row["profile"], "ptype": row.get("ptype"), "src": "他桌"}
    return None


ORDER = ["翻前", "翻牌", "转牌", "河牌"]
EN = {"翻前": "preflop", "翻牌": "flop", "转牌": "turn", "河牌": "river"}
OTHER = {"BTN": "BB", "BB": "BTN"}


def bb(x):
    """筹码 -> BB 文本（1BB=2 筹码）"""
    return "?" if x is None else f"{round(x / 2, 1):g}"


def _pct(a, b):
    return f"{a}/{b}={a / b * 100:.0f}%" if b else "无样本"


# 对手统计计数器（全是可加的计数；“X_opp”=机会数，配对的计数=做了几次）。对手库 players 表每项一列，player_rates 视图换算成百分比
STAT_FIELDS = (
    ("n", "累计手数"),
    ("vp_n", "翻前有主动决策的手（VPIP/PFR 分母）"), ("vp", "主动入池 VPIP（入池率）"), ("pfr", "翻前加注 PFR"),
    ("won", "收池（对手赢下底池）"), ("vp_won", "主动入池后收池"), ("net", "对手净赢筹码（1BB=2，可为负）"),
    ("btn", "按钮位有行动"), ("open", "按钮位开池加注"), ("limp", "按钮位平跟 limp"),
    ("vs_open", "大盲面对我方开池"), ("b3", "3bet"), ("bb_call", "大盲跟注开池"), ("bb_fold", "大盲弃牌"),
    ("vs_limp", "大盲面对我方 limp"), ("iso", "对 limp 加注"),
    ("f3_opp", "开池后面对我方 3bet"), ("f3_fold", "面对 3bet 弃牌"), ("b4", "4bet"),
    ("f4_opp", "3bet 后面对我方 4bet"), ("f4_fold", "面对 4bet 弃牌"),
    ("flop", "看到翻牌"),
    ("cb_opp", "翻牌 cbet 机会"), ("cb", "翻牌 cbet"),
    ("cb2_opp", "转牌二次 cbet 机会"), ("cb2", "转牌二次 cbet"),
    ("fcb_opp", "面对我方翻牌 cbet"), ("fcb", "面对 cbet 弃牌"), ("rcb", "面对 cbet 加注"),
    ("xr_opp", "翻牌让牌后面对我方下注"), ("xr", "翻牌 check-raise"),
    ("agg", "翻后下注/加注次数"), ("calls", "翻后跟注次数"),
    ("wtsd", "到摊牌"), ("wsd", "摊牌赢（我方亏）"),
    # 以下为多人桌口径（2026-10 起；单挑手只计 ld/rv/rr）
    ("mw_n", "多人桌手数（3 人以上）"),
    ("rfi_ep_opp", "前位(UTG~MP)未入池时轮到他"), ("rfi_ep", "前位开池"), ("rfi_hj_opp", "HJ 未入池时轮到他"), ("rfi_hj", "HJ 开池"),
    ("rfi_co_opp", "CO 未入池时轮到他"), ("rfi_co", "CO 开池"), ("rfi_bt_opp", "BTN 未入池时轮到他"), ("rfi_bt", "BTN 开池"),
    ("rfi_sb_opp", "SB 未入池时轮到他"), ("rfi_sb", "SB 开池"),
    ("m3_opp", "面对开局加注（未 3bet 前）"), ("m3", "多人桌 3bet"), ("mc", "面对开局加注跟注"),
    ("mf3_opp", "开池后面对 3bet"), ("mf3", "面对 3bet 弃牌"), ("m4_opp", "面对 3bet（自己开池或冷跟后）"), ("m4", "多人桌 4bet"),
    ("m5_opp", "面对 4bet"), ("m5", "5bet/全下"),
    ("ld_opp", "翻牌在翻前进攻方之前行动且无人下注"), ("ld", "翻牌领打(donk)"),
    ("rv_opp", "河牌轮到他时无人下注"), ("rv", "河牌主动下注"), ("rr_opp", "河牌面对下注"), ("rr", "河牌面对下注加注"),
)
STAT_KEYS = tuple(k for k, _ in STAT_FIELDS)
_RAISE = re.compile(r"加注|全下|下注到")


def opp_stats(hands):
    """从已结束的手（rec: pos=我方位置, ev, board, how, delta）统计对手计数（字段见 STAT_FIELDS）。
    单挑手（np 缺省或 2）：对手=“对手”；看不到对手按钮位直接弃牌的手（这类手不进记录），所以按钮位的分母是“有行动”的手。
    多人手（np>2）：统计 opp_pos 那个位置的玩家（对手库按 seats 展开时填上）；没有 opp_pos 的多人手跳过。"""
    multi = [h for h in hands if (h.get("np") or 2) > 2 and h.get("opp_pos")]
    hands = [h for h in hands if (h.get("np") or 2) <= 2]
    S = dict.fromkeys(STAT_KEYS, 0); S["n"] = len(hands) + len(multi)
    for h in multi: _multi_counts(S, h)
    for h in hands: _late_counts(S, T.walk_full([tuple(x) for x in h.get("ev") or []], h.get("pos"), 2), "opp")
    opp_r = lambda e: e.startswith("对手") and bool(_RAISE.search(e))
    me_r = lambda e: e.startswith("我方") and bool(_RAISE.search(e))
    nxt = lambda seq, i, who: next((j for j in range(i, len(seq)) if seq[j].startswith(who)), None)
    for h in hands:
        ev, pos, board = h.get("ev") or [], h.get("pos"), h.get("board") or []
        vp0 = S["vp"]
        pre = [e for s, e in ev if s == "翻前"]
        last = ev[-1][1] if ev else ""
        folded_pre = not board and last.startswith("我方")          # 翻前以我方动作收尾 = 对手弃牌
        if pos == "BB":                                               # 对手在按钮位（先行动）
            k = nxt(pre, 0, "对手")
            if k is not None and ("平跟" in pre[k] or opp_r(pre[k])):
                S["btn"] += 1; S["vp"] += 1; S["vp_n"] += 1
                if "平跟" in pre[k]: S["limp"] += 1
                else:
                    S["open"] += 1; S["pfr"] += 1
                    j = nxt(pre, k + 1, "我方")
                    if j is not None and me_r(pre[j]):               # 我方 3bet
                        S["f3_opp"] += 1
                        r = nxt(pre, j + 1, "对手")
                        if r is None and folded_pre: S["f3_fold"] += 1
                        elif r is not None and opp_r(pre[r]): S["b4"] += 1
        elif pos == "BTN":                                            # 对手在大盲
            k = nxt(pre, 0, "我方")
            if k is not None and me_r(pre[k]):                        # 我方开池
                S["vs_open"] += 1; S["vp_n"] += 1
                r = nxt(pre, k + 1, "对手")
                if r is not None and opp_r(pre[r]):                   # 对手 3bet
                    S["b3"] += 1; S["vp"] += 1; S["pfr"] += 1
                    j = nxt(pre, r + 1, "我方")
                    if j is not None and me_r(pre[j]):               # 我方 4bet
                        S["f4_opp"] += 1
                        if nxt(pre, j + 1, "对手") is None and folded_pre: S["f4_fold"] += 1
                elif board or (r is not None and "跟注" in pre[r]): S["bb_call"] += 1; S["vp"] += 1
                elif folded_pre: S["bb_fold"] += 1
            elif k is not None and ("跟注" in pre[k] or "平跟" in pre[k]):   # 我方 limp
                S["vs_limp"] += 1; S["vp_n"] += 1
                r = nxt(pre, k + 1, "对手")
                if r is not None and opp_r(pre[r]): S["iso"] += 1; S["vp"] += 1; S["pfr"] += 1
        d = h.get("delta")                                            # 我方盈亏：负 = 对手收池
        if d is not None:
            S["net"] -= d
            if d < 0: S["won"] += 1; S["vp_won"] += S["vp"] > vp0
        agg = next((("opp" if e.startswith("对手") else "me") for e in reversed(pre) if _RAISE.search(e)), None)   # 翻前最后的进攻方
        if len(board) >= 3:
            S["flop"] += 1
            fl = [e for s, e in ev if s == "翻牌"]
            tu = [e for s, e in ev if s == "转牌"]
            bet = lambda e, who: bool(re.match(who + r"(下注|加注|全下)", e))
            def first_act(seq, who, other):
                """who 本街第一个动作（之前 other 没有下注才算有 cbet 机会）；返回 (下标, 是否有机会)"""
                i = next((n for n, e in enumerate(seq) if e.startswith(who)), None)
                return i, i is not None and not any(bet(e, other) for e in seq[:i])
            o, ok = first_act(fl, "对手", "我方")
            opp_cb = False
            if agg == "opp" and ok:                                   # 对手翻牌 cbet 机会（我方没有先下注）
                S["cb_opp"] += 1; opp_cb = bet(fl[o], "对手"); S["cb"] += opp_cb
            if opp_cb and len(board) >= 4:                           # 转牌二次 cbet
                o2, ok2 = first_act(tu, "对手", "我方")
                if ok2: S["cb2_opp"] += 1; S["cb2"] += bet(tu[o2], "对手")
            m, okm = first_act(fl, "我方", "对手")
            if agg == "me" and okm and bet(fl[m], "我方"):             # 我方 cbet
                S["fcb_opp"] += 1
                r = next((e for e in fl[m + 1:] if e.startswith("对手")), None)
                if r and bet(r, "对手"): S["rcb"] += 1
                elif r is None and len(board) == 3 and last == fl[m]: S["fcb"] += 1
            i = next((n for n, e in enumerate(fl) if e.startswith("对手")), None)     # 翻牌 check-raise
            if i is not None and fl[i].startswith("对手让牌"):
                j = next((n for n in range(i + 1, len(fl)) if re.match(r"我方(下注|加注|全下)", fl[n])), None)
                if j is not None:
                    S["xr_opp"] += 1
                    r = next((e for e in fl[j + 1:] if e.startswith("对手")), None)
                    S["xr"] += bool(r and re.match(r"对手(加注|全下)", r))
            if "摊牌" in (h.get("how") or ""):
                S["wtsd"] += 1; S["wsd"] += (h.get("delta") or 0) < 0
        for s, e in ev:
            if s == "翻前": continue
            if re.match(r"对手(下注|加注|全下)", e): S["agg"] += 1
            elif e.startswith("对手跟注"): S["calls"] += 1
    return S


def _late_counts(S, w, k):
    """单挑/多人通用：翻牌领打、河牌主动下注、河牌面对下注加注（k=对手 key）"""
    pre = [a for a in w["acts"] if a["st"] == "翻前"]
    pa = next((a["key"] for a in reversed(pre) if a["agg"]), None)        # 翻前最后的进攻方
    for st in ("翻牌", "河牌"):
        A = [a for a in w["acts"] if a["st"] == st]
        i = next((n for n, a in enumerate(A) if a["key"] == k), None)
        if i is None: continue
        bet_before = any(a["typ"] in ("bet", "raise", "allin") for a in A[:i])
        if st == "翻牌":
            if pa and pa != k and not bet_before and any(a["key"] == pa for a in A[i + 1:]):
                S["ld_opp"] += 1; S["ld"] += A[i]["typ"] in ("bet", "allin")
            continue
        if not bet_before: S["rv_opp"] += 1; S["rv"] += A[i]["typ"] in ("bet", "allin")
        j = next((n for n in range(len(A)) if A[n]["key"] == k and any(a["typ"] in ("bet", "raise", "allin") and a["key"] != k for a in A[:n])), None)
        if j is not None: S["rr_opp"] += 1; S["rr"] += A[j]["agg"]


def _multi_counts(S, h):
    """一手多人桌记录里 opp_pos 那个玩家的计数（翻前按位置开池/3bet/4bet/5bet，翻后 cbet/面对cbet/check-raise/AF/摊牌）"""
    np_, k = h.get("np"), h["opp_pos"]
    w = T.walk_full([tuple(x) for x in h.get("ev") or []], h.get("pos"), np_)
    if k == h.get("pos") or k not in w["pos_of"]: return
    S["mw_n"] += 1
    pre = [a for a in w["acts"] if a["st"] == "翻前"]
    mine = [(n, a) for n, a in enumerate(pre) if a["key"] == k]
    if not mine and (k in w["folded"] or k not in w["active"]) and k in w["keys"]:          # 没记录动作、行动已轮过他 = 弃牌：补一个虚拟弃牌，计入开池/面对开局的机会
        after = w["keys"][w["keys"].index(k) + 1:]
        n0 = next((n for n, a in enumerate(pre) if a["key"] in after), len(pre))
        v = {"st": "翻前", "key": k, "pos": w["pos_of"][k], "typ": "fold", "agg": False, "to": 0, "lv": None}
        pre = pre[:n0] + [v] + pre[n0:]; mine = [(n0, v)]
    if mine or k in w["folded"]: S["vp_n"] += 1
    if any(a["typ"] in ("call", "limp", "raise", "bet", "allin") for _, a in mine): S["vp"] += 1
    if any(a["agg"] for _, a in mine): S["pfr"] += 1
    opened = False
    for n, a in mine:
        before = pre[:n]
        lv = max([b["lv"] for b in before if b["agg"]] or [1])
        if lv == 1 and not any(b["typ"] in ("call", "limp") for b in before) and a["pos"] != "BB":
            g = T.group(a["pos"], np_); S[f"rfi_{g}_opp"] += 1; S[f"rfi_{g}"] += a["agg"]; opened = a["agg"]
        elif lv == 2 and not opened and not any(b["key"] == k and b["agg"] for b in before):
            S["m3_opp"] += 1; S["m3"] += a["agg"]; S["mc"] += a["typ"] == "call"
        elif lv == 3:
            if opened: S["mf3_opp"] += 1; S["mf3"] += a["typ"] == "fold"
            S["m4_opp"] += 1; S["m4"] += a["agg"]
        elif lv >= 4:
            S["m5_opp"] += 1; S["m5"] += a["agg"]
    pa = next((a["key"] for a in reversed(pre) if a["agg"]), None)
    board = h.get("board") or []
    alive_flop = k not in w["folded"] or any(a["key"] == k and a["st"] != "翻前" for a in w["acts"])
    if len(board) >= 3 and alive_flop:
        S["flop"] += 1
        fl = [a for a in w["acts"] if a["st"] == "翻牌"]
        i = next((n for n, a in enumerate(fl) if a["key"] == k), None)
        bet = lambda a: a["typ"] in ("bet", "raise", "allin")
        if i is not None:
            if pa == k and not any(bet(a) for a in fl[:i]):
                S["cb_opp"] += 1; S["cb"] += bet(fl[i])
                tu = [a for a in w["acts"] if a["st"] == "转牌"]
                j = next((n for n, a in enumerate(tu) if a["key"] == k), None)
                if bet(fl[i]) and j is not None and not any(bet(a) for a in tu[:j]): S["cb2_opp"] += 1; S["cb2"] += bet(tu[j])
            fb = next((a for a in fl[:i] if bet(a)), None)
            if fb and fb["key"] == pa and pa != k:
                S["fcb_opp"] += 1; S["fcb"] += fl[i]["typ"] == "fold"; S["rcb"] += fl[i]["agg"]
            if fl[i]["typ"] == "check":
                r = next((a for a in fl[i + 1:] if a["key"] == k), None)
                if r is not None and any(bet(a) for a in fl[i + 1:fl.index(r)]): S["xr_opp"] += 1; S["xr"] += r["agg"]
        for a in w["acts"]:
            if a["st"] == "翻前" or a["key"] != k: continue
            if bet(a): S["agg"] += 1
            elif a["typ"] == "call": S["calls"] += 1
        if "摊牌" in (h.get("how") or "") and k not in w["folded"]: S["wtsd"] += 1
    _late_counts(S, w, k)


def _type_line(table_dir, opp):
    """<opp>.md 里「类型：」一行（模板占位不算）"""
    p = profile_path(table_dir, opp)
    return profile_type(p.read_text()) if p else None


def opp_desc(S, table_dir=None, opp=None, typ=None):
    """一句话描述：优先用画像的类型判断（typ=对手库里查到的；否则读画像文件），没有就按统计启发式生成"""
    t = typ or _type_line(table_dir, opp)
    if t: return t
    if S["vp_n"] < 4: return "样本太少，按基线打"
    vp, pfr = S["vp"] / S["vp_n"], S["pfr"] / S["vp_n"]
    d = ("松凶，常主动抢池" if vp >= .6 and pfr >= .4 else "松被动，爱跟注" if vp >= .6 else
         "紧凶" if pfr >= .25 else "偏紧偏被动")
    if S["cb_opp"] >= 3 and S["cb"] / S["cb_opp"] >= .7: d += "，持续下注多"
    if S["fcb_opp"] >= 3 and S["fcb"] / S["fcb_opp"] >= .6: d += "，面对cbet常弃"
    if S["calls"] >= 3 and S["agg"] / S["calls"] < 1: d += "，翻后偏被动"
    elif S["calls"] >= 3 and S["agg"] / S["calls"] >= 2: d += "，翻后下注加注很多"
    if S["vs_open"] >= 3 and S["bb_fold"] / S["vs_open"] >= .6: d += "，大盲常弃"
    return ("初判（样本少）" if S["n"] < 30 else "") + d


def profile_block(S, desc, n_hist=0, pop=None):
    """手牌下方的对手画像：统计（有历史时为累计=历史+本局）+ 简短描述（后续动作行不再重复）；
    pop=群体统计（对手样本少时作冷启动参考）"""
    tail = []
    if pop and pop["n"] and S["n"] < COLD_N:
        tail = [f"  群体参考（其他对手共{pop['n']}手，该对手样本少时向它回归）："] + ["  " + l for l in _stat_lines(pop, "群体")]
    if not S["n"]: return [f"  本局还没有已结束的手，无统计；简述：{desc}"] + tail
    scope = f"累计{S['n']}手(此前{n_hist}+本局{S['n'] - n_hist})" if n_hist else f"本局{S['n']}手"
    return _stat_lines(S, scope) + [f"  简述：{desc}"] + tail


def _stat_lines(S, scope, multi=False):
    """multi=True（多人桌决策）：翻前换成多人口径（按位置开池 / 面对开局 3bet、跟注 / 面对 3bet、4bet），不写盈亏（多人手无法归属）"""
    S = dict(dict.fromkeys(STAT_KEYS, 0), **S)
    af = f"{S['agg'] / S['calls']:.1f}" if S["calls"] else "—"
    late = (f"  领打/河牌：翻牌领打{_pct(S['ld'], S['ld_opp'])}、河牌主动下注{_pct(S['rv'], S['rv_opp'])}、"
            f"河牌面对下注加注{_pct(S['rr'], S['rr_opp'])}")
    post = [f"  翻牌：cbet{_pct(S['cb'], S['cb_opp'])}、面对cbet弃{_pct(S['fcb'], S['fcb_opp'])}/加注{_pct(S['rcb'], S['fcb_opp'])}、"
            f"check-raise{_pct(S['xr'], S['xr_opp'])}、看翻牌{S['flop']}手",
            f"  转牌/河牌：转牌二次cbet{_pct(S['cb2'], S['cb2_opp'])}、翻后下注加注{S['agg']}次/跟注{S['calls']}次（AF {af}）、"
            f"到摊牌{_pct(S['wtsd'], S['flop'])}" + ("" if multi else f"、摊牌赢{_pct(S['wsd'], S['wtsd'])}")]
    if multi:
        return [f"  翻前：{scope}（多人桌{S['mw_n']}手）VPIP{_pct(S['vp'], S['vp_n'])}、PFR{_pct(S['pfr'], S['vp_n'])}、"
                f"开池 前位{_pct(S['rfi_ep'], S['rfi_ep_opp'])}/HJ{_pct(S['rfi_hj'], S['rfi_hj_opp'])}/CO{_pct(S['rfi_co'], S['rfi_co_opp'])}/"
                f"BTN{_pct(S['rfi_bt'], S['rfi_bt_opp'])}/SB{_pct(S['rfi_sb'], S['rfi_sb_opp'])}",
                f"  面对开局：3B{_pct(S['m3'], S['m3_opp'])}、跟注{_pct(S['mc'], S['m3_opp'])}；开池后面对3B弃{_pct(S['mf3'], S['mf3_opp'])}、"
                f"4B{_pct(S['m4'], S['m4_opp'])}、面对4B再加注{_pct(S['m5'], S['m5_opp'])}"] + post + [late]
    return [f"  盈亏：收池{_pct(S['won'], S['n'])}、入池后收池{_pct(S['vp_won'], S['vp'])}、对手净赢 {bb(S['net'])}BB"
            + (f"（{S['net'] / 2 / S['n'] * 100:+.0f}BB/100手）" if S["n"] else ""),
            f"  翻前：{scope} VPIP{_pct(S['vp'], S['vp_n'])}、PFR{_pct(S['pfr'], S['vp_n'])}、开池{_pct(S['open'], S['btn'])}、limp{_pct(S['limp'], S['btn'])}、"
            f"3B{_pct(S['b3'], S['vs_open'])}、4B{_pct(S['b4'], S['f3_opp'])}、大盲面对开池弃{_pct(S['bb_fold'], S['vs_open'])}、"
            f"面对3B弃{_pct(S['f3_fold'], S['f3_opp'])}、面对4B弃{_pct(S['f4_fold'], S['f4_opp'])}、对limp加注{_pct(S['iso'], S['vs_limp'])}",
            ] + post + ([late] if S["rv_opp"] or S["ld_opp"] or S["rr_opp"] else [])


def walk(ev, hero_pos, n=2):
    """按事件逐步累计投入（tools/table.walk_full 的旧接口）。返回 (steps, prior, cur, c, base)：steps=[(街, who, 动作文本(BB), 该玩家本手累计投入)]；
    prior[街]={who: 该街开始前的累计投入（筹码）}。单挑 who=me/opp；多人桌 who=me/位置名。"""
    w = T.walk_full(ev, hero_pos, n)
    return w["steps"], w["prior"], w["cur"], w["c"], w["base"]


ACT = (("弃牌", "fold"), ("让牌", "check"), ("平跟", "limp"), ("跟注", "call"), ("全下", "allin"), ("加注", "raise"), ("下注", "bet"))


def hand_detail(rec, table=None, msgs=None):
    """一手的对局流水记录（写 tables/<桌>/hands.jsonl）：hid + 原始记录（重建统计用）+ 逐个动作的位置/类型/注码（BB）；
    msgs=该手模型对话所在文件（tables/<桌>/msgs.jsonl，按 hid 关联）。
    acts[i] = {st 街, who me/opp, pos BTN/BB, act fold/check/limp/call/bet/raise/allin, add 本次投入, to 本街累计到,
               pot 动作前底池, left 动作后剩余筹码（起始筹码未知时为 None）}"""
    pos, np_ = rec.get("pos"), rec.get("np") or 2
    w = T.walk_full(rec.get("ev") or [], pos, np_)
    steps, prior = w["steps"], w["prior"]
    last = T.blinds(pos, np_)                                                # 盲注
    start = rec.get("start") or {}
    acts = []
    for (s, who, txt, tot), a in zip(steps, w["acts"]):
        street_base = (prior.get(s) or {}).get(who, 0)
        acts.append({"st": EN[s], "who": who, "pos": (pos if who == "me" else w["pos_of"].get(who)) or "?", "act": a["typ"],
                     "add": round((tot - last.get(who, 0)) / 2, 1), "to": round((tot - street_base) / 2, 1),
                     "pot": round(sum(last.values()) / 2, 1),
                     "left": None if start.get(who) is None else round(max(start[who] - tot, 0) / 2, 1)})
        last[who] = max(last.get(who, 0), tot)
    d = {k: rec.get(k) for k in ("hid", "t", "n", "opp", "pos", "hero", "board", "ev", "how", "delta")}
    if np_ > 2: d["np"], d["seats"] = np_, rec.get("seats") or {}
    if table or rec.get("table"): d["table"] = table or rec.get("table")
    if start: d["start_bb"] = {k: round(v / 2, 1) for k, v in start.items() if v is not None}
    d["acts"] = acts
    if msgs: d["msgs"] = msgs
    if rec.get("src"): d["src"] = rec["src"]
    return d


def snapshot_bb(st, hist, ctx=None):
    """当前局面快照（BB 单位），只放决策需要的代码计算结果。
    ctx（session 给）：x=翻前最后进攻者的范围前X%（翻前面对大注用）、k=仍在局的对手数（含未行动）、base=我方位置基线、
    behind=本街我方之后还没行动的对手位置、eff=有效筹码（筹码）、multi=多人桌"""
    n, b = st.get("nums") or {}, st["buttons"]
    hero, board = st["hero"], st["board"]
    ctx = ctx or {}
    L = []
    pr = wpstate.preflop_rank(hero) if not board else None
    k = ctx.get("k") or 1
    if pr:
        mw = T.mw_eq(pr[1], k) if k >= 2 else None
        L.append(f"起手牌 {pr[0]} 前{pr[1]['top']:.0f}%（单挑对随机牌胜率 {pr[1]['eq']:.0f}%"
                 + (f"；对{k}个随机对手胜率 {mw:.0f}%" if mw else "") + "）")
    if pr and ctx.get("base"): L.append("我方位置基线 " + ctx["base"])
    md = wpstate.made_desc(hero, board)
    if md: L.append("牌力 " + md[0].split(": ", 1)[1] + ("；" + md[1].replace(": ", " ") if len(md) > 1 else ""))
    pot, tc, ms, os_ = n.get("pot"), n.get("to_call"), n.get("my_stack"), n.get("opp_stack")
    if pot is not None:
        ep = wpstate.cap_call(n)[0]
        L.append(f"底池 {bb(pot)}BB（本街 {'最高注' if ctx.get('multi') else '对手'} {bb(n.get('opp_bet') or 0)} / 我方 {bb(n.get('my_bet') or 0)}）"
                 + (f"；对手超出我方筹码的部分会退回，有效底池 {bb(ep)}BB" if ep is not None and ep < pot else ""))
    if ms is not None and (os_ is not None or ctx.get("eff") is not None):
        eff = ctx["eff"] if ctx.get("eff") is not None else min(ms + (n.get('my_bet') or 0), os_ + (n.get('opp_bet') or 0))
        L.append(f"有效筹码 {bb(eff)}BB" + ("（我方 vs 在局对手中筹码最深者）" if ctx.get("multi") else ""))
        ep = wpstate.cap_call(n)[0] if pot is not None and os_ is not None else pot
        if board and ep:                       # SPR = 双方剩余较小者 / 当前（有效）底池
            L.append(f"SPR {min(ms, os_ if os_ is not None else eff) / ep:.1f}")
    if tc:
        pot, tc = wpstate.cap_call(n)          # 对手下注超过我方筹码时，多出的部分会退回，不算赔率
        need = tc / ((pot or 0) + tc) * 100
        L.append(f"需跟注 {bb(tc)}BB，所需胜率 {need:.0f}%" + ("（跟注即全下）" if ms is not None and tc >= ms else ""))
        if pr and tc > 10:
            if ctx.get("x"):                   # 用代码推出的对手范围（范围表里最后进攻者那一行）
                x = ctx["x"]; e = T.vs_eq(pr[1], x)
                L.append(f"翻前面对大注 对其范围(约前{T._fmt(x)}%)胜率约 {e:.0f}% {'≥' if e >= need else '<'} 所需 {need:.0f}%（{'够跟' if e >= need else '不够'}）"
                         + (f"；另有{k - 1}个对手在局或未行动，实际胜率更低（近似）" if k >= 2 else ""))
            else:
                lv = hist.level if hist is not None and getattr(hist, "street", None) == "翻前" else 2
                L.append(wpstate.big_line(pr[1], need, lv, (n.get("opp_bet") or 0) / 2))
    if md and len(board) in (3, 4):
        L.extend(wpstate.draw_line(md[-1], len(board)))
    opts = []
    for k in b:
        if k == "跟注": opts.append(f"跟注 {bb(tc)}BB" if tc else "跟注")
        elif k in ("All in", "ALL IN", "全下"): opts.append(f"全下 {bb((ms or 0) + (n.get('my_bet') or 0))}BB")
        elif k in ("弃牌", "让牌"): opts.append(k)
        elif k in ("下注", "加注"):
            ps = []
            ob = n.get("opp_bet") or 0
            xm = (lambda a: f"({a / ob:.1f}倍)" if a else "") if not board and tc and ob > 2 else (lambda a: "")   # 翻前面对加注：标对方注的倍数（尺度基线按倍数）
            for p in wpstate.usable_presets(st):
                a = float(p["amt"]) if p.get("amt") else None
                if p["frac"].endswith("BB"): ps.append(p["frac"] if a is None or abs(a / 2 - float(p["frac"][:-2])) < .01 else f"{p['frac']}={bb(a)}BB")
                else: ps.append(f"{p['frac']}池" + (f"={bb(a)}BB{xm(a)}" if a is not None else ""))
            opts.append(f"{k}[{'加注到 ' if tc and ps else ''}{'、'.join(ps)}]" if ps else k)
    if ctx.get("order"): L.append(ctx["order"])
    if ctx.get("behind"): L.append(f"身后未行动 {'、'.join(ctx['behind'])}（我方动作后他们还会行动，可能加注）")
    L.append("可选 " + " / ".join(opts))
    return L


def post_order_line(pos, opp_pos, np_):
    """多人桌：在局者（未弃牌，含尚未表态的）的翻后行动顺序 + 我方对谁有位置（代码给出，模型不用自己推）"""
    live = set(opp_pos)
    seq = [p for p in T.post_order(np_) if p == pos or p in live]
    if pos not in seq or len(seq) < 2: return None
    i = seq.index(pos); before, after = seq[:i], seq[i + 1:]
    rel = ("我方最后行动，对所有在局者都有位置" if not after else "我方最先行动，对所有在局者都没有位置" if not before
           else f"我方对 {'、'.join(before)} 有位置，对 {'、'.join(after)} 没有位置")
    return f"翻后行动顺序（在局者，含尚未表态的）：{' → '.join('我方' + p if p == pos else p for p in seq)}；{rel}"


class HandSession:
    """维护 session 模式的请求。build() 生成本次请求（本手完整记录，单条 user）；commit() 记下模型回复。"""
    def __init__(self, table_dir=None, opp=None, keep_hands=6, hands_log=None, on_hand_end=None, hero_name=None,
                 history=None, population=None, warm=False, votes=3, log=print, store=None, store_write=False, hist_stats=None,
                 msgs_log=None, on_summary=None, explain=False):
        """history=该对手此前（跨桌）已结束的手 / hist_stats=其累计统计计数，热启动；population=其他对手的手，对手样本少时作群体参考（冷启动）。
        对手昵称由 see_opp() 从截图识别（多帧投票），识别到/换人后 warm=True 时后台加载该对手跨桌历史，下一手开始生效；
        opp 参数只是初始值（可为 None）。
        每手记录：hands_log=对局流水文件（真机：tables/<桌>/hands.jsonl；回放：logs/<日期>/hands__*.jsonl）；
        msgs_log=模型对话文件（真机：tables/<桌>/msgs.jsonl；按 hid 关联，流水里 msgs 字段记它的路径）。
        on_summary=每手结束的一行摘要回调（真机写 ops.log）。
        store=对手库 OppDB：有它时历史统计/画像/群体参考按主键查库；store_write=True（仅真机）时每手把计数直接加进库（不读 jsonl）。"""
        self.table_dir, self.opp, self.keep = table_dir, opp, keep_hands
        self.explain = explain               # True=测试模式输出（逐街范围推演 + 理论依据）；真机用短 JSON
        self.hist_S = hist_stats or (opp_stats(history) if history else None)
        self.prof = None                     # 对手画像 {"profile","ptype","src"}：本桌 <opp>.md 优先，其次对手库里最近一份
        self.store, self.store_write = store, store_write
        self.msgs_log, self.on_summary = msgs_log, on_summary
        self.sid = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)      # 本次运行的 id；hid = sid-手序号
        self.pop = opp_stats(population) if population else (store.pop(opp) if store and warm else None)
        self.warm, self.need_votes, self.log = warm, votes, log
        self._votes, self._pending, self._loading, self._thread = [], None, None, None
        self._unwritten = []                 # 对手还没识别出来时结束的手，识别后补上昵称再写
        self._wq, self._writer, self._prof_new = queue.Queue(), None, None   # 每手记录的后台写线程（jsonl/对手库/画像回调都不在主线程）
        self._static()
        self.hands_log, self.on_hand_end = hands_log, on_hand_end
        if opp and warm and history is None and hist_stats is None: self._start_load(opp)
        self.hero_name = hero_name or self._hero_name(table_dir)
        self.recent, self.hands = [], []     # 最近几手摘要（文本） / 本局已结束手的结构化记录（算统计）
        self.msgs, self.ev_ref = [], None
        self.cur = None                      # 当前手：{"hero","board","pos","start_total","ev","start","np","seats"}
        self.book = {}                       # 多人桌：对手昵称 -> {"hist": 跨桌累计统计, "prof": 画像}（add_opp 注入 / 后台查库）
        self.rng, self.fixed_r, self.last_r = random.Random(), None, None   # 每次决策的随机数 R（混合策略按频率执行）；fixed_r=评测固定值
        self._restat()
        self.dynamic = self._dyn()
        self.n_hands = 0

    def _static(self):
        self.static = SYS_STATIC.format(exploit=_exploit_table(), reference=_reference(),
                                        table=_table_brief(self.table_dir) + (f"\n- 当前对手：{self.opp}" if self.opp else ""),
                                        output=OUT_EXPLAIN if self.explain else OUT_LIVE)

    # ---- 多人桌对手 ----
    def add_opp(self, name, stats=None, prof=None):
        """登记一个多人桌对手的跨桌统计和画像（评测注入；真机由座位识别后查库填，见 _load_seats）"""
        self.book[name] = {"hist": stats, "prof": prof}

    def _load_seats(self, seats):
        """新一手的座位里有没见过的昵称：后台查对手库（统计 + 画像），查到前按本局统计/基线"""
        new = [nm for nm in seats.values() if nm and nm not in self.book]
        if not new: return
        for nm in new: self.book[nm] = {"hist": None, "prof": None}
        if self.store is None: return
        def run():
            for nm in new:
                try:
                    d = self.store.lookup(nm)
                    self.book[nm] = {"hist": d and d["stats"], "prof": find_profile(self.table_dir, nm, d)}
                except Exception as e: self.log(f"[session] 加载对手 {nm} 失败: {type(e).__name__}: {e}")
        threading.Thread(target=run, daemon=True).start()

    def opp_S(self, name, pos=None):
        """多人桌某对手的统计：跨桌累计 + 本局他入座的多人手"""
        mine = [dict(h, opp_pos=p) for h in self.hands if (h.get("np") or 2) > 2
                for p, nm in (h.get("seats") or {}).items() if same_opp(nm, name) and p != h.get("pos")]
        cur = opp_stats(mine)
        hist = (self.book.get(name) or {}).get("hist") or {}
        return {k: v + hist.get(k, 0) for k, v in cur.items()}, cur["n"]

    # ---- 对手识别 / 异步加载 ----
    def see_opp(self, raw):
        """每帧调用：raw=state.opp_name 读到的昵称原文。最近 8 次读数里同一个键 ≥votes 次且与当前对手不同 → 后台加载"""
        if not raw: return
        k = wpstate.name_key(raw)
        self._votes = (self._votes + [(k, raw)])[-8:]
        if sum(v[0] == k for v in self._votes) < self.need_votes: return
        if same_opp(k, self.opp if self._pending is None else self._pending[0]) or same_opp(k, self._loading): return
        raws = [r for kk, r in self._votes if kk == k]
        self._start_load(max(set(raws), key=raws.count))

    def _start_load(self, raw):
        self._loading = raw
        def run():
            try:
                if not self.warm:
                    name = display_name(raw); self._pending = (name, None, None, find_profile(self.table_dir, name))
                elif self.store:                    # 对手库：按主键查库
                    d = self.store.lookup(raw)
                    name = d["name"] if d else display_name(raw)
                    self._pending = (name, d and d["stats"], self.store.pop(name), find_profile(self.table_dir, name, d))
                else:                               # 无库：扫各桌 jsonl
                    name = display_name(raw, known_names())
                    hist, pop = load_hands(name), load_hands(exclude=name)
                    self._pending = (name, opp_stats(hist) if hist else None, opp_stats(pop) if pop else None, find_profile(self.table_dir, name))
            except Exception as e:
                self.log(f"[session] 加载对手 {raw} 失败: {type(e).__name__}: {e}")
                self._pending = (display_name(raw), None, None, None)
            finally:
                self._loading = None
        self._thread = threading.Thread(target=run, daemon=True); self._thread.start()

    def _apply_pending(self):
        """手与手之间切换到识别出的对手：此前对手未知 → 已结束的手补上昵称；换了人 → 本局统计/近况清空"""
        if self._pending is None: return
        name, hist, pop, prof = self._pending; self._pending = None
        if same_opp(name, self.opp):                 # --opp 初值就是这个人：补上历史/画像（本局还没打过手时才并入，免得和本局重复计）
            self.opp = name
            if not self.hands and self.hist_S is None: self.hist_S, self.pop = hist, pop or self.pop
            if prof and not self.prof: self.prof = prof
            return
        if self.opp is None:
            for h in self.hands: h["opp"] = name
        else:
            self.hands, self.recent = [], []
        self.opp, self.hist_S, self.pop = name, hist, pop
        self.prof = prof
        self._static()
        for rec in self._unwritten:
            rec["opp"] = name; self._emit(rec)
        self._unwritten = []
        self.log(f"[session] 识别到对手 {name}：跨桌历史 {hist['n'] if hist else 0} 手，群体参考 {pop['n'] if pop else 0} 手"
                 + (f"，已载入画像（{self.prof['src']}）" if self.prof else ""))

    def _emit(self, rec):
        """一手结束：交给后台写线程（按顺序：追加 jsonl → 累计进对手库 → 画像回调 → 取最新画像），主线程只入队"""
        if self._writer is None:
            self._writer = threading.Thread(target=self._write_loop, daemon=True); self._writer.start()
        self._wq.put(dict(rec))

    def _write_loop(self):
        while True:
            rec = self._wq.get()
            try: self._write(rec)
            except Exception as e: self.log(f"[session] 写手牌记录失败: {type(e).__name__}: {e}")
            finally: self._wq.task_done()

    def _write(self, rec):
        msgs = None
        if self.msgs_log is not None and rec.get("msgs"):                  # 模型对话单独存，按 hid 关联
            p = Path(os.fspath(self.msgs_log)); p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a", encoding="utf-8") as f:
                f.write(json.dumps({"hid": rec.get("hid"), "t": rec.get("t"), "opp": rec.get("opp"), "msgs": rec["msgs"]}, ensure_ascii=False) + "\n")
            msgs = rel(p)
        p = self.hands_log
        if p:
            p = Path(os.fspath(p)); p.parent.mkdir(parents=True, exist_ok=True)
            tb = Path(self.table_dir).name if self.table_dir else None
            with open(p, "a", encoding="utf-8") as f: f.write(json.dumps(hand_detail(rec, tb, msgs), ensure_ascii=False) + "\n")
        if self.store is not None and self.store_write and (rec.get("opp") or rec.get("seats")):
            try: self.store.add_hands([rec])            # 这一手的计数直接加到该对手那一行（UPDATE n=n+1, vp=vp+…）
            except Exception as e: self.log(f"[session] 写对手库失败: {type(e).__name__}: {e}")
        if self.on_hand_end:
            try: self.on_hand_end(rec, self)
            except Exception: pass
        if rec.get("opp"):    # 画像可能被 ProfileUpdater 更新过：查好放着，下一手开始时直接换上（本桌 md 优先，其次库里最近一份）
            d = self.store.lookup(rec["opp"]) if self.store is not None else None
            pf = find_profile(self.table_dir, rec["opp"], d)
            if pf: self._prof_new = (rec["opp"], pf)

    def flush(self):
        """等后台写完（退出前 / 测试用）；对手一直没识别出来的手也写进流水（opp=null，不计入对手库）"""
        for rec in self._unwritten: self._emit(rec)
        self._unwritten = []
        self._wq.join()

    def _refresh_prof(self):
        """每手开始换上后台写线程查好的最新画像（主线程不查库）"""
        p, self._prof_new = self._prof_new, None
        if p and same_opp(p[0], self.opp): self.prof = p[1]

    @property
    def n_hist(self):
        return self.hist_S["n"] if self.hist_S else 0

    def _restat(self):
        cur = opp_stats(self.hands)
        self.stats = {k: v + (self.hist_S or {}).get(k, 0) for k, v in cur.items()}
        typ = self.prof and self.prof["ptype"]
        self.desc = opp_desc(self.stats, self.table_dir, self.opp, typ)
        if self.stats["n"] < 4 and self.pop and self.pop["vp_n"] >= 4 and not (typ or _type_line(self.table_dir, self.opp)):
            self.desc = "新对手无样本，先按群体倾向（" + opp_desc(self.pop).replace("初判（样本少）", "") + "）小幅偏移、贴近基线"

    @staticmethod
    def _hero_name(table_dir):
        p = Path(table_dir) / "table.md" if table_dir else None
        m = re.search(r"Hero 昵称[^：]*：\s*([^\s/]+)", p.read_text()) if p and p.exists() else None
        return m.group(1) if m else "乌贼69"

    def _dyn(self):
        c = self.cur or {}
        if (c.get("np") or 2) > 2:
            L = ["## 对手画像（本手入座的对手，手与手之间由代码更新）"]
            for p, nm in (c.get("seats") or {}).items():
                if p == c.get("pos") or not nm: continue
                pf = (self.book.get(nm) or {}).get("prof")
                L.append(f"### {p}[{nm}]\n" + (profile_text(pf["profile"], 800) if pf else "暂无画像，按统计和基线"))
            L += ["", "## 最近几手（代码记录，单位 BB）"] + (self.recent[-self.keep:] or ["(本局刚开始)"])
            return "\n".join(L)
        L = ["## 对手画像（手与手之间由代码更新）", _profile_brief(self.table_dir, self.opp, prof=self.prof and self.prof["profile"])]
        if self.n_hist and not self.recent:
            L += ["", f"## 与该对手的历史（此前 {self.n_hist} 手，跨桌累计；统计已并入手牌下方的对手画像）"]
        L += ["", "## 最近几手（代码记录，单位 BB）"] + (self.recent[-self.keep:] or ["(本局刚开始)"])
        return "\n".join(L)

    @staticmethod
    def _total(st):
        n = st.get("nums") or {}
        ms = n.get("my_stack")
        return None if ms is None else ms + (n.get("my_bet") or 0)

    def _name(self, who, pos, seats=None):
        if who == "me": nm = self.hero_name
        elif seats is not None: nm = seats.get(pos) or "玩家"
        else: nm = self.opp or "对手"
        return f"{pos or '?'}[{nm}]"

    def lines(self, ev, pos, board, start, cur_street=None, np_=2, seats=None):
        """逐街行动线（位置[用户名] 动作，剩余 BB）。start={key: 本手开始时总筹码}（单挑 key=me/opp，多人 me/位置名）。"""
        w = T.walk_full(ev, pos, np_)
        steps, prior, last, pos_of = w["steps"], w["prior"], w["cur"], w["pos_of"]
        streets = ORDER[:ORDER.index(cur_street) + 1] if cur_street in ORDER else ORDER[:ORDER.index(last) + 1]
        mp = pos
        sv = seats if np_ > 2 else None
        out = []
        for s in streets:
            if s == "翻前":
                if np_ <= 2: head = f"{EN[s]}（盲注 {'我方' if mp == 'BTN' else '对手'} BTN 0.5BB / {'对手' if mp == 'BTN' else '我方'} BB 1BB）:"
                else:
                    nm = {pos_of[k]: self._name(k, pos_of[k], sv) for k in pos_of if pos_of[k] in ("SB", "BB")}
                    head = f"{EN[s]}（{np_}人桌，盲注 {nm.get('SB')} 0.5BB / {nm.get('BB')} 1BB）:"
            else:
                cards = {"翻牌": board[:3], "转牌": board[3:4], "河牌": board[4:5]}[s]
                pr = prior.get(s)
                head = f"{EN[s]} [{' '.join(cards)}]" + (f" 底池 {bb(sum(pr.values()))}BB" if pr else "") + ":"
            acts = []
            for s2, who, txt, tot in steps:
                if s2 != s: continue
                rem = None if start.get(who) is None else max(start[who] - tot, 0)
                tag = "(我方)" if who == "me" else ""
                if txt == "弃牌" and who != "me": rem = None
                acts.append(f"{self._name(who, pos_of.get(who), sv)}{tag} {txt}" + ("" if rem is None and txt == "弃牌" else f"，剩余 {bb(rem)}BB")
                            + ("（全下）" if rem == 0 else ""))
            if s == cur_street:
                acts.append(f"轮到 {self._name('me', mp, sv)}(我方)")
            out.append(head + " " + (" -> ".join(acts) if acts else "(无动作)"))
        return out

    def _close(self, st_new):
        """上一手结束：生成摘要。盈亏 = 新一手开始时我方总筹码 − 上一手开始时总筹码（含期间未观察到的手）"""
        c = self.cur
        if not c: return
        tot = self._total(st_new)
        delta = None if tot is None or c["start_total"] is None else tot - c["start_total"]
        ev = c["ev"]
        last_me = next((e for _, e in reversed(ev) if e.startswith("我方")), "")
        if "弃牌" in last_me: how = "我方弃牌"
        elif re.search(r"我方(下注|加注|全下)", last_me) and (delta or 0) > 0 and ev and not ev[-1][1].startswith("对手"): how = "对手弃牌（推断）"
        elif delta is not None and abs(delta) > 0 and len(c["board"]) == 5: how = "摊牌或河牌后结束"
        else: how = "结束方式未知"
        line = (f"手牌 #{self.n_hands} 我方{c['pos'] or '?'} {' '.join(c['hero'])} | "
                + " ".join(self.lines(ev, c["pos"], c["board"], c.get("start") or {}, np_=c.get("np") or 2, seats=c.get("seats")))
                + f" | {how}，我方 {'?' if delta is None else ('+' if delta > 0 else '') + bb(delta)}BB")
        if delta is not None and abs(delta) > 400: line += "（变化过大，可能有补码或漏观察的手）"
        self.recent.append(line)
        if self.on_summary:
            try: self.on_summary(line)
            except Exception: pass
        rec = {"hid": f"{self.sid}-{self.n_hands:04d}", "t": time.strftime("%Y-%m-%d %H:%M:%S"), "n": self.n_hands, "opp": self.opp, "pos": c["pos"], "hero": c["hero"],
               "board": c["board"], "ev": ev, "how": how, "delta": delta, "start": dict(c.get("start") or {}), "msgs": self.msgs}
        if (c.get("np") or 2) > 2: rec.update(opp=None, np=c["np"], seats=dict(c.get("seats") or {}))
        self.hands.append(rec)
        if rec.get("np"): self._emit(rec)                      # 多人桌：座位昵称已在 seats 里
        elif self.opp is None: self._unwritten.append(rec)     # 对手未识别：等识别出来再写（补上 opp）
        else: self._emit(rec)

    def _r(self):
        self.last_r = self.fixed_r if self.fixed_r is not None else self.rng.randint(0, 99)
        return self.last_r

    def user_text(self, st, hist):
        c = self.cur
        if (c.get("np") or 2) > 2: return self._user_multi(st, hist)
        n = st.get("nums") or {}
        pos, street = c["pos"], wpstate.STREETS.get(len(st["board"]), "翻前")
        # 以当前帧为准反推本手开始时的总筹码：start = 当前剩余 + 之前各街投入 + 本街已下
        w = T.walk_full(hist.ev, pos, 2)
        pr = w["prior"].get(street) or {"me": 0, "opp": 0}
        for who, sk, bk in (("me", "my_stack", "my_bet"), ("opp", "opp_stack", "opp_bet")):
            if n.get(sk) is not None: c["start"][who] = n[sk] + pr[who] + (n.get(bk) or 0)
        s = c["start"]
        rk = wpstate.preflop_rank(st["hero"])
        L = [f"手牌 #{self.n_hands}",
             f"当前手牌 {' '.join(st['hero'])}（{wpstate.hand_desc(st['hero'])}" + (f"，{rk[0]} 前{rk[1]['top']:.0f}%" if rk else "") + "）；"
             f"我方 {self._name('me', pos)}，对手 {self._name('opp', OTHER.get(pos))}"]
        rg = T.preflop_ranges(w, 2, lambda k: self.stats)
        if not any(e.startswith("对手弃牌") for _, e in hist.ev):      # 只写未弃牌的对手（单挑时就是唯一的对手）
            L += [f"对手画像 {self._name('opp', OTHER.get(pos))}（" + ("跨桌累计统计" if self.n_hist else "本局统计") + "，次数/机会=频率）"] + profile_block(self.stats, self.desc, self.n_hist, self.pop)
            if rg.get("opp"): L.append("对手翻前范围（代码按位置基线 + 他的统计收缩，翻后在此基础上按行动线推演）：" + T.range_text(rg["opp"]))
        L += [f"起始筹码 我方 {bb(s.get('me'))}BB / 对手 {bb(s.get('opp'))}BB"]
        L += self.lines(hist.ev, pos, st["board"], s, cur_street=street)
        ctx = {"x": rg["opp"]["x"] if rg.get("opp") and street == "翻前" and any(a["agg"] and a["key"] == "opp" for a in w["acts"]) else None,
               "base": T.hero_baseline(w, 2, pos) if street == "翻前" and pos else None}
        L += ["当前局面（代码计算）"] + snapshot_bb(st, hist, ctx)
        self._r()                                   # 抽本次决策的随机数 R（不给模型看；bot_core 按 mix+R 选动作）
        L.append("现在根据用户行为、画像和相关数据进行决策行动")
        return "\n".join(L)

    def _user_multi(self, st, hist):
        """多人桌（3~8 人）：每个未弃牌对手的画像 + 代码给的范围表 + 起始筹码 + 行动线 + 局面"""
        c, n = self.cur, st.get("nums") or {}
        pos, street, np_, seats = c["pos"], wpstate.STREETS.get(len(st["board"]), "翻前"), c["np"], c.get("seats") or {}
        w = T.walk_full(hist.ev, pos, np_)
        pos_of, active = w["pos_of"], w["active"]
        cc, base = w["c"], w["base"]
        stacks = n.get("stacks") or {}                      # 识别到的各位置剩余筹码（多人桌识别给；没有就不写）
        for k in w["keys"]:
            rem = n.get("my_stack") if k == "me" else stacks.get(pos_of[k])
            if rem is not None and (k not in c["start"] or street == "翻前"):
                c["start"][k] = rem + base.get(k, 0) + cc.get(k, 0)
        s = c["start"]
        stat = {}
        for k in active:
            if k == "me": continue
            nm = seats.get(pos_of[k])
            stat[k] = self.opp_S(nm) if nm else ({}, 0)
        rg = T.preflop_ranges(w, np_, lambda k: stat.get(k, ({}, 0))[0])
        rk = wpstate.preflop_rank(st["hero"])
        opps = [k for k in active if k != "me"]
        L = [f"手牌 #{self.n_hands}",
             f"当前手牌 {' '.join(st['hero'])}（{wpstate.hand_desc(st['hero'])}" + (f"，{rk[0]} 前{rk[1]['top']:.0f}%" if rk else "") + "）；"
             f"{np_}人桌，我方 {self._name('me', pos, seats)}；未弃牌对手 " + ("、".join(self._name(k, pos_of[k], seats) for k in opps) or "无")]
        for k in opps:
            S, cur_n = stat[k]
            nm = seats.get(pos_of[k])
            hdr = f"对手画像 {self._name(k, pos_of[k], seats)}"
            if not S.get("n"):
                L += [hdr + "：无统计样本，按位置基线"]
                continue
            pf = (self.book.get(nm) or {}).get("prof")
            scope = f"累计{S['n']}手" + (f"(本局{cur_n})" if cur_n else "")
            L += [hdr + "（跨桌累计统计，次数/机会=频率）"] + _stat_lines(S, scope, multi=True) + [f"  简述：{opp_desc(S, typ=pf and pf.get('ptype'))}"]
        if rg:
            L.append("对手翻前范围表（代码按人数×位置基线 + 他的统计收缩，翻后在此基础上按行动线推演）：")
            L += [f"  {self._name(k, r['pos'], seats)}：{T.range_text(r)}" for k, r in rg.items() if k in active]
        L.append("起始筹码 " + " / ".join(f"{self._name(k, pos_of[k], seats)} {bb(s.get(k))}BB" for k in w["keys"] if k in s and (k == "me" or k in opps)))
        L += self.lines(hist.ev, pos, st["board"], s, cur_street=street, np_=np_, seats=seats)
        aggs = [a for a in w["acts"] if a["st"] == "翻前" and a["agg"] and a["key"] != "me"]
        last = aggs[-1]["key"] if aggs else None
        mine = (n.get("my_stack") or 0) + (n.get("my_bet") or 0)
        others = [(stacks.get(pos_of[k]) or 0) + cc.get(k, 0) for k in opps if stacks.get(pos_of[k]) is not None]
        behind = T.to_act(w, pos, np_, street)
        ctx = {"multi": True, "k": len(opps) or 1, "behind": behind, "order": post_order_line(pos, [pos_of[k] for k in opps], np_) if street == "翻前" else None,   # 翻后行动线本身已显示顺序；翻后再写易诱导偷池
               "base": T.hero_baseline(w, np_, pos) if street == "翻前" else None,
               "x": rg[last]["x"] if street == "翻前" and last in rg else None,
               "eff": min(mine, max(others)) if others and n.get("my_stack") is not None else None}
        L += ["当前局面（代码计算）"] + snapshot_bb(st, hist, ctx)
        self._r()                                   # 抽本次决策的随机数 R（不给模型看；bot_core 按 mix+R 选动作）
        L.append("现在根据用户行为、画像和相关数据进行决策行动")
        return "\n".join(L)

    def build(self, st, hist):
        """返回 (请求 dict, 本条 user 文本)"""
        if hist.ev is not self.ev_ref:            # HandHist.reset() 会换新的 ev 列表 => 新的一手
            self._close(st)
            self._apply_pending()
            self._refresh_prof()
            self.n_hands += 1
            self.ev_ref, self.msgs = hist.ev, []
            self.cur = {"hero": list(st["hero"]), "board": [], "pos": hist.pos, "start_total": self._total(st), "ev": hist.ev, "start": {},
                        "np": getattr(hist, "np", 2) or 2, "seats": dict(getattr(hist, "seats", {}) or {})}
            if self.cur["np"] > 2: self._load_seats(self.cur["seats"])
            self._restat()
            self.dynamic = self._dyn()           # 画像/近况/统计只在手开始时刷新，手内 system 保持不变
        self.cur["board"] = list(st["board"])
        if self.cur["pos"] is None: self.cur["pos"] = hist.pos
        user = self.user_text(st, hist)
        req = {"system": [{"type": "text", "text": self.static, "cache_control": {"type": "ephemeral"}},
                          {"type": "text", "text": self.dynamic}],
               "messages": [{"role": "user", "content": user}]}
        return req, user

    def commit(self, user, dec):
        self.msgs = self.msgs + [{"role": "user", "content": user},
                                 {"role": "assistant", "content": json.dumps(dec, ensure_ascii=False)}]


def flatten(req):
    """非 anthropic provider 用：把 session 请求拼成单个文本 prompt"""
    if isinstance(req, str): return req
    parts = [b["text"] for b in req["system"]]
    for m in req["messages"]:
        parts.append(("【我方决策】" if m["role"] == "assistant" else "【牌桌】") + "\n" + m["content"])
    return "\n\n".join(parts)


PROFILE_PROMPT = """你在维护德州扑克对手画像。下面是模板、旧画像（可能为空）和机器人最近记录的手牌（代码自动记录：行动线、结束方式、我方盈亏；
看不到对手底牌，除非注明）。请输出完整的新画像 Markdown（沿用模板结构），要求：
- 这是本桌画像（同一人在不同桌可能打法不同）：统计计数器按这些手牌能确认的数据累加到旧画像上，看不出来的不要编；
  旧画像若注明来自其他桌，只作先验，计数从本桌手牌重新累计，“其他出现过的桌”里写上来源桌；
- 样本少（<100 手）时类型判断写“初判”，可信度标低，偏移策略要保守；
- “已确认漏洞”每条要写证据（第几手/统计）；
- “最后更新”写到最新手的序号与时间。
只输出 Markdown，不要解释。对手昵称：{opp}；桌：{table}（单挑，我方=乌贼69）。

### 模板
{tpl}

### 旧画像
{old}

### 最近手牌
{hands}"""


class ProfileUpdater:
    """每 every 手异步调一次模型更新本桌画像 tables/<桌>/<opp>.md（不在决策路径上），写完同步进对手库。replay 时不要启用。"""
    def __init__(self, model, table_dir, opp, every=10, store=None):
        self.model, self.dir, self.opp, self.every, self.store = model, Path(table_dir), opp, every, store
        self.buf, self.busy = [], False

    def __call__(self, rec, sess):
        opp = rec.get("opp")
        if not opp: return
        if not same_opp(opp, self.opp): self.opp, self.buf = opp, []      # 换对手：缓冲清空，画像写到新对手
        self.buf.append({k: rec[k] for k in ("n", "t", "pos", "hero", "board", "ev", "how", "delta")})
        if self.every <= 0 or len(self.buf) < self.every or self.busy: return
        hands, self.buf, self.busy = self.buf, [], True
        threading.Thread(target=self._run, args=(hands, self.opp), daemon=True).start()

    def _run(self, hands, opp=None):
        opp = opp or self.opp
        try:
            old = profile_path(self.dir, opp)                          # 旧画像：本桌的；本桌没有就用库里最近一份（他桌）作先验
            if old: old_t = old.read_text()
            else:
                d = self.store.lookup(opp) if self.store is not None else None
                old_t = f"（来自其他桌，仅作先验）\n{d['profile']}" if d and d.get("profile") else "(无)"
            p = old or self.dir / f"{safe_name(opp)}.md"
            tpl = (self.dir.parent / "_template" / "_user_template.md")
            prompt = PROFILE_PROMPT.format(opp=opp, table=self.dir.name, tpl=tpl.read_text() if tpl.exists() else "", old=old_t,
                                           hands="\n".join(json.dumps(h, ensure_ascii=False) for h in hands))
            md = self.model(prompt, timeout=180, max_tokens=4000).strip()
            md = re.sub(r"^```(?:markdown)?\n|\n```$", "", md)
            if md.startswith("# "):
                p.parent.mkdir(parents=True, exist_ok=True)
                if p.exists(): p.with_suffix(".md.bak").write_text(p.read_text())
                p.write_text(md + "\n")
                if self.store is not None: self.store.sync_profiles([p])     # 画像进库，下一手开始生效
        except Exception as e:
            import logroll
            logroll.logger("session").error("[profile] 更新 %s 画像失败: %s: %s", opp, type(e).__name__, str(e)[:120], exc_info=True)
        finally:
            self.busy = False
