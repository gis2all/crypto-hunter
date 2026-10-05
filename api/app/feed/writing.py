"""入库时的写作站: 规则版先落库, 模型写好的字再盖上去.

DESIGN.md 9 的几条约束都落在这个文件里.

**两档.** 「理解」给全部条目 (`prompts/understand.md`): 中文标题 + 摘要 + 推荐理由, 一次调用写完
三样. 「翻译」只给过了精选线、原文不是中文、而且真有正文的条目 (`prompts/translate-body.md`): 逐块
译正文. 分档的理由是成本 —— 译全文比写一句话贵一个量级, 而没进精选的条目没有人会展开读它的正文.

**结构另起一档.** `prompts/structure.md` 抽类别、标签、主体与事实骨架, 一个字都不给读者看。
它单独一档的理由是成本: 标签词表与事实骨架是分组与筛选栏要的, 而改一次词表不该把最贵的理解档
整库重跑一遍。它对每一条材料都跑 (与评分并列 —— AIHOT 的 structure 也跑在评分旁边), 骨架与
`subjects` 因此只由它写, 理解档不再输出这两样; 标签两档都写, 理解档给了就以它为准
(AIHOT 的 `writing.tags ?? structure.tags`), 没给才用结构档那一组。

**翻译按块对账.** 正文在库里是一串块 (htmlbody.py), 交给模型的也是这一串: 几块进就要几块出, 少
一块、多一块都算这一趟没译成 —— 猜一个错位的草稿, 会让页面把第二段的话印在第三段的位置. 一趟装
不完就按 `BATCH_CHARS` 分批, 一批对不上就把这批对半再问一次. 译上的块按位置写回原文的**结构**里,
没译上的块留着原文, 并把这一条记成不完整 (`body_complete`) —— 页面因此在正文上面加一行提示. 存
半篇比存空好, 但说半篇是整篇比存半篇坏.

**推文另走一条路.** X 帖的正文就是那条推文本身, 所以它**整条当一块**问 `prompts/translate-post.md`
(`@` / `#` / 网址 / 换行都保留), 而不是按块分批; 挖掉网址后不到 `X_MIN_CHARS` 字的**短帖不译** —— 短推
的意思已经由中文标题与摘要承载, 再译一遍只是把同一句话说两遍. 引用推文既不进正文也不单独译, 它只作为
背景进理解档的材料 (摘要要把它的关键点整合进去).

**中文源不问翻译档.** 判据 `zh_source()` 先信源自己的声明, 再退回数正文里的汉字. 中文源译一遍等于
把原文抄一遍, 白花一次调用, 还会让页面多一个切过去没有变化的"原文"开关.

**失败必留痕、必可补.** 库里只存**模型写过的字**; 规则版由 `analysis.py` 在读取时现算, 一个字都
不存. `llm_stage` / `prompt_version` / `llm_at` 三列说清一条走到哪一档、用的哪一版、什么时候问的.
模型没配、超时、答案不合格式, 这一行就停在上一档, 过了 `retryHours` 下一轮还会再问它. 翻译没译
完 (`body_complete` 还是 0) 也停在上一档, 下一轮接着补 —— 两档写的是不同的列, 所以翻译的失败不
牵连已经写好的标题与摘要.

**限流要认.** 一轮里**连着**失败到 `maxFailures` 就收手: 被限流时硬把一批条目写成半截, 比这一轮
什么都没写更坏. 收手之后 `llm_at` 把这批推到 `retryHours` 之后, 自愈而不需要人看着.

提示词版本取**内容哈希**, 不手写序号 (DESIGN.md 9.6): 手写的序号, 人改了文件会忘, 而忘了改的那一
版会在账本里冒充上一版.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import math
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator, Sequence

from pydantic import BaseModel, ValidationError, field_validator

from ..llm import client as llm
from ..llm import receipts
from ..store import db
from .sources import detail
from . import (analysis, capability, entities, htmlbody, identity, localtranslate, materials,
               taxonomy)
from . import input as feed_input
from .models import Event, Item
from .sources import registry
from .sources import pipeline
from .sources.snapshot import row_to_discovery

log = logging.getLogger(__name__)

PROMPT_DIR = Path(__file__).with_name("prompts")

#: 账本里这两站的名字 (`llm_calls.purpose`)。带 `feed.` 前缀是为了和雷达那边的 `research`
#: 分开看: 两边共用一个端点, 但预算与失败率是两件事.
PURPOSE_PREFILTER = "feed.prefilter"
PURPOSE_SCORE = "feed.score"
PURPOSE_UNDERSTAND = "feed.understand"
PURPOSE_SUMMARIZE = "feed.summarize"
PURPOSE_TRANSLATE = "feed.translate"
PURPOSE_STRUCTURE = "feed.structure"

#: 评分档的提示词 (AIHOT 的 selection-score)。
PREFILTER_PROMPT = "prefilter.md"
SCORE_PROMPT = "selection-score.md"

#: 便宜档按材料分派 (AIHOT 的 summarize-{article,short-post,long-post,*quoted,empty}): 文章写
#: 摘要、短帖直接译、长帖写摘要，引用与空正文各一段附则。六份共用**一个**版本 —— 改其中任何
#: 一份，这一档的每一条都重跑 (AIHOT 的 `PROMPT_VERSIONS.summarize` 也是把这几份一起哈希)。
SUMMARIZE_PROMPTS = (
    "summarize-article.md",
    "summarize-article-empty.md",
    "summarize-short-post.md",
    "summarize-short-post-quoted.md",
    "summarize-long-post.md",
    "summarize-long-post-quoted.md",
)
#: 这一档的版本 token 前缀。历史行里还留着 `summarize.md@...`，`drop` 顺手一起清掉。
SUMMARIZE_STEM = "summarize"

#: 一篇材料独立打几次分 (AIHOT 的 `SCORE_CALLS`)。两次独立评分, 和大于等于两倍门槛才入选,
#: 页面上印两次的均值。评一次是把"这稿写得好不好"和"这件事值不值得看"混在一起。
#: 预筛档跑几次. 与评分/结构两档同一把尺: 0 = 这一档不跑 (测试用它把一条材料的判定收窄到
#: 正在测的那一档).
PREFILTER_CALLS = 1
#: 预筛档的答案很短, 温度 0 (AIHOT 的 `maxTokens: 512` / `temperature: 0`): 同一份材料两次
#: 预筛必须给同一个标签 —— 这条判断是短路, 不该有随机性。
PREFILTER_MAX_TOKENS = 512
#: 预筛判为无关时这一条停在哪: 它是一条**终态**, 与 `full` 一样不再排进预算, 但页面上一个字
#: 都不印 (没有中文标题摘要 = 不发布).
STAGE_BLOCKED = "blocked"

SCORE_CALLS = 2

#: 信源分级 -> 入选门槛(平均分), 与 AIHOT 的 `industry/selection.ts` 同数: 官方一手低一些,
#: 媒体与个人高一些。**不在这张表里的分级不参与精选评分**, 只进全部动态。
SELECTION_THRESHOLDS = {"T1": 60, "T1_5": 65, "T2": 76}

#: 没入选、但平均分高过这个数的材料也用理解档来写 (AIHOT 的 `understandFloor`): 精选线附近
#: 的那一批不该只拿到一句干巴巴的翻译。低于它只写标题与摘要。
UNDERSTAND_FLOOR = 50

#: 一次调用里装多少字. 与 AIHOT 的 `BATCH_CHARS` 同数: 一篇文章的段落数可以上百, 一次全塞进去
#: 就等着答案在某处开始错位.
BATCH_CHARS = 3500

#: 每一档的答案上限 (AIHOT 的调用表): 按"这一档要写多少字"给, 不是一把全局的尺. 一次理解要写
#: 三段中文, 与一条摘要共用同一个上限会让它在中间被截断 —— 截断出来的草稿不合格式, 整条作废,
#: 而账单上只看得到一次答得不好. 全局旋钮是"表外那一档"的兜底.
STAGE_MAX_TOKENS = {"structure": 800, "understand": 16384, "summarize": 2048}

#: 评分档的调用参数, 按模型查表 (AIHOT 的 `SCORE_CALL`): 默认 0.2 / 1024 / 120s; 思考型的评分
#: 模型要temperature 1 与更大的额度, 否则它把额度花在思考上、答案被截掉.
SCORE_CALL_DEFAULT = (0.2, 1024, 120.0)
SCORE_CALL = {"glm-5.3-flash-selection": (1.0, 65536, 180.0)}

#: 理解档要读完全文再写三段字, AIHOT 给它 180 秒. 这一档的超时是**地板**: 旋钮调得比它长时
#: 听旋钮的, 免得一次慢回答被当成失败、下一轮再来一遍.
UNDERSTAND_TIMEOUT = 180.0

#: 正文与推文各一个提示词. 推文那一份要求整条一块、保留 @ / # / 网址与换行 (AIHOT 的 translate-post).
BODY_PROMPT = "translate-body.md"
POST_PROMPT = "translate-post.md"

#: 身份守卫那段附则 (AIHOT 的 identity-context): 材料里核过的发布主体, 接在摘要档系统提示词后面.
IDENTITY_PROMPT = "identity-context.md"

#: 结构档 (AIHOT 的 structure): 抽类别、标签、主体与事实骨架, 一个字都不给读者看.
STRUCTURE_PROMPT = "structure.md"

#: 结构一篇文章问几次. 0 = 不问 (测试里按住它, 与 SCORE_CALLS 同一手).
STRUCTURE_CALLS = 1

#: 页面上有角标的实体名录. subjects 里只有落在这张表里的实体才印角标 (AIHOT 的 `displayTag`
#: 那一道闸): 词典里其余的实体仍然进 `subjects`, 用于分组与更深的筛选, 只是不上卡片.
_ENTITY_TAG_NAMES = {name for name, _ in taxonomy.ENTITY_TAGS}

#: X 短帖的界线: 挖掉网址之后还剩这么多字才算"有正文". 与 AIHOT 的 `X_MIN_CHARS` 同数.
X_MIN_CHARS = 60

#: 汉字. 判一篇的原文是不是中文用的 (AIHOT 的 `isChinese` / `looksZh`).
_HAN = re.compile(r"[\u4e00-\u9fff]")

#: 数推文长度之前先把它摘掉: 一条 t.co 链接比推文本身还长.
_URL = re.compile(r"https?://\S+")


#: 片段 (AIHOT 的 `{{> file}}`): 把另一份提示词整段插进来. 共用规则只写一份 —— 几档都要的
#: 「不可信数据」「防幻觉」「标题自洽」各写各的, 改一条就要记得改五处, 迟早会有两处不一样.
_INCLUDE = re.compile(r"\{\{>\s*([A-Za-z][\w.-]*)\s*\}\}")
#: 还没被替换的 `{{key}}`。片段 (`{{> file}}`) 在 `_expand` 里已经展开, 所以这里只认
#: 纯键 —— 一份提示词里剩下任何一个都说明调用方漏传了值 (AIHOT 的 `promptText`)。
_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z][\w]*)\s*\}\}")


def _expand(name: str, seen: tuple[str, ...] = ()) -> tuple[str, tuple[str, ...]]:
    """一份提示词, 连同它引用过的每一份的名字 (读到它的顺序, 重复只留一次).

    环和缺文件都直接报错, 不留白: 一份静默少了规则的提示词会安静地改掉判据, 而这种错要过很久
    才有人发现 (CLAUDE.md 2.3.6 的同一条理由).
    """
    if name in seen:
        raise ValueError("prompt include cycle: %s" % " -> ".join(seen + (name,)))
    text = (PROMPT_DIR / name).read_text(encoding="utf-8")
    used = [name]

    def one(match: "re.Match[str]") -> str:
        inner, inner_used = _expand("%s.md" % match.group(1), seen + (name,))
        used.extend(inner_used)
        return inner

    return _INCLUDE.sub(one, text), tuple(used)


def read_prompt(name: str, **values: object) -> str:
    """读一份提示词: 先展开片段, 再把 `{{key}}` 换成传进来的值 (AIHOT 的 `promptText`).

    结构档与理解档要词表替身 (标签白名单), 其余几份是纯文本.
    """
    text, _used = _expand(name)
    for key, value in values.items():
        text = text.replace("{{%s}}" % key, str(value))
    # 缺值就报错, 不静默留白 (AIHOT 的 `promptText`): 一个没被替换的 `{{topicTags}}`
    # 会原样进模型, 而它长得像提示词的一部分 —— 模型照着它答, 页面上谁也看不出这一档
    # 少了词表。
    leftover = _PLACEHOLDER.findall(text)
    if leftover:
        raise ValueError("prompt %s is missing values: %s" % (name, ", ".join(sorted(set(leftover)))))
    return text


def version(name: str, extra: str = "") -> str:
    """`understand.md@1a2b3c4d`: 提示词的版本就是它的内容, 改一个字就是一个新版本.

    `extra` 把**词表**也算进版本里: 结构档换的是 taxonomy 里的标签词表, 提示词文件一个字没动 ——
    只哈希文件的话, 库里那些按旧词表抽过的行不会重跑, 旧标签会一直冒充新词表, 而且完全静默。

    片段也算在内: 共用规则改一个字, 用到它的每一档都该重跑 (AIHOT 的 `promptVersion` 哈希的是
    「这个提示词读到的每一个文件」). 一份没有片段的提示词, 版本与从前逐字相同 —— 不为一次换写法
    白花一轮模型钱.
    """
    text, used = _expand(name)
    parts = [text]
    for other in sorted(set(used) - {name}):
        parts.append((PROMPT_DIR / other).read_text(encoding="utf-8"))
    digest = hashlib.sha256(("".join(parts) + extra).encode("utf-8")).hexdigest()[:8]
    return "%s@%s" % (name, digest)


def summarize_version() -> str:
    """便宜档那几份提示词的**合成**版本 (AIHOT 的 `PROMPT_VERSIONS.summarize`)。

    这一档按材料挑其中的一份, 版本却只有一个: 改的是哪一份都算这一档换版 —— "这条是按哪一版
    写的"要能用**一个** token 回答. 六份各自为政的话, 判"要不要重跑"就得先猜它上次走的是哪一份。
    """
    # 身份附则跟着这一档走 (AIHOT 的 `PROMPT_VERSIONS.summarize` 把 `identity-context` 也算进去):
    # 改了白名单的注入方式, 这一档的每一条都该重跑 —— 否则旧行会拿没有身份约束的字冒充新的.
    names = SUMMARIZE_PROMPTS + (IDENTITY_PROMPT,)
    digest = hashlib.sha256("".join(version(name) for name in names).encode("utf-8"))
    return "%s@%s" % (SUMMARIZE_STEM, digest.hexdigest()[:8])


def writes() -> bool:
    """这一档预设到底写不写条目 —— 页面那一侧问的是这个问题, 不是「有没有模型」.

    节省档四站全关, 一个字都不写; 若页面照旧按 enabled() 藏起没写过的条目, 整页会安静地变成
    空的. 与 enabled() 分开是必须的: 报刊那一站只要有模型就能写 (一次一期), 与这一档无关.
    """
    return enabled() and pipeline.stages()["summarize"] > 0


def enabled() -> bool:
    """这一站开不开: 旋钮说开, 而且真有一个模型可问.

    页面按它决定要不要"没写过的条目先不印" —— 写作站关掉时(开发与 mock 用的那一档)那条门槛
    必须跟着关掉, 否则一个没有模型的机器上, 信息流会安静地变成空页 (DESIGN.md 9.7).
    """
    if int(pipeline.llm().get("maxItems") or 0) <= 0:
        return False
    return llm.configured()


def zh_source(language: str, sample: str) -> bool:
    """这一篇的原文是不是中文.

    先信源自己的声明: 写着 `en` 的源, 正文里混着几个汉字也还是英文; 写着 `zh` 就是中文. 声明
    缺席 (或写着别的) 才去数正文里的汉字 —— X 那种一条几十字的正文, 数不出汉字说明不了什么,
    所以那一条判断只在声明缺失时才用.
    """
    if language == "zh":
        return True
    if language == "en":
        return False
    return bool(_HAN.search((sample or "")[:400]))


def short_post(text: str) -> bool:
    """这条推文值不值得译: 挖掉网址再数. 一条只有链接的推文, 它的长度全是那个链接."""
    stripped = _URL.sub("", text or "")
    return len(re.sub(r"\s+", " ", stripped).strip()) < X_MIN_CHARS


def looks_zh(text: str) -> bool:
    """这一串是不是中文 (AIHOT 的 `looksZh`): 有汉字, 且不是日文假名或韩文。

    实现只有 `identity.looks_zh` 一份 —— 身份守卫也要它, 两处各写一遍迟早会不一样。
    """
    return identity.looks_zh(text)


def is_short_tweet(text: str) -> bool:
    """短帖那把尺 (AIHOT 的 `isShortTweet`): 汉字占三成以上按 100 字算, 其余按 500 字算.

    与 `short_post` 是**两把尺**: 这一把决定"这条推文走短帖档还是长帖档", 那一把 (60) 决定
    "这段正文还值不值得花钱译". 一条 80 字的英文推文是短帖, 但仍然要译正文 —— 两件事。
    """
    if not text:
        return False
    cjk = len(_HAN.findall(text))
    return len(text) < (100 if cjk > len(text) * 0.3 else 500)


def _chinese_density(text: str) -> float:
    total = len(re.sub(r"\s+", "", text))
    return 0.0 if not total else len(_HAN.findall(text)) / total


#: 数中文密度之前先摘掉: 网址、@ 与话题标签都不是"原文说了什么".
_POST_NOISE = re.compile(r"https?://\S+|@[A-Za-z0-9_]+|#[A-Za-z0-9_]+")


def needs_short_translation(text: str) -> bool:
    """这条短帖要不要译 (AIHOT 的 `needsShortTweetTranslation`): 本来就全是中文的不译 ——

    它不是"没有可译的", 而是**它自己就是稿子**, 照抄进标题与摘要即可 (AIHOT 的 `verbatim`)。
    混了英文、中文密度不到 0.65、或有一段 10 个字母以上的英文连写, 都算要译。
    """
    clean = _POST_NOISE.sub(" ", text or "")
    if not looks_zh(clean):
        return True
    if _chinese_density(clean) < 0.65:
        return True
    runs = re.findall(r"[A-Za-z][A-Za-z0-9+.#/-]*(?:\s+[A-Za-z][A-Za-z0-9+.#/-]*)+", clean)
    return any(len(re.sub(r"\s+", "", run)) >= 10 for run in runs)


#: 类别数量印在提示词里的那个汉字 ("十选一" 比 "10选一" 好读).
_ZH_NUMBERS = {8: "八", 9: "九", 10: "十", 11: "十一", 12: "十二"}


def _structure_fillings() -> dict[str, str]:
    """结构档提示词里那几处词表替身. 词表在 taxonomy.py, 提示词里不再抄一份."""
    return {
        "categoryCount": _ZH_NUMBERS.get(len(taxonomy.CATEGORIES), str(len(taxonomy.CATEGORIES))),
        "categoryGuide": "\n".join("- %s（%s）：%s" % (entry["key"], entry["label"], entry["guide"])
                                   for entry in taxonomy.CATEGORIES),
        "categoryTags": "、".join(taxonomy.CATEGORY_TAGS),
        "topicTags": "、".join(name for name, _ in taxonomy.TOPIC_TAGS),
        "entityTags": "、".join(name for name, _ in taxonomy.ENTITY_TAGS),
        "entities": "，".join("%s（%s）" % (entity.id, "/".join(entity.aliases[:3]))
                              for entity in entities.all()),
    }


def _structure_extra() -> str:
    return "|".join(_structure_fillings().values())


def prefilter_prompt() -> str:
    return read_prompt(PREFILTER_PROMPT)


def prefilter_version() -> str:
    return version(PREFILTER_PROMPT)


def structure_prompt() -> str:
    return read_prompt(STRUCTURE_PROMPT, **_structure_fillings())


def structure_version() -> str:
    """结构档的版本 = 提示词 + 词表: 改词表也要让按旧词表抽过的行重跑."""
    return version(STRUCTURE_PROMPT, _structure_extra())


def understand_prompt() -> str:
    """理解档的提示词, 标签白名单照结构档那份填法 (AIHOT 把白名单写在提示词文件里)."""
    return read_prompt("understand.md", **_structure_fillings())


def understand_version() -> str:
    """理解档的版本 = 提示词 + 词表: 它现在也写标签, 改词表就得重跑, 与结构档同一把尺."""
    return version("understand.md", _structure_extra())


class PrefilterDraft(BaseModel):
    """预筛档的答案 (AIHOT 的 `PrefilterSchema`): 三态标签 + 一句依据.

    标签先归一大小写与空白再校验 (AIHOT 的 `z.preprocess(...toUpperCase())`): 模型写 `pass`
    与写 `PASS` 是同一件事, 而写别的东西是格式错 —— 格式错照旧算这一档失败, 不猜。
    """

    label: str
    reason: str = ""

    @field_validator("label", mode="before")
    @classmethod
    def _normalise(cls, value: object) -> str:
        return str(value or "").strip().upper()

    @field_validator("label")
    @classmethod
    def _known(cls, value: str) -> str:
        if value not in ("PASS", "BLOCK", "UNKNOWN"):
            raise ValueError("label must be PASS, BLOCK or UNKNOWN")
        return value


class FactDraft(BaseModel):
    """这件事的骨架 (AIHOT 的 fact frame): 谁、做了什么、对什么.

    三格都可以是空串: 材料没说清就不许编, 而骨架的用处是给分组判定当输入 —— 编出来的骨架比空
    骨架坏得多, 它会让两件不相干的事看起来像同一件。
    """

    subject: str = ""
    action: str = ""
    object: str = ""


class StructureDraft(BaseModel):
    """`prompts/structure.md` 的形状 (AIHOT 的 StructureSchema): 类别、标签、主体、事实骨架.

    四格都有默认值 —— 模型漏写一格不该让整条答案作废, 而漏写的那一格本来就有确定的兜底:
    类别退回规则版读出来的那一个, 标签与主体空着就是"没有"。
    """

    category: str = ""
    tags: list[str] = []
    subjects: list[str] = []
    fact: FactDraft | None = None


class UnderstandDraft(BaseModel):
    """`prompts/understand.md` 那个 JSON 对象的形状. 前两样必填: 没有标题与摘要的答案没有用.

    标签也在这里 (AIHOT 的 `UnderstandSchema.tags`): 理解档读过全文, 它给的标签比只看标题与
    首段的标签档准, 两处都给时以它为准。类别、主体与事实骨架仍然只在结构档 —— 类别是结构档的
    判断, `subjects` 与骨架是分组要的, 都不该因为改一次写作提示词就重跑。
    """

    title_zh: str
    summary: str
    reason: str = ""
    tags: list[str] = []


class ScoreDraft(BaseModel):
    """`prompts/selection-score.md` 的形状: 只有一个 0-100 的整数。"""

    attentionScore: int


class SummarizeDraft(BaseModel):
    """便宜档的形状 (AIHOT 的 SummarizeSchema): 标题 + 摘要, 不写理由、骨架与实体.

    短帖那一份把 `summary` 当**译文**用 —— AIHOT 也是 `titleZh` + `bodyZh`, 到页面上是同一个
    位置 ("这条推文的中文").
    """

    title_zh: str
    summary: str


class TranslateDraft(BaseModel):
    """`prompts/translate-body.md` 那个 JSON 对象的形状: 一串译文, 与进去的块一一对应."""

    t: list[str]


def tier_of(row: dict) -> str:
    """这条材料来自哪一档信源 (T1 | T1_5 | T2)。"""
    return (registry.of(row["source_id"]).tier or "").strip()


def threshold_for(tier: str) -> int | None:
    """这一档够不够得着精选; 不在这张表里的分级不评分 (AIHOT 的 selection.ts)。"""
    return SELECTION_THRESHOLDS.get(tier)


def scores_of(row: dict) -> list[int]:
    """库里存的两次评分; 没评过或读坏了都是空表."""
    raw = _json_row(row.get("llm_scores_json"), [])
    return [int(value) for value in raw if isinstance(value, (int, float))]


def mean_score(row: dict) -> float | None:
    """两次评分的均值; 没评满两次就是 None —— 一次评分不算数, 否则"独立打两次"只是句空话."""
    values = scores_of(row)
    return sum(values) / len(values) if values and len(values) >= SCORE_CALLS else None


def picked_in_curated(row: dict, rule_score: int) -> bool:
    """"这条在不在精选线上": 模型分在就用模型分, 没有就退回规则分 (没配模型时的兜底).

    不在这张表里的分级**不进精选** (AIHOT 的 selection.ts: 表外分级不参与精选评分), 所以它们
    也不该花翻译的钱 —— 退回规则分那条路只留给"门槛有、分没拿到"的情形。
    """
    values = scores_of(row)
    threshold = threshold_for(tier_of(row))
    if threshold is None:
        return False
    if values and len(values) >= SCORE_CALLS:
        return sum(values) >= SCORE_CALLS * threshold
    return rule_score >= analysis.CURATED_MIN_SCORE


def _score_once(a: feed_input.AnalyzeInput, timeout: float, *, attempt: int = 1) -> int:
    """一次独立评分. 两次之间不做任何共享: 参数按模型查 AIHOT 的 `SCORE_CALL` 表, 表外 0.2."""
    model = capability.model_for("score")
    temperature, max_tokens, floor = SCORE_CALL.get(model, SCORE_CALL_DEFAULT)
    draft = _ask(SCORE_PROMPT, feed_input.score_input(a), ScoreDraft, purpose=PURPOSE_SCORE,
                 timeout=max(timeout, floor), model=model, temperature=temperature,
                 max_tokens=max_tokens, attempt_tag="score-%d" % attempt)
    return max(0, min(100, int(draft.attentionScore)))


def quoted_block(row: dict) -> str:
    """这条引用的那条推, 有才写.

    它必须走到模型的眼前, 而不是只躺在库里: 摘要要把引用里承载上下文的关键点整合进去 (AIHOT 的
    `summarize-*-quoted`). "这一种情况该怎么写"的规则由 `summarize_system` 附在系统提示词后面;
    引用正文本身既不进正文那一栏、也不单独译 —— 这里(format 好的那块)是它唯一的去处。
    """
    return feed_input.quoted_block(row)


def _quoted_parts(row: dict) -> tuple[str, str]:
    """引用推文的作者与正文, 有才给 (解析只有 feed/input.py 一份)."""
    return feed_input.quoted_parts(row)


def _with_identity(system: str, identity_block: str) -> str:
    """把「已核验身份上下文」接在系统提示词后面 (AIHOT 把它填进 `{{identity}}` 那一格)。"""
    return "%s\n\n%s" % (system, identity_block) if identity_block else system


#: 模型有时把提示词里的行原样抄在答案末尾 (`来源：`、`【已核验身份上下文】`、`原始标题：`)。
_ECHO_LINE = re.compile(r"^(来源[:：]|【已核验身份上下文】|这些事实只用于防止|原始标题[:：]|【时间锚点】)")


def strip_echo(text: str) -> str:
    """答案末尾抄回来的提示词行去掉 (AIHOT 的 `stripEcho`)。

    只从末尾往前剥: 中间出现的「来源：」可能是正文的正常说法, 末尾那一行才是回声。
    """
    lines = (text or "").split("\n")
    while lines and (not lines[-1].strip() or _ECHO_LINE.match(lines[-1].strip())):
        lines.pop()
    return "\n".join(lines).strip()


def _guard_json(guard: dict) -> str:
    """身份守卫的账目: 只有真退过才落库 (AIHOT 只在 `outcome === "fallback"` 时记它)。"""
    return json.dumps(guard, ensure_ascii=False) if guard.get("outcome") == "fallback" else ""


def summarize_system(row: dict, *, post: bool, text: str,
                     identity_block: str = "") -> tuple[str, str]:
    """便宜档这一问用哪一份, 以及要一并附上的那一段附则: (主提示词名, 系统提示词全文).

    三份主提示词按材料分派 (AIHOT 的 `buildShortTweetPrompt` / `buildLongTweetPrompt` /
    `buildArticlePrompt`), 两种"特殊情况"各有一段附则 —— 引用推文、正文为空. 附则接在系统
    提示词后面而不是塞进材料块: 材料是可变的输入, 附则是"这一种情况该怎么写"的规则。
    """
    if post:
        name = "summarize-short-post.md" if is_short_tweet(text) else "summarize-long-post.md"
        system = read_prompt(name)
        if _quoted_parts(row)[1]:
            system = "%s\n\n%s" % (system, read_prompt("%s-quoted.md" % name[:-3]))
        return name, _with_identity(system, identity_block)
    system = read_prompt("summarize-article.md")
    if not text.strip():
        system = "%s\n\n%s" % (system, read_prompt("summarize-article-empty.md"))
    return "summarize-article.md", _with_identity(system, identity_block)


def compact_answer_first(summary: str, max_chars: int = 190) -> str:
    """摘要在本地压短 (AIHOT 的 `compactAnswerFirstSummary`): 先留整句, 整句太长才在分句处截.

    不为"超了几个字"再问一次模型 —— 那一次要钱、要等, 而且未必更准. 截的时候只落标点处,
    绝不切在名字或数字中间。
    """
    text = re.sub(r"\s*\n+\s*", " ", summary.strip())
    if len(text) <= max_chars:
        return text
    result = ""
    for sentence in re.findall(r"[^。！？!?]+[。！？!?]?", text) or [text]:
        if len(result + sentence) > max_chars:
            break
        result += sentence
        if len(result) >= 80:
            break
    if len(result) >= 50:
        return result.strip()
    result = ""
    for clause in re.findall(r"[^，；：、,;:]+[，；：、,;:]?", text) or [text]:
        if len(result + clause) + 1 > max_chars:
            break
        result += clause
        if len(result) >= 80:
            break
    if len(result) < 50:
        return text
    return "%s。" % result.rstrip("，；：、,;:")


def answer_first_ok(summary: str, source_length: int) -> bool:
    """摘要的形状对不对 (AIHOT 的 `answerFirstSummaryLengthOk`): 太长 / 太长句 / 太短都算不对,
    不对的交 `compact_answer_first` 压一遍。
    """
    text = summary.strip()
    sentences = len([part for part in re.split(r"[。！？!?]+", text) if part.strip()])
    rich = source_length >= 500
    return (len(text) <= 200 and len(text) >= (80 if rich else 50)
            and sentences <= 3 and (not rich or sentences >= 2))


def material(row: dict, names: dict[str, str]) -> str:
    """理解档的材料块 (feed/input.py `context_material`).

    时间交原样的 ISO 串 —— 带偏移量的 ISO 8601 没有歧义, 而让模型去猜一个本地时间是不是 UTC,
    是给它一道和它的活无关的题。正文交**纯文字**: 标签是给人看的结构, 模型要的是字。
    """
    return feed_input.context_material(feed_input.load(row, names), annotate_quoted=True)


def translate_material(parts: Sequence[str]) -> str:
    """翻译档的材料: 块按顺序给出去, 回的是同样条数的一串. 条数就是地址, 所以不用给块编号."""
    return json.dumps({"segments": list(parts)}, ensure_ascii=False)


def _ask(prompt: str, block: str, schema: type[BaseModel], *, purpose: str,
         timeout: float, model: str | None = None,
         temperature: float = 0.2, system: str | None = None,
         prompt_extra: str = "", prompt_version: str | None = None,
         max_tokens: int | None = None, attempt_tag: str | None = None) -> BaseModel:
    """一次调用, 或者一个 `LLMUnavailable`. 答案不合格式也算失败 —— 猜一个坏草稿的意思, 是让
    页面说出一件没有人决定过的事 (llm.complete_json 那句话).

    `model` 是这一档自己的模型 (feed/capability.py). 传 None 就是系统设置里那一个 —— 只配了
    一个模型的机器照旧能用。
    """
    messages = [{"role": "system", "content": system or read_prompt(prompt)},
                {"role": "user", "content": block}]
    raw = llm.complete_json(messages, purpose=purpose,
                            prompt_version=prompt_version or version(prompt, prompt_extra),
                            timeout=timeout, temperature=temperature, model=model,
                            max_tokens=max_tokens, attempt_tag=attempt_tag)
    try:
        return schema.model_validate(raw)
    except ValidationError as exc:
        raise llm.LLMUnavailable("%s: draft did not fit %s: %s"
                                 % (prompt, schema.__name__, str(exc)[:200])) from exc


def _batches(parts: Sequence[str], limit: int | None = None) -> Iterator[list[int]]:
    """按字数分批, 回的是每批的**块号**: "这块译上了没有"要能在原串里对上号, 而不是只知道自己
    是第几批里的第几个."""
    limit = BATCH_CHARS if limit is None else limit
    start = 0
    while start < len(parts):
        end, size = start, 0
        while end < len(parts) and (end == start or size + len(parts[end]) <= limit):
            size += len(parts[end])
            end += 1
        yield list(range(start, end))
        start = end


def _translate_batch(indexes: Sequence[int], parts: Sequence[str], timeout: float,
                     prompt: str) -> list[str] | None:
    """一批下去, 一批上来. 条数不对就是 `None` —— 交给调用方决定要不要对半再问一次."""
    block = translate_material([parts[index] for index in indexes])
    draft = _ask(prompt, block, TranslateDraft, purpose=PURPOSE_TRANSLATE,
                 timeout=timeout, model=capability.model_for("translate"))
    return list(draft.t) if len(draft.t) == len(indexes) else None


def translate_blocks(parts: Sequence[str], timeout: float,
                     prompt: str = BODY_PROMPT) -> list[str | None]:
    """逐块译: 译不上的是 `None`, 由调用方拿原文顶上.

    一批对不上就把这批**对半**再问一次 (AIHOT 的 translateAll 同一招): 错位往往来自这一批里
    某一块太长或太怪, 对半之后坏的那一半自己会露出来. 与 AIHOT 有一处不同: 错的那一半不算数,
    **对的那一半照样收下** —— 他们只在两边都对上时才要, 而这里已经花掉的往返不该白花.
    """
    out: list[str | None] = [None] * len(parts)
    for indexes in _batches(parts):
        answer = _translate_batch(indexes, parts, timeout, prompt)
        if answer:
            for position, text in zip(indexes, answer):
                out[position] = text
            continue
        if len(indexes) == 1:
            continue
        middle = (len(indexes) + 1) // 2
        for half in (indexes[:middle], indexes[middle:]):
            answer = _translate_batch(half, parts, timeout, prompt)
            if answer:
                for position, text in zip(half, answer):
                    out[position] = text
    return out


#: 引用帖那一路的账 (AIHOT 的 purpose `translate_quoted`)。与正文翻译分开记: 它按**被引的帖子**
#: 去重, 一次失败不该把那一批正文的重试预算一起吃掉。
PURPOSE_QUOTE = "feed.translate_quoted"

#: 引用帖值不值得单独译 (AIHOT 的 `quoteTranslatable`): 挖掉网址之后还剩这么多字, 而且本来不是
#: 中文。一段 `"哈哈"` 或者一条纯链接没有可译的东西, 译了只是给账单加一行。
QUOTE_MIN_CHARS = 10

#: 一句话的翻译给多久。AIHOT 给 120 秒 —— 一句原话比一篇文章短得多, 慢的是模型排队不是这一句。
QUOTE_TIMEOUT = 120.0

#: 链接里的推文号 (AIHOT 的 `/status/(\\d+)`)。同一个号才是同一条推, 网址不是。
_STATUS_ID = re.compile(r"/status/(\d+)")


def quote_tweet_id(url: str) -> str:
    """一条引用链接指向的推文号。取不到就是空串 (拿不到号就没法去重, 这一条不译)。"""
    match = _STATUS_ID.search(url or "")
    return match.group(1) if match else ""


def quote_translatable(text: str) -> bool:
    """这一句值不值得译。判据与 AIHOT 同一套: 有字、够长、而且不是中文。"""
    words = _URL.sub(" ", text or "").strip()
    return len(words) >= QUOTE_MIN_CHARS and not looks_zh(words)


def translate_quotes(now: datetime, *, days: int = 3, limit: int = 30) -> dict:
    """别人被引的那几句话, 单独走一遍翻译 (AIHOT 的 `translateQuotes`)。

    一条 X 帖引了别人的一句话 —— 那句话是**别人的原话**, 混进正文一起译时提示词把它当成这条
    帖子自己的语气, 而它往往正是这条帖子要反驳的那一句。所以它单独一趟, 结果写回引用那一格。

    一条被引的帖子只译一次: 十条帖子引同一句话, 只花一次钱 (`feed_quote_translations` 的主键是
    被引帖的号)。如果被引的那条帖子**自己**也被我们收过、译过 (`feed_quote_own_text`), 就直接
    拿它那一份 —— 同一条推文的两份译文不一样, 读的人会以为其中一份在改口。

    模型不可用 (没配 / 被限流) 就往上抛: 这一趟收手, 剩下的下一轮再来 (与写作站同一手)。
    """
    since = (now - timedelta(days=days)).isoformat(timespec="seconds")
    rows = db.feed_quoted_items(since, limit=max(limit * 4, limit))
    seen: set[str] = set()
    report = {"stored": 0, "reused": 0, "asked": 0, "written": 0, "local": 0}
    local = pipeline.stages()["translate"] == "local"
    for row in rows:
        if report["stored"] >= limit:
            break
        try:
            quoted = json.loads(row.get("quoted_json") or "{}")
        except ValueError:
            continue
        if not isinstance(quoted, dict):
            continue
        tweet_id = quote_tweet_id(str(quoted.get("url") or ""))
        text = str(quoted.get("text") or "").strip()
        if not tweet_id or tweet_id in seen or not quote_translatable(text):
            continue
        seen.add(tweet_id)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        known = db.feed_quote_translation_get(tweet_id)
        if known and str(known.get("text_hash") or "") == digest:
            continue
        zh = db.feed_quote_own_text(tweet_id)
        origin = "reused"
        if not zh and local:
            # 本地这一路不是一次调用: 它不记收据, 失败也不该占模型的退避预算. 译不上就跳过,
            # 下一轮照样会轮到它 —— 与模型那条路的区别只在没有人被叫醒.
            origin = "local"
            zh = localtranslate.translate(text) or ""
            if not zh:
                continue
        if not zh:
            origin = "model"
            draft = _ask(BODY_PROMPT, translate_material([text]), TranslateDraft,
                         purpose=PURPOSE_QUOTE, timeout=QUOTE_TIMEOUT,
                         model=capability.model_for("translate"),
                         prompt_version=version(BODY_PROMPT),
                         max_tokens=min(4000, int(len(text) * 1.5) + 200))
            zh = draft.t[0].strip() if len(draft.t) == 1 else ""
            if not zh:
                continue
        db.feed_quote_translation_put(tweet_id, digest, zh, origin)
        report["written"] += db.feed_item_quoted_apply(tweet_id, zh)
        report["stored"] += 1
        report[{"reused": "reused", "model": "asked"}.get(origin, "local")] += 1
    return report


def fact_json(draft: StructureDraft) -> str:
    """骨架按固定的三格存. 分组的判定要读它, 所以列的形状不能这一条三格下一条两格。"""
    fact = draft.fact
    return json.dumps({"subject": (fact.subject if fact else "").strip(),
                       "action": (fact.action if fact else "").strip(),
                       "object": (fact.object if fact else "").strip()}, ensure_ascii=False)


def _tags_json(raw: object) -> dict:
    """读回一行里存着的那组标签. 坏值是空组: 这是上一轮的答案, 读不动不该让这一轮停下."""
    try:
        stored = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {"tags": [], "entities": []}
    if not isinstance(stored, dict):
        return {"tags": [], "entities": []}
    return {"tags": [str(name) for name in (stored.get("tags") or [])],
            "entities": [str(name) for name in (stored.get("entities") or [])]}


def understanding_tags(raw: object, fallback: str, subject_tags: Sequence[str]) -> dict | None:
    """理解档写出的标签 (AIHOT 的 `writing.tags ?? structure.tags`).

    它给了就以它为准, 结构档那一组不再合并进来 —— AIHOT 是 `??` 不是拼接. 主体带出来的实体
    标签照旧补在后面: 那是从 `subjects` 推的, 不是这一档的判断. 模型一个标签都没写时回 `None`,
    保留结构档那一组 —— 空答案不该把已经抽好的标签抹掉.
    """
    items = raw if isinstance(raw, list) else [raw]
    if not [name for name in items if str(name or "").strip()]:
        return None
    norm = taxonomy.normalize_tags(raw, fallback=fallback)
    shown = list(norm["entities"])
    for name in subject_tags:
        if name not in shown:
            shown.append(name)
    return {"tags_json": json.dumps({"tags": norm["tags"], "entities": shown},
                                    ensure_ascii=False)}


def write_one(row: dict, moment: datetime, names: dict[str, str],
              budget: dict) -> tuple[list[dict], str]:
    """一条的答案: 要写进那一行的补丁 (按顺序), 和一个可能为空的失败原因.

    这一条走到哪一档, 由它上一次留下的 `llm_stage` 说, 不由调用方记 —— 所以「上一轮翻译失败了」
    不需要任何额外的状态, 而这一轮它会从翻译那一档接着走.

    补丁是**一串**而不是一个: 理解档成功、翻译档失败时, 已经写好的标题与摘要照写, 只是这一条
    还没到 `full`. 把两个答案捆成一个, 失败一次就等于把上一档白写一遍.
    """
    discovery = row_to_discovery(row)
    item = analysis.read(discovery, moment)
    stamp = moment.isoformat(timespec="seconds")
    stamps = {"source_id": row["source_id"], "external_id": row["external_id"], "llm_at": stamp}
    timeout = float(budget.get("timeoutSeconds") or 60)
    # 这一档预设放行哪几站. run() 已经把答案放进预算里 (一轮读一次文件); 一个自己传预算的
    # 调用方没带这个键, 就从它现推一份 —— 那条路按 full 走, 与接预设之前一样.
    stages = budget.get("stages") or pipeline.stages(budget)
    patches: list[dict] = []
    # 材料一次读完, 每个档各自渲染 (AIHOT 的 loadAnalyzeInput + build*; feed/input.py)。
    a = feed_input.load(row, names)

    # 两档各自比版本: `prompt_version` 里含哪一版的哈希, 就说明哪一档是按那一版写的. 只比
    # `llm_stage` 是不够的 —— 一条已经写到 `full` 的旧行, 改了提示词之后每轮都会被排进队列,
    # 而每轮都只会写回一个空的 `full`, 永远追不上版本, 于是永远重问 (白花钱, 还静默).
    # 提示词版本是一串 token (逗号分开), 不是整串比较: 评分、理解、翻译各记各的, 换了哪一档
    # 的提示词就只重跑哪一档 (AIHOT 的 promptVersion + regroup_pending 同一件事)。
    tokens = [token for token in (row.get("prompt_version") or "").split(",") if token]
    stage = row.get("llm_stage") or ""

    def stamp(**cells) -> dict:
        return {**stamps, "prompt_version": ",".join(tokens), **cells}

    def keep(name: str, extra: str = "") -> None:
        """把这一档的版本记成**现在这一版**: 旧的那一条要顶掉, 不是加在后面 —— 一档攒着几代
        旧 token, 目录越滚越长, 而"这一档按哪一版写的"这个问题就没有答案了。"""
        drop(name)
        tokens.append(version(name, extra))

    def drop(name: str) -> None:
        """这一档的 token 不算数了. 两个写作档共用一份字: 上次按理解档写的行, 这次分掉到
        地板以下只写了便宜档, 那理解档的 token 必须跟着走 —— 留着它, 下回分再涨上来时, 这条
        会拿便宜档的字冒充理解档的字, 而且完全静默。"""
        tokens[:] = [token for token in tokens
                     if token.split("@", 1)[0] not in (name, name + ".md")]

    # 理解档要用结构档的两样东西: 类别的兜底, 与「主体带出来的实体标签」. 结构档这一轮没跑
    # (token 已在) 就从上一次的结果里读 —— 两档分开记账, 上一次的仍然算数。
    category_key = row.get("category_llm") or ""
    subject_tags = _tags_json(row.get("tags_json"))["entities"]

    # 预筛档 (AIHOT 的 `runSelectionPrefilter`): 先回答"这条材料是不是本行业的". 它排在花钱的
    # 几档之前 —— 判为 BLOCK 就一次都不打, 省下的是整条的分析预算; UNKNOWN 放行 (材料只有标题
    # 时 BLOCK 不算数, 只当"等材料", AIHOT 的 `missingEvidence`).
    if PREFILTER_CALLS > 0 and stages["prefilter"] > 0 and prefilter_version() not in tokens:
        try:
            draft = _ask(PREFILTER_PROMPT, feed_input.context_material(a), PrefilterDraft,
                         purpose=PURPOSE_PREFILTER, timeout=timeout,
                         model=capability.model_for("prefilter"), temperature=0.0,
                         prompt_version=prefilter_version(),
                         max_tokens=PREFILTER_MAX_TOKENS)
        except Exception as exc:
            return [dict(stamps)], "prefilter: %s" % str(exc)[:200]
        keep(PREFILTER_PROMPT)
        label = "UNKNOWN" if draft.label == "BLOCK" and feed_input.missing_evidence(a) else draft.label
        # 库里存小写 (AIHOT 的 `relevance: "pass" | "block" | "unknown"`): 标签本身是三态枚举,
        # 大小写只属于模型那边的输入约定.
        patches.append(stamp(**{"relevance": label.lower()}))
        if label == "BLOCK":
            patches.append(stamp(**{"llm_stage": STAGE_BLOCKED}))
            return patches, ""

    # 结构档 (AIHOT 的 structure): 类别、标签、主体与事实骨架. 它不写读者看得见的字, 所以与
    # 评分、写作分开 —— 改一次标签词表不必把最贵的理解档整库重跑。对每一条材料都跑, 不看分级。
    if STRUCTURE_CALLS > 0 and stages["structure"] > 0 and structure_version() not in tokens:
        try:
            draft = _ask(STRUCTURE_PROMPT, feed_input.structure_material(a), StructureDraft,
                         purpose=PURPOSE_STRUCTURE, timeout=timeout,
                         model=capability.model_for("structure"), temperature=0.2,
                         system=structure_prompt(), prompt_extra=_structure_extra(),
                         max_tokens=STAGE_MAX_TOKENS["structure"])
        except Exception as exc:
            return [dict(stamps)], "structure: %s" % str(exc)[:200]
        keep(STRUCTURE_PROMPT, extra=_structure_extra())
        rule = taxonomy.classify("%s %s" % (row.get("title") or "",
                                            htmlbody.to_text(row.get("body") or "")))
        norm = taxonomy.normalize_tags(draft.tags,
                                       fallback=draft.category or rule["category"])
        subjects = entities.subjects(draft.subjects)
        shown = list(norm["entities"])
        for name in entities.labels(subjects):
            if name in _ENTITY_TAG_NAMES and name not in shown:
                shown.append(name)
        category_key = norm["category"]
        subject_tags = shown
        patches.append(stamp(**{
            "category_llm": norm["category"],
            "tags_json": json.dumps({"tags": norm["tags"], "entities": shown},
                                    ensure_ascii=False),
            "subjects_json": json.dumps(subjects, ensure_ascii=False),
            "fact_json": fact_json(draft)}))

    # 评分档 (AIHOT 的 score): 只有参与精选的分级才评, 每次独立评两次。分数决定后面走最贵的
    # 理解档还是便宜的摘要档, 所以它排在两者之前。
    scores = scores_of(row)
    threshold = threshold_for(tier_of(row))
    calls = min(SCORE_CALLS, int(stages["score"]))
    if (calls > 0 and threshold is not None
            and version(SCORE_PROMPT) not in tokens):
        try:
            scores = [_score_once(a, timeout, attempt=i + 1) for i in range(calls)]
        except Exception as exc:
            return [dict(stamps)], "score: %s" % str(exc)[:200]
        keep(SCORE_PROMPT)
        patches.append(stamp(**{"llm_scores_json": json.dumps(scores),
                                "score_llm": math.floor(sum(scores) / len(scores))}))
    mean = sum(scores) / len(scores) if scores and len(scores) >= calls else None

    # 材料块与"这条是不是推文"在这里先取出来: 便宜档要按它分派 (文章 / 短帖 / 长帖), 翻译档也要
    # 用它 —— 两档共用一份, 不重复读.
    body = item.body_html
    parts = htmlbody.blocks(body)
    plain = htmlbody.to_text(body)
    source = registry.of(row["source_id"])
    post = item.discovery.source_kind == "x"
    main = re.sub(r"\s+", " ", (plain or row.get("title") or "")).strip()
    # 身份白名单: 材料里出现过的公司/机构 (AIHOT 的 `identityContext`)。摘要档把核过的
    # 发布主体接在提示词后面, 写完再拿它裁一遍标题与摘要 —— 模型能措辞, 不能引入材料里没有的主体.
    ident = identity.context(
        title=a.title, text=main if post else a.body_text, main_text=main if post else "",
        quoted_text=a.quoted_text, source_name=a.source_name, url=a.url)
    identity_block = read_prompt(IDENTITY_PROMPT, facts=ident.facts())

    # 写哪一档: 分**高过**理解档地板 (50) 才用理解档; 到地板为止 (或这一档不参与精选) 只写
    # 标题与摘要. 边界是「高过」不是「不低于」—— AIHOT 的 `sum > UNDERSTAND_FLOOR * SCORE_CALLS`,
    # 均值正好 50 走便宜档.
    # 理解档不放行时一律走便宜档: 分数关掉之后 mean 永远是 None, 而这几档的分级都在表里 ——
    # 不补这一句, 关掉评分反而把每一条都推进最贵的那一档.
    cheap = ((not stages["understand"]) or threshold is None
             or (mean is not None and mean <= UNDERSTAND_FLOOR))

    def summarize_stage() -> str:
        """便宜档: 标题与摘要 (AIHOT 的 `runSummarize`). 空串表示写成了, 非空是失败原因.

        理解档被上游的内容审核挡下时也走这里 (AIHOT 的 `?? await runSummarize()`): 一条材料
        写不成"好稿"不该等于这一条没有稿 —— 页面宁可印朴素的那一版, 也不要一个空位.
        """
        nonlocal stage
        # 短帖要的是**译**不是**摘** (AIHOT 的 summarize-short-post): 把 80-160 字那套要求放到
        # 一条三十字的推文上, 只会把原文没有的意思补出来. 判"短不短"用 isShortTweet 那把尺
        # (100/500), 与翻译档那把 (60) 各管各的事.
        short = post and is_short_tweet(main)
        if short and not needs_short_translation(main):
            # 短帖本来就是中文: 它自己就是稿子, 一个字都不用模型写 (AIHOT 的 verbatim).
            draft = SummarizeDraft(title_zh=main, summary=main)
        elif not post and len(plain.strip()) < 20:
            # 正文短到没有可写的: 不拿标题脑补一篇 (AIHOT 的 none). 中文原标题照留.
            title = (row.get("title") or "").strip()
            draft = SummarizeDraft(title_zh=title if looks_zh(title) else "", summary="")
        else:
            name, system = summarize_system(row, post=post, text=main if post else plain,
                                            identity_block=identity_block)
            try:
                draft = _ask(name, feed_input.summary_material(a, post=post), SummarizeDraft, system=system,
                             purpose=PURPOSE_SUMMARIZE, timeout=timeout,
                             model=capability.model_for("summarize"),
                             prompt_version=summarize_version(),
                             max_tokens=STAGE_MAX_TOKENS["summarize"])
            except Exception as exc:
                return "summarize: %s" % str(exc)[:200]
            summary = draft.summary.strip()
            # 太长的摘要在本地压短, 不再问一次模型 (AIHOT 的 `finalizeCopy`).
            if not short and summary and not answer_first_ok(summary, len(plain)):
                summary = compact_answer_first(summary)
            draft = SummarizeDraft(title_zh=draft.title_zh.strip(), summary=summary)
        title_zh, summary, guard = ident.guard(
            title_zh=strip_echo(draft.title_zh), summary=strip_echo(draft.summary),
            original_title=a.title)
        drop(SUMMARIZE_STEM)
        tokens.append(summarize_version())
        drop("understand.md")
        patches.append(stamp(**{"title_zh": title_zh,
                                "summary_llm": summary,
                                "identity_guard": _guard_json(guard),
                                "llm_stage": "summarize"}))
        stage = "summarize"
        return ""

    if cheap and stages["summarize"] > 0 and summarize_version() not in tokens:
        error = summarize_stage()
        if error:
            return patches + [dict(stamps)], error
    elif not cheap and understand_version() not in tokens:
        try:
            draft = _ask("understand.md", feed_input.context_material(a, annotate_quoted=True), UnderstandDraft,
                         purpose=PURPOSE_UNDERSTAND,
                         timeout=max(timeout, UNDERSTAND_TIMEOUT),
                         model=capability.model_for("understand"),
                         system=understand_prompt(), prompt_version=understand_version(),
                         max_tokens=STAGE_MAX_TOKENS["understand"])
        except llm.ContentRefused as exc:
            # 上游按内容审核挡下了这条材料. AIHOT 也只在**内容拒绝**时退档 (isContentFilter),
            # 其余错误照旧往上抛 —— 把限流、超时当成"写不了", 会让一整轮静默地降级.
            log.info("feed: understand refused for %s, writing the summary instead (%s)",
                     row.get("external_id"), str(exc)[:120])
            error = summarize_stage()
            if error:
                return patches + [dict(stamps)], error
        except Exception as exc:
            return patches + [dict(stamps)], "understand: %s" % str(exc)[:200]
        else:
            title_zh, summary, guard = ident.guard(
                title_zh=strip_echo(draft.title_zh), summary=strip_echo(draft.summary),
                original_title=a.title)
            keep("understand.md", extra=_structure_extra())
            drop(SUMMARIZE_STEM)
            patches.append(stamp(**{"title_zh": title_zh,
                                    "summary_llm": summary,
                                    "identity_guard": _guard_json(guard),
                                    "reason_llm": draft.reason.strip(),
                                    "llm_stage": "understand"}))
            tags = understanding_tags(draft.tags, category_key, subject_tags)
            if tags:
                patches.append(stamp(**tags))
            stage = "understand"

    # 翻译这一档只给过了精选线的、原文不是中文的、而且真有块的条目. 没有可译的东西就到此为止 ——
    # `llm_stage` 记 `full`, 因为它该由模型写的部分已经写完了, 而不是因为它译过了. 分数线与精选页
    # 共用同一个数 (analysis.CURATED_MIN_SCORE).
    # 过没过精选线由模型分说话; 不参与精选的分级一律不进; 模型分没拿到时退回规则分
    # (没配模型的那一档兜底)。
    if threshold is None:
        picked = False
    elif mean is not None:
        picked = sum(scores) >= SCORE_CALLS * threshold
    else:
        picked = item.score >= analysis.CURATED_MIN_SCORE
    if (not parts or zh_source(source.language, plain)
            or not picked
            or (post and short_post(plain))):
        if stage != "full":
            patches.append(stamp(**{"llm_stage": "full"}))
        return patches, ""

    prompt = POST_PROMPT if post else BODY_PROMPT
    if stage == "full" and version(prompt) in tokens:
        # 这一档已经按现在这一版写过了, 一个字都不用改 (缺块的那种停在 `understand`, 不是这里).
        return patches, ""
    # 块里的行内标签先护住 (AIHOT 的 `shield`): 媒体与行内代码换占位记号、链接换
    # `<a id="Ln">`, 模型只翻文字; 回来时按记号对账还原 (`restore`), 对不上的那块留原文.
    guarded = [] if post else htmlbody.shields(body)
    local = stages["translate"] == "local"
    try:
        if local:
            # 本地这一档只认纯文字: 块与小标题原样留着 (rewrite_html 拼回去), 块里行内的链接与
            # 代码换不回来 —— 译出来的是那一块的纯文字, 不是它的排版. 这是不花钱的代价, 写在
            # 明处 (见 localtranslate.py).
            answers = localtranslate.translate_all(
                [plain] if post else htmlbody.blocks(body))
        else:
            asked = [plain] if post else [item.html for item in guarded]
            answers = translate_blocks(asked, timeout, prompt=prompt)
    except Exception as exc:
        # 理解档的补丁照写, 这一条停在 `understand`, 下一轮从翻译接着走.
        return patches + [dict(stamps)], "translate: %s" % str(exc)[:200]
    if not any(answers):
        return patches + [dict(stamps)], "translate: 没有一块对上"

    # 译文末尾抄回来的提示词行也去掉 (AIHOT 在 body 上也过一遍 `stripEcho`):
    # 它是译者写的模板行, 不是原文里的话.
    if post:
        # 模型交回来的是一个字符串, 而页面排的是块: 按推文自己的分段重新落块.
        clean = strip_echo(answers[0])
        zh = htmlbody.from_plain(clean)
        complete = bool(clean)
    else:
        # 本地的译文是纯文字, 没有记号要对账: 转义回去就是那一块的内容. 走 restore 会拿
        # unshield 去对账, 而那条路的记号一个都不在 —— 每一块都会被判成没对上.
        restored = [(html.escape(strip_echo(answer), quote=False) if answer else None)
                    if local else
                    htmlbody.restore(item, strip_echo(answer) if answer else "")
                    for answer, item in zip(answers, guarded)]
        written = [text if text is not None else item.inner
                   for text, item in zip(restored, guarded)]
        zh = htmlbody.rewrite_html(body, written)
        complete = all(text is not None for text in restored)
    keep(prompt)
    patches.append(stamp(**{"body_zh": zh, "body_complete": int(complete),
                            # 没译完的停在 `understand`: 过了 `retryHours` 这一轮还会来把缺的块
                            # 补上 (AIHOT 也是这么做的 —— 缺块的译文只算半篇, 不算译过).
                            "llm_stage": "full" if complete else "understand"}))
    return patches, ""


def _json_row(raw, fallback):
    """库里那一列是 JSON 串; 读坏了就当它没有 —— 一列读不出来不该让整页 500, 规则版还在。"""
    if not raw:
        return fallback
    try:
        value = json.loads(raw)
    except ValueError:
        return fallback
    return value if isinstance(value, type(fallback)) else fallback


def apply_item(item: Item, row: dict) -> None:
    """把库里那一行的字盖到这一条上, 就地改.

    只盖**模型真的写过的**那几格: 空着的那几格保持规则版, 因为规则版至少是原文自己的话, 而
    一个空串会让页面印出一块空白。`title_zh` 是例外 —— 它就是那一格本身, 空着的意思正是
    "印源自己的标题" (`Item.display_title`).
    """
    item.title_zh = (row.get("title_zh") or "").strip()
    summary = (row.get("summary_llm") or "").strip()
    if summary:
        item.summary = summary
    reason = (row.get("reason_llm") or "").strip()
    if reason:
        item.reason = reason
    zh = (row.get("body_zh") or "").strip()
    if zh:
        item.body_zh = zh
        item.body_complete = bool(row.get("body_complete"))
    category = (row.get("category_llm") or "").strip()
    if category:
        item.category = category
        item.category_label = taxonomy.category_label(category)
    tags = _json_row(row.get("tags_json"), {})
    if isinstance(tags, dict):
        if tags.get("tags"):
            item.tags = [str(name) for name in tags["tags"]]
        if tags.get("entities"):
            item.entities = [str(name) for name in tags["entities"]]
    fact = _json_row(row.get("fact_json"), {})
    if fact:
        item.fact = fact
    subjects = _json_row(row.get("subjects_json"), [])
    if subjects:
        item.subjects = subjects
    # 分析档位 (4.9b): 入库那一刻定的, 页面与出口都读它。
    item.analysis_scope = (row.get("analysis_scope") or "").strip()
    # 评分是模型的, 就盖掉规则分 (页面上印的是它, 精选线也按它算); 没评分才轮到规则分。
    score = mean_score(row)
    if score is not None:
        item.score = int(score)
    item.selected = picked_in_curated(row, item.score)


def overlay(events: Sequence[Event], written: dict[tuple[str, str], dict]) -> int:
    """把模型写好的字盖上这一批事件, 回盖到了几条.

    字在**库**里, 不在内存里缓存: 入库那一刻写一次, 之后每个请求读同一份 —— 页面为画一屏不去
    敲模型, 与四个视图读同一份快照是同一个理由。

    事件的标题也跟着换: 一件事的名字本来是"第一个说它的人怎么写的", 那个人换成了中文标题,
    这件事的名字就跟着它走。不跟的话, 热点榜上是英文, 点进去是中文。
    """
    stamped = 0
    for event in events:
        for item in event.items:
            row = written.get((item.discovery.source_id, item.discovery.external_id))
            if row:
                apply_item(item, row)
                stamped += 1
        # 分数与精选是**事件级**的: 盖完条目要把它们抬上去, 否则热点榜的次序还按规则分排,
        # 页面上却印着模型分 (两处不一致且完全静默)。
        event.score = max((item.score for item in event.items), default=event.score)
        event.selected = any(item.selected for item in event.items) or event.selected
        lead = materials.representative(event.discoveries)
        for item in event.items:
            if item.discovery is lead:
                event.title = item.display_title
                break
    return stamped


def _batched(rows: Sequence[dict], size: int) -> Iterator[Sequence[dict]]:
    step = max(1, size)
    for start in range(0, len(rows), step):
        yield rows[start:start + step]


def _waits_for_page(row: dict) -> bool:
    """这一条还在等原文 (AIHOT `waitsForPage`): 没有正文, 而链接可以去读, 且还没去读过。

    三个条件缺一不可: 已经有正文的不等; 拿不到原文的类型 (推文) 不等;
    去读过一趟的不等 —— 抹掉这一条, 一条永远抓不到的页面会把它永远留在队里。
    """
    return (not (row.get("body") or "").strip()
            and row.get("source_kind") in detail.PAGE_KINDS
            and bool(row.get("url"))
            and not row.get("detail_at"))


def run(moment: datetime, *, names: dict[str, str], budget: dict) -> dict:
    """一轮写作: 把「该问模型、还没问成」的那些条目问一遍.

    条数 (`maxItems`) 与在飞数 (`concurrency`) 都是旋钮: 这一站是采集循环里唯一会花钱 (免费期
    是唯一会撞限流) 的一步, 没有闸门就是「一轮 tick 打几百次模型」.

    熔断的那道闸在**批与批之间**, 一批里最坏只多浪费 `concurrency` 次调用; 已经收手的那批不算
    这一轮问过, `llm_at` 还停在原处, 下一轮照样轮到它们.
    """
    limit = max(0, int(budget.get("maxItems") or 0))
    report = {"asked": 0, "wrote": 0, "full": 0, "blocked": 0, "failed": 0, "stopped": False}
    if limit <= 0:
        return report
    # 这一档预设放行哪几站: 一轮算一次, 随预算发给每一条 —— 每条自己读一次就是每条把那 12KB
    # 的 YAML 解析一遍, 而一轮要跑的正是几百条.
    budget = {**budget, "stages": pipeline.stages(budget)}

    # 上一次留下的在途请求先落定 (AIHOT 的 `recoverStaleWork`): 崩溃留下的 pending 记成结果
    # 未知, 未知超过半小时的放行一次。放在这一站, 因为它就是唯一会花钱的那一站。
    receipts.recover()

    stale = moment - timedelta(hours=float(budget.get("retryHours") or 24))
    # 提示词版本一起传下去: 改过提示词的行要重新排进预算, 否则库里那几千行会一直用旧口径
    # 冒充新口径, 而且完全静默 (db.feed_items_pending_llm 的第 3 条).
    rewritten = [(SCORE_PROMPT + "@", version(SCORE_PROMPT)),
                 (BODY_PROMPT + "@", version(BODY_PROMPT)),
                 (POST_PROMPT + "@", version(POST_PROMPT))]
    if STRUCTURE_CALLS > 0 and pipeline.stages(budget)["structure"] > 0:
        rewritten.append((STRUCTURE_PROMPT + "@", structure_version()))
    # 便宜档的 token 名是 `summarize`, 不占某一份提示词的名字: 前缀用 `summarize` 才盖得住
    # 历史上那批 `summarize.md@...`。
    rewritten.append((SUMMARIZE_STEM, summarize_version()))
    rows = db.feed_items_pending_llm(
        stale.isoformat(timespec="seconds"), limit=limit,
        # 写作档两个: 分高的那条走理解, 分低的走便宜, 命中一个就算这条写对了。
        versions=[understand_version(), summarize_version()],
        # 写过才有 token 的那两档: 提示词一改, 评过分/译过的行要重跑 (AIHOT 的 promptVersion)。
        rewritten=rewritten)
    # 还在等原文的那些先不问 (AIHOT 的 `waitsForPage` / `needsBody`): 一条只有标题、
    # 而链接可以去读的材料, 现在问它就是在标题上写摘要 —— 而原文一到,
    # 这一份就过期了, 钱花两遍。拿不到原文的 (推文、报名类源) 不在此列 ——
    # 它们的材料就是它自己。
    #
    # "去读过一趟但没读到" (`detail_at` 有值) 也不在此列: 与 AIHOT 一样,
    # 抓不到就当"仅标题可用"写下去, 不是无限等。否则一条 404 的链接会把这条
    # 永远卡在队里, 而它本来至少有标题可写。
    rows = [row for row in rows if not _waits_for_page(row)]
    if not rows:
        return report
    report["asked"] = len(rows)

    concurrency = max(1, int(budget.get("concurrency") or 4))
    max_failures = max(1, int(budget.get("maxFailures") or 3))
    consecutive = 0

    def one(row: dict) -> tuple[dict, list[dict], str]:
        """一条的答案, 异常不外抛: `map` 会把异常带到来收结果的那一行, 于是这一批的其它条目
        跟着一起没有结果."""
        try:
            patches, error = write_one(row, moment, names, budget)
        except Exception as exc:  # 连 analysis 都没读动 (一行坏数据不该让整批作废)
            return row, [], "%s: %s" % (type(exc).__name__, str(exc)[:200])
        return row, patches, error

    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="feed-write") as pool:
        for batch in _batched(rows, concurrency):
            if consecutive >= max_failures:
                report["stopped"] = True
                break
            for _row, patches, error in pool.map(one, batch):
                if patches:
                    db.feed_items_write_llm(patches)
                    # 失败的那份补丁只带 `llm_at` (把这一条推到下一轮), 它一个字都没写上 ——
                    # 数进 `wrote` 会让日志说这一轮写了四十条, 而页面上一个字都没变.
                    # 预筛判为无关的那一条一个字都没写: 数进 `blocked`, 不数进 `wrote` ——
                    # 否则日志会说这一轮写了四十条, 而页面上一条都没变.
                    if any(part.get("llm_stage") == STAGE_BLOCKED for part in patches):
                        report["blocked"] += 1
                    elif any("llm_stage" in part for part in patches):
                        report["wrote"] += 1
                    if patches[-1].get("llm_stage") == "full":
                        report["full"] += 1
                if error:
                    consecutive += 1
                    report["failed"] += 1
                    log.debug("feed: writing failed: %s", error)
                else:
                    consecutive = 0
    if report["stopped"]:
        log.warning("feed: writing stopped early after %d consecutive failures", consecutive)
    return report
