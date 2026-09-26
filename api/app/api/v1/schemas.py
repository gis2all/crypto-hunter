"""Response models. These are the contract the frontend types are generated from.

Never return a bare dict from a router: an untyped response would quietly become
`Record<string, never>` in the generated TypeScript and the whole point of
"backend Pydantic is the single source of truth" would be lost.

Two shapes carry the product:

* `BatchOut` - everything the report page draws, in one response. The SPA must not
  have to stitch ten requests together to draw one screen.
* `SettingsFormOut` - the settings page as a form model. The UI renders widgets from
  `widget`, so adding a filter is a backend change and never a frontend rewrite.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

Scalar = str | float | int | bool | list[str] | None


class HealthOut(BaseModel):
    status: str
    version: str
    time: str
    db: bool
    db_error: str | None = None
    db_path: str
    web_dist: bool
    #: Whether the writing stage has an endpoint. A round still runs without one - the
    #: cards just carry facts and no commentary - so this is the operator's answer for
    #: "why is there no text", and it belongs next to the other environment probes.
    llm: bool
    llm_model: str
    chains: list[str]


class ChainOut(BaseModel):
    id: str
    label: str
    enabled: bool
    explorer: str | None = None
    sources: list[str]
    schedule: dict | None = None
    report: dict | None = None
    pipeline: dict | None = None


class RunOut(BaseModel):
    id: str
    run_key: str
    chain_id: str
    trigger: str
    scheduled_at: str | None = None
    status: str
    scanned: int
    dropped: int
    candidates: int
    selected: int
    settings_version_id: int | None = None
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    progress: int = 0
    progress_label: str | None = None


class FunnelOut(BaseModel):
    scanned: int
    dropped: int
    candidates: int
    selected: int


class RuleCountOut(BaseModel):
    key: str
    label: str
    count: int | None = None


class ChangeOut(BaseModel):
    kind: str
    direction: str          # in | out
    sym: str
    detail: str


class SourceCountOut(BaseModel):
    name: str
    count: int = 0


class PushOut(BaseModel):
    status: str
    delay: str = ""
    unavailable: str = ""  # why no status was recorded; never shown as a status


class SegmentOut(BaseModel):
    kind: str               # text | bold | code | link
    text: str
    url: str | None = None


class MetricOut(BaseModel):
    key: str
    value: str
    na: bool = False
    long: bool = False
    title: str = ""
    #: Empty for plain rows. 貔貅 carries ok/danger/warn so the card can box the verdict.
    tone: str = ""


class DeltaOut(BaseModel):
    key: str | None = None
    value: str | None = None


class PillOut(BaseModel):
    label: str
    tone: str


class ChipOut(BaseModel):
    label: str
    value: str
    tone: str


class PoolOut(BaseModel):
    """One pool the token trades in, as the chain feed lists it."""

    name: str = ""
    liquidity: str = ""
    pair: str = ""
    #: The unabbreviated pair id, for the tooltip and for copying.
    pair_full: str = ""


class PoolsOut(BaseModel):
    rows: list[PoolOut] = []
    #: How many pools there were in total, when the table is shorter than that.
    note: str = ""


class HolderOut(BaseModel):
    rank: int
    addr: str = ""
    addr_full: str = ""
    pct: str = ""
    tags: list[str] = []


class HoldersOut(BaseModel):
    rows: list[HolderOut] = []
    #: Burn / locked / contract share of supply among the holders listed.
    summary: list[ChipOut] = []
    note: str = ""


class SafetyOut(BaseModel):
    perms: list[PillOut]
    lp: PillOut
    chips: list[ChipOut]
    #: The remaining flags the chain feed set, and what the owner and deployer still hold.
    warnings: list[PillOut] = []
    holdings: list[ChipOut] = []
    pools: PoolsOut = Field(default_factory=PoolsOut)
    holders: HoldersOut = Field(default_factory=HoldersOut)


class RanksOut(BaseModel):
    liquidity: int | None = None
    volume: int | None = None
    holders: int | None = None


class SparkOut(BaseModel):
    path: str
    trend: str
    delta: str = ""


class DetailOut(BaseModel):
    title: str
    variant: str = "plain"
    body: list[SegmentOut]


class LinkOut(BaseModel):
    label: str
    url: str


class CardOut(BaseModel):
    rank: int
    ca: str
    sym: str
    tag: str = ""
    fit_label: str = ""
    is_new: bool = False
    score: float | None = None
    onchain: float | None = None
    product: float | None = None
    onchain_weight: float = 55
    product_weight: float = 45
    ranks: RanksOut
    spark: SparkOut
    verdict: list[SegmentOut]
    #: Set when the conclusion is older than the numbers beside it: "解读写于 3 小时前 ·
    #: 本轮数据已更新". Empty when this round wrote the words.
    entry_note: str = ""
    metrics: list[MetricOut]
    deltas: list[DeltaOut]
    safety: SafetyOut
    details: list[DetailOut]
    links: list[LinkOut]


class DeclineRowOut(BaseModel):
    #: The full address behind `sym`, so the overview can offer a copy button instead of a
    #: column of 42-character strings no one reads.
    ca: str = ""
    sym: str
    market_cap: str
    liquidity: str
    holders: str
    entrapment: str
    score: float | None = None
    reason: str


class GateOut(BaseModel):
    judged: int = 0
    unjudged: int = 0


class BatchOut(BaseModel):
    run: RunOut
    batch_label: str
    funnel: FunnelOut
    #: The hard filter's share of `funnel.dropped`; the safety gate owns the rest. None
    #: for rounds written before the split existed, whose `funnel.dropped` was the
    #: hard filter's own subtotal already.
    dropped_hard: int | None = None
    gate: GateOut = Field(default_factory=GateOut)
    rule_counts: list[RuleCountOut]
    changes: list[ChangeOut]
    caliber: list[str]
    sources: list[SourceCountOut]
    push: PushOut
    note: str = ""
    #: Wall clock per pipeline phase, in seconds, keyed by the stopwatch's own names
    #: (pull, dedupe, prefilter, gate, score, sharpen, draft, commentary, write). Empty
    #: until the first phase ends, and empty for rounds that predate it.
    phase_timings: dict[str, float] = Field(default_factory=dict)
    cards: list[CardOut]
    decline: list[DeclineRowOut]


class SettingsOut(BaseModel):
    chain_id: str
    version: int
    source: str
    settings_version_id: int | None = None
    created_at: str | None = None
    note: str | None = None
    payload: dict = Field(default_factory=dict)


class FieldOptionOut(BaseModel):
    value: str
    label: str
    count: int | None = None


class FieldOut(BaseModel):
    key: str
    key2: str | None = None      # range widgets carry two keys
    label: str
    dim: str | None = None
    dim_in: str | None = None   # where the dim sits: label | field
    widget: str             # num | range | bool | select | checks | text | ro
    op: str | None = None
    unit: str | None = None
    value: Scalar = None
    options: list[FieldOptionOut]
    checks: list[FieldOptionOut]
    drop_count: int | None = None
    drop_x: str = ""        # the note a counted row swaps in once it goes dirty
    drop_prefix: str = "-"  # counted rows read as eliminations unless told otherwise
    ro_row: bool = False    # a system-supplied row: sunken, its label muted
    ro_value: bool = False  # the value sits next to an action, so it clamps
    ro_button: str = ""    # the inert action the prototype puts beside the value
    ro_button_note: str = ""  # why that action is inert
    pending: str | None = None
    enabled: bool | None = None
    enabled_key: str | None = None
    check_label: str = ""
    warn: str = ""


class GroupOut(BaseModel):
    key: str
    title: str
    hint: str = ""
    fields: list[FieldOut]
    #: The line that has to stay on the page: a reading of this round, or a rule the
    #: operator has to act on. The reason behind it travels in `detail`, behind the icon.
    note: str = ""
    #: The 口径 folded into an icon: rules, definitions, what a field does not do. Read
    #: once and then in the way, so it is opened on demand rather than printed every time.
    detail: str = ""
    warn: str = ""


class FormTotalsOut(BaseModel):
    universe: int = 0
    candidates: int = 0
    selected: int = 0
    onchain_avg: float | None = None
    product_avg: float | None = None
    total_avg: float | None = None
    lexicon_size: int = 0
    lexicon_hit: int = 0
    lexicon_updated: str | None = None
    #: How many entries the refresh rules would rewrite today, and where the deep pass
    #: stands. Both are read-only rows on the settings page.
    lexicon_stale: int = 0
    lexicon_research: str | None = None
    run_seconds: int | None = None
    last_batch: str | None = None


class LexiconRowOut(BaseModel):
    ca: str
    symbol: str | None = None
    tag: str | None = None
    narrative: float | None = None
    product: float | None = None
    #: The token size, formatted the way the card formats money ($64.2K): the market cap
    #: its newest snapshot carries. The row baseline_mcap is the filing moment, not
    #: today, so this is the one number that says how big it is now.
    mcap: str = "—"
    social: str | None = None
    concl: str | None = None
    review_state: str
    updated_at: str
    current: bool
    #: The freshness badge, the research state, and the evidence the deep pass filed - the
    #: same three things the card and the archive show, computed by the same rule.
    refresh_reason: str | None = None
    baseline_at: str | None = None
    age_hours: float | None = None
    stale: bool = False
    research_state: str = "none"
    research_note: str | None = None
    sources: list["LinkOut"] = []


class BlockOut(BaseModel):
    key: str
    title: str
    weight: str = ""
    columns: int = 3
    groups: list[GroupOut]


class SettingsFormOut(BaseModel):
    chain_id: str
    label: str
    version: int
    note_bar: list[SegmentOut]
    #: The line in the bar that cannot be read off the page: how a change takes effect, and
    #: that a batch already run keeps the口径 it ran under. Opens from the bar's 说明 icon.
    note_detail: str = ""
    #: The reading rules for the lexicon table: what 「本轮」 marks, that both score
    #: columns are the raw 0-5 judgement, and what the weight folds them by. A rule,
    #: not a reading, so it opens from the table header's icon instead of printing
    #: itself under the rows.
    lexicon_note: str = ""
    dirty_hint: str
    blocks: list[BlockOut]
    totals: FormTotalsOut
    lexicon: list[LexiconRowOut]
    blocked: list[str]


class LexiconOut(BaseModel):
    ca: str
    chain_id: str = "arc"
    symbol: str | None = None
    tag: str | None = None
    narrative: float | None = None
    product: float | None = None
    social: str | None = None
    concl: str | None = None
    narr_text: str | None = None
    prod_text: str | None = None
    mech_text: str | None = None
    pros: str | None = None
    cons: str | None = None
    watch: str | None = None
    scenario: str | None = None
    review_state: str
    evidence_json: str | None = None
    updated_at: str
    #: The freshness bookkeeping. `entry_note` on a card is built from these: how old the
    #: words are, why they were rewritten last, and where the deep pass stands.
    refresh_reason: str | None = None
    baseline_mcap: float | None = None
    baseline_at: str | None = None
    age_hours: float | None = None
    stale: bool = False
    research_state: str = "none"
    research_note: str | None = None
    #: The pages the deep pass read, so the archive can show its evidence.
    sources: list["LinkOut"] = []


class ErrorOut(BaseModel):
    detail: str
