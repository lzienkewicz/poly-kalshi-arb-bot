# Arb bot project rules

- This project is paper-trading only unless LIVE_TRADING=true.
- Use official APIs only. Do not scrape websites.
- Only trade exact equivalent markets.
- Minimum net edge is $0.02 after fees, slippage buffers, and stale-data penalties.
- Terminal runtime only.
- Send Discord notifications for qualified opportunities.
- Simulate both legs automatically in paper mode.
- Polling first. WebSocket support comes later.
- Every module must include tests.
- Never store secrets in code.
- Every trading decision must be logged with a reason code.
- If market equivalence confidence is not exact, reject.
- Respect per-trade and portfolio risk caps from config.
- Keep venue adapters isolated because upstream APIs may change.
