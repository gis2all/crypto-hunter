"""谁在说: 一个 source_id 对应哪个名字、哪一档、是不是当事方自己。

这张表以前是文件里的常量, 现在是 `feed_sources` 的那些行 —— 采集清单与名录是同一件事的两面:
一个源的名字、分级、一手标记, 只在它被登记的地方写一次。`enabled: false` 的源照样在这里
答得出来 (见 `config/feed/sources.yaml` 末尾那几个): 采集跳过它们, 但一条昨天存下的记录
今天仍然要印出是谁说的。

分三档 (照 AIHOT 的分法):

  T1    官方一手: 官网、官方博客、监管机构公告。当事方自己发的, 就是原始记录。
  T1_5  官方账号与准官方创作者: 官方 X 账号这类, 与 T1 同一张嘴, 但转述渠道不如官网稳。
  T2    媒体与个人: 报道、转述、监测账号。独立来源的计数里它们同样算一家。

`first_party` 与 tier 是两件事: 交易所的上币公告是 T1 一手, 而媒体报道同一条是 T2
非一手 —— 判"这条是不是当事方自己在说"读的是它, 不是分级。

查不到就回一个显式占位 (名字写出来、分级按最低、非一手), 而不是猜一个默认值: 一个没人登记
的 id 是数据错误, 而一个看起来像真的默认值会让它一直错下去 (CLAUDE.md 2.3.6)。

表在进程里缓存一份, 因为它在一次渲染里会被问几百次 (每一条记录、每一处来源名), 而它一轮
才变一次。导入或改完之后叫 `forget()`。
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass

from ...store import db


@dataclass(frozen=True)
class Source:
    """One party the feed listens to."""

    id: str
    #: 页面上的写法, 不是 id: 读者看的是 "The Block", 不是 "rss:theblock"。
    name: str
    kind: str            # blog | exchange | regulator | rss | x | json_list | web_list
    tier: str            # T1 | T1_5 | T2
    first_party: bool = False
    #: 源的原文语言 (zh | en | 空). 空 = 清单没说, 由正文里的汉字判 (writing.zh_source):
    #: 这一格唯一的用处是"要不要译正文", 而译不译得对与源自己在说哪种话有关, 与读者无关.
    language: str = ""
    #: 这个源是"报道"还是"讨论证据" (AIHOT 的 `participation_mode`). `hot_signal` 的源不立事件,
    #: 它只往别人立好的那件事上挂一条证据 (热度按"几家在说"数它一次); 阅读侧也不印它 ——
    #: 一条推文被当成一件事的正文印出来, 读者看到的就是"这条新闻只有一句话".
    participation: str = "editorial"
    #: 内容许可与可见性 (AIHOT 的 SourceFacts): 三格都由清单写, 不是运行态。
    #: `visibility` 空 = public | withdrawn; 下架的源照样在名录里 ——
    #: 一条昨天存下的记录今天仍然要印出是谁说的。
    visibility: str = "public"
    withdrawn_at: str = ""
    #: 允许在站内印全文（付费墙/图书馆源不允许）。
    site_fulltext: bool = False
    #: 允许把全文再分发给 RSS / API（比前一格严）。
    syndicate_fulltext: bool = False
    #: 它在页面上露的那张脸 (AIHOT 的 `icon_url`)。空 = 还没有, 页面画首字母。
    icon: str = ""


#: 名录里没有的 id 不该出现 —— 出现了说明采集层写了个没人登记的名字, 那是数据错误而不是
#: 一个可以猜的默认值。所以这里给一个显式的占位: 名字写出来, 分级按最低算, 非一手。
_UNKNOWN = Source("", "未知来源", "rss", "T2")


class _Roster(Mapping):
    """`feed_sources` 那张表, 当成一个只读的映射。

    做成映射而不是一个函数, 是为了让 `in` 与 `[...]` 两种问法都成立 —— 而两者都在用。
    """

    def _rows(self) -> dict[str, Source]:
        global _cache
        if _cache is None:
            _cache = {
                row["id"]: Source(row["id"], row["name"], row["kind"], row["tier"],
                                  bool(row["first_party"]), row.get("language") or "",
                                  row.get("participation_mode") or "editorial",
                                  row.get("visibility") or "public",
                                  row.get("withdrawn_at") or "",
                                  bool(row.get("site_fulltext")),
                                  bool(row.get("syndicate_fulltext")),
                                  row.get("icon_url") or "")
                for row in db.feed_sources_all()
            }
        return _cache

    def __getitem__(self, key: str) -> Source:
        return self._rows()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._rows())

    def __len__(self) -> int:
        return len(self._rows())

    def __contains__(self, key: object) -> bool:
        return key in self._rows()


_cache: dict[str, Source] | None = None

#: 谁在说。`of()` 是它唯一的读法, `SOURCES` 只是让"在不在名录里"这个问题问得出来。
SOURCES: Mapping[str, Source] = _Roster()


def forget() -> None:
    """名录改了。下一次问的时候重新读一遍表。"""
    global _cache
    _cache = None


def of(source_id: str) -> Source:
    """The source, or a placeholder that says so rather than a guess (CLAUDE.md 2.3.6)."""
    known = SOURCES.get(source_id)
    if known is not None:
        return known
    return Source(source_id, source_id or _UNKNOWN.name, "rss", "T2")

