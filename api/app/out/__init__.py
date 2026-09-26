"""Push outlets.

Telegram is explicitly out of scope for now (decided in this thread): the web UI is
the only outlet until that changes. When it comes back, it becomes one more consumer
of the same report rows - never a second source of truth, and never a blocker for a
round to finish.
"""

def configured() -> bool:
    """False until an outlet exists. `run_chain` asks this to decide whether a round is
    finished: with nothing to push to, waiting for a push would leave every round reading
    as still running forever."""
    return False


def status() -> str:
    """What the round and the settings page say about the push step. A round with no
    outlet is not a failed round, it is a round nobody asked to send."""
    return "未配置（跳过推送，报告已落库）" if not configured() else "已送出"
