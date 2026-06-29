import os, json, threading, datetime, time, uuid
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

    def _exec_polymarket(self, leg, token_id, size):
        if DRY_RUN:
            return {"status": "dry_run", "platform": "polymarket", "leg": leg, "size": size}
        if not token_id:
            return {"status": "error", "platform": "polymarket", "error": "no token_id"}
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import MarketOrderArgs

            key = os.getenv("POLYMARKET_PRIVATE_KEY", "")
            client = ClobClient(host="https://clob.polymarket.com", key=key, chain_id=137)
            client.set_api_creds(client.create_or_derive_api_creds())

            order_args = MarketOrderArgs(token_id=token_id, amount=size)
            signed_order = client.create_market_order(order_args)
            resp = client.post_order(signed_order)

            order_id = resp.get("orderID") or resp.get("order_id", "?")
            return {"status": "executed", "platform": "polymarket", "leg": leg, "order_id": order_id}
        except Exception as e:
            return {"status": "error", "platform": "polymarket", "error": str(e)}

    def _exec_kalshi(self, leg, ticker, size):
        if DRY_RUN:
            return {"status": "dry_run", "platform": "kalshi", "leg": leg, "size": size}
        if not ticker:
            return {"status": "error", "platform": "kalshi", "error": "no ticker"}
        try:
            import requests, base64

            key_id      = os.getenv("KALSHI_KEY_ID", "")
            private_key = os.getenv("KALSHI_PRIVATE_KEY", "")

            # V2 API: side is "bid" (buy Yes) or "ask" (buy No)
            # price is in fixed-point dollars ("0.4700"), not cents
            # count is number of contracts (each pays $1 on win)
            side = "bid" if leg.lower() == "yes" else "ask"

            body = json.dumps({
                "ticker":                      ticker,
                "client_order_id":             str(uuid.uuid4()),
                "side":                        side,
                "count":                       "1",           # 1 contract = $1 payout on win
                "price":                       f"{size:.4f}", # price in dollars, e.g. "0.4700"
                "time_in_force":               "immediate_or_cancel",  # fast fill like market order
                "self_trade_prevention_type":  "taker_at_cross",
            })

            # New v2 endpoint path
            path     = "/portfolio/events/orders"
            base_url = "https://api.elections.kalshi.com/trade-api/v2"

            # RSA signing if key looks like PEM, else try Bearer token
            if "BEGIN" in private_key:
                from cryptography.hazmat.primitives import hashes, serialization
                from cryptography.hazmat.primitives.asymmetric import padding as asym_padding

                ts = str(int(time.time() * 1000))
                pem_key = serialization.load_pem_private_key(private_key.encode(), password=None)
                msg = (ts + "POST" + path + body).encode()
                sig = pem_key.sign(msg, asym_padding.PKCS1v15(), hashes.SHA256())
                headers = {
                    "KALSHI-ACCESS-KEY":       key_id,
                    "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
                    "KALSHI-ACCESS-TIMESTAMP": ts,
                    "Content-Type":            "application/json",
                }
            else:
                # ponytail: Bearer fallback — will 401 if Kalshi requires RSA.
                # Fix: generate RSA key pair in Kalshi Settings → API Keys,
                # paste the PEM private key into KALSHI_PRIVATE_KEY in .env
                headers = {
                    "Authorization": f"Bearer {private_key}",
                    "Content-Type":  "application/json",
                }

            resp = requests.post(base_url + path, data=body, headers=headers, timeout=10)

            if resp.status_code in (200, 201):
                data = resp.json()
                order_id = data.get("order", {}).get("order_id", "?")
                return {"status": "executed", "platform": "kalshi", "leg": leg, "order_id": order_id}
            else:
                return {"status": "error", "platform": "kalshi",
                        "http_status": resp.status_code, "error": resp.text[:300]}
        except Exception as e:
            return {"status": "error", "platform": "kalshi", "error": str(e)}

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
            "kalshi_ticker":       opportunity.get("kalshi_ticker", ""),
            "poly_token_id":       opportunity.get("poly_token_id", ""),
            "total_cost":          opportunity["total_cost"],
            "margin":              opportunity["margin"],
            "size_usd":            TRADE_SIZE_USD,
            "expected_profit_usd": round(opportunity["margin"] * TRADE_SIZE_USD, 4),
        }

        with ThreadPoolExecutor(max_workers=2) as pool:
            pf = pool.submit(self._exec_polymarket,
                             opportunity["poly_leg"],
                             opportunity.get("poly_token_id", ""),
                             TRADE_SIZE_USD)
            kf = pool.submit(self._exec_kalshi,
                             opportunity["kalshi_leg"],
                             opportunity.get("kalshi_ticker", ""),
                             TRADE_SIZE_USD)
            trade["polymarket_result"] = pf.result()
            trade["kalshi_result"]     = kf.result()

        ok = {"executed", "dry_run"}
        statuses = {trade["polymarket_result"]["status"], trade["kalshi_result"]["status"]}
        if statuses <= ok:
            trade["outcome"] = "success"
        elif statuses & ok:
            trade["outcome"] = "partial"   # ponytail: one leg failed — log for manual fix
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
        partials  = [t for t in trades if t.get("outcome") == "partial"]
        profit    = sum(t.get("expected_profit_usd", 0) for t in successes)
        return {
            "dry_run":                   DRY_RUN,
            "trade_size_usd":            TRADE_SIZE_USD,
            "min_margin_pct":            MIN_MARGIN * 100,
            "cooldown_seconds":          COOLDOWN_SECONDS,
            "total_trades":              len(trades),
            "successful_trades":         len(successes),
            "partial_trades":            len(partials),
            "total_expected_profit_usd": round(profit, 4),
            "last_trade":                trades[-1] if trades else None,
        }
