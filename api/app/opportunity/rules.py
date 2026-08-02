"""The free gate, as rules instead of a word list.

A scan reads a few hundred posts and asks a model about a fraction of them. The decision of
*which* fraction used to be one substring test against a list of action words
(`scan.ACTION_WORDS`), and that gate had two problems this module could not fix by tuning:

- It could only say yes or no. When more material passes than the extraction pass has budget
  for, "which twelve" was answered by the clock rather than by anything about the material.
- It could not express a *refusal*. The list had no way to say "an exchange trading campaign is
  not what this page is for", which is the one thing the first live scans showed it needed.

So the gate is a rule set: conditions over the text, the author, the numbers and the age; each
rule scores, levels or excludes; every hit is recorded with the conditions that matched. It is
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

#: What a post has to reach to be worth a model call. A link alone (10) is not enough; a fresh
#: post with a link (30) is. The number is a budget decision as much as a quality one - the
#: extraction pass reads twelve materials a scan, so the gate's job is to hand it the twelve
#: best, not to be right about every post.
PASS_SCORE = 30

#: ...and it has to carry one of these, whatever it scored. Without this, "an account we do not
#: watch, posted today" (25 + 20 = 45) passed on its own - measured on the first run of this
#: evaluator, where a stranger's `gm` cleared the gate. Being unknown is a reason to look closer,
#: not a reason to spend a model call: the post still has to announce, launch or offer something.
CONTENT_RULES = ("action-keywords", "early-words", "launch-words")

_cache: dict = {}


def load(path: Path | None = None) -> dict:
    """The rule set, read once per process."""
    key = str(path or RULE_PATH)
    if key not in _cache:
        target = path or RULE_PATH
        _cache[key] = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {
            "levels": [], "rules": []}
    return _cache[key]


def features(text: str, *, author: str = "", metrics: dict | None = None,
             created_at: str | None = None, now: datetime | None = None,
             known: set[str] | None = None) -> dict:
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
        "is_retweet": lowered.startswith("rt @"),
        "is_reply": lowered.startswith("@"),
    }


def condition_hit(condition: dict, facts: dict) -> bool:
    """One condition. An unknown type never matches - a typo must not open the gate."""
    kind = condition.get("type")
    values = [str(value).lower() for value in condition.get("values") or []]
    if kind == "text_contains_any":
        return any(value in facts["text_lower"] for value in values)
    if kind == "text_not_contains_any":
        return bool(values) and not any(value in facts["text_lower"] for value in values)
    if kind == "author_in":
        return facts["author"] in {value.lstrip("@") for value in values}
    if kind == "author_not_in":
        return facts["author"] not in {value.lstrip("@") for value in values}
    if kind == "author_contains_any":
        return any(value in facts["author"] for value in values)
    if kind == "author_not_known":
        # 不在 watchlist 里的账号：不是"可疑"，是"还没被我们认识"，所以它加分而不是被排除。
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
    return False


def describe(condition: dict) -> str:
    kind = condition.get("type")
    if kind in ("text_contains_any", "text_not_contains_any", "author_in", "author_not_in",
                "author_contains_any"):
        return "%s:%s" % (kind, ", ".join(str(value) for value in condition.get("values") or []))
    if kind == "metric_at_least":
        return "%s >= %s" % (condition.get("metric") or "views", condition.get("value") or 0)
    if kind in ("age_within_days",):
        return "%s:%s" % (kind, condition.get("value") or 0)
    return str(kind)


def judge(text: str, *, author: str = "", metrics: dict | None = None,
          created_at: str | None = None, ruleset: dict | None = None,
          now: datetime | None = None, known: set[str] | None = None) -> dict:
    """One post against the rule set: does it pass, how does it score, and why.

    Two ways to fail. An `exclude` rule is a refusal - read in order, the first one ends the
    reading, which is what makes it a refusal rather than a penalty. Otherwise the post has to
    reach `PASS_SCORE`: a link and nothing else is not worth a model call, so the gate is a
    threshold rather than "any rule matched".
    """
    from . import settings

    definition = ruleset or load()
    threshold = int(settings.value("pass_score", PASS_SCORE))
    facts = features(text, author=author, metrics=metrics, created_at=created_at, now=now,
                     known=known)
    score = 0
    hits: list[dict] = []
    level = ""
    for rule in definition.get("rules") or []:
        if not rule.get("enabled", True):
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
        if effect.get("level"):
            level = str(effect["level"])
    if not level:
        for band in sorted(definition.get("levels") or [],
                           key=lambda item: int(item.get("min_score") or 0), reverse=True):
            if score >= int(band.get("min_score") or 0):
                level = str(band.get("id"))
                break
    return {"pass": score >= threshold and any(hit["rule"] in CONTENT_RULES for hit in hits),
            "score": score, "level": level, "excluded": False, "hits": hits}
