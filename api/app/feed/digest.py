"""事件综述: 把一件事下面的报道重写成一整段话 (AIHOT 的 `events/digest.ts`).

分组站把报道归到「事实 / 事件」上, 但一件事在页面上还要有一段**连续的话**: 它是怎么来的, 现在到
哪了. 规则版把最新的三家各写一句 (`analysis.digest`), 读起来是三条并排; 这个站让模型按时间重写成
一整段, 而且**矛盾要说明白** —— 早先报道说 A、后续报道说 B 的时候, 替它们挑一个是最不该做的事.

它排在分组站**之后**: 综述的材料是「这件事下面有哪些报道」, 没有分组就没有这件事.

只有真配了模型才跑 (`writing.enabled`), 没配时页面印规则版 —— 没模型时不空、不报错, 是这套设计
唯一的底线 (DESIGN.md 9.7).
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Collection, Sequence
from datetime import datetime, timedelta

from pydantic import BaseModel, ValidationError

from ..llm import client as llm
from ..store import db
from . import capability, materials, writing
from .sources import pipeline

log = logging.getLogger(__name__)

#: 只有最近这些天还在动的事才值得重写综述. 更早的事已经 settle, 重写它既不省钱也不增加信息.
DAYS = 14.0

#: 综述一次看多少条报道, 取**最新**的那些 (AIHOT 的 `reports.slice(-40)`). 一件事的报道可能上百,
#: 而"现在到哪了"这件事由最近这些条决定; 更早的转折已经写进上一版综述里了.
REPORT_WINDOW = 40

#: 状态: 最近还在动 / 还在观察 / 已经settle. 阈值照 AIHOT 的 `storyStatusFor`.
ACTIVE_HOURS = 24.0
WATCHING_HOURS = 72.0

PURPOSE_DIGEST = "feed.digest"
DIGEST_PROMPT = "story-digest.md"


class DigestDraft(BaseModel):
    """`prompts/story-digest.md` 那个 JSON 对象的形状 (AIHOT 的 `Schema`)."""

    title: str = ""
    digest: str = ""
    latest: str = ""


def report_id(row: dict) -> str:
    """一条报道的身份: `源|外号`. 库里主键就是这两列 —— 行号会随重抓变, 拿它当身份, 指纹会假变。"""
    return "%s|%s" % (row.get("source_id") or "", row.get("external_id") or "")


def _known_ids(raw: str) -> list[str]:
    """库里那一列是 JSON 数组; 读坏了就当它没有 —— 一列读不出来不该让整件事一直不重写。"""
    try:
        value = json.loads(raw or "[]")
    except ValueError:
        return []
    return [str(item) for item in value] if isinstance(value, list) else []


def inputs_hash(reports: Sequence[dict]) -> str:
    """这一批报道的输入指纹 (AIHOT 的 `digestInputsHash`): 排序后把每条的可变字段拼起来哈希.

    比"报道条数"强的地方: 换了一条报道、某条改了标题或摘要, 条数都看不出来, 而这个看得见 ——
    那两种情况下旧综述说的已经不是这件事现在的样子。只哈希**进得了综述的那些字段** (不含正文
    块), 否则一条报道改了没被引用的段落也会白花一次模型钱。
    """
    stable = sorted(([report_id(row), (row.get("title_zh") or row.get("title") or "").strip(),
                      (row.get("summary_llm") or "").strip(), row.get("source_id") or "",
                      row.get("published_at") or ""] for row in reports),
                    key=lambda entry: entry[0])
    blob = json.dumps(stable, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def status_for(last_at: str, now: datetime) -> str:
    """这件事现在算哪一档. 空时刻算 settle —— 没有"最近", 就没有"还在动"."""
    if not last_at:
        return "settled"
    try:
        age = (now - materials.moment(last_at)).total_seconds() / 3600.0
    except ValueError:
        return "settled"
    if age < ACTIVE_HOURS:
        return "active"
    if age < WATCHING_HOURS:
        return "watching"
    return "settled"


def user_block(story: dict, reports: list[dict], *, previous: str = "",
               known: Collection[str] = ()) -> str:
    """这一件事的材料: 一行一条报道, 带来源与时间; 有摘要的再跟一句.

    能确认上一版综述就是这批报道的一个子集写的 (`previous` 给了、`known` 是它覆盖过的那些 id),
    就把上一版一起递过去, 并把新增的标上【新】—— 这一趟是**增量**重写, 不是从头猜 (AIHOT 的
    incremental 分支). 给不出来就老规矩: 只给报道, 并明说不要沿用以前版本的说法.
    """
    lines = ["这件事现在的标题：%s" % ((story.get("title") or "").strip() or "（无）"), ""]
    if previous:
        lines.append("已核对输入的上一版综述：%s" % previous.strip())
        lines.append("")
        lines.append("报道（按时间，标【新】的是新增报道）：")
    else:
        lines.append("请只依据下面这些报道的当前内容重写综述，不要沿用以前版本的说法。")
        lines.append("报道（按时间）：")
    for row in reports:
        title = (row.get("title_zh") or row.get("title") or "").strip()
        mark = "【新】" if previous and report_id(row) not in known else ""
        lines.append("%s%s｜%s｜%s" % (mark, row.get("published_at") or "",
                                      row.get("source_id") or "", title))
        summary = (row.get("summary_llm") or "").strip()
        if summary:
            lines.append("    %s" % summary[:220])
    return "\n".join(lines)


def _ask(story: dict, reports: list[dict], *, previous: str = "", known: Collection[str] = (),
         timeout: float) -> DigestDraft:
    messages = [{"role": "system", "content": writing.read_prompt(DIGEST_PROMPT)},
                {"role": "user", "content": user_block(story, reports, previous=previous,
                                                       known=known)}]
    raw = llm.complete_json(messages, purpose=PURPOSE_DIGEST,
                            prompt_version=writing.version(DIGEST_PROMPT),
                            timeout=timeout, temperature=0.3,
                            model=capability.model_for("digest"))
    try:
        return DigestDraft.model_validate(raw)
    except ValidationError as exc:
        raise llm.LLMUnavailable("%s: draft did not fit DigestDraft: %s"
                                 % (DIGEST_PROMPT, str(exc)[:200])) from exc


def run(moment: datetime, *, budget: dict) -> dict:
    """一轮综述: 把「窗口里、输入指纹和上次写的时候不一样」的那些事重写一遍.

    一条一次调用, 而且每写完一条就落库 —— 与分组站同一条理由: 预算在一半用光时, 前面写好的那些
    不该跟着丢掉.

    "输入没变"由**指纹**说了算 (`inputs_hash` 对全部报道算), 不是条数: 换了一条报道、某条改了
    标题或摘要, 条数一样而指纹不一样; 指纹一样才不重问 (AIHOT 的 `inputs_hash` 短路)。上一版
    覆盖的报道一条都没少、内容也没变时, 走增量: 把上一版递回去、新增的标【新】, 而不是让它从头
    猜一遍 (AIHOT 的 `proven` + incremental)。

    答案回来之后把输入**再核一遍**: 模型想答案那段时间成员可能被合并/纠错搬走, 对不上就把这次
    答案丢掉 (AIHOT `composeStoryDigest` 写库前比 `story.version` 与 `digestInputsHash`)。
    """
    limit = max(0, int(budget.get("maxItems") or 0))
    report = {"asked": 0, "written": 0, "failed": 0, "changed": 0}
    if limit <= 0 or not writing.enabled() or not pipeline.stages(budget)["digest"]:
        return report
    since = (moment - timedelta(days=DAYS)).isoformat(timespec="seconds")
    rows = db.feed_stories_for_digest(since)
    timeout = float(budget.get("timeoutSeconds") or 60)
    for story in rows:
        if report["asked"] >= limit:
            break
        reports = db.feed_story_reports(story["id"])
        if not reports:
            continue
        fingerprint = inputs_hash(reports)
        known = _known_ids(story.get("digest_ids"))
        covered = [row for row in reports if report_id(row) in known]
        # 上一版能当增量输入的前提: 它覆盖的报道一条都还在、内容一个字没变 (AIHOT 的 `proven`)。
        # 不满足就按"从头重写"走 —— 沿用一段已经对不上的话, 比多花一次钱坏得多。
        proven = (bool(story.get("digest")) and bool(story.get("digest_inputs"))
                  and len(covered) == len(known)
                  and inputs_hash(covered) == story["digest_inputs"])
        if proven and story["digest_inputs"] == fingerprint:
            continue                      # 输入没变: 不重问, 不重写
        report["asked"] += 1
        try:
            draft = _ask(story, reports[-REPORT_WINDOW:], previous=story.get("digest") or "",
                         known=known if proven else (), timeout=timeout)
        except llm.LLMUnavailable as exc:
            # 模型不可用 (没配 / 限流 / 答案不合格式) 就收手: 这一轮不写综述, 那些事留在"规则版"
            # 那一档, 下一轮照样轮到它们. 半截地写下去只会让页面上有的模型写、有的规则版.
            report["failed"] += 1
            log.warning("feed: digest stopped: %s", str(exc)[:200])
            break
        # 模型想的这段时间里成员可能变了 (合并搬事实、纠错作废、下一轮重分组). AIHOT 写库前把
        # 输入再核一遍 (`composeStoryDigest`: `story.version` 与 `digestInputsHash` 各比一次),
        # 对不上就把这次答案丢掉 —— 一段描述「另一批报道」的话, 比这一轮没写更坏。
        if inputs_hash(db.feed_story_reports(story["id"])) != fingerprint:
            report["changed"] += 1
            log.info("feed: digest for story %s dropped, its inputs moved while the model wrote",
                     story["id"])
            continue
        db.feed_story_digest_set(story["id"], {
            "title": (draft.title or "").strip()[:120] or (story.get("title") or ""),
            "digest": (draft.digest or "").strip()[:2000],
            "latest": (draft.latest or "").strip()[:300],
            "status": status_for(story.get("last_at") or "", moment),
            "digest_inputs": fingerprint,
            "digest_ids": json.dumps([report_id(row) for row in reports], ensure_ascii=False),
        })
        report["written"] += 1
    return report
