#!/usr/bin/env python3
"""
Stamina CS Intelligence — queue_generator.py

Generates the daily ticket queue for each CSM pair. Runs at 5am IST every day.

What it does (mirrors the OS doc 8-step nightly process):
  Step 1  — Status reconciliation (standup recordings not in system — skipped)
  Step 2  — Customer-request tickets from last 24h Slack + Fathom calls
  Step 3  — Real-time alert promotion (bounce spikes, warmup drops, SLA breaches)
  Step 4  — Sprint carry-forward (open tickets from tickets table roll forward)
  Step 5  — Reporting auto-suggests (unreplied positive leads re-flagged daily)
  Step 6  — Priority sort and section grouping (iteration / upsell / ticketing)
  Step 7  — Stats roll-up (header strip counts)
  Step 8  — PDF generated and emailed to pair + Amartya CC

Output: one PDF per CSM pair, three sections:
  ITERATION  (red)    — underperforming KPIs, deliverability issues, campaign rebuilds
  UPSELL     (purple) — expansion signals, forward commitment progress, unprompted customer pulls
  TICKETING  (cyan)   — customer requests, Slack asks, Stamina-side commitments, onboarding risks

Usage:
  python3 queue_generator.py             # live run
  python3 queue_generator.py --dry-run   # print what would happen, no writes/sends
  python3 queue_generator.py --pair "Dan"  # run for one pair only (testing)
"""

import base64
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

import requests
from openai import OpenAI
from supabase import create_client

DRY_RUN    = "--dry-run" in sys.argv
PAIR_FILTER = next((sys.argv[i+1] for i, a in enumerate(sys.argv) if a == "--pair"), None)

now       = datetime.now(timezone.utc)
today_str = now.strftime("%Y-%m-%d")
yesterday_str = (now - timedelta(days=1)).strftime("%Y-%m-%d")

# ── Credentials ───────────────────────────────────────────────────────────────

SUPABASE_URL   = os.environ.get("SUPABASE_URL", "https://jgvyeavyffenvuhphejg.supabase.co")
SUPABASE_KEY   = os.environ.get("SUPABASE_KEY")
OPENAI_KEY     = os.environ.get("OPENAI_KEY")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY")

RESEND_FROM   = "Stamina <stamina@reports.stamina.io>"
AMARTYA_EMAIL = "amartya@stamina.io"
TEST_EMAIL    = os.environ.get("TEST_EMAIL")

sb     = create_client(SUPABASE_URL, SUPABASE_KEY)
openai = OpenAI(api_key=OPENAI_KEY)

LOGO_PATH = os.path.join(os.path.dirname(__file__), "logo.png")
with open(LOGO_PATH, "rb") as _f:
    LOGO_B64 = base64.b64encode(_f.read()).decode()

# ── Logging ───────────────────────────────────────────────────────────────────

def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def with_retry(fn, retries=3, delay=5, label=""):
    for attempt in range(1, retries + 1):
        try:
            return fn()
        except Exception as e:
            if attempt == retries:
                raise
            log(f"  Retry {attempt}/{retries} for {label}: {e} — waiting {delay}s")
            time.sleep(delay)

# ── System prompt ─────────────────────────────────────────────────────────────

QUEUE_SYSTEM_PROMPT = """
You are Stamina CS Intelligence generating the daily ticket queue for a CSM pair.

Return a JSON array of tickets. Every actionable signal becomes a ticket.
When uncertain, set low_confidence=true and open anyway.

---

## DATA SCOPE

SLACK (last 24h) + PYLON ISSUES (open) → TICKETING only
  Customer-raised signals. A customer Slack message is a service request or concern.
  A Pylon issue is something flagged by customer or CSM. NOT iteration or upsell sources.

CAMPAIGN STATS + ACCOUNT METRICS + EMAIL INBOXES (7–14 days, current state) → ITERATION + UPSELL
  Performance signals. Analyse current state and trends — NOT time-boxed to 24h.
  Poor performance → Iteration. Strong performance → Upsell.

REPLY DATA → TICKETING (unreplied leads) + UPSELL (positive engagement signals)

KICKOFF CONTEXT → UPSELL timing (forward commitment progress)

---

## ITERATION — poor performance data triggers this, not the customer

Scan EVERY account's campaign stats, account metrics, and inboxes. Open iteration tickets when:

From CAMPAIGN STATS:
- reply_rate < 1% on any active campaign
- positive_reply_rate = 0 on a campaign running 2+ weeks
- negative_replies significantly outnumber positive_replies (positioning problem — name the ratio)
- bounce_rate > 2% on any campaign
- One variant performing far worse than others — kill it

From ACCOUNT METRICS (account_metrics_daily):
- reply_rate declining for 2+ consecutive periods
- positive_replies dropped significantly vs prior period
- bounce_rate trending up

From EMAIL INBOXES:
- is_active = false on any inbox — URGENT if 2+ disconnected
- health_score < 90 on any inbox
- bounce_rate > 2% on any inbox

Mark URGENT if: bounce > 4%, 3+ inboxes disconnected, reply_rate = 0 for 2+ weeks, or positive replies = 0 across all campaigns.
Issue field MUST cite actual numbers: "reply_rate 0.3% (campaign: Segment A Variant B), declining 3 weeks"

### UPSELL — strong performance signals trigger this

Scan EVERY account's campaign stats, metrics, and reply data. Open upsell tickets when:

From CAMPAIGN STATS:
- reply_rate > 3% or positive_reply_rate > 1.5% on any campaign
- Positive replies consistently arriving week-over-week
- Multiple segments showing strong performance (volume expansion case)

From ACCOUNT METRICS:
- positive_replies increasing week-over-week
- Emails sent approaching what looks like the plan cap

From REPLY DATA:
- customer_responded = true on multiple positive leads (customer is actively converting)
- Low response delays (engaged customer = expansion-ready)

From KICKOFF CONTEXT:
- Forward commitment KPI is close to target or hit — upsell timing conversation

From SLACK (last 24h):
- Customer mentions expansion, adding inboxes, scaling, or new segments → URGENT upsell

Lever to assign:
- Approaching email volume cap → Higher Email Volume
- Wants more specific targeting → Custom Signals
- Generic copy discussion → Custom Personalization
- Large contact list → Larger Contact Database
- Multiple ICPs → Credit Volume
- CRM/dialer/automation mentioned → Custom Services
- Agency for clients → Whitelabel

Signal field MUST cite actual numbers: "positive_reply_rate 2.8% on Segment A, up from 1.1% last week"

### CUSTOMER-REQUEST (ticketing) — customer or Stamina commitment triggers this

Sources: SLACK (last 24h) and PYLON ISSUES only.

Open when:
- Customer Slack message: question, concern, request, or deadline mention
  → Quote exact message in source_detail with timestamp
- Open Pylon issue → ticket if not already in queue
- Stamina CSM committed something on a call → open on CSM
- Positive lead unreplied 24h+ → URGENT (SLA breach)
- Account 2–7 days old with 0 active inboxes OR 0 meetings → onboarding risk

Grey zone: customer asks to change campaign AND data shows it's underperforming → open BOTH a customer-request AND an iteration ticket.

---

## TICKET SCHEMA

{
  "ticket_id": "TKT-[CUSTOMER_INITIALS]-[NNNN]",
  "customer_name": "exact account name",
  "type": "iteration | upsell | customer-request",
  "title": "Action-verb-led, specific. Include account name and actual metric. 'Fix 0% positive reply rate — Segment A dead for 3 weeks [Account]'",
  "source": "campaign-stats | account-metrics | inbox-health | slack | pylon-issue | reply-data | kickoff-document | onboarding-check",
  "source_detail": "Cite actual data: 'Campaign: SaaS Segment A, reply_rate 0.3% for 14 days' or 'Slack [timestamp]: [exact quote]'",
  "priority": "urgent | this_week | this_month",
  "owner": "CSM | GTM Engineer",
  "due_date": "YYYY-MM-DD",
  "status": "open",
  "notes": "Context with actual numbers the CSM needs to understand why this ticket exists",
  "dependencies": "",
  "low_confidence": false,
  "issue": "iteration: specific problem with numeric evidence — cite campaign name, metric, value, trend",
  "action": "iteration: what to do, who does it, by when",
  "lever": "upsell: exact lever name",
  "signal": "upsell: specific data point with numbers — cite campaign, metric, value",
  "upsell_action": "upsell: specific next step for the CSM",
  "request": "customer-request: one-line description of what was asked",
  "blocker": ""
}

## Priority rules
- urgent: 3+ disconnected inboxes, bounce > 4%, reply rate = 0 for 2+ weeks, unprompted expansion Slack, unreplied positive lead > 24h
- this_week: reply rate trending down, strong upsell signal, open Pylon issue, pending customer request
- this_month: forward commitment check, positive trend worth monitoring

## Non-negotiable rules
1. Iteration = performance data. Ticketing = customer actions. Upsell = strong performance data.
2. Every issue/signal field MUST cite actual numbers from the input data — no generic claims
3. Every ticket has a specific due_date
4. Slack source_detail must quote the actual message with timestamp
5. Inbox issues: cite exact inbox email, health_score, bounce_rate
6. Campaign issues: cite exact campaign name, variant, and specific metric value
7. When uncertain: low_confidence=true, still open
8. Multiple severe issues for one account = one high-priority iteration ticket covering all of them
9. Return ONLY a valid JSON array. No prose, no explanations.
"""
# ── Data gathering ────────────────────────────────────────────────────────────

def get_accounts_for_pair(pair: dict) -> list:
    ft = pair["filter_type"]
    fv = pair["filter_value"]
    q  = sb.table("customers").select(
        "id, name, domain, tier, pylon_account_id, brand_id, "
        "health_score, active_inboxes, disconnected_inboxes, created_at, tags, "
        "csm_owner, account_owner"
    ).eq("status", "active")  # Active accounts only
    if ft == "csm_owner":
        q = q.eq("csm_owner", fv)
    elif ft == "account_owner":
        q = q.eq("account_owner", fv)
    elif ft == "tag":
        q = q.contains("tags", [fv])
    return q.execute().data


def gather_account_signals(customer: dict) -> dict:
    """
    Gather all signals for one account for queue generation.
    Covers the last 24h for Slack/calls plus current state for metrics/inboxes.
    """
    cid      = customer["id"]
    brand_id = customer.get("brand_id")

    # Last 24h Slack messages (customer messages only — these generate ticketing items)
    slack = (
        sb.table("slack_messages")
        .select("user_name, is_internal, text, message_date")
        .eq("customer_id", cid)
        .gte("message_date", yesterday_str)
        .order("message_date", desc=False)
        .execute()
        .data
    )

    # Meetings in last 24h (Fathom calls — extract commitments and concerns)
    recent_calls = (
        sb.table("meetings")
        .select("title, meeting_date, meeting_type, summary_text")
        .eq("customer_id", cid)
        .gte("meeting_date", yesterday_str)
        .order("meeting_date", desc=False)
        .execute()
        .data
    )

    # Latest metrics (last 7 days for threshold comparison)
    metrics = (
        sb.table("account_metrics_daily")
        .select("date, emails_sent_total, number_of_emails_sent, reply_rate, "
                "positive_replies, bounce_rate, live_campaigns, total_leads_contacted")
        .eq("customer_id", cid)
        .gte("date", (now - timedelta(days=7)).strftime("%Y-%m-%d"))
        .order("date", desc=False)
        .execute()
        .data
    )

    # All email inboxes — health, warmup, bounce rate (real-time alert source)
    inboxes = []
    if brand_id:
        inboxes = (
            sb.table("email_inboxes")
            .select("email_account, is_active, is_warming, health_score, bounce_rate, snapshot_date")
            .eq("brand_id", brand_id)
            .order("snapshot_date", desc=True)
            .limit(20)
            .execute()
            .data
        )

    # Campaign stats (last 14 days for variant comparison and trend)
    campaigns = []
    if brand_id:
        campaigns = (
            sb.table("campaign_stats")
            .select("campaign_name, segment, variant_name, emails_sent, replies, "
                    "positive_replies, negative_replies, reply_rate, positive_reply_rate, "
                    "bounce_rate, campaign_progress, snapshot_date")
            .eq("brand_id", brand_id)
            .gte("snapshot_date", (now - timedelta(days=14)).strftime("%Y-%m-%d"))
            .order("snapshot_date", desc=False)
            .execute()
            .data
        )

    # Unreplied positive leads (reply_data) — SLA breach check
    unreplied_leads = []
    if brand_id:
        all_positive = (
            sb.table("reply_data")
            .select("prospect_first_name, prospect_last_name, prospect_company, "
                    "reply_body, reply_label, replied_at, campaign_name, "
                    "customer_responded, customer_response_delay_hrs")
            .eq("brand_id", brand_id)
            .in_("reply_label", ["positive", "interested"])
            .eq("customer_responded", False)
            .order("replied_at", desc=True)
            .limit(20)
            .execute()
            .data
        )
        unreplied_leads = all_positive

    # Open issues (Pylon)
    issues = (
        sb.table("issues")
        .select("title, status, priority, created_at")
        .eq("customer_id", cid)
        .in_("status", ["open", "in_progress"])
        .execute()
        .data
    )

    # Kickoff document (measurement contract + forward commitment)
    kickoff = (
        sb.table("kickoff_documents")
        .select("content_md, pass_number")
        .eq("customer_id", cid)
        .in_("pass_number", [1, 2])
        .execute()
        .data
    )
    kickoff_context = ""
    for k in kickoff:
        if k["content_md"] not in ("EXISTING_ACCOUNT_SKIP",):
            kickoff_context += f"[Pass {k['pass_number']}]\n{k['content_md'][:1500]}\n\n"

    # Onboarding risk check
    created_at = customer.get("created_at", "")
    onboarding_risk = False
    if created_at:
        days_old = (now.date() - datetime.fromisoformat(
            created_at.replace("Z", "+00:00")).date()).days
        if 2 <= days_old <= 7:
            if customer.get("active_inboxes", 0) == 0 or len(recent_calls) == 0:
                onboarding_risk = True

    return {
        "customer":        customer,
        "slack":           slack,
        "recent_calls":    recent_calls,
        "metrics":         metrics,
        "inboxes":         inboxes,
        "campaigns":       campaigns,
        "unreplied_leads": unreplied_leads,
        "issues":          issues,
        "kickoff_context": kickoff_context,
        "onboarding_risk": onboarding_risk,
    }


def flag(val, threshold, label, higher_is_bad=True, fmt=lambda v: f"{v}") -> str:
    """Return a pre-flagged string if val breaches threshold."""
    if val is None:
        return f"{label}: no data"
    breached = (val > threshold) if higher_is_bad else (val < threshold)
    icon = "🚨 ALERT" if breached else "✓"
    return f"{icon} {label}: {fmt(val)} (threshold: {fmt(threshold)})"


def format_signals_for_prompt(data: dict) -> str:
    """
    Format one account's signals for the GPT-4o prompt.
    Values are PRE-FLAGGED against thresholds so the model knows what is actionable.
    """
    c = data["customer"]

    # ── Customer-level inbox summary (from customers table, always available) ──
    active_inboxes      = c.get("active_inboxes") or 0
    disconnected_inboxes = c.get("disconnected_inboxes") or 0
    inbox_summary = f"{active_inboxes} active / {disconnected_inboxes} disconnected"
    if disconnected_inboxes >= 3:
        inbox_summary += f"  🚨 CRITICAL: {disconnected_inboxes} INBOXES DISCONNECTED"
    elif disconnected_inboxes > 0:
        inbox_summary += f"  ⚠ WARNING: {disconnected_inboxes} inbox(es) disconnected"

    # ── Onboarding flag ──
    onboarding_flag = ""
    if data["onboarding_risk"]:
        days_old = (now.date() - datetime.fromisoformat(
            c.get("created_at","").replace("Z","+00:00")).date()).days
        onboarding_flag = (
            f"\n🚨 ONBOARDING RISK: Added {days_old} days ago — "
            f"Active inboxes: {active_inboxes}, Recent calls: {len(data['recent_calls'])}"
        )

    # ── Metrics — show all 7 days + pre-flag bad values ──
    metrics = data["metrics"]
    metrics_lines = []
    if metrics:
        # Show each day
        for m in metrics:
            rr  = m.get("reply_rate")
            pr  = m.get("positive_replies", 0)
            br  = m.get("bounce_rate")
            sent = m.get("emails_sent_total") or m.get("number_of_emails_sent", 0)
            rr_flag = " 🚨 BELOW 1%" if (rr is not None and rr < 1) else ""
            br_flag = " 🚨 ABOVE 2% THRESHOLD" if (br is not None and br > 2) else ""
            metrics_lines.append(
                f"  {m.get('date','?')}: sent={sent}, "
                f"reply_rate={rr}%{rr_flag}, "
                f"positive_replies={pr}, "
                f"bounce={br}%{br_flag}, "
                f"live_campaigns={m.get('live_campaigns',0)}"
            )
        # Aggregate flags
        avg_rr = sum(m.get("reply_rate") or 0 for m in metrics) / len(metrics)
        avg_br = sum(m.get("bounce_rate") or 0 for m in metrics) / len(metrics)
        total_pr = sum(m.get("positive_replies") or 0 for m in metrics)
        metrics_lines.append(
            f"\n  7-day averages: avg_reply_rate={round(avg_rr,2)}%, "
            f"avg_bounce={round(avg_br,2)}%, total_positive_replies={total_pr}"
        )
        if avg_rr < 1:
            metrics_lines.append(f"  🚨 ITERATION SIGNAL: avg reply rate {round(avg_rr,2)}% is BELOW 1% threshold")
        if avg_br > 2:
            metrics_lines.append(f"  🚨 ITERATION SIGNAL: avg bounce rate {round(avg_br,2)}% is ABOVE 2% threshold")
        if total_pr == 0:
            metrics_lines.append(f"  🚨 ITERATION SIGNAL: ZERO positive replies in 7 days")
    else:
        metrics_lines = ["  No account metrics data"]
    metrics_text = "\n".join(metrics_lines)

    # ── Email inboxes — pre-flag every unhealthy inbox ──
    inbox_detail_lines = []
    for inbox in data["inboxes"]:
        br = inbox.get("bounce_rate") or 0
        hs = inbox.get("health_score") or 100
        active = inbox.get("is_active")
        flags = []
        if not active:
            flags.append("🚨 INACTIVE")
        if br > 4:
            flags.append(f"🚨 BOUNCE {br}% — FAR ABOVE 2% THRESHOLD")
        elif br > 2:
            flags.append(f"⚠ BOUNCE {br}% — ABOVE 2% THRESHOLD")
        if hs < 90:
            flags.append(f"⚠ HEALTH {hs}% — BELOW 90%")
        flag_str = " | ".join(flags) if flags else "✓ healthy"
        inbox_detail_lines.append(
            f"  {inbox['email_account']}: active={active}, health={hs}, bounce={br}% — {flag_str}"
        )
    # Also use customers.disconnected_inboxes if no inbox detail available
    if not inbox_detail_lines:
        if disconnected_inboxes > 0:
            inbox_detail_lines.append(
                f"  🚨 {disconnected_inboxes} DISCONNECTED INBOXES (from Pylon — no inbox detail in DB)"
            )
        else:
            inbox_detail_lines.append("  No inbox detail available")
    inbox_detail_text = "\n".join(inbox_detail_lines)

    # ── Campaign performance — pre-flag poor campaigns ──
    campaigns = sorted(data["campaigns"], key=lambda x: x.get("positive_reply_rate") or 0, reverse=True)
    camp_lines = []
    for camp in campaigns[:15]:
        rr   = camp.get("reply_rate") or 0
        prr  = camp.get("positive_reply_rate") or 0
        br   = camp.get("bounce_rate") or 0
        pr   = camp.get("positive_replies") or 0
        nr   = camp.get("negative_replies") or 0
        flags = []
        if rr < 1:
            flags.append(f"🚨 reply_rate {rr}% BELOW 1%")
        if prr == 0 and camp.get("emails_sent", 0) > 100:
            flags.append("🚨 ZERO positive replies")
        if br > 2:
            flags.append(f"🚨 bounce {br}% ABOVE 2%")
        if prr > 1.5:
            flags.append(f"✅ STRONG prr {prr}% — upsell signal")
        if rr > 3:
            flags.append(f"✅ STRONG reply_rate {rr}%")
        flag_str = " | ".join(flags) if flags else ""
        camp_lines.append(
            f"  [{camp.get('snapshot_date','')[:10]}] {camp['campaign_name']} "
            f"[{camp.get('segment','')}|{camp.get('variant_name','')}]: "
            f"sent={camp.get('emails_sent',0)}, rr={rr}%, prr={prr}%, "
            f"+replies={pr}, -replies={nr}, bounce={br}%"
            + (f"\n    → {flag_str}" if flag_str else "")
        )
    camp_text = "\n".join(camp_lines) or "  None"

    # Last 24h Slack (with full text for customer messages)
    slack_text = "\n".join(
        f"  [{s['message_date'][:16]}] {'CSM' if s['is_internal'] else 'CUSTOMER'}: "
        f"{s['text'][:400]}"
        for s in data["slack"]
    ) or "  No messages in last 24h"

    # Recent calls (last 24h)
    calls_text = "\n".join(
        f"  [{m['meeting_date'][:10]}] [{m['meeting_type']}] {m['title']}\n"
        f"  {(m.get('summary_text') or 'No summary')[:1000]}"
        for m in data["recent_calls"]
    ) or "  No calls in last 24h"

    # Unreplied positive leads (SLA breach)
    unreplied_text = ""
    for r in data["unreplied_leads"]:
        name = f"{r.get('prospect_first_name','')} {r.get('prospect_last_name','')}".strip()
        co   = r.get("prospect_company", "")
        hrs  = r.get("customer_response_delay_hrs") or 0
        unreplied_text += (
            f"  {name} ({co}) — replied {(r.get('replied_at') or '')[:10]}, "
            f"{round(hrs)}hrs ago still unreplied\n"
            f"  Their reply: \"{(r.get('reply_body') or '')[:300]}\"\n"
        )
    unreplied_text = unreplied_text or "  None"

    # Open issues
    issues_text = "\n".join(
        f"  [{i['priority']}] {i['title']} ({i['status']})"
        for i in data["issues"]
    ) or "  None"

    return f"""
━━━ {c['name']} | {c.get('tier','?')} | Health:{c.get('health_score','?')} | Inboxes: {inbox_summary}{onboarding_flag}

ACCOUNT METRICS (last 7 days — pre-flagged against thresholds):
{metrics_text}

EMAIL INBOX DETAIL (pre-flagged):
{inbox_detail_text}

CAMPAIGN PERFORMANCE (last 14 days — pre-flagged):
{camp_text}

SLACK MESSAGES (last 24h):
{slack_text}

RECENT CALLS (last 24h):
{calls_text}

UNREPLIED POSITIVE LEADS (SLA breach — no customer response):
{unreplied_text}

OPEN PYLON ISSUES:
{issues_text}

KICKOFF CONTEXT (measurement contract + forward commitment):
{data['kickoff_context'][:1500] if data['kickoff_context'] else 'No kickoff document available'}
"""


# ── Existing tickets (carry-forward) ─────────────────────────────────────────

def get_open_tickets(pair_name: str) -> list:
    """Fetch all non-closed tickets for this pair to carry forward."""
    return (
        sb.table("tickets")
        .select("*")
        .eq("pair_name", pair_name)
        .not_.in_("status", ["closed"])
        .execute()
        .data
    )


def get_closed_yesterday_tickets(pair_name: str) -> list:
    """Fetch tickets closed in the last 24h for the 'closed yesterday' section."""
    return (
        sb.table("tickets")
        .select("*")
        .eq("pair_name", pair_name)
        .eq("status", "closed")
        .gte("closed_at", yesterday_str)
        .execute()
        .data
    )


# ── GPT-4o ticket generation ──────────────────────────────────────────────────

def generate_new_tickets(pair: dict, accounts_signals: list) -> list:
    """Call GPT-4o to classify all signals and generate new tickets."""
    signals_block = "\n".join(
        format_signals_for_prompt(s) for s in accounts_signals
    )

    user_prompt = f"""Generate the daily ticket queue for {pair['pair_name']}.
Date: {today_str}
Pair: {pair['pair_name']} ({', '.join(pair['csm_emails'])})
Accounts: {len(accounts_signals)}

Analyse every signal below and generate tickets. Classify each signal correctly.
Return ONLY a valid JSON array of ticket objects.

{signals_block}
"""

    response = openai.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": QUEUE_SYSTEM_PROMPT},
            {"role": "user",   "content": user_prompt},
        ],
        temperature=0.2,
        max_tokens=6000,
        response_format={"type": "json_object"},
    )

    raw = json.loads(response.choices[0].message.content)
    # Handle both {"tickets": [...]} and [...] response formats
    if isinstance(raw, list):
        return raw
    return raw.get("tickets", raw.get("items", []))


# ── Ticket persistence ────────────────────────────────────────────────────────

def upsert_tickets(new_tickets: list, pair_name: str, account_map: dict):
    """Store new tickets, skip duplicates by ticket_id."""
    existing_ids = {
        t["ticket_id"] for t in
        sb.table("tickets").select("ticket_id").eq("pair_name", pair_name).execute().data
    }

    inserted = 0
    for t in new_tickets:
        tid = t.get("ticket_id") or f"TKT-{uuid.uuid4().hex[:6].upper()}"
        if tid in existing_ids:
            continue

        # Resolve customer_id from name
        cust_id = account_map.get(t.get("customer_name", ""))

        row = {
            "ticket_id":     tid,
            "customer_id":   cust_id,
            "customer_name": t.get("customer_name", "Unknown"),
            "pair_name":     pair_name,
            "type":          t.get("type", "customer-request"),
            "title":         t.get("title", "Untitled ticket"),
            "source":        t.get("source", "unknown"),
            "source_detail": t.get("source_detail", ""),
            "priority":      t.get("priority", "this_week"),
            "owner":         t.get("owner", "CSM"),
            "due_date":      t.get("due_date"),
            "status":        "open",
            "notes":         t.get("notes", ""),
            "dependencies":  t.get("dependencies", ""),
            "issue":         t.get("issue"),
            "action":        t.get("action"),
            "lever":         t.get("lever"),
            "signal":        t.get("signal"),
            "upsell_action": t.get("upsell_action"),
            "request":       t.get("request"),
            "blocker":       t.get("blocker"),
        }
        sb.table("tickets").insert(row).execute()
        inserted += 1

    return inserted


# ── PDF rendering ─────────────────────────────────────────────────────────────

def render_queue_pdf(
    all_tickets: list,
    closed_yesterday: list,
    pair_name: str
) -> bytes:
    from weasyprint import HTML as WP_HTML

    iteration  = [t for t in all_tickets if t["type"] == "iteration"]
    upsell     = [t for t in all_tickets if t["type"] == "upsell"]
    ticketing  = [t for t in all_tickets if t["type"] == "customer-request"]

    # Sort each section: urgent → blocked → this_week → this_month, then by customer
    priority_order = {"urgent": 0, "this_week": 1, "this_month": 2}
    def sort_key(t):
        p = priority_order.get(t.get("priority", "this_week"), 1)
        b = 0 if t.get("status") == "blocked" else 1
        return (p, b, t.get("customer_name", ""))

    iteration  = sorted(iteration,  key=sort_key)
    upsell     = sorted(upsell,     key=sort_key)
    ticketing  = sorted(ticketing,  key=sort_key)

    # Stats
    blocked_count = sum(1 for t in all_tickets if t.get("status") == "blocked")
    today_label   = now.strftime("%B %d, %Y")

    def priority_pill(p):
        colors = {"urgent": "#dc2626", "this_week": "#d97706", "this_month": "#6b7280"}
        c = colors.get(p, "#6b7280")
        return f'<span style="background:{c};color:white;padding:2px 8px;border-radius:12px;font-size:10px;font-weight:700;text-transform:uppercase;">{p}</span>'

    def status_pill(s):
        colors = {"open": "#3b82f6", "in_progress": "#8b5cf6", "blocked": "#dc2626",
                  "deferred": "#9ca3af", "closed": "#10b981"}
        c = colors.get(s, "#6b7280")
        return f'<span style="background:{c};color:white;padding:2px 8px;border-radius:12px;font-size:10px;text-transform:uppercase;">{s}</span>'

    def iteration_card(t) -> str:
        border = "#dc2626" if t.get("status") == "blocked" or t.get("priority") == "urgent" else "#dc2626"
        lc = ' <span style="color:#f59e0b;font-size:10px;">[low confidence]</span>' if t.get("low_confidence") else ""
        return f"""
        <div style="border-left:4px solid {border};background:#fff5f5;border-radius:0 8px 8px 0;
                    padding:14px 16px;margin-bottom:12px;">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
            <span style="font-weight:700;font-size:13px;">{t.get('customer_name','')}</span>
            <span>{priority_pill(t.get('priority','this_week'))} {status_pill(t.get('status','open'))}</span>
          </div>
          <div style="font-size:11px;color:#666;margin-bottom:8px;">{t.get('ticket_id','')} · {t.get('title','')}{lc}</div>
          <div style="margin-bottom:6px;"><span style="color:#dc2626;font-weight:700;font-size:11px;">↓ ISSUE</span>
            <div style="font-size:12px;color:#333;margin-top:3px;">{t.get('issue','')}</div></div>
          <div style="margin-bottom:6px;"><span style="color:#dc2626;font-weight:700;font-size:11px;">→ ACTION</span>
            <div style="font-size:12px;color:#333;margin-top:3px;">{t.get('action','')}</div></div>
          <div style="font-size:10px;color:#999;border-top:1px solid #fecaca;padding-top:6px;margin-top:8px;">
            Owner: {t.get('owner','CSM')} · Due: {t.get('due_date','TBD')} · Source: {t.get('source_detail','')}</div>
        </div>"""

    def upsell_card(t) -> str:
        border = "#dc2626" if t.get("priority") == "urgent" else "#7c3aed"
        lc = ' <span style="color:#f59e0b;font-size:10px;">[low confidence]</span>' if t.get("low_confidence") else ""
        return f"""
        <div style="border-left:4px solid {border};background:#faf5ff;border-radius:0 8px 8px 0;
                    padding:14px 16px;margin-bottom:12px;">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
            <span style="font-weight:700;font-size:13px;">{t.get('customer_name','')}</span>
            <span>{priority_pill(t.get('priority','this_week'))} {status_pill(t.get('status','open'))}</span>
          </div>
          <div style="font-size:11px;color:#666;margin-bottom:8px;">{t.get('ticket_id','')} · {t.get('title','')}{lc}</div>
          <div style="margin-bottom:6px;"><span style="color:#7c3aed;font-weight:700;font-size:11px;">↑ LEVER</span>
            <div style="font-size:12px;color:#333;margin-top:3px;">{t.get('lever','')}</div></div>
          <div style="margin-bottom:6px;"><span style="color:#7c3aed;font-weight:700;font-size:11px;">⚡ SIGNAL</span>
            <div style="font-size:12px;color:#333;margin-top:3px;">{t.get('signal','')}</div></div>
          <div style="margin-bottom:6px;"><span style="color:#7c3aed;font-weight:700;font-size:11px;">→ ACTION</span>
            <div style="font-size:12px;color:#333;margin-top:3px;">{t.get('upsell_action','')}</div></div>
          <div style="font-size:10px;color:#999;border-top:1px solid #e9d5ff;padding-top:6px;margin-top:8px;">
            Owner: {t.get('owner','CSM')} · Due: {t.get('due_date','TBD')} · Source: {t.get('source_detail','')}</div>
        </div>"""

    def ticketing_item(t, closed=False) -> str:
        checkbox = "☑" if closed else "☐"
        style = "text-decoration:line-through;color:#9ca3af;" if closed else "color:#1a1a1a;"
        urgent_dot = '<span style="color:#dc2626;font-weight:700;">● </span>' if t.get("priority") == "urgent" and not closed else ""
        blocked_note = f' <span style="color:#dc2626;font-size:10px;">[BLOCKED: {t.get("blocker","")}]</span>' if t.get("status") == "blocked" else ""
        lc = ' <span style="color:#f59e0b;font-size:10px;">[low confidence]</span>' if t.get("low_confidence") else ""
        return f"""
        <div style="padding:8px 12px;border-bottom:1px solid #f0f9ff;font-size:12px;">
          <span style="font-size:14px;">{checkbox}</span>
          {urgent_dot}<strong style="{style}">{t.get('customer_name','')}</strong>
          <span style="{style}"> — {t.get('request') or t.get('title','')}</span>{blocked_note}{lc}
          <div style="font-size:10px;color:#9ca3af;margin-top:2px;">
            {t.get('ticket_id','')} · Source: {t.get('source_detail','')} · Owner: {t.get('owner','CSM')} · Due: {t.get('due_date','TBD')}</div>
        </div>"""

    # Build sections HTML
    iter_html   = "".join(iteration_card(t) for t in iteration) or '<p style="color:#9ca3af;font-size:12px;padding:12px;">No iteration tickets today.</p>'
    upsell_html = "".join(upsell_card(t) for t in upsell)       or '<p style="color:#9ca3af;font-size:12px;padding:12px;">No upsell opportunities today.</p>'
    ticket_html = "".join(ticketing_item(t) for t in ticketing) or '<p style="color:#9ca3af;font-size:12px;padding:12px;">No open requests.</p>'
    closed_html = "".join(ticketing_item(t, closed=True) for t in closed_yesterday)

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  @page {{ size: A4; margin: 10mm 10mm 22mm 10mm; }}
  @page :first {{ margin-top: 0; }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: "Helvetica Neue", Arial, sans-serif; background: white; color: #1a1a1a; font-size: 13px; }}
  .header {{ background: #1a2035; padding: 24px 40px; display: flex; align-items: center; justify-content: space-between; }}
  .header img {{ height: 22px; filter: brightness(0) invert(1); }}
  .header-right {{ text-align: right; }}
  .header-right .label {{ color: #8892a4; font-size: 10px; letter-spacing: 1.5px; text-transform: uppercase; }}
  .header-right .title {{ color: white; font-size: 15px; font-weight: 700; margin-top: 3px; }}
  .header-right .sub {{ color: #8892a4; font-size: 11px; margin-top: 2px; }}
  .stats-strip {{ background: #f8f9fb; border-bottom: 1px solid #e8eaed; padding: 10px 40px; display: flex; gap: 24px; align-items: center; }}
  .stat {{ text-align: center; }}
  .stat .n {{ font-size: 18px; font-weight: 700; }}
  .stat .lbl {{ font-size: 10px; color: #9ca3af; text-transform: uppercase; letter-spacing: 0.5px; }}
  .stat.red .n {{ color: #dc2626; }}
  .stat.green .n {{ color: #10b981; }}
  .section-banner {{ padding: 10px 40px; font-weight: 700; font-size: 12px; letter-spacing: 0.5px; display: flex; justify-content: space-between; align-items: center; }}
  .section-banner.red {{ background: #fef2f2; color: #dc2626; border-bottom: 2px solid #dc2626; }}
  .section-banner.purple {{ background: #faf5ff; color: #7c3aed; border-bottom: 2px solid #7c3aed; }}
  .section-banner.cyan {{ background: #f0f9ff; color: #0284c7; border-bottom: 2px solid #0284c7; }}
  .section-body {{ padding: 16px 40px; }}
  .closed-group {{ background: #f9fafb; border-radius: 6px; margin-top: 12px; padding: 4px 0; }}
  .closed-label {{ font-size: 10px; color: #9ca3af; text-transform: uppercase; letter-spacing: 1px; padding: 8px 12px; }}
  .footer {{ position: fixed; bottom: 0; left: 0; right: 0; padding: 7px 40px; border-top: 1px solid #e8eaed; display: flex; justify-content: space-between; font-size: 9px; color: #bbb; background: white; }}
</style>
</head>
<body>
<div class="header">
  <img src="data:image/png;base64,{LOGO_B64}">
  <div class="header-right">
    <div class="label">Daily Ticket Queue — Internal</div>
    <div class="title">{pair_name}</div>
    <div class="sub">{today_label}</div>
  </div>
</div>

<div class="stats-strip">
  <div class="stat"><div class="n" style="color:#dc2626;">{len(iteration)}</div><div class="lbl">Iteration</div></div>
  <div class="stat"><div class="n" style="color:#7c3aed;">{len(upsell)}</div><div class="lbl">Upsell</div></div>
  <div class="stat"><div class="n">{len(ticketing)}</div><div class="lbl">Open Tickets</div></div>
  <div class="stat red"><div class="n">{blocked_count}</div><div class="lbl">Blocked</div></div>
  <div class="stat green"><div class="n">{len(closed_yesterday)}</div><div class="lbl">Closed Yesterday</div></div>
</div>

<div class="section-banner red">
  <span>ITERATION — Underperforming customers and KPIs. Fix these.</span>
  <span>{len(iteration)} tickets</span>
</div>
<div class="section-body">{iter_html}</div>

<div class="section-banner purple">
  <span>UPSELL — Expansion opportunities. Move these forward.</span>
  <span>{len(upsell)} tickets</span>
</div>
<div class="section-body">{upsell_html}</div>

<div class="section-banner cyan">
  <span>TICKETING — Customer questions and requests. Check off as you go.</span>
  <span>{len(ticketing)} open</span>
</div>
<div class="section-body">
  {ticket_html}
  {f'<div class="closed-group"><div class="closed-label">✓ Closed Yesterday</div>{closed_html}</div>' if closed_html else ''}
</div>

<div class="footer">
  <span>Stamina CS Intelligence · {today_label}</span>
  <span>INTERNAL — {pair_name}</span>
</div>
</body>
</html>"""

    return WP_HTML(string=html).write_pdf()


# ── Email sending ─────────────────────────────────────────────────────────────

def send_queue_email(pair: dict, pdf_bytes: bytes):
    to_emails = pair.get("report_email") or pair.get("csm_emails") or []
    if not to_emails:
        log(f"  No emails for {pair['pair_name']} — skipping")
        return

    filename = f"{pair['pair_name'].replace(' ', '_')}_Queue_{today_str}.pdf"

    if TEST_EMAIL:
        to_emails = [TEST_EMAIL]
        cc_list   = []
        reply_to  = TEST_EMAIL
        log(f"  [TEST MODE] Sending to {TEST_EMAIL} only")
    else:
        cc_list  = [AMARTYA_EMAIL]
        reply_to = AMARTYA_EMAIL

    payload = {
        "from":     RESEND_FROM,
        "to":       to_emails,
        "reply_to": reply_to,
        "template": {"id": "daily-ticket-queue", "variables": {}},
        "attachments": [{"filename": filename,
                         "content": base64.b64encode(pdf_bytes).decode()}],
    }
    if cc_list:
        payload["cc"] = cc_list

    resp = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {RESEND_API_KEY}",
                 "Content-Type": "application/json"},
        json=payload, timeout=30,
    )
    resp.raise_for_status()
    log(f"  Email sent → {to_emails} (ID: {resp.json().get('id')})")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log(f"Queue generator started {'[DRY RUN] ' if DRY_RUN else ''}— {today_str}")

    pairs = sb.table("csm_pairs").select("*").eq("is_active", True).execute().data
    if PAIR_FILTER:
        pairs = [p for p in pairs if p["pair_name"] == PAIR_FILTER]

    for pair in pairs:
        pair_name = pair["pair_name"]
        log(f"\nPair: {pair_name}")

        accounts = get_accounts_for_pair(pair)
        if not accounts:
            log(f"  No accounts — skipping")
            continue
        log(f"  {len(accounts)} accounts")

        # Build account map for ticket persistence
        account_map = {a["name"]: a["id"] for a in accounts}

        # Gather signals for all accounts
        accounts_signals = []
        for account in accounts:
            try:
                signals = gather_account_signals(account)
                accounts_signals.append(signals)
            except Exception as e:
                log(f"  Signal error for {account.get('name')}: {e}")

        if not accounts_signals:
            continue

        # Carry-forward open tickets
        open_tickets   = get_open_tickets(pair_name)
        closed_yest    = get_closed_yesterday_tickets(pair_name)
        log(f"  Carry-forward: {len(open_tickets)} open, {len(closed_yest)} closed yesterday")

        if DRY_RUN:
            log(f"  [DRY RUN] Would generate tickets for {len(accounts_signals)} accounts")
            log(f"  [DRY RUN] Would email to {pair.get('report_email')}")
            continue

        # Generate new tickets via GPT-4o
        log(f"  Generating new tickets via GPT-4o...")
        try:
            new_tickets = with_retry(
                lambda: generate_new_tickets(pair, accounts_signals),
                retries=3, delay=10, label=f"queue {pair_name}"
            )
            log(f"  Generated {len(new_tickets)} new tickets")
        except Exception as e:
            log(f"  ERROR generating tickets: {e}")
            new_tickets = []

        # Persist new tickets
        inserted = upsert_tickets(new_tickets, pair_name, account_map)
        log(f"  Inserted {inserted} new tickets into DB")

        # Fetch all open tickets (carry-forward + new)
        all_open = get_open_tickets(pair_name)

        # Generate PDF
        try:
            pdf = render_queue_pdf(all_open, closed_yest, pair_name)
            send_queue_email(pair, pdf)
            log(f"  ✓ Queue PDF emailed to {pair.get('report_email')}")
        except Exception as e:
            log(f"  ERROR rendering/sending PDF: {e}")

    log("\nQueue generator complete.")


if __name__ == "__main__":
    main()
