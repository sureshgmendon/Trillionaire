"""
Trillionaire Trading Engine - Risk Management Module
Institutional risk gatekeeper, dynamic position sizer, and intraday circuit breakers.
Exports PortfolioRiskGatekeeper.
"""

from trading_engine.risk.portfolio_risk import PortfolioRiskGatekeeper

__all__ = ["PortfolioRiskGatekeeper"]
