from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

try:
    import requests
    from requests import Response
    RequestException = requests.RequestException
except ImportError:  # pragma: no cover - production installs requests from requirements.txt
    requests = None  # type: ignore
    Response = Any  # type: ignore
    RequestException = RuntimeError

try:
    from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential
except ImportError:  # pragma: no cover - production installs tenacity from requirements.txt
    def retry(*args, **kwargs):
        def decorator(func):
            return func
        return decorator

    def retry_if_exception_type(*args, **kwargs):
        return None

    def stop_after_attempt(*args, **kwargs):
        return None

    def wait_exponential(*args, **kwargs):
        return None

from .config import BotConfig
from .utils import SimpleRateLimiter, json_dumps, now_ms, sanitize


class BitgetAPIError(RuntimeError):
    pass


@dataclass
class BitgetResponse:
    code: str
    msg: str
    data: Any
    request_time: int | None = None

    @property
    def ok(self) -> bool:
        return self.code == "00000"


class BitgetClient:
    """Small REST client for documented Bitget Futures v2 endpoints.

    The client does not log secrets and all private requests use Bitget's HMAC
    signature format: timestamp + method + path + optional query + body.
    """

    def __init__(self, config: BotConfig, logger) -> None:
        if requests is None:
            raise BitgetAPIError("El paquete 'requests' no está instalado. Ejecuta pip install -r requirements.txt.")
        self.config = config
        self.logger = logger
        self.session = requests.Session()
        self.limiter = SimpleRateLimiter(max_calls=8, period_seconds=1.0)
        self.failure_count = 0
        self.last_error: str = ""

    def _sign(self, timestamp: str, method: str, path: str, query: str = "", body: str = "") -> str:
        query_part = f"?{query}" if query else ""
        payload = f"{timestamp}{method.upper()}{path}{query_part}{body}"
        digest = hmac.new(
            self.config.bitget_api_secret.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        return base64.b64encode(digest).decode("utf-8")

    def _headers(self, method: str, path: str, query: str, body: str, auth: bool) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "locale": "en-US"}
        if not auth:
            return headers
        if not self.config.has_bitget_credentials:
            raise BitgetAPIError("Credenciales Bitget incompletas para endpoint privado.")
        timestamp = str(now_ms())
        headers.update(
            {
                "ACCESS-KEY": self.config.bitget_api_key,
                "ACCESS-SIGN": self._sign(timestamp, method, path, query, body),
                "ACCESS-TIMESTAMP": timestamp,
                "ACCESS-PASSPHRASE": self.config.bitget_passphrase,
            }
        )
        return headers

    def _parse_response(self, response: Response) -> BitgetResponse:
        try:
            payload = response.json()
        except ValueError as exc:
            raise BitgetAPIError(f"Respuesta no JSON de Bitget: HTTP {response.status_code}") from exc
        parsed = BitgetResponse(
            code=str(payload.get("code", "")),
            msg=str(payload.get("msg", "")),
            data=payload.get("data"),
            request_time=payload.get("requestTime"),
        )
        if response.status_code >= 400 or not parsed.ok:
            raise BitgetAPIError(f"Bitget error HTTP {response.status_code}: {parsed.code} {parsed.msg}")
        return parsed

    @retry(
        retry=retry_if_exception_type((RequestException, BitgetAPIError)),
        wait=wait_exponential(multiplier=0.8, min=1, max=8),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        auth: bool = False,
        timeout: int = 10,
    ) -> BitgetResponse:
        params = params or {}
        body = body or {}
        query = urlencode(sorted((k, v) for k, v in params.items() if v is not None))
        body_json = json.dumps(body, separators=(",", ":"), ensure_ascii=False) if body else ""
        url = f"{self.config.bitget_base_url}{path}"
        self.limiter.wait()
        try:
            response = self.session.request(
                method=method.upper(),
                url=url,
                params=params if params else None,
                data=body_json if body_json else None,
                headers=self._headers(method, path, query, body_json, auth),
                timeout=timeout,
            )
            parsed = self._parse_response(response)
            self.failure_count = 0
            self.last_error = ""
            return parsed
        except Exception as exc:
            self.failure_count += 1
            self.last_error = str(exc)
            self.logger.warning("Fallo API Bitget (%s): %s", self.failure_count, sanitize(str(exc)))
            raise

    def public_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self.request("GET", path, params=params, auth=False).data

    def private_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self.request("GET", path, params=params, auth=True).data

    def private_post(self, path: str, body: dict[str, Any]) -> Any:
        return self.request("POST", path, body=body, auth=True).data

    def get_contracts(self, symbol: str | None = None) -> list[dict[str, Any]]:
        params = {"productType": self.config.product_type}
        if symbol:
            params["symbol"] = symbol
        data = self.public_get("/api/v2/mix/market/contracts", params=params)
        return data or []

    def get_ticker(self, symbol: str) -> dict[str, Any]:
        data = self.public_get(
            "/api/v2/mix/market/ticker",
            params={"productType": self.config.product_type, "symbol": symbol},
        )
        if isinstance(data, list):
            return data[0] if data else {}
        return data or {}

    def get_candles(self, symbol: str, granularity: str, limit: int = 200) -> list[list[str]]:
        return self.public_get(
            "/api/v2/mix/market/candles",
            params={
                "symbol": symbol,
                "granularity": granularity,
                "limit": min(limit, 1000),
                "productType": self.config.product_type,
            },
        ) or []

    def get_account(self, symbol: str = "BTCUSDT") -> dict[str, Any]:
        return self.private_get(
            "/api/v2/mix/account/account",
            params={
                "symbol": symbol,
                "productType": self.config.product_type,
                "marginCoin": self.config.margin_coin,
            },
        ) or {}

    def get_margin_mode(
        self,
        symbol: str,
        product_type: str = "USDT-FUTURES",
        margin_coin: str = "USDT",
    ) -> str:
        account = self.private_get(
            "/api/v2/mix/account/account",
            params={"symbol": symbol, "productType": product_type, "marginCoin": margin_coin.upper()},
        ) or {}
        return str(account.get("marginMode", "")).lower()

    def set_margin_mode_isolated(
        self,
        symbol: str,
        product_type: str = "USDT-FUTURES",
        margin_coin: str = "USDT",
    ) -> dict[str, Any]:
        return self.private_post(
            "/api/v2/mix/account/set-margin-mode",
            {
                "symbol": symbol,
                "productType": product_type,
                "marginCoin": margin_coin.upper(),
                "marginMode": "isolated",
            },
        ) or {}

    def set_auto_margin_off(self, symbol: str, hold_side: str) -> dict[str, Any]:
        return self.private_post(
            "/api/v2/mix/account/set-auto-margin",
            {
                "symbol": symbol,
                "autoMargin": "off",
                "marginCoin": self.config.margin_coin.upper(),
                "holdSide": hold_side.lower(),
            },
        ) or {}

    def ensure_isolated_margin(self, symbol: str, side: str) -> dict[str, Any]:
        if self.config.margin_mode != "isolated":
            raise BitgetAPIError("Configuracion insegura: margin_mode no es isolated.")

        product_type = self.config.product_type
        margin_coin = self.config.margin_coin.upper()
        hold_side = "long" if side.upper() == "LONG" else "short"
        before = self.get_margin_mode(symbol, product_type, margin_coin)
        if before in {"cross", "crossed"}:
            self.logger.warning("%s estaba en margin mode %s; intentando cambiar a isolated.", symbol, before)
        if before != "isolated":
            self.set_margin_mode_isolated(symbol, product_type, margin_coin)

        after = self.get_margin_mode(symbol, product_type, margin_coin)
        if after != "isolated":
            raise BitgetAPIError(
                f"No se pudo verificar isolated margin para {symbol}. marginMode actual: {after or 'desconocido'}"
            )

        auto_margin_off = False
        if not self.config.auto_margin:
            self.set_auto_margin_off(symbol, hold_side)
            auto_margin_off = True

        return {
            "symbol": symbol,
            "marginModeBefore": before,
            "marginMode": after,
            "isolatedVerified": True,
            "autoMarginOff": auto_margin_off,
            "holdSide": hold_side,
        }

    def get_positions(self) -> list[dict[str, Any]]:
        return self.private_get(
            "/api/v2/mix/position/all-position",
            params={"productType": self.config.product_type, "marginCoin": self.config.margin_coin},
        ) or []

    def set_leverage(self, symbol: str, leverage: int, hold_side: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "symbol": symbol,
            "productType": self.config.product_type,
            "marginCoin": self.config.margin_coin,
            "leverage": str(leverage),
        }
        if hold_side:
            body["holdSide"] = hold_side.lower()
        return self.private_post("/api/v2/mix/account/set-leverage", body)

    def place_order(
        self,
        *,
        symbol: str,
        side: str,
        size: str,
        order_type: str,
        client_oid: str,
        trade_side: str = "open",
        price: str | None = None,
        reduce_only: bool = False,
        preset_stop_loss_price: str | None = None,
        preset_take_profit_price: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "symbol": symbol,
            "productType": self.config.product_type,
            "marginMode": self.config.margin_mode,
            "marginCoin": self.config.margin_coin,
            "size": size,
            "side": side.lower(),
            "tradeSide": trade_side,
            "orderType": order_type.lower(),
            "clientOid": client_oid,
            "reduceOnly": "YES" if reduce_only else "NO",
        }
        if order_type.lower() == "limit":
            body["price"] = price
            body["force"] = "gtc"
        if preset_stop_loss_price:
            body["presetStopLossPrice"] = preset_stop_loss_price
        if preset_take_profit_price:
            body["presetStopSurplusPrice"] = preset_take_profit_price
        return self.private_post("/api/v2/mix/order/place-order", body)

    def get_order_detail(self, symbol: str, order_id: str | None = None, client_oid: str | None = None) -> dict[str, Any]:
        params = {"symbol": symbol, "productType": self.config.product_type}
        if order_id:
            params["orderId"] = order_id
        if client_oid:
            params["clientOid"] = client_oid
        return self.private_get("/api/v2/mix/order/detail", params=params) or {}

    def place_tpsl_order(
        self,
        *,
        symbol: str,
        plan_type: str,
        trigger_price: str,
        hold_side: str,
        size: str | None,
        client_oid: str,
        execute_price: str = "0",
        trigger_type: str = "mark_price",
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "marginCoin": self.config.margin_coin,
            "productType": self.config.product_type,
            "symbol": symbol,
            "planType": plan_type,
            "triggerPrice": trigger_price,
            "triggerType": trigger_type,
            "executePrice": execute_price,
            "holdSide": hold_side.lower(),
            "clientOid": client_oid,
        }
        if size is not None:
            body["size"] = size
        return self.private_post("/api/v2/mix/order/place-tpsl-order", body)

    def get_pending_tpsl(self, symbol: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"productType": self.config.product_type, "planType": "profit_loss", "limit": "100"}
        if symbol:
            params["symbol"] = symbol
        return self.private_get("/api/v2/mix/order/orders-plan-pending", params=params) or {}

    def close_position_market(self, symbol: str, side: str, size: str, client_oid: str) -> dict[str, Any]:
        normalized_side = side.upper()
        if normalized_side not in {"LONG", "SHORT"}:
            raise ValueError(f"close_position_market side invalido: {side}. Debe ser LONG o SHORT.")
        hold_side = "long" if normalized_side == "LONG" else "short"
        # Bitget mix hedge mode uses side as position direction:
        # Close long: side=buy + tradeSide=close; close short: side=sell + tradeSide=close.
        # In one-way mode, reduceOnly=YES is the protection that prevents exposure increase.
        close_side = "buy" if hold_side == "long" else "sell"
        self.logger.info(
            "Close position market %s side=%s close_side=%s tradeSide=close reduceOnly=YES",
            symbol,
            normalized_side,
            close_side,
        )
        return self.place_order(
            symbol=symbol,
            side=close_side,
            size=size,
            order_type="market",
            trade_side="close",
            client_oid=client_oid,
            reduce_only=True,
        )

    def ping_private(self) -> bool:
        self.get_account("BTCUSDT")
        time.sleep(0.05)
        return True

    def safe_public_summary(self) -> str:
        return json_dumps({"base_url": self.config.bitget_base_url, "product_type": self.config.product_type})
