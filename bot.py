"""
iPhone Duo US stock alert -> Telegram.

Checks Apple's US online store for:
  - in-store pickup availability near your ZIP codes
  - online delivery availability
and sends a Telegram message when a model becomes available
(only on changes, so you don't get spammed every run).

Run once:   python bot.py
Run forever: python bot.py --loop 300     (checks every 300 s)
Test Telegram: python bot.py --test
"""

import json
import os
import re
import sys
import time

import requests

# ---------- config (environment variables) ----------
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
ZIP_CODES = [z.strip() for z in os.environ.get("ZIP_CODES", "10001,94103,60601").split(",") if z.strip()]
# Optional: comma-separated US part numbers (e.g. "MABC1LL/A"). If empty, auto-discovered.
PART_NUMBERS = [p.strip() for p in os.environ.get("PART_NUMBERS", "").split(",") if p.strip()]
# Only alert for stores within this many miles of the ZIP (Apple returns ~12 nearest stores)
MAX_MILES = float(os.environ.get("MAX_MILES", "50"))
CHECK_DELIVERY = os.environ.get("CHECK_DELIVERY", "1") == "1"
STATE_FILE = os.environ.get("STATE_FILE", "state.json")

BUY_URL = "https://www.apple.com/shop/buy-iphone/iphone-duo"
PICKUP_URL = "https://www.apple.com/shop/fulfillment-messages"
DELIVERY_URL = "https://www.apple.com/shop/delivery-message"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

session = requests.Session()
session.headers.update(HEADERS)


# ---------- telegram ----------
# Option B (no BotFather): send from your own account to Saved Messages.
TG_API_ID = os.environ.get("TG_API_ID", "")
TG_API_HASH = os.environ.get("TG_API_HASH", "")
TG_SESSION = os.environ.get("TG_SESSION", "")  # string from login.py


def send_as_user(text: str) -> None:
    from telethon.sync import TelegramClient
    from telethon.sessions import StringSession

    with TelegramClient(StringSession(TG_SESSION), int(TG_API_ID), TG_API_HASH) as client:
        client.send_message("me", text, parse_mode="html", link_preview=False)


# Option C: ntfy push notifications (https://ntfy.sh), no account needed.
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")


def send_ntfy(text: str) -> None:
    plain = re.sub(r"<[^>]+>", "", text)  # strip Telegram HTML tags
    r = requests.post(
        f"{NTFY_SERVER}/{NTFY_TOPIC}",
        data=plain.encode("utf-8"),
        headers={
            "Title": "iPhone Duo in stock (US)",
            "Priority": "urgent",
            "Tags": "iphone,rotating_light",
            "Click": BUY_URL,
        },
        timeout=20,
    )
    if not r.ok:
        print(f"[ntfy] error {r.status_code}: {r.text}")


def send_telegram(text: str) -> None:
    if NTFY_TOPIC:
        try:
            send_ntfy(text)
        except Exception as e:
            print(f"[ntfy] error: {e}")
        return
    if TG_API_ID and TG_API_HASH and TG_SESSION:
        try:
            send_as_user(text)
        except Exception as e:
            print(f"[telegram-user] error: {e}")
        return
    if not TOKEN or not CHAT_ID:
        print("[telegram] missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID; message was:\n" + text)
        return
    r = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": True},
        timeout=20,
    )
    if not r.ok:
        print(f"[telegram] error {r.status_code}: {r.text}")


# ---------- state ----------
def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


# ---------- apple ----------
def discover_parts() -> list[str]:
    """Pull US part numbers (….LL/A) out of the iPhone Duo buy page."""
    r = session.get(BUY_URL, timeout=30)
    r.raise_for_status()
    parts = sorted(set(re.findall(r"\b([A-Z0-9]{5}LL/A)\b", r.text)))
    return parts


def check_pickup(part: str, zip_code: str) -> list[dict]:
    """Return stores near zip_code where `part` is available for pickup."""
    params = {"pl": "true", "mts.0": "regular", "parts.0": part, "location": zip_code}
    r = session.get(PICKUP_URL, params=params, timeout=30)
    r.raise_for_status()
    stores = (r.json().get("body", {}).get("content", {})
              .get("pickupMessage", {}).get("stores", []))
    hits = []
    for s in stores:
        pa = s.get("partsAvailability", {}).get(part, {})
        if pa.get("pickupDisplay") != "available":
            continue
        try:
            miles = float(s.get("storedistance", 0))
        except (TypeError, ValueError):
            miles = 0.0
        if miles > MAX_MILES:
            continue
        reg = pa.get("messageTypes", {}).get("regular", {})
        hits.append({
            "store": s.get("storeName", "?"),
            "city": s.get("city", ""),
            "state": s.get("state", ""),
            "miles": miles,
            "title": reg.get("storePickupProductTitle", part),
            "quote": reg.get("storePickupQuote", ""),
        })
    return hits


def check_delivery(part: str, zip_code: str) -> str | None:
    """Return a delivery message if the part can ship, else None."""
    params = {"parts.0": part, "mts.0": "regular", "location": zip_code}
    r = session.get(DELIVERY_URL, params=params, timeout=30)
    r.raise_for_status()
    dm = (r.json().get("body", {}).get("content", {})
          .get("deliveryMessage", {}).get(part, {}).get("regular", {}))
    if not dm:
        return None
    if not dm.get("buyability", {}).get("isBuyable", False):
        return None
    msgs = dm.get("deliveryOptionMessages") or []
    text = " / ".join(m.get("displayName", "") for m in msgs if m.get("displayName"))
    if not text or "unavailable" in text.lower():
        return None
    return text


# ---------- main ----------
def run_once() -> None:
    parts = PART_NUMBERS or discover_parts()
    if not parts:
        print("No part numbers found on the buy page. Set PART_NUMBERS manually.")
        return
    print(f"Checking {len(parts)} models across ZIPs {ZIP_CODES}")

    old = load_state()
    new = {}
    alerts = []

    for part in parts:
        # pickup
        for z in ZIP_CODES:
            try:
                hits = check_pickup(part, z)
            except Exception as e:
                print(f"[pickup] {part} @ {z}: {e}")
                continue
            for h in hits:
                key = f"pickup|{part}|{h['store']}"
                new[key] = True
                if key not in old:
                    alerts.append(
                        f"🟢 <b>{h['title']}</b>\n"
                        f"Pickup at Apple {h['store']} ({h['city']}, {h['state']}, "
                        f"{h['miles']:.0f} mi from {z})\n{h['quote']}"
                    )
            time.sleep(1.5)  # be polite

        # delivery
        if CHECK_DELIVERY:
            try:
                msg = check_delivery(part, ZIP_CODES[0])
            except Exception as e:
                print(f"[delivery] {part}: {e}")
                msg = None
            if msg:
                key = f"delivery|{part}"
                new[key] = msg
                if old.get(key) != msg:
                    alerts.append(f"📦 <b>{part}</b> can ship: {msg}")
            time.sleep(1.5)

    if alerts:
        header = "📱 <b>iPhone Duo in stock (US)</b>\n\n"
        footer = f"\n\nBuy: {BUY_URL}"
        # Telegram limit is 4096 chars; chunk if needed
        body = "\n\n".join(alerts)
        for i in range(0, len(body), 3500):
            send_telegram(header + body[i:i + 3500] + footer)
        print(f"Sent {len(alerts)} alert(s).")
    else:
        print("Nothing new in stock.")

    save_state(new)


def main() -> None:
    args = sys.argv[1:]
    if "--test" in args:
        send_telegram("✅ iPhone Duo stock bot is connected.")
        return
    if "--parts" in args:
        print("\n".join(discover_parts()))
        return
    if "--loop" in args:
        i = args.index("--loop")
        interval = int(args[i + 1]) if i + 1 < len(args) else 300
        while True:
            try:
                run_once()
            except Exception as e:
                print(f"[run] {e}")
            time.sleep(interval)
    else:
        run_once()


if __name__ == "__main__":
    main()
