#!/usr/bin/env python3
"""
Stamina CS Intelligence — slack_sync.py

Incrementally pulls new messages from every customer Slack channel.
Only fetches messages newer than the latest timestamp already stored
per channel — never re-pulls what we already have.

Runs every 15 minutes via GitHub Actions.

Usage:
  python3 slack_sync.py            # live run
  python3 slack_sync.py --dry-run  # print what would happen, write nothing
"""

import os
import sys
import time
from datetime import datetime, timezone

import requests
from supabase import create_client

DRY_RUN = "--dry-run" in sys.argv

# ── Credentials ───────────────────────────────────────────────────────────────

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://jgvyeavyffenvuhphejg.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
SLACK_TOKEN  = os.environ.get("SLACK_TOKEN")

SLACK_BASE = "https://slack.com/api"

sb = create_client(SUPABASE_URL, SUPABASE_KEY)

# ── Helpers ───────────────────────────────────────────────────────────────────

def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def slack_get(endpoint: str, params: dict = None) -> dict:
    resp = requests.get(
        f"{SLACK_BASE}/{endpoint}",
        headers={"Authorization": f"Bearer {SLACK_TOKEN}"},
        params=params or {},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Slack API error on {endpoint}: {data.get('error')}")
    return data


def ts_to_dt(ts: str) -> str:
    """Convert Slack timestamp (e.g. '1748123456.123456') to ISO datetime string."""
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()


# ── User cache (user_id → {name, email, is_internal}) ────────────────────────

_user_cache: dict = {}


def resolve_user(user_id: str) -> dict:
    """Return {name, email, is_internal} for a Slack user ID. Cached."""
    if user_id in _user_cache:
        return _user_cache[user_id]

    try:
        data    = slack_get("users.info", {"user": user_id})
        user    = data["user"]
        profile = user.get("profile", {})
        email   = profile.get("email", "")
        result  = {
            "name":        user.get("real_name") or profile.get("display_name") or user_id,
            "email":       email,
            "is_internal": email.endswith("@stamina.io"),
        }
    except Exception:
        result = {"name": user_id, "email": "", "is_internal": False}

    _user_cache[user_id] = result
    return result


# ── Per-channel latest timestamp ──────────────────────────────────────────────

def latest_ts_for_channel(channel_id: str):
    """Return the highest message_ts already stored for this channel, or None."""
    rows = (
        sb.table("slack_messages")
        .select("message_ts")
        .eq("slack_channel_id", channel_id)
        .order("message_ts", desc=True)
        .limit(1)
        .execute()
        .data
    )
    return rows[0]["message_ts"] if rows else None


# ── Channel sync ──────────────────────────────────────────────────────────────

def sync_channel(customer: dict) -> tuple[int, int]:
    """
    Fetch new messages for one customer channel.
    Returns (new_messages_count, error_count).
    """
    channel_id  = customer["slack_channel_id"]
    customer_id = customer["id"]
    brand_id    = customer.get("brand_id")
    name        = customer.get("name", channel_id)

    # Only fetch messages newer than what we already have
    oldest_ts = latest_ts_for_channel(channel_id)

    params = {"channel": channel_id, "limit": 200}
    if oldest_ts:
        # Add a tiny increment so we don't re-fetch the last stored message
        params["oldest"] = str(float(oldest_ts) + 0.000001)
    else:
        # First run — get last 30 days
        cutoff = time.time() - (30 * 24 * 60 * 60)
        params["oldest"] = str(cutoff)

    new_count = 0
    errors    = 0

    try:
        # Paginate through all new messages
        while True:
            data     = slack_get("conversations.history", params)
            messages = data.get("messages", [])

            for msg in messages:
                # Skip non-message events (joins, leaves, bot messages)
                if msg.get("subtype") in ("channel_join", "channel_leave", "bot_message"):
                    continue
                if not msg.get("text") or not msg.get("user"):
                    continue

                ts      = msg["ts"]
                user_id = msg["user"]
                user    = resolve_user(user_id)

                row = {
                    "customer_id":      customer_id,
                    "brand_id":         brand_id,
                    "slack_channel_id": channel_id,
                    "message_ts":       ts,
                    "user_id":          user_id,
                    "user_name":        user["name"],
                    "is_internal":      user["is_internal"],
                    "text":             msg.get("text", ""),
                    "message_date":     ts_to_dt(ts),
                }

                if not DRY_RUN:
                    sb.table("slack_messages").upsert(
                        row, on_conflict="slack_channel_id,message_ts"
                    ).execute()

                new_count += 1

            # Paginate if more messages exist
            if data.get("has_more") and data.get("response_metadata", {}).get("next_cursor"):
                params["cursor"] = data["response_metadata"]["next_cursor"]
            else:
                break

        if new_count:
            log(f"  {name}: {new_count} new messages")

    except Exception as e:
        log(f"  ERROR {name} ({channel_id}): {e}")
        errors += 1

    return new_count, errors


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log(f"Slack sync started {'[DRY RUN] ' if DRY_RUN else ''}...")

    # All customers with a Slack channel
    customers = (
        sb.table("customers")
        .select("id, name, brand_id, slack_channel_id")
        .not_.is_("slack_channel_id", "null")
        .execute()
        .data
    )
    log(f"  {len(customers)} customers with Slack channels")

    total_new = 0
    total_err = 0

    for customer in customers:
        new, err = sync_channel(customer)
        total_new += new
        total_err += err
        # Small delay to respect Slack rate limits (Tier 3 = 50 req/min)
        time.sleep(0.3)

    log(f"Done. new_messages={total_new}, errors={total_err}")


if __name__ == "__main__":
    main()
