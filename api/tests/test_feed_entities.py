"""The entity dictionary: 谁在说谁, and the one list the model may pick from.

词典是人写的名单 (config/feed/entities.yaml), 这几条测的是它作为**白名单**的那一半: 整词、
代码的大小写、过筛、以及一行写坏了会不会静默地少一个实体。它不是分组键 —— 拿它当分组键的
结果记在 entities.py 的开头。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.feed import entities


def test_the_dictionary_reads_and_every_row_is_usable():
    rows = entities.all()
    assert len(rows) >= 50, "词典太小了, 白名单会把它没写的实体全筛掉"
    assert len({row.id for row in rows}) == len(rows)
    for row in rows:
        assert row.id and row.id == row.id.lower()
        assert row.aliases or row.tickers, row.id


def test_a_name_is_matched_as_a_whole_word():
    assert "ethereum" in entities.candidates("Ethereum Foundation ships something")
    # `Ethereal` 里没有 `ether` 这个词 —— 整词的边界就是这条测试守的东西.
    assert "ethereum" not in entities.candidates("Ethereal things happened")


def test_a_bare_ticker_counts_only_in_upper_case():
    assert "ethereum" in entities.candidates("ETH broke 4,000")
    # 小写的 eth 是英文句子里的三个字母, 不是代码.
    assert "ethereum" not in entities.candidates("the eth of it")


def test_a_subject_that_is_not_in_the_dictionary_does_not_survive():
    assert entities.subjects(["ethereum", "a-made-up-coin", "ETH", "ethereum"]) == ["ethereum"]


def test_a_label_is_the_writing_the_dictionary_gives():
    assert entities.label("ethereum") == "Ethereum"
    # 词典里没有的 id 印它自己: 页面不能因为一个生 id 就印空白.
    assert entities.label("a-made-up-coin") == "a-made-up-coin"


def test_a_row_with_an_unknown_key_is_refused(tmp_path: Path):
    bad = tmp_path / "entities.yaml"
    bad.write_text("entities:\n  - {id: nova, name: Nova, aliases: [nova], kind: chain, nickname: Nova}\n",
                   encoding="utf-8")
    with pytest.raises(entities.EntitiesRefused):
        entities.load(bad)


def test_a_row_with_no_way_to_recognise_it_is_refused(tmp_path: Path):
    bad = tmp_path / "entities.yaml"
    bad.write_text("entities:\n  - {id: nova, name: Nova}\n", encoding="utf-8")
    with pytest.raises(entities.EntitiesRefused):
        entities.load(bad)


def test_no_dictionary_file_is_an_empty_table(tmp_path: Path):
    # 一台刚拉下来的机器不该因为少一个词典就不能出刊.
    assert entities.load(tmp_path / "nothing.yaml") == ()
