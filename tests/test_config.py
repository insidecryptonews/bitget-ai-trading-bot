from app.config import BotConfig


def assert_raises_value_error(fn):
    try:
        fn()
    except ValueError:
        return
    raise AssertionError("Expected ValueError")


def test_config_aborts_live_crossed_margin():
    assert_raises_value_error(
        lambda: BotConfig(
            paper_trading=False,
            live_trading=True,
            dry_run=False,
            margin_mode="crossed",
            disallow_crossed_margin=True,
        )
    )


def test_config_aborts_live_without_force_isolated():
    assert_raises_value_error(
        lambda: BotConfig(
            paper_trading=False,
            live_trading=True,
            dry_run=False,
            force_isolated_margin=False,
        )
    )

