"""The shapes the feed passes around. Plain data: no IO, no clock, no database.

Four shapes, one direction:

    Discovery  一条来源说了什么, 原样                  采集层交回来的东西
    Item       Discovery + 读出来的东西                页面上的一个条目 (全部动态)
    Event      若干 Item 说的是同一件事                页面上的一个故事 (热点榜)
    Edition    一期刊物                                一张报纸 (日报 / 周报 / 月报)

A `Discovery` carries no category, no score and no summary on purpose: those are *read out of*
the text by the analysis station, once, for every source there will ever be. A fixture that
arrived pre-analysed would let the pipeline skip the station that decides everything downstream
(DESIGN.md 5.2).
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: 分析档位的两档写法 (4.9b)。页面印 `scope_label`, 判定用 key。
SCOPE_LABELS: dict[str, str] = {"primary": "单一事实", "composite": "综合"}


@dataclass(frozen=True)
class Discovery:
    """One source, one thing said, before anything has been decided about it."""

    source_kind: str        # rss | x | exchange | regulator
    source_id: str          # the independent party, so heat can count it once
    external_id: str        # the source's own id for it; a re-read is not a second record
    title: str
    url: str
    published_at: str       # ISO 8601, the source's own timestamp
    body: str = ""
    author: str = ""
    #: 源自己在清单里给的摘要 (纯文字, 最多 `materials.EXCERPT_CHARS`).
    #: 它与正文分开存: 一条记录可以只有摘要没有正文 (列表页只给一句话),
    #: 而那句话正是后续判定"这条值不值得去读原文"的依据。
    excerpt: str = ""
    #: 这条自带的图, 原样的 url. 只有 X 这类来源会带, 而且是**源自己的**图 —— 我们不下载
    #: 也不重绘, 页面拿它当缩略图画. 空的那些就是没有图, 不是"图还没取到".
    media: list[str] = field(default_factory=list)
    #: 这条引用的那条推 (`{"text", "handle", "url"}`); 空字典就是没引用. 它只喂给理解档当背景
    #: —— 摘要要把引用里承载上下文的关键点整合进去 —— 不进正文, 也不单独译 (writing.py).
    quoted: dict = field(default_factory=dict)
    #: 我们**第一次**读到它的时刻 (AIHOT 的 `discovered_at`). 只由 `snapshot.row_to_discovery`
    #: 从库里读回来 —— 抓取器交出的那一条还不知道自己是第几次被看见. 空 = 不知道.
    #: 时间线判定要它: 一条源自称三天前的记录, 是不是"刚被翻出来的旧闻"由它说了算
    #: (materials.is_historical).
    seen_at: str = ""


@dataclass
class Item:
    """A discovery plus what the analysis station read out of it.

    This is the shape both list views draw, and it is deliberately the *item* and not the story:
    a story is a cluster and a cluster has no single source, no single timestamp and no single
    category - everything a reader scans in a list belongs to one party saying one thing.

    `score` is 0-100 and it is the analysis station's answer, not a reader's: it decides 精选
    (which is why it is on the item) and it never decides 热度 (which is why it is not on the
    story - see heat.py on what may be bought and what may not).
    """

    discovery: Discovery
    category: str = ""
    category_label: str = ""
    tags: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    #: 当事方自己在说 (官网、官方博客、交易所公告、监管机构). Read off the source registry,
    #: never off the text - "we are excited to announce" is not evidence of anything.
    first_party: bool = False
    score: int = 0

    #: 这条在不在精选线上。规则版按分数算 (analysis.read); 模型评过分之后按信源分级的门槛算
    #: (writing.apply_item) —— 两个数不是一回事, 所以它单列一格。
    selected: bool = False
    #: 推荐理由: 一句"为什么这个行业的观察者要看它". 规则版按类别给, 模型接手后逐条写.
    reason: str = ""
    #: 摘要: 模型写的那一句"发生了什么". 还没被问过的条目退回正文的第一段 (analysis.article).
    summary: str = ""
    #: 正文的**原文**: 净化过的块级 HTML —— 块是源自己的段落与 `<h2>`, 行内标签压成了文字
    #: (htmlbody.py). 页面印的正文由它长出来, 译文那一份换的是块里的字, 结构不动.
    body_html: str = ""
    #: 正文的中文译文, 同一套块. 空 = 还没有人译过它; 中文源的条目永远是空的 —— 没有可译的
    #: 东西就不该有"中文/原文"那个开关 (writing.zh_source 那条判断).
    body_zh: str = ""
    #: 正文档位 (AIHOT 的 `bodyModeOf`): `full` = 站内可以印全文, `summary` = 只印摘要.
    #: “能抓到”不等于“可以印” —— 一个不许转载的源, 正文存在库里当页也只给摘要.
    #: 由 `analysis.read` 按源清单与这一版有没有读到正文算出来 (rules.body_mode_of)。
    body_mode: str = "summary"
    #: 译文是不是**完整**: 有一块没译上就是 false, 页面据此在正文上加一行提示. 少了这一格,
    #: "译了一半"与"整篇译完"在页面上长得一模一样.
    body_complete: bool = False
    #: 模型写的中文标题 (入库时那一站的产物). 空 = 这一条还没有人用中文写过它, 页面就印源自己
    #: 的标题 —— 所以"标题是中文的"这件事在页面上永远成立, 而库里两样都在.
    title_zh: str = ""
    #: 这件事的骨架: `{"subject","action","object"}`, 模型在理解档抽的 (AIHOT 的 fact frame)。
    #: 没人问过就是空字典 —— 骨架是分组判定的输入, 规则版算不出它, 也不该假装算得出。
    fact: dict = field(default_factory=dict)
    #: 这条在说谁: 实体词典的 id, 过了白名单那一道筛。空表 = 这条材料里没有认识的实体, 也是答案。
    subjects: list[str] = field(default_factory=list)
    #: 这条的分析是哪一种 (AIHOT 的 `analyses.output.scope`, 4.9b)。空 = 还没判过;
    #: `primary` = 只谈一件事; `composite` = 把几件事装在一起 (盘点 / 日报)。复合的那一条
    #: **只提及**那些事实, 不是任何一件的证据 —— 所以它不立事件, 也不进热点榜。
    analysis_scope: str = ""

    @property
    def scope_label(self) -> str:
        """这一格在页面上印什么字。空 = 不印 (没有这一格的东西不该多一行灰字)。"""
        return SCOPE_LABELS.get(self.analysis_scope, "")

    @property
    def display_title(self) -> str:
        """页面印的标题: 模型写过就用它写的那条, 没有就是源自己的.

        只此一处判"印哪一个": 卡片、事件时间线、热点榜上「谁在说」那一列全都问它, 所以三处
        印出来的标题不可能不一样.
        """
        return self.title_zh or self.discovery.title.strip()


@dataclass
class Event:
    """Several items about the same thing, and what the industry says about it."""

    identity_key: str
    kind: str
    #: The kind as the page prints it. Travels with the event rather than being looked up
    #: per render, so an event nobody read still has a name to be listed under.
    kind_label: str
    title: str
    url: str
    first_seen_at: str
    last_seen_at: str
    #: Every item that landed here, kept whole rather than counted: a page has to be able to
    #: point back at what a story was written from, and "how many sources" cannot do that.
    items: list[Item] = field(default_factory=list)
    #: Distinct `source_id`s. Not the same as `len(items)`: one wire service reprinted nine
    #: times is one party speaking, and a list of nine that reads as a crowd is the failure
    #: mode this number exists to prevent.
    sources: int = 0
    heat: float = 0.0
    #: 热度指数: `heat` 的 10 倍值, 一位小数 (AIHOT 的 `heatIndex`). 页面印的是它, 排序用的是
    #: `heat` —— 同一个数, 两个单位.
    heat_index: float = 0.0
    #: 最近 24 小时的热度走势, 每小时一个点, 24 个点. 画成一条线, 不印数字.
    series: list[float] = field(default_factory=list)
    #: 五态走势 (AIHOT 的 `trend`): `new` / `unknown` / `up` / `down` / `flat`.
    #: `new` = 6 小时前这件事还不存在; `unknown` = 那时有它但算不出涨幅.
    trend: str = ""
    #: `trend` 那一眼的具体涨幅, 百分比一位小数. None = 6 小时前这件事还不存在.
    trend_pct: float | None = None
    #: 窗口里的参与者里, 几家是发布源、几家是讨论源 (AIHOT 的 `editorial_participants` /
    #: `signal_participants`). 页面印的"来源数"是前者: 讨论帖不是"谁在报这件事".
    participants_editorial: int = 0
    participants_signal: int = 0
    #: 标题上那几枚小标签 (AIHOT 的 hot badges): `surge` / `new` / `rising`, 空表 = 不给.
    #: 由 `heat.badges` 按三条**各自成立**的规则判, 页面只翻牌子不重新判断 —— 刚发生又被三家
    #: 同时跟进的事会同时带着 `surge` 和 `new`.
    badges: list[str] = field(default_factory=list)
    #: 这件事现在算哪一档 (digest.status_for): `active` / `watching` / `settled`. 空 = 综述站
    #: 还没写过它, 页面退回按"最近有没有新报道"那条旧规则印.
    status: str = ""
    #: 分类是成员里最常见的那一类, 分数是成员里最高的那个. 都不是"平均": 一个事件的
    #: 分量由它最硬的那条决定, 不由它最软的那条决定.
    category: str = ""
    category_label: str = ""
    tags: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    score: int = 0
    selected: bool = False
    #: 最新进展: 最新一条的标题. 一个故事最重要的是"刚刚又发生了什么".
    latest: str = ""
    #: AI 综述: 分段. 规则版列出前几家的说法, 模型接手后重写成一整段.
    summary: list[str] = field(default_factory=list)
    #: 过去 6 小时里**第一次出现**的独立来源数, 以及这段时间里新增的条目数. 与涨跌同一个窗口
    #: (`heat.TREND_BACK_HOURS`) —— "为什么热"那一栏印的是这两个数, 不是全天总数.
    recent_sources: int = 0
    recent_reports: int = 0
    #: The reading pass's output for this event. `None` = nobody has read it yet, which is not
    #: the same as "nothing to say about it" - and it is why this is not an empty dict.
    judgement: dict | None = None

    @property
    def discoveries(self) -> list[Discovery]:
        """The raw records behind this event, for the stations that work on raw text."""
        return [item.discovery for item in self.items]


@dataclass
class Edition:
    """One issue: what the window held, ordered and ready to print.

    A `kind` and a `key` rather than a timestamp: an issue is "the third of October", not "the
    run at 14:48:52". The key is what a reader names an issue by, and it is what makes a re-run
    the same issue rather than a second one (`service.edition_id`) - the same day run twice
    has one key, so it has one id, so it has one row.

    `headline` is what the archive list draws a row from, and it is computed once here rather
    than on the page: the page would have to open a body to find it, and the list is meant to
    draw without one. 期号 不在这里 —— 它是对同一份档案里比自己早的那些数出来的，不是一期
    自带的性质 (store/db.py `_ISSUE_NUMBER`).
    """

    kind: str                 # day | week | month
    key: str                  # 2026-10-03 | 2026-W40 | 2026-10
    window_from: str
    window_to: str
    entries: list[Event] = field(default_factory=list)
    #: How many discoveries the window held. Kept beside the entries so an edition with
    #: nothing in it can say "we read 400 things and none of them mattered" instead of
    #: looking like a run that failed.
    seen: int = 0
    #: 一期一个标题: 代表条目 (排第一的那条) 的标题. 档案栏那一列画的就是它.
    headline: str = ""
    #: 导语 (5.7): 模型写的一段话, 讲整期在说什么. 没配模型时是空 ——
    #: 页面就只印报头与条目, 不留一个空框子.
    lead: str = ""
    #: 报头那几个数: `[{key, label, value}]`, 次序即版面次序. 词和数一起从后端来 —— 上一版
    #: 页面自己编了「读了 N 条」, 就是这里少了一半.
    metrics: list[dict] = field(default_factory=list)
