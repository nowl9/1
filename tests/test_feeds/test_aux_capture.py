"""Tests for feeds/aux_capture.py -- capture-only latency-analysis streams.

Covers the goal's acceptance points:
* Each new stream (spot / chainlink / pm5min) writes valid gzipped JSONL in
  the EXISTING recorder format with the outer ``ts`` present.
* The existing replay reader's k-way merge PARSES a widened recording (new
  source tags interleaved with deribit) without error and stays ts-ordered.
* Default capture (deribit / kalshi / polymarket) is BYTE-UNCHANGED -- the
  ``DataSource | str`` overload does not alter enum-source output.
* Unit coverage: Chainlink latestRoundData decode (incl. signed answer), the
  BTC 5-min discovery filter / Up-token resolution, and network-free
  discovery + book recording via an httpx mock transport.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import types
from datetime import datetime, timezone
from pathlib import Path

import httpx

from btc_pm_arb.feeds.aux_capture import (
    SOURCE_CHAINLINK,
    SOURCE_PM5MIN,
    SOURCE_SPOT,
    ChainlinkRoundCapture,
    DeribitIndexCapture,
    Polymarket5MinCapture,
    decode_latest_round_data,
    is_btc_5min_updown,
    resolve_up_token,
)
from btc_pm_arb.feeds.recorder import FrameRecorder
from btc_pm_arb.feeds.replay import ReplayReader
from btc_pm_arb.models import DataSource

_T0 = datetime(2026, 5, 31, 15, 0, 0, tzinfo=timezone.utc)


def _fixed_clock() -> datetime:
    return _T0


def _read_frames(path: Path) -> list[dict]:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return [json.loads(ln) for ln in f if ln.strip()]


# -- Stream 1: spot (Deribit index WS) ------------------------------------------


def test_spot_record_writes_existing_format(tmp_path: Path) -> None:
    """A spot WS frame lands as gzipped JSONL with the outer ts + 'spot' tag."""
    rec = FrameRecorder(tmp_path)
    cap = DeribitIndexCapture(rec, url="wss://example/ws", clock=_fixed_clock)
    raw = json.dumps(
        {
            "method": "subscription",
            "params": {
                "channel": "deribit_price_index.btc_usd",
                "data": {"index_name": "btc_usd", "price": 73570.86,
                         "timestamp": 1780239600000},
            },
        }
    )
    cap._record(raw)
    rec.close()

    path = tmp_path / SOURCE_SPOT / "2026-05-31" / "frames-15.jsonl.gz"
    assert path.exists()
    [line] = _read_frames(path)
    assert line["source"] == "spot"
    assert line["ts"] == _T0.isoformat()
    assert line["endpoint"] is None
    assert json.loads(line["frame"])["params"]["data"]["price"] == 73570.86


def test_spot_subscribe_payload_targets_index_channel() -> None:
    cap = DeribitIndexCapture(
        FrameRecorder("unused"), url="wss://x", index_name="btc_usd",
    )
    payload = json.loads(cap._subscribe_payload())
    assert payload["method"] == "public/subscribe"
    assert payload["params"]["channels"] == ["deribit_price_index.btc_usd"]


# -- Stream 2: chainlink (Polygon RPC eth_call) ----------------------------------


def test_decode_latest_round_data_synthetic() -> None:
    """Five ABI words decode to the right uint/int fields; answer is signed."""

    def _w(v: int) -> str:
        return f"{v & (2**256 - 1):064x}"

    round_id = (3 << 64) | 0x368AA0
    answer = 7_357_086_000_000  # BTC/USD * 1e8
    started, updated, answered = 1780239460, 1780239466, round_id
    raw = "0x" + _w(round_id) + _w(answer) + _w(started) + _w(updated) + _w(answered)
    out = decode_latest_round_data(raw)
    assert out["round_id"] == round_id
    assert out["answer"] == answer
    assert out["started_at"] == started
    assert out["updated_at"] == updated
    assert out["answered_in_round"] == answered
    assert out["decimals"] == 8


def test_decode_latest_round_data_signed_negative_answer() -> None:
    def _w(v: int) -> str:
        return f"{v & (2**256 - 1):064x}"

    raw = "0x" + _w(1) + _w(-5) + _w(0) + _w(0) + _w(1)
    out = decode_latest_round_data(raw)
    assert out["answer"] == -5


def test_decode_latest_round_data_short_result_raises() -> None:
    try:
        decode_latest_round_data("0xdeadbeef")
    except ValueError:
        return
    raise AssertionError("expected ValueError on short result")


def test_chainlink_record_writes_existing_format(tmp_path: Path) -> None:
    """A decoded round lands under 'chainlink' with updated_at preserved."""
    rec = FrameRecorder(tmp_path)
    cap = ChainlinkRoundCapture(
        rec, rpc_url="https://rpc", feed_address="0xfeed", clock=_fixed_clock,
    )
    decoded = {
        "round_id": 99, "answer": 7_357_086_000_000, "started_at": 1780239460,
        "updated_at": 1780239466, "answered_in_round": 99, "decimals": 8,
    }
    cap._record(decoded, "0xabc")
    rec.close()

    path = tmp_path / SOURCE_CHAINLINK / "2026-05-31" / "frames-15.jsonl.gz"
    [line] = _read_frames(path)
    assert line["source"] == "chainlink"
    assert line["ts"] == _T0.isoformat()
    assert line["endpoint"] == "latestRoundData"
    frame = json.loads(line["frame"])
    # The latency-critical push timestamp is captured, not just the price.
    assert frame["updated_at"] == 1780239466
    assert frame["answer"] == 7_357_086_000_000
    assert frame["feed_address"] == "0xfeed"
    assert frame["raw_result"] == "0xabc"


async def test_chainlink_poll_once_decodes_and_records(tmp_path: Path) -> None:
    """_poll_once eth_calls a mock RPC, decodes, and records one frame."""

    def _w(v: int) -> str:
        return f"{v & (2**256 - 1):064x}"

    result = "0x" + _w(1) + _w(7_000_000_000_000) + _w(10) + _w(20) + _w(1)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

    rec = FrameRecorder(tmp_path)
    cap = ChainlinkRoundCapture(
        rec, rpc_url="https://rpc.example", feed_address="0xfeed",
        clock=_fixed_clock,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await cap._poll_once(client)
    rec.close()

    path = tmp_path / SOURCE_CHAINLINK / "2026-05-31" / "frames-15.jsonl.gz"
    [line] = _read_frames(path)
    assert json.loads(line["frame"])["updated_at"] == 20


# -- Stream 3: pm5min (Polymarket 5-minute up/down odds) -------------------------


def _updown_market() -> dict:
    return {
        "slug": "btc-updown-5m-1780239600",
        "question": "Bitcoin Up or Down - May 31, 11:00AM-11:05AM ET",
        "active": True, "closed": False, "acceptingOrders": True,
        "endDate": "2026-05-31T15:05:00Z",
        "outcomes": '["Up", "Down"]',
        "clobTokenIds": '["111up", "222down"]',
    }


def test_is_btc_5min_updown_filter() -> None:
    assert is_btc_5min_updown(_updown_market()) is True
    # A threshold market (what the arb feed DOES track) is excluded here.
    assert is_btc_5min_updown(
        {"slug": "will-btc-hit-100k", "question": "Will Bitcoin reach $100,000?"}
    ) is False


def test_resolve_up_token_by_name() -> None:
    assert resolve_up_token(_updown_market()) == "111up"


def test_parse_discovery_tracks_updown_only() -> None:
    cap = Polymarket5MinCapture(
        FrameRecorder("unused"), gamma_url="https://g", clob_url="https://c",
    )
    body = [
        _updown_market(),
        {"slug": "will-btc-hit-100k", "question": "Will Bitcoin reach $100,000?",
         "outcomes": '["Yes","No"]', "clobTokenIds": '["a","b"]'},
    ]
    tracked = cap._parse_discovery(body)
    assert list(tracked) == ["111up"]


async def test_pm5min_discover_and_poll_records(tmp_path: Path) -> None:
    """Network-free: mock gamma + clob, assert pm5min discovery + book frames."""
    book_body = {"asks": [{"price": "0.55", "size": "100"}],
                 "bids": [{"price": "0.45", "size": "100"}]}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/markets":
            return httpx.Response(200, json=[_updown_market()])
        if request.url.path == "/book":
            return httpx.Response(200, json=book_body)
        return httpx.Response(404)

    rec = FrameRecorder(tmp_path)
    cap = Polymarket5MinCapture(
        rec, gamma_url="https://gamma.example", clob_url="https://clob.example",
        clock=_fixed_clock,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await cap._discover(client)
        await cap._poll_books(client)
    rec.close()

    assert cap._tracked == {"111up": _updown_market()}
    path = tmp_path / SOURCE_PM5MIN / "2026-05-31" / "frames-15.jsonl.gz"
    frames = _read_frames(path)
    # First frame: the discovery /markets body; second: the /book poll.
    assert all(fr["source"] == "pm5min" for fr in frames)
    assert all(fr["ts"] == _T0.isoformat() for fr in frames)
    endpoints = [fr["endpoint"] for fr in frames]
    assert any(e.startswith("/markets") for e in endpoints)
    assert any(e == "/book?token_id=111up" for e in endpoints)


# -- Replay reader parses the widened recording (k-way merge unbroken) -----------


def test_replay_merge_parses_new_source_tags(tmp_path: Path) -> None:
    """A widened recording (deribit + spot + chainlink + pm5min) merges
    ts-ordered without error; unknown tags fall to source-order 99."""
    rec = FrameRecorder(tmp_path)
    # Interleave timestamps so the merge has to order across sources.
    rec.record(DataSource.DERIBIT, "d0", _T0)
    rec.record(SOURCE_SPOT, "s0", _T0.replace(second=1))
    rec.record(SOURCE_CHAINLINK, {"updated_at": 1}, _T0.replace(second=2),
               endpoint="latestRoundData")
    rec.record(SOURCE_PM5MIN, b'{"asks":[]}', _T0.replace(second=3),
               endpoint="/book?token_id=111up")
    rec.record(DataSource.DERIBIT, "d1", _T0.replace(second=4))
    rec.close()

    stub_agent = types.SimpleNamespace(
        clock=types.SimpleNamespace(mode="replay")
    )
    reader = ReplayReader(
        record_dir=tmp_path,
        date="2026-05-31",
        agent=stub_agent,
        sources=("deribit", "spot", "chainlink", "pm5min"),
        jump_to_expiry=False,
    )
    merged = list(reader._merged_frames())
    # All five frames are parsed, in ts order, across the four source tags.
    assert len(merged) == 5
    tss = [item[0] for item in merged]
    assert tss == sorted(tss)
    seen_sources = {item[2] for item in merged}
    assert seen_sources == {"deribit", "spot", "chainlink", "pm5min"}


# -- Default capture is byte-unchanged by the DataSource|str overload ------------


def test_datasource_record_is_byte_identical(tmp_path: Path) -> None:
    """Recording with a DataSource enum produces the exact same on-disk line
    as before the str overload existed -- the existing three feeds' recordings
    are unaffected when the new streams are off/unavailable."""
    rec = FrameRecorder(tmp_path)
    rec.record(DataSource.DERIBIT, "ws-frame", _T0, endpoint=None)
    rec.close()

    path = tmp_path / "deribit" / "2026-05-31" / "frames-15.jsonl.gz"
    with gzip.open(path, "rt", encoding="utf-8") as f:
        raw_line = f.read()
    expected = json.dumps(
        {"ts": _T0.isoformat(), "source": "deribit", "endpoint": None,
         "frame": "ws-frame"}
    ) + "\n"
    assert raw_line == expected
