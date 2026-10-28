"""新机会：从外部世界发现活动与新项目。

这个模块与代币筛选流水线无关：不读候选池、不写 `runs`、不参与打分。它读 watchlist（官方号、
平台、交易所公告、生态目录），把读到的材料分组，再把其中的活动与新项目抽成带来源的条目。

分层与边界写在同目录的 README.md；HTTP 层在 `api/v1/opportunity.py`，表在 `store/schema.sql`。
"""

from __future__ import annotations

from . import job, promote, recategorize, schema, trust, watchlist  # noqa: F401  (the module's front door)

__all__ = ["job", "promote", "recategorize", "schema", "trust", "watchlist"]
