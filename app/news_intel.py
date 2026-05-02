from __future__ import annotations

from dataclasses import dataclass, field

try:
    import requests
except ImportError:  # pragma: no cover - production installs requests from requirements.txt
    requests = None  # type: ignore

from .config import BotConfig


@dataclass
class NewsIntelResult:
    enabled: bool
    block_trading: bool = False
    reduce_risk: bool = False
    warnings: list[str] = field(default_factory=list)
    context: list[str] = field(default_factory=list)


class NewsIntel:
    def __init__(self, config: BotConfig, logger) -> None:
        self.config = config
        self.logger = logger

    def check(self) -> NewsIntelResult:
        if not self.config.enable_news_intel:
            return NewsIntelResult(enabled=False)
        result = NewsIntelResult(enabled=True)
        if requests is None:
            result.reduce_risk = True
            result.warnings.append("requests no instalado; news intel no puede verificar fuentes")
            return result
        if not self.config.news_api_key:
            result.reduce_risk = True
            result.warnings.append("NEWS_INTEL activo sin NEWS_API_KEY; reduciendo riesgo por falta de verificación")
            return result
        try:
            response = requests.get(
                "https://newsapi.org/v2/everything",
                params={
                    "q": "crypto OR bitcoin OR ethereum hack OR liquidation OR SEC OR ETF",
                    "sortBy": "publishedAt",
                    "pageSize": 10,
                    "apiKey": self.config.news_api_key,
                },
                timeout=8,
            )
            response.raise_for_status()
            articles = response.json().get("articles", [])
            high_risk_terms = {"hack", "exploit", "lawsuit", "ban", "liquidation", "bankruptcy"}
            for article in articles:
                title = (article.get("title") or "").lower()
                if any(term in title for term in high_risk_terms):
                    result.reduce_risk = True
                    result.warnings.append(f"Noticia de riesgo: {article.get('title')}")
            result.context = [article.get("title", "") for article in articles[:3] if article.get("title")]
        except Exception as exc:
            result.reduce_risk = True
            result.warnings.append("No se pudo verificar noticias; reduciendo riesgo")
            self.logger.warning("News intel falló: %s", exc)
        return result
