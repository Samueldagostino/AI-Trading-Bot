"""
Pre-flight check for IBKR paper/live trading.
Run: python scripts/preflight_check.py

Checks everything needed before the trading runner starts.
Exit code 0 = all clear, non-zero = blocked.
"""

import os
import sys
import socket
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# Resolve project root (nq_bot_vscode/)
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent / "nq_bot_vscode"
if not PROJECT_DIR.exists():
    # Might be running from within nq_bot_vscode/scripts/
    PROJECT_DIR = SCRIPT_DIR.parent

# Add project root to path for imports
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))


class PreflightCheck:
    """Comprehensive pre-flight validator for IBKR trading."""

    def __init__(self):
        self.results = []
        self.critical_failures = []
        self.warnings = []

    def _pass(self, category, message):
        self.results.append((category, "PASS", message))

    def _fail(self, category, message):
        self.results.append((category, "FAIL", message))
        self.critical_failures.append(message)

    def _warn(self, category, message):
        self.results.append((category, "WARN", message))
        self.warnings.append(message)

    def _info(self, category, message):
        self.results.append((category, "INFO", message))

    # ── ENVIRONMENT CHECKS ────────────────────────────────────

    def check_python_version(self):
        v = sys.version_info
        if v.major >= 3 and v.minor >= 10:
            self._pass("ENVIRONMENT", f"Python {v.major}.{v.minor}.{v.micro}")
        else:
            self._fail("ENVIRONMENT", f"Python {v.major}.{v.minor} < 3.10 required")

    def check_packages(self):
        required = [
            "aiohttp", "numpy", "dotenv",
        ]
        missing = []
        for pkg in required:
            try:
                if pkg == "dotenv":
                    __import__("dotenv")
                else:
                    __import__(pkg)
            except ImportError:
                missing.append(pkg)

        if not missing:
            self._pass("ENVIRONMENT", "All required packages installed")
        else:
            self._fail("ENVIRONMENT", f"Missing packages: {', '.join(missing)}")

    def check_env_file(self):
        env_path = PROJECT_DIR / ".env"
        if not env_path.exists():
            self._fail("ENVIRONMENT", ".env file not found")
            return

        try:
            from dotenv import dotenv_values
            vals = dotenv_values(str(env_path))
        except ImportError:
            # Fallback: manual parse
            vals = {}
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    vals[k.strip()] = v.strip()

        required_vars = ["IBKR_GATEWAY_HOST", "IBKR_GATEWAY_PORT", "IBKR_ACCOUNT_TYPE"]
        missing = [v for v in required_vars if v not in vals or not vals[v]]
        if missing:
            self._fail("ENVIRONMENT", f".env missing: {', '.join(missing)}")
        else:
            self._pass("ENVIRONMENT", f".env loaded ({len(vals)} vars)")

    def check_no_exposed_credentials(self):
        """Scan .py files for plain-text credential patterns."""
        patterns = [
            re.compile(r'password\s*=\s*["\'][^"\']+["\']', re.IGNORECASE),
            re.compile(r'api_key\s*=\s*["\'][^"\']+["\']', re.IGNORECASE),
            re.compile(r'secret\s*=\s*["\'][A-Za-z0-9+/=]{20,}["\']', re.IGNORECASE),
        ]
        found = []
        for py_file in PROJECT_DIR.rglob("*.py"):
            if "__pycache__" in str(py_file) or "test_" in py_file.name:
                continue
            try:
                content = py_file.read_text()
                for pat in patterns:
                    matches = pat.findall(content)
                    for m in matches:
                        # Skip os.getenv defaults and empty strings
                        if "os.getenv" in content[max(0, content.find(m) - 50):content.find(m)]:
                            continue
                        if '""' in m or "''" in m:
                            continue
                        found.append(f"{py_file.name}: {m[:40]}...")
            except Exception:
                continue

        if not found:
            self._pass("ENVIRONMENT", "No exposed credentials")
        else:
            self._warn("ENVIRONMENT", f"Possible credentials in: {found[0]}")

    # ── IBKR GATEWAY CHECKS ──────────────────────────────────

    def check_gateway_reachable(self):
        """Check if IBKR gateway is reachable via TCP."""
        host = os.getenv("IBKR_GATEWAY_HOST", "localhost")
        port = int(os.getenv("IBKR_GATEWAY_PORT", "5000"))

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5)
            result = sock.connect_ex((host, port))
            sock.close()
            if result == 0:
                self._pass("IBKR GATEWAY", f"Gateway reachable at {host}:{port}")
                return True
            else:
                self._fail("IBKR GATEWAY", f"Gateway unreachable at {host}:{port}")
                return False
        except Exception as e:
            self._fail("IBKR GATEWAY", f"Gateway connection error: {e}")
            return False

    def check_gateway_auth(self):
        """Check auth status via IBKR Client Portal API."""
        host = os.getenv("IBKR_GATEWAY_HOST", "localhost")
        port = int(os.getenv("IBKR_GATEWAY_PORT", "5000"))
        base_url = f"https://{host}:{port}/v1/api"

        try:
            import urllib.request
            import ssl
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

            req = urllib.request.Request(f"{base_url}/iserver/auth/status")
            resp = urllib.request.urlopen(req, timeout=5, context=ctx)
            data = json.loads(resp.read())

            if data.get("authenticated"):
                self._pass("IBKR GATEWAY", "Session authenticated")
            else:
                self._fail("IBKR GATEWAY", "Session NOT authenticated — log in to Client Portal")
            return data

        except Exception as e:
            self._fail("IBKR GATEWAY", f"Auth check failed: {e}")
            return None

    def check_account(self, auth_data=None):
        """Check account ID and paper/live mode."""
        host = os.getenv("IBKR_GATEWAY_HOST", "localhost")
        port = int(os.getenv("IBKR_GATEWAY_PORT", "5000"))
        expected_type = os.getenv("IBKR_ACCOUNT_TYPE", "paper")

        try:
            import urllib.request
            import ssl
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

            base_url = f"https://{host}:{port}/v1/api"
            req = urllib.request.Request(f"{base_url}/portfolio/accounts")
            resp = urllib.request.urlopen(req, timeout=5, context=ctx)
            accounts = json.loads(resp.read())

            if accounts and len(accounts) > 0:
                acct = accounts[0]
                acct_id = acct.get("accountId", "unknown")
                acct_type = acct.get("type", "unknown")
                self._pass("IBKR GATEWAY", f"Account: {acct_id} ({acct_type})")

                # Verify paper vs live matches
                is_paper = "paper" in acct_type.lower() or acct_id.startswith("D")
                if expected_type == "paper" and is_paper:
                    self._pass("IBKR GATEWAY", "Mode: PAPER")
                elif expected_type == "live" and not is_paper:
                    self._pass("IBKR GATEWAY", "Mode: LIVE")
                else:
                    self._warn("IBKR GATEWAY",
                               f"Mode mismatch: expected {expected_type}, got {acct_type}")
            else:
                self._fail("IBKR GATEWAY", "No accounts found")
        except Exception as e:
            self._fail("IBKR GATEWAY", f"Account check failed: {e}")

    # ── CONTRACT CHECKS ───────────────────────────────────────

    def check_contract_resolution(self):
        """Attempt to resolve MNQ front-month contract."""
        host = os.getenv("IBKR_GATEWAY_HOST", "localhost")
        port = int(os.getenv("IBKR_GATEWAY_PORT", "5000"))

        try:
            import urllib.request
            import ssl
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

            base_url = f"https://{host}:{port}/v1/api"

            # Strategy 1: Search
            search_data = json.dumps({"symbol": "MNQ", "secType": "FUT"}).encode()
            req = urllib.request.Request(
                f"{base_url}/iserver/secdef/search",
                data=search_data,
                headers={"Content-Type": "application/json"},
            )
            resp = urllib.request.urlopen(req, timeout=10, context=ctx)
            results = json.loads(resp.read())

            if results:
                contract = results[0] if isinstance(results, list) else results
                conid = contract.get("conid", "unknown")
                symbol = contract.get("symbol", "MNQ")
                expiry = contract.get("expiry", contract.get("maturityDate", "unknown"))
                self._pass("CONTRACT", f"MNQ resolved: {symbol} (conid={conid}, expiry={expiry})")

                # Rollover check
                if expiry and expiry != "unknown":
                    self._check_rollover(expiry)
                return True
            else:
                self._warn("CONTRACT", "Contract search returned empty — gateway may need data subscription")
                return False

        except Exception as e:
            self._warn("CONTRACT", f"Contract resolution failed (expected if gateway offline): {e}")
            return False

    def _check_rollover(self, expiry_str):
        """Warn if contract is within 5 trading days of expiry."""
        try:
            # Parse various expiry formats
            for fmt in ("%Y%m%d", "%Y-%m-%d", "%m/%d/%Y"):
                try:
                    expiry_date = datetime.strptime(expiry_str, fmt).date()
                    break
                except ValueError:
                    continue
            else:
                return

            today = datetime.now(ET).date()
            days_to_expiry = (expiry_date - today).days

            if days_to_expiry <= 5:
                self._warn("CONTRACT", f"Contract expires in {days_to_expiry} days — ROLL IMMEDIATELY")
            elif days_to_expiry <= 14:
                roll_date = expiry_date
                # Roll date is typically 8 trading days before expiry
                self._warn("CONTRACT", f"Contract expires in {days_to_expiry} days — plan roll")
            else:
                self._pass("CONTRACT", f"Expiry in {days_to_expiry} days — no roll needed")
        except Exception:
            pass

    # ── MARKET DATA CHECKS ────────────────────────────────────

    def check_market_data(self):
        """Verify market data snapshot returns valid prices."""
        # This check is best-effort — may not work outside market hours
        now_et = datetime.now(ET)
        hour = now_et.hour

        is_rth = 9 <= hour < 16 and now_et.weekday() < 5
        is_eth = (hour >= 18 or hour < 9) and now_et.weekday() < 5

        if not is_rth and not is_eth:
            self._info("MARKET DATA", "Outside market hours — data may be stale")
            return

        if is_rth:
            self._info("MARKET DATA", "Session: RTH")
        else:
            self._info("MARKET DATA", "Session: ETH")

        # Attempt to get snapshot via gateway (best-effort)
        try:
            host = os.getenv("IBKR_GATEWAY_HOST", "localhost")
            port = int(os.getenv("IBKR_GATEWAY_PORT", "5000"))
            import urllib.request
            import ssl
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

            base_url = f"https://{host}:{port}/v1/api"
            req = urllib.request.Request(f"{base_url}/iserver/marketdata/snapshot?conids=0&fields=31,84,86")
            resp = urllib.request.urlopen(req, timeout=5, context=ctx)
            data = json.loads(resp.read())

            if data and isinstance(data, list) and len(data) > 0:
                snap = data[0]
                last = snap.get("31", 0)
                bid = snap.get("84", 0)
                ask = snap.get("86", 0)
                if last and bid and ask:
                    spread_ticks = abs(ask - bid) / 0.25 if ask and bid else 0
                    self._pass("MARKET DATA",
                               f"Last: {last:,.2f} | Bid: {bid:,.2f} | Ask: {ask:,.2f}")
                    if spread_ticks <= 5:
                        self._pass("MARKET DATA", f"Spread: {spread_ticks:.0f} ticks (normal)")
                    else:
                        self._warn("MARKET DATA", f"Spread: {spread_ticks:.0f} ticks (wide)")
                else:
                    self._info("MARKET DATA", "Snapshot returned but prices may be pending")
            else:
                self._info("MARKET DATA", "No snapshot available — may need market data subscription")

        except Exception:
            self._info("MARKET DATA", "Market data check skipped (gateway not available)")

    # ── SYSTEM INTEGRITY CHECKS ───────────────────────────────

    def check_system_integrity(self):
        """Verify critical trading system parameters."""
        # Read constants.py
        constants_path = PROJECT_DIR / "config" / "constants.py"
        if constants_path.exists():
            content = constants_path.read_text()

            # HC score threshold
            if "HIGH_CONVICTION_MIN_SCORE" in content:
                match = re.search(r"HIGH_CONVICTION_MIN_SCORE.*?=\s*([\d.]+)", content)
                if match and float(match.group(1)) == 0.75:
                    self._pass("SYSTEM INTEGRITY", "HC score threshold: 0.75")
                else:
                    self._fail("SYSTEM INTEGRITY",
                               f"HC score threshold: {match.group(1) if match else 'missing'} (expected 0.75)")

            # Max stop cap
            if "HIGH_CONVICTION_MAX_STOP_PTS" in content:
                match = re.search(r"HIGH_CONVICTION_MAX_STOP_PTS.*?=\s*([\d.]+)", content)
                if match and float(match.group(1)) == 30.0:
                    self._pass("SYSTEM INTEGRITY", "Max stop cap: 30pts")
                else:
                    self._fail("SYSTEM INTEGRITY",
                               f"Max stop cap: {match.group(1) if match else 'missing'} (expected 30)")

            # HTF gate
            if "HTF_STRENGTH_GATE" in content:
                self._pass("SYSTEM INTEGRITY", "HTF choke-point gate: present")
            else:
                self._fail("SYSTEM INTEGRITY", "HTF gate missing from constants.py")
        else:
            self._fail("SYSTEM INTEGRITY", "config/constants.py not found")

        # Read order executor safety constants
        executor_path = PROJECT_DIR / "Broker" / "order_executor.py"
        if executor_path.exists():
            content = executor_path.read_text()

            match_contracts = re.search(r"MAX_CONTRACTS_PER_ORDER\s*=\s*(\d+)", content)
            match_positions = re.search(r"MAX_OPEN_POSITIONS\s*=\s*(\d+)", content)
            match_daily = re.search(r"DAILY_LOSS_LIMIT_DOLLARS\s*=\s*([\d.]+)", content)
            match_kill = re.search(r"KILL_SWITCH_THRESHOLD_DOLLARS\s*=\s*([\d.]+)", content)

            if match_daily and float(match_daily.group(1)) == 500.0:
                self._pass("SYSTEM INTEGRITY", "Daily loss limit: $500")
            else:
                self._fail("SYSTEM INTEGRITY", "Daily loss limit mismatch")

            if match_kill and float(match_kill.group(1)) == 1000.0:
                self._pass("SYSTEM INTEGRITY", "Kill switch: $1,000")
            else:
                self._fail("SYSTEM INTEGRITY", "Kill switch threshold mismatch")

            if match_contracts and int(match_contracts.group(1)) == 2:
                self._pass("SYSTEM INTEGRITY", "Max contracts/order: 2")
            else:
                self._fail("SYSTEM INTEGRITY", "Max contracts/order mismatch")

            if match_positions and int(match_positions.group(1)) == 4:
                self._pass("SYSTEM INTEGRITY", "Max open positions: 4")
            else:
                self._fail("SYSTEM INTEGRITY", "Max open positions mismatch")
        else:
            self._fail("SYSTEM INTEGRITY", "Broker/order_executor.py not found")

    # ── RUN ALL CHECKS ────────────────────────────────────────

    def run(self) -> int:
        """Run all pre-flight checks. Returns 0 if all clear, 1 if blocked."""
        now_str = datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S ET")

        # Environment checks (always run)
        self.check_python_version()
        self.check_packages()
        self.check_env_file()
        self.check_no_exposed_credentials()

        # Gateway checks (best-effort)
        gateway_ok = self.check_gateway_reachable()
        if gateway_ok:
            auth_data = self.check_gateway_auth()
            self.check_account(auth_data)
            self.check_contract_resolution()
            self.check_market_data()
        else:
            self._info("IBKR GATEWAY", "Skipping auth/account/contract checks (gateway offline)")
            self._info("CONTRACT", "Skipped (gateway offline)")
            self._info("MARKET DATA", "Skipped (gateway offline)")

        # System integrity (always run)
        self.check_system_integrity()

        # Print results
        print(f"\n{'=' * 60}")
        print(f"  NQ TRADING SYSTEM — PRE-FLIGHT CHECK")
        print(f"  Timestamp: {now_str}")
        print(f"{'=' * 60}")

        current_category = None
        for category, status, message in self.results:
            if category != current_category:
                print(f"\n{category}:")
                current_category = category

            icon = {"PASS": "PASS", "FAIL": "FAIL", "WARN": "WARN", "INFO": "INFO"}[status]
            print(f"  [{icon}] {message}")

        print(f"\n{'=' * 60}")

        if self.critical_failures:
            print(f"VERDICT: BLOCKED — {self.critical_failures[0]}. Do not start trading runner.")
            return 1
        elif self.warnings:
            print(f"VERDICT: CLEAR WITH WARNINGS — review warnings above before starting.")
            return 0
        else:
            print(f"VERDICT: ALL CLEAR — ready for paper trading")
            return 0


def main():
    checker = PreflightCheck()
    exit_code = checker.run()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
