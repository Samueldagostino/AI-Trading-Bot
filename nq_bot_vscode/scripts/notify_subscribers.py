"""
SMS Notification System for NQ.BOT
====================================
Sends SMS alerts to subscribers when the bot simulation goes live.

Supports two subscriber sources:
  1. Google Apps Script (recommended) — reads from Google Sheet via web app
  2. Local subscribers.json file — manual fallback

SMS is sent via Twilio.

Required .env variables:
    TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxx
    TWILIO_AUTH_TOKEN=xxxxxxxxxxxxxxxxx
    TWILIO_FROM_NUMBER=+1XXXXXXXXXX
    NOTIFY_APPS_SCRIPT_URL=https://script.google.com/macros/s/xxx/exec   (optional)

Usage:
    python scripts/notify_subscribers.py                  # Send "bot is live" notification
    python scripts/notify_subscribers.py --message "Custom message"
    python scripts/notify_subscribers.py --test +15551234567   # Test single number
    python scripts/notify_subscribers.py --list            # List all subscribers
    python scripts/notify_subscribers.py --dry-run         # Show what would be sent
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
SUBSCRIBERS_FILE = PROJECT_DIR / "logs" / "subscribers.json"
NOTIFY_LOG_FILE = PROJECT_DIR / "logs" / "notification_log.json"

# Load .env
load_dotenv(PROJECT_DIR / ".env")


def _get_twilio_config() -> dict:
    """Load Twilio credentials from environment."""
    sid = os.getenv("TWILIO_ACCOUNT_SID", "")
    token = os.getenv("TWILIO_AUTH_TOKEN", "")
    from_number = os.getenv("TWILIO_FROM_NUMBER", "")
    return {"sid": sid, "token": token, "from": from_number}


def _get_apps_script_url() -> str:
    return os.getenv("NOTIFY_APPS_SCRIPT_URL", "")


def fetch_subscribers_from_sheet() -> list[dict]:
    """Fetch active subscribers from Google Apps Script web app."""
    url = _get_apps_script_url()
    if not url:
        logger.info("No NOTIFY_APPS_SCRIPT_URL set, skipping sheet fetch")
        return []

    try:
        resp = requests.get(url, params={"action": "list"}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if data.get("result") == "success":
            subs = data.get("subscribers", [])
            logger.info("Fetched %d subscribers from Google Sheet", len(subs))
            return subs
        else:
            logger.warning("Apps Script returned: %s", data)
            return []
    except Exception as e:
        logger.warning("Failed to fetch from Apps Script: %s", e)
        return []


def load_local_subscribers() -> list[dict]:
    """Load subscribers from local JSON file."""
    if not SUBSCRIBERS_FILE.exists():
        return []
    try:
        data = json.loads(SUBSCRIBERS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
        return data.get("subscribers", [])
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Error reading %s: %s", SUBSCRIBERS_FILE, e)
        return []


def save_local_subscribers(subscribers: list[dict]) -> None:
    """Save subscribers to local JSON cache."""
    SUBSCRIBERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SUBSCRIBERS_FILE, "w", encoding="utf-8") as f:
        json.dump({"subscribers": subscribers, "updated": datetime.now(timezone.utc).isoformat()}, f, indent=2)


def get_all_subscribers() -> list[dict]:
    """Merge subscribers from all sources, deduplicating by phone number."""
    seen = set()
    merged = []

    # Google Sheet subscribers (primary)
    for sub in fetch_subscribers_from_sheet():
        phone = _normalize_phone(sub.get("phone", ""))
        if phone and phone not in seen:
            seen.add(phone)
            merged.append({"phone": phone, "source": "sheet"})

    # Local file subscribers (fallback)
    for sub in load_local_subscribers():
        phone = _normalize_phone(sub.get("phone", ""))
        if phone and phone not in seen:
            seen.add(phone)
            merged.append({"phone": phone, "source": "local"})

    # Cache merged list locally
    if merged:
        save_local_subscribers(merged)

    return merged


def _normalize_phone(phone) -> str:
    """Normalize phone number to E.164 format."""
    if not phone:
        return ""
    phone = str(phone).strip()
    digits = "".join(c for c in phone if c.isdigit())
    if len(digits) == 10:
        return f"+1{digits}"  # Assume US
    elif len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    elif phone.startswith("+") and len(digits) >= 10:
        return f"+{digits}"
    return phone  # Return as-is if we can't normalize


def send_sms(to: str, message: str, dry_run: bool = False) -> bool:
    """Send a single SMS via Twilio REST API (no SDK needed)."""
    cfg = _get_twilio_config()
    if not cfg["sid"] or not cfg["token"] or not cfg["from"]:
        logger.error("Twilio credentials not configured. Set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER in .env")
        return False

    if dry_run:
        logger.info("[DRY-RUN] Would send to %s: %s", to, message[:50])
        return True

    url = f"https://api.twilio.com/2010-04-01/Accounts/{cfg['sid']}/Messages.json"
    try:
        resp = requests.post(
            url,
            auth=(cfg["sid"], cfg["token"]),
            data={
                "To": to,
                "From": cfg["from"],
                "Body": message,
            },
            timeout=15,
        )
        if resp.status_code in (200, 201):
            logger.info("SMS sent to %s (SID: %s)", to, resp.json().get("sid", "?"))
            return True
        else:
            logger.error("Twilio error for %s: %d — %s", to, resp.status_code, resp.text[:200])
            return False
    except Exception as e:
        logger.error("Failed to send SMS to %s: %s", to, e)
        return False


def notify_all(message: str, dry_run: bool = False) -> dict:
    """Send notification to all subscribers. Returns summary."""
    subscribers = get_all_subscribers()
    if not subscribers:
        logger.info("No subscribers to notify")
        return {"total": 0, "sent": 0, "failed": 0}

    logger.info("Notifying %d subscribers...", len(subscribers))
    sent = 0
    failed = 0

    for sub in subscribers:
        phone = sub.get("phone", "")
        if not phone:
            continue
        ok = send_sms(phone, message, dry_run=dry_run)
        if ok:
            sent += 1
        else:
            failed += 1
        # Small delay to avoid Twilio rate limits
        if not dry_run:
            time.sleep(0.5)

    summary = {
        "total": len(subscribers),
        "sent": sent,
        "failed": failed,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "message": message[:100],
    }

    # Log notification event
    _log_notification(summary)
    logger.info("Notification complete: %d sent, %d failed out of %d", sent, failed, len(subscribers))
    return summary


def _log_notification(summary: dict) -> None:
    """Append notification event to log file."""
    try:
        NOTIFY_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(NOTIFY_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(summary) + "\n")
    except OSError:
        pass


def main():
    parser = argparse.ArgumentParser(description="NQ.BOT SMS Notification System")
    parser.add_argument("--message", "-m", type=str, default=None,
                        help="Custom message (default: bot going live message)")
    parser.add_argument("--test", type=str, default=None,
                        help="Send test SMS to a single phone number")
    parser.add_argument("--list", action="store_true",
                        help="List all subscribers")
    parser.add_argument("--dry-run", action="store_true",
                        help="Don't actually send SMS")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%H:%M:%S",
    )

    default_msg = (
        "NQ.BOT is now LIVE and trading!\n\n"
        "The MNQ futures simulation is running.\n"
        "Watch live: www.makemoneymarkets.com\n\n"
        "— NQ.BOT"
    )
    message = args.message or default_msg

    if args.test:
        logger.info("Sending test SMS to %s", args.test)
        ok = send_sms(args.test, message, dry_run=args.dry_run)
        sys.exit(0 if ok else 1)

    if args.list:
        subs = get_all_subscribers()
        if not subs:
            print("No subscribers found.")
        else:
            print(f"\n{'Phone':<20} {'Source':<10}")
            print("-" * 30)
            for s in subs:
                print(f"{s['phone']:<20} {s.get('source', '?'):<10}")
            print(f"\nTotal: {len(subs)} subscribers")
        return

    result = notify_all(message, dry_run=args.dry_run)
    print(f"\nResults: {result['sent']} sent, {result['failed']} failed out of {result['total']}")


if __name__ == "__main__":
    main()
