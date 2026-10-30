"""Monitors: standing questions this app asks the outside world on its own clock.

One monitor owns its queries, its cards, its page and its cadence (user, 2026-10-29). Nothing is
shared between two monitors: the same project may be a card in two of them, and that is the
normal outcome rather than something to deduplicate away.
"""
