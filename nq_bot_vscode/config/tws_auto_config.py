"""
TWS Auto-Launch Configuration
==============================
Dataclass holding all settings for automated TWS startup, IBC integration,
and crash recovery. Auto-detects TWS installation on Windows.

SECURITY: Credentials are read from env vars and NEVER logged.
"""

import os
import platform
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


def _detect_tws_path() -> Optional[str]:
    """Auto-detect TWS installation on Windows."""
    if platform.system() != "Windows":
        return None

    candidates = [
        Path(os.environ.get("IBKR_TWS_PATH", "")) if os.environ.get("IBKR_TWS_PATH") else None,
        Path(os.environ.get("LOCALAPPDATA", ""), "Jts", "tws.exe"),
        Path("C:/Jts/tws.exe"),
        Path(os.environ.get("ProgramFiles", ""), "Jts", "tws.exe"),
        Path(os.environ.get("ProgramFiles(x86)", ""), "Jts", "tws.exe"),
        Path(os.environ.get("USERPROFILE", ""), "Jts", "tws.exe"),
    ]

    for p in candidates:
        if p is not None and p.exists():
            return str(p)
    return None


def _detect_ibc_path() -> Optional[str]:
    """Auto-detect IBC installation on Windows."""
    if platform.system() != "Windows":
        return None

    env_path = os.environ.get("IBKR_IBC_PATH", "")
    if env_path and Path(env_path).exists():
        return env_path

    candidates = [
        Path("C:/IBC"),
        Path(os.environ.get("ProgramFiles", ""), "IBC"),
        Path(os.environ.get("USERPROFILE", ""), "IBC"),
    ]

    for p in candidates:
        if p.exists() and (p / "StartTWS.bat").exists():
            return str(p)
    return None


@dataclass
class TWSAutoConfig:
    """Configuration for automated TWS launch and health monitoring."""

    # ── TWS Settings ──
    tws_path: str = ""
    tws_port: int = 7497           # 7497 = paper, 7496 = live
    tws_host: str = "127.0.0.1"
    client_id: int = 1

    # ── IBC Settings ──
    ibc_path: str = ""
    ibc_username: str = ""         # Read from env, NEVER logged
    ibc_password: str = ""         # Read from env, NEVER logged
    trading_mode: str = "paper"    # "paper" or "live"

    # ── Launch Settings ──
    startup_timeout: int = 120     # Seconds to wait for TWS to accept connections
    port_probe_interval: float = 2.0  # Seconds between port probes during startup
    post_login_delay: int = 30     # Extra seconds after port is open (TWS needs warm-up)

    # ── Health Monitor ──
    health_check_interval: float = 10.0  # Seconds between health checks
    consecutive_failures_threshold: int = 3  # Failures before triggering restart

    # ── Crash Recovery ──
    auto_restart: bool = True
    max_restart_attempts: int = 5
    restart_cooldown: float = 30.0  # Seconds between restart attempts
    flatten_on_crash: bool = True   # Try to flatten positions before restart

    # ── Logging ──
    heartbeat_file: str = ""       # Path to heartbeat_state.json (auto-set)

    def __post_init__(self):
        """Auto-detect paths and read credentials from environment."""
        if not self.tws_path:
            detected = _detect_tws_path()
            if detected:
                self.tws_path = detected

        if not self.ibc_path:
            detected = _detect_ibc_path()
            if detected:
                self.ibc_path = detected

        # Read from env vars (never hardcoded)
        self.tws_host = os.environ.get("IBKR_TWS_HOST", self.tws_host)
        self.tws_port = int(os.environ.get("IBKR_TWS_PORT", str(self.tws_port)))
        self.client_id = int(os.environ.get("IBKR_CLIENT_ID", str(self.client_id)))
        self.ibc_username = os.environ.get("IBKR_USERNAME", self.ibc_username)
        self.ibc_password = os.environ.get("IBKR_PASSWORD", self.ibc_password)
        self.auto_restart = os.environ.get("IBKR_AUTO_RESTART", "true").lower() == "true"

        max_attempts_env = os.environ.get("IBKR_MAX_RESTART_ATTEMPTS", "")
        if max_attempts_env:
            self.max_restart_attempts = int(max_attempts_env)

    @property
    def is_paper(self) -> bool:
        return self.trading_mode == "paper"

    @property
    def tws_available(self) -> bool:
        return bool(self.tws_path) and Path(self.tws_path).exists()

    @property
    def ibc_available(self) -> bool:
        return bool(self.ibc_path) and Path(self.ibc_path).exists()

    def generate_ibc_ini(self, output_path: Path) -> Path:
        """
        Generate IBC config.ini for automated TWS login.
        Returns path to generated file.
        """
        ini_content = f"""\
# IBC Configuration — Auto-generated
# DO NOT commit this file (contains no secrets, but path-specific)

LogToConsole=yes

FIX=no
IbLoginId={self.ibc_username}
IbPassword={self.ibc_password}
TradingMode={self.trading_mode}

# Accept incoming API connections on the configured port
IbAutoClosedown=no
ClosedownAt=
AcceptIncomingConnectionAction=accept
AcceptNonBrokerageAccountWarning=yes
ExistingSessionDetectedAction=primary

# Suppress paper trading warning
DismissPasswordExpiryWarning=yes
DismissNSEComplianceNotice=yes
ReadOnlyLogin=no

# TWS API settings
OverrideTwsApiPort={self.tws_port}
"""
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(ini_content, encoding="utf-8")
        return output_path

    def summary(self) -> str:
        """Return a safe summary string (no credentials)."""
        return (
            f"TWSAutoConfig(\n"
            f"  tws_path={self.tws_path or '(not found)'},\n"
            f"  ibc_path={self.ibc_path or '(not found)'},\n"
            f"  port={self.tws_port}, host={self.tws_host},\n"
            f"  trading_mode={self.trading_mode},\n"
            f"  auto_restart={self.auto_restart}, max_attempts={self.max_restart_attempts},\n"
            f"  ibc_available={self.ibc_available}, tws_available={self.tws_available}\n"
            f")"
        )
