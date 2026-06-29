import os, json, threading, datetime, time
from concurrent.futures import ThreadPoolExecutor

DRY_RUN          = os.getenv("DRY_RUN", "true").lower() == "true"
TRADE_SIZE_USD   = float(os.getenv("TRADE_SIZE_USD", "1.0"))
MIN_MARGIN       = float(os.getenv("MIN_MARGIN", "0.05"))
COOLDOWN_SECONDS = int(os.getenv("TRADE_COOLDOWN_SECONDS", "60"))
LOG_FILE         = os.path.join(os.path.dirname(__file__), "trades.log")


class ArbitrageExecutor:
    def __init__(self):
        self._lock      = threading.Lock()
        self._cooldowns = {}   # (poly_strike, kalshi_strike) -> epoch
        self._trades    = []
        self._load_existing_trades()

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _load_existing_trades(self):
        if not os.path.exists(LOG_FILE):
            return
        with open(LOG_FILE) as f:
            for line in f:
                try:
                    self._trades.append(json.loads(line))
                except Exception:
                    pass

    def _can_trade(self, key):
        now = time.time()
        with self._lock:
            return (now - self._cooldowns.get(key, 0)) >= COOLDOWN_SECONDS

    def _mark_cooldown(self, key):
        with self._lock:
            self._cooldowns[key] = time.time()

    def _persist(self, trade):
        with self._lock:
            self._trades.append(trade)
        with open(LOG_FILE, "a") as f:
            f.write(json.dumps(trade) + "\n")

    # ------------------------------------------------------------------ #
    # Leg executors
    # ------------------------------------------------------------------ #

    def _exec_polymarket(self, leg, size):
        if DRY_RUN:
            return {"status": "dry_run", "platform": "polymarket", "leg": leg, "size": size}
        # ponytail: production path — needs py-clob-client + Polygon private key.
        # Install: pip install py-clob-client
        # Upgrade path: set DRY_RUN=false and ensure POLYMARKET_PRIVATE_KEY is 64-char hex.
        return {"status": "not_implemented", "platform": "polymarket",
                "error": "Set DRY_RUN=false only after verifying private key and USDC balance."}

    def _exec_kalshi(self, leg, size):
        if DRY_RUN:
            return {"status": "dry_run", "platform": "kalshi", "leg": leg, "size": size}
        # ponytail: production path — needs Kalshi REST API + RSA key signing.
        # Upgrade path: set DRY_RUN=false and ensure KALSHI_KEY_ID + KALSHI_PRIVATE_KEY (PEM).
        return {"status": "not_implemented", "platform": "kalshi",
                "error": "Set DRY_RUN=false only after depositing USD and getting RSA key pair."}

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def execute(self, opportunity):
        """
        Execute both legs of an arbitrage opportunity in parallel.
        Returns the trade record, or None if skipped (margin < MIN_MARGIN or cooldown active).
        """
        if opportunity["margin"] < MIN_MARGIN:
            return None

        key = (opportunity.get("poly_strike"), opportunity["kalshi_strike"])
        if not self._can_trade(key):
            return None
        self._mark_cooldown(key)

        trade = {
            "timestamp":           datetime.datetime.now().isoformat(),
            "dry_run":             DRY_RUN,
            "poly_leg":            opportunity["poly_leg"],
            "kalshi_leg":          opportunity["kalshi_leg"],
            "poly_strike":         opportunity.get("poly_strike"),
            "kalshi_strike":       opportunity["kalshi_strike"],
            "total_cost":          opportunity["total_cost"],
            "margin":              opportunity["margin"],
            "size_usd":            TRADE_SIZE_USD,
            "expected_profit_usd": round(opportunity["margin"] * TRADE_SIZE_USD, 4),
        }

        with ThreadPoolExecutor(max_workers=2) as pool:
            pf = pool.submit(self._exec_polymarket, opportunity["poly_leg"], TRADE_SIZE_USD)
            kf = pool.submit(self._exec_kalshi,     opportunity["kalshi_leg"], TRADE_SIZE_USD)
            trade["polymarket_result"] = pf.result()
            trade["kalshi_result"]     = kf.result()

        ok = {"executed", "dry_run"}
        statuses = {trade["polymarket_result"]["status"], trade["kalshi_result"]["status"]}
        if statuses <= ok:
            trade["outcome"] = "success"
        elif statuses & ok:
            trade["outcome"] = "partial"   # ponytail: one leg failed, log for manual intervention
        else:
            trade["outcome"] = "failed"

        self._persist(trade)
        return trade

    def get_trades(self):
        with self._lock:
            return list(self._trades)

    def get_status(self):
        trades    = self.get_trades()
        successes = [t for t in trades if t.get("outcome") == "success"]
        profit    = sum(t.get("expected_profit_usd", 0) for t in successes)
        return {
            "dry_run":                   DRY_RUN,
            "trade_size_usd":            TRADE_SIZE_USD,
            "min_margin_pct":            MIN_MARGIN * 100,
            "cooldown_seconds":          COOLDOWN_SECONDS,
            "total_trades":              len(trades),
            "successful_trades":         len(successes),
            "total_expected_profit_usd": round(profit, 4),
            "last_trade":                trades[-1] if trades else None,
        }
