"""When a lexicon entry may be reused, and when it has to be written again.

The entry is both the 45% product score and the words on the card, and it used to be
written once and reused forever - which is how a card came to print a verdict quoting a
market cap four days old while the table beside it showed today's. So every entry now
carries the numbers it was written against, and every round asks this module whether the
entry still stands.

The order of the rules is the whole design, and one part of it is deliberate: a refresh
condition (expiry, a 40% move, a 40% holder gain) outranks the "written recently" window.
A token that doubled in five hours is exactly the one worth re-reading, so the cheap-window
rule must not be able to skip past it.

The refresh conditions also include a changed safety verdict (`safety_signature`): an entry
written while the LP question was still open says so on the card, and it has to be written
again once the answer arrives, whatever the price did in the meantime.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

#: The three verdicts the safety line prints, as the words a person reads. A test keeps them
#: in step with `report/facts.py`, because a row that records why it was rewritten should
#: quote the same labels the reader sees on the card.
SAFETY_SLOTS = ("源码", "权限", "LP")

#: What a reading looks like when the round could not answer any of the three questions.
#: It has a name because two readers ask about it: the writer stores it as a baseline (a
#: verdict that was still open IS what those words were written against), and the refresh
#: rule refuses to act on it (a verdict going missing is not a verdict changing).
UNDETERMINED = "源码 未判定 ｜ 权限 未判定 ｜ LP 未判定"

#: Settings defaults. The settings page reads the same keys off `pipeline.lexicon`.
DEFAULTS: dict[str, float] = {
    "expire_days": 2.0,
    "mcap_move_pct": 40.0,
    "holders_gain_pct": 40.0,
    "rewrite_skip_hours": 6.0,
}


@dataclass(frozen=True)
class Decision:
    """What to do with one entry, and the sentence that explains why."""

    action: str        # create | rewrite | reuse
    reason: str
    age_hours: float | None
    expired: bool


def settings(raw: Mapping[str, Any] | None) -> dict[str, float]:
    """The four knobs, with a default for every one a chain has not set."""
    out = dict(DEFAULTS)
    for key, fallback in DEFAULTS.items():
        try:
            value = float((raw or {}).get(key))
        except (TypeError, ValueError):
            continue
        if value >= 0:
            out[key] = value
    return out


def _stamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.astimezone()
    return stamp


def _move_pct(now_value: Any, baseline: Any) -> float | None:
    """How far a reading has travelled from the entry's own baseline, in percent."""
    try:
        current, base = float(now_value), float(baseline)
    except (TypeError, ValueError):
        return None
    if base <= 0:
        return None
    return (current - base) / base * 100.0


def _read(metrics: Any, key: str) -> Any:
    """One field off a mapping or off a model. Both arrive here: the engine builds the two
    readings it needs by hand, and a caller that already holds a TokenMetrics hands that."""
    if metrics is None:
        return None
    if isinstance(metrics, Mapping):
        return metrics.get(key)
    return getattr(metrics, key, None)


def _hours(now: datetime, then: datetime) -> float:
    return (now - then).total_seconds() / 3600.0


def _verdict(value: Any, yes: str, no: str) -> str:
    """A three-state reading: a blank is not a no."""
    if value is None:
        return "未判定"
    return yes if value else no


def safety_signature(facts: Any) -> str:
    """The three verdicts on the safety line, as one comparable string.

    Empty only when the caller has no reading at all. A reading in which nothing was
    answered is a real value (`UNDETERMINED`) on purpose: it is what an entry written while
    a source was down was written against, and the next round that does answer has to notice
    that the sentence saying "未判定" is now out of date. Both `due` and `mark_written` need
    the two cases told apart.
    """
    if facts is None:
        return ""
    verified = _read(facts, "source_verified")
    perms = _read(facts, "dangerous_perms")
    locked = _read(facts, "lp_locked")
    out = ("源码 " + _verdict(verified, "已验证", "未验证"),
           "权限 " + ("未判定" if perms is None else ("有危险权限" if perms else "无危险权限")),
           "LP " + _verdict(locked, "已锁", "未锁"))
    return " ｜ ".join(out)


def safety_moved(baseline: str, signature: str) -> bool:
    """Whether this round's reading is a change worth a rewrite.

    Three things are deliberately not changes: no baseline (nothing to compare against -
    `db.init_db` fills those from history), no reading at all, and a reading in which
    nothing was answered. The last two are one outage seen from two directions, and
    rewriting an entry because its sources went quiet would spend the rewrite and lose the
    evidence the card already carries.
    """
    return bool(baseline) and signature not in ("", UNDETERMINED) and baseline != signature


def _safety_change(before: str, after: str) -> str:
    """Which verdicts moved, in the words the reason column prints."""
    out = []
    for old, new in zip(before.split(" ｜ "), after.split(" ｜ ")):
        if old == new:
            continue
        label, _, was = old.partition(" ")
        _, _, now_text = new.partition(" ")
        out.append("%s %s→%s" % (label, was, now_text))
    return "、".join(out) or "判定更新"


def due(entry: Mapping[str, Any] | None, metrics: Mapping[str, Any] | None,
        cfg: Mapping[str, Any] | None, now: datetime | None = None,
        safety: Any = None) -> Decision:
    """create (no entry yet), rewrite (the entry no longer stands) or reuse.

    `safety` is this round's verdict block, when the caller has one: an entry whose words
    were written while the LP question was still open has to be written again once the
    answer lands, even if the market cap never moved. A round that cannot answer leaves the
    old words alone - see `safety_signature`.
    """
    conf = settings(cfg)
    now = now or datetime.now().astimezone()
    metrics = metrics or {}

    if not entry:
        return Decision("create", "新建档", None, False)

    written = _stamp(entry.get("updated_at"))
    if written is None:
        return Decision("rewrite", "档案没有可用时间戳", None, True)
    age = _hours(now, written)
    expired = age > conf["expire_days"] * 24.0

    move = _move_pct(_read(metrics, "market_cap_usd"), entry.get("baseline_mcap"))
    if move is not None and abs(move) >= conf["mcap_move_pct"]:
        return Decision("rewrite", "市值较建档 %+.0f%%（阈值 %.0f%%）"
                        % (move, conf["mcap_move_pct"]), age, expired)

    gain = _move_pct(_read(metrics, "holders"), entry.get("baseline_holders"))
    if gain is not None and gain >= conf["holders_gain_pct"]:
        return Decision("rewrite", "持有人较建档 %+.0f%%（阈值 %.0f%%）"
                        % (gain, conf["holders_gain_pct"]), age, expired)

    # The verdict changed, so the sentence that quoted the old one is now wrong. This
    # outranks the expiry rule below because it is the more informative reason, and it
    # outranks the skip window for the same reason every other refresh condition does.
    baseline_safety = str(entry.get("baseline_safety") or "").strip()
    signature = safety_signature(safety)
    if safety_moved(baseline_safety, signature):
        return Decision("rewrite", "安全判定变化（%s）"
                        % _safety_change(baseline_safety, signature), age, expired)

    if expired:
        return Decision("rewrite", "档案已 %.1f 天未重写（阈值 %.1f 天）"
                        % (age / 24.0, conf["expire_days"]), age, True)

    if age < conf["rewrite_skip_hours"]:
        return Decision("reuse", "%.1f 小时前刚重写过" % age, age, False)

    return Decision("rewrite", "距上次重写 %.1f 小时（跳过窗口 %.1f 小时）"
                    % (age, conf["rewrite_skip_hours"]), age, False)

def mark_written(entry: Mapping[str, Any] | None, metrics: Mapping[str, Any] | None,
                 observed_at: Any, safety: Any = None) -> dict[str, Any]:
    """The baseline columns a fresh entry carries: what it was written against."""
    return {
        "baseline_mcap": _read(metrics, "market_cap_usd"),
        "baseline_holders": _read(metrics, "holders"),
        "baseline_at": observed_at,
        # Stored as the readable signature, not a hash: it lands in a reason column and a
        # diff of it is the human sentence the card shows.
        "baseline_safety": safety_signature(safety) or None,
    }


def age_text(hours: float | None) -> str:
    """How old the words are, short enough for the badge the card carries.

    The card prints this next to today numbers, and that is the point: a verdict written
    four days ago must never read like something this round decided.
    """
    if hours is None:
        return "时间未知"
    if hours < 1:
        return "不到 1 小时前"
    if hours < 48:
        return "%.0f 小时前" % hours
    return "%.1f 天前" % (hours / 24.0)
