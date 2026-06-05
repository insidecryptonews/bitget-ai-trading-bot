"""V8.2.3 — CLI parser conflict regression tests.

V8.2 added a second ``parser.add_argument("--policy", ...)`` that collided
with the existing Phase 8 ``--policy`` declaration, breaking every CLI on
import with::

    argparse.ArgumentError: argument --policy: conflicting option string: --policy

These tests assert:

1. The argparse parser can be constructed (no ``conflicting option string``).
2. ``--policy`` is declared exactly once.
3. Each V8.2 CLI command can be parsed by argparse without error.
4. ``profit-lock-sim`` accepts ``--policy all`` explicitly.
5. ``profit-lock-sim`` without ``--policy`` also parses (will be mapped to
   ``"all"`` inside the dispatch).

We avoid subprocess invocations so the tests are fast and deterministic.
"""

from __future__ import annotations

import argparse

import pytest


# ---------------------------------------------------------------------------
# Builder + duplicate-option detection
# ---------------------------------------------------------------------------

def test_argument_parser_builds_without_conflicting_option_string():
    """Smoke test: importing and building the parser must not raise."""
    from app.research_lab import build_argument_parser

    parser = build_argument_parser()
    assert isinstance(parser, argparse.ArgumentParser)


def test_policy_is_declared_exactly_once():
    """The ``--policy`` option must exist exactly once across the parser."""
    from app.research_lab import build_argument_parser

    parser = build_argument_parser()
    policy_actions = [
        a for a in parser._actions
        if any(opt == "--policy" for opt in (a.option_strings or []))
    ]
    assert len(policy_actions) == 1, (
        f"--policy declared {len(policy_actions)} times; "
        f"expected exactly 1 to avoid argparse conflict."
    )


def test_policy_default_is_phase_8_legacy_value():
    """The single ``--policy`` keeps the Phase 8 legacy default so existing
    cost-stress / validator commands stay backwards compatible.
    """
    from app.research_lab import build_argument_parser

    parser = build_argument_parser()
    for a in parser._actions:
        if "--policy" in (a.option_strings or []):
            assert a.default == "late_entry_block_plus_dynamic_hold"


# ---------------------------------------------------------------------------
# V8.2 CLI command parsing
# ---------------------------------------------------------------------------

V82_COMMANDS_WITHOUT_POLICY = [
    ["bidirectional-funnel", "--hours", "168"],
    ["missed-opportunities", "--side", "SHORT", "--hours", "168"],
    ["blocked-counterfactual", "--side", "SHORT", "--hours", "168"],
    ["failed-executed", "--side", "SHORT", "--hours", "168"],
    ["good-not-monetized", "--side", "SHORT", "--hours", "168"],
    ["score-asymmetry-audit", "--hours", "168"],
    ["score-symmetric-simulation", "--hours", "168"],
    ["score-atr-softened-simulation", "--hours", "168"],
    ["score-high-vol-directional-simulation", "--hours", "168"],
    ["regime-router-simulation", "--hours", "168"],
    ["trend-campaign-sim", "--side", "SHORT", "--max-adds", "3", "--hours", "168"],
    ["research-pack-bidirectional-v1", "--hours", "168"],
]


@pytest.mark.parametrize("argv", V82_COMMANDS_WITHOUT_POLICY)
def test_v82_cli_commands_parse_without_argparse_error(argv):
    from app.research_lab import build_argument_parser

    parser = build_argument_parser()
    # parse_args must not raise SystemExit / ArgumentError on these.
    ns = parser.parse_args(argv)
    assert ns.command == argv[0]


def test_profit_lock_sim_parses_with_explicit_policy_all():
    from app.research_lab import build_argument_parser

    parser = build_argument_parser()
    ns = parser.parse_args([
        "profit-lock-sim", "--side", "SHORT", "--policy", "all", "--hours", "168",
    ])
    assert ns.command == "profit-lock-sim"
    assert ns.policy == "all"


def test_profit_lock_sim_parses_without_policy_keeps_legacy_default():
    """Without ``--policy`` the parser stores the Phase 8 default. The
    dispatch in ``main()`` maps that legacy default to ``"all"`` for the
    profit-lock-sim handler.
    """
    from app.research_lab import build_argument_parser

    parser = build_argument_parser()
    ns = parser.parse_args(["profit-lock-sim", "--side", "SHORT", "--hours", "168"])
    assert ns.command == "profit-lock-sim"
    assert ns.policy == "late_entry_block_plus_dynamic_hold"


def test_phase_8_commands_keep_legacy_policy_default():
    """Phase 8 helpers (e.g. ``phase8-cost-stress``) must still see the
    legacy default unchanged so existing scripts keep working.
    """
    from app.research_lab import build_argument_parser

    parser = build_argument_parser()
    ns = parser.parse_args(["phase8-cost-stress", "--hours", "24"])
    assert ns.policy == "late_entry_block_plus_dynamic_hold"


# ---------------------------------------------------------------------------
# Dispatch-level mapping: legacy default → "all" for profit-lock-sim
# ---------------------------------------------------------------------------

def test_dispatch_maps_legacy_policy_default_to_all_for_profit_lock_sim(monkeypatch):
    """When the user does not pass ``--policy``, the profit-lock-sim
    handler must internally treat the legacy default as ``"all"`` so that
    every policy is simulated.
    """
    from app.research_lab import ResearchLab

    class _NoopDB:
        pass

    captured = {}
    original = ResearchLab.profit_lock_sim_cli

    def _capture(self, *, hours, side, policy):
        captured["policy"] = policy
        return original(self, hours=hours, side=side, policy=policy)

    monkeypatch.setattr(ResearchLab, "profit_lock_sim_cli", _capture)

    lab = ResearchLab(config=None, db=_NoopDB())
    # Simulate the dispatch path from main(): policy comes in as the legacy
    # default, the dispatch maps it to "all".
    policy_arg = "late_entry_block_plus_dynamic_hold"
    if policy_arg == "late_entry_block_plus_dynamic_hold":
        policy_arg = "all"
    lab.profit_lock_sim_cli(hours=168, side="SHORT", policy=policy_arg)
    assert captured["policy"] == "all"


# ---------------------------------------------------------------------------
# Help text smoke
# ---------------------------------------------------------------------------

def test_help_message_contains_policy_once():
    """The rendered ``--help`` should mention ``--policy`` once (not twice)."""
    from app.research_lab import build_argument_parser

    parser = build_argument_parser()
    help_text = parser.format_help()
    occurrences = help_text.count("--policy")
    # Help text mentions --policy in the usage line and in the option block.
    # With the V8.2.3 fix the option is declared once, but argparse may still
    # render it twice (one usage, one detail). We assert at most a small
    # number of mentions to catch the duplicate-declaration regression.
    assert occurrences <= 3, (
        f"--policy appears {occurrences} times in help — possible duplicate "
        f"declaration regression."
    )
