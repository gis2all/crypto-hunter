"""The model layer: one blocking OpenAI-compatible client.

Re-exported here so the rest of the code imports the package rather than reaching into
`client`: the pipeline only ever wants "write me text, or tell me you cannot".
"""

from __future__ import annotations

from .client import (ContentRefused, LLMUnavailable, complete, complete_json, configured,
                    settings)

__all__ = ["ContentRefused", "LLMUnavailable", "complete", "complete_json", "configured",
           "settings"]
