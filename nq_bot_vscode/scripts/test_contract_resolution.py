"""
Contract Resolution Diagnostic Script
=======================================
Standalone diagnostic that tests all 4 contract resolution strategies
against the IBKR Client Portal Gateway.

Usage:
    python scripts/test_contract_resolution.py

Requires:
    - IBKR Client Portal Gateway running
    - IBKR_GATEWAY_HOST, IBKR_GATEWAY_PORT env vars (or defaults)
    - CME market data subscription active
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

# Project path setup
script_dir = Path(__file__).resolve().parent
project_dir = script_dir.parent
sys.path.insert(0, str(project_dir))

# Load .env
_env_path = project_dir / ".env"
if _env_path.exists():
    with open(_env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                val = val.strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
                    val = val[1:-1]
                os.environ.setdefault(key.strip(), val)

from Broker.ibkr_client_portal import IBKRClient, IBKRConfig

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


async def test_all_strategies():
    """Connect to gateway and try all 4 resolution strategies."""
    config = IBKRConfig(
        gateway_host=os.getenv("IBKR_GATEWAY_HOST", "localhost"),
        gateway_port=int(os.getenv("IBKR_GATEWAY_PORT", "5000")),
        account_type=os.getenv("IBKR_ACCOUNT_TYPE", "paper"),
        symbol=os.getenv("IBKR_SYMBOL", "MNQ"),
    )

    client = IBKRClient(config)
    symbol = config.symbol

    print()
    print("=" * 60)
    print("  CONTRACT RESOLUTION DIAGNOSTIC")
    print(f"  Gateway: {config.gateway_host}:{config.gateway_port}")
    print(f"  Symbol:  {symbol}")
    print("=" * 60)
    print()

    # Connect session (auth + account only, skip contract resolution)
    import aiohttp
    import ssl
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    connector = aiohttp.TCPConnector(ssl=ssl_ctx)
    client._session = aiohttp.ClientSession(connector=connector)

    # Check auth
    auth_data = await client._get("/iserver/auth/status")
    if not auth_data:
        print("FATAL: Cannot reach gateway or session not authenticated.")
        print("       Please log in via the Client Portal Gateway web UI.")
        await client._session.close()
        return

    authenticated = auth_data.get("authenticated", False)
    print(f"  Gateway auth status: {'AUTHENTICATED' if authenticated else 'NOT AUTHENTICATED'}")
    if not authenticated:
        print("  WARNING: Gateway session not authenticated — strategies may fail.")
    print()

    # Test each strategy
    strategies = [
        ("Strategy 1 — Minimal Search (POST /iserver/secdef/search)", client._resolve_strategy_search),
        ("Strategy 2 — Direct Lookup (GET /iserver/secdef/info)", client._resolve_strategy_secdef_info),
        ("Strategy 3 — Futures Endpoint (GET /trsrv/futures)", client._resolve_strategy_trsrv_futures),
        ("Strategy 4 — Hardcoded Fallback", client._resolve_strategy_hardcoded),
    ]

    results = []
    winner = None

    for i, (name, fn) in enumerate(strategies, 1):
        print(f"  [{i}/4] {name}")
        try:
            result = await fn(symbol)
            if result:
                print(f"         PASS — conid={result.conid}, symbol={result.symbol}, "
                      f"expiry={result.expiry}, exchange={result.exchange}")
                results.append((i, "PASS", result))
                if winner is None:
                    winner = (i, result)
            else:
                print(f"         FAIL — returned None")
                results.append((i, "FAIL", None))
        except Exception as e:
            print(f"         ERROR — {e}")
            results.append((i, "ERROR", str(e)))
        print()

    # Summary
    print("=" * 60)
    print("  RESULTS SUMMARY")
    print("=" * 60)
    print()

    for num, status, detail in results:
        detail_str = ""
        if status == "PASS" and detail:
            detail_str = f"conid={detail.conid}, expiry={detail.expiry}"
        elif status == "ERROR":
            detail_str = str(detail)[:60]
        print(f"  Strategy {num}: {status}  {detail_str}")

    print()

    if winner:
        num, contract = winner
        print(f"  Strategy that works: {num}")
        print(f"  Resolved contract: {contract.symbol} {contract.expiry}, "
              f"conid={contract.conid}, exchange={contract.exchange}")
        print(f"  [PASS]")
    else:
        print("  Strategy that works: NONE")
        print("  CONTRACT RESOLUTION BLOCKED — likely CME data subscription issue")
        print()
        print("  Required actions:")
        print("    1. IBKR Account Management -> Market Data Subscriptions")
        print("       -> CME Real-Time (NP,L1) must be ACTIVE")
        print("    2. Subscription must be propagated to paper trading account")
        print("    3. Market Data API Acknowledgement form must be signed")
        print(f"  [FAIL]")

    print()

    await client._session.close()


def main():
    asyncio.run(test_all_strategies())


if __name__ == "__main__":
    main()
