"""The JSON-RPC hop: which failures are worth another node, and which are answers."""

from __future__ import annotations


class _Reply:
    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


class _Client:
    def __init__(self, bodies):
        self.bodies = list(bodies)
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append(url)
        return _Reply(self.bodies.pop(0) if self.bodies else {"result": "0x0"})


def test_a_revert_is_not_asked_of_every_node(monkeypatch):
    """A missing getter reverts on all three nodes; rotating only triples the cost."""
    from app.monitors.core import chain

    fake = _Client([{"error": {"message": "execution reverted"}}])
    monkeypatch.setattr(chain, "_CLIENT", fake)
    monkeypatch.setattr(chain, "_RPC_AT", 0)
    assert chain._rpc("eth_call", [{}]) is None
    assert len(fake.calls) == 1


def test_a_busy_node_rotates_to_the_next(monkeypatch):
    from app.monitors.core import chain

    fake = _Client([{"error": {"message": "429 too many requests"}}, {"result": "0x1"}])
    monkeypatch.setattr(chain, "_CLIENT", fake)
    monkeypatch.setattr(chain, "_RPC_AT", 0)
    assert chain._rpc("eth_call", [{}]) == "0x1"
    assert len(fake.calls) == 2


def test_rpc_uses_the_chain_s_own_node(monkeypatch):
    from app import config
    from app.monitors.core import chain

    fake = _Client([{"result": "0x1"}])
    monkeypatch.setattr(chain, "_CLIENT", fake)
    monkeypatch.setattr(config, "load_chains",
                        lambda: {"robinhood": {"rpc": "https://rh.example"}})
    assert chain._rpc("eth_blockNumber", [], chain="robinhood") == "0x1"
    assert fake.calls == ["https://rh.example"]
