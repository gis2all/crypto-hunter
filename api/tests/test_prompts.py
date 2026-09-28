"""The writing prompts are a contract, and their examples are part of it.

Each prompt ends with a JSON example of one entry. When the entry model
gained the five `f_*` judgements the examples kept the old `narrative` / `product` pair:
the model was asked for keys the validator rejects, and an entry that never validates is
an entry that never scores - silently. A prompt and its model are one thing written in
two files, so the drift is measured here rather than found in a round.
"""

from __future__ import annotations

import json
import re

import pytest

from app.report import lexicon
from app.report.schema import NewTokenEntry

PROMPTS = ("research.md",)


def _example_entry(name: str) -> dict:
    """The single entry object each prompt's ```json block shows."""
    text = lexicon.read_prompt(name)
    block = re.search(r"```json\n(.*?)```", text, re.S)
    assert block, "%s has no ```json example" % name
    example = json.loads(block.group(1))
    entries = example.get("entries") or example.get("new_tokens")
    assert entries, "%s: the example carries no entry object" % name
    assert len(entries) == 1, "%s: the example should show one entry" % name
    return next(iter(entries.values()))


@pytest.mark.parametrize("name", PROMPTS)
def test_prompt_example_matches_the_entry_model(name):
    shown = set(_example_entry(name))
    wanted = set(NewTokenEntry.model_fields)
    assert shown == wanted, "%s: example vs NewTokenEntry: %s" % (name, sorted(shown ^ wanted))
