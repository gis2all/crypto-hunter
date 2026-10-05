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

from typing import Any, Literal

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
    widget: str             # num | range | bool | select | checks | text | password | ro
    op: str | None = None
    unit: str | None = None
    value: Scalar = None
    options: list[FieldOptionOut]
    checks: list[FieldOptionOut]
    #: Values the page may *type for* the operator into a box that stays a box - the gateway
    #: addresses this install is usually pointed at. A shortcut, never a whitelist.
    presets: list[FieldOptionOut] = []
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


class ActionOut(BaseModel):
    """A button a group carries. What it does is the page's business; what it says is not.

    The label travels with it (2.3.3), and the id is the one word the page does act on - it is
    the map from a name to a handler, which is behaviour and belongs to the page. A group with
    no actions draws no strip.
    """

    id: str                 # models | test
    label: str


class GroupOut(BaseModel):
    key: str
    title: str
    hint: str = ""
    fields: list[FieldOut]
    actions: list[ActionOut] = []
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


class SystemFormOut(BaseModel):
    """系统设置: this machine's own wiring - the LLM endpoint, the X session, the log level.

    The blocks are a chain's 设定 blocks on purpose: one shape, so the two pages are drawn by
    the same renderer and a row cannot mean one thing here and another there. `path` is the
    file the rows are written to, which is the one thing this page has and a chain's does not.
    """

    path: str
    blocks: list[BlockOut]


class SystemModelsOut(BaseModel):
    """What `/models` answered, and which row the answer is for.

    `apply_to` travels so the page can offer the list on the right row without learning a key
    name - the same reason the rows themselves arrive composed (8.9.1).
    """

    ok: bool
    models: list[str] = []
    apply_to: str = ""
    note: str = ""


class SystemTestLineOut(BaseModel):
    """One key's probe: the row it came from, and what the gateway answered it."""

    label: str
    ok: bool
    note: str
    status: int | None = None
    latency_ms: int | None = None
    chars: int | None = None
    finish_reason: str = ""


class SystemTestOut(BaseModel):
    """One small real call per configured key, in the order a round would try them (8.9.7)."""

    ok: bool
    lines: list[SystemTestLineOut]


class SystemXOut(BaseModel):
    """Whether the X session still works, in the client's own words (8.9.10).

    One sentence rather than 测试连接's line per key: there is one session, and it either is
    logged in or says why it is not.
    """

    ok: bool
    note: str


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


class FeedItemOut(BaseModel):
    """One source saying one thing, exactly as it was read.

    The raw record, not a summary of it: an issue is allowed to be wrong about what a thing
    means, but it must never be wrong about who said it and when. This is the shape it can be
    checked against.
    """

    source_kind: str                  # rss | x | exchange | regulator
    source_id: str                    # the independent party; heat counts these, not records
    external_id: str                  # that source's own id for it
    title: str
    url: str
    published_at: str
    body: str = ""
    author: str = ""


class FeedJudgementOut(BaseModel):
    """What the reading pass decided about one event.

    A model rather than a bare dict (the rule this file opens with): an untyped answer reaches
    the page as `unknown`, the page has to guess, and guessing is how a label ends up hardcoded
    in the frontend and drifting from the rule that produced it.

    `certainty_label` is the whole reason this is a contract. An event two parties reported and
    an event one party reported read identically once each is one sentence long, and this is the
    field that keeps them from looking identical on the page.
    """

    provider: str                      # rules | llm - how this reading was produced
    summary: str
    why: str
    certainty: str                     # fact | discussion
    certainty_label: str


class FeedEventOut(BaseModel):
    """One thing that happened, and how many independent parties said so.

    `sources` is the count everything downstream is built on, and it is deliberately not
    `len(discoveries)`: one wire service reprinted nine times is one party, and a page that
    showed nine would make a story look hot when it is not. `heat` is that count discounted by
    how long ago the last word came in.

    `judgement` is the reading pass's answer, and it travels with the event rather than being
    baked into the text: `certainty` is the one field a reader must be able to see, because a
    fact and a rumour of one look identical once they are summarised.
    """

    identity_key: str
    kind: str = ""
    kind_label: str = ""
    title: str
    url: str
    first_seen_at: str
    last_seen_at: str
    sources: int = 0
    heat: float = 0.0
    judgement: FeedJudgementOut | None = None
    discoveries: list[FeedItemOut] = []


class FeedMetricOut(BaseModel):
    """报头上的一个数, 连同它旁边那个词.

    The word is not decoration and it is not the page's to write: "个来源" means "independent
    parties after dedupe by source_id", which is a fact about a rule in `heat.py`. A page that
    wrote its own word would be restating that rule a second time, and it would drift.
    """

    key: str
    label: str
    value: int


class FeedOutlineOut(BaseModel):
    """正文目录的一条 (AIHOT 的 `OutlineEntry`): 锚点、标题、层级.

    锚点是后端注进正文的 (`sec-N`), 所以页面点目录只要滚到那个 id 上, 不必自己认标题.
    """

    id: str
    text: str
    level: int = 2


class FeedHighlightOut(BaseModel):
    """这一期的要点一条 (AIHOT 的 `highlights`): 标题、一句摘要、几家在说."""

    identity_key: str
    title: str
    summary: str = ""
    sources: int = 0
    kind_label: str = ""


class FeedFlashOut(BaseModel):
    """纸尾那一行快讯 (AIHOT 的 `flashes`): 只够一行的一条, 点开才是详情."""

    identity_key: str
    title: str
    last_seen_at: str = ""


class FeedEditionOut(BaseModel):
    """One issue.

    `kind` and `key` are the period it is about: `key` is a name a reader recognises
    (`2026-10-03` / `2026-W40` / `2026-10`), which is why the page does not derive one - a week
    number cannot be recovered from a timestamp without re-implementing ISO weeks.

    `seen` and `entries` are different numbers on purpose: the window held `seen` records and
    `entries` of them were worth printing. A quiet morning is an issue that read four hundred
    things and printed three, which is a finding - not the same screen as a run that failed.
    """

    id: str
    kind: str                          # day | week | month
    key: str
    window_from: str
    window_to: str
    seen: int = 0
    issue_number: int = 0
    headline: str = ""
    #: 导语 (5.7): 模型写的一段话, 空 = 这一期没有 (没配模型, 或模型这一趟不可用).
    lead: str = ""
    metrics: list[FeedMetricOut] = []
    created_at: str
    entries: list[FeedEventOut] = []
    #: 这一期的要点 (AIHOT 的 `highlights`): 判词是「事实」档的那几条. 空则不画那一栏.
    highlights: list[FeedHighlightOut] = []
    #: 纸尾那一行快讯 (AIHOT 的 `flashes`): 只够一行、没被展开的条目.
    flashes: list[FeedFlashOut] = []
    #: 同一门刊里相邻的两期, 给的是 id. 空 = 到头了, 页面那一侧的按钮不画.
    prev: str = ""
    next: str = ""


class FeedEditionRowOut(BaseModel):
    """One issue in the list, without its body: a list draws a mark, a title and a time.

    `headline` and `issue_number` are here rather than derived on the page for the same reason
    they are stored: the list loads the last few dozen issues, and neither "what was this issue
    about" nor "which issue is this" can be read off a truncated list.
    """

    id: str
    kind: str
    key: str
    window_from: str
    window_to: str
    seen: int
    entries: int
    issue_number: int
    headline: str
    created_at: str
class FeedCategoryOut(BaseModel):
    """筛选栏上的一个类别: 它的名字, 以及这一类在当前这批里有多少条.

    `section` 是它在一份刊物里归到哪一节 —— 那是类别的属性, 不是页面的属性, 所以它跟着
    类别走 (日报还没做, 见 DESIGN.md 8).
    """

    key: str
    label: str
    count: int = 0
    section: str = ""


class FeedOtherReportOut(BaseModel):
    """同一件事里, 别家还怎么说过它 —— 卡片上「另有 N 家信源报道」展开后的那一行.

    署名是**来源**不是作者: 折叠行数的是"几家在说", 所以这一列只到来源为止.
    """

    #: 这一条报道自己的 id, 和列表行同一个拼法. 点标题进它自己的页, 不是跳出去.
    id: str = ""
    source_name: str = ""
    title: str = ""
    url: str = ""
    time_label: str = ""


class FeedDisplayTagOut(BaseModel):
    """卡片上的一格角标: 印什么字, 以及点不点得动."""

    text: str
    #: 实体 (牵了谁) 点得动 —— 点开是主题页; 分类标签点不动. 页面据此决定给不给链接.
    entity: bool = False


class FeedListItemOut(BaseModel):
    """列表里的一行, 全部动态与精选共用.

    这是**条目**不是事件: 一个条目有唯一的一个来源、唯一的一个时刻、唯一的一个类别, 而这三样
    正是一个读者扫列表时看的东西. 要读"多少家都在说", 看热点榜.

    `score` 是这一行的分量 (0-100), `reason` 是"为什么值得看", `summary` 是它说了什么.
    三者都随行走, 页面不再查任何地方.
    """

    id: str
    #: 这条属于哪个事件. 点击以后要去的地方, 页面不用自己推.
    story_id: str = ""
    title: str
    #: 源自己的标题, 只在模型写过这一条、而且与上面那条**不同**的时候给 —— 页面据此决定
    #: 要不要给「原文」那个开关. 两样一样时给空串, 免得页面拿一个和正文重复的按钮去问读者.
    title_original: str = ""
    #: 这一条的字**是不是模型写的**. 页面上「AI 导读」这个说法是不是真的, 由它决定.
    written: bool = False
    url: str
    summary: str = ""
    reason: str = ""
    category: str = ""
    category_label: str = ""
    tags: list[str] = []
    entities: list[str] = []
    #: 卡片角标那一串: 实体与分类标签并成一行, `entity` 说这一格点不点得动 (AIHOT 的 `displayTags`).
    display_tags: list[FeedDisplayTagOut] = []
    score: int = 0
    first_party: bool = False
    source_id: str
    source_name: str
    source_kind: str = ""
    author: str = ""
    published_at: str
    time_label: str = ""
    #: 这条自带的图 (X 这类来源才有), 原样的 url. 页面当缩略图画, 空的就是没有图.
    media: list[str] = []
    #: 同一件事里别家的报道: 卡片上「另有 N 家信源报道」展开后的列表 (最多 5 条).
    other_reports: list[FeedOtherReportOut] = []
    #: 上面那个折叠行印的数. 是**信源家数**, 不是列表长度 —— 列表截断过.
    other_report_count: int = 0
    #: 这一条走哪一路 (AIHOT 的 `channelOf`): `news` / `x`. 卡片按它筛选.
    channel: str = ""
    #: 正文档位 (AIHOT 的 `bodyModeOf`): `full` = 站内印全文, `summary` = 只印摘要.
    body_mode: str = "summary"
    #: 这一条有没有自己的页.
    has_page: bool = True
    #: X 这一条的版式字段; 不是 X 的条目这里是 None —— 页面据此选一套版式, 而不是猜.
    x_view: FeedXViewOut | None = None
    #: 分析档位 (4.9b): `primary` / `composite` / 空 = 还没判过.
    analysis_scope: str = ""
    #: 上面那一档在页面上印的字 (`单一事实` / `综合`), 空 = 不印.
    scope_label: str = ""


class FeedDayOut(BaseModel):
    """一天. 日期与星期是这一组的头, 条数是右边那个数."""

    date: str
    date_label: str
    weekday_label: str
    count: int = 0
    items: list[FeedListItemOut] = []


class FeedChannelOut(BaseModel):
    """筛选栏上的一路: 它的键, 以及这一路在当前这批里有多少条.

    `all` / `news` / `x` / `firstParty` 由后端给 (AIHOT 的 `CHANNEL_KEYS`): 页面不认识
    任何一路, 加一路只改 `feed/rules.py`, 前端一个字节都不动.
    """

    key: str
    count: int = 0


class FeedQuotedOut(BaseModel):
    """被引的那条帖子 (AIHOT 的 `XPostView.quoted`)。

    `translation` 是给读者看的那一份 (3.6): 别人的原话单独走一遍翻译, 不是正文那一段的译文。
    它为空就是还没译上, 页面只印原文.
    """

    handle: str = ""
    author_name: str = ""
    text: str = ""
    translation: str = ""
    url: str = ""


class FeedXViewOut(BaseModel):
    """X 这一条在卡片上的几格 (AIHOT 的 `XPostView`)。不是 X 的条目没有这一个对象。"""

    author: str = ""
    text: str = ""
    original: str = ""
    media: list[str] = []
    quoted: FeedQuotedOut | None = None


class FeedFaceOut(BaseModel):
    """参与者头像 (AIHOT 的 `Faces.tsx`): 一家一枚, 没图用首字母。"""

    source_id: str = ""
    name: str = ""
    initial: str = "A"
    icon: str = ""


class FeedItemsOut(BaseModel):
    """全部动态 (scope=all) 与精选 (scope=curated) 共用的一份答案."""

    scope: str
    category: str = ""
    channel: str = "all"
    query: str = ""
    page: int = 1
    page_size: int = 0
    next_cursor: str = ""
    window_hours: float = 0.0
    total: int = 0
    filtered: int = 0
    shown: int = 0
    channels: list[FeedChannelOut] = []
    categories: list[FeedCategoryOut] = []
    days: list[FeedDayOut] = []


class FeedTopicRowOut(BaseModel):
    """主题页左栏的一行: 一个实体, 以及它在窗口里有多少条.

    `id` 就是名录里的名字 (`Binance` / `BNB Chain`), 因为它已经是唯一的, 再编一个 id 只是
    多一处能对不上的东西. 页面拿它当 URL 的一部分, 所以它必须稳 —— 改词表里的写法就是改网址.
    """

    id: str
    name: str
    count: int = 0


class FeedTopicGroupOut(BaseModel):
    """左栏的一段. `name` 是页面上那行小标题 (链与协议 / 交易所 / 机构与监管)."""

    key: str
    name: str
    rows: list[FeedTopicRowOut] = []


class FeedTopicsOut(BaseModel):
    """主题页左栏的目录.

    `window_days` 是那句「近 N 天」里的 N. 由后端给而不是页面写死 30: 窗口的长短是一条
    规则 (`views.TOPIC_HOURS`), 规则改了词要跟着改, 而页面不认识那条规则 (CLAUDE.md 2.3.3).
    """

    window_days: float = 0.0
    groups: list[FeedTopicGroupOut] = []


class FeedTopicItemsOut(BaseModel):
    """一个实体名下的条目, 按天分组.

    与 `FeedItemsOut` 同源 (`by_day`), 少了筛选栏那几样: 左栏就是筛选栏, 这里不需要第二份
    类别计数. `topic` 原样带回, 所以页面不必记住自己刚才问的是哪条线.
    """

    topic: str
    window_days: float = 0.0
    shown: int = 0
    days: list[FeedDayOut] = []


class FeedStoryItemOut(BaseModel):
    """热点榜里, 一条故事下面挂着的一条来源."""

    source_name: str
    first_party: bool = False
    title: str
    url: str
    time_label: str = ""


class FeedStoryOut(BaseModel):
    """热点榜上的一条.

    `sources` 是整件事见过的独立来源数, `participants` 是**窗口里**还在说的家数, 其中
    `source_count` 家是发布源、`signal_count` 家是讨论源 (页面印的"来源数"是 `source_count`:
    讨论帖不是"谁在报这件事"). `heat_index` 是热度指数 (10 倍值, 一位小数), `series` 是榜单
    那一刻往前 24 小时的 25 个**小时**格子, 缺的小时补 `null` —— 读的是落库的快照, 不是每个
    请求重算一遍 (snapshot_heat). `trend` 是五态走势 (new/unknown/up/down/flat), `trend_pct` 是
    它那一眼的具体涨幅 —— `None` 表示那时它还不在. `hot_badges` 是标题上那几枚小标签, 可以同时
    带 `surge` 和 `new` (heat.badges).
    """

    id: str
    title: str
    url: str
    #: 最新进展: 这个事件最新的一条标题.
    latest: str = ""
    category: str = ""
    category_label: str = ""
    tags: list[str] = []
    entities: list[str] = []
    #: 卡片角标那一串 (同 `FeedListItemOut.display_tags`).
    display_tags: list[FeedDisplayTagOut] = []
    score: int = 0
    sources: int = 0
    participants: int = 0
    source_count: int = 0
    signal_count: int = 0
    source_names: list[str] = []
    heat_index: float = 0.0
    series: list[float | None] = []
    trend: str = ""
    trend_pct: float | None = None
    hot_badges: list[str] = []
    first_seen_at: str
    last_seen_at: str
    lead_source: str = ""
    items: list[FeedStoryItemOut] = []
    #: 这件事里**真报道**的条数 (讨论证据不算): 卡片上那个折叠行印的是它.
    item_count: int = 0
    #: 参与者头像 (AIHOT 的 `Faces.tsx`): 最近在说的几家, 最多几枚.
    faces: list[FeedFaceOut] = []


class FeedHotOut(BaseModel):
    window_hours: float = 0.0
    generated_at: str
    total: int = 0
    stories: list[FeedStoryOut] = []


class FeedItemDetailOut(FeedListItemOut):
    """一条的整页.

    继承列表那一行而不是另写一份: 同一件事写两遍, 迟早有一边多一个字段, 而读的人不知道哪边
    是对的. 这里只加**只有详情页看得见**的那几样.
    """

    #: 正文的原文, 净化过的块级 HTML (htmlbody.py): 块是源自己的段落与小标题, 行内标签压成
    #: 了文字. 这一格是页面印正文的来源, 也是译文那一份的结构.
    body_html: str = ""
    #: 正文的中文译文 (同一套块). 空 = 没译过: 中文源, 或还没译完/不该译的条目.
    body_zh: str = ""
    #: 译文是不是整篇. 有译文而这一格是 false 时, 页面在正文上面加一行提示.
    body_complete: bool = False
    domain: str = ""
    published_label: str = ""
    published_ago: str = ""
    #: 这条属于的那件事叫什么. 空 = 它那件事没进窗口, 页面就不画那一行.
    story_title: str = ""
    #: 「事件后续」(AIHOT 的 `StoryFollowups.tsx`): 这件事别家还怎么说过它, 最新在前.
    followups: list[FeedOtherReportOut] = []
    followup_count: int = 0
    #: 这一条的资格明细 (5.5): 每一道门槛的名字、过没过、卡在哪一道.
    eligibility: "FeedGateResultOut | None" = None
    #: 这条**提及**了哪几件事 (4.9b): 复合分析的那一格, 提及不是证据所以与 followups 分开.
    mentions: list["FeedMentionOut"] = []
    #: 正文的目录 (AIHOT 的 `outline`): 标题的锚点就注在 `body_html` / `body_zh` 里.
    outline: list[FeedOutlineOut] = []


class FeedMentionOut(BaseModel):
    """复合分析提到的一件事 (4.9b): 事件 id、标题与它挂在哪个事实上."""

    story_id: str = ""
    story_title: str = ""
    fact_id: int = 0


class FeedGateResultOut(BaseModel):
    """一道门槛在**这一条**上的结论 (5.5). `ok` 是 null = 这一层答不上来, 不是"没过"。"""

    key: str = ""
    label: str = ""
    ok: bool | None = None
    detail: str = ""


class FeedGateOut(BaseModel):
    """门槛本身 (5.5): 叫什么、问什么、为什么是这个口径。"""

    key: str = ""
    label: str = ""
    question: str = ""
    why: str = ""
    needs: list[str] = []


class FeedEligibilityOut(BaseModel):
    """资格的全体 (5.5): 名单 + 那两条数字线。"""

    release_delay_seconds: int = 0
    curated_min_score: int = 0
    gates: list[FeedGateOut] = []


class FeedTimelineItemOut(BaseModel):
    """事件时间线上的一行. 有 id, 所以点得进去 —— 时间线是去条目详情页的另一个入口."""

    id: str
    title: str
    url: str
    source_name: str = ""
    first_party: bool = False
    category_label: str = ""
    score: int = 0
    time_label: str = ""
    #: 被折叠起来的同源条数 (4.10). 0 = 这一行就是一条.
    folded: int = 0
    #: 这一行代表的那几条的 id (头一个就是上面的 `id`).
    ids: list[str] = []


class FeedTimelineDayOut(BaseModel):
    """一天. 和列表页的按天分组同一个形状, 理由也一样: 日期是这套语言的写法, 由后端写."""

    date: str
    date_label: str
    weekday_label: str
    count: int = 0
    items: list[FeedTimelineItemOut] = []


class FeedStoryRefOut(BaseModel):
    """关联事件那一栏的一行 (AIHOT 的 `StoryRef`): 另一件事的 id、标题与关系词。

    `relation` 是枚举 (`storyline` / `related`), `relation_label` 是它在这一页上的中文 ——
    词由后端给, 页面不认识那个枚举 (CLAUDE.md 2.3.3).
    """

    id: str
    title: str
    relation: str = "related"
    relation_label: str = ""
    last_seen_ago: str = ""


class FeedHeatPointOut(BaseModel):
    """热度曲线上的一格 (AIHOT `heatSeries` 的 `HeatPoint`): 一个小时的指数与参与者家数。"""

    hour: str
    heat: float = 0.0
    participants: int = 0


class FeedStoryDetailOut(FeedStoryOut):
    """一件事的整页.

    `items` 是卡上那份"谁在说", `days` 是同一批报道按天的分组 —— 两个投影来自同一份列表,
    算在同一个函数里, 所以不可能对不上; 页面读 `days`.
    """

    #: 榜内名次, 1 起. 不在榜上的事没有名次, 所以是 0 不是 1.
    rank: int = 0
    badges: list[str] = []
    #: AI 综述: 分段. 规则版列出最新的几家各自说了什么, 模型接手后重写成一整段.
    summary: list[str] = []
    recent_sources: int = 0
    recent_reports: int = 0
    first_seen_ago: str = ""
    last_seen_ago: str = ""
    #: 逐小时的热度曲线 (7 天), 只含观察完整的小时 (snapshot_heat.series / AIHOT 的
    #: `heatSeries`). 空 = 还没有快照, 页面退回卡片上那条 24 小时的线.
    heat: list[FeedHeatPointOut] = []
    #: 关联事件 (AIHOT 的 `relatedStories`): 对面还活着、还有人在报, 最多 8 条.
    related: list[FeedStoryRefOut] = []
    days: list[FeedTimelineDayOut] = []
    #: 当事方自己发的报道 (AIHOT 的 `officialReports`): 与 `other_reports` 互补 —— 那边是别家
    #: 怎么说它, 这边是它自己怎么说.
    official_reports: list[FeedOtherReportOut] = []


class FeedTaxonomyOut(BaseModel):
    """筛选栏自己的词表: 类别、主题标签、实体名录.

    页面拿它画筛选栏, 所以类别一个也不认识也画得出来 —— 加一个类别只改后端.
    """

    categories: list[FeedCategoryOut] = []
    topics: list[str] = []
    entities: list[str] = []
    #: 印在数字旁边的那几个词, 例如 "AI 评分" / "独立来源" / "热度指数". 页面不自己写它们:
    #: 一个词是对一条规则的解释, 解释改了词要跟着改, 而那条规则在后端.
    labels: dict[str, str] = {}


class FeedOverrideRowOut(BaseModel):
    """一条人工归属决定 (4.8b). 自动分组读到就跳过这条, 只有显式「重新分组」才清。"""

    source_id: str
    external_id: str
    reason: str = ""
    actor: str = ""
    created_at: str


class FeedOverridesOut(BaseModel):
    rows: list[FeedOverrideRowOut] = []


class FeedOverrideIn(BaseModel):
    """人写下的一条决定: 这一条不许自动归组。actor 与 reason 是追查的两个键。"""

    source_id: str
    external_id: str
    reason: str = ""
    actor: str = ""


class FeedJobOut(BaseModel):
    name: str
    cadence_minutes: int
    enabled: bool = True
    last_slot: str | None = None
    last_run_at: str | None = None
    last_status: str | None = None
    last_error: str | None = None


class FeedJobRunOut(BaseModel):
    id: int
    name: str
    slot: str
    started_at: str
    finished_at: str | None = None
    status: str
    detail: dict = {}
    error: str | None = None


class FeedFetchRunOut(BaseModel):
    id: int
    source_id: str
    started_at: str
    finished_at: str | None = None
    status: str
    found: int = 0
    created: int = 0
    error: str | None = None


class FeedCoverageOut(BaseModel):
    """写作站的覆盖率 (2.3b): 每一档各有多少条。

    档名就是 `llm_stage` (没问过的是 `none`); 不合并成一个百分比 —— 卡在哪一步看得见。
    """

    total: int = 0
    stages: dict[str, int] = {}


class FeedEvalAxisOut(BaseModel):
    """金标评测里一个轴的成绩 (5.22): 这一类判断对了几条。"""

    axis: str
    passed: int = 0
    total: int = 0


class FeedEvalMissOut(BaseModel):
    """一条没对上的样本: 人标的是什么, 机器答的是什么, 以及为什么这样标。"""

    id: str = ""
    axis: str = ""
    want: Any = None
    got: Any = None
    why: str = ""


class FeedEvalOut(BaseModel):
    """金标评测 (5.22): 人工标好的样本与现在的规则对一遍的结果。"""

    version: int = 0
    total: int = 0
    passed: int = 0
    #: 合格率, 0-1. 单独回一个数是为了让它能被一条曲线跟上去。
    agreement: float = 0.0
    axes: list[FeedEvalAxisOut] = []
    misses: list[FeedEvalMissOut] = []


class FeedAdminSessionOut(BaseModel):
    """管理台的开关 (5.12): 设不设防, 以及这一个请求算不算登录。

    两个都要回答 —— 只回后一个的话, 页面在没配口令时看不出自己是不是正在裸奔。
    """

    guarded: bool = False
    signed_in: bool = False


class FeedAdminLoginIn(BaseModel):
    password: str = ""


class FeedAdminSettingOut(BaseModel):
    """一个本地键在页面上的样子: 名字、配没配、以及一个掩码。

    `masked` 而不是明文: 这几个键都是密钥, 而一个能在页面上读出来的密钥
    就不再是密钥。页面要的只是「这一格空不空」。
    """

    key: str
    label: str = ""
    configured: bool = False
    masked: str = ""


class FeedAdminSettingsOut(BaseModel):
    settings: list[FeedAdminSettingOut] = []


class FeedAdminSettingsIn(BaseModel):
    #: 键 -> 值。空串 = 清掉这一个键。
    values: dict[str, str] = {}


class FeedOpsOut(BaseModel):
    """运维读口 (5.11): 定时活儿的健康、最近几趟活儿与抓取、最近的审计。"""

    jobs: list[FeedJobOut] = []
    job_runs: list[FeedJobRunOut] = []
    fetch_runs: list[FeedFetchRunOut] = []
    audit: list[dict] = []
    #: 写作站覆盖率。
    coverage: FeedCoverageOut = FeedCoverageOut()
    #: 补正文队列的现状 (1.2): 每一档几条、到点了还没跑的几条。
    queue: dict = {}
    #: 最近几条投递 (5.8): 发到哪、发过几次、成没成。
    deliveries: list[dict] = []


class FeedLedgerRowOut(BaseModel):
    """精选台账上的一笔 (5.6): 这一条什么时候进的首页 / 什么时候下来的."""

    id: int
    item_id: str
    op: str
    changed_at: str
    visible_at: str


class FeedLedgerOut(BaseModel):
    rows: list[FeedLedgerRowOut] = []
