# Pump.fun Replay Sprint — Paper Bot

[![make.fun](https://img.shields.io/badge/make.fun-bounty-blue)](https://make.fun/bounty/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A paper trading bot for make.fun's **Pump.fun Replay Sprint — Paper Bot, Real Ranking** bounty. It trades 10 SOL of paper money on a replay of recorded pump.fun activity on Solana. Nothing is traded for real.

**Bounty:** https://make.fun/bounty/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122
**Referral:** `jwvw22pf`

## Strategy: Big-Launch Rider

The dev window (1,222 coins, 24h) says the first pump happens inside a coin's first 2 slots — before any order can fill (2-slot latency) — so chasing visible moves loses money. What survives is buying the **strongest early pumps**: a coin whose price already ran 2.2x–3.5x in its first 1–3 slots on real volume (≥ 7 SOL of real reserves) keeps running often enough to pay. Winners and dumps look identical at entry, so the edge is in the exit:

- **Entry:** age 1–3 slots, real SOL ≥ 7, price 2.2x–3.5x of create price, creator bought ≤ 2.5 SOL, no creator selling. Stake up to 0.25 SOL (capped at 1% of real reserves).
- **Exits:** bank half at **+20%**, let the rest ride to +100% or a trailing stop; **exit the moment the creator sells** (that signal alone is worth ~0.2 SOL); −15% stop-loss; real-SOL drawdown exit; max hold 2,000 slots.
- **Slippage:** tight 200 bps buy slippage is a free limit order — fills whose price ran away are rejected at no cost.
- The fat tail pays: 7 slow completers run to 14.7x; the bot catches 4–5 of them. Win rate is only ~11%, but the average win is ~10x the average loss.

## Results (dev window, `grade-local --driver process`)

| Metric | Value |
|---|---|
| Net PnL | **+0.318 SOL** (+3.18%) |
| 90% interval | −0.192 … +0.877 SOL |
| Net PnL at 2x fees | +0.129 SOL |
| Max drawdown | 0.038 (limit 0.50) |
| Win rate | 11.4% |
| Round trips / fills | 166 / 339 |
| Distinct mints | 61 (≥ 20 required) |
| Guardrails | **compliant** |
| Determinism | L1 regrade reproduced byte-for-byte (2 runs, identical `result_sha256`) |

Local sim (own Python port of the kit scorer, validated to 9 decimals against `grade-local` on the house equal-weight baseline): +0.419 SOL.

## Run it

```bash
python3 -P -s main.py   # the harness calls create_bot() / on_batch via vendor/makefun_bot.py
```

Deterministic by construction: no clock, no randomness, iteration only over insertion-ordered dicts/lists fed by the batch stream. `vendor/makefun_bot.py` is the kit's Python SDK, vendored as-is.

## License

MIT — see [LICENSE](LICENSE).
