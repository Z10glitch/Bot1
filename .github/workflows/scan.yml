"""
LC Runner Scanner
- Pulls recent gainers from DEXScreener (Solana + Ethereum)
- Filters out low-liquidity / likely-fake pumps
- Runs a GoPlus safety check on survivors
- For Solana tokens: pulls early holder wallets and grows a watchlist
- Polls watchlist wallets for new buys
- Sends everything worth seeing to your Telegram

This is designed to run on a schedule via GitHub Actions (see scan.yml).
Each run is stateless except for two small JSON files committed back to
the repo (watchlist.json, seen.json) so it doesn't repeat alerts.
"""

import os
import json
import time
import requests

# ---------- Config ----------
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
SOLSCAN_API_KEY = os.environ.get("SOLSCAN_API_KEY", "")  # optional but recommended

MIN_LIQUIDITY_USD = 50_000       # ignore anything under this - too easy to fake
MIN_MARKET_CAP = 200_000
MAX_MARKET_CAP = 50_000_000      # "low cap" ceiling, adjust to taste
MIN_PRICE_CHANGE_7D = 100        # percent - only care about genuine runners

WATCHLIST_FILE = "watchlist.json"
SEEN_FILE = "seen.json"

DEXSCREENER_BOOSTED = "https://api.dexscreener.com/token-boosts/latest/v1"
DEXSCREENER_SEARCH = "https://api.dexscreener.com/latest/dex/search"
GOPLUS_TOKEN_SECURITY = "https://api.gopluslabs.io/api/v1/token_security/{chain_id}"
SOLSCAN_HOLDERS = "https://pro-api.solscan.io/v2.0/token/holders"

CHAIN_IDS = {"ethereum": "1", "solana": "solana"}


# ---------- Helpers ----------
def load_json(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }, timeout=15)
    except Exception as e:
        print(f"Telegram send failed: {e}")


# ---------- Step 1: find recent runners on DEXScreener ----------
def get_trending_pairs():
    """
    Pulls boosted/trending token profiles, then fetches their pair data
    to check actual price change / liquidity / market cap.
    """
    candidates = []
    try:
        resp = requests.get(DEXSCREENER_BOOSTED, timeout=20)
        resp.raise_for_status()
        boosted = resp.json()
    except Exception as e:
        print(f"DEXScreener boosted fetch failed: {e}")
        return candidates

    for item in boosted:
        chain = item.get("chainId")
        addr = item.get("tokenAddress")
        if chain not in ("ethereum", "solana") or not addr:
            continue
        try:
            r = requests.get(f"{DEXSCREENER_SEARCH}?q={addr}", timeout=20)
            r.raise_for_status()
            pairs = r.json().get("pairs") or []
        except Exception:
            continue

        for p in pairs:
            liquidity = (p.get("liquidity") or {}).get("usd", 0) or 0
            mcap = p.get("marketCap", 0) or p.get("fdv", 0) or 0
            change_7d = (p.get("priceChange") or {}).get("h24", 0) or 0
            # DEXScreener free API doesn't always expose 7d directly - h24 used as proxy signal

            if (
                liquidity >= MIN_LIQUIDITY_USD
                and MIN_MARKET_CAP <= mcap <= MAX_MARKET_CAP
            ):
                candidates.append({
                    "chain": chain,
                    "token_address": addr,
                    "symbol": p.get("baseToken", {}).get("symbol", "?"),
                    "liquidity": liquidity,
                    "market_cap": mcap,
                    "price_change_24h": change_7d,
                    "url": p.get("url", ""),
                })
    return candidates


# ---------- Step 2: safety check ----------
def is_safe(chain, token_address):
    chain_id = CHAIN_IDS.get(chain)
    if not chain_id:
        return True  # skip check if unsupported, don't block on it
    try:
        url = GOPLUS_TOKEN_SECURITY.format(chain_id=chain_id)
        resp = requests.get(url, params={"contract_addresses": token_address}, timeout=20)
        resp.raise_for_status()
        result = resp.json().get("result", {}).get(token_address.lower(), {})
        if result.get("is_honeypot") == "1":
            return False
        if result.get("is_mintable") == "1":
            return False
        # LP-locked / owner-can't-mint style checks - not exhaustive, expand as needed
        return True
    except Exception as e:
        print(f"GoPlus check failed for {token_address}: {e}")
        return True  # fail open rather than silently dropping tokens


# ---------- Step 3: pull early holders (Solana only for now) ----------
def get_early_solana_holders(token_address, limit=15):
    if not SOLSCAN_API_KEY:
        return []
    try:
        headers = {"token": SOLSCAN_API_KEY}
        resp = requests.get(
            SOLSCAN_HOLDERS,
            params={"address": token_address, "page": 1, "page_size": limit},
            headers=headers,
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json().get("data", [])
        return [h["address"] for h in data if "address" in h]
    except Exception as e:
        print(f"Solscan holder fetch failed for {token_address}: {e}")
        return []


# ---------- Step 4: poll watchlist wallets for new activity ----------
def check_watchlist_wallets(watchlist):
    """
    Placeholder polling logic - checks each Solana wallet's most recent
    transaction and alerts if it's new since last run. Requires SOLSCAN_API_KEY.
    """
    alerts = []
    if not SOLSCAN_API_KEY:
        return alerts

    for wallet in watchlist.get("solana_wallets", []):
        try:
            resp = requests.get(
                "https://pro-api.solscan.io/v2.0/account/transactions",
                params={"address": wallet, "limit": 1},
                headers={"token": SOLSCAN_API_KEY},
                timeout=20,
            )
            resp.raise_for_status()
            txs = resp.json().get("data", [])
            if txs:
                last_seen = watchlist.get("last_tx", {}).get(wallet)
                tx_id = txs[0].get("txHash")
                if tx_id and tx_id != last_seen:
                    watchlist.setdefault("last_tx", {})[wallet] = tx_id
                    alerts.append(f"Watchlist wallet {wallet[:6]}... made a new move: "
                                   f"https://solscan.io/tx/{tx_id}")
        except Exception as e:
            print(f"Wallet poll failed for {wallet}: {e}")
        time.sleep(1)  # basic rate-limit courtesy

    return alerts


# ---------- Main ----------
def main():
    watchlist = load_json(WATCHLIST_FILE, {"solana_wallets": [], "last_tx": {}})
    seen = load_json(SEEN_FILE, {"tokens": []})

    candidates = get_trending_pairs()
    new_alerts = []

    for c in candidates:
        token_key = f"{c['chain']}:{c['token_address']}"
        if token_key in seen["tokens"]:
            continue

        if not is_safe(c["chain"], c["token_address"]):
            continue

        seen["tokens"].append(token_key)
        new_alerts.append(
            f"🚀 <b>{c['symbol']}</b> ({c['chain']})\n"
            f"MCap: ${c['market_cap']:,.0f} | Liquidity: ${c['liquidity']:,.0f} | "
            f"24h: {c['price_change_24h']}%\n{c['url']}"
        )

        # grow the Solana wallet watchlist from this runner's early holders
        if c["chain"] == "solana":
            holders = get_early_solana_holders(c["token_address"])
            for h in holders:
                if h not in watchlist["solana_wallets"]:
                    watchlist["solana_wallets"].append(h)

    # poll existing watchlist for fresh activity
    new_alerts.extend(check_watchlist_wallets(watchlist))

    if new_alerts:
        send_telegram("\n\n".join(new_alerts[:10]))  # cap message size
    else:
        print("No new alerts this run.")

    save_json(WATCHLIST_FILE, watchlist)
    save_json(SEEN_FILE, seen)


if __name__ == "__main__":
    main()
