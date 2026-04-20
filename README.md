# poly-kalshi-arb-bot

Arbitrage bot that finds and executes (paper or live) equivalent markets across Polymarket and Kalshi.

## Rules

- Paper-trading only unless `LIVE_TRADING=true`
- Only trades exact equivalent markets
- Minimum net edge $0.02 after fees and slippage
- Polling-based; WebSocket support is a future milestone

## Setup

```bash
cp .env.example .env
# fill in your API keys
pip install -e ".[dev]"
```

## Usage

```bash
make run          # paper mode
make run-live     # live mode (requires LIVE_TRADING=true in .env)
make test         # run tests
make lint         # run linters
```

## Architecture

```
src/
  config.py          # loads and validates env/config
  venues/
    polymarket.py    # Polymarket adapter
    kalshi.py        # Kalshi adapter
  matcher.py         # market equivalence matching
  edge.py            # edge calculation after fees/slippage
  executor.py        # order execution (paper + live)
  notifier.py        # Discord notifications
  logger.py          # structured trade decision logging
  bot.py             # main polling loop
tests/
  test_edge.py
  test_matcher.py
  test_executor.py
  test_notifier.py
  test_config.py
```

## Environment Variables

See `.env.example` for all required and optional variables.
