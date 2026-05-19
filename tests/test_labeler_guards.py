from app.data_guards import classify_label_path_consistency, should_insert_label
from app.labeler import LabelOutcome, TripleBarrierLabeler


class _FakeDb:
    def __init__(self) -> None:
        self.labels = [{"id": 7, "observation_id": 1, "first_barrier_hit": "TP1"}]
        self.inserted = 0

    def fetch_signal_label_for_observation(self, observation_id: int):
        return next((row for row in self.labels if row["observation_id"] == observation_id), None)

    def record_signal_label(self, _payload):
        self.inserted += 1
        return 99


def test_duplicate_label_guard_blocks_final_duplicate():
    existing = [{"observation_id": 1, "first_barrier_hit": "TP1"}]

    allowed, reason = should_insert_label(existing, {"observation_id": 1, "first_barrier_hit": "SL"})

    assert allowed is False
    assert reason == "conflicting_final_label_exists"


def test_labeler_save_label_skips_existing_final_label():
    fake = _FakeDb()
    labeler = TripleBarrierLabeler(config=object(), db=fake)
    outcome = LabelOutcome(1, 1, "TP1", 3, 0.5, -0.1, 0.5, 0.0, True)

    assert labeler.save_label(outcome) == 7
    assert fake.inserted == 0


def test_mfe_mae_label_mismatch_detection():
    assert classify_label_path_consistency(label_hit="TIME", mfe_pct=0.6, mae_pct=0.1, tp_threshold_pct=0.5, sl_threshold_pct=0.75) == "MISSED_TP_POSSIBLE"
    assert classify_label_path_consistency(label_hit="TIME", mfe_pct=0.1, mae_pct=0.8, tp_threshold_pct=0.5, sl_threshold_pct=0.75) == "MISSED_SL_POSSIBLE"
    assert classify_label_path_consistency(label_hit="TIME", mfe_pct=0.6, mae_pct=0.8, tp_threshold_pct=0.5, sl_threshold_pct=0.75) == "AMBIGUOUS_BOTH_TOUCHED"
