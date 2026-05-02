from app.config import BotConfig
from app.main import _refresh_live_account_balance


class FailingClient:
    def get_account(self, symbol):
        raise RuntimeError("api down")


class OkClient:
    def get_account(self, symbol):
        return {"usdtEquity": "40", "available": "28"}


class DummyLogger:
    def __init__(self):
        self.errors = []
        self.infos = []

    def info(self, *args):
        self.infos.append(args[0] % args[1:] if len(args) > 1 else args[0])

    def error(self, *args):
        self.errors.append(args[0] % args[1:] if len(args) > 1 else args[0])


class DummyTelegram:
    def __init__(self):
        self.messages = []

    def critical(self, message):
        self.messages.append(message)


def live_config():
    return BotConfig(
        paper_trading=False,
        live_trading=True,
        dry_run=False,
        bitget_api_key="key",
        bitget_api_secret="secret",
        bitget_passphrase="pass",
    )


def test_live_balance_refresh_failure_blocks_operation():
    logger = DummyLogger()
    telegram = DummyTelegram()
    balance, available, used_margin, ok = _refresh_live_account_balance(
        live_config(), FailingClient(), "BTCUSDT", logger, telegram, 40, 40
    )
    assert not ok
    assert balance == 40
    assert available == 40
    assert used_margin == 0
    assert logger.errors
    assert telegram.messages


def test_live_balance_refresh_logs_live_balances():
    logger = DummyLogger()
    telegram = DummyTelegram()
    balance, available, used_margin, ok = _refresh_live_account_balance(
        live_config(), OkClient(), "BTCUSDT", logger, telegram, 40, 40
    )
    assert ok
    assert balance == 40
    assert available == 28
    assert used_margin == 12
    assert "live_balance=40.0000" in logger.infos[0]
