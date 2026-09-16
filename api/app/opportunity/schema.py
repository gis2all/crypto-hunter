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
CLASSIFY_VERSION = "classify.md@3"
EXTRACT_VERSION = "extract.md@5"

#: What a material can be. The first two are the ones that become entries; the rest are
#: recorded so a scan can say what it read and did not use, which is what makes the funnel
#: auditable at all.
MATERIAL_KINDS = ("activity", "project", "opinion", "news", "other")

#: The categories, split by kind. Closed lists, because a category is a filter on the page and
#: a free string is not a filter: two scans writing 挖矿 and 挖矿活动 make two categories.
ACTIVITY_CATEGORIES = ("打新", "挖矿", "节点", "做任务", "交互", "空投", "测试网", "Mint",
                       "白名单", "治理", "其他")
PROJECT_CATEGORIES = ("新协议", "新实验", "新工具", "新标准", "基础设施", "其他")
NOVELTIES = ("protocol", "gameplay", "mechanism", "standard", "asset", "chain",
             "tooling", "market")

#: The broad classes the panel filters by, in the order it prints them. A category is one
#: leaf; a group is what a reader browses by ("今天有哪些投入型机会"), and the leaves stay on
#: the card. Derived from `category` at read time - no column, no migration.
#: Not to be confused with `opportunity_sources.family` (the 1–5 gate families): that one is
#: how a query was collected, this one is how a card reads.
CATEGORY_GROUPS = ("invest", "node", "qualify", "engage", "protocol", "tooling", "other")
CATEGORY_GROUP_LABELS = {
    "invest": "Mint / 挖矿 / 打新（投入型）",
    "node": "节点 / 验证（投入型）",
    "qualify": "空投 / 白名单（资格型）",
    "engage": "测试网 / 做任务 / 交互 / 治理（参与型）",
    "protocol": "新协议 / 新实验（观察型）",
    "tooling": "新工具 / 基础设施 / 新标准（观察型）",
    "other": "其他",
}

#: One category belongs to exactly one group, grouped by what the reader has to put in:
#: 投入型 (mint / mine / subscribe), 节点 (VPS / uptime / stake), 资格型 (claim / allowlist),
#: 参与型 (testnet / quest / interaction / governance), then the two observation groups.
#: 治理 sits in 参与型 because only one card carried the leaf after the backfill - give it its
#: own row when it grows. `挖矿` still covers both liquidity and hardware mining; it stays in
#: 投入型 until the extraction splits them. `其他` - and anything the vocabulary does not know -
#: is its own group.
CATEGORY_GROUP = {
    "mint": "invest", "挖矿": "invest", "打新": "invest", "节点": "node",
    "空投": "qualify", "白名单": "qualify",
    "测试网": "engage", "做任务": "engage", "交互": "engage", "治理": "engage",
    "新协议": "protocol", "新实验": "protocol",
    "新工具": "tooling", "基础设施": "tooling", "新标准": "tooling",
    "其他": "other",
}


def category_group_for(category: object, kind: object = "") -> str:
    """The broad class a card belongs to. Never empty: `other` is a reading, not a missing one."""
    token = str(category or "").strip().lower()
    return CATEGORY_GROUP.get(token, "other")

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
CHAINS = ("bitcoin", "ethereum", "robinhood", "xlayer", "solana", "base", "bsc", "arbitrum", "sui", "aptos", "near",
          "avax", "cosmos", "starknet", "berachain", "megaeth")

#: Every spelling that means one of the chains above. This is what makes "eth", "Ethereum" and
#: "以太坊" one row instead of three - the reason a free-text field was rejected in the first
#: place, solved by judging rather than by forbidding.
CHAIN_ALIASES = {
    "bitcoin": ("bitcoin", "btc", "比特币", "ordinals", "brc-20", "brc20", "runes", "lightning"),
    "ethereum": ("ethereum", "eth", "以太坊", "erc-20", "erc20", "mainnet eth"),
    "robinhood": ("robinhood", "robinhood chain", "robinhood-chain", "robinhoodchain"),
    "xlayer": ("xlayer", "x layer", "x-layer"),
    "solana": ("solana", "sol", "spl", "svm"),
    "base": ("base", "base chain", "coinbase base"),
    "bsc": ("bsc", "bnb", "bnb chain", "binance smart chain", "opbnb", "币安链"),
    "arbitrum": ("arbitrum", "arb", "arb one", "arbitrum one"),
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
    does not know is kept as itself, lower-cased, because it is a real answer and the panel can
    show it; anything that is not a chain name - a sentence, a symbol, an empty string - is
    `未判定`, which is the honest reading of "we could not tell".

    The stored value is always the key, never the label: one spelling per chain, so the filter
    cannot grow a second `Tron` row next to `tron`. `ecosystem_label` is what the panel prints.
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
    return text.lower() if _CHAIN_NAME.match(text) else UNJUDGED


#: What the panel prints for a stored key. Known chains carry their own spelling (`BSC`, `BTTC`,
#: `MultiversX`); a chain that is not here falls back to capitalising each word.
ECOSYSTEM_LABELS = {
    "bitcoin": "Bitcoin",
    "ethereum": "Ethereum",
    "robinhood": "Robinhood",
    "xlayer": "X Layer",
    "solana": "Solana",
    "base": "Base",
    "bsc": "BSC",
    "arbitrum": "Arbitrum",
    "sui": "Sui",
    "aptos": "Aptos",
    "near": "Near",
    "avax": "AVAX",
    "cosmos": "Cosmos",
    "starknet": "Starknet",
    "berachain": "Berachain",
    "megaeth": "MegaETH",
    "tron": "Tron",
    "hyperliquid": "Hyperliquid",
    "flare": "Flare",
    "aleo": "Aleo",
    "bttc": "BTTC",
    "multiversx": "MultiversX",
    "injective": "Injective",
    "ronin": "Ronin",
}

#: Words that stay upper-case when a new chain is title-cased by the fallback.
ECOSYSTEM_ACRONYMS = frozenset(("bsc", "bttc", "avax", "evm", "l2", "rwa", "dao", "defi", "nft"))

#: Verified public networks, with what they are. The panel's 生态 filter only offers these;
#: `多链` / `未判定` stay as readings, and a name that is not registered is not offered at all.
#: 2026-10-27: Asentum, Elyon, Interlink, Sovren, Strium and Txflow were read off their own
#: docs/sites and are public L1s (Elyon's site calls itself an L2 ecosystem, its chain docs an
#: EVM L1; registered as L1 and flagged for a later look). Type is `l1` / `l2` / `appchain`.
CHAIN_TYPES = {
    "bitcoin": "l1", "ethereum": "l1", "solana": "l1", "bsc": "l1", "tron": "l1",
    "near": "l1", "cardano": "l1", "stacks": "l2", "kaspa": "l1", "aleo": "l1",
    "flare": "l1", "hyperliquid": "l1", "monero": "l1", "zcash": "l1",
    "multiversx": "l1", "icp": "l1", "cosmos": "l1", "osmosis": "appchain",
    "stellar": "l1", "sui": "l1", "aptos": "l1", "avax": "l1", "sei": "l1",
    "injective": "appchain", "sonic": "l1", "monad": "l1", "berachain": "l1",
    "megaeth": "l2", "somnia": "l1", "pharos": "l1", "zig": "l1",
    "assetchain": "l1", "rayls": "l1", "asentum": "l1", "elyon": "l1",
    "interlink": "l1", "sovren": "l1", "strium": "l1", "txflow": "l1",
    "kite": "l1", "metal": "l1", "bttc": "l1",
    "base": "l2", "arbitrum": "l2", "optimism": "l2", "polygon": "l2",
    "ink": "l2", "xlayer": "l2", "robinhood": "l2", "giwa": "l2",
    "starknet": "l2", "blast": "l2", "mantle": "l2", "linea": "l2",
    "scroll": "l2", "zksync": "l2", "ronin": "appchain", "arc": "l1",
}


def chain_type(name: object) -> str:
    """l1 / l2 / appchain for a verified public network, `unknown` otherwise."""
    key = str(name or "").strip().lower()
    from .chains import meta
    row = meta(key)
    if row:
        return str(row.get("type") or "unverified")
    return CHAIN_TYPES.get(key, "unknown")


def chain_is_public(name: object) -> bool:
    """Whether this key is a verified public network (and therefore belongs in 生态)."""
    key = str(name or "").strip().lower()
    from .chains import meta
    row = meta(key)
    if row:
        return bool(row.get("is_public"))
    return key in CHAIN_TYPES


def ecosystem_label(name: object) -> str:
    """The stored key as the panel prints it. Unknown chains still read as a name, not a key."""
    text = " ".join(str(name or "").split()).strip()
    if not text or text in (MULTI, UNJUDGED):
        return text
    lowered = text.lower()
    if lowered in ECOSYSTEM_LABELS:
        return ECOSYSTEM_LABELS[lowered]
    from .chains import meta
    row = meta(lowered)
    if row and row.get("label"):
        return str(row["label"])
    parts = re.split(r"([\s\-]+)", lowered)
    return "".join(part.upper() if part in ECOSYSTEM_ACRONYMS
                   else (part[:1].upper() + part[1:] if part else part)
                   for part in parts)


def order_ecosystems(values) -> list[str]:
    """The panel's order: every chain A-Z by the name it is printed as, then the two readings
    that are not a chain (多链, then 未判定).

    Capitalisation and the label map are what the reader sees, so the key is the label rather
    than the stored key: `BSC` sorts under B, not after `bitcoin` because of the raw string.
    """
    seen = {str(name) for name in values if name}
    chains = sorted((name for name in seen if name not in (MULTI, UNJUDGED)),
                    key=lambda name: ecosystem_label(name).lower())
    return chains + [name for name in (MULTI, UNJUDGED) if name in seen]


_EVENT_STOP = frozenset((
    "the", "and", "for", "with", "from", "this", "that", "new", "live", "update",
    "public", "ended", "last", "day", "now", "today",
))
_SALE_WORDS = ("公开发售", "公售", "发售", "public sale", "sale")
_STATUS_WORDS = ("已结束", "结束", "剩余", "可询价", "最新", "更新", "公告", "上线", "开放", "mint", "铸造")


def project_key(value: object) -> str:
    """One project's normalized identity for card dedupe."""
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(value or "").lower()) or "unknown"


def event_signature(project: object, title: object) -> str:
    """A title reduced to the action, keeping tickers so two tokens stay two events."""
    text = str(title or "").lower()
    proj = " ".join(str(project or "").lower().split())
    if proj:
        text = re.sub(r"(?<![a-z0-9])%s(?![a-z0-9])" % re.escape(proj), " ", text)
    for word in re.findall(r"[a-z]{4,}", proj):
        text = re.sub(r"(?<![a-z0-9])%s(?![a-z0-9])" % re.escape(word), " ", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"\b[a-z0-9-]+\.(?:com|net|org|io|xyz|fun|ai|finance|site|app)\b", " ", text)
    text = re.sub(r"\b\d+(?:\.\d+)?\b", " ", text)
    for word in ("btc", "eth", "usdt", "usdc", "usd", "per piece", "piece", "件"):
        text = text.replace(word, " ")
    for word in _SALE_WORDS:
        text = text.replace(word, " sale ")
    for word in _STATUS_WORDS:
        text = text.replace(word, " ")
    text = re.sub(r"[^0-9a-z\u4e00-\u9fff$]+", " ", text)
    tokens = re.findall(r"\$[a-z0-9]+|[a-z]{3,}|[\u4e00-\u9fff]{2,}", text)
    out = []
    for token in tokens:
        if token in _EVENT_STOP or token in out:
            continue
        out.append(token)
    return "-".join(out)


def event_key(project: object, kind: str, title: object) -> str:
    """Stable identity for one project event; distinct tickers/actions stay apart."""
    project_id = project_key(project)
    signature = event_signature(project, title) or project_key(title)
    return "%s|%s|%s" % (project_id, kind, signature)


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
    novelty: str = ""
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
        entry.novelty = str(entry.novelty or "").strip().lower()
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
        if entry.novelty and entry.novelty not in NOVELTIES:
            issues.append("%s: novelty %r is not one of %s" % (where, entry.novelty,
                                                            ", ".join(NOVELTIES)))
            entry.novelty = ""
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
