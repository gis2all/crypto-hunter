"""The contract the model must satisfy, for both passes.

Two calls, two shapes, and the difference between them is what each one costs.

`MaterialVerdict` is the cheap pass: is this material an activity, a project, or neither. It
runs against every material that survived the free word gate (`scan.action_hit`), and its answer
decides whether the expensive pass runs at all.

`ExtractDraft` is the expensive pass, and it returns **entries in the plural** on purpose: one
post usually carries one activity, while one platform listing page carries a dozen, and the
caller has no way to know which it is holding until the model has read it.

Both mirror the prompts beside this file. `test_opportunity.py` reads the JSON example out of
each prompt and compares it with these models, so a field added here without its prompt - or the
reverse - fails there instead of silently in a scan.
"""

from __future__ import annotations

import logging
import re

from pydantic import BaseModel, Field, ValidationError

log = logging.getLogger("crypto-hunter.opportunity")

#: The version each answer was written by. Stored on the entry, because a rule change is
#: otherwise indistinguishable from a judgement the model made under the old rules.
CLASSIFY_VERSION = "classify.md@2"
EXTRACT_VERSION = "extract.md@2"

#: What a material can be. The first two are the ones that become entries; the rest are
#: recorded so a scan can say what it read and did not use, which is what makes the funnel
#: auditable at all.
MATERIAL_KINDS = ("activity", "project", "opinion", "news", "other")

#: The categories, split by kind. Closed lists, because a category is a filter on the page and
#: a free string is not a filter: two scans writing 挖矿 and 挖矿活动 make two categories.
ACTIVITY_CATEGORIES = ("打新", "挖矿", "做任务", "空投", "测试网", "Mint", "白名单", "AMA", "其他")
PROJECT_CATEGORIES = ("新协议", "新实验", "新工具", "新标准", "基础设施", "其他")

#: The eight barriers an activity can carry. Multi-select, and "none of them" means the
#: material did not say - never "no barrier": that is the one reading this field exists to
#: avoid, because a missing gas cost is not a free one.
BARRIERS = ("无成本", "需 gas", "需资金", "需质押", "需白名单", "需 KYC", "需设备或挂机", "需社交任务")

#: How far along a project is. `新实验` and `新协议` are the same shape to this pipeline; this
#: is the field that tells them apart.
MATURITIES = ("实验性", "测试网", "主网")

#: The chains this app knows, in the order the panel shows them. **Not a closed vocabulary**:
#: the extraction pass may name a chain that is not here, and it is kept as itself - the set of
#: public chains is finite and known, so the job is to *judge* what the model wrote, not to
#: restrict it (user, 2026-10-19). Adding a chain to this tuple only fixes its position and gives
#: it aliases; a chain that is missing still works, it just sorts after the known ones.
CHAINS = ("bitcoin", "ethereum", "solana", "base", "bsc", "arbitrum", "sui", "aptos", "near",
          "avax", "cosmos", "starknet", "berachain", "megaeth")

#: Every spelling that means one of the chains above. This is what makes "eth", "Ethereum" and
#: "以太坊" one row instead of three - the reason a free-text field was rejected in the first
#: place, solved by judging rather than by forbidding.
CHAIN_ALIASES = {
    "bitcoin": ("bitcoin", "btc", "比特币", "ordinals", "brc-20", "brc20", "runes", "lightning"),
    "ethereum": ("ethereum", "eth", "以太坊", "erc-20", "erc20", "mainnet eth"),
    "solana": ("solana", "sol", "spl", "svm"),
    "base": ("base", "base chain", "coinbase base"),
    "bsc": ("bsc", "bnb", "bnb chain", "binance smart chain", "opbnb", "币安链"),
    "arbitrum": ("arbitrum", "arb", "arb one", "arbitrum one", "nova"),
    "sui": ("sui", "sui network"),
    "aptos": ("aptos", "apt"),
    "near": ("near", "near protocol"),
    "avax": ("avax", "avalanche", "avalanche c-chain", "雪崩"),
    "cosmos": ("cosmos", "atom", "cosmos hub", "ibc"),
    "starknet": ("starknet", "stark", "cairo"),
    "berachain": ("berachain", "bera"),
    "megaeth": ("megaeth", "mega eth"),
}

#: The two readings that are not a chain. `多链` says the material named several; `未判定` says it
#: named none - they were one value (`multi`) until 2026-10-19.
MULTI = "多链"
UNJUDGED = "未判定"
MULTI_ALIASES = ("多链", "multi", "multichain", "multi-chain", "cross-chain", "跨链", "多条链",
                 "several chains", "omnichain")
#: Word-boundary matches only: `na` inside `solanaa` used to read as 未判定, which quietly
#: swallowed a typo instead of letting it show up as its own row.
UNJUDGED_ALIASES = ("未判定", "unknown", "n/a", "none", "unspecified", "不确定", "没说")

#: What a chain name may look like when it is not one of the known ones: short, latin, no prose.
_CHAIN_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9 .\-]{1,19}$")


def canonical_ecosystem(raw: object) -> str:
    """Whatever the model wrote, as one canonical chain name.

    A known chain (through its aliases) collapses to its canonical spelling; a chain this build
    does not know is kept as itself, title-cased, because it is a real answer and the panel can
    show it; anything that is not a chain name - a sentence, a symbol, an empty string - is
    `未判定`, which is the honest reading of "we could not tell".
    """
    text = " ".join(str(raw or "").split()).strip()
    if not text:
        return UNJUDGED
    lowered = text.lower()
    for name, aliases in CHAIN_ALIASES.items():
        for alias in aliases:
            if re.search(r"(?<![a-z0-9])%s(?![a-z0-9])" % re.escape(alias), lowered):
                return name
    if any(re.search(r"(?<![a-z0-9])%s(?![a-z0-9])" % re.escape(alias), lowered)
           for alias in MULTI_ALIASES):
        return MULTI
    if any(re.search(r"(?<![a-z0-9])%s(?![a-z0-9])" % re.escape(alias), lowered)
           for alias in UNJUDGED_ALIASES):
        return UNJUDGED
    return text.title() if _CHAIN_NAME.match(text) else UNJUDGED


def order_ecosystems(values) -> list[str]:
    """The panel's order: known chains first (in `CHAINS` order), then anything new, then the two
    readings that are not a chain. New chains sort alphabetically among themselves, so the panel
    reads the same way twice even while it grows."""
    def key(name: str) -> tuple:
        if name in CHAINS:
            return (0, CHAINS.index(name), "")
        if name == MULTI:
            return (2, 0, "")
        if name == UNJUDGED:
            return (3, 0, "")
        return (1, 0, name)

    return sorted({str(name) for name in values if name}, key=key)


#: Lengths, as caps and never as rejections - the same rule `report/schema.py` follows: a
#: draft that runs long is a writing problem, and throwing it away costs the whole entry.
CAPS = {"title": 40, "summary": 200, "risk": 120, "step_body": 200}

#: A title shorter than this is not a title ("X", "??"), and an entry without one cannot be read.
MIN_TITLE = 3

#: How many steps an activity may carry. A real quest has three or four; a list longer than this
#: is a page copied whole rather than steps read out of it.
MAX_STEPS = 8


class MaterialVerdict(BaseModel):
    """What one material is, and why. `why` is read by a human debugging a scan."""

    kind: str = "other"
    category: str = ""
    #: 项目自己的名字，照材料里的写法。溯源用它去搜官方号（`trace.py`）：名字是模型读完整条帖
    #: 给出的，不是正则猜的 —— 第一版用正则猜，八条线索猜出 "good night"、"where fhe"、
    #: "$mkchdz"，于是八条全部溯源失败（2026-10-19 实测）。
    name: str = ""
    why: str = ""


class Step(BaseModel):
    """One participation step, and where it was read.

    `source_url` is the material's own URL, so a step always points back at something that was
    actually fetched. A step whose source is "the model knows how this works" is exactly what
    this field exists to prevent.
    """

    title: str = ""
    body: str = ""
    source_url: str = ""
    source_at: str = ""


class Entry(BaseModel):
    """One thing worth a row: an activity with steps, or a project with a sentence.

    Every text field defaults to empty because "the material did not say" is a legitimate
    answer to every one of them, and a required field would force the model to invent it.
    """

    title: str = ""
    kind: str = "activity"
    category: str = ""
    organizer: str = ""
    official_url: str = ""
    summary: str = ""
    steps: list[Step] = Field(default_factory=list)
    barrier: list[str] = Field(default_factory=list)
    maturity: str = ""
    ecosystem: str = ""
    risk: str = ""
    started_at: str = ""
    deadline: str = ""


class ExtractDraft(BaseModel):
    """What one material yielded. Empty is a real answer: most posts yield nothing."""

    entries: list[Entry] = Field(default_factory=list)


def _trim(text: object, cap: int) -> str:
    return str(text or "").strip()[:cap]


def _log_long(what: str, name: str, text: str, cap: int) -> None:
    if len(text) > cap:
        log.warning("opportunity %s: %s over cap (%d > %d chars)", what, name, len(text), cap)


#: Things that are not a project name however they were written: the regex guess produced these
#: from real posts, and a trace that searches for one of them wastes a scan's budget.
_NOT_A_NAME = {"gm", "good night", "good morning", "hello", "hi", "thanks", "testnet", "mainnet",
               "airdrop", "points", "quest", "launch", "update", "news", "公告", "更新", "空投",
               "测试网", "积分", "任务"}


def read_verdict(raw: dict) -> tuple[MaterialVerdict | None, list[str]]:
    """One classification answer, or the reasons it cannot be read.

    An unknown kind is *not* repaired here: it becomes `other`, which is the honest reading of
    an answer this build does not understand, and the caller records it rather than retrying.
    """
    try:
        verdict = MaterialVerdict.model_validate(raw)
    except ValidationError as exc:
        return None, ["schema: %s" % exc.errors()[:3]]
    if verdict.kind not in MATERIAL_KINDS:
        return None, ["kind %r is not one of %s" % (verdict.kind, ", ".join(MATERIAL_KINDS))]
    verdict.name = " ".join(str(verdict.name or "").split())[:40]
    if verdict.name.startswith("$") or verdict.name.lower() in _NOT_A_NAME:
        verdict.name = ""
    issues: list[str] = []
    if verdict.kind in ("activity", "project"):
        allowed = ACTIVITY_CATEGORIES if verdict.kind == "activity" else PROJECT_CATEGORIES
        if verdict.category not in allowed:
            issues.append("category %r is not one of %s" % (verdict.category, ", ".join(allowed)))
    return verdict, issues


def read_entries(raw: dict) -> tuple[list[Entry], list[str]]:
    """Every entry one material yielded, with the problems that were found on the way.

    The issues are what the repair pass is asked to fix; an entry that still has them is not
    promoted, so nothing here is silently corrected except the two normalisations that cannot
    change what the entry says: an unknown category becomes 其他, and a barrier this build does
    not have is dropped (with the drop recorded).
    """
    try:
        draft = ExtractDraft.model_validate(raw)
    except ValidationError as exc:
        return [], ["schema: %s" % err for err in exc.errors()[:6]]

    issues: list[str] = []
    kept: list[Entry] = []
    for index, entry in enumerate(draft.entries):
        where = "entries[%d]" % index
        entry.title = _trim(entry.title, CAPS["title"])
        entry.summary = _trim(entry.summary, CAPS["summary"])
        entry.risk = _trim(entry.risk, CAPS["risk"])
        entry.organizer = str(entry.organizer or "").strip().lstrip("@")
        entry.official_url = str(entry.official_url or "").strip()
        entry.started_at = str(entry.started_at or "").strip()
        entry.deadline = str(entry.deadline or "").strip()
        entry.steps = [step for step in entry.steps if str(step.body or "").strip()][:MAX_STEPS]
        for step in entry.steps:
            step.title = _trim(step.title, CAPS["title"])
            step.body = _trim(step.body, CAPS["step_body"])
            _log_long(where, "step.body", step.body, CAPS["step_body"])

        # 生态是**判断**出来的，不是从枚举里挑的：别名表把 eth / Ethereum / 以太坊 归一成
        # 一行，不认识的链保留它自己（面板会为它长一行），说不清才是 未判定。
        entry.ecosystem = canonical_ecosystem(entry.ecosystem)

        if entry.kind not in ("activity", "project"):
            issues.append("%s: kind %r is neither activity nor project" % (where, entry.kind))
            continue
        if len(entry.title) < MIN_TITLE:
            issues.append("%s: title is empty" % where)
            continue
        allowed = ACTIVITY_CATEGORIES if entry.kind == "activity" else PROJECT_CATEGORIES
        if entry.category not in allowed:
            issues.append("%s: category %r is not one of %s" % (where, entry.category,
                                                                ", ".join(allowed)))
            entry.category = "其他"
        unknown = [name for name in entry.barrier if name not in BARRIERS]
        if unknown:
            issues.append("%s: barrier %s is not one of the eight" % (where, ", ".join(unknown)))
        entry.barrier = [name for name in BARRIERS if name in entry.barrier]
        if entry.maturity and entry.maturity not in MATURITIES:
            issues.append("%s: maturity %r is not one of %s" % (where, entry.maturity,
                                                                ", ".join(MATURITIES)))
            entry.maturity = ""
        if entry.kind == "activity" and not entry.steps:
            issues.append("%s: an activity without a single step" % where)
        if entry.kind == "project" and not entry.summary:
            issues.append("%s: a project without a summary" % where)
        for name, cap in (("title", CAPS["title"]), ("summary", CAPS["summary"]),
                          ("risk", CAPS["risk"])):
            _log_long(where, name, getattr(entry, name), cap)
        kept.append(entry)
    return kept, issues


_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slug_for(entry: Entry) -> str:
    """A stable, readable id for one entry: `opp-<words>` when the title has any latin in it.

    The reference product's ids read `opp-kol-ordworld-public-beta`, and the readable half is
    worth keeping: it is what a person pastes into a chat. A Chinese title yields no latin
    words, so those entries fall back to `opp-<organizer>-<hash>`, which is still stable across
    scans - stability is the requirement, readability is the bonus.
    """
    words = _SLUG_STRIP.sub("-", entry.title.lower()).strip("-")[:32].strip("-")
    tail = _SLUG_STRIP.sub("", str(entry.organizer or "").lower())[:16]
    if len(words) >= 3:
        return "opp-%s%s" % (words, ("-" + tail) if tail else "")
    digest = "%08x" % (abs(hash((entry.title, entry.organizer, entry.category))) % (1 << 32))
    return "opp-%s%s" % (tail + "-" if tail else "", digest)
