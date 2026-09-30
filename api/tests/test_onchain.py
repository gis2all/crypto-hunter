"""The on-chain reads behind the LP and launch-path facts.

The interesting failures here are silent: a branch that raises is caught, turned into
"鏈上查询失败" and shown to the operator as a missing fact, so nothing crashes and nothing
looks wrong until someone asks why the fact is missing.
"""

import pytest

from app.sources import onchain

POOL = "0x" + "1" * 40
#: The explorer row these tests use. Every real chain config carries one; a chain without
#: one is an error now, not a fallback to Arc (see `onchain._own_address`).
EXPLORER = "https://explorer.arc.io"
FACTORY = "0x" + "2" * 40


def _blockscout(routes: dict, answered: bool = True):
    def fake(_chain, path, tries: int = 3):
        return routes.get(path, {}), answered
    return fake


def test_a_pool_whose_creation_transaction_came_back_does_not_crash(monkeypatch):
    """The target's contract-ness was asked through a name that had just been rebound to a
    local: every pool with a readable creation transaction raised UnboundLocalError."""
    monkeypatch.setattr(onchain, "blockscout_checked", _blockscout({
        "addresses/%s" % POOL: {"creation_transaction_hash": "0xtx"},
        "smart-contracts/%s" % FACTORY: {"name": "LaunchFactory"},
    }))
    monkeypatch.setattr(onchain, "rpc", lambda _chain, _method, _params: {"to": FACTORY})
    monkeypatch.setattr(onchain, "is_contract", lambda _chain, _address: True)

    target, target_is_contract, name, reason = onchain.launch_path({"id": "arc"}, POOL)

    assert (target, target_is_contract, name) == (FACTORY, True, "LaunchFactory")
    assert reason == onchain.LAUNCH_OK


def test_a_pool_target_that_is_not_a_contract_says_so(monkeypatch):
    monkeypatch.setattr(onchain, "blockscout_checked", _blockscout({
        "addresses/%s" % POOL: {"creation_transaction_hash": "0xtx"},
    }))
    monkeypatch.setattr(onchain, "rpc", lambda _chain, _method, _params: {"to": FACTORY})
    monkeypatch.setattr(onchain, "is_contract", lambda _chain, _address: False)

    assert onchain.launch_path({"id": "arc"}, POOL) == (FACTORY, False, "", onchain.LAUNCH_OK)


def test_a_v4_pool_id_is_not_an_address_and_is_left_alone(monkeypatch):
    def boom(*_a):
        raise AssertionError("a pool id is not an address: nothing to ask")

    monkeypatch.setattr(onchain, "blockscout_checked", boom)
    assert onchain.launch_path({"id": "arc"}, "0x" + "3" * 64) == (
        None, None, "", onchain.LAUNCH_V4,
    )


def test_a_launchpad_pool_with_no_creation_transaction_is_an_honest_pass(monkeypatch):
    """Nothing to read is not a reason to invent a verdict, but a launchpad that issued it
    is the reference's own "LP locked by the launch path" case."""
    monkeypatch.setattr(onchain, "lp_holders", lambda _chain, _pair: [])
    monkeypatch.setattr(onchain, "nft_balances", lambda _chain, managers, _owner: {label: 0 for label in managers.values()})
    monkeypatch.setattr(onchain, "launch_path", lambda _chain, _pair: (None, None, "", onchain.LAUNCH_NO_TX))

    locked, basis, _evidence = onchain.lock_facts(
        {"id": "arc"}, ca="0x" + "4" * 40, pair=None, creator="0x" + "5" * 40,
        launchpad="minara")

    assert locked is True
    assert "minara" in basis


def test_nothing_to_read_and_no_launchpad_is_not_a_pass(monkeypatch):
    monkeypatch.setattr(onchain, "lp_holders", lambda _chain, _pair: [])
    monkeypatch.setattr(onchain, "nft_balances", lambda _chain, managers, _owner: {label: 0 for label in managers.values()})
    monkeypatch.setattr(onchain, "launch_path", lambda _chain, _pair: (None, None, "", onchain.LAUNCH_NO_TX))

    locked, _basis, _evidence = onchain.lock_facts(
        {"id": "arc"}, ca="0x" + "4" * 40, pair=None, creator="0x" + "5" * 40,
        launchpad=None)

    assert locked is None


def test_a_404_is_an_answer_and_a_timeout_is_not(monkeypatch):
    """The whole point of the split. An explorer that returns 404 has told us the address
    is not indexed; an explorer that times out has told us nothing at all."""
    from app.sources import http as http_mod

    def four_oh_four(*_a, **_k):
        raise http_mod.SourceError("https://explorer.arc.io/api/v2/addresses/0x1 -> HTTP 404")

    monkeypatch.setattr(onchain, "json_get", four_oh_four)
    assert onchain.blockscout_checked({"id": "arc", "explorer": EXPLORER}, "addresses/0x1") == ({}, True)

    def timeout(*_a, **_k):
        raise http_mod.SourceError("https://explorer.arc.io/api/v2/addresses/0x1 -> ConnectTimeout")

    monkeypatch.setattr(onchain, "json_get", timeout)
    assert onchain.blockscout_checked({"id": "arc", "explorer": EXPLORER}, "addresses/0x1") == ({}, False)


def test_an_explorer_that_never_answered_is_not_written_down_as_no_launch(monkeypatch):
    """The flake this split exists for: the same pool answered twice and timed out once,
    and the timeout reached the operator as 未取到建池交易 - a finding about the token."""
    monkeypatch.setattr(onchain, "blockscout_checked", _blockscout({}, answered=False))

    target, is_contract_, name, reason = onchain.launch_path({"id": "arc"}, POOL)

    assert (target, is_contract_, name) == (None, None, "")
    assert reason == onchain.LAUNCH_EXPLORER
    assert "可重试" in onchain.LAUNCH_BASIS[reason]
    assert "可重试" not in onchain.LAUNCH_BASIS[onchain.LAUNCH_NO_TX]


def test_an_answered_explorer_with_nothing_is_a_real_no_launch_transaction(monkeypatch):
    monkeypatch.setattr(onchain, "blockscout_checked", _blockscout({}))

    assert onchain.launch_path({"id": "arc"}, POOL)[3] == onchain.LAUNCH_NO_TX


def test_a_node_that_did_not_answer_the_creation_tx_is_marked_retryable(monkeypatch):
    monkeypatch.setattr(onchain, "blockscout_checked", _blockscout({
        "addresses/%s" % POOL: {"creation_transaction_hash": "0xtx"},
    }))
    monkeypatch.setattr(onchain, "rpc", lambda *_a: None)

    assert onchain.launch_path({"id": "arc"}, POOL)[3] == onchain.LAUNCH_NODE


def test_lock_facts_keeps_the_two_unknowns_apart(monkeypatch):
    monkeypatch.setattr(onchain, "lp_holders", lambda _c, _p: [])
    monkeypatch.setattr(onchain, "nft_balances", lambda _c, managers, _o: {label: 0 for label in managers.values()})
    monkeypatch.setattr(onchain, "launch_path",
                        lambda _c, _p: (None, None, "", onchain.LAUNCH_EXPLORER))

    locked, basis, evidence = onchain.lock_facts(
        {"id": "arc"}, ca="0x" + "4" * 40, pair=POOL, creator="0x" + "5" * 40,
        launchpad=None)

    assert locked is None
    assert "可重试" in basis
    assert evidence["launch_reason"] == onchain.LAUNCH_EXPLORER


#: A node answers a too-wide `eth_getLogs` with a JSON-RPC error rather than a timeout.
#: Retrying it is the same question, refused the same way, so it must not be retried.
class _Answer:
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


class _Pacer:
    def wait(self):
        return None


def test_the_explorer_is_reached_on_the_browser_lane(monkeypatch):
    """Cloudflare reads the TLS fingerprint, so the same headers get 200 from curl and
    403 from httpx. A 403 is not an empty answer - it is no answer, and the difference is
    whether a token is written down as having no launch path."""
    captured = {}

    def fake(url, headers=None, **kw):
        captured.update(kw)
        captured['url'] = url
        return {"creation_transaction_hash": "0xdead"}

    monkeypatch.setattr(onchain, "json_get", fake)
    body, answered = onchain.blockscout_checked({"id": "arc", "explorer": "https://explorer.arc.io"}, "addresses/0x1")
    assert answered is True and body['creation_transaction_hash'] == '0xdead'
    assert captured["transport"] == onchain.BLOCKSCOUT_TRANSPORT == "browser"
    assert captured["url"] == "https://explorer.arc.io/api/v2/addresses/0x1"


def test_the_referer_is_the_explorer_of_the_chain_in_hand(monkeypatch):
    """A single hardcoded Arc referer used to be sent to whichever explorer the chain
    config named. A site that checks the referer refuses the whole sweep, and a refused
    sweep reads downstream as "this token is not in the explorer" - a wrong fact about the
    token rather than a header problem. Arc reading is unchanged, byte for byte."""
    seen = []

    def fake(url, headers=None, **kw):
        seen.append(headers)
        return {}

    monkeypatch.setattr(onchain, "json_get", fake)
    onchain.blockscout_checked(
        {"id": "arc", "explorer": "https://explorer.arc.io"}, "addresses/0x1")
    onchain.blockscout_checked(
        {"id": "robinhood", "explorer": "https://robinhoodchain.blockscout.com"},
        "addresses/0x1")
    assert seen[0]["Referer"] == "https://explorer.arc.io/"
    assert seen[1]["Referer"] == "https://robinhoodchain.blockscout.com/"


def test_a_rejected_query_is_not_asked_again(monkeypatch):
    import httpx

    calls = []

    def fake_post(*_args, **_kw):
        calls.append(1)
        return _Answer({"jsonrpc": "2.0", "error": {"code": -32602, "message": "query exceeds max block range 100000"}})

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(onchain, "pacer", lambda _url: _Pacer())
    chain = {"id": "arc", "rpc": "https://rpc.example"}
    assert onchain.rpc(chain, "eth_getLogs", [{}], tries=3) is None
    assert len(calls) == 1, "a refusal is an answer, not a flake"


def test_a_silent_node_is_still_retried(monkeypatch):
    import httpx

    calls = []

    def fake_post(*_args, **_kw):
        calls.append(1)
        raise RuntimeError('connection reset')

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(onchain, "pacer", lambda _url: _Pacer())
    chain = {"id": "arc", "rpc": "https://rpc.example"}
    assert onchain.rpc(chain, "eth_getLogs", [{}], tries=3) is None
    assert len(calls) == 3


def test_a_window_the_node_refuses_is_halved_until_it_fits(monkeypatch):
    """The node caps both the range and the number of results, and it names neither in a
    way a caller can plan for. So a refusal is read as "too wide" and the window is split,
    rather than being taken for a pool with no LP events."""
    widths = []

    def fake(_chain, _method, params):
        query = params[0]
        width = int(query['toBlock'], 16) - int(query['fromBlock'], 16)
        widths.append(width)
        if width > 5_000:
            return None, "query exceeds max block range 100000"
        return [{"topics": [onchain.POOL_MINT_TOPIC, "0x0", "0x" + "ab" * 32]}], ""

    monkeypatch.setattr(onchain, "rpc_checked", fake)
    logs = onchain._logs_in({}, '0xpool', 0, 20_000)
    # 20k refused, 10k refused, then four 5k windows that answer.
    assert widths == [20_000, 10_000, 5_000, 5_000, 10_000, 5_000, 5_000]
    assert len(logs) == 4


def test_the_lookback_is_read_in_windows_not_in_one_call(monkeypatch):
    spans = []

    def fake(_chain, method, params):
        if method == "eth_blockNumber":
            return hex(300_000), ""
        query = params[0]
        spans.append((int(query['fromBlock'], 16), int(query['toBlock'], 16)))
        return [], ""

    monkeypatch.setattr(onchain, "rpc_checked", fake)
    monkeypatch.setattr(onchain, "rpc", lambda *_a, **_k: hex(300_000))
    onchain.lp_holders({"id": "arc"}, '0x' + 'aa' * 20, blocks=50_000)
    assert spans == [(280_000, 300_000), (260_000, 280_000), (250_000, 260_000)]


def test_a_pool_without_an_address_is_not_asked_about(monkeypatch):
    monkeypatch.setattr(onchain, "rpc_checked", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no call")))
    assert onchain.lp_holders({}, None) == []
    assert onchain.lp_holders({}, '') == []


def test_a_v4_pool_id_is_not_a_pool_address_and_is_not_scanned(monkeypatch):
    """RadarDEX names most pools by a 32-byte id. `eth_getLogs` takes an address, so the
    node refuses every window - and a refusal that looks like "too wide" turns one
    question into thirty-one calls per window. Measured: 248 calls, ten minutes, per
    candidate, for a pool that has no logs to read at all."""
    monkeypatch.setattr(onchain, "rpc_checked", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no call")))
    assert onchain.lp_holders({"id": "arc"}, '0x' + 'ab' * 32) == []


def test_a_query_the_node_calls_invalid_is_not_halved(monkeypatch):
    """-32602 is a verdict on the shape of the query. Halving it asks a narrower question
    that is refused for the same reason, so the only thing splitting buys is calls."""
    calls = []

    def fake(*_args):
        calls.append(1)
        return None, ('Invalid params: Invalid variadic value or array type: data did not match '
                      'any variant of untagged enum Variadic')

    monkeypatch.setattr(onchain, "rpc_checked", fake)
    assert onchain._logs_in({}, '0xpool', 0, 20_000) == []
    assert len(calls) == 1


def test_the_node_s_own_words_survive_the_call(monkeypatch):
    import httpx

    def fake_post(*_args, **_kw):
        return _Answer({"jsonrpc": "2.0", "error": {"code": -32602, "message": "Invalid params"}})

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(onchain, "pacer", lambda _url: _Pacer())
    chain = {"id": "arc", "rpc": "https://rpc.example"}
    result, error = onchain.rpc_checked(chain, "eth_getLogs", [{}], tries=2)
    assert result is None and "Invalid params" in error


def test_the_node_is_counted_on_the_rounds_host_line(monkeypatch):
    """The node is asked over JSON-RPC POST, never through `get`, so it had no tally at
    all - the gate's slowest host was the one the log could not name."""
    import httpx

    from app.sources import http

    def fake_post(*_args, **_kw):
        return _Answer({"jsonrpc": "2.0", "result": "0x1"})

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(onchain, "pacer", lambda _url: _Pacer())
    http.stats(reset=True)

    onchain.rpc_checked({"id": "arc", "rpc": "https://rpc.example"}, "eth_blockNumber", [])

    row = http.stats()["rpc.example"]
    assert row["ok"] == 1 and row["fail"] == 0 and row["retry"] == 0
    assert any("rpc.example" in line for line in http.stats_lines())


def test_a_node_that_never_answers_is_a_retry_then_a_failure(monkeypatch):
    import httpx

    from app.sources import http

    def fake_post(*_args, **_kw):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(onchain, "pacer", lambda _url: _Pacer())
    http.stats(reset=True)

    assert onchain.rpc({"id": "arc", "rpc": "https://rpc.example"}, "eth_blockNumber", [], tries=2) is None

    row = http.stats()["rpc.example"]
    assert row["ok"] == 0 and row["retry"] == 2 and row["fail"] == 1

def test_the_two_manager_balances_go_out_in_one_round_trip(monkeypatch):
    """Two independent reads, one request: the node charges per request (measured 4.6s for
    two single calls against 3.5s batched), and neither manager answers for the other."""
    import httpx

    seen = []

    def fake_post(_url, json=None, **_kw):
        seen.append(json)
        # The node may answer in its own order; the caller must not depend on it.
        return _Answer([
            {"jsonrpc": "2.0", "id": entry["id"], "result": hex(entry["id"] + 1)}
            for entry in reversed(json)
        ])

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(onchain, "pacer", lambda _url: _Pacer())

    held = onchain.nft_balances(
        {"id": "arc", "rpc": "https://rpc.example"},
        {"0x" + "a" * 40: "v3", "0x" + "b" * 40: "v4"},
        "0x" + "5" * 40,
    )

    assert len(seen) == 1 and len(seen[0]) == 2
    assert held == {"v3": 1, "v4": 2}


def test_a_batch_the_node_refuses_is_retried_as_a_whole_and_then_given_up_on(monkeypatch):
    import httpx

    from app.sources import http

    def fake_post(*_a, **_kw):
        return _Answer({"jsonrpc": "2.0", "error": {"code": -32600, "message": "no batches"}})

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(onchain, "pacer", lambda _url: _Pacer())
    http.stats(reset=True)

    answers = onchain.rpc_batch(
        {"id": "arc", "rpc": "https://rpc.example"}, [("eth_call", [{}]), ("eth_call", [{}])],
        tries=2,
    )

    assert answers == [None, None]
    assert http.stats()["rpc.example"]["fail"] == 1, "a refused batch is one failed request"


def test_no_creator_means_no_question_asked(monkeypatch):
    """A token whose creator nobody knows must not turn into a read of every manager."""
    import httpx

    def boom(*_a, **_kw):
        raise AssertionError("no call")

    monkeypatch.setattr(httpx, "post", boom)
    held = onchain.nft_balances(
        {"id": "arc", "rpc": "https://rpc.example"}, {"0x" + "a" * 40: "v3"}, "",
    )
    assert held == {"v3": None}


def _lp(percent: float, **marks) -> dict:
    return {"address": "0x" + "a" * 40, "pct": percent, **marks}


def test_lp_in_a_contract_is_locked():
    """A v4 position lives in a manager, so the chain read has no pool contract to ask.
    The LP holder table answers the same question, in two states."""
    locked, basis = onchain.lp_lock_from_holders([
        _lp(99.96, contract=True), _lp(0.04),
    ])
    assert locked is True
    assert basis.startswith("GoPlus：LP 99.96% 在合约")


def test_lp_in_a_wallet_is_not_locked():
    locked, basis = onchain.lp_lock_from_holders([
        _lp(80.0), _lp(20.0, contract=True),
    ])
    assert locked is False
    assert basis.startswith("GoPlus：LP 80.00% 在钱包")


def test_a_burn_address_counts_as_out_of_reach():
    assert onchain.lp_lock_from_holders([_lp(70.0, burnt=True), _lp(30.0)])[0] is True


def test_a_table_covering_too_little_of_the_lp_says_nothing():
    """A truncated table is a missing answer, not an unlocked LP: 未判定 stays 未判定."""
    assert onchain.lp_lock_from_holders([_lp(12.0, contract=True)]) == (None, "")
    assert onchain.lp_lock_from_holders([]) == (None, "")
    assert onchain.lp_lock_from_holders(None) == (None, "")


def test_a_chain_without_a_node_is_an_error_not_arc():
    """Both helpers used to fall back to Arc's node. A chain file that forgot `rpc`
    would read Arc's chain and print the answer under its own name."""
    with pytest.raises(KeyError) as rpc_error:
        onchain.rpc_url({"id": "newchain"})
    assert "newchain" in str(rpc_error.value)
    with pytest.raises(KeyError) as explorer_error:
        onchain.explorer_url({"id": "newchain"})
    assert "newchain" in str(explorer_error.value)
