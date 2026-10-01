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

from typing import Literal

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


class ChainConfigOut(BaseModel):
    """One set of settings on a chain, as a switcher needs to list it.

    The口径 themselves are read one at a time through `/settings`; this is the name, the
    switch, and whether the entry can be taken away at all.
    """

    id: str
    label: str
    enabled: bool = True
    #: The default entry. A chain always keeps one set of settings to fall back to, so
    #: this one cannot be deleted.
    locked: bool = False


class ChainOut(BaseModel):
    id: str
    label: str
    #: The chain's own name (config.chain_name), never the radar's: the rail and the drawer
    #: print `label`, and a sentence about the chain itself - a search query, a kept card's
    #: provenance line - wants "Arc", not "Arc 雷达".
    name: str = ""
    enabled: bool
    explorer: str | None = None
    sources: list[str]
    schedule: dict | None = None
    report: dict | None = None
    pipeline: dict | None = None
    #: Every set of settings this chain carries, in file order, the default first. The
    #: settings fields above are the default one's, so a client that never learned about
    #: the switcher keeps reading exactly what it read before.
    configs: list[ChainConfigOut] = []
    #: Something about the file a human has to fix, as the backend's own sentence. A chain
    #: file with two sets of settings switched on is the one case today: it is read as the
    #: first one winning, and the page prints this so the file gets edited.
    warning: str | None = None


class LibraryOut(BaseModel):
    """How much one set of settings has, for the dialog that asks before deleting it."""

    runs: int = 0
    reports: int = 0
    entries: int = 0
    versions: int = 0


class ChainConfigDetailOut(ChainConfigOut):
    """One set of settings, with the size of what deleting it would destroy."""

    library: LibraryOut = LibraryOut()


class ChainConfigIn(BaseModel):
    """Add a set of settings: a fresh one, or a copy of one that exists.

    `label` is what the dialog had in it. Blank means "use the naming rule" - `配置 N` for a
    new one, `<源> 副本` for a copy - which is what the page prefills anyway.
    """

    mode: Literal["new", "copy"] = "new"
    from_config_id: str | None = None
    label: str | None = None


class LabelIn(BaseModel):
    """The new name for a set of settings. Only the label moves; the id never does."""

    label: str = ""


class DeleteIn(BaseModel):
    """The name typed back by whoever asked for the delete.

    A delete that cannot be undone gets a gate a click cannot pass by accident: the page
    makes the human type the label, and this is that same word arriving here. A mismatch is
    a refusal rather than a shrug, because the only caller that gets it wrong is one that
    did not mean it.
    """

    label: str = ""


class RunOut(BaseModel):
    id: str
    run_key: str
    chain_id: str
    #: Which of the chain's sets of settings this round ran under. Two sets keep two
    #: report libraries, and this is what tells them apart.
    config_id: str = "default"
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
    badge: str = ""
    body: list[SegmentOut]


class LinkOut(BaseModel):
    label: str
    url: str


class CardOut(BaseModel):
    rank: int
    ca: str
    sym: str
    #: The token's square mark, as a URL - empty when no source had one. The card draws the
    #: slot either way, so a card with no picture is the same card with nothing in that box
    #: rather than a card of a different height.
    logo: str = ""
    tag: str = ""
    is_new: bool = False
    score: float | None = None
    onchain: float | None = None
    product: float | None = None
    onchain_weight: float = 55
    offchain_weight: float = 45
    ranks: RanksOut
    spark: SparkOut
    verdict: list[SegmentOut]
    metrics: list[MetricOut]
    deltas: list[DeltaOut]
    safety: SafetyOut
    details: list[DetailOut]
    links: list[LinkOut]
    #: Why the off-chain half is empty, when it is. A round's card never carries one - a token
    #: with no readable link is not written as a card at all there - but a CA search answers
    #: with whatever the token does have, so the card says which half it could not read
    #: instead of leaving the 结论 block looking broken (app/lookup.py).
    research_note: str = ""


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
    #: (pull, gate, score, draft, write). Empty until the first phase ends, and empty
    #: for rounds that predate it.
    phase_timings: dict[str, float] = Field(default_factory=dict)
    cards: list[CardOut]
    decline: list[DeclineRowOut]



class FavoriteOut(BaseModel):
    """One card on file for a token: collected, or a record of a CA search for it.

    A copy rather than a reference, on purpose: a report row is deleted with its round, and
    a whole report library can be dropped, while a card the reader holds on to has to stay
    readable. It is also why `config_id` is not part of the key - a card can come from a
    chain this app does not screen, so the table is keyed by (chain_id, ca) alone. It is a
    value: the set of settings the round ran under, for the line the card's foot prints.

    Two kinds of row answer to this, and the reader only sees the difference on the card's
    foot: a round's card carries `origin_run_id` and `config_id`, and a CA search's carries
    neither (`origin = 'search'`, no score - there is no pool to rank in, app/lookup.py). A
    round that writes one of these tokens again replaces the payload in place
    (report/assemble.py `refresh_kept`), so `origin_run_id` / `origin_label` move forward
    while `saved_at` stays at the moment the row was first put here.
    """

    chain_id: str
    ca: str
    symbol: str | None = None
    origin: str = "round"               # round | search
    origin_run_id: str | None = None
    origin_label: str = ""
    score: float | None = None
    rank: int | None = None
    config_id: str | None = None
    saved_at: str
    updated_at: str
    card: CardOut


class JobStageOut(BaseModel):
    """One row of a search's step list: what the stage is called and where it has got to.

    The four states are the ones a round's step list draws too (pending / run / done /
    failed), so the two panels cannot disagree about what a stopped stage looks like.
    `seconds` is filled the moment the stage ends and stays empty until then.
    """

    key: str
    label: str
    state: str                       # pending | run | done | failed
    seconds: float | None = None


class JobOut(BaseModel):
    """One CA search, as the page polls it.

    A job rather than a synchronous answer, because the search is minutes long and a page
    that shows which stage it is on needs somewhere to read that from. `row` is the stored
    card once the status is `done` - the same row the shelf and the recent list read, so a
    searched card cannot be two different cards depending on how the page got it.
    """

    id: str
    ca: str
    chain_id: str | None = None
    status: str                       # running | done | failed
    stage: str | None = None          # the stage running now; a finished job carries none
    stages: list[JobStageOut] = []    # in order, first to last
    error: str | None = None
    row: FavoriteOut | None = None
    started_at: str
    finished_at: str | None = None


class RemovedOut(BaseModel):
    removed: bool




class SettingsOut(BaseModel):
    chain_id: str
    config_id: str = "default"
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


class FormTotalsOut(BaseModel):
    universe: int = 0
    candidates: int = 0
    selected: int = 0
    onchain_avg: float | None = None
    product_avg: float | None = None
    total_avg: float | None = None
    run_seconds: int | None = None
    last_batch: str | None = None


class BlockOut(BaseModel):
    key: str
    title: str
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
    dirty_hint: str
    blocks: list[BlockOut]
    totals: FormTotalsOut
    blocked: list[str]


class LexiconOut(BaseModel):
    ca: str
    chain_id: str = "arc"
    config_id: str = "default"
    run_id: str | None = None
    symbol: str | None = None
    tag: str | None = None
    #: The five 0-5 judgements this round made about the project.
    f_completeness: float | None = None
    f_substance: float | None = None
    f_team: float | None = None
    f_narrative: float | None = None
    f_community: float | None = None
    social: str | None = None
    concl: str | None = None
    narr_text: str | None = None
    prod_text: str | None = None
    substance_text: str | None = None
    team_text: str | None = None
    watch: str | None = None
    scenario: str | None = None
    evidence_json: str | None = None
    updated_at: str
    #: The pages the reading pass kept, so an entry can show its evidence.
    sources: list["LinkOut"] = []


class ErrorOut(BaseModel):
    detail: str
