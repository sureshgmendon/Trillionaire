"""
Trillionaire Sentinel - Pre-Market Headline LLM & Rule-Based Classifier
Classifies overnight financial news against the 25 NIFTY 50 universe stocks and their parent sectors.
Provides Gemini AI scoring with deterministic rule-based regex fallback for sub-second, resilient triage.
Propagates sector contagion (e.g., US tech/NVIDIA shock -> NIFTY IT constituents).
"""

from datetime import datetime
import json
import re
from typing import Dict, List, Optional
import httpx
from loguru import logger

from config.settings import settings
from sentinel.news_triage.news_fetcher import NewsItem
from shared.constants import IST_TZ
from shared.enums import SentinelStatus
from shared.schemas import NewsSentinelTag

# Critical keyword triggers for severe idiosyncratic risk (DO_NOT_TRADE / 0.0x)
SEVERE_FRAUD_KEYWORDS = [
    r"\bfraud\b",
    r"\bcbi\b",
    r"\bsebi (?:probe|investigation|ban|order)\b",
    r"\braid[s]?\b",
    r"\barrest[s]?\b",
    r"\bforensic audit\b",
    r"\bdefault(?:ed)?\b",
    r"\bbankruptcy\b",
    r"\bhindenburg\b",
    r"\bmoney laundering\b",
    r"\baccounting scandal\b",
    r"\bwhistleblower\b",
]

# Moderate caution keywords (CAUTION_HALF_SIZE / 0.5x)
MODERATE_RISK_KEYWORDS = [
    r"\bdowngrade[d]?\b",
    r"\bprofit warning\b",
    r"\bmiss(?:es|ed) estimates\b",
    r"\bweak guidance\b",
    r"\bblock deal\b",
    r"\bpromoter (?:stake sale|selling)\b",
    r"\bresign(?:s|ed|ation)\b",
    r"\btax notice\b",
    r"\bantitrust\b",
    r"\bpenalty\b",
    r"\bfine[d]?\b",
]

# Sector contagion trigger patterns
SECTOR_CONTAGION_RULES = [
    {
        "sector": "NIFTY IT",
        "patterns": [
            r"\bnvidia\b",
            r"\bnasdaq (?:crash|plunge|slump|tumble|selloff|rout)\b",
            r"\bsemiconductor (?:rout|slump|selloff|tumble)\b",
            r"\bus tech (?:rout|slump|selloff|plunge)\b",
            r"\btech (?:selloff|rout|crash|plunge)\b",
            r"\bchip (?:shortage|stocks tumble)\b",
        ],
        "status": SentinelStatus.CAUTION_HALF_SIZE,
        "multiplier": 0.5,
        "category": "Global tech/semiconductor volatility shock",
    },
    {
        "sector": "NIFTY BANK",
        "patterns": [
            r"\bus (?:regional )?bank(?:ing)? crisis\b",
            r"\bbank run\b",
            r"\bcredit rating downgrade bank\b",
            r"\brbi penalty\b",
            r"\bnpas surge\b",
        ],
        "status": SentinelStatus.CAUTION_HALF_SIZE,
        "multiplier": 0.5,
        "category": "Systemic banking sector volatility shock",
    },
    {
        "sector": "NIFTY ENERGY",
        "patterns": [
            r"\bcrude (?:surges|spikes|jumps|plunges|crashes)\b",
            r"\boil (?:spikes|surges|shock|plunges)\b",
            r"\bopec\+? (?:surprise|cut|shock)\b",
        ],
        "status": SentinelStatus.CAUTION_HALF_SIZE,
        "multiplier": 0.5,
        "category": "Global crude & energy market shock",
    },
    {
        "sector": "NIFTY AUTO",
        "patterns": [
            r"\bauto (?:tariff|sales slump|slowdown)\b",
            r"\bev subsidy cut\b",
        ],
        "status": SentinelStatus.CAUTION_HALF_SIZE,
        "multiplier": 0.5,
        "category": "Automotive macro demand/tariff shock",
    },
]


class PreMarketNewsClassifier:
    """Evaluates pre-market news items against universe constituents with sector contagion."""

    def __init__(self):
        self._load_sector_mappings()

    def _load_sector_mappings(self) -> None:
        """Loads stock-to-sector and sector-to-stocks mappings from sector_map.yaml."""
        sector_data = settings.load_sector_map()
        self.stocks = [s["tradingsymbol"] for s in sector_data.get("stocks", [])]
        self.stock_to_sector: Dict[str, str] = {
            s["tradingsymbol"]: s["parent_index"] for s in sector_data.get("stocks", [])
        }
        self.sector_to_stocks: Dict[str, List[str]] = {}
        for s in sector_data.get("stocks", []):
            self.sector_to_stocks.setdefault(s["parent_index"], []).append(s["tradingsymbol"])

    HIGH_SEVERITY_PATTERNS = [
        re.compile(p, re.IGNORECASE) for p in [
            r"\bsebi\b", r"\braid[s]?\b", r"\bfraud\b", r"\bdefault\b",
            r"\bban\b", r"\bmerger\b", r"\bearnings\b", r"\bresults\b",
            r"\brbi\b", r"\bdowngrade\b", r"\bcrash\b"
        ]
    ]

    async def classify_news(
        self,
        news_items: List[NewsItem],
        is_premarket: bool = True,
    ) -> Dict[str, NewsSentinelTag]:
        """
        Classifies news items for all 25 universe stocks.
        Zero-token efficiency gate: only invokes Gemini during premarket triage (08:30) or
        when high-severity keywords match ('SEBI', 'raid', 'fraud', 'default', 'ban', 'merger',
        'earnings', 'results', 'RBI', 'downgrade', 'crash'). Otherwise executes deterministic regex.
        """
        has_severe_headline = any(
            any(pat.search(f"{item.headline} {item.summary}") for pat in self.HIGH_SEVERITY_PATTERNS)
            for item in news_items
        )

        should_call_gemini = bool(settings.gemini_api_key) and (is_premarket or has_severe_headline)

        if should_call_gemini:
            try:
                return await self._classify_via_gemini(news_items)
            except Exception as e:
                logger.warning(f"Gemini API triage failed or timed out: {e}. Executing rule-based fallback.")

        return self.classify_rule_based(news_items)

    def classify_rule_based(self, news_items: List[NewsItem]) -> Dict[str, NewsSentinelTag]:
        """
        Deterministic, sub-second regex headline classifier.
        Applies idiosyncratic stock checks and propagates sector contagion.
        """
        now = datetime.now(IST_TZ)
        results: Dict[str, NewsSentinelTag] = {}

        # Default clean state for all universe stocks
        for sym in self.stocks:
            results[sym] = NewsSentinelTag(
                tradingsymbol=sym,
                status=SentinelStatus.NORMAL,
                reason="Overnight cues neutral / no adverse headlines",
                risk_multiplier=1.0,
                updated_at_ist=now,
            )

        if not news_items:
            return results

        # Process each headline
        for item in news_items:
            text = f"{item.headline} {item.summary}".lower()

            # 1. Check Sector Contagion
            for rule in SECTOR_CONTAGION_RULES:
                sector = rule["sector"]
                for pattern in rule["patterns"]:
                    if re.search(pattern, text):
                        affected_stocks = self.sector_to_stocks.get(sector, [])
                        for sym in affected_stocks:
                            self._apply_conservative_tag(
                                results,
                                sym=sym,
                                status=rule["status"],
                                multiplier=rule["multiplier"],
                                reason=f"{rule['category']}: {item.headline}",
                                updated_at=now,
                            )
                        break

            # 2. Check Specific Stock Idiosyncratic Headlines
            for sym in self.stocks:
                sym_pattern = r"\b" + re.escape(sym.lower()) + r"\b"
                # Also check common company aliases (e.g. Reliance, Tata Motors, HDFC Bank, Infosys)
                alias_pattern = self._get_company_alias_pattern(sym)

                if re.search(sym_pattern, text) or (alias_pattern and re.search(alias_pattern, text)):
                    # Check for Severe Fraud / Legal / Regulatory keywords
                    is_severe = any(re.search(pat, text) for pat in SEVERE_FRAUD_KEYWORDS)
                    if is_severe:
                        self._apply_conservative_tag(
                            results,
                            sym=sym,
                            status=SentinelStatus.DO_NOT_TRADE,
                            multiplier=0.0,
                            reason=f"Severe regulatory/fraud alert: {item.headline}",
                            updated_at=now,
                        )
                        continue

                    # Check for Moderate Risk / Downgrade keywords
                    is_moderate = any(re.search(pat, text) for pat in MODERATE_RISK_KEYWORDS)
                    if is_moderate:
                        self._apply_conservative_tag(
                            results,
                            sym=sym,
                            status=SentinelStatus.CAUTION_HALF_SIZE,
                            multiplier=0.5,
                            reason=f"Company-specific risk alert: {item.headline}",
                            updated_at=now,
                        )

        return results

    def _apply_conservative_tag(
        self,
        results: Dict[str, NewsSentinelTag],
        sym: str,
        status: SentinelStatus,
        multiplier: float,
        reason: str,
        updated_at: datetime,
    ) -> None:
        """Updates symbol risk tag ONLY if new tag is strictly more conservative (lower multiplier)."""
        existing = results.get(sym)
        if existing is None or multiplier < existing.risk_multiplier:
            results[sym] = NewsSentinelTag(
                tradingsymbol=sym,
                status=status,
                reason=reason,
                risk_multiplier=multiplier,
                updated_at_ist=updated_at,
            )

    @staticmethod
    def _get_company_alias_pattern(sym: str) -> Optional[str]:
        """Provides natural language company aliases."""
        aliases = {
            "RELIANCE": r"\breliance\b",
            "TCS": r"\btcs\b|\btata consultancy\b",
            "INFY": r"\binfosys\b|\binfy\b",
            "HDFCBANK": r"\bhdfc bank\b",
            "ICICIBANK": r"\bicici bank\b",
            "SBIN": r"\bstate bank of india\b|\bsbi\b",
            "TATAMOTORS": r"\btata motors\b",
            "TMPV": r"\btata motors\b|\btmpv\b",
            "M&M": r"\bmahindra\b|\bm&m\b",
            "SUNPHARMA": r"\bsun pharma\b",
            "BHARTIARTL": r"\bharti airtel\b|\bairtel\b",
            "ITC": r"\bitc\b",
            "HINDUNILVR": r"\bhul\b|\bhindustan unilever\b",
            "LT": r"\blarsen\b|\bl&t\b",
        }
        return aliases.get(sym)

    async def _classify_via_gemini(self, news_items: List[NewsItem]) -> Dict[str, NewsSentinelTag]:
        """Queries Google Gemini API (gemini-3.8-flash -> gemini-2.5-pro) for semantic news risk classification."""
        from sentinel.news_triage.gemini_client import call_gemini_async

        now = datetime.now(IST_TZ)

        headlines_text = "\n".join(f"- {n.headline}: {n.summary}" for n in news_items)
        stocks_list = ", ".join(self.stocks)

        prompt = f"""
You are an institutional pre-market risk officer for an automated Indian intraday trading engine.
Analyze these pre-market news items against the following 25 NSE NIFTY 50 equities:
{stocks_list}

News Headlines:
{headlines_text}

Rules:
1. If a stock or its parent sector has severe fraud, SEBI/CBI probe, or accounting scandal: status="DO_NOT_TRADE", multiplier=0.0.
2. If high volatility, earnings warning, rating downgrade, or global sector shock (e.g. Nasdaq/Nvidia slump affecting NIFTY IT): status="CAUTION_HALF_SIZE", multiplier=0.5.
3. Otherwise: status="NORMAL", multiplier=1.0.

Respond strictly with valid JSON format:
{{
  "evaluations": [
    {{"tradingsymbol": "SYMBOL", "status": "NORMAL|CAUTION_HALF_SIZE|DO_NOT_TRADE", "risk_multiplier": 1.0, "reason": "brief reason"}}
  ]
}}
"""
        ai_res = await call_gemini_async(
            parts=[{"text": prompt}],
            model_candidates=("gemini-3.8-flash", "gemini-2.5-pro"),
            timeout_sec=8.0,
            api_key=settings.gemini_api_key,
        )
        if not ai_res.get("ok") or not isinstance(ai_res.get("data"), dict):
            raise RuntimeError(str(ai_res.get("error") or "Gemini classification failed"))
        parsed = ai_res["data"]

        results: Dict[str, NewsSentinelTag] = {}
        # Fill defaults
        for sym in self.stocks:
            results[sym] = NewsSentinelTag(
                tradingsymbol=sym,
                status=SentinelStatus.NORMAL,
                reason="Overnight cues neutral",
                risk_multiplier=1.0,
                updated_at_ist=now,
            )

        for ev in parsed.get("evaluations", []):
            sym = ev.get("tradingsymbol")
            if sym in results:
                status = SentinelStatus(ev.get("status", "NORMAL"))
                mult = float(ev.get("risk_multiplier", 1.0))
                reason = ev.get("reason", "Gemini evaluation")
                results[sym] = NewsSentinelTag(
                    tradingsymbol=sym,
                    status=status,
                    reason=reason,
                    risk_multiplier=mult,
                    updated_at_ist=now,
                )

        return results
