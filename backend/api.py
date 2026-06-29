import os, datetime, threading, time
from contextlib import asynccontextmanager

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from fetch_current_polymarket import fetch_polymarket_data_struct
from fetch_current_kalshi import fetch_kalshi_data_struct
from executor import ArbitrageExecutor

_executor = ArbitrageExecutor()


# ------------------------------------------------------------------ #
# Core detection (shared between HTTP endpoint and background loop)
# ------------------------------------------------------------------ #

def detect_opportunities():
    """
    Fetch live prices, compute all arbitrage checks, return:
      (full_response_dict, list_of_opportunity_dicts)
    """
    poly_data, poly_err   = fetch_polymarket_data_struct()
    kalshi_data, kalshi_err = fetch_kalshi_data_struct()

    response = {
        "timestamp":     datetime.datetime.now().isoformat(),
        "polymarket":    poly_data,
        "kalshi":        kalshi_data,
        "checks":        [],
        "opportunities": [],
        "errors":        [],
    }

    if poly_err:   response["errors"].append(poly_err)
    if kalshi_err: response["errors"].append(kalshi_err)
    if not poly_data or not kalshi_data:
        return response, []

    poly_strike    = poly_data["price_to_beat"]
    poly_up_cost   = poly_data["prices"].get("Up",   0.0)
    poly_down_cost = poly_data["prices"].get("Down", 0.0)
    poly_token_ids = poly_data.get("token_ids", {})

    if poly_strike is None:
        response["errors"].append("Polymarket Strike is None")
        return response, []

    kalshi_markets = sorted(kalshi_data.get("markets", []), key=lambda x: x["strike"])

    # Find nearest Kalshi strike and select ±4 markets around it
    closest_idx = min(range(len(kalshi_markets)),
                      key=lambda i: abs(kalshi_markets[i]["strike"] - poly_strike),
                      default=0)
    selected = kalshi_markets[max(0, closest_idx - 4): closest_idx + 5]

    opportunities = []

    for km in selected:
        kalshi_strike   = km["strike"]
        kalshi_yes_cost = km["yes_ask"] / 100.0
        kalshi_no_cost  = km["no_ask"]  / 100.0
        kalshi_ticker   = km.get("ticker", "")

        base = {
            "poly_strike":    poly_strike,
            "kalshi_strike":  kalshi_strike,
            "kalshi_ticker":  kalshi_ticker,
            "kalshi_yes":     kalshi_yes_cost,
            "kalshi_no":      kalshi_no_cost,
            "is_arbitrage":   False,
            "margin":         0,
        }

        def _check(poly_leg, kalshi_leg, poly_cost, kalshi_cost, type_label):
            total = poly_cost + kalshi_cost
            return {**base,
                    "type":          type_label,
                    "poly_leg":      poly_leg,
                    "kalshi_leg":    kalshi_leg,
                    "poly_token_id": poly_token_ids.get(poly_leg, ""),
                    "poly_cost":     poly_cost,
                    "kalshi_cost":   kalshi_cost,
                    "total_cost":    total,
                    "is_arbitrage":  total < 1.00,
                    "margin":        round(1.00 - total, 4) if total < 1.00 else 0}

        if poly_strike > kalshi_strike:
            c = _check("Down", "Yes", poly_down_cost, kalshi_yes_cost, "Poly>Kalshi")
            response["checks"].append(c)
            if c["is_arbitrage"]:
                response["opportunities"].append(c)
                opportunities.append(c)

        elif poly_strike < kalshi_strike:
            c = _check("Up", "No", poly_up_cost, kalshi_no_cost, "Poly<Kalshi")
            response["checks"].append(c)
            if c["is_arbitrage"]:
                response["opportunities"].append(c)
                opportunities.append(c)

        else:  # equal strikes — check both combos
            for poly_leg, kalshi_leg, poly_cost, kalshi_cost in [
                ("Down", "Yes", poly_down_cost, kalshi_yes_cost),
                ("Up",   "No",  poly_up_cost,   kalshi_no_cost),
            ]:
                c = _check(poly_leg, kalshi_leg, poly_cost, kalshi_cost, "Equal")
                response["checks"].append(c)
                if c["is_arbitrage"]:
                    response["opportunities"].append(c)
                    opportunities.append(c)

    return response, opportunities


# ------------------------------------------------------------------ #
# Background execution loop
# ------------------------------------------------------------------ #

def _bot_loop():
    while True:
        try:
            _, opportunities = detect_opportunities()
            for opp in opportunities:
                trade = _executor.execute(opp)
                if trade:
                    status = trade.get("outcome", "?")
                    print(f"[TRADE] {status.upper()} | "
                          f"Poly {trade['poly_leg']} + Kalshi {trade['kalshi_leg']} | "
                          f"margin={trade['margin']:.1%} | "
                          f"expected_profit=${trade['expected_profit_usd']:.4f} "
                          f"{'(DRY RUN)' if trade['dry_run'] else ''}")
        except Exception as e:
            print(f"[BOT ERROR] {e}")
        time.sleep(1)


# ------------------------------------------------------------------ #
# FastAPI app
# ------------------------------------------------------------------ #

@asynccontextmanager
async def lifespan(app: FastAPI):
    t = threading.Thread(target=_bot_loop, daemon=True)
    t.start()
    print(f"[BOT] Execution loop started (DRY_RUN={os.getenv('DRY_RUN', 'true')})")
    yield

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/arbitrage")
def get_arbitrage_data():
    response, _ = detect_opportunities()
    return response


@app.get("/status")
def get_status():
    return _executor.get_status()


@app.get("/trades")
def get_trades():
    return _executor.get_trades()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
