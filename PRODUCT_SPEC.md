# Product Spec — poly-kalshi-arb-bot

**Version:** 0.1.0  
**Date:** 2026-04-20  
**Status:** Draft

---

## 1. System Goal

Continuously scan equivalent binary-outcome markets on **Polymarket** and **Kalshi**, identify price discrepancies where a risk-free (or near-risk-free) profit exists after all costs, simulate both legs in paper-trading mode, alert via Discord, and log every decision with a reason code.

The bot must never trade when it cannot confirm exact market equivalence, and must never execute a live trade unless `LIVE_TRADING=true` is explicitly set in environment config.

---

## 2. Non-Goals

| Out of scope | Reason |
|---|---|
| Multi-leg / basket arbitrage | Complexity, correlation risk |
| Options, spreads, or non-binary contracts | Different payoff structures |
| Web scraping of any venue | Fragile; violates TOS |
| Fuzzy / probabilistic market equivalence | Too many false positives |
| Automated live trading in v0.1 | Paper-first discipline |
| WebSocket streaming in v0.1 | Polling is sufficient to validate the model |
| Portfolio optimization / Kelly sizing | Out of scope for MVP |
| Tax / accounting reporting | Out of scope |

---

## 3. Market Definitions

### 3.1 What Counts as a Market

A **market** is a binary prediction contract with exactly two outcomes: YES and NO, where:

- The contract settles to **$1.00** on the winning side and **$0.00** on the losing side.
- The contract has a defined, unambiguous resolution condition tied to a publicly verifiable event.
- The contract is currently **open** (accepting orders) and has not passed its resolution deadline.
- The contract has at least one resting offer on both the YES and NO sides of the order book.

**Polymarket market fields used:**
- `conditionId` — unique market identifier
- `question` — human-readable resolution question
- `endDate` — resolution deadline (UTC ISO-8601)
- `active` — must be `true`
- `closed` — must be `false`
- `tokens[YES].price` / `tokens[NO].price` — mid prices from CLOB
- `tokens[YES].bestAsk` / `tokens[NO].bestAsk` — top-of-book executable prices

**Kalshi market fields used:**
- `ticker` — unique market identifier
- `title` — human-readable resolution question
- `close_time` — resolution deadline (UTC ISO-8601)
- `status` — must be `"open"`
- `yes_ask` / `no_ask` — top-of-book executable prices (in cents, divided by 100)
- `yes_bid` / `no_bid` — top-of-book bid prices

### 3.2 What Does NOT Count as a Market

- Markets with `status != "open"` or `active == false`
- Markets past their `close_time` / `endDate`
- Markets with no resting liquidity on either side
- Markets whose resolution condition references a proprietary, non-public data source
- Markets that settle on a continuous (non-binary) variable

---

## 4. Equivalent Market Pair Rules

Two markets — one on Polymarket, one on Kalshi — are **equivalent** if and only if **all** of the following conditions hold simultaneously. A single failing condition produces an automatic rejection with the corresponding reason code.

| # | Rule | Rejection Code |
|---|---|---|
| 4.1 | **Same underlying event** — the resolution questions, when normalized (lowercased, punctuation stripped, common synonyms unified), refer to the same real-world outcome | `EQUIV_EVENT_MISMATCH` |
| 4.2 | **Same resolution direction** — YES on Polymarket and YES on Kalshi both pay out under the same scenario; if they are inverted, the bot must explicitly note the inversion and flip the Kalshi leg | `EQUIV_DIRECTION_MISMATCH` |
| 4.3 | **Deadline alignment** — resolution deadlines are within **24 hours** of each other | `EQUIV_DEADLINE_GAP` |
| 4.4 | **Same resolution authority** — both contracts resolve using the same or an equivalent authoritative public source (e.g., both use official election results, same agency data release) | `EQUIV_RESOLVER_MISMATCH` |
| 4.5 | **No conditional clauses** — neither market has an additional condition that the other does not (e.g., "if the game is not cancelled") | `EQUIV_CONDITIONAL_MISMATCH` |
| 4.6 | **Exact match confidence** — the matcher assigns confidence `EXACT` (see Section 5). Any other confidence level (PROBABLE, POSSIBLE, UNKNOWN) is rejected | `EQUIV_CONFIDENCE_NOT_EXACT` |

Matching is performed by exact string normalization first, then a mandatory human-review allowlist for any pair that has never been seen before. The bot will never auto-approve a new pair without a pre-approved entry in `config/approved_pairs.json`.

---

## 5. Matcher Confidence Levels

| Level | Meaning | Tradeable |
|---|---|---|
| `EXACT` | Normalized question strings match AND deadline within 24 h AND same resolver | YES |
| `PROBABLE` | Question strings match after synonym expansion but deadline or resolver uncertain | NO — logged as `REJECTED` |
| `POSSIBLE` | Partial keyword overlap | NO — discarded silently |
| `UNKNOWN` | No meaningful overlap | NO — discarded silently |

---

## 6. Pricing and Net-Edge Formulas

### 6.1 Symbols

| Symbol | Definition |
|---|---|
| `P_yes_ask` | Polymarket best ask for YES leg (the price to buy YES) |
| `K_no_ask` | Kalshi best ask for NO leg (the price to buy NO), scaled to [0, 1] |
| `F_poly` | Polymarket taker fee rate (currently 0% for most markets; use 0.0 unless env overrides) |
| `F_kalshi` | Kalshi taker fee rate (currently 2% of notional; configurable) |
| `SLIP_poly` | Polymarket slippage buffer (configurable, default 0.005 = 0.5¢) |
| `SLIP_kalshi` | Kalshi slippage buffer (configurable, default 0.005 = 0.5¢) |
| `STALE_PENALTY` | Added cost per leg when price data age exceeds `MAX_PRICE_AGE_S` (default 0.005) |

### 6.2 Opportunity Leg Structure

The strategy is to buy YES on one venue and NO on the other, such that exactly one leg always pays $1.00 regardless of the outcome, creating a guaranteed payoff of $1.00 against a total cost less than $1.00.

**Direction A** — Buy YES on Polymarket, Buy NO on Kalshi:

```
total_cost_A = (P_yes_ask * (1 + F_poly) + SLIP_poly) + (K_no_ask * (1 + F_kalshi) + SLIP_kalshi)
gross_edge_A = 1.00 - total_cost_A
```

**Direction B** — Buy NO on Polymarket, Buy YES on Kalshi:

```
P_no_ask   = best ask for NO on Polymarket
K_yes_ask  = best ask for YES on Kalshi

total_cost_B = (P_no_ask * (1 + F_poly) + SLIP_poly) + (K_yes_ask * (1 + F_kalshi) + SLIP_kalshi)
gross_edge_B = 1.00 - total_cost_B
```

### 6.3 Stale Data Penalty

If the timestamp of either price quote is older than `MAX_PRICE_AGE_S` seconds (default: 30):

```
stale_legs = number of legs with stale data   # 0, 1, or 2
adjusted_edge = gross_edge - (stale_legs * STALE_PENALTY)
```

### 6.4 Net Edge

```
net_edge = max(gross_edge_A, gross_edge_B) - (stale_legs * STALE_PENALTY)
```

The **direction** with the higher gross edge is the one that gets evaluated and potentially traded.

### 6.5 Minimum Edge Threshold

```
NET_EDGE_MIN = 0.02   # $0.02 minimum; configurable via env
```

An opportunity is **tradeable** only if `net_edge >= NET_EDGE_MIN`.

---

## 7. Opportunity Classification

| Classification | Condition | Action |
|---|---|---|
| `TRADEABLE` | Equivalent pair confirmed + `net_edge >= NET_EDGE_MIN` | Execute both legs (paper or live), send Discord alert, log |
| `REJECTED_EDGE` | Equivalent pair confirmed + `net_edge < NET_EDGE_MIN` | Log with reason code, no alert |
| `REJECTED_EQUIV` | Any equivalence rule fails | Log with reason code, no alert |
| `REJECTED_STALE` | `net_edge >= NET_EDGE_MIN` before stale penalty but not after | Log with reason code, no alert |
| `REJECTED_DEPTH` | Insufficient book depth to fill `TRADE_SIZE_USDC` without crossing next price level | Log with reason code, no alert |
| `REJECTED_RISK` | Would breach a portfolio risk limit | Log with reason code, no alert |

---

## 8. Book Depth Check

Before classifying an opportunity as `TRADEABLE`, the bot must confirm that the required position size can be filled at the quoted ask without moving to the next price level.

```
TRADE_SIZE_USDC = configured notional per trade (default: $10.00)

poly_contracts_needed  = TRADE_SIZE_USDC / P_ask_leg
kalshi_contracts_needed = TRADE_SIZE_USDC / K_ask_leg

# Both must be satisfiable at the top-of-book level
if poly_available_at_ask < poly_contracts_needed:
    reject → REJECTED_DEPTH
if kalshi_available_at_ask < kalshi_contracts_needed:
    reject → REJECTED_DEPTH
```

If depth data is unavailable, treat as `REJECTED_DEPTH` (fail safe).

---

## 9. Paper Trading Assumptions

- Both legs are simulated immediately at the ask prices observed at decision time.
- No partial fills. The full `TRADE_SIZE_USDC` is assumed to execute.
- P&L is tracked in a local SQLite log (see Section 12).
- Settlement is simulated when either market's `close_time` / `endDate` passes and the bot can read the final resolution from the API.
- Paper-mode P&L = sum of settled positions. Positions that have not settled yet are marked as open.
- Simulated fills do not affect real order books.
- Paper mode is the default. Live mode requires `LIVE_TRADING=true` in `.env`.

---

## 10. Risk Limits

All limits are configurable via environment variables or `config/risk.json`.

| Limit | Default | Env Variable | Behavior on breach |
|---|---|---|---|
| Max notional per trade | $10.00 | `TRADE_SIZE_USDC` | Hard cap — do not exceed |
| Max open positions | 5 | `MAX_OPEN_POSITIONS` | Reject new opportunities → `REJECTED_RISK` |
| Max notional deployed | $50.00 | `MAX_DEPLOYED_USDC` | Reject new opportunities → `REJECTED_RISK` |
| Max loss per day (paper) | $20.00 | `MAX_DAILY_LOSS_USDC` | Halt bot for remainder of UTC day, alert Discord |
| Min net edge | $0.02 | `NET_EDGE_MIN` | Reject → `REJECTED_EDGE` |
| Max price age | 30 s | `MAX_PRICE_AGE_S` | Apply stale penalty per leg |
| Polling interval | 60 s | `POLL_INTERVAL_S` | Configurable floor; do not go below 10 s |

If any risk parameter cannot be loaded from config, the bot must refuse to start.

---

## 11. Discord Alert Format

Alerts are sent only for `TRADEABLE` opportunities and for risk-limit halt events.

### 11.1 Tradeable Opportunity Alert

```
[PAPER] ARB OPPORTUNITY FOUND
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Market      : <normalized question>
Direction   : BUY YES on <venue> / BUY NO on <venue>
Poly price  : <ask> (<YES|NO>)
Kalshi price: <ask> (<YES|NO>)
Net edge    : $<X.XX>
Trade size  : $<TRADE_SIZE_USDC>
Est. profit : $<net_edge * TRADE_SIZE_USDC>
Poly mkt    : <conditionId>
Kalshi mkt  : <ticker>
Timestamp   : <UTC ISO-8601>
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

- Replace `[PAPER]` with `[LIVE]` when `LIVE_TRADING=true`.
- All prices formatted to 4 decimal places.
- Dollar amounts to 2 decimal places.

### 11.2 Risk Halt Alert

```
[RISK HALT] Bot paused — daily loss limit reached
Limit    : $<MAX_DAILY_LOSS_USDC>
Realized : $<current_daily_loss>
Resumes  : next UTC midnight
Timestamp: <UTC ISO-8601>
```

### 11.3 Startup / Shutdown Alerts

- On startup: `[BOT START] poly-kalshi-arb-bot v0.1.0 | mode: PAPER | poll: <N>s`
- On clean shutdown: `[BOT STOP] Uptime: <HH:MM:SS> | Trades executed: <N> | P&L: $<X.XX>`

---

## 12. Persistence and Logging Plan

### 12.1 Structured Decision Log (structlog → stdout + file)

Every market evaluation produces one log line in JSON format containing:

```json
{
  "ts": "<UTC ISO-8601>",
  "event": "<TRADEABLE | REJECTED_EDGE | REJECTED_EQUIV | ...>",
  "reason_code": "<specific code from Section 7>",
  "poly_market_id": "<conditionId>",
  "kalshi_market_id": "<ticker>",
  "question_normalized": "<string>",
  "direction": "<A | B | null>",
  "poly_ask": <float>,
  "kalshi_ask": <float>,
  "gross_edge": <float>,
  "net_edge": <float>,
  "stale_penalty": <float>,
  "mode": "<paper | live>"
}
```

Log file rotates daily. Stored in `logs/decisions_<YYYYMMDD>.jsonl`.

### 12.2 Trade Log (SQLite)

File: `data/trades.db`

Table `trades`:

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PK | Auto-increment |
| `ts_entered` | TEXT | UTC ISO-8601 timestamp of fill simulation |
| `ts_settled` | TEXT | UTC ISO-8601 timestamp of settlement (null until resolved) |
| `mode` | TEXT | `paper` or `live` |
| `question` | TEXT | Normalized question |
| `poly_market_id` | TEXT | Polymarket conditionId |
| `kalshi_market_id` | TEXT | Kalshi ticker |
| `direction` | TEXT | `A` or `B` |
| `poly_side` | TEXT | `YES` or `NO` |
| `kalshi_side` | TEXT | `YES` or `NO` |
| `poly_price` | REAL | Executed ask price |
| `kalshi_price` | REAL | Executed ask price |
| `trade_size_usdc` | REAL | Notional per leg |
| `net_edge_at_entry` | REAL | Edge at time of decision |
| `pnl` | REAL | Realized P&L (null until settled) |
| `status` | TEXT | `open`, `settled`, `voided` |

Table `daily_summary` is updated at UTC midnight with aggregate P&L.

---

## 13. Test Plan

Each module must have a corresponding test file under `tests/`. Tests run with `pytest` and must pass before any commit merges.

| Module | Test File | What to test |
|---|---|---|
| `config.py` | `test_config.py` | Missing required env vars raise errors; defaults load correctly; `LIVE_TRADING` gate works |
| `venues/polymarket.py` | `test_polymarket.py` | Market fetch, price parsing, stale detection; HTTP errors handled gracefully (mock via `respx`) |
| `venues/kalshi.py` | `test_kalshi.py` | Market fetch, price parsing (cents→float), stale detection; HTTP errors handled (mock via `respx`) |
| `matcher.py` | `test_matcher.py` | All 6 equivalence rules; each rejection code fires correctly; approved-pairs allowlist respected; direction inversion detected |
| `edge.py` | `test_edge.py` | Direction A and B formulas; stale penalty applied correctly; depth check rejects correctly; net_edge floor enforced |
| `executor.py` | `test_executor.py` | Paper fills recorded to DB; live mode gated behind flag; risk limits block execution; settled P&L computed correctly |
| `notifier.py` | `test_notifier.py` | Correct message format for each alert type; no alert sent for rejected opportunities; Discord webhook called once per opportunity |
| `logger.py` | `test_logger.py` | JSON log line contains all required fields; reason codes are valid enum values |
| `bot.py` | `test_bot.py` | Poll loop calls venues, matcher, edge, executor in correct order; halt on risk breach; clean shutdown |

**Coverage target:** 90% line coverage on all modules in `src/`.

All tests are run with `make test`. CI will enforce this on every PR.

---

## 14. Rollout Plan

### Phase 1 — Spec + Scaffold (current)
- [x] Write `PRODUCT_SPEC.md`
- [ ] Scaffold all module files with stubs and type signatures
- [ ] Set up `.env.example` with all required variables
- [ ] Set up `config/approved_pairs.json` schema

### Phase 2 — Venue Adapters
- [ ] Implement `venues/polymarket.py` — REST polling of CLOB/Gamma API
- [ ] Implement `venues/kalshi.py` — REST polling of official market-data API
- [ ] Tests pass for both adapters with mocked HTTP

### Phase 3 — Core Logic
- [ ] Implement `matcher.py` — equivalence rules, allowlist gate
- [ ] Implement `edge.py` — pricing formulas, depth check, stale penalty
- [ ] Implement `executor.py` — paper fill simulation, SQLite write
- [ ] Implement `notifier.py` — Discord webhook
- [ ] Implement `logger.py` — structlog JSON output
- [ ] All unit tests passing, 90%+ coverage

### Phase 4 — Integration
- [ ] Implement `bot.py` — polling loop wiring everything together
- [ ] End-to-end paper run for 24 hours minimum
- [ ] Review decision log for false positives
- [ ] Review Discord alerts for formatting

### Phase 5 — Live Trading Gate
- [ ] Audit all risk limits
- [ ] Confirm `LIVE_TRADING=true` path in `executor.py`
- [ ] Manual review of at least 10 paper trades before enabling live
- [ ] Live mode tested with minimum trade size ($1.00) first

---

## 15. Open Questions / Decisions Needed

| # | Question | Default assumption |
|---|---|---|
| Q1 | Does Polymarket charge taker fees? | Assume 0% until confirmed from official docs |
| Q2 | Kalshi fee structure for small accounts? | Assume 2% of notional per trade |
| Q3 | Approved pairs allowlist — manual seeding or automated suggestion + human approval? | Manual seeding for v0.1 |
| Q4 | What is the Polymarket rate limit for the CLOB API? | Assume conservative 10 req/min until confirmed |
| Q5 | What is the Kalshi rate limit for the market-data REST API? | Assume 10 req/min until confirmed |
| Q6 | Does the bot need to handle Kalshi markets denominated in non-USD? | No — USD only for v0.1 |
