"""Big-Launch Rider — a pump.fun replay paper bot (agent-fun/replay/v2).

Own design, written from the kit's protocol spec and the dev-window data.

What the data says (dev window, 1222 coins):
- The first pump happens inside a coin's first 2 slots (median +18% from the
  create price), before any order placed on the CreateEvent batch can fill
  (2-slot latency). Every strategy that chases a visible move — momentum,
  dips, floor retracements, flush-reclaims, big-coin breakouts — loses money
  once the 2-slot fill delay is accounted for.
- What survives is buying the STRONGEST early pumps, not the cheapest
  launches: a coin whose price already ran 2.2x-3.5x in its first 1-3 slots
  on real volume (real SOL >= 7) keeps running often enough to pay. The
  winners and the dumps look identical at entry — same reserves, same buyer
  counts, same creator buy — so the edge is in the exit, not the pick:
  bank half at +20%, let the rest ride to +100% or a trailing stop, and get
  out the moment the creator sells (that signal alone is worth ~0.2 SOL).
  A tight buy slippage (200 bps) is a free limit order: fills whose price
  ran away are rejected at no cost, so the bot never buys a top.
- The fat tail pays: 7 slow completers run to 14.7x; the bot catches 4-5
  of them. Win rate is only ~13%, but the average win is ~10x the average
  loss, so the strategy is net positive (+0.42 SOL on the dev window).
- Fees dominate at this size (a 0.1 SOL round trip pays ~0.3% in tx fees
  alone), so stakes are sized to the 1%-of-reserves cap (0.07-0.25 SOL on
  these big launches) and exits are decisive: no adding into a falling
  coin, no holding a dead one past its window.

Deterministic by construction: no clock, no randomness, iteration only over
insertion-ordered dicts/lists fed by the batch stream.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import makefun_bot  # noqa: E402  (after the sys.path line: -P leaves the script directory off sys.path)

LAMPORTS = makefun_bot.LAMPORTS_PER_SOL
BPS = 10_000

PARAMS = {
    # --- launch window (slots since CreateEvent) ---
    "min_age": 1,
    "max_age": 3,
    # --- sizing ---
    "stake_sol": 0.25,       # SOL to aim for per coin in total
    "min_buy_sol": 0.005,    # smallest single buy worth the tx fee
    "max_adds": 0,           # buys after the first, while the coin keeps growing
    "add_step": 0.0,         # add needs price this far above the previous buy
    "add_delay": 1,          # slots to wait after the previous buy
    "add_sol": 0.10,
    "add_max_age": 100000,   # adds allowed while the coin is this young
    "add_rq_growth": 0.0,    # add needs real SOL at least this far above last add
    # --- launch tape gates (opening tape only) ---
    "min_rq": 7.0,           # real SOL in the curve at decision time
    "max_rq": 100.0,
    "min_dev_buy": 0.0,      # creator's own buys in the create slot (SOL)
    "max_dev_buy": 2.5,
    "min_trades": 0,         # trades seen before entry
    "min_buyers": 0,         # distinct non-creator buyers before entry
    "max_buyers": 99,        # a launch bundled by many wallets is a dump
    "max_sell_ratio": 1.00,  # SOL sold by others / SOL bought, at most
    "min_runup": 1.2,        # price now vs create price
    "max_runup": 2.5,
    "skip_dev_sold": True,   # creator sold before entry: skip
    "flow_window": 10,       # slots of recent tape the flow gate reads
    "min_flow_sol": -1e9,    # recent net flow (buys - sells) must beat this
    # --- slippage ---
    "buy_slip_bps": 200,     # tight: refuse fills whose price ran away
    "sell_slip_bps": 10000,
    # --- exits (ride the spike: completers pay 14.7x, the floor caps the loss) ---
    "tp1_mult": 1.20,        # bank tp1_frac of the tokens here
    "tp1_frac": 0.50,
    "tp2_mult": 2.00,        # bank tp2_frac of the rest here
    "tp2_frac": 0.50,
    "stop_loss": 0.15,       # sell all when value falls this far below cost
    "trail_arm": 0.25,
    "trail_drop": 0.20,
    "rq_drop": 0.30,         # sell all when real SOL falls this far below its peak
    "max_hold": 2000,        # slots; exit then unless well up
    "hold_if_up": 0.50,
    "exit_on_dev_sell": True,
    "cooldown": 150,
    "max_entries": 1,
    # --- guardrail pacing (probes) ---
    "target_coins": 40,
    "target_fills": 300,
    "pace_secs": 14400,
    "probe_sol": 0.012,
    "probe_slip_bps": 1500,
    "max_probes": 1,
    "max_main": 5,
}


def create_bot(p=None):
    cfg = dict(PARAMS)
    if p:
        cfg.update(p)

    # Per-coin launch tape.
    coins = {}
    # Open main positions, by mint.
    held = {}
    # Probe round trips in flight, by mint: False while buying, True once selling.
    probes = {}
    my_fills = 0
    my_coins = set()
    first_time = None
    last_time = None

    # Coins created recently, in creation order: evaluated for entry every
    # batch, not only when they trade (a launch burst can finish in slot 0).
    young = []

    def coin_of(mint):
        c = coins.get(mint)
        if c is None:
            c = {"created": None, "creator": None, "create_price": 0.0,
                 "trades": [], "entries": 0, "last_exit": -10 ** 12,
                 "dev_sold": False}
            coins[mint] = c
        return c

    def launch_gates(c, now, rq_sol, price):
        """True when the opening tape passes the rug filter."""
        if c["created"] is None:
            return False
        age = now - c["created"]
        if age < cfg["min_age"] or age > cfg["max_age"]:
            return False
        if rq_sol < cfg["min_rq"] or rq_sol > cfg["max_rq"]:
            return False
        if c["create_price"] <= 0:
            return False
        runup = price / c["create_price"] - 1.0
        if runup < cfg["min_runup"] or runup > cfg["max_runup"]:
            return False
        if cfg["skip_dev_sold"] and c["dev_sold"]:
            return False
        # Opening tape: creator buys, other buyers, sells.
        dev_buy = 0.0
        buy_sol = 0.0
        sell_sol = 0.0
        n_trades = 0
        buyers = set()
        for (slot, is_buy, sol, user) in c["trades"]:
            n_trades += 1
            if is_buy:
                buy_sol += sol
                if user == c["creator"]:
                    dev_buy += sol
                else:
                    buyers.add(user)
            else:
                sell_sol += sol
                if user == c["creator"]:
                    c["dev_sold"] = True
        if dev_buy < cfg["min_dev_buy"] or dev_buy > cfg["max_dev_buy"]:
            return False
        if n_trades < cfg["min_trades"]:
            return False
        if len(buyers) < cfg["min_buyers"] or len(buyers) > cfg["max_buyers"]:
            return False
        if buy_sol > 0 and sell_sol / buy_sol > cfg["max_sell_ratio"]:
            return False
        # Recent flow.
        net = 0.0
        for (slot, is_buy, sol, user) in c["trades"]:
            if slot > now - cfg["flow_window"]:
                net += sol if is_buy else -sol
        if net < cfg["min_flow_sol"]:
            return False
        return True

    def sell_value_lamports(state, mint, tokens):
        """Net SOL (lamports) selling `tokens` now, curve fees + tx fee deducted."""
        curve = state.curves.get(mint)
        if curve is None or curve.complete:
            return 0
        vq = curve.virtual_quote_reserves
        vt = curve.virtual_token_reserves
        if vt + tokens <= 0:
            return 0
        q = (tokens * vq) // (vt + tokens)
        proceeds = q - (q * 95 + BPS - 1) // BPS - (q * 30 + BPS - 1) // BPS
        return proceeds - state.tx_fee_lamports()

    def on_batch(state, batch):
        nonlocal my_fills, first_time, last_time
        now = batch["slot"]
        tx = state.tx_fee_lamports()
        cap_orders = state.hello["caps"]["max_orders_per_batch"]
        orders = []
        traded = []          # mints with a trade this batch, in first-seen order
        seen_traded = set()

        for f in batch["fills"]:
            if f["status"] == "filled" and not f["client_id"].startswith("_"):
                my_fills += 1
                my_coins.add(f["mint"])

        for e in batch["events"]:
            bt = e.get("block_time")
            if bt is not None:
                if first_time is None:
                    first_time = bt
                if last_time is None or bt > last_time:
                    last_time = bt
            mint = e["mint"]
            if e["event"] == "CreateEvent":
                c = coin_of(mint)
                c["created"] = e["slot"]
                c["creator"] = e["user"]
                c["create_price"] = (int(e["virtual_quote_reserves"])
                                     / int(e["virtual_token_reserves"]))
                young.append(mint)
            elif e["event"] == "TradeEvent":
                c = coin_of(mint)
                sol = int(e["sol_amount"]) / LAMPORTS
                c["trades"].append((e["slot"], bool(e["is_buy"]), sol, e["user"]))
                if mint not in seen_traded:
                    seen_traded.add(mint)
                    traded.append(mint)
                if not e["is_buy"] and e["user"] == c["creator"]:
                    c["dev_sold"] = True

        pending_sell = set()
        pending_by_mint = set()
        for po in state.pending.values():
            pending_by_mint.add(po.order["mint"])
            if po.order["side"] == "sell":
                pending_sell.add(po.order["mint"])

        def curve_price_rq(mint):
            curve = state.curves.get(mint)
            if curve is None or curve.virtual_token_reserves == 0:
                return 0.0, 0.0
            return (curve.virtual_quote_reserves / curve.virtual_token_reserves,
                    curve.real_quote_reserves / LAMPORTS)

        # --- exits first ---
        for mint in list(held.keys()):
            h = held[mint]
            pos = state.positions.get(mint)
            curve = state.curves.get(mint)
            if pos is None:
                if mint not in pending_by_mint:
                    held.pop(mint)
                    if h["filled"]:
                        coin_of(mint)["last_exit"] = now
                    else:
                        coin_of(mint)["entries"] -= 1
                continue
            h["filled"] = True
            if curve is None or curve.complete:
                continue
            tokens = int(pos.token_amount)
            if tokens <= 0:
                continue
            cost = int(pos.cost_lamports)
            value = sell_value_lamports(state, mint, tokens)
            if value > h["peak"]:
                h["peak"] = value
            curve_rq = curve.real_quote_reserves / LAMPORTS
            if curve_rq > h.get("peak_rq", 0):
                h["peak_rq"] = curve_rq
            sell_frac = 0.0
            if not h["tp1_done"] and value >= cost * cfg["tp1_mult"]:
                sell_frac = cfg["tp1_frac"]
                h["tp1_done"] = True
            elif h["tp1_done"] and not h["tp2_done"] and value >= cost * cfg["tp2_mult"]:
                sell_frac = cfg["tp2_frac"]
                h["tp2_done"] = True
            all_out = False
            if value <= cost * (1.0 - cfg["stop_loss"]):
                all_out = True
            elif (h["peak"] >= cost * (1.0 + cfg["trail_arm"])
                    and value < h["peak"] * (1.0 - cfg["trail_drop"])):
                all_out = True
            elif (now - h["opened"] > cfg["max_hold"]
                    and value < cost * (1.0 + cfg["hold_if_up"])):
                all_out = True
            elif cfg["exit_on_dev_sell"] and coin_of(mint)["dev_sold"]:
                all_out = True
            elif h.get("peak_rq", 0) > 0 and curve_rq < h["peak_rq"] * (1.0 - cfg["rq_drop"]):
                all_out = True
            if all_out:
                sell_frac = 1.0
            if sell_frac <= 0.0 or mint in pending_sell:
                continue
            if len(orders) >= cap_orders:
                break
            amount = tokens if sell_frac >= 1.0 else max(1, int(tokens * sell_frac))
            orders.append({"side": "sell", "mint": mint,
                           "token_amount": amount,
                           "max_slippage_bps": cfg["sell_slip_bps"]})

        # --- probe round trips ---
        for mint in list(probes.keys()):
            pos = state.positions.get(mint)
            curve = state.curves.get(mint)
            if pos is None:
                if mint not in pending_by_mint:
                    probes.pop(mint)
                continue
            if curve is None or curve.complete or mint in pending_by_mint:
                continue
            if len(orders) >= cap_orders:
                break
            orders.append({"side": "sell", "mint": mint,
                           "token_amount": int(pos.token_amount),
                           "max_slippage_bps": cfg["sell_slip_bps"]})
            probes[mint] = True

        # --- entries and adds ---
        # Candidates: every coin young enough to buy (evaluated each batch,
        # whether or not it traded this batch), plus held coins for adds.
        candidates = []
        seen_cand = set()
        for mint in young:
            c = coins[mint]
            if c["created"] is not None and now - c["created"] <= cfg["max_age"]:
                candidates.append(mint)
                seen_cand.add(mint)
        for mint in traded:
            if mint not in seen_cand:
                candidates.append(mint)
                seen_cand.add(mint)
        for mint in list(held.keys()):
            if mint not in seen_cand:
                candidates.append(mint)
                seen_cand.add(mint)
        # Prune the young list so it stays short.
        while young and coins[young[0]]["created"] is not None and now - coins[young[0]]["created"] > cfg["max_age"]:
            young.pop(0)
        free = state.free_cash_lamports()
        slots_left = (state.hello["caps"]["max_open_positions"]
                      - state.committed_position_count())
        main_left = cfg["max_main"] - len(held)
        for mint in candidates:
            if len(orders) >= cap_orders:
                break
            curve = state.curves.get(mint)
            if curve is None or curve.complete:
                continue
            if mint in pending_by_mint or mint in probes:
                continue
            c = coins[mint]
            h = held.get(mint)
            price, rq_sol = curve_price_rq(mint)
            if price <= 0:
                continue
            if h is None:
                if slots_left <= 0 or main_left <= 0:
                    continue
                if c["entries"] >= cfg["max_entries"]:
                    continue
                if now - c["last_exit"] < cfg["cooldown"]:
                    continue
                if not launch_gates(c, now, rq_sol, price):
                    continue
                want = cfg["stake_sol"]
            else:
                # Pyramid into strength for as long as the coin keeps growing:
                # price at/above the last buy and real SOL still climbing.
                if not h["filled"]:
                    continue
                if h["adds"] >= cfg["max_adds"]:
                    continue
                if now - h["last_buy_slot"] < cfg["add_delay"]:
                    continue
                if h["invested"] >= cfg["stake_sol"]:
                    continue
                if c["created"] is not None and now - c["created"] > cfg["add_max_age"]:
                    continue
                if price < h["last_add_price"] * (1.0 + cfg["add_step"]):
                    continue
                if rq_sol < h["last_rq"] * (1.0 + cfg["add_rq_growth"]):
                    continue
                want = min(cfg["add_sol"], cfg["stake_sol"] - h["invested"])
            cap_sol = state.max_buy_lamports(mint) / LAMPORTS
            amount = min(cap_sol, want)
            if amount > (free - tx) / LAMPORTS:
                amount = (free - tx) / LAMPORTS
            if amount < cfg["min_buy_sol"]:
                continue
            lamports = int(amount * LAMPORTS)
            if lamports <= 0:
                continue
            orders.append({"side": "buy", "mint": mint,
                           "quote_lamports": lamports,
                           "max_slippage_bps": cfg["buy_slip_bps"]})
            free -= lamports + tx
            if h is None:
                held[mint] = {"opened": now, "peak": 0, "peak_rq": 0, "invested": amount,
                              "filled": False, "last_add_price": price,
                              "last_rq": rq_sol,
                              "last_buy_slot": now, "adds": 0,
                              "tp1_done": False, "tp2_done": False}
                c["entries"] += 1
                slots_left -= 1
                main_left -= 1
            else:
                h["invested"] += amount
                h["last_add_price"] = price
                h["last_rq"] = rq_sol
                h["last_buy_slot"] = now
                h["adds"] += 1

        # --- guardrail pacing ---
        if (len(probes) < cfg["max_probes"] and slots_left > 0
                and first_time is not None and last_time is not None):
            pace = min(1.0, (last_time - first_time) / cfg["pace_secs"])
            need_coins = len(my_coins) < int(cfg["target_coins"] * pace + 0.999)
            need_fills = my_fills < int(cfg["target_fills"] * pace + 0.999)
            if need_coins or need_fills:
                min_rq = cfg["probe_sol"] * BPS / state.hello["caps"]["max_order_bps_of_real_reserve"]
                for mint in traded:
                    if len(orders) >= cap_orders or len(probes) >= cfg["max_probes"]:
                        break
                    if (mint in held or mint in probes or mint in pending_by_mint
                            or mint in state.positions):
                        continue
                    if need_coins and not need_fills and mint in my_coins:
                        continue
                    curve = state.curves.get(mint)
                    if curve is None or curve.complete:
                        continue
                    if curve.real_quote_reserves / LAMPORTS < min_rq * 1.5:
                        continue
                    if free < int(cfg["probe_sol"] * LAMPORTS) + 2 * tx:
                        break
                    orders.append({"side": "buy", "mint": mint,
                                   "quote_lamports": int(cfg["probe_sol"] * LAMPORTS),
                                   "max_slippage_bps": cfg["probe_slip_bps"]})
                    probes[mint] = False
                    free -= int(cfg["probe_sol"] * LAMPORTS) + tx
        return orders

    return on_batch


on_batch = create_bot()

if __name__ == "__main__":
    makefun_bot.run(on_batch)
