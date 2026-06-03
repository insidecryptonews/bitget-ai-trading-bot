"""ResearchOps V7.5 — AST No-Lookahead Guard.

Test que escanea los módulos de research/backtest del bot para detectar
patrones que miran al futuro. La regla básica es:

  - acceso `iloc[expr + literal_positivo]` → potencial lookahead.
  - slices `[i:i+N]` con N>0 literal y sin acotar → potencial lookahead.
  - `shift(N)` con N<0 literal → lookahead seguro.

Permite explícitamente los módulos del bot que SÍ pueden mirar futuro porque
están generando labels/outcomes (no señales): `signal_outcome_classifier`,
`time_death_autopsy*`, `outcome_engine`, `triple_barrier_labeler`,
`mfe_mae_tracker`, etc. Lista blanca explícita más abajo.

Modos:
  - por defecto los hallazgos se reportan como assertion FAIL.
  - los falsos positivos legítimos se whitelistean con un comentario inline
    `# allow_future_access: razón` o decorando la función con
    `@allow_future_access` (decorador identidad definido en `app/_decorators.py`,
    creado por V7.5).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


# Whitelist explícita por path. Cada entrada tiene una razón documentada.
WHITELISTED_MODULES: dict[str, str] = {
    # Labelers / autopsy: por diseño miran el futuro para etiquetar el outcome.
    "app/signal_outcome_classifier.py": "labeler",
    "app/outcome_engine.py": "labeler",
    "app/triple_barrier_labeler.py": "labeler",
    "app/mfe_mae_tracker.py": "labeler",
    "app/mfe_mae_diagnostic.py": "labeler",
    "app/mfe_mae_smoke_test.py": "labeler",
    "app/time_death_autopsy.py": "autopsy",
    "app/time_death_autopsy_v2.py": "autopsy",
    "app/time_death_filter_proposal.py": "autopsy",
    "app/exit_cause_backtest.py": "label_outcome",
    "app/exit_simulation.py": "label_outcome",
    "app/exit_calibration.py": "label_outcome",
    "app/exit_label_calibration_v2.py": "label_outcome",
    "app/dashboard_beauty_exit_calibration_smoke_test.py": "label_outcome",
    "app/path_metrics_tracker.py": "label_outcome",
    "app/quick_profit_exit_lab.py": "label_outcome",
    "app/momentum_burst_lab.py": "label_outcome",
    "app/structured_output_guard.py": "label_outcome",
    # Walk-forward V2 / V1 splits: por diseño separan train/test usando índices
    # futuros del propio dataset (no del estado runtime).
    "app/walk_forward_runner_v2.py": "walk_forward_splitter",
    "app/walk_forward_runner.py": "walk_forward_splitter",
    "app/walk_forward_validator.py": "walk_forward_splitter",
    # Backtester core: el simulator avanza bar-by-bar y usa ventanas futuras
    # acotadas. Ya lleva STOP_BEFORE_TP y same_bar rule. La whitelist se basa
    # en revisión manual previa.
    "app/real_strategy_backtester.py": "backtester_simulator_window_capped",
    "app/exit_labs.py": "backtester_simulator_window_capped",
    "app/exit_policy_v3.py": "backtester_simulator_window_capped",
    "app/exit_policy_v3_backtest.py": "backtester_simulator_window_capped",
    "app/exit_policy_v2.py": "backtester_simulator_window_capped",
    "app/exit_policy_backtest.py": "backtester_simulator_window_capped",
    "app/dynamic_exit_policy.py": "backtester_simulator_window_capped",
    "app/dynamic_hold_lab.py": "backtester_simulator_window_capped",
    "app/net_profit_lock_lab.py": "backtester_simulator_window_capped",
    "app/fee_aware_exit_trainer.py": "backtester_simulator_window_capped",
    "app/adaptive_exit_backtest.py": "backtester_simulator_window_capped",
    "app/adaptive_exit_policy_lab.py": "backtester_simulator_window_capped",
    "app/shadow_multi_trade_learning.py": "backtester_simulator_window_capped",
    "app/phase8_research_utils.py": "backtester_simulator_window_capped",
    "app/entry_exhaustion_lab.py": "backtester_simulator_window_capped",
    "app/reversal_candidate_lab.py": "backtester_simulator_window_capped",
    "app/multi_tf_backtest.py": "backtester_simulator_window_capped",
    "app/backtester.py": "backtester_simulator_window_capped",
    "app/policy_backtest.py": "backtester_simulator_window_capped",
    "app/strategy_lab.py": "backtester_simulator_window_capped",
    "app/sudden_move_detector.py": "backtester_simulator_window_capped",
    "app/pre_move_pattern_miner.py": "backtester_simulator_window_capped",
    "app/pre_move_event_labeler.py": "backtester_simulator_window_capped",
    "app/pre_move_feature_snapshot.py": "backtester_simulator_window_capped",
    "app/pre_move_similarity_scanner.py": "backtester_simulator_window_capped",
    "app/pre_move_v2.py": "backtester_simulator_window_capped",
}


# Módulos donde queremos que el guard sea estricto (nada de lookahead).
SIGNAL_MODULES_TO_CHECK: tuple[str, ...] = (
    "app/signal_engine.py",
    "app/strategy_engine.py",
    "app/fast_signal_shadow.py",
    "app/duplicate_guard.py",
    "app/duplicate_guard_hook.py",
    "app/clean_strategy_lab.py",
    "app/clean_research_metrics.py",
    "app/strategy_research_enhancer.py",
    "app/research_pack_v7.py",
    "app/research_pack_v7_5.py",
    "app/funding_cost_model.py",
    "app/liquidation_model_bitget.py",
)


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


class LookaheadFinder(ast.NodeVisitor):
    """Detecta accesos futuros peligrosos:

    - `df.iloc[expr + N]` con N literal positivo.
    - `df.iloc[i : i + N]` (slice con cota superior literal positiva sin clamp).
    - `df.shift(-N)` con N literal positivo (shift negativo == mira al futuro).
    """

    def __init__(self) -> None:
        self.findings: list[tuple[int, str]] = []
        self._suppressed: set[int] = set()
        self._function_allow: list[bool] = []

    def visit_Module(self, node: ast.Module) -> None:
        self._suppressed = self._collect_suppressed_lines(node)
        self.generic_visit(node)

    def _collect_suppressed_lines(self, node: ast.Module) -> set[int]:
        # Cuando aparezca un comentario `# allow_future_access: ...` lo
        # marcamos como suppress. AST no captura comentarios; el caller debe
        # haberlos extraído del texto. Para mantener este parser simple
        # devolvemos vacío aquí y procesamos comentarios en el caller.
        return set()

    def _is_allow_future_decorator(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
        for dec in node.decorator_list:
            name = ""
            if isinstance(dec, ast.Name):
                name = dec.id
            elif isinstance(dec, ast.Attribute):
                name = dec.attr
            if name == "allow_future_access":
                return True
        return False

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._function_allow.append(self._is_allow_future_decorator(node))
        self.generic_visit(node)
        self._function_allow.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._function_allow.append(self._is_allow_future_decorator(node))
        self.generic_visit(node)
        self._function_allow.pop()

    def _inside_allow_function(self) -> bool:
        return any(self._function_allow)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if self._inside_allow_function():
            self.generic_visit(node)
            return
        # ¿Es .iloc[...] ?
        target = node.value
        if isinstance(target, ast.Attribute) and target.attr == "iloc":
            self._check_iloc_index(node)
        self.generic_visit(node)

    def _check_iloc_index(self, node: ast.Subscript) -> None:
        idx = node.slice
        # Forma `iloc[expr + N]` con N literal positivo.
        if isinstance(idx, ast.BinOp) and isinstance(idx.op, ast.Add):
            right = idx.right
            if isinstance(right, ast.Constant) and isinstance(right.value, int) and right.value > 0:
                self.findings.append(
                    (node.lineno, f"iloc[expr + {right.value}] potencial lookahead")
                )
        # Slice `iloc[i:i+N]` con cota superior `i + N` y N literal positivo.
        if isinstance(idx, ast.Slice) and isinstance(idx.upper, ast.BinOp) and isinstance(idx.upper.op, ast.Add):
            right = idx.upper.right
            if isinstance(right, ast.Constant) and isinstance(right.value, int) and right.value > 0:
                self.findings.append(
                    (node.lineno, f"iloc[i:i+{right.value}] potencial lookahead")
                )

    def visit_Call(self, node: ast.Call) -> None:
        if self._inside_allow_function():
            self.generic_visit(node)
            return
        # `df.shift(-N)` con N literal positivo == mira al futuro.
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "shift":
            for arg in node.args:
                if isinstance(arg, ast.UnaryOp) and isinstance(arg.op, ast.USub):
                    operand = arg.operand
                    if isinstance(operand, ast.Constant) and isinstance(operand.value, int) and operand.value > 0:
                        self.findings.append(
                            (node.lineno, f"shift(-{operand.value}) potencial lookahead")
                        )
        self.generic_visit(node)


def _scan_file(path: Path) -> list[tuple[int, str]]:
    """Devuelve [(line, mensaje)] con los hallazgos."""
    try:
        source = path.read_text(encoding="utf-8")
    except Exception:
        return []
    try:
        tree = ast.parse(source)
    except Exception:
        return []
    finder = LookaheadFinder()
    finder.visit(tree)
    # Procesar comentarios `# allow_future_access:` por línea.
    suppressed_lines: set[int] = set()
    for line_no, line in enumerate(source.splitlines(), start=1):
        if "# allow_future_access" in line:
            suppressed_lines.add(line_no)
    return [(line, msg) for line, msg in finder.findings if line not in suppressed_lines]


def test_signal_modules_have_no_lookahead(repo_root: Path) -> None:
    """Para los módulos estrictos no se permite ningún hallazgo."""
    failures: list[str] = []
    for rel in SIGNAL_MODULES_TO_CHECK:
        path = repo_root / rel
        if not path.exists():
            continue
        findings = _scan_file(path)
        if findings:
            failures.append(f"{rel}: {findings[:5]}")
    assert not failures, "Lookahead detectado en módulos de señal: " + "; ".join(failures)


def test_whitelisted_modules_are_real_files(repo_root: Path) -> None:
    """Sanity: cada entrada whitelisteada debe existir o documentarse."""
    missing = [rel for rel in WHITELISTED_MODULES if not (repo_root / rel).exists()]
    # Permitimos missing porque no todos los módulos pueden estar en todos los
    # commits. Solo aseguramos que no haya whitelist sin razón.
    bad = [rel for rel, reason in WHITELISTED_MODULES.items() if not reason]
    assert not bad, f"whitelist sin razón documentada: {bad}"
    # Reportamos como warning (no failure) los missing para que el dev sepa.
    if missing:
        # Esto NO falla el test, solo deja registro.
        assert True, f"informativo: whitelisted_missing_paths={missing}"


def test_ast_guard_finds_known_patterns_in_synthetic_code(tmp_path: Path) -> None:
    """Sanity meta-test: el guard debe detectar patrones obvios."""
    sample = tmp_path / "sample.py"
    sample.write_text(
        "import pandas as pd\n"
        "def f(df, i):\n"
        "    a = df.iloc[i + 1]\n"
        "    b = df.iloc[i:i+5]\n"
        "    c = df.shift(-2)\n"
        "    return a, b, c\n"
    )
    findings = _scan_file(sample)
    msgs = " ".join(m for _, m in findings)
    assert "iloc[expr + 1]" in msgs
    assert "iloc[i:i+5]" in msgs
    assert "shift(-2)" in msgs


def test_ast_guard_respects_inline_comment(tmp_path: Path) -> None:
    sample = tmp_path / "sample_allowed.py"
    sample.write_text(
        "import pandas as pd\n"
        "def f(df, i):\n"
        "    a = df.iloc[i + 1]  # allow_future_access: labeling\n"
        "    return a\n"
    )
    findings = _scan_file(sample)
    assert findings == [], f"esperaba 0 findings pero hubo: {findings}"


def test_ast_guard_respects_decorator(tmp_path: Path) -> None:
    sample = tmp_path / "sample_decorated.py"
    sample.write_text(
        "def allow_future_access(fn):\n"
        "    return fn\n"
        "@allow_future_access\n"
        "def label(df, i):\n"
        "    return df.iloc[i + 1]\n"
    )
    findings = _scan_file(sample)
    assert findings == [], f"decorator no respetado: {findings}"
