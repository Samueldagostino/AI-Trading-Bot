"""
QuantData API Discovery Tool
==============================
The QuantData v3 dashboard at v3.quantdata.us renders real-time charts and tables.
These are powered by API calls to a backend. This script attempts to discover
those endpoints.

RUN THIS MANUALLY:
    python data_feeds/quantdata_discovery.py

It will guide you through capturing API endpoints from your browser.
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

# Ensure project root is on path
_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_DIR = _SCRIPT_DIR.parent
sys.path.insert(0, str(_PROJECT_DIR))

CONFIG_DIR = _PROJECT_DIR / "config"
ENDPOINTS_FILE = CONFIG_DIR / "quantdata_endpoints.json"


def print_instructions():
    """Print step-by-step instructions for capturing API endpoints."""
    print("""
╔══════════════════════════════════════════════════════════════╗
║              QUANTDATA API DISCOVERY                        ║
╚══════════════════════════════════════════════════════════════╝

Step 1: Open v3.quantdata.us in Chrome
Step 2: Open Developer Tools (F12) → Network tab
Step 3: Filter by "XHR" or "Fetch"
Step 4: Load the Gamma Exposure page for SPY or QQQ
Step 5: Look for API calls — they typically look like:
        https://api.quantdata.us/v1/...  or
        https://v3.quantdata.us/api/...  or
        https://core-lb-prod.quantdata.us/api/...  or
        similar patterns

Step 6: For each API call you find, note:
        - Full URL
        - Request method (GET/POST)
        - Headers (especially Authorization, Cookie, or API-Key)
        - Response format (JSON)

Step 7: Paste the URLs below when prompted, or save them to
        config/quantdata_endpoints.json

Known base URLs from previous discovery:
  - https://core-lb-prod.quantdata.us
  - Endpoints: /api/options/heat-map/
               /api/options/exposure/strike/
               /api/options/expirations
""")


def discover_endpoint(name: str, description: str) -> dict:
    """Prompt user for a specific endpoint."""
    print(f"\n--- {name.upper()} ---")
    print(f"    {description}")
    url = input(f"  Paste {name} endpoint URL (or press Enter to skip): ").strip()

    if not url:
        return {"url": None, "status": "skipped"}

    return {"url": url, "status": "discovered"}


def test_endpoint(url: str, headers: dict) -> dict:
    """Test connectivity to a discovered endpoint."""
    try:
        import requests
        resp = requests.get(url, headers=headers, timeout=10)
        result = {
            "status_code": resp.status_code,
            "content_type": resp.headers.get("Content-Type", ""),
        }
        if resp.status_code == 200:
            try:
                data = resp.json()
                result["response_keys"] = list(data.keys()) if isinstance(data, dict) else f"array[{len(data)}]"
                result["success"] = True
            except ValueError:
                result["response_keys"] = None
                result["success"] = False
                result["note"] = "Response is not JSON"
        else:
            result["success"] = False
            result["note"] = f"HTTP {resp.status_code}"

        return result
    except ImportError:
        return {"success": False, "note": "requests library not installed (pip install requests)"}
    except Exception as e:
        return {"success": False, "note": str(e)}


def discover_auth() -> dict:
    """Prompt user for authentication details."""
    print("""
--- AUTHENTICATION ---
How does the QuantData API authenticate? Check the request headers:
  1. Bearer token (Authorization: Bearer <token>)
  2. API key (X-API-Key: <key>)
  3. Cookie (Cookie: <session>)
  4. No auth / unknown
""")
    choice = input("  Auth method (1/2/3/4): ").strip()

    method_map = {"1": "bearer", "2": "api_key", "3": "cookie", "4": "none"}
    method = method_map.get(choice, "none")

    if method != "none":
        value = input(f"  Paste the {method} value: ").strip()
        print("\n  NOTE: The auth value will be stored in your .env file,")
        print("  not in the config JSON (for security).")
        return {"method": method, "value": value}

    return {"method": "none", "value": ""}


def save_config(base_url: str, auth: dict, endpoints: dict):
    """Save discovered configuration."""
    config = {
        "base_url": base_url,
        "auth_method": auth["method"],
        "auth_value": "STORED_IN_ENV",
        "endpoints": {},
        "discovered_at": datetime.now().isoformat(),
        "status": "discovered" if any(
            e.get("status") == "discovered" for e in endpoints.values()
        ) else "manual",
    }

    for name, info in endpoints.items():
        if info.get("url"):
            # Store relative path from base URL
            url = info["url"]
            if url.startswith(base_url):
                url = url[len(base_url):]
            config["endpoints"][name] = url

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    ENDPOINTS_FILE.write_text(json.dumps(config, indent=2))
    print(f"\n  Config saved to: {ENDPOINTS_FILE}")

    # Save auth to .env
    if auth["value"] and auth["method"] != "none":
        env_path = _PROJECT_DIR / ".env"
        env_line = f"QUANTDATA_AUTH={auth['value']}"

        existing = ""
        if env_path.exists():
            existing = env_path.read_text()

        if "QUANTDATA_AUTH" not in existing:
            with open(env_path, "a") as f:
                f.write(f"\n# QuantData authentication\n{env_line}\n")
            print(f"  Auth value appended to: {env_path}")
        else:
            print(f"  QUANTDATA_AUTH already exists in {env_path} — update manually if needed")


def main():
    print_instructions()

    proceed = input("Ready to begin discovery? (y/n): ").strip().lower()
    if proceed != "y":
        print("\nDiscovery cancelled. You can use manual mode instead.")
        print("Edit config/quantdata_manual_input.json with data from the dashboard.")
        return

    # Base URL
    print("\n--- BASE URL ---")
    base_url = input(
        "  Base URL (default: https://core-lb-prod.quantdata.us): "
    ).strip()
    if not base_url:
        base_url = "https://core-lb-prod.quantdata.us"

    # Auth
    auth = discover_auth()

    # Build headers for testing
    headers = {}
    if auth["method"] == "bearer":
        headers["Authorization"] = f"Bearer {auth['value']}"
    elif auth["method"] == "api_key":
        headers["X-API-Key"] = auth["value"]
    elif auth["method"] == "cookie":
        headers["Cookie"] = auth["value"]

    # Discover endpoints
    endpoints = {}

    endpoint_configs = [
        ("gex", "Gamma Exposure by strike (GEX chart data)"),
        ("dex", "Delta Exposure by strike"),
        ("net_flow", "Net options premium flow (call vs put)"),
        ("dark_pool", "Dark pool / off-exchange prints"),
        ("vol_skew", "Implied volatility skew across strikes"),
    ]

    for name, description in endpoint_configs:
        endpoints[name] = discover_endpoint(name, description)

    # Test discovered endpoints
    print("\n\n=== TESTING DISCOVERED ENDPOINTS ===\n")
    any_found = False

    for name, info in endpoints.items():
        if info.get("url"):
            any_found = True
            print(f"  Testing {name}: {info['url']}")
            result = test_endpoint(info["url"], headers)
            if result.get("success"):
                print(f"    ✓ Status: {result['status_code']}")
                print(f"    ✓ Keys: {result.get('response_keys')}")
            else:
                print(f"    ✗ Failed: {result.get('note', 'unknown error')}")

    if not any_found:
        print("  No endpoints discovered.")
        print("  Setting mode to 'manual' — use config/quantdata_manual_input.json")
        save_config(base_url, auth, endpoints)
        config = json.loads(ENDPOINTS_FILE.read_text())
        config["status"] = "manual"
        ENDPOINTS_FILE.write_text(json.dumps(config, indent=2))
    else:
        save_config(base_url, auth, endpoints)

    print("""
╔══════════════════════════════════════════════════════════════╗
║                    DISCOVERY COMPLETE                        ║
╠══════════════════════════════════════════════════════════════╣
║                                                              ║
║  If endpoints were found:                                    ║
║    → The system will auto-pull data every 30 minutes         ║
║                                                              ║
║  If no endpoints found:                                      ║
║    → Edit config/quantdata_manual_input.json before trading  ║
║    → See docs/quantdata_setup.md for instructions            ║
║                                                              ║
╚══════════════════════════════════════════════════════════════╝
""")


if __name__ == "__main__":
    main()
