# Trillionaire Web Dashboard (Cockpit)

> **Sprint 9 Initialization Placeholder**

The Trillionaire Next.js Web Cockpit provides real-time portfolio telemetry, cluster regime visualization, Sentinel risk radar, and manual operator overrides.

## Tech Stack (Sprint 9 Planned)
- **Framework**: Next.js 14+ (App Router, React Server Components)
- **Styling**: Tailwind CSS + Shadcn UI
- **State & Streaming**: TanStack Query + WebSocket streaming from `api_bridge`
- **Charts**: Lightweight Charts (TradingView) for sub-second candle/tick rendering
- **Authentication**: JWT / Secure Session cookies

## Monorepo Integration
- Connects to `api_bridge` REST endpoints (`http://localhost:8000/api/v1`)
- Streams real-time portfolio, order book, and sentinel alerts over WebSocket (`ws://localhost:8000/ws/telemetry`)
