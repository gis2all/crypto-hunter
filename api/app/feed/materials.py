"""The feed's single entrance: what is the same thing, and does it count as today.

Every discovery any source will ever produce walks through here before anything else looks at
it, because the two questions answered here are the two that everything downstream stands on.
Read `identity_key` wrong and one event becomes nine; read the window wrong and last week's
news goes out as today's.

Both answers are read out of the text, never taken from the source. Two outlets describe one
listing in two wordings, and a key made out of the wording would make them two events - so the
key is built from what the text is *about*: the kind, and the parties it names. The wording is
the fallback, and only the fallback.

Nothing here is a judgement about importance. "Is this the same thing" and "is this today" are
facts about the record, and a pass that cannot tell them apart cannot be corrected later.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Sequence

from .models import Discovery

#: What a headline can be about. Checked in order, first hit wins, so the narrower kinds sit
#: above the wider ones: a bridge that is hacked and then relaunched is a hack, not a launch.
#: Vocabulary only - a real reading pass replaces this table, and the key's shape does not
#: change when it does.
KINDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("hack", ("exploit", "hacked", "drained", "breach", "被盗", "漏洞", "攻击")),
    ("regulator", ("sec ", "cftc", "lawsuit", "charged", "监管", "起诉", "处罚", "罚款")),
    ("launch", ("mainnet", "testnet", "launches", "上线主网", "主网上线", "发币", "空投")),
    ("funding", ("raises", "raise ", "funding round", "series a", "融资", "估值")),
    ("listing", ("lists ", "listing", "will list", "delist", "上线", "上所", "现货")),
    ("partnership", ("partners", "partnership", "integrates", "合作", "集成")),
)

#: What each kind is called on the page. Beside `KINDS` on purpose: the two are one table
#: seen from two sides, and a kind that exists without a label is a row the page cannot name.
#: The words live here rather than in the frontend because the frontend draws labels it is
#: handed (CLAUDE.md 2.3.3) - renaming one is a change in the file that knows what it means.
KIND_LABELS = {
    "hack": "被盗 / 漏洞",
    "regulator": "监管",
    "listing": "上币",
    "funding": "融资",
    "launch": "主网 / 发行",
    "partnership": "合作",
    "": "动态",
}

#: A ticker written the way the industry writes them. The dollar sign is what makes this safe
#: to run over raw text: `$SOL` is a party, `100` and `SOL` on their own are not, and a rule
#: that guessed at bare capitals would turn the word "Bridge" into a party.
_TICKER = re.compile(r"\$([A-Za-z][A-Za-z0-9]{1,9})\b")

_WS = re.compile(r"\s+")

#: 数命中用的词: 只服务"这个名字在窗口里出现几次", 认词本身仍走 `mentions` 的边界正则。
_WORD = re.compile(r"[a-z0-9]+")

#: 一个**学来的**名字命中超过窗口这个比例时, 它是词不是名字 (见 `learn_aliases`)。
LEARNED_ALIAS_SHARE = 0.05

#: 上限的保底条数: 小窗口里比例没有意义 —— 61 条的一期里, 一条真正的四家报道占 6.5%, 按比例算
#: 会被当成词扔掉, 而那正是这一期最该聚起来的东西。窗口小的时候只有"命中条数"能说明问题。
LEARNED_ALIAS_FLOOR = 10


def normalize(text: str) -> str:
    """Case and spacing folded away, punctuation left alone.

    Enough to make two wordings of one headline comparable and no more: a stemmer here would be
    a third opinion about what the words are, taken before anybody has read them.
    """
    return _WS.sub(" ", text.strip().lower())


def mentions(low_text: str, name: str) -> bool:
    """Is this name in the text as a word of its own?

    Whole words only, and escaped: a party called `Nova` must not be found inside `Novation`,
    and an operator who writes a name with a dot or a bracket in it must not be able to write a
    regex by accident (they cannot write one on purpose either - the config is not code).

    公开的: 实体词典 (entities.py) 按同一条规矩认词, 两处各写一遍迟早有一处松掉。
    """
    return re.search(r"(?<![a-z0-9])%s(?![a-z0-9])" % re.escape(name), low_text) is not None


def kind_of(text: str) -> str:
    """What kind of thing happened, or "" when nothing in the vocabulary fits.

    An empty answer is an answer: it says the table does not know this one, and the key falls
    back to the wording knowing that it did.
    """
    low = text.lower()
    for kind, words in KINDS:
        if any(word in low for word in words):
            return kind
    return ""


def parties_of(text: str, names: Sequence[str] = ()) -> tuple[str, ...]:
    """Who the text names, lower-cased and sorted. The half of the key that says about what.

    `names` is what to look for by name - the operator's own list plus whatever this window
    taught (`learn_aliases`). Tickers are always read, in any text, whatever `names` holds.
    """
    low = text.lower()
    by_name = {name.lower() for name in names if name and mentions(low, name.lower())}
    if by_name:
        # One party, two spellings: an outlet that writes `$ZEPH` beside "Zephyr" means one of
        # them, not two. The longer spelling is the name and the shorter is its ticker, so the
        # shorter one is dropped from the key - otherwise a record that uses both would refuse
        # to merge with a record that uses only the name, which is every other outlet.
        return tuple(sorted(n for n in by_name if not any(n != o and n in o for o in by_name)))
    # Nothing known by name. A ticker on its own is still the best answer there is, and the
    # window's own `learn_aliases` will have taught the name for whatever comes after it.
    return tuple(sorted({match.group(1).lower() for match in _TICKER.finditer(text)}))


def learn_aliases(items: Iterable[Discovery], names: Sequence[str] = ()) -> tuple[str, ...]:
    """Names to look for by name, learned from this window's own tickers.

    An outlet writes `$NOVA` once and `Nova` in every sentence after, and a key that understood
    only the dollar form would file that one event as two. So the window reads itself first:
    every ticker written anywhere in it becomes a name to look for, in the rest of it.

    Cheap, needs no dictionary, and it is the reason the fixture can be written the way the
    industry actually writes. What it cannot see is a party nobody ever spelled with a dollar
    sign - which is why the operator's own list is passed in beside it, and why entity
    extraction is the first thing on DESIGN.md 8's list of what to replace.
    """
    items = list(items)
    known = {name.lower() for name in names if name}
    found = set(known)
    for item in items:
        text = "%s %s" % (item.title, item.body)
        found.update(match.group(1).lower() for match in _TICKER.finditer(text))

    # 一个学来的名字在窗口里命中太多条时, 它多半是个词: 有人写过一次 `$AI`, 于是"AI"成了名字,
    # 而标题里顺手提一句 AI 的新闻就全粘进同一个桶 (实测 48 小时窗口 587 条里命中 133 条; `$NEAR`
    # / `$HYPE` 这种英文常用词同理)。手写的名单 (`names`) 不受这条限制 —— 那是人挑过的词。
    # 数命中用词集合而不是逐名跑正则: 页面每个请求都要走这一步。
    learned = found - known
    if learned:
        cap = max(LEARNED_ALIAS_FLOOR, int(len(items) * LEARNED_ALIAS_SHARE))
        hits = {name: 0 for name in learned}
        for item in items:
            words = set(_WORD.findall(("%s %s" % (item.title, item.body)).lower()))
            for name in learned & words:
                hits[name] += 1
        found -= {name for name in learned if hits[name] > cap}
    return tuple(sorted(found))


def identity_key(discovery: Discovery, names: Sequence[str] = ()) -> str:
    """What this discovery is about, in one readable string.

    Readable on purpose: this string decides which discoveries become one event, and when two
    things merge that should not have, the first question is always "what did they look like" -
    which a hash cannot answer.

    A discovery that names nobody and matches no kind falls back to its own wording, so two
    reprints of one untagged story still become one event. That fallback is the only path that
    reads the title, and it is why such a record is worth noticing rather than hiding.
    """
    text = "%s %s" % (discovery.title, discovery.body)
    parties = parties_of(text, names)
    if not parties:
        return "text|" + normalize(discovery.title)[:120]
    return "%s|%s" % (kind_of(text) or "other", ",".join(parties))


def moment(iso: str) -> datetime:
    """A source's own timestamp as an aware datetime.

    Sources write ISO 8601 in several dialects. `fromisoformat` reads most of them; the one it
    does not is the `Z` suffix, which is not a dialect but a timezone. A source that wrote no
    zone at all is read as UTC rather than as this machine's time - the machine is in one place
    and the industry is not.
    """
    parsed = datetime.fromisoformat(iso.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


#: 发现时间比源自己说的晚这么多: 这条是**历史**, 不是新闻 (AIHOT 的 STALE_ON_DISCOVERY_MS).
#: 一个源第一次被读时, 它的历史存档整批进来 —— 那些是资料, 不是今天发生的事.
STALE_ON_DISCOVERY_HOURS = 48.0

#: 源说的时间比我们发现它还晚这么多: 那个时钟不可信 (AIHOT 的 FUTURE_TOLERANCE_MS). 一小时是
#: 给正常时区和发布延迟留的余量, 再往前就不是钟错了几分钟, 是那条记录的时间没意义.
FUTURE_TOLERANCE_HOURS = 1.0


@dataclass(frozen=True)
class Timeline:
    """一条记录的时间怎么读.

    `published_at` 是**可以信的**源时间 —— 未来得离谱的换成发现时刻; `historical` 说明它是被
    翻出来的旧闻. 两个判断共用一条入口规则 (AIHOT 的 `decideTimeline`): 分开写迟早会有一处
    把"未来"和"很旧"对同一条件出两种答案.
    """

    published_at: str
    historical: bool


def decide_timeline(published_at: str, discovered_at: datetime) -> Timeline:
    """源说的时间该不该信, 以及它算不算历史.

    两种读法, 一条规则: 时间太靠后(超过 `FUTURE_TOLERANCE_HOURS`)就当没说, 用我们第一次见到
    它的时刻代替; 时间太靠前(超过 `STALE_ON_DISCOVERY_HOURS`)就照实留着, 但标成历史. 前者救的
    是窗口 (一条时标跑到三天后的记录会在窗口判定里挤掉今天的事), 后者救的是事件图 (旧闻不该
    以"今天第一次出现"的身份和今天的新闻抢位置).
    """
    claimed: datetime | None = None
    if published_at:
        try:
            claimed = moment(published_at)
        except ValueError:
            claimed = None
    if claimed is not None and claimed > discovered_at + timedelta(hours=FUTURE_TOLERANCE_HOURS):
        claimed = None
    if claimed is None:
        return Timeline(discovered_at.isoformat(timespec="seconds"), False)
    late = (discovered_at - claimed).total_seconds() / 3600.0
    return Timeline(published_at, late > STALE_ON_DISCOVERY_HOURS)


def is_historical(discovery: Discovery) -> bool:
    """这条是不是被翻出来的旧闻.

    `seen_at` 是**第一次**读到它的时刻 (db.feed_items_put), 没记过就不能判 —— 不知道什么时候
    发现的, 就不知道它算不算历史, 那就不当历史 (宁可把它当今天的, 也不让它凭空消失).
    """
    if not discovery.seen_at:
        return False
    try:
        found = moment(discovery.seen_at)
    except ValueError:
        return False
    return decide_timeline(discovery.published_at, found).historical


def in_window(discovery: Discovery, start: datetime, end: datetime) -> bool:
    """Does this count as today?

    Half-open: `start` belongs to this window and `end` to the next one, so a discovery landing
    exactly on the boundary is published once rather than twice or never.
    """
    return start <= moment(discovery.published_at) < end


def dedupe(items: Iterable[Discovery]) -> list[Discovery]:
    """One record per (source, that source's own id). A re-read is not a second record.

    Order is kept: the earliest reading of a thing is the one an edition quotes.
    """
    seen: set[tuple[str, str]] = set()
    kept: list[Discovery] = []
    for item in items:
        key = (item.source_id, item.external_id)
        if key in seen:
            continue
        seen.add(key)
        kept.append(item)
    return kept


def representative(items: Sequence[Discovery]) -> Discovery:
    """The one to quote: whoever said it first.

    Ties go to the order they arrived in, which for two sources agreeing to the second is the
    order the sources are read - a stable answer, not an important one.
    """
    return min(items, key=lambda item: moment(item.published_at))


#: 源自己在清单里给的摘要最多存多少字 (AIHOT `feedText`: 2000)。
EXCERPT_CHARS = 2000
#: RSS 正文短于这个数就不当正文用 (AIHOT `feedText`: 280)。
#: 一句话的摘要不是正文, 而一条" 正文" 只有一句话时,
#: 摘要档写出来的东西就是那句话的同义句。
MIN_BODY_CHARS = 280

_COLLAPSE_RE = re.compile(r"\s+")
_LOST_RE = re.compile("\ufffd+")
#: 传输中丢掉的字符 (AIHOT 的 `LOST`)。
_LOST = "\ufffd"


def collapse(text: str | None) -> str:
    """白空归一 (AIHOT `collapseWhitespace`): 换行与缩进是渲染的差别, 不是内容的差别。"""
    return _COLLAPSE_RE.sub(" ", text or "").strip()


def content_hash(title: str, body_text: str | None = None, excerpt: str | None = None) -> str:
    """内容的身份 (AIHOT `contentHash`): 这一串变了, revision 就 +1。

    三个字段各归一空白后拿 U+0001 拼起来再 sha256。分隔符是必须的:
    `("a","b")` 与 `("ab","")` 不是同一份内容, 而直接拼接分不出这个差别。
    """
    joined = "\u0001".join((collapse(title), collapse(body_text or ""), collapse(excerpt or "")))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _lossy(text: str | None) -> list[str]:
    return list(_LOST_RE.sub(_LOST, collapse(text or "")))


def same_barring_loss(a: str | None, b: str | None) -> bool:
    """两份渲染只差在传输中丢掉的字符上时算同一份 (AIHOT `sameBarringLoss`)。

    有些 feed 每次读都会随机糊掉几个字: 同一篇文章两次读到的文本就不一样。
    一个 U+FFFD (或连续一串) 代表"这里本来有一个字", 两边逐位对得上就算同
    一份内容 —— 不然这种 feed 每读一次都是一版新正文, revision 会一直涨。
    """
    left = _lossy(a)
    right = _lossy(b)
    if len(left) != len(right):
        return False
    return all(x == y or x == _LOST or y == _LOST for x, y in zip(left, right))
