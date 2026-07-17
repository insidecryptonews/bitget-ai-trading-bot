"""Adversarial contracts for CROSS-VENUE INTELLIGENCE (research-only)."""

from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path

import pytest

from app.labs.cross_venue import ACCOUNT_ID, safety_envelope
from app.labs.cross_venue import adapters as A
from app.labs.cross_venue import storage as S
from app.labs.cross_venue import service as SV
from app.labs.cross_venue.api import READERS
from app.labs.cross_venue import api as CVAPI
from app.labs.cross_venue.cli import build_parser
from app.labs.cross_venue.collector import collect_session, run_collector
from app.labs.cross_venue.leadlag import LeadLagEngine
from app.labs.cross_venue.ledger import CrossVenueLedger
from app.labs.cross_venue.leverage import LeverageLab, simulate_trade
from app.labs.cross_venue.models import CanonicalEvent, comparable_to_bitget
from app.labs.cross_venue.paper import PaperSimulator, choose_bar_exit
from app.labs.cross_venue.providers import (
    PUBLIC_WS_ENDPOINTS, assert_public_ws_url, load_config, load_inventory,
)


CLOCK = ("2026-07-17T00:00:00+00:00", 1_752_710_400_000, 1_000_000_000)


def _event(venue: str, mono: int, price: float, *, symbol: str = "BTCUSDT", bid=None, ask=None,
           product="LINEAR_PERPETUAL", quote="USDT") -> dict:
    return {
        "venue": venue, "symbol": symbol, "canonical_symbol": symbol,
        "product_type": product, "quote_asset": quote, "event_type": "book_l1" if bid else "trade",
        "exchange_event_ts": 1000, "exchange_publish_ts": 1000,
        "local_receive_wall_ts": "2026-07-17T00:00:00+00:00",
        "local_receive_wall_ms": 1_752_710_400_000,
        "local_receive_monotonic_ns": mono, "price": price,
        "best_bid": bid, "best_ask": ask, "bid_size": 10 if bid else None,
        "ask_size": 10 if ask else None, "source_status": "OK",
    }


def test_safety_envelope_is_immutable_in_intent():
    value = safety_envelope()
    assert value["mode"] == "RESEARCH_PAPER_ONLY"
    assert value["paper_trading"] is True
    assert value["live_trading"] is False
    assert value["paper_filter_enabled"] is False
    assert value["can_send_real_orders"] is False
    assert value["uses_private_endpoints"] is False
    assert value["edge_validated"] is False
    assert value["final_recommendation"] == "NO LIVE"


def test_provider_inventory_has_official_provenance_and_unique_ids():
    inventory = load_inventory(); providers = inventory["providers"]
    assert len(providers) >= 7
    assert len({row["provider_id"] for row in providers}) == len(providers)
    assert {"bitget", "binance", "bybit", "okx", "hyperliquid"} <= {row["provider_id"] for row in providers}
    for row in providers:
        assert row["official_docs_url"].startswith("https://")
        assert row["last_verified_at"]
        assert row["commercial_use_status"] in {"NEEDS_MANUAL_TERMS_REVIEW"}
    hyper = next(row for row in providers if row["provider_id"] == "hyperliquid")
    assert "OBSERVATION_ONLY" in hyper["rejection_reason"]


def test_config_refuses_operational_flags(tmp_path):
    config = load_config(); assert config["can_send_real_orders"] is False
    bad = dict(config); bad["can_send_real_orders"] = True
    path = tmp_path / "bad.json"; path.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ValueError, match="UNSAFE_CONFIG"):
        load_config(path)


def test_config_refuses_invalid_cost_or_venue_contract(tmp_path):
    config=dict(load_config()); config["round_trip_taker_fee_bps"]=-1
    path=tmp_path/"bad-cost.json"; path.write_text(json.dumps(config),encoding="utf-8")
    with pytest.raises(ValueError,match="NUMERIC_INVALID"):
        load_config(path)
    config=dict(load_config()); config["signal_eligible_venues"]=["not-active"]
    path=tmp_path/"bad-venue.json"; path.write_text(json.dumps(config),encoding="utf-8")
    with pytest.raises(ValueError,match="SIGNAL_VENUE"):
        load_config(path)


@pytest.mark.parametrize("venue,url", PUBLIC_WS_ENDPOINTS.items())
def test_only_exact_public_websocket_urls_are_allowed(venue, url):
    assert assert_public_ws_url(venue, url) == url


@pytest.mark.parametrize("url", [
    "wss://stream.bybit.com/v5/private", "wss://fstream.binance.com/ws/order",
    "ws://ws.bitget.com/v2/ws/public", "wss://evil.example/ws",
    "wss://ws.okx.com:8443/ws/v5/private",
])
def test_private_or_unallowlisted_websocket_urls_are_blocked(url):
    with pytest.raises(ValueError):
        assert_public_ws_url("bybit" if "bybit" in url else "okx", url)


def test_sensitive_query_parameters_are_blocked():
    with pytest.raises(ValueError, match="SENSITIVE_QUERY"):
        assert_public_ws_url("binance", PUBLIC_WS_ENDPOINTS["binance"] + "?signature=fake")
    assert assert_public_ws_url("binance", PUBLIC_WS_ENDPOINTS["binance"] + "?streams=btcusdt@aggTrade")


def test_canonical_event_rejects_non_finite_and_crossed_book():
    base = dict(venue="x", symbol="BTCUSDT", canonical_symbol="BTCUSDT", product_type="LINEAR_PERPETUAL",
                quote_asset="USDT", event_type="trade", exchange_event_ts=1, exchange_publish_ts=1,
                local_receive_wall_ts=CLOCK[0], local_receive_wall_ms=CLOCK[1], local_receive_monotonic_ns=CLOCK[2])
    with pytest.raises(ValueError, match="NON_FINITE"):
        CanonicalEvent(**base, price=math.nan).to_dict()
    with pytest.raises(ValueError, match="CROSSED_BOOK"):
        CanonicalEvent(**base, best_bid=101, best_ask=100).to_dict()


def test_event_identity_survives_reconnect_when_source_identity_exists():
    base = dict(
        venue="bybit", symbol="BTCUSDT", canonical_symbol="BTCUSDT",
        product_type="LINEAR_PERPETUAL", quote_asset="USDT", event_type="trade",
        exchange_event_ts=10, exchange_publish_ts=11, local_receive_wall_ts=CLOCK[0],
        local_receive_wall_ms=CLOCK[1], local_receive_monotonic_ns=CLOCK[2],
        trade_id="trade-7", price=100, size=1,
    )
    first = CanonicalEvent(**base, connection_id="bybit_a").to_dict()
    second = CanonicalEvent(**base, connection_id="bybit_b").to_dict()
    assert first["event_id"] == second["event_id"]


def test_contract_equivalence_excludes_hyperliquid_until_basis_normalized():
    assert comparable_to_bitget(_event("binance", 1, 100))
    assert not comparable_to_bitget(_event("hyperliquid", 1, 100, product="OTHER_PERPETUAL", quote="USD"))


def test_bitget_normalization_trade_book_and_ticker():
    adapter = A.BitgetAdapter(["BTCUSDT"])
    trade = adapter.normalize({"arg":{"channel":"trade","instId":"BTCUSDT"},"data":[{"ts":"10","price":"100","size":"2","side":"buy","tradeId":"t"}]}, clock=CLOCK)[0]
    book = adapter.normalize({"arg":{"channel":"books1","instId":"BTCUSDT"},"action":"snapshot","data":[{"ts":"11","bids":[["99","3"]],"asks":[["101","4"]],"seq":"2"}]}, clock=CLOCK)[0]
    ticker = adapter.normalize({"arg":{"channel":"ticker","instId":"BTCUSDT"},"data":[{"ts":"12","lastPr":"100","markPrice":"100.1","indexPrice":"99.9","fundingRate":"0.0001"}]}, clock=CLOCK)[0]
    assert (trade["taker_side"], trade["trade_id"]) == ("BUY", "t")
    assert (book["best_bid"], book["best_ask"]) == (99.0, 101.0)
    assert ticker["funding_rate"] == pytest.approx(0.0001)


def test_binance_aggressor_side_and_l1_normalization():
    adapter = A.BinanceAdapter(["BTCUSDT"])
    sell = adapter.normalize({"data":{"e":"aggTrade","E":20,"T":19,"s":"BTCUSDT","a":7,"p":"100","q":"1","m":True}}, clock=CLOCK)[0]
    buy = adapter.normalize({"data":{"e":"aggTrade","E":20,"T":19,"s":"BTCUSDT","a":8,"p":"100","q":"1","m":False}}, clock=CLOCK)[0]
    book = adapter.normalize({"data":{"e":"bookTicker","E":21,"s":"BTCUSDT","u":9,"b":"99","B":"2","a":"101","A":"3"}}, clock=CLOCK)[0]
    assert sell["taker_side"] == "SELL" and buy["taker_side"] == "BUY"
    assert book["snapshot_kind"] == "ABSOLUTE_L1"


def test_bybit_okx_and_hyperliquid_normalization():
    bybit = A.BybitAdapter(["BTCUSDT"]).normalize({"topic":"orderbook.1.BTCUSDT","type":"snapshot","ts":30,"data":{"s":"BTCUSDT","b":[["99","2"]],"a":[["101","3"]],"u":1,"seq":2,"cts":29}}, clock=CLOCK)[0]
    okx = A.OkxAdapter(["BTCUSDT"]).normalize({"arg":{"channel":"trades","instId":"BTC-USDT-SWAP"},"data":[{"ts":"40","px":"100","sz":"1","side":"sell","tradeId":"x"}]}, clock=CLOCK)[0]
    hyper = A.HyperliquidAdapter(["BTCUSDT"]).normalize({"channel":"trades","data":[{"coin":"BTC","side":"B","px":"100","sz":"1","time":50,"tid":"h"}]}, clock=CLOCK)[0]
    assert bybit["sequence_id"] == "2" and bybit["best_bid"] == 99
    assert okx["canonical_symbol"] == "BTCUSDT" and okx["taker_side"] == "SELL"
    assert hyper["canonical_symbol"] == "BTCUSDT" and hyper["quote_asset"] == "USD"
    assert hyper["product_type"] == "OTHER_PERPETUAL"


def test_sequence_regression_is_visible_without_inventing_strict_gap_math():
    adapter=A.BybitAdapter(["BTCUSDT"])
    first={"topic":"orderbook.1.BTCUSDT","type":"snapshot","ts":30,"data":{"s":"BTCUSDT","b":[["99","2"]],"a":[["101","3"]],"u":10,"seq":10,"cts":29}}
    second={"topic":"orderbook.1.BTCUSDT","type":"delta","ts":31,"data":{"s":"BTCUSDT","b":[["99","2"]],"a":[["101","3"]],"u":9,"seq":9,"cts":30}}
    adapter.normalize(first,clock=CLOCK)
    row=adapter.normalize(second,clock=(CLOCK[0],CLOCK[1]+1,CLOCK[2]+1))[0]
    assert row["source_status"] == "SEQUENCE_REGRESSION_OBSERVED"
    assert adapter.health(now_monotonic_ns=CLOCK[2]+1)["sequence_regressions"] == 1


def test_adapter_contract_has_no_auth_and_only_public_subscriptions():
    for venue in ("bitget", "binance", "bybit", "okx", "hyperliquid"):
        adapter = A.make_adapter(venue, ["BTCUSDT", "ETHUSDT"])
        assert adapter.capabilities()["authentication"] is False
        encoded = json.dumps(adapter.subscription_messages()).lower()
        assert all(token not in encoded for token in ("api_key", "signature", "withdraw", "transfer", "account"))


def test_remote_frame_size_and_non_finite_json_are_blocked():
    class Socket:
        def __init__(self, value): self.value=value
        def recv(self): return self.value
    adapter=A.BybitAdapter(["BTCUSDT"]); adapter._socket=Socket("x"*20); adapter.max_message_bytes=10
    with pytest.raises(ValueError,match="FRAME_SIZE"):
        adapter.receive()
    adapter._socket=Socket('{"value":NaN}'); adapter.max_message_bytes=100
    with pytest.raises(ValueError,match="NON_FINITE"):
        adapter.receive()


def test_bitget_public_application_heartbeat_is_sent_without_auth():
    class Socket:
        def __init__(self): self.sent=[]
        def send(self, value): self.sent.append(value)
        def recv(self): return "pong"

    adapter=A.BitgetAdapter(["BTCUSDT"]); socket=Socket()
    adapter._socket=socket; adapter.connected=True
    adapter._last_application_heartbeat_ns=1
    frame=adapter.receive()
    assert frame == {"control": "pong"}
    assert socket.sent == ["ping"]
    health=adapter.health()
    assert health["application_heartbeat_interval_seconds"] == 25.0
    assert health["application_heartbeats_sent"] == 1
    assert A.BinanceAdapter(["BTCUSDT"]).application_heartbeat_payload is None


def test_collector_rejects_symbols_outside_versioned_allowlist_before_network():
    with pytest.raises(ValueError,match="SYMBOL_OUTSIDE_ALLOWLIST"):
        run_collector("bybit",symbols=["UNKNOWNUSDT"],max_sessions=1,max_messages=1)


def _isolated_store(tmp_path, monkeypatch, venue="bybit"):
    root = tmp_path / "external_data" / "staging" / "cross_venue_v1"
    monkeypatch.setattr(S, "STAGING_ROOT", root)
    store = S.StreamStore(venue, root); store.open(); return store


def test_append_only_store_deduplicates_and_writes_manifest(tmp_path, monkeypatch):
    store = _isolated_store(tmp_path, monkeypatch)
    row = A.BybitAdapter(["BTCUSDT"]).normalize({"topic":"publicTrade.BTCUSDT","ts":2,"data":[{"T":1,"s":"BTCUSDT","S":"Buy","v":"1","p":"100","i":"t"}]}, clock=CLOCK)[0]
    assert store.append_events([row]) == 1
    assert store.append_events([row]) == 0
    store.write_health({"status":"HEALTHY"}); store.close()
    assert store.stream_path.read_text(encoding="utf-8").count("\n") == 1
    manifest = json.loads(store.manifest_path.read_text(encoding="utf-8"))
    assert manifest["append_only"] is True and manifest["last_hash"]


def test_capacity_guard_fails_before_write_without_poisoning_dedup(tmp_path, monkeypatch):
    store=_isolated_store(tmp_path,monkeypatch)
    row=A.BybitAdapter(["BTCUSDT"]).normalize({"topic":"publicTrade.BTCUSDT","ts":2,"data":[{"T":1,"s":"BTCUSDT","S":"Buy","v":"1","p":"100","i":"cap"}]},clock=CLOCK)[0]
    store.maximum_stream_bytes=1
    with pytest.raises(RuntimeError,match="STREAM_SIZE_GUARD"):
        store.append_events([row])
    store.maximum_stream_bytes=1_000_000
    assert store.append_events([row]) == 1
    store.close()


def test_writer_lease_blocks_second_process_contract(tmp_path, monkeypatch):
    root=tmp_path/"external_data"/"staging"/"cross_venue_v1"; monkeypatch.setattr(S,"STAGING_ROOT",root)
    first=S.StreamStore("bybit",root); second=S.StreamStore("bybit",root)
    first.open()
    try:
        with pytest.raises(RuntimeError,match="WRITER_ALREADY_ACTIVE"):
            second.open()
    finally:
        first.lease.path.unlink(missing_ok=True); first.close()


def test_collector_session_uses_public_socket_and_real_frame(tmp_path, monkeypatch):
    store=_isolated_store(tmp_path,monkeypatch,"bitget")
    frame={"arg":{"channel":"trade","instId":"BTCUSDT"},"data":[{"ts":"10","price":"100","size":"1","side":"buy","tradeId":"t"}]}
    class Socket:
        def __init__(self): self.sent=[]; self.frames=[json.dumps(frame)]
        def send(self,value): self.sent.append(value)
        def recv(self): return self.frames.pop(0)
        def close(self): pass
    socket=Socket(); calls=[]
    def connector(url,**kwargs): calls.append((url,kwargs)); return socket
    try:
        result=collect_session(A.BitgetAdapter(["BTCUSDT"]),store,connector=connector,max_messages=1)
    finally: store.close()
    assert result["normalized_events"]==1 and result["raw_frames"]==1
    assert calls[0][0]==PUBLIC_WS_ENDPOINTS["bitget"] and calls[0][1]["header"]==[]
    assert socket.sent and "subscribe" in socket.sent[0]
    verification=json.loads((store.venue_root/"session_verification.json").read_text(encoding="utf-8"))
    assert verification["dns_tls_websocket_upgrade"] == "MOCK_OR_INJECTED_CONNECTOR"
    assert verification["result"] == "TEST_CONNECTOR_ONLY"
    assert verification["exchange_clock_used_for_leadership"] is False


def test_stream_reader_waits_on_partial_line(tmp_path):
    path = tmp_path / "x.jsonl"; path.write_bytes(b'{"ok":true}\n{"partial":')
    rows, offset, error = S.read_new_jsonl(path, 0)
    assert rows == [{"ok": True}] and error == "PARTIAL_LINE_WAITING"
    assert offset == len(b'{"ok":true}\n')


def test_atomic_json_retries_transient_windows_replace_denial(tmp_path, monkeypatch):
    path = tmp_path / "health.json"
    real_replace = S.os.replace
    attempts = []

    def flaky_replace(source, target):
        attempts.append((source, target))
        if len(attempts) < 3:
            raise PermissionError("transient reader lock")
        return real_replace(source, target)

    monkeypatch.setattr(S.os, "replace", flaky_replace)
    S.atomic_json(path, {"status": "HEALTHY"})
    assert json.loads(path.read_text(encoding="utf-8")) == {"status": "HEALTHY"}
    assert len(attempts) == 3
    assert not list(tmp_path.glob("*.tmp"))


def test_staging_path_is_exact_not_marker_substring(tmp_path, monkeypatch):
    expected = tmp_path / "external_data" / "staging" / "cross_venue_v1"
    monkeypatch.setattr(S, "STAGING_ROOT", expected)
    assert S.safe_staging_root(expected) == expected
    with pytest.raises(ValueError, match="OUTSIDE_ALLOWLIST"):
        S.safe_staging_root(tmp_path / "reports" / "cross_venue_v1")
    with pytest.raises(ValueError, match="OUTSIDE_ALLOWLIST"):
        S.safe_staging_root(Path(str(expected) + "_evil"))


def _leadlag_fixture():
    config = dict(load_config()); config.update(minimum_leader_move_bps=4, minimum_net_edge_bps=1)
    engine = LeadLagEngine(config); t = 1_000_000_000
    engine.process(_event("bitget", t, 100, bid=99.99, ask=100.01))
    engine.process(_event("binance", t, 100, bid=99.99, ask=100.01))
    engine.process(_event("bybit", t, 100, bid=99.99, ask=100.01))
    engine.process(_event("bitget", t + 400_000_000, 100, bid=99.99, ask=100.01))
    engine.process(_event("binance", t + 500_000_000, 100.40, bid=100.39, ask=100.41))
    result = engine.process(_event("bybit", t + 510_000_000, 100.35, bid=100.34, ask=100.36))
    return engine, result, t


def test_leadlag_consensus_is_causal_and_cost_aware():
    engine, result, t = _leadlag_fixture(); signal = result["signal"]
    assert signal["status"] == "CANDIDATE_RESEARCH_ONLY"
    assert signal["decision_monotonic_ns"] == t + 510_000_000
    assert signal["first_lead_event_monotonic_ns"] <= signal["decision_monotonic_ns"]
    assert signal["unlevered_net_edge_bps"] == pytest.approx(
        signal["expected_remaining_move_bps"] - signal["estimated_total_cost_bps"]
    )
    assert signal["features"]["ordering_clock"] == "LOCAL_MONOTONIC_RECEIVE"
    assert signal["edge_validated"] is False
    assert signal["first_lead_event_ts"]
    assert signal["code_commit"]


def test_leadlag_outcome_requires_future_target_receive_event():
    engine, result, t = _leadlag_fixture(); signal = result["signal"]
    before = engine.process(_event("bitget", signal["decision_monotonic_ns"] + 500_000_000, 100.1, bid=100.09, ask=100.11))
    after = engine.process(_event("bitget", signal["decision_monotonic_ns"] + 1_000_000_001, 100.2, bid=100.19, ask=100.21))
    assert before["outcomes"] == []
    assert after["outcomes"][0]["counterfactual_outcome_only"] is True
    assert after["outcomes"][0]["no_lookahead_status"] == "OK_FORWARD_ONLY"


def test_service_globally_sorts_receive_clock_and_drops_late_batches(tmp_path, monkeypatch):
    root=tmp_path/"external_data"/"staging"/"cross_venue_v1"; monkeypatch.setattr(S,"STAGING_ROOT",root)
    runtime=tmp_path/"runtime"; monkeypatch.setattr(SV,"RUNTIME_ROOT",runtime)
    monkeypatch.setattr(SV,"OFFSETS_PATH",runtime/"offsets.json")
    monkeypatch.setattr(SV,"ENGINE_STATUS_PATH",runtime/"status.json")
    monkeypatch.setattr(SV,"ENGINE_SNAPSHOT_PATH",runtime/"snapshot.json")
    config=dict(load_config()); config["active_venues"]=["bitget","binance","bybit"]
    initial=[_event("bitget",300,100,bid=99.99,ask=100.01),_event("binance",100,100),_event("bybit",200,100)]
    for row in initial:
        path=root/row["venue"]/"normalized"/"current.jsonl"; path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(row)+"\n",encoding="utf-8")
    service=SV.CrossVenueService(
        config=config, root=root, ledger=CrossVenueLedger(runtime/"paper.sqlite"),
        bootstrap_existing=True,
    )
    seen=[]; original=service.engine.process
    service.engine.process=lambda row:(seen.append(row["local_receive_monotonic_ns"]) or original(row))
    service.cycle(); assert seen==[100,200,300]
    late=_event("binance",250,101); fresh=_event("binance",400,102)
    with (root/"binance"/"normalized"/"current.jsonl").open("a",encoding="utf-8") as handle:
        handle.write(json.dumps(late)+"\n"+json.dumps(fresh)+"\n")
    service.cycle(); assert seen[-1]==400 and 250 not in seen
    assert service.late_events_dropped==1


def test_service_does_not_overtake_unread_busy_venue_backlog(tmp_path, monkeypatch):
    root=tmp_path/"external_data"/"staging"/"cross_venue_v1"; monkeypatch.setattr(S,"STAGING_ROOT",root)
    runtime=tmp_path/"runtime"; monkeypatch.setattr(SV,"RUNTIME_ROOT",runtime)
    monkeypatch.setattr(SV,"OFFSETS_PATH",runtime/"offsets.json")
    monkeypatch.setattr(SV,"ENGINE_STATUS_PATH",runtime/"status.json")
    monkeypatch.setattr(SV,"ENGINE_SNAPSHOT_PATH",runtime/"snapshot.json")
    config=dict(load_config()); config["active_venues"]=["bitget","binance"]
    rows={
        "binance": [_event("binance",mono,100+mono/1000,bid=99.99,ask=100.01) for mono in (100,200,300,400)],
        "bitget": [_event("bitget",350,100,bid=99.99,ask=100.01)],
    }
    for venue, events in rows.items():
        path=root/venue/"normalized"/"current.jsonl"; path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text("".join(json.dumps(row)+"\n" for row in events),encoding="utf-8")
    service=SV.CrossVenueService(
        config=config,root=root,ledger=CrossVenueLedger(runtime/"paper.sqlite"),bootstrap_existing=True,
    )
    seen=[]; original=service.engine.process
    service.engine.process=lambda row:(seen.append(row["local_receive_monotonic_ns"]) or original(row))
    service.cycle(max_rows_per_venue=2)
    assert seen == [100,200]
    service.cycle(max_rows_per_venue=2)
    service.cycle(max_rows_per_venue=2)
    assert seen == [100,200,300,350,400]
    assert service.late_events_dropped == 0


def test_service_reorder_buffer_holds_recent_rows_without_advancing_offset(tmp_path, monkeypatch):
    root=tmp_path/"external_data"/"staging"/"cross_venue_v1"; monkeypatch.setattr(S,"STAGING_ROOT",root)
    runtime=tmp_path/"runtime"; monkeypatch.setattr(SV,"RUNTIME_ROOT",runtime)
    monkeypatch.setattr(SV,"OFFSETS_PATH",runtime/"offsets.json")
    monkeypatch.setattr(SV,"ENGINE_STATUS_PATH",runtime/"status.json")
    monkeypatch.setattr(SV,"ENGINE_SNAPSHOT_PATH",runtime/"snapshot.json")
    clock={"now":1_000_000_000}; monkeypatch.setattr(SV.time,"monotonic_ns",lambda:clock["now"])
    config=dict(load_config()); config["active_venues"]=["bitget"]; config["causal_reorder_buffer_ms"]=250
    path=root/"bitget"/"normalized"/"current.jsonl"; path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(_event("bitget",900_000_000,100,bid=99.99,ask=100.01))+"\n",encoding="utf-8")
    service=SV.CrossVenueService(
        config=config,root=root,ledger=CrossVenueLedger(runtime/"paper.sqlite"),bootstrap_existing=True,
    )
    assert service.cycle()["cycle_events"] == 0
    assert service.offsets["bitget"] == 0
    clock["now"] = 1_200_000_000
    assert service.cycle()["cycle_events"] == 1
    assert service.offsets["bitget"] == path.stat().st_size


def test_rejected_observations_do_not_make_leadlag_signal_healthy(tmp_path, monkeypatch):
    root=tmp_path/"external_data"/"staging"/"cross_venue_v1"; monkeypatch.setattr(S,"STAGING_ROOT",root)
    runtime=tmp_path/"runtime"; monkeypatch.setattr(SV,"RUNTIME_ROOT",runtime)
    monkeypatch.setattr(SV,"OFFSETS_PATH",runtime/"offsets.json")
    monkeypatch.setattr(SV,"ENGINE_STATUS_PATH",runtime/"status.json")
    monkeypatch.setattr(SV,"ENGINE_SNAPSHOT_PATH",runtime/"snapshot.json")
    service=SV.CrossVenueService(root=root,ledger=CrossVenueLedger(runtime/"paper.sqlite"))
    service.observations_recorded=100; service.signals_recorded=0
    component=service._health(service.engine.snapshot())["components"]["CROSS_VENUE_LEADLAG"]
    assert component["status"] == "WAITING_FOR_SIGNAL"
    assert component["observations_recorded"] == 100
    assert component["candidate_signals_recorded"] == 0


def test_service_skips_paper_ledger_hot_path_without_pending_work(tmp_path, monkeypatch):
    root=tmp_path/"external_data"/"staging"/"cross_venue_v1"; monkeypatch.setattr(S,"STAGING_ROOT",root)
    runtime=tmp_path/"runtime"; monkeypatch.setattr(SV,"RUNTIME_ROOT",runtime)
    monkeypatch.setattr(SV,"OFFSETS_PATH",runtime/"offsets.json")
    monkeypatch.setattr(SV,"ENGINE_STATUS_PATH",runtime/"status.json")
    monkeypatch.setattr(SV,"ENGINE_SNAPSHOT_PATH",runtime/"snapshot.json")
    config=dict(load_config()); config["active_venues"]=["bitget"]
    path=root/"bitget"/"normalized"/"current.jsonl"; path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text("".join(
        json.dumps(_event("bitget",mono,100,bid=99.99,ask=100.01))+"\n"
        for mono in range(1,101)
    ),encoding="utf-8")
    service=SV.CrossVenueService(
        config=config,root=root,ledger=CrossVenueLedger(runtime/"paper.sqlite"),bootstrap_existing=True,
    )
    quote_calls=[]
    monkeypatch.setattr(service.paper,"on_bitget_quote",lambda event: quote_calls.append(event) or {"opened":[],"closed":[]})
    result=service.cycle()
    assert result["cycle_events"] == 100
    assert quote_calls == []


def test_productive_service_freezes_existing_stream_as_forward_boundary(tmp_path, monkeypatch):
    root=tmp_path/"external_data"/"staging"/"cross_venue_v1"; monkeypatch.setattr(S,"STAGING_ROOT",root)
    runtime=tmp_path/"runtime"; monkeypatch.setattr(SV,"RUNTIME_ROOT",runtime)
    monkeypatch.setattr(SV,"OFFSETS_PATH",runtime/"offsets.json")
    monkeypatch.setattr(SV,"ENGINE_STATUS_PATH",runtime/"status.json")
    monkeypatch.setattr(SV,"ENGINE_SNAPSHOT_PATH",runtime/"snapshot.json")
    config=dict(load_config()); config["active_venues"]=["bitget"]
    stream=root/"bitget"/"normalized"/"current.jsonl"; stream.parent.mkdir(parents=True)
    historical=_event("bitget",100,100,bid=99.99,ask=100.01)
    stream.write_text(json.dumps(historical)+"\n",encoding="utf-8")
    service=SV.CrossVenueService(config=config,root=root,ledger=CrossVenueLedger(runtime/"paper.sqlite"))
    assert service.cycle()["cycle_events"]==0
    forward=_event("bitget",200,101,bid=100.99,ask=101.01)
    with stream.open("a",encoding="utf-8") as handle:
        handle.write(json.dumps(forward)+"\n")
    result=service.cycle()
    assert result["cycle_events"]==1
    assert result["forward_boundary"]["historical_rows_eligible_for_paper"] is False
    assert result["forward_boundary"]["boundary_mode"] == "FROZEN_AT_CURRENT_STREAM_END"


def test_stale_collectors_cannot_be_aggregated_as_paper_research(tmp_path, monkeypatch):
    root=tmp_path/"external_data"/"staging"/"cross_venue_v1"; monkeypatch.setattr(S,"STAGING_ROOT",root)
    runtime=tmp_path/"runtime"; monkeypatch.setattr(SV,"RUNTIME_ROOT",runtime)
    monkeypatch.setattr(SV,"OFFSETS_PATH",runtime/"offsets.json")
    monkeypatch.setattr(SV,"ENGINE_STATUS_PATH",runtime/"status.json")
    monkeypatch.setattr(SV,"ENGINE_SNAPSHOT_PATH",runtime/"snapshot.json")
    monkeypatch.setattr(SV,"collector_health",lambda venue,**kwargs:{"venue":venue,"status":"STALE"})
    service=SV.CrossVenueService(root=root,ledger=CrossVenueLedger(runtime/"paper.sqlite"))
    health=service._health(service.engine.snapshot())
    assert health["status"] == "DEGRADED"
    assert health["components"]["CROSS_VENUE_NORMALIZER"]["status"] == "DEGRADED"


def test_stale_engine_artifact_is_not_reported_as_current(tmp_path, monkeypatch):
    path=tmp_path/"engine_status.json"
    path.write_text(json.dumps({"status":"PAPER_RESEARCH"}),encoding="utf-8")
    old=time.time()-30; os.utime(path,(old,old))
    monkeypatch.setattr(CVAPI,"ENGINE_STATUS_PATH",path)
    payload=CVAPI.health_payload()
    assert payload["status"] == "STALE"
    assert payload["engine_status_age_seconds"] >= 20


def test_single_venue_move_is_rejected_not_forced():
    config = load_config(); engine = LeadLagEngine(config); t = 1_000_000_000
    engine.process(_event("bitget", t, 100, bid=99.99, ask=100.01))
    engine.process(_event("binance", t, 100, bid=99.99, ask=100.01))
    signal = engine.process(_event("binance", t + 500_000_000, 100.4, bid=100.39, ask=100.41))["signal"]
    assert signal["status"] == "REJECTED_INSUFFICIENT_CONSENSUS"


def test_tiny_move_cannot_clear_cost_gate():
    config = dict(load_config()); config["minimum_leader_move_bps"] = 0.1
    engine = LeadLagEngine(config); t = 1_000_000_000
    for row in (_event("bitget",t,100,bid=99.99,ask=100.01),_event("binance",t,100,bid=99.99,ask=100.01),_event("bybit",t,100,bid=99.99,ask=100.01)):
        engine.process(row)
    engine.process(_event("binance",t+500_000_000,100.02,bid=100.01,ask=100.03))
    signal=engine.process(_event("bybit",t+510_000_000,100.02,bid=100.01,ask=100.03))["signal"]
    assert signal["status"] == "REJECTED_COSTS" and signal["unlevered_net_edge_bps"] < 0


def test_trade_or_mark_price_cannot_contaminate_l1_lead_history():
    engine=LeadLagEngine(load_config()); t=1_000_000_000
    engine.process(_event("binance",t,100,bid=99.99,ask=100.01))
    trade=_event("binance",t+500_000_000,130)
    assert engine.process(trade)["signal"] is None
    mark=_event("binance",t+600_000_000,0); mark["event_type"]="mark_price"; mark["price"]=None; mark["mark_price"]=140
    assert engine.process(mark)["signal"] is None
    assert engine.history[("binance","BTCUSDT")][-1][1] == pytest.approx(100)


def test_funding_and_oi_remain_diagnostic_without_becoming_signal_price():
    engine=LeadLagEngine(load_config()); t=1_000_000_000
    engine.process(_event("okx",t,100,bid=99.99,ask=100.01))
    diagnostic=_event("okx",t+1,0); diagnostic.update(event_type="funding",price=None,funding_rate=0.0001,open_interest=1234)
    assert engine.process(diagnostic)["signal"] is None
    venue=next(row for row in engine.snapshot()["venues"] if row["venue"]=="okx")
    assert venue["funding_rate"] == pytest.approx(0.0001)
    assert venue["open_interest"] == pytest.approx(1234)
    assert venue["price_basis"] == "L1_MIDPOINT_ONLY"


def test_account_is_credited_once_and_reconciles(tmp_path):
    ledger = CrossVenueLedger(tmp_path / "cross.sqlite")
    assert ledger.initialize()["created"] is True
    assert ledger.initialize()["created"] is False
    assert ledger.account()["cash"] == 50
    assert ledger.reconcile()["status"] == "PASS"


def _candidate(signal_id="s1", direction="LONG"):
    return {"signal_id":signal_id,"symbol":"BTCUSDT","direction":direction,
            "decision_ts":"2026-07-17T00:00:00+00:00","decision_monotonic_ns":1_000_000_000,
            "status":"CANDIDATE_RESEARCH_ONLY","rejection_reason":None,
            "unlevered_net_edge_bps":20,"expected_remaining_move_bps":40,
            "bitget_state_at_decision":{"price":100}}


def test_paper_fill_is_strictly_after_decision_and_persists(tmp_path):
    config=load_config(); ledger=CrossVenueLedger(tmp_path/"paper.sqlite"); paper=PaperSimulator(config,ledger)
    paper.on_signal(_candidate())
    early=_event("bitget",1_100_000_000,100,bid=99.99,ask=100.01); early["local_receive_wall_ts"]="early"
    late=_event("bitget",1_300_000_000,100,bid=99.99,ask=100.01); late["local_receive_wall_ts"]="late"
    assert paper.on_bitget_quote(early)["opened"] == []
    assert len(paper.on_bitget_quote(late)["opened"]) == 1
    assert CrossVenueLedger(tmp_path/"paper.sqlite").open_positions()[0]["entry_monotonic_ns"] > 1_000_000_000


def test_paper_rejects_unknown_l1_size_instead_of_inventing_fill(tmp_path):
    config=load_config(); ledger=CrossVenueLedger(tmp_path/"paper.sqlite"); paper=PaperSimulator(config,ledger)
    paper.on_signal(_candidate())
    quote=_event("bitget",1_300_000_000,100,bid=99.99,ask=100.01)
    quote["ask_size"]=None
    assert paper.on_bitget_quote(quote)["opened"] == []
    assert ledger.rows("signals",1)[0]["status"] == "REJECTED_L1_SIZE_MISSING"


def test_slippage_is_explicit_cost_and_not_embedded_twice(tmp_path):
    config=dict(load_config()); config["paper_max_holding_seconds"]=0
    ledger=CrossVenueLedger(tmp_path/"paper.sqlite"); paper=PaperSimulator(config,ledger)
    paper.on_signal(_candidate())
    quote=_event("bitget",1_300_000_000,100,bid=99.99,ask=100.01); quote["local_receive_wall_ts"]="entry"
    opened=paper.on_bitget_quote(quote)["opened"][0]
    assert opened["entry_price"] == pytest.approx(100.01)
    exit_quote=_event("bitget",1_400_000_000,100,bid=99.99,ask=100.01); exit_quote["local_receive_wall_ts"]="exit"
    trade=paper.on_bitget_quote(exit_quote)["closed"][0]
    expected=(trade["exit_price"]-trade["entry_price"])*trade["quantity"]-trade["fees"]-trade["slippage"]-trade["funding"]
    assert trade["net_pnl"] == pytest.approx(expected)
    assert trade["funding_status"] == "CONSERVATIVE_RESERVE_NOT_ACTUAL_PAYMENT"


def test_ledger_rejects_negative_fee_or_slippage(tmp_path):
    ledger=CrossVenueLedger(tmp_path/"paper.sqlite"); paper=PaperSimulator(load_config(),ledger)
    paper.on_signal(_candidate())
    quote=_event("bitget",1_300_000_000,100,bid=99.99,ask=100.01); quote["local_receive_wall_ts"]="entry"
    position=paper.on_bitget_quote(quote)["opened"][0]
    with pytest.raises(ValueError):
        ledger.close_simulated_position(position["position_id"],{
            "exit_price":100,"exit_fee":-1,"exit_slippage":0,"funding":0,
            "exit_ts":"exit","exit_reason":"TEST","holding_seconds":1,
        })


def test_same_bar_always_stops_before_tp_long_and_short():
    assert choose_bar_exit("LONG", high=102, low=98, stop=99, take_profit=101) == "STOP_BEFORE_TP"
    assert choose_bar_exit("SHORT", high=102, low=98, stop=101, take_profit=99) == "STOP_BEFORE_TP"


def test_long_short_paper_price_symmetry(tmp_path):
    config=load_config()
    for direction in ("LONG","SHORT"):
        ledger=CrossVenueLedger(tmp_path/f"{direction}.sqlite"); paper=PaperSimulator(config,ledger)
        paper.on_signal(_candidate(direction=direction))
        quote=_event("bitget",1_300_000_000,100,bid=99.99,ask=100.01); quote["local_receive_wall_ts"]="t"
        opened=paper.on_bitget_quote(quote)["opened"][0]
        if direction=="LONG": assert opened["stop_price"] < opened["entry_price"] < opened["take_profit_price"]
        else: assert opened["take_profit_price"] < opened["entry_price"] < opened["stop_price"]


def test_leverage_scenarios_reuse_fill_and_negative_edge_stays_negative():
    trade={"trade_id":"t","notional":5,"gross_return_bps":5,"total_cost_bps":15,"mae_bps":1}
    one=simulate_trade(trade,1,50); fifty=simulate_trade(trade,50,50)
    assert one["same_fill_base"] and fifty["same_market_path"]
    assert one["pnl"] < 0 and fifty["pnl"] < 0
    assert fifty["unlevered_net_return_bps"] == one["unlevered_net_return_bps"] == -10


def test_leverage_liquidation_is_conservative():
    trade={"trade_id":"t","notional":5,"gross_return_bps":100,"total_cost_bps":10,"mae_bps":300}
    assert simulate_trade(trade,50,50)["liquidated"] is True
    assert simulate_trade(trade,1,50)["liquidated"] is False


def test_api_surface_is_read_only_only():
    expected={"status","providers","venues","prices","orderflow","leadlag","signals","account","positions","trades","equity","leverage","health"}
    actual={path.rsplit("/",1)[-1] for path in READERS}
    assert expected <= actual
    forbidden=("open","close","reset","configure","leverage-set","live","key","order")
    assert all(not any(token == path.rsplit("/",1)[-1] for token in forbidden) for path in READERS)


def test_cli_parser_is_standalone_and_safe():
    args=build_parser().parse_args(["collect","--venue","bybit","--max-sessions","1","--max-messages","2","--stop-file","stop.flag"])
    assert args.venue=="bybit" and args.max_messages==2 and args.stop_file=="stop.flag"
    assert build_parser().parse_args(["engine","--max-cycles","1"]).max_cycles==1


def test_engine_honors_existing_cooperative_stop_file(tmp_path):
    stop=tmp_path/"stack.stop"; stop.write_text("stop",encoding="ascii")
    class Service:
        def cycle(self): raise AssertionError("cycle must not run")
    payload=SV.run_service(service=Service(),stop_file=stop)
    assert payload["cycles"]==0 and payload["can_send_real_orders"] is False


def test_dashboard_has_single_cross_venue_panel_and_no_mutation(tmp_path):
    from app.labs import research_dashboard_v10_43c as D
    state={"tool_version":"v10.43c","symbol":"BTCUSDT","generated_at":"now","git_head":"x","health":{},"view":{},"data_quality":{},"shadow":None,"scoreboard":[],"bankroll":None,"ws_dataset":{},"persistent_health":{},"persistent_continuity":{},"source_compare_3way":{},"strategy_hardening":{},"ws_persistent_tournament":{},"exit_optimization":{},"readiness_v1043c":{"primary":"RESEARCH_ONLY","states":[]},"ati_paper":{},"cross_venue":{"health":{"status":"CONNECTING"},"venues":[],"signals":[],"positions":[],"trades":[],"leadlag":{"leaderboard":[]},"leverage":{"scenarios":[]},"account":None}}
    page=D.build_dashboard("BTCUSDT",state=state,out_dir=tmp_path,write=False)["html_str"]
    assert page.count('id="crossVenuePanel"')==1
    assert "CROSS-VENUE INTELLIGENCE" in page and "NOT ACTIONABLE" in page and "NO LIVE" in page
    assert "/api/cross-venue/order" not in page and "/api/cross-venue/reset" not in page


def test_local_stack_contains_six_isolated_cross_venue_components():
    root=Path(__file__).resolve().parents[1]; common=(root/"scripts/local_stack_common.ps1").read_text(encoding="utf-8")
    for name in ("bitget","binance","bybit","okx","hyperliquid"):
        assert f"run_cross_venue_{name}_forever.ps1" in common
    assert "run_cross_venue_engine_forever.ps1" in common
    combined="\n".join(path.read_text(encoding="utf-8") for path in (root/"scripts").glob("run_cross_venue*.ps1"))
    for token in ("private_get", "private_post", "set_margin_mode", "ExecutionEngine.execute", "PaperTrader.open_position", "LIVE_TRADING=True", "can_send_real_orders=True"):
        assert token not in combined
    assert "NO LIVE" in combined
    assert "--stop-file" in combined and "stack.stop" in combined


def test_productive_cross_venue_package_has_no_execution_surface():
    root=Path(__file__).resolve().parents[1]/"app/labs/cross_venue"
    productive="\n".join(path.read_text(encoding="utf-8") for path in root.glob("*.py"))
    forbidden=("private_get(","private_post(","place_order(","set_leverage(","set_margin_mode(",
               "ExecutionEngine.execute","PaperTrader.open_position","can_send_real_orders=True",
               "LIVE_TRADING=True","ENABLE_PAPER_POLICY_FILTER=True","allow_real_writes=True")
    assert all(token not in productive for token in forbidden)
    assert "app.execution_engine" not in productive and "app.paper_trader" not in productive


def test_health_server_exposes_only_get_routing_for_cross_venue():
    source=(Path(__file__).resolve().parents[1]/"app/health_server.py").read_text(encoding="utf-8")
    assert '_CROSS_VENUE_API_PREFIX = "/api/cross-venue/"' in source
    assert "cross_venue_api_payload(path, query)" in source
    assert "def do_POST" not in source or "/api/cross-venue/" not in source[source.find("def do_POST"):source.find("def do_POST")+1500]
