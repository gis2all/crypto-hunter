"""The free gate, as rules instead of a word list.

A scan reads a few hundred posts and asks a model about a fraction of them. The decision of
*which* fraction used to be one substring test against a list of action words
(`scan.ACTION_WORDS`), and that gate had two problems this module could not fix by tuning:

- It could only say yes or no. When more material passes than the extraction pass has budget
  for, "which twelve" was answered by the clock rather than by anything about the material.
- It could not express a *refusal*. The list had no way to say "an exchange trading campaign is
  not what this page is for", which is the one thing the first live scans showed it needed.

So the gate is a rule set: conditions over the text, the author, the numbers and the age; each
rule scores, levels, qualifies or excludes; every hit is recorded with the conditions that
matched. It is
the reference implementation's model (`backend/collector_rules.py` in xdata-collector, whose
`exclude-trade-gated` rule is the one copied first), narrowed to what this module can explain.

**What it is not.** It does not classify, it does not rank the page, and it is not a third
badge: it decides who reaches the model and, when there is more material than budget, who is
asked first. Discovery comes from the query list; a word list cannot find anything new.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

#: The rule set lives beside the watchlist seed: data, not code, because the whole point is that
#: it can be re-read and revised without touching the evaluator.
RULE_PATH = Path(__file__).with_name("seeds") / "rules.json"

HASHTAG = re.compile(r"(^|\s)#\w+")
CASHTAG = re.compile(r"(^|\s)\$[A-Za-z][A-Za-z0-9_]{1,9}")
URL = re.compile(r"https?://", re.IGNORECASE)
EMOJI = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF]")

#: Broad enough to recognize a crypto post without mistaking ordinary commerce for one.
#: Generic words that non-crypto posts also use (`sale`, `subscription`, `points`, `quest`,
#: `early access`) are deliberately absent - those are the words that let a concert ticket sale
#: or a bookmark manager through.
CRYPTO_WORDS = (
    "crypto", "cryptocurrency", "blockchain", "onchain", "on-chain", "web3", "web 3",
    "token", "tokenomics", "tokenized", "memecoin", "stablecoin", "altcoin",
    "defi", "dapp", "dapps", "dao", "nft", "nfts", "smart contract", "wallet",
    "seed phrase", "private key", "gas fee", "mainnet", "testnet", "devnet", "faucet",
    "staking", "restaking", "validator", "sequencer", "prover", "rollup", "node",
    "l1", "l2", "layer 1", "layer 2", "bridge", "dex", "cex", "liquidity",
    "yield farming", "launchpad", "airdrop", "allowlist", "whitelist", "mint",
    "fair launch", "bonding curve", "token sale", "tge", "ido", "ico", "lbp",
    "rwa", "depin", "ordinal", "inscription", "brc-20", "erc-20", "spl token",
    "block explorer", "crypto exchange", "web3 wallet", "digital asset",
    "digital collectible", "nft marketplace", "protocol",
    "bitcoin", "btc", "ethereum", "eth", "solana", "base chain", "on base",
    "built on base", "bnb chain", "bsc", "binance smart chain", "arbitrum",
    "optimism", "polygon", "avalanche", "tron", "sui", "aptos", "near protocol",
    "cosmos", "celestia", "monad", "berachain", "hyperliquid", "robinhood chain",
    "x layer", "starknet", "zksync", "scroll", "linea", "mantle", "blast", "sei",
    "injective", "stacks", "cardano", "kaspa", "aleo", "flare", "sonic", "unichain",
    "apechain", "moonbeam", "gnosis", "fantom", "metis", "taiko", "fuel", "initia",
    "加密", "加密货币", "区块链", "链上", "公链", "主网", "测试网", "水龙头",
    "代币", "通证", "稳定币", "钱包", "私钥", "助记词", "合约", "智能合约",
    "空投", "铸造", "白名单", "打新", "认购", "质押", "验证者", "验证节点",
    "矿工", "挖矿", "流动性", "交易对", "去中心化", "数字藏品", "铭文", "符文",
    "链上游戏", "元宇宙", "代币经济", "加密交易所", "节点", "协议",
    "比特币", "以太坊", "币安链", "币安智能链", "波场", "索拉纳",
)

#: A post that links to an explorer / market / quest platform is a crypto post even when the
#: text itself is terse.
CRYPTO_DOMAINS = (
    "etherscan.io", "bscscan.com", "basescan.org", "arbiscan.io", "tronscan.org",
    "solscan.io", "suiscan.xyz", "dexscreener.com", "coinmarketcap.com", "coingecko.com",
    "galxe.com", "layer3.xyz", "zealy.io", "intract.io", "mirror.xyz", "paragraph.xyz",
    "zora.co", "opensea.io", "blur.io", "pump.fun", "four.meme", "clanker.world",
)

_CASHTAG_ANY = re.compile(r"\$[A-Za-z][A-Za-z0-9_]{1,9}\b")
_EVM_ADDRESS = re.compile(r"(?<![0-9a-z])0x[0-9a-f]{40}(?![0-9a-z])", re.IGNORECASE)

#: The floor a post has to reach *after* it has qualified. The score is a ranking number, not the
#: decision: the decision is whether a rule marked `qualify` was hit, and only a concrete action
#: is allowed to carry that mark. Kept as a floor so a rule set that drifts towards small weights
#: still cannot open the gate on bonus points alone.
PASS_SCORE = 30

_cache: dict = {}
_words: dict[str, re.Pattern] = {}


def load(path: Path | None = None) -> dict:
    """The rule set, read once per process."""
    key = str(path or RULE_PATH)
    if key not in _cache:
        target = path or RULE_PATH
        _cache[key] = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {
            "levels": [], "rules": []}
    return _cache[key]


def _word(value: str) -> re.Pattern:
    """One ASCII phrase, matched on word boundaries."""
    pattern = _words.get(value)
    if pattern is None:
        pattern = re.compile(r"(?<![a-z0-9])" + re.escape(value) + r"(?![a-z0-9])")
        _words[value] = pattern
    return pattern


def word_hit(value: str, text_lower: str) -> bool:
    """One phrase against the lowered text.

    ASCII matches on word boundaries so `lend` does not fire inside `lending`; CJK has no word
    boundaries, so it stays a substring test.
    """
    if value.isascii():
        return bool(_word(value).search(text_lower))
    return value in text_lower


def crypto_context(text: str) -> bool:
    """Is there a crypto anchor anywhere in this text?

    Used by the gate (a non-crypto post must not qualify on `public sale` / `early access`) and
    by extraction (a material with no anchor produces no entries). The vocabulary is deliberately
    wide - chains, tokens, wallets, contracts, DeFi, staking, gas, mainnet/testnet, Chinese terms,
    cashtags, EVM addresses and crypto-native domains - because the failure mode we are fixing
    was a false *positive*, not a false negative.
    """
    body = str(text or "")
    low = body.lower()
    if _CASHTAG_ANY.search(body) or _EVM_ADDRESS.search(body):
        return True
    if any(domain in low for domain in CRYPTO_DOMAINS):
        return True
    return any(word_hit(word, low) for word in CRYPTO_WORDS)


def features(text: str, *, author: str = "", metrics: dict | None = None,
             created_at: str | None = None, now: datetime | None = None,
             known: set[str] | None = None, source_official: bool = False) -> dict:
    """Everything a condition may read, derived once per post.

    `known` is the watchlist's own handles, injected rather than written into the rule set: the
    list changes as sources are added, and a rule set that carried a copy of it would be a second
    truth about who we watch.
    """
    body = str(text or "")
    lowered = body.lower()
    numbers = metrics or {}
    moment = None
    if created_at:
        try:
            moment = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
        except ValueError:
            moment = None
    handle = str(author or "").lstrip("@").lower()
    return {
        "text": body,
        "text_lower": lowered,
        "author": handle,
        "known": {str(name).lstrip("@").lower() for name in (known or set())},
        "metrics": {key: int(numbers.get(key) or 0)
                    for key in ("views", "likes", "replies", "retweets")},
        "created_at": moment,
        "age_days": None if moment is None
        else max(0.0, ((now or datetime.now(timezone.utc)) - moment).total_seconds() / 86400.0),
        "has_link": bool(URL.search(body)),
        "has_hashtag": bool(HASHTAG.search(body)),
        "has_cashtag": bool(CASHTAG.search(body)),
        "has_emoji": bool(EMOJI.search(body)),
        "crypto_context": crypto_context(body),
        "is_retweet": lowered.startswith("rt @"),
        "is_reply": lowered.startswith("@"),
        "source_official": bool(source_official),
    }


def condition_hit(condition: dict, facts: dict) -> bool:
    """One condition. An unknown type never matches - a typo must not open the gate."""
    kind = condition.get("type")
    values = [str(value).lower() for value in condition.get("values") or []]
    if kind == "text_contains_any":
        return any(value in facts["text_lower"] for value in values)
    if kind == "text_not_contains_any":
        return bool(values) and not any(value in facts["text_lower"] for value in values)
    if kind == "crypto_context_missing":
        return not facts.get("crypto_context")
    if kind == "text_word_any":
        return any(word_hit(value, facts["text_lower"]) for value in values)
    if kind == "text_word_not_any":
        return bool(values) and not any(
            word_hit(value, facts["text_lower"]) for value in values)
    if kind == "author_in":
        return facts["author"] in {value.lstrip("@") for value in values}
    if kind == "author_not_in":
        return facts["author"] not in {value.lstrip("@") for value in values}
    if kind == "author_contains_any":
        return any(value in facts["author"] for value in values)
    if kind == "author_not_known":
        # 不在 watchlist 里的账号。规则表不再用它加分：陌生账号不是机会信号。
        return bool(facts["author"]) and facts["author"] not in facts["known"]
    if kind == "metric_at_least":
        return facts["metrics"].get(str(condition.get("metric") or "views"), 0) >= int(
            condition.get("value") or 0)
    if kind == "age_within_days":
        if facts["age_days"] is None:
            return False
        return facts["age_days"] <= float(condition.get("value") or 0)
    if kind == "has_link":
        return facts["has_link"]
    if kind == "has_hashtag":
        return facts["has_hashtag"]
    if kind == "has_cashtag":
        return facts["has_cashtag"]
    if kind == "has_emoji":
        return facts["has_emoji"]
    if kind == "is_retweet":
        return facts["is_retweet"]
    if kind == "is_reply":
        return facts["is_reply"]
    if kind == "source_official_or_link":
        return bool(facts["source_official"] or facts["has_link"])
    return False


def describe(condition: dict) -> str:
    kind = condition.get("type")
    if kind in ("text_contains_any", "text_not_contains_any", "text_word_any",
                "text_word_not_any", "author_in", "author_not_in", "author_contains_any"):
        return "%s:%s" % (kind, ", ".join(str(value) for value in condition.get("values") or []))
    if kind == "metric_at_least":
        return "%s >= %s" % (condition.get("metric") or "views", condition.get("value") or 0)
    if kind in ("age_within_days",):
        return "%s:%s" % (kind, condition.get("value") or 0)
    return str(kind)


def judge(text: str, *, author: str = "", metrics: dict | None = None,
          created_at: str | None = None, ruleset: dict | None = None,
          now: datetime | None = None, known: set[str] | None = None,
          family: str | None = None, source_official: bool = False) -> dict:
    """One post against the rule set: does it pass, how does it score, and why.

    Two ways to fail. An `exclude` rule is a refusal - read in order, the first one ends the
    reading, which is what makes it a refusal rather than a penalty. Otherwise the post has to
    hit a rule that carries `qualify`, and clear `PASS_SCORE` on top of it. Only a concrete
    action carries that mark: a noun that happens to appear in a project's own prose
    (`validator`, `yield`, `lending`) scores but does not open the gate.
    """
    from . import settings

    definition = ruleset or load()
    threshold = int(settings.value("pass_score", PASS_SCORE))
    facts = features(text, author=author, metrics=metrics, created_at=created_at, now=now,
                     known=known, source_official=source_official)
    score = 0
    hits: list[dict] = []
    level = ""
    qualified = 0
    for rule in definition.get("rules") or []:
        if not rule.get("enabled", True):
            continue
        rule_family = rule.get("family")
        if rule_family and rule_family != family:
            continue
        if rule.get("id") == "action-keywords" and family:
            continue
        matched = [condition for condition in rule.get("conditions") or []
                   if condition_hit(condition, facts)]
        required = len(rule.get("conditions") or [])
        if not matched or (rule.get("operator", "AND") == "AND" and len(matched) != required):
            continue
        effect = rule.get("effect") or {}
        hits.append({"rule": rule.get("id"), "name": rule.get("name"),
                     "action": effect.get("action", "score"),
                     "score": int(effect.get("score") or 0),
                     "matched": [describe(condition) for condition in matched]})
        if effect.get("action") == "exclude":
            return {"pass": False, "score": 0, "level": "", "excluded": True, "hits": hits}
        score += int(effect.get("score") or 0)
        if effect.get("qualify"):
            qualified += 1
        if effect.get("level"):
            level = str(effect["level"])
    if not level:
        for band in sorted(definition.get("levels") or [],
                           key=lambda item: int(item.get("min_score") or 0), reverse=True):
            if score >= int(band.get("min_score") or 0):
                level = str(band.get("id"))
                break
    return {"pass": qualified > 0 and score >= threshold,
            "score": score, "level": level, "excluded": False, "hits": hits}
