"""分组: 把窗口里的条目分成"同一件事"。

AIHOT 的分组键不是词, 是语义 (`packages/backend/src/events/group.ts`)。四步:

1. 拿"标题 + 摘要"当这一条的文本 (`reportText`);
2. 在过去 14 天的事实里召回最像的若干条: 有 embedding 用余弦 (线 0.6), 没有就退回字符二元组重合
   (线 0.25 —— AIHOT 的 `lexicalSimilarity` 就是这条兜底);
3. 让模型逐条判四类关系 (同一次发生 / 同一事件的不同进展 / 无关 / 盘点), 各带置信度;
4. 到线才合并, 而且**只挂故事的根**: 同一次发生 → 报进那个事实; 明确是后续进展 → 在那件事下起一个
   新事实; 其余 → 自己起一件事。**盘点永不合并**。

这个文件只放"怎么判", 不碰库 —— 读写由采集那一站做, 所以它可以拿假模型整段测。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Callable, Iterable, Sequence

from pydantic import BaseModel, ValidationError

from ..llm import client as llm
from . import capability, writing

#: 四类关系。名字与 AIHOT 的 RELATIONS 一一对应, 小写是为了不把大写带进数据。
RELATIONS = ("same_occurrence", "same_story", "unrelated", "roundup")

#: 模型的答案可能写成 AIHOT 的大写枚举, 也可能写中文; 都收, 收不下的按 unrelated 算。
_ALIASES = {"same_occurrence": "same_occurrence", "sameoccurence": "same_occurrence",
            "same_story": "same_story", "samestory": "same_story",
            "unrelated": "unrelated", "roundup": "roundup",
            "同一次发生": "same_occurrence", "同一事件的不同进展": "same_story",
            "无关": "unrelated", "盘点": "roundup"}

#: 召回窗口. 14 天是 AIHOT 的值: 一件事的后续通常在这个跨度里, 再长候选里全是旧闻。
RECALL_DAYS = 14
RECALL_TOP = 10
COSINE_MIN = 0.6
#: 字符二元组的召回线: 两串共享的二元组占较短那一边的多少。
LEXICAL_MIN = 0.25

#: 相似度到这个数就不必再问第二遍 (AIHOT 的 CONFIRM_BELOW_COSINE)。低于它、模型却说"同一次发生"
#: 时, 值得单独复核一次 —— 那是召回噪声最容易冒充结论的区间。
CONFIRM_BELOW_COSINE = 0.85
#: 两条故事并起来要的置信度, 与故事级复核的门槛 (AIHOT 的 TIE_/STORY_REVIEW_MIN_CONFIDENCE)。
TIE_MIN_CONFIDENCE = 0.8
STORY_REVIEW_MIN_CONFIDENCE = 0.75

GROUP_PROMPT = "group-batch.md"
PAIR_PROMPT = "group-pair.md"

#: 讨论帖单独一档提示词: 它问的是"这条帖子在说哪一件事", 材料里那条是帖子不是报道。
GROUP_SIGNAL_PROMPT = "group-signal.md"

#: 讨论帖的召回与自动挂靠 (AIHOT 的 `SIGNAL_MIN_COSINE` / `SIGNAL_AUTO_COSINE` / `SIGNAL_TOP_FACTS`)。
#: 三条都比报道那三条高: 一条推文只有几十字, 0.6 那一档召回来的大半是"提到了同一个代币"而已。
SIGNAL_MIN_COSINE = 0.72
SIGNAL_AUTO_COSINE = 0.92
SIGNAL_TOP = 4
#: 帖子被判成"在直接反应这件事"要的置信度 (AIHOT 的 `minReactionConfidence`)。
SIGNAL_REACTION_MIN = 0.8

PURPOSE_GROUP = "feed.group"
PURPOSE_GROUP_REVIEW = "feed.group_review"
PURPOSE_GROUP_SIGNAL = "feed.group_signal"


def station_version() -> str:
    """这一站的提示词版本 (AIHOT 的 `ANALYZE_PROMPT_VERSION`): 三条提示词各算各的哈希再拼起来.

    每一条定夺都把它记下来 (`feed_item_facts` / `feed_story_signals` 的 `prompt_version`) —— 提示词
    改一个字就是一个新版本, 而库里按旧版定过的行不会自己重跑, 会一直用旧口径冒充新口径且完全
    静默。挑出旧版重判的入口在 `stories.run` (AIHOT 的 `regroup_pending`)。
    """
    return "+".join(writing.version(name)
                    for name in (GROUP_PROMPT, GROUP_SIGNAL_PROMPT, PAIR_PROMPT))


def report_text(title: str, summary: str | None) -> str:
    """比对的文本: 标题 + 摘要前 300 字, 两边永远同一个写法 (AIHOT 的 `reportText`)。"""
    return "%s。%s" % ((title or "").strip(), (summary or "")[:300])


def lexical(a: str, b: str) -> float:
    """没有 embedding 时的相似度: 两串共享的**字符**二元组占较短那边的比例。

    与 AIHOT 的 `lexicalSimilarity` 同一条 —— 中文没有空格, 按词切先要有一个分词器, 而字符
    二元组不需要, 也不会因为分词错了把两件不相干的事拉到一起。
    """
    def grams(text: str) -> set[str]:
        chars = list((text or "").replace(" ", ""))
        return {"".join(chars[i:i + 2]) for i in range(len(chars) - 1)}
    left, right = grams(a), grams(b)
    if not left or not right:
        return 0.0
    return len(left & right) / min(len(left), len(right))


@dataclass(frozen=True)
class Candidate:
    """召回里的一个候选: 一个**事实**, 以及它属于哪个故事。"""

    key: str                 # 事实的键
    story: str               # 故事的键 ( = 我们存的那件事)
    title: str
    summary: str = ""
    source: str = ""
    at: str = ""
    #: 这个事实是不是它那个故事的**根** (最早那一次发生)。只有根能收后续 —— 不链式挂
    #: (AIHOT 的 `storyRoot`)。
    root: bool = True
    score: float = 0.0       # 召回相似度, 由 `recall` 填
    #: 判定材料里的那几格, 与 AIHOT `reportView.frame` / `CandidateView` 一一对应
    first_party: bool = False
    subject: str = ""
    action: str = ""
    object: str = ""
    occurred_at: str = ""
    #: 只有候选那一侧用得上: 这个事实下面已经有多少篇报道、它自己的标题
    members: int = 0
    fact_title: str = ""

    @property
    def text(self) -> str:
        return report_text(self.title, self.summary)


@dataclass(frozen=True)
class Verdict:
    relation: str
    confidence: float = 0.0
    note: str = ""


@dataclass(frozen=True)
class Decision:
    """新报道归到哪儿。

    `story` 空 = 自己起一件事; `fact` 空 = 起一个新事实 (在 `story` 之下, 或者就是新故事的第一个);
    `role` 是 `report` 时表示它是那次发生的又一次报道, 不是新进展 (AIHOT 的 fact_articles.role)。
    """

    story: str
    fact: str
    role: str
    relation: str
    confidence: float
    #: 这一条「提及」的事实键 (4.9b): 盘点/日报把几件事装在一起时, 它对这些事是提及而不是
    #: 证据。默认空 = 一条报道只谈一件事。
    mentions: tuple[str, ...] = ()

    @property
    def scope(self) -> str:
        """分析档位 (AIHOT 的 `analyses.output.scope`): 提了别的事实就是复合的。"""
        return "composite" if self.mentions else "primary"


def recall(query: Candidate | str, pool: Iterable[Candidate], *,
           top: int = RECALL_TOP, min_score: float | None = None,
           similarity: Callable[[str, str], float] = lexical,
           boost: Sequence[Candidate] = ()) -> list[Candidate]:
    """池子里最像的几条候选, 相似度高的在前。

    `boost` 是"这条推文回复/引用了那一条"这种已知关系 (AIHOT 的 `boost` 直接给满分): 那不是
    模型该猜的东西, 是读材料就知道的事实。
    """
    text = query.text if isinstance(query, Candidate) else str(query or "")
    floor = LEXICAL_MIN if min_score is None else min_score
    scored: list[Candidate] = []
    for candidate in pool:
        score = similarity(text, candidate.text)
        if score >= floor:
            scored.append(_with_score(candidate, score))
    for candidate in boost:
        scored.append(_with_score(candidate, 1.0))
    seen: dict[str, Candidate] = {}
    for candidate in scored:
        previous = seen.get(candidate.key)
        if previous is None or candidate.score > previous.score:
            seen[candidate.key] = candidate
    return sorted(seen.values(), key=lambda c: c.score, reverse=True)[:top]


def _with_score(candidate: Candidate, score: float) -> Candidate:
    return replace(candidate, score=score)


class DecisionDraft(BaseModel):
    id: str
    relation: str = "unrelated"
    confidence: float = 0.0
    note: str = ""


class GroupDraft(BaseModel):
    """`prompts/group-batch.md` 那个 JSON 对象的形状 (AIHOT 的 BatchSchema)。"""

    query: str = ""
    decisions: list[DecisionDraft] = []


class SignalDraft(BaseModel):
    """`prompts/group-signal.md` 的形状 (AIHOT 的 SignalSchema)。只有关系, 没有 note: 这一档的
    答案不印给读者, 多一格说明就多一格可以编的地方。"""

    decisions: list[DecisionDraft] = []


class PairDraft(BaseModel):
    """`prompts/group-pair.md` 的形状 (AIHOT 的 PairSchema): 两条报道之间的关系。"""

    a: str = ""
    b: str = ""
    relation: str = "unrelated"
    difference: str = ""
    confidence: float = 0.0


def _relation(raw: str) -> str:
    key = str(raw or "").strip().lower().replace(" ", "_")
    return _ALIASES.get(key, "unrelated")


def verdicts_of(draft: GroupDraft, candidates: Sequence[Candidate]) -> dict[str, Verdict]:
    """按候选号 (`C1`/`1`) 把答案拨回候选身上: 模型漏了的候选算 `unrelated` (AIHOT 同一条)。"""
    return _verdicts(draft.decisions, candidates)


def _verdicts(decisions: Sequence[DecisionDraft],
              candidates: Sequence[Candidate]) -> dict[str, Verdict]:
    out: dict[str, Verdict] = {}
    for decision in decisions:
        number = str(decision.id or "").strip().lstrip("Cc")
        if not number.isdigit():
            continue
        index = int(number) - 1
        if 0 <= index < len(candidates):
            key = candidates[index].key
            if key not in out:
                out[key] = Verdict(_relation(decision.relation), float(decision.confidence or 0),
                                   (decision.note or "").strip())
    for candidate in candidates:
        out.setdefault(candidate.key, Verdict("unrelated", 0.0, ""))
    return out


def same_occurrence(candidates: Sequence[Candidate],
                    verdicts: dict[str, Verdict]) -> list[Candidate]:
    """模型说是"同一次发生"的候选, 置信度高的在前, 同分按召回相似度 (AIHOT 的 `sameOccurrence`)。"""
    picked = [c for c in candidates if verdicts.get(c.key, Verdict("unrelated")).relation == "same_occurrence"]
    return sorted(picked, key=lambda c: (verdicts[c.key].confidence, c.score), reverse=True)


def story_for_development(candidates: Sequence[Candidate],
                          verdicts: dict[str, Verdict]) -> Candidate | None:
    """后续进展只挂**根**事实: 一条已经有后续的事实不再收新的后续, 否则时间线会长成一条链。"""
    picked = [c for c in candidates
              if c.root and verdicts.get(c.key, Verdict("unrelated")).relation == "same_story"]
    return max(picked, key=lambda c: c.score, default=None)


def looks_like_roundup(candidates: Sequence[Candidate],
                       verdicts: dict[str, Verdict]) -> bool:
    """候选全是盘点 (日报/周报把这件事当一个条目): 这一篇自己起一件事, 永不合并。"""
    return bool(candidates) and all(
        verdicts.get(c.key, Verdict("unrelated")).relation == "roundup" for c in candidates)


def signal_target(candidates: Sequence[Candidate], verdicts: dict[str, Verdict],
                  *, min_reaction: float = SIGNAL_REACTION_MIN) -> Candidate | None:
    """这条帖子该挂到哪个事实上 (AIHOT 的 `signalTarget`)。

    先看"同一次发生" —— 帖子转述的就是那一次; 再退一步收**明显在反应**的后续, 要 0.8 以上的
    置信度: 一条只是顺手评论了同一件事的推文, 不该因此把两件事连起来。
    """
    reported = same_occurrence(candidates, verdicts)
    if reported:
        return reported[0]
    reacted = [c for c in candidates
               if verdicts.get(c.key, Verdict("unrelated")).relation == "same_story"
               and verdicts[c.key].confidence >= min_reaction]
    return max(reacted, key=lambda c: c.score, default=None)


def firmly_tied(verdict: Verdict, *, min_confidence: float = TIE_MIN_CONFIDENCE) -> bool:
    """这一对算不算"牢牢绑住" (AIHOT 的 `firmlyTied`): 同一次发生、或明确的后续, 而且过了线.

    只有"同一次"与"后续"算绑住 —— `unrelated` 与 `roundup` 都不算, 否则每一对在一张盘点的
    候选表里碰过的报道都会让两件事连起来。
    """
    return (verdict.relation in ("same_occurrence", "same_story")
            and verdict.confidence >= min_confidence)


def tied_stories(candidates: Sequence[Candidate], verdicts: dict[str, Verdict], *,
                 min_confidence: float = TIE_MIN_CONFIDENCE) -> list[tuple[str, str, float]]:
    """这条材料被牢牢绑到的那些**事**: (故事键, 关系, 置信度) 一串, 置信度高的在前。

    判定一次本来就知道全部候选的关系, 从前只用了被选中的那一条。剩下的正是 `consolidate` 要的:
    同一条报道同时绑着两件事, 那两件事可能本来就是一件。
    """
    out: dict[str, tuple[str, float]] = {}
    for candidate in candidates:
        verdict = verdicts.get(candidate.key)
        if not candidate.story or verdict is None:
            continue
        if not firmly_tied(verdict, min_confidence=min_confidence):
            continue
        if candidate.story not in out or verdict.confidence > out[candidate.story][1]:
            out[candidate.story] = (verdict.relation, verdict.confidence)
    return [(story, relation, confidence)
            for story, (relation, confidence)
            in sorted(out.items(), key=lambda item: item[1][1], reverse=True)]


def user_block(query: Candidate, candidates: Sequence[Candidate], *, label: str = "新报道",
               ask: str = "这篇新报道与每个候选的关系是什么？") -> str:
    """判定档的材料: 待判的那条 + 候选列表, 候选带编号 (答案按编号回来)。

    报道与讨论帖共用这一份材料, 只有标签与最后那一问措辞不同 —— 同一条帖子叫成"新报道"会让
    模型按报道的严法去读它, 判出来的关系跟着变 (AIHOT 的 `batchUser`)。
    """
    blocks = [describe(query, label=label)]
    for index, candidate in enumerate(candidates, start=1):
        blocks.append(describe(
            candidate, label="候选 C%d" % index,
            extra="（该事实已有 %d 篇报道；事实标题：%s）"
                  % (candidate.members, candidate.fact_title.strip() or "（无）")))
    return "\n\n".join(blocks) + "\n\n" + ask


#: 摘要进材料时截多长 (AIHOT 的 `describeReport`)。160 字常常刚好切在「这件事到底怎么了」
#: 之前, 而判定要的正是那半句。
SUMMARY_CHARS = 360


def describe(candidate: Candidate, *, label: str = "", extra: str = "") -> str:
    """一份报道进判定材料的样子 (AIHOT 的 `describeReport`).

    一栏一行、带字段名: 模型读的是「标题／来源／摘要／事实要素」这几格。两边材料形状不一致
    (一边多一格、一边少一格) 会直接改变判出来的关系, 所以待判的那条与候选走同一个函数。

    `label` / `extra` 只给候选那一侧用: 编号要能拨回候选 (`C1`), 而「这个事实下面已经有好几
    篇报道」是判断"这是又一条报道, 还是新的一次发生"的一条依据。
    """
    lines: list[str] = []
    if label:
        lines.append("【%s】%s" % (label, extra))
    lines.append("标题：%s" % candidate.title.strip())
    lines.append("来源：%s%s｜发布时间：%s"
                 % (candidate.source.strip(),
                    "（当事方/官方）" if candidate.first_party else "", candidate.at.strip()))
    lines.append("摘要：%s" % (candidate.summary.strip()[:SUMMARY_CHARS] or "（无）"))
    if candidate.subject or candidate.action or candidate.object:
        lines.append("事实要素：主体=%s；动作=%s；对象=%s；日期=%s"
                     % (candidate.subject.strip() or "?", candidate.action.strip() or "?",
                        candidate.object.strip() or "?", candidate.occurred_at.strip() or "未知"))
    return "\n".join(lines)


def _ask(prompt: str, block: str, schema: type[BaseModel], *, purpose: str,
         timeout: float, capability_key: str = "group") -> BaseModel:
    """一次调用, 或者一个 LLMUnavailable. 与 writing._ask 同一条: 答案不合格式就是失败。

    `capability_key` 说这一问算谁的活: 逐条判定是 `group`, 复核是 `groupReview` —— 复核的意义
    就在于换一双眼睛, 用同一家模型复核自己的判断只是再掷一次骰子 (AIHOT 的两个能力位)。
    """
    messages = [{"role": "system", "content": writing.read_prompt(prompt)},
                {"role": "user", "content": block}]
    raw = llm.complete_json(messages, purpose=purpose, prompt_version=writing.version(prompt),
                            timeout=timeout, temperature=0.0,
                            model=capability.model_for(capability_key))
    try:
        return schema.model_validate(raw)
    except ValidationError as exc:
        raise llm.LLMUnavailable("%s: draft did not fit %s: %s"
                                 % (prompt, schema.__name__, str(exc)[:200])) from exc


def judge(query: Candidate, candidates: Sequence[Candidate], *, timeout: float = 60) -> dict[str, Verdict]:
    """让模型逐条判关系。返回值按候选键索引, 漏掉的算 unrelated。"""
    draft = _ask(GROUP_PROMPT, user_block(query, candidates), GroupDraft,
                 purpose=PURPOSE_GROUP, timeout=timeout)
    return verdicts_of(draft, candidates)


def judge_signal(query: Candidate, candidates: Sequence[Candidate], *,
                 timeout: float = 60) -> dict[str, Verdict]:
    """让模型逐条判"这条帖子在说哪一件事" (AIHOT 的 `judgeSignal`)。"""
    draft = _ask(GROUP_SIGNAL_PROMPT,
                 user_block(query, candidates, label="帖子",
                            ask="这条帖子与每个候选的关系是什么？"),
                 SignalDraft, purpose=PURPOSE_GROUP_SIGNAL, timeout=timeout)
    return _verdicts(draft.decisions, candidates)


def review(query: Candidate, candidate: Candidate, *, timeout: float = 60,
           purpose: str = PURPOSE_GROUP_REVIEW) -> Verdict:
    """复核一对: 召回相似度不高、模型却说是同一次发生的那一对值得再问一遍 (AIHOT 的 confirmMerge)。"""
    block = "%s\n\n%s\n\n这两篇报道是什么关系？" % (describe(query, label="报道 A"),
                                                    describe(candidate, label="报道 B"))
    draft = _ask(PAIR_PROMPT, block, PairDraft, purpose=purpose, timeout=timeout,
                 capability_key="groupReview")
    return Verdict(_relation(draft.relation), float(draft.confidence or 0), (draft.difference or "").strip())


def decide(query: Candidate, candidates: Sequence[Candidate], verdicts: dict[str, Verdict], *,
           confirm: Callable[[Candidate, Candidate], Verdict] | None = None) -> Decision:
    """AIHOT 的 `decide` 那一段, 去掉读写库那一半。

    顺序是固定的, 换一种会改变结果: 先看"同一次发生"(报进那个事实), 再在复核里接受"后续进展"
    (在那件事下起新事实), 最后才退回按进展或按盘点自己起一件事。
    """
    for pick in same_occurrence(candidates, verdicts):
        verdict = verdicts[pick.key]
        if pick.score >= CONFIRM_BELOW_COSINE:
            return Decision(pick.story, pick.key, "report", "same_occurrence", verdict.confidence)
        if confirm is None:
            return Decision(pick.story, pick.key, "report", "same_occurrence", verdict.confidence)
        second = confirm(query, pick)
        if second.relation == "same_occurrence":
            return Decision(pick.story, pick.key, "report", "same_occurrence", second.confidence)
        if second.relation == "same_story" and pick.root:
            return Decision(pick.story, "", "primary", "same_story", second.confidence)

    development = story_for_development(candidates, verdicts)
    if development is not None:
        return Decision(development.story, "", "primary", "same_story",
                        verdicts[development.key].confidence)
    if looks_like_roundup(candidates, verdicts):
        # 盘点 (4.9b): 候选全是"它把我这件事当成条目之一"。这一篇**不立事件** —— 它对这些
        # 事实是提及而不是证据 (AIHOT 的 `roundup` 判定与 `evidenceCondition` 的 composite
        # 排除是同一条)。所以 role 是 mention、mentions 是它谈到的每一个候选。
        covered = tuple(c.key for c in candidates
                        if verdicts.get(c.key, Verdict("unrelated")).relation == "roundup")
        return Decision("", "", "mention", "roundup", 0.0, covered)
    return Decision("", "", "primary", "unrelated", 0.0)


def group_with_ties(query: Candidate, pool: Sequence[Candidate], *, top: int = RECALL_TOP,
                    min_score: float | None = None,
                    similarity: Callable[[str, str], float] = lexical,
                    boost: Sequence[Candidate] = (),
                    timeout: float = 60) -> tuple[list[Decision], list[tuple[str, str, float]]]:
    """一条新报道的完整一趟: 召回 → 判定 → 复核 → 定夺, 外加"它还被绑到了哪些事上"。

    定夺只留一个事实 (一条报道归一个事实); 绑住的**事**可能有几件, 那是 `firmly_tied` 那一档
    说的另一件事 —— 它不改变这条报道归哪儿, 只说明几件事可能是一件 (AIHOT 的 consolidate 输入)。
    """
    candidates = recall(query, pool, top=top, min_score=min_score, similarity=similarity, boost=boost)
    if not candidates:
        return [Decision("", "", "primary", "unrelated", 0.0)], []
    verdicts = judge(query, candidates, timeout=timeout)
    return [decide(query, candidates, verdicts,
                   confirm=lambda q, c: review(q, c, timeout=timeout))], \
        tied_stories(candidates, verdicts)


def group(query: Candidate, pool: Sequence[Candidate], *, top: int = RECALL_TOP,
          min_score: float | None = None, similarity: Callable[[str, str], float] = lexical,
          boost: Sequence[Candidate] = (), timeout: float = 60) -> list[Decision]:
    """一条新报道的完整一趟: 召回 → 判定 → 复核 → 定夺。

    回的是一个**只有一个元素**的列表, 与"一条报道只归到一个事实"同义; 留成列表是为了让调用方
    的写法与未来可能的多事实一致 (AIHOT 的 `decide` 也只定一个)。
    """
    return group_with_ties(query, pool, top=top, min_score=min_score, similarity=similarity,
                           boost=boost, timeout=timeout)[0]
