"""身份守卫: 标题与摘要里出现的公司/机构名, 必须在原文里也出现过。

(AIHOT 的 `industry/taxonomy` 身份词典 + `editorial/writing.ts` 的 `enforceIdentity`。)

**为什么要有.** 模型只负责措辞, 不负责引入事实。一条讲 A 交易所的公告, 模型顺手写成 B 交易所
—— 数字、时间、动作都对得上, 只有主体错了, 而没人会专门去核这一处, 印出去就是张冠李戴。所以:

1. 标题里点了原文没说过的公司: 退回原标题 (原标题是中文) 或者留空;
2. 摘要里点了: 整条摘要丢掉, 页面印规则版。

**白名单只认材料里真出现过的.** 原文标题 / 正文 / 主推文 / 引用推文 / 来源名各算一份, 再加两样
"就算正文没提也一定算数"的: 文档发布域 (一家公司在自家域名上发的公告, 发布方就是它) 与来源账号
主体。同一个名字的变体也算 (AIHOT 的 `surfaceVariants`: "USDC 5" 与 "USDC-5" 是同一个)。

**词典是行业相关的.** AIHOT 那份列的是模型厂商, 我们这份列的是交易所 / 公链 / 发行方 / 监管
机构。这个行业没有这种错时, `IDENTITY_LEXICON` 留空即可 —— 但加密货币新闻里它很常见 (把某家
交易所的公告写成另一家、把 USDC 的事写成 USDT 的)。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

_HAN = re.compile(r"[\u4e00-\u9fff]")
_KANA = re.compile(r"[\u3040-\u30ff]")
_HANGUL = re.compile(r"[\uac00-\ud7af]")


def looks_zh(text: str) -> bool:
    """这一串是不是中文 (AIHOT 的 `looksZh`): 有汉字, 且不是日文假名或韩文。"""
    if not _HAN.search(text or ""):
        return False
    return not (_KANA.search(text) or _HANGUL.search(text))


@dataclass(frozen=True)
class Entity:
    """一条身份词条: 内部 id、印给人看的名字、认出它的正则 (任意一条命中就算提到)."""

    id: str
    name: str
    patterns: tuple[re.Pattern[str], ...]


def _entity(entity_id: str, name: str, *patterns: str) -> Entity:
    return Entity(entity_id, name, tuple(re.compile(p, re.I) for p in patterns))


@dataclass(frozen=True)
class Publisher:
    """一个域名归谁: 这个域上的公告, 发布方就是这家 (托管平台不算, AIHOT 的 `PUBLISHER_DOMAINS`)."""

    entity_id: str
    domains: tuple[str, ...]


#: 已知主体。只列"被写错会造成张冠李戴"的名字: 交易所、公链、发行方、监管机构。宁可少列 ——
#: 少列一个只是少抓到一次错, 多列一个会把写对了的标题也退回去 (比如把 `BNB` 这种币名当主体)。
IDENTITY_LEXICON: tuple[Entity, ...] = (
    _entity("binance", "Binance", r"binance", r"\u5e01\u5b89"),
    _entity("coinbase", "Coinbase", r"coinbase"),
    _entity("okx", "OKX", r"\bokx\b", r"\u6b27\u6613"),
    _entity("bybit", "Bybit", r"bybit"),
    _entity("kraken", "Kraken", r"\bkraken\b"),
    _entity("robinhood", "Robinhood", r"robinhood"),
    _entity("gemini", "Gemini", r"gemini\s+(?:exchange|trust|wallet)", r"winklevoss"),
    _entity("upbit", "Upbit", r"upbit"),
    _entity("circle", "Circle", r"\bcircle\b", r"usdc"),
    _entity("tether", "Tether", r"tether", r"usdt"),
    _entity("ripple", "Ripple", r"ripple", r"\bxrp\b"),
    _entity("ethereum-foundation", "Ethereum Foundation", r"ethereum\s+foundation", r"\u4ee5\u592a\u574a\u57fa\u91d1\u4f1a"),
    _entity("ethereum", "Ethereum", r"ethereum", r"\u4ee5\u592a\u574a"),
    _entity("solana", "Solana", r"solana", r"\u7d22\u62c9\u7eb3"),
    _entity("base", "Base", r"base\s+(?:chain|l2|network|app)", r"coinbase\s+base"),
    _entity("arbitrum", "Arbitrum", r"arbitrum"),
    _entity("optimism", "Optimism", r"\boptimism\b"),
    _entity("polygon", "Polygon", r"polygon", r"\bmatic\b"),
    _entity("uniswap", "Uniswap", r"uniswap"),
    _entity("aave", "Aave", r"\baave\b"),
    _entity("chainlink", "Chainlink", r"chainlink", r"chain\.link"),
    _entity("hyperliquid", "Hyperliquid", r"hyperliquid"),
    _entity("pumpfun", "pump.fun", r"pump\.?fun"),
    _entity("polymarket", "Polymarket", r"polymarket"),
    _entity("telegram", "Telegram / TON", r"telegram", r"toncoin", r"ton\s+(?:chain|blockchain|network)", r"\u7535\u62a5"),
    _entity("tron", "Tron", r"\btron\b", r"\u6ce2\u573a"),
    _entity("strategy", "Strategy (MicroStrategy)", r"microstrategy", r"\bmstr\b"),
    _entity("blackrock", "BlackRock", r"blackrock", r"\u8d1d\u83b1\u5fb7"),
    _entity("sec", "SEC", r"\bsec\b", r"securities and exchange commission", r"\u7f8e\u56fd\u8bc1\u76d1\u4f1a"),
    _entity("cftc", "CFTC", r"\bcftc\b"),
    _entity("trump", "Trump", r"\btrump\b", r"\u7279\u6717\u666e"),
    _entity("world-liberty", "World Liberty Financial", r"world\s+liberty"),
    _entity("galaxy-digital", "Galaxy Digital", r"galaxy\s+digital"),
    _entity("cantor", "Cantor Fitzgerald", r"cantor\s+fitzgerald"),
)

#: 这个域上的文章, 发布方就是对应的主体 (托管平台如 GitHub、Medium 不算).
PUBLISHER_DOMAINS: tuple[Publisher, ...] = (
    Publisher("binance", ("binance.com",)),
    Publisher("coinbase", ("coinbase.com",)),
    Publisher("okx", ("okx.com",)),
    Publisher("bybit", ("bybit.com",)),
    Publisher("kraken", ("kraken.com",)),
    Publisher("robinhood", ("robinhood.com",)),
    Publisher("gemini", ("gemini.com",)),
    Publisher("upbit", ("upbit.com",)),
    Publisher("circle", ("circle.com",)),
    Publisher("tether", ("tether.to",)),
    Publisher("ripple", ("ripple.com",)),
    Publisher("ethereum-foundation", ("ethereum.org", "ethereum.foundation")),
    Publisher("solana", ("solana.com", "solanalabs.com")),
    Publisher("base", ("base.org",)),
    Publisher("arbitrum", ("arbitrum.foundation", "arbitrum.io")),
    Publisher("optimism", ("optimism.io",)),
    Publisher("polygon", ("polygon.technology",)),
    Publisher("uniswap", ("uniswap.org",)),
    Publisher("aave", ("aave.com",)),
    Publisher("chainlink", ("chain.link",)),
    Publisher("hyperliquid", ("hyperliquid.xyz",)),
    Publisher("polymarket", ("polymarket.com",)),
    Publisher("telegram", ("telegram.org",)),
    Publisher("tron", ("tron.network",)),
    Publisher("strategy", ("strategy.com", "microstrategy.com")),
    Publisher("blackrock", ("blackrock.com",)),
    Publisher("sec", ("sec.gov",)),
    Publisher("cftc", ("cftc.gov",)),
)

#: 原文里的这些写法也算提到了对应主体 (账号句柄一类文本正则接不住的写法, AIHOT 的
#: `IDENTITY_CONTEXT_ALIASES`)。
IDENTITY_CONTEXT_ALIASES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ethereum-foundation", re.compile(r"@ethereumfndn\b", re.I)),
    ("tether", re.compile(r"@tether_to\b", re.I)),
)

#: 译文可能把名字重新连起来 ("USDC 5" -> "USDC-5"): 同一段文字的两种写法都算提到 (AIHOT 的
#: `surfaceVariants`)。
_VARIANT = re.compile(r"\b(usdc|usdt|usde|bnb|eth|sol|xrp|uni|aave|arb|op)\s+(?=\d)", re.I)


def name_of(entity_id: str) -> str | None:
    """内部 id 印给人看的名字; 不认得这个 id 就是 None (AIHOT 的 `lexiconName`)。"""
    for entity in IDENTITY_LEXICON:
        if entity.id == entity_id:
            return entity.name
    return None


def match_ids(texts) -> list[str]:
    """这些文字里点到了哪些已知主体 (每一段各自算, 不看跨段, AIHOT 的 `matchEntityIds`)。"""
    strings = [t for t in texts if isinstance(t, str) and t.strip()]
    return [entity.id for entity in IDENTITY_LEXICON
            if any(pattern.search(t) for pattern in entity.patterns for t in strings)]


def variants(texts) -> list[str]:
    """原样的每一段, 加上"重新连字符"的写法。"""
    exact = [t for t in texts if isinstance(t, str) and t.strip()]
    return exact + [_VARIANT.sub(r"\1-", t) for t in exact]


def publisher_id(url: str) -> str | None:
    """这个地址的发布方是谁: 域名命中 `PUBLISHER_DOMAINS` 才算 (AIHOT 的 `publisherEntityId`)。"""
    host = (urlsplit(url or "").hostname or "").lower()
    if not host:
        return None
    for entry in PUBLISHER_DOMAINS:
        if any(host == domain or host.endswith("." + domain) for domain in entry.domains):
            return entry.entity_id
    return None


@dataclass(frozen=True)
class Context:
    """一条材料的身份白名单: 谁在材料里出现过, 谁就一定算数 (发布域与来源主体)。"""

    allowed: tuple[str, ...]
    publisher: str | None = None
    owner: str | None = None

    def facts(self) -> str:
        """提示词里那段「已核验身份上下文」的正文 (没有就明说没识别到)。"""
        facts: list[str] = []
        if self.publisher:
            facts.append("\u6587\u6863\u53d1\u5e03\u57df\u4e3b\u4f53=%s" % (name_of(self.publisher) or self.publisher))
        if self.owner:
            facts.append("\u6765\u6e90\u8d26\u53f7\u4e3b\u4f53=%s" % (name_of(self.owner) or self.owner))
        return "\uff1b".join(facts) if facts else "\u672a\u8bc6\u522b\u5230\u660e\u786e\u53d1\u5e03\u4e3b\u4f53"

    def guard(self, *, title_zh: str, summary: str, original_title: str) -> tuple[str, str, dict]:
        """按白名单裁一遍标题与摘要 (AIHOT 的 `enforceIdentity`)。

        退回去的东西: 标题用**原标题**(原文本就是中文), 摘要直接丢掉 —— 宁可少说一句, 也不说
        一句主体错的。返回 (标题, 摘要, 账目): 账目只在真退过时才有内容, 供落库与排查。
        """
        allowed = set(self.allowed)
        bad_title = [entity_id for entity_id in match_ids([title_zh]) if entity_id not in allowed]
        bad_summary = [entity_id for entity_id in match_ids([summary]) if entity_id not in allowed]
        if bad_title:
            title = original_title if looks_zh(original_title) else ""
        else:
            title = title_zh
        return (title, "" if bad_summary else summary,
                {"outcome": "fallback" if bad_title or bad_summary else "pass",
                 "title": bad_title, "summary": bad_summary})


def context(*, title: str = "", text: str = "", main_text: str = "",
            quoted_text: str = "", source_name: str = "", url: str = "",
            owner: str | None = None) -> Context:
    """一条材料的身份白名单 (AIHOT 的 `identityContext`)。

    材料里出现过的都算, 发布域与来源主体各额外算一个 —— 一份"某交易所公告"的材料, 即使正文只有
    一句话, 发布域已经说明了发布方是谁。
    """
    texts = [title, text, main_text, quoted_text, source_name]
    allowed = set(match_ids(variants(texts)))
    joined = "\n".join(t for t in texts if t)
    for entity_id, pattern in IDENTITY_CONTEXT_ALIASES:
        if pattern.search(joined):
            allowed.add(entity_id)
    publisher = publisher_id(url)
    if publisher:
        allowed.add(publisher)
    if owner and name_of(owner):
        allowed.add(owner)
    return Context(tuple(sorted(allowed)), publisher, owner)
