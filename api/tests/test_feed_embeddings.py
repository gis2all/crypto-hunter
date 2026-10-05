"""向量召回: 配了端点走余弦 (线 0.6), 没配退回字符二元组 (线 0.25), 两条线不混。

`embeddings.warm` 的整批语义是这里的要点: 一半候选用余弦、另一半用字面, 两条线不在一个尺度上,
结果既不是向量召回也不是字面召回, 而且完全静默。所以只要有一条没拿到, 整批就当没拿到。

AIHOT 的对应物是 `providers/embeddings.ts` + `events/recall.ts`; 它的向量落库 (`embeddings`
表), 我们只放内存并带 TTL —— 换来的是一条 404 不会让分组整轮停摆。
"""

from __future__ import annotations

import pytest

from app import config
from app.feed import embeddings, grouping, stories


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    """每个用例从「没配端点、缓存是空的」开始, 而且别让本机的 system.yaml 插手。"""
    embeddings.forget()
    monkeypatch.setattr(config, "load_env_file", lambda path=None: 0)
    for key in embeddings.ENV:
        monkeypatch.delenv(key, raising=False)
    yield
    embeddings.forget()


def _configure(monkeypatch, **extra):
    monkeypatch.setenv("CH_EMBEDDING_BASE_URL", "https://gw.example/v1")
    monkeypatch.setenv("CH_EMBEDDING_API_KEY", "k")
    monkeypatch.setenv("CH_EMBEDDING_MODEL", "m")
    for key, value in extra.items():
        monkeypatch.setenv("CH_EMBEDDING_DIMS", str(value))


class _Response:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("HTTP %d" % self.status_code)

    def json(self):
        return self._payload


def _post(monkeypatch, table=None, *, status=200, default=None) -> list[list[str]]:
    """假端点: 按文本从 `table` 取向量, 取不到就回 404. 记录每次请求送出去的文本."""
    calls: list[list[str]] = []

    def call(url, headers=None, json=None, timeout=None):
        texts = list(json["input"])
        calls.append(texts)
        if status >= 400:
            return _Response({}, status)
        data = []
        for index, text in enumerate(texts):
            vector = (table or {}).get(text, default)
            if vector is None:
                return _Response({}, 404)
            data.append({"index": index, "embedding": list(vector)})
        return _Response({"data": data})

    monkeypatch.setattr(embeddings.httpx, "post", call)
    return calls


def _candidate(key: str, title: str) -> grouping.Candidate:
    return grouping.Candidate(key, key, title, "", "CoinDesk", "2026-10-05T00:00:00Z", True, 0.0)


def test_without_an_endpoint_the_lexical_line_is_the_answer():
    assert embeddings.available() is False
    assert embeddings.warm(["anything"]) is None


def test_a_gateway_without_embeddings_does_not_take_the_round_down(monkeypatch):
    _configure(monkeypatch)
    _post(monkeypatch, status=404)
    assert embeddings.available() is True
    assert embeddings.warm(["anything"]) is None


def test_half_a_batch_is_no_batch(monkeypatch):
    _configure(monkeypatch)

    def call(url, headers=None, json=None, timeout=None):
        texts = list(json["input"])
        data = [{"index": index, "embedding": [1.0, 0.0] if index else []}
                for index in range(len(texts))]
        return _Response({"data": data})

    monkeypatch.setattr(embeddings.httpx, "post", call)
    assert embeddings.warm(["a", "b", "c"]) is None


def test_a_width_that_is_not_the_configured_one_is_no_batch(monkeypatch):
    _configure(monkeypatch, dims=4)
    _post(monkeypatch, default=[1.0, 0.0])
    assert embeddings.warm(["a"]) is None


def test_a_long_body_is_cut_before_it_is_paid_for(monkeypatch):
    _configure(monkeypatch)
    calls = _post(monkeypatch, default=[1.0, 0.0])
    embeddings.warm(["x" * 3000])
    assert len(calls[0][0]) == embeddings.TRUNCATE


def test_a_configured_endpoint_moves_recall_onto_the_cosine_line(monkeypatch):
    _configure(monkeypatch)
    query = _candidate("q", "桥被盗事件")
    near = _candidate("near", "abcdefgh")
    # 字面这条线召不回它: 两串共享零个二元组 —— 只有余弦能看见它们是一件事.
    assert grouping.lexical(query.text, near.text) == 0.0
    assert [c.key for c in grouping.recall(query, [near])] == []
    _post(monkeypatch, {query.text: [1.0, 0.0], near.text: [0.9, 0.44]})

    asked: list[str] = []

    def answer(messages, **kw):
        asked.append(kw["purpose"])
        return {"query": "x", "decisions": [{"id": "C1", "relation": "unrelated",
                                             "confidence": 0.5}]}

    monkeypatch.setattr(grouping.llm, "complete_json", answer)
    decisions, _ = stories._group(query, [near], [], timeout=1)
    # 模型被问了, 说明候选进了召回 —— 走的是余弦那条线.
    assert asked == [grouping.PURPOSE_GROUP]
    assert decisions[0].relation == "unrelated"


def test_the_second_round_does_not_pay_again(monkeypatch):
    _configure(monkeypatch)
    calls = _post(monkeypatch, {"a": [1.0, 0.0]})
    assert embeddings.warm(["a"]) == {"a": [1.0, 0.0]}
    assert embeddings.warm(["a"]) == {"a": [1.0, 0.0]}
    assert len(calls) == 1
