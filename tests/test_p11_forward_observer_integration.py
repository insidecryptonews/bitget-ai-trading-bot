"""Integration-only tests for the isolated P11_SHORT forward observer.

These tests exercise CLI/loop wiring with fakes. The observer lifecycle and its
Bitget data semantics are covered by the core observer tests.
"""

from __future__ import annotations

import ast
import inspect
import json
import shutil
import sys
import textwrap
import uuid
from pathlib import Path

from app import research_lab
from app.labs import cross_exchange_public_ohlcv_v10_15 as X
from app.labs import multi_symbol_opportunity_scanner_v10_28 as S


def _output_dir() -> str:
    return f"reports/research/v10_28/_pytest_p11_hook_{uuid.uuid4().hex[:8]}"


def _cleanup(output_dir: str) -> None:
    root = Path(research_lab.__file__).resolve().parents[1]
    shutil.rmtree(root / output_dir, ignore_errors=True)


class _FakeObserver:
    def __init__(self, result=None) -> None:
        self.result = result or {"status": "ok"}
        self.poll_calls = 0
        self.close_calls = 0

    def poll_once(self):
        self.poll_calls += 1
        return self.result

    def close(self) -> None:
        self.close_calls += 1


def test_scanner_hook_failure_is_isolated_and_close_runs_once():
    output_dir = _output_dir()
    emitted: list[str] = []
    calls = {"poll": 0, "close": 0}

    def hook():
        calls["poll"] += 1
        if calls["poll"] == 1:
            raise RuntimeError("observer unavailable")

    def close():
        calls["close"] += 1

    try:
        summary = S.run_loop(
            universe=["BTCUSDT"],
            bars_provider=lambda _symbol: [],
            max_scans=3,
            interval_seconds=0.0,
            output_dir=output_dir,
            sleep_fn=lambda _seconds: None,
            should_stop=lambda: False,
            emit=emitted.append,
            observer_hook=hook,
            observer_close=close,
        )
        assert summary["scans_completed"] == 3
        assert summary["observer_hook_errors"] == 1
        assert calls == {"poll": 3, "close": 1}
        assert any("isolated P11 observer error" in line for line in emitted)
        assert any("CLEAN SHUTDOWN COMPLETE" in line for line in emitted)
    finally:
        _cleanup(output_dir)


def test_observer_cli_handlers_poll_close_and_use_continuous_helper():
    lab = research_lab.ResearchLab.__new__(research_lab.ResearchLab)
    observer = _FakeObserver({"processed_bars": 2})

    once = json.loads(
        lab.p11_forward_observer_once_cli(observer_factory=lambda: observer)
    )
    assert once["result"] == {"processed_bars": 2}
    assert once["can_send_real_orders"] is False
    assert observer.poll_calls == 1 and observer.close_calls == 1

    runner_calls = []
    run = json.loads(
        lab.p11_forward_observer_run_cli(
            runner=lambda: runner_calls.append("run") or {"stop_reason": "test"}
        )
    )
    assert runner_calls == ["run"]
    assert run["result"]["stop_reason"] == "test"
    assert run["can_send_real_orders"] is False


def test_observer_commands_are_early_public_and_skip_private_bootstrap(monkeypatch, capsys):
    for command in ("p11-forward-observer-once", "p11-forward-observer-run"):
        assert command in research_lab.PUBLIC_RESEARCH_ONLY_COMMANDS
        assert research_lab.build_argument_parser().parse_args([command]).command == command

    monkeypatch.setattr(
        research_lab,
        "load_config",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not load config/.env")),
    )
    monkeypatch.setattr(
        research_lab,
        "Database",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not open database")),
    )
    monkeypatch.setattr(
        research_lab.ResearchLab,
        "p11_forward_observer_once_cli",
        lambda self: "isolated-once",
    )
    monkeypatch.setattr(
        research_lab.ResearchLab,
        "p11_forward_observer_run_cli",
        lambda self: "isolated-run",
    )

    old_argv = sys.argv
    try:
        sys.argv = ["prog", "p11-forward-observer-once"]
        research_lab.main()
        sys.argv = ["prog", "p11-forward-observer-run"]
        research_lab.main()
    finally:
        sys.argv = old_argv
    assert capsys.readouterr().out.splitlines() == ["isolated-once", "isolated-run"]


def test_real_scanner_cli_auto_attaches_observer_without_sharing_scanner_bars(monkeypatch):
    output_dir = _output_dir()
    observer = _FakeObserver()
    lab = research_lab.ResearchLab.__new__(research_lab.ResearchLab)
    monkeypatch.setattr(X, "fetch_series", lambda *a, **k: ([], 1))

    try:
        result = lab.opportunity_scanner_run_v1028_cli(
            universe="BTCUSDT",
            max_scans=1,
            interval_seconds=0.0,
            output_dir=output_dir,
            should_stop=lambda: False,
            emit=lambda _line: None,
            observer_factory=lambda: observer,
        )
        assert observer.poll_calls == 1 and observer.close_calls == 1
        assert "p11_observer_attached=true" in result
        assert "p11_observer_errors=0" in result
        assert "can_send_real_orders=false" in result
    finally:
        _cleanup(output_dir)


def test_integration_wiring_has_no_order_capable_calls():
    sources = [
        inspect.getsource(S.run_loop),
        inspect.getsource(research_lab.ResearchLab.p11_forward_observer_once_cli),
        inspect.getsource(research_lab.ResearchLab.p11_forward_observer_run_cli),
        inspect.getsource(research_lab.ResearchLab.opportunity_scanner_run_v1028_cli),
    ]
    banned = {
        "place_order",
        "create_order",
        "open_position",
        "execute_order",
        "private_get",
        "private_post",
    }
    called = set()
    for source in sources:
        tree = ast.parse(textwrap.dedent(source))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name):
                    called.add(func.id)
                elif isinstance(func, ast.Attribute):
                    called.add(func.attr)
    assert called.isdisjoint(banned)
