"""
Trillionaire Trading Engine - Execution Module
SEBI-compliant order management, rate limiting, trailing stop brackets, and paper broker simulation.
Exports OrderManager and PaperBroker.
"""

from trading_engine.execution.order_manager import OrderManager
from trading_engine.execution.paper_broker import PaperBroker

__all__ = ["OrderManager", "PaperBroker"]
