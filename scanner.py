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
MIN_PRICE_CHANGE_24H = 50        # percent - must be UP at least this much in 24h, filters out dumps/fades

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
# NOTE: DEXScreener's free API has no true "top gainers" endpoint. Two
# broader (non-keyword-search) sources are combined here instead:
#  - token-boosts/latest: pay-to-appear, but broad and fast-moving
#  - token-profiles/latest: newly-submitted token profiles, not payment-gated
# Neither is a perfect "market-wide gainers" feed, but both surface a much
# wider set of real candidates than searching by name/symbol did.

DEXSCREENER_PROFILES = "https://api.dexscreener.com/token-profiles/latest/v1"


def _fetch_pair_data(chain, addr):
    """Given a token address, fetch its actual pair data (price, liquidity, etc)."""
    try:
        r = requests.get(f"{DEXSCREENER_SEARCH}?q={addr}", timeout=20)
        r.raise_for_status()
        return r.json().get("pairs") or []
    except Exception as e:
        print(f"DEXScreener pair fetch failed for {addr}: {e}")
        return []


def get_trending_pairs():
    seed_tokens = []  # list of (chain, address) to check

    # Source 1: boosted/paid listings
    try:
        r = requests.get(DEXSCREENER_BOOSTED, timeout=20)
        r.raise_for_status()
        for item in r.json():
            if item.get("chainId") in ("ethereum", "solana") and item.get("tokenAddress"):
                seed_tokens.append((item["chainId"], item["tokenAddress"]))
    except Exception as e:
        print(f"DEXScreener boosted fetch failed: {e}")

    # Source 2: newly submitted profiles (not payment-gated)
    try:
        r = requests.get(DEXSCREENER_PROFILES, timeout=20)
        r.raise_for_status()
        for item in r.json():
            if item.get("chainId") in ("ethereum", "solana") and item.get("tokenAddress"):
                seed_tokens.append((item["chainId"], item["tokenAddress"]))
    except Exception as e:
        print(f"DEXScreener profiles fetch failed: {e}")

    print(f"Seed tokens pulled from boosted+profiles: {len(seed_tokens)}")

    candidates = {}
    for chain, addr in seed_tokens:
        for p in _fetch_pair_data(chain, addr):
            if p.get("chainId") != chain:
                continue
            base_addr = (p.get("baseToken") or {}).get("address")
            if not base_addr:
                continue

            liquidity = (p.get("liquidity") or {}).get("usd", 0) or 0
            mcap = p.get("marketCap", 0) or p.get("fdv", 0) or 0
            change_24h = (p.get("priceChange") or {}).get("h24", 0) or 0
            volume_24h = (p.get("volume") or {}).get("h24", 0) or 0

            key = f"{chain}:{base_addr}"
            if (
                liquidity >= MIN_LIQUIDITY_USD
                and MIN_MARKET_CAP <= mcap <= MAX_MARKET_CAP
                and change_24h >= MIN_PRICE_CHANGE_24H
            ):
                if key not in candidates or volume_24h > candidates[key]["volume_24h"]:
                    candidates[key] = {
                        "chain": chain,
                        "token_address": base_addr,
                        "symbol": p.get("baseToken", {}).get("symbol", "?"),
                        "liquidity": liquidity,
                        "market_cap": mcap,
                        "price_change_24h": change_24h,
                        "volume_24h": volume_24h,
                        "url": p.get("url", ""),
                    }

    print(f"Candidates passing filters: {len(candidates)}")
    ranked = sorted(candidates.values(), key=lambda c: c["price_change_24h"], reverse=True)
    return ranked[:25]


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
                if tx_id and last_seen is None:
                    # first time polling this wallet - just record baseline, don't alert
                    watchlist.setdefault("last_tx", {})[wallet] = tx_id
                elif tx_id and tx_id != last_seen:
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
    print(f"Current wallet watchlist size: {len(watchlist.get('solana_wallets', []))}")
    print(f"Solscan API key present: {bool(SOLSCAN_API_KEY)}")

    # --- Wallet buys: this is the actually-early signal, sent first and loud ---
    wallet_alerts = check_watchlist_wallets(watchlist)
    if wallet_alerts:
        send_telegram(
            "🐋 <b>Watchlist wallet activity</b>\n\n" + "\n\n".join(wallet_alerts[:10])
        )

    # --- DEXScreener discovery: these tokens have ALREADY moved by the time
    # they show up here. Treat this as background research to grow the wallet
    # watchlist, not as a buy signal. Sent quietly, once, as a digest - not as
    # individual hype-framed alerts. ---
    candidates = get_trending_pairs()
    new_finds = []

    for c in candidates:
        token_key = f"{c['chain']}:{c['token_address']}"
        if token_key in seen["tokens"]:
            continue
        if not is_safe(c["chain"], c["token_address"]):
            continue

        seen["tokens"].append(token_key)
        new_finds.append(c)

        # grow the Solana wallet watchlist from this runner's early holders -
        # this is the actual point of scanning these, not the alert itself
        if c["chain"] == "solana":
            holders = get_early_solana_holders(c["token_address"])
            print(f"  {c['symbol']}: fetched {len(holders)} holders")
            for h in holders:
                if h not in watchlist["solana_wallets"]:
                    watchlist["solana_wallets"].append(h)

    if new_finds:
        lines = [
            f"• <b>{c['symbol']}</b> ({c['chain']}) already +{c['price_change_24h']:.0f}% "
            f"24h - MCap ${c['market_cap']:,.0f}, added holders to watchlist"
            for c in new_finds
        ]
        send_telegram(
            "🔍 <b>Background discovery (already moved - not a buy signal)</b>\n"
            "These grow the wallet watchlist above. Watch for wallet alerts instead.\n\n"
            + "\n".join(lines[:15])
        )
    elif not wallet_alerts:
        print("No new alerts this run.")

    save_json(WATCHLIST_FILE, watchlist)
    save_json(SEEN_FILE, seen)


if __name__ == "__main__":
    main()
