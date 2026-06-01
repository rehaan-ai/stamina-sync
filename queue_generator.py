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
import anthropic
from supabase import create_client

DRY_RUN    = "--dry-run" in sys.argv
PAIR_FILTER = next((sys.argv[i+1] for i, a in enumerate(sys.argv) if a == "--pair"), None)

now       = datetime.now(timezone.utc)
today_str = now.strftime("%Y-%m-%d")
yesterday_str = (now - timedelta(days=1)).strftime("%Y-%m-%d")

# ── Credentials ───────────────────────────────────────────────────────────────

SUPABASE_URL   = os.environ.get("SUPABASE_URL", "https://jgvyeavyffenvuhphejg.supabase.co")
SUPABASE_KEY   = os.environ.get("SUPABASE_KEY")
ANTHROPIC_KEY  = os.environ.get("ANTHROPIC_KEY")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY")

RESEND_FROM   = "Stamina <stamina@reports.stamina.io>"
AMARTYA_EMAIL = "amartya@stamina.io"
BCC_EMAILS    = ["arjun@stamina.io"]
TEST_EMAIL    = os.environ.get("TEST_EMAIL")

sb     = create_client(SUPABASE_URL, SUPABASE_KEY)
claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

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

SLACK (last 24h) + PYLON ISSUES (open) + CS CALL SUMMARIES (last 48h) → TICKETING
  Customer-raised signals from three sources:
  - Slack: customer questions, requests, concerns
  - CS calls: anything the customer said or that Stamina committed to on the call
  - Standup summaries: context only — use to understand account status, NOT to open new tickets
  NOT iteration or upsell sources.

CAMPAIGN STATS + ACCOUNT METRICS + EMAIL INBOXES (7–14 days, current state) → ITERATION + UPSELL
  Performance signals. Analyse current state and trends — NOT time-boxed to 24h.
  Poor performance → Iteration. Strong performance → Upsell.

REPLY DATA → TICKETING (unreplied leads) + UPSELL (positive engagement signals)

KICKOFF CONTEXT → UPSELL timing (forward commitment progress)

---

## ITERATION — poor performance data triggers this, not the customer

Scan EVERY account's campaign stats, account metrics, and inboxes. Open iteration tickets when:

The data you receive is PRE-FLAGGED with 🚨 and ✅ markers. Every 🚨 line is an iteration signal you MUST turn into a ticket.

From CAMPAIGN STATS (minimum 800 emails sent per campaign/variant for these thresholds):
- reply_rate < 1% on any campaign/variant → iteration ticket naming exact campaign + variant
- positive_reply_rate < 0.5% on any campaign/variant → iteration ticket naming exact campaign + variant
- positive_reply_rate = 0 on active campaign → URGENT iteration ticket
- bounce_rate > 2% on any campaign → iteration ticket
- bounce_rate > 4% → URGENT iteration ticket
- negative_replies significantly outnumber positive_replies → positioning problem — name the ratio

From ACCOUNT METRICS (account_metrics_daily):
- reply_rate < 1% (avg over 7 days) → iteration ticket
- bounce_rate > 2% (avg over 7 days) → iteration ticket; > 4% → URGENT
- zero positive replies over 7 days → iteration ticket
- campaign_progress > 65% → iteration ticket: "New campaigns required — current campaigns at X% completion"
- e_l_ratio outside 400–700 (only when emails_sent_total >= 1200) → iteration ticket:
  below 400 = too few emails per lead (underutilising); above 700 = too many emails per lead (over-contacting)

From EMAIL INBOXES:
- any inbox is_active = false → iteration ticket (threshold is 0 disconnected — any disconnected is a problem)
- health_score < 90% → iteration ticket; < 80% → URGENT
- bounce_rate > 2% on any inbox → iteration ticket
- bounce_rate > 4% → URGENT

From ENGAGEMENT SIGNALS section in the data (pre-flagged — every 🚨 here is an iteration ticket):
- 🚨 CUSTOMER SLACK SILENCE > 5 days → iteration ticket: customer disengagement risk
- 🚨 NO CS CALL IN > 14 DAYS → iteration ticket: meeting cadence breach
- 🚨 NEGATIVE/POSITIVE RATIO > 3:1 → iteration ticket: positioning/messaging problem
- 🚨 A/B GAP (best variant > 2x worst) → iteration ticket: kill underperformer, name the variant

Mark URGENT if: bounce > 4% anywhere, any disconnected inbox, reply_rate = 0 for 7 days, positive replies = 0 across all campaigns, campaign_progress > 65%.
Issue field MUST cite actual numbers and campaign/variant names from the data.

### UPSELL — strong performance signals trigger this

Scan EVERY account's campaign stats, metrics, and reply data. Open upsell tickets when:

From CAMPAIGN STATS:
- reply_rate > 3% or positive_reply_rate > 1.5% on any campaign
- Positive replies consistently arriving week-over-week
- Multiple segments showing strong performance (volume expansion case)

From ACCOUNT METRICS:
- positive_replies increasing week-over-week
- reply_rate > 3% (avg) → campaigns working well, expansion opportunity
- Emails sent approaching what looks like the plan cap

From REPLY DATA:
- customer_responded = true on multiple positive leads (customer is actively converting)
- Low response delays (engaged customer = expansion-ready)

From KICKOFF CONTEXT:
- Forward commitment KPI is close to target or hit — upsell timing conversation

From SLACK (last 24h) or CS CALL SUMMARY:
- Customer mentions expansion, adding inboxes, scaling, or new segments → URGENT upsell
- CS call summary mentions customer asking about expansion, new services, or scaling → URGENT upsell

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

Sources: SLACK (last 24h), PYLON ISSUES, and CS CALL SUMMARIES (last 48h).

Open when:
- Customer Slack message: question, concern, request, or deadline mention
  → Quote exact message in source_detail with timestamp
- CS call / customer meeting summary: any customer question, concern, request, or
  stated commitment (e.g. "I'll send you that by Friday") mentioned on the call
  → Cite the call date and quote the relevant part of the summary
- Standup meeting summary: if the standup discussed a specific account's ticket
  status, update that context but do NOT open new tickets from standup alone
  (standups are for tracking, not for generating new requests)
- Open Pylon issue → ticket if not already in queue
- Stamina CSM committed something on a call → open on CSM with the call date
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
  "source": "campaign-stats | account-metrics | inbox-health | slack | pylon-issue | reply-data | kickoff-document | onboarding-check | cs-call | standup | engagement-flag",
  "source_detail": "Cite actual data: 'Campaign: SaaS Segment A, reply_rate 0.3% for 14 days' or 'Slack [timestamp]: [exact quote]'",
  "priority": "urgent | this_week | this_month",
  "owner": "CSM | GTM Engineer",
  "due_date": "YYYY-MM-DD",
  "status": "open",
  "notes": "Context with actual numbers the CSM needs to understand why this ticket exists",
  "dependencies": "",
  "low_confidence": false,
  "issue": "Specific problem with numeric evidence — e.g. 'Reply rate 0.48% (threshold 1%) for 7 days; 14 inboxes disconnected'",
  "action": "What to do, who does it, by when — e.g. 'Reconnect inboxes and rebuild messaging — GTM Engineer by June 3'",
  "lever": "Exact lever name from the list above",
  "signal": "Specific data point — e.g. 'Positive reply rate 2.3% on SaaS segment, above 1.5% threshold'",
  "upsell_action": "Specific next step for the CSM",
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
8. ONE iteration ticket per account maximum — combine ALL issues for that account into one ticket's issue and action fields
9. ONE upsell ticket per account maximum — combine all upsell signals into one ticket
9. Return ONLY this exact JSON structure — a wrapper object with a "tickets" array:
   {"tickets": [ticket1, ticket2, ticket3, ...]}
   Every 🚨 line in the data must produce at least one ticket in this array.
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

    # Last customer Slack message (for silence detection — separate from 24h window)
    last_customer_slack = (
        sb.table("slack_messages")
        .select("message_date")
        .eq("customer_id", cid)
        .eq("is_internal", False)
        .order("message_date", desc=True)
        .limit(1)
        .execute()
        .data
    )
    last_customer_slack_date = last_customer_slack[0]["message_date"] if last_customer_slack else None

    # Meetings in last 48h — catch yesterday's standup + recent CS calls
    two_days_ago = (now - timedelta(days=2)).strftime("%Y-%m-%d")
    recent_calls = (
        sb.table("meetings")
        .select("title, meeting_date, meeting_type, summary_text")
        .eq("customer_id", cid)
        .gte("meeting_date", two_days_ago)
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

    # Positive leads — fetch ALL (unreplied + slow response > 2h)
    unreplied_leads = []
    if brand_id:
        all_positive = (
            sb.table("reply_data")
            .select("prospect_first_name, prospect_last_name, prospect_company, "
                    "reply_body, reply_label, replied_at, campaign_name, "
                    "customer_responded, customer_response_delay_hrs")
            .eq("brand_id", brand_id)
            .in_("reply_label", ["positive", "interested"])
            .order("replied_at", desc=True)
            .limit(30)
            .execute()
            .data
        )
        # Flag: not responded at all OR responded but took > 2 hours
        unreplied_leads = [
            r for r in all_positive
            if not r.get("customer_responded")
            or (r.get("customer_response_delay_hrs") or 0) > 2
        ]

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
        "customer":               customer,
        "slack":                  slack,
        "last_customer_slack_date": last_customer_slack_date,
        "recent_calls":           recent_calls,
        "metrics":                metrics,
        "inboxes":                inboxes,
        "campaigns":              campaigns,
        "unreplied_leads":        unreplied_leads,
        "issues":                 issues,
        "kickoff_context":        kickoff_context,
        "onboarding_risk":        onboarding_risk,
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
        inbox_summary += f"  🚨 CRITICAL: {disconnected_inboxes} INBOXES DISCONNECTED — URGENT ITERATION"
    elif disconnected_inboxes > 0:
        inbox_summary += f"  🚨 ALERT: {disconnected_inboxes} inbox(es) disconnected — threshold is 0"

    # ── Onboarding flag ──
    onboarding_flag = ""
    if data["onboarding_risk"]:
        days_old = (now.date() - datetime.fromisoformat(
            c.get("created_at","").replace("Z","+00:00")).date()).days
        onboarding_flag = (
            f"\n🚨 ONBOARDING RISK: Added {days_old} days ago — "
            f"Active inboxes: {active_inboxes}, Recent calls: {len(data['recent_calls'])}"
        )

    # Standard thresholds
    THRESH_REPLY_RATE_POOR     = 1.0   # % — below this = iteration
    THRESH_REPLY_RATE_GOOD     = 3.0   # % — above this = upsell signal
    THRESH_POS_REPLY_RATE_POOR = 0.5   # % — below this = iteration
    THRESH_POS_REPLY_RATE_GOOD = 1.5   # % — above this = upsell signal
    THRESH_BOUNCE_ALERT        = 2.0   # % — above this = iteration
    THRESH_BOUNCE_CRITICAL     = 4.0   # % — above this = urgent iteration
    THRESH_DISCONNECTED        = 0     # any disconnected inbox = alert
    THRESH_HEALTH_WARN         = 90    # % — below this = warning
    THRESH_HEALTH_ALERT        = 80    # % — below this = alert

    # ── Metrics — show all 7 days + pre-flag against thresholds ──
    metrics = data["metrics"]
    metrics_lines = []
    def pct(v): return f"{round(float(v),2)}%" if v is not None else "N/A"

    if metrics:
        for m in metrics:
            rr   = m.get("reply_rate")
            pr   = m.get("positive_replies", 0)
            br   = m.get("bounce_rate")
            sent = m.get("emails_sent_total") or m.get("number_of_emails_sent", 0)
            rr_flag = (f" 🚨 BELOW {THRESH_REPLY_RATE_POOR}% THRESHOLD"
                       if (rr is not None and rr < THRESH_REPLY_RATE_POOR) else
                       (f" ✅ ABOVE {THRESH_REPLY_RATE_GOOD}%" if (rr is not None and rr > THRESH_REPLY_RATE_GOOD) else ""))
            br_flag = (f" 🚨 CRITICAL — ABOVE {THRESH_BOUNCE_CRITICAL}%"
                       if (br is not None and br > THRESH_BOUNCE_CRITICAL) else
                       (f" 🚨 ABOVE {THRESH_BOUNCE_ALERT}% THRESHOLD"
                        if (br is not None and br > THRESH_BOUNCE_ALERT) else ""))
            metrics_lines.append(
                f"  {m.get('date','?')}: sent={sent}, "
                f"reply_rate={pct(rr)}{rr_flag}, "
                f"positive_replies={pr}, "
                f"bounce={pct(br)}{br_flag}, "
                f"live_campaigns={m.get('live_campaigns',0)}"
            )
        # Aggregate summary + flags
        avg_rr  = sum(m.get("reply_rate")  or 0 for m in metrics) / len(metrics)
        avg_br  = sum(m.get("bounce_rate") or 0 for m in metrics) / len(metrics)
        total_pr = sum(m.get("positive_replies") or 0 for m in metrics)
        metrics_lines.append(
            f"\n  7-day averages: reply_rate={round(avg_rr,2)}% (threshold: {THRESH_REPLY_RATE_POOR}%), "
            f"bounce={round(avg_br,2)}% (threshold: {THRESH_BOUNCE_ALERT}%), "
            f"total_positive_replies={total_pr}"
        )
        if avg_rr < THRESH_REPLY_RATE_POOR:
            metrics_lines.append(
                f"  🚨 ITERATION: reply_rate {round(avg_rr,2)}% is BELOW {THRESH_REPLY_RATE_POOR}% threshold for 7 days")
        if avg_br > THRESH_BOUNCE_CRITICAL:
            metrics_lines.append(
                f"  🚨 URGENT ITERATION: bounce {round(avg_br,2)}% is ABOVE {THRESH_BOUNCE_CRITICAL}% — critical")
        elif avg_br > THRESH_BOUNCE_ALERT:
            metrics_lines.append(
                f"  🚨 ITERATION: bounce {round(avg_br,2)}% is ABOVE {THRESH_BOUNCE_ALERT}% threshold")
        if total_pr == 0:
            metrics_lines.append(f"  🚨 ITERATION: ZERO positive replies in 7 days")
        if avg_rr > THRESH_REPLY_RATE_GOOD:
            metrics_lines.append(
                f"  ✅ UPSELL SIGNAL: reply_rate {round(avg_rr,2)}% is ABOVE {THRESH_REPLY_RATE_GOOD}% — campaigns working well")

        # campaign_progress > 65% → new campaigns needed
        for m in metrics:
            cp = m.get("campaign_progress")
            if cp is not None:
                try:
                    cp_val = float(str(cp).replace("%",""))
                    if cp_val > 65:
                        metrics_lines.append(
                            f"  🚨 ITERATION: campaign_progress {cp_val}% on {m.get('date','?')} "
                            f"— above 65% threshold, new campaigns required")
                        break
                except (ValueError, TypeError):
                    pass

        # e_l_ratio: flag if outside 400–700 when emails_sent_total >= 1200
        E_L_MIN, E_L_MAX, E_L_MIN_SENT = 400, 700, 1200
        for m in metrics:
            el = m.get("e_l_ratio")
            sent = m.get("emails_sent_total") or m.get("number_of_emails_sent", 0)
            if el is not None and sent is not None and (sent or 0) >= E_L_MIN_SENT:
                try:
                    el_val = float(el)
                    if el_val < E_L_MIN:
                        metrics_lines.append(
                            f"  🚨 ITERATION: e_l_ratio {el_val} on {m.get('date','?')} "
                            f"is BELOW {E_L_MIN} (acceptable range: {E_L_MIN}–{E_L_MAX}, "
                            f"emails_sent={sent})")
                    elif el_val > E_L_MAX:
                        metrics_lines.append(
                            f"  🚨 ITERATION: e_l_ratio {el_val} on {m.get('date','?')} "
                            f"is ABOVE {E_L_MAX} (acceptable range: {E_L_MIN}–{E_L_MAX}, "
                            f"emails_sent={sent})")
                except (ValueError, TypeError):
                    pass
    else:
        metrics_lines = ["  No account metrics data"]
    metrics_text = "\n".join(metrics_lines)

    # ── Email inboxes — pre-flag every unhealthy inbox ──
    inbox_detail_lines = []
    for inbox in data["inboxes"]:
        br_raw = inbox.get("bounce_rate") or 0
        br  = round(float(br_raw), 2)
        hs  = inbox.get("health_score") or 100
        active = inbox.get("is_active")
        flags = []
        if not active:
            flags.append("🚨 DISCONNECTED — threshold is 0 disconnected inboxes")
        if br > THRESH_BOUNCE_CRITICAL:
            flags.append(f"🚨 BOUNCE {br}% — CRITICAL (threshold: {THRESH_BOUNCE_ALERT}%)")
        elif br > THRESH_BOUNCE_ALERT:
            flags.append(f"🚨 BOUNCE {br}% — ABOVE {THRESH_BOUNCE_ALERT}% THRESHOLD")
        if hs < THRESH_HEALTH_ALERT:
            flags.append(f"🚨 HEALTH {hs}% — BELOW {THRESH_HEALTH_ALERT}%")
        elif hs < THRESH_HEALTH_WARN:
            flags.append(f"⚠ HEALTH {hs}% — BELOW {THRESH_HEALTH_WARN}%")
        flag_str = " | ".join(flags) if flags else "✓ healthy"
        inbox_detail_lines.append(
            f"  {inbox['email_account']}: active={active}, health={hs}, bounce={br}% — {flag_str}"
        )
    # Also use customers.disconnected_inboxes if no inbox detail available
    if not inbox_detail_lines:
        if disconnected_inboxes > THRESH_DISCONNECTED:
            inbox_detail_lines.append(
                f"  🚨 {disconnected_inboxes} DISCONNECTED INBOXES — threshold is 0 (from Pylon sync)"
            )
        else:
            inbox_detail_lines.append("  No inbox detail in DB — check Pylon")
    inbox_detail_text = "\n".join(inbox_detail_lines)

    # ── Campaign performance — pre-flag poor campaigns + underperforming variants ──
    CAMP_MIN_SENT = 800  # minimum emails sent to consider a campaign/variant statistically
    campaigns = sorted(data["campaigns"], key=lambda x: x.get("positive_reply_rate") or 0, reverse=True)
    camp_lines = []
    for camp in campaigns[:8]:  # top 8 only to keep prompt size manageable
        rr   = round(float(camp.get("reply_rate") or 0), 2)
        prr  = round(float(camp.get("positive_reply_rate") or 0), 2)
        br   = round(float(camp.get("bounce_rate") or 0), 2)
        pr   = camp.get("positive_replies") or 0
        nr   = camp.get("negative_replies") or 0
        sent = camp.get("emails_sent", 0) or 0
        flags = []
        # Only flag underperformance if minimum sample size reached
        if sent >= CAMP_MIN_SENT:
            if rr < THRESH_REPLY_RATE_POOR:
                flags.append(
                    f"🚨 UNDERPERFORMING VARIANT — reply_rate {rr}% BELOW {THRESH_REPLY_RATE_POOR}% "
                    f"(campaign: '{camp['campaign_name']}', variant: '{camp.get('variant_name','—')}')")
            if prr < THRESH_POS_REPLY_RATE_POOR:
                flags.append(
                    f"🚨 UNDERPERFORMING VARIANT — positive_reply_rate {prr}% BELOW {THRESH_POS_REPLY_RATE_POOR}% "
                    f"(campaign: '{camp['campaign_name']}', variant: '{camp.get('variant_name','—')}')")
            if prr == 0:
                flags.append(
                    f"🚨 URGENT — ZERO positive replies on '{camp['campaign_name']}' / '{camp.get('variant_name','—')}' "
                    f"({sent} emails sent)")
            if br > THRESH_BOUNCE_CRITICAL:
                flags.append(
                    f"🚨 URGENT — bounce {br}% CRITICAL on '{camp['campaign_name']}' / '{camp.get('variant_name','—')}'")
            elif br > THRESH_BOUNCE_ALERT:
                flags.append(
                    f"🚨 bounce {br}% ABOVE {THRESH_BOUNCE_ALERT}% on '{camp['campaign_name']}' / '{camp.get('variant_name','—')}'")
        if prr > THRESH_POS_REPLY_RATE_GOOD:
            flags.append(f"✅ UPSELL SIGNAL: positive_reply_rate {prr}% ABOVE {THRESH_POS_REPLY_RATE_GOOD}%")
        if rr > THRESH_REPLY_RATE_GOOD:
            flags.append(f"✅ UPSELL SIGNAL: reply_rate {rr}% ABOVE {THRESH_REPLY_RATE_GOOD}%")
        flag_str = " | ".join(flags) if flags else ""
        camp_lines.append(
            f"  [{camp.get('snapshot_date','')[:10]}] {camp['campaign_name']} "
            f"[{camp.get('segment','')}|{camp.get('variant_name','')}]: "
            f"sent={camp.get('emails_sent',0)}, rr={rr}%, prr={prr}%, "
            f"+replies={pr}, -replies={nr}, bounce={br}%"
            + (f"\n    → {flag_str}" if flag_str else "")
        )
    camp_text = "\n".join(camp_lines) or "  None"

    # Last 24h Slack — customer messages only, trimmed
    slack_text = "\n".join(
        f"  [{s['message_date'][:16]}] {'CSM' if s['is_internal'] else 'CUSTOMER'}: "
        f"{s['text'][:150]}"
        for s in data["slack"][:8]
    ) or "  No messages in last 24h"

    # Recent meetings (last 48h) — separated by type, trimmed summaries
    standups = [m for m in data["recent_calls"] if m.get("meeting_type") == "standup"]
    cs_calls = [m for m in data["recent_calls"] if m.get("meeting_type") == "cs_call"]
    kickoffs = [m for m in data["recent_calls"] if m.get("meeting_type") == "kickoff"]

    def fmt_meeting(m):
        return (f"  [{m['meeting_date'][:10]}] {m['title']}: "
                f"{(m.get('summary_text') or 'No summary')[:300]}")

    standup_text = "\n".join(fmt_meeting(m) for m in standups[:2]) or "  None in last 48h"
    cs_call_text = "\n".join(fmt_meeting(m) for m in cs_calls[:2]) or "  None in last 48h"
    kickoff_text = "\n".join(fmt_meeting(m) for m in kickoffs[:1]) or "  None"

    # Positive lead SLA flags — no response OR response took > 2 hours
    unreplied_lines = []
    for r in data["unreplied_leads"]:
        name      = f"{r.get('prospect_first_name','')} {r.get('prospect_last_name','')}".strip()
        co        = r.get("prospect_company", "")
        hrs       = round(r.get("customer_response_delay_hrs") or 0, 1)
        responded = r.get("customer_responded", False)
        replied_at = (r.get("replied_at") or "")[:10]
        snippet   = (r.get("reply_body") or "")[:200]

        if not responded:
            unreplied_lines.append(
                f"  🚨 NO RESPONSE: {name} ({co}) — prospect replied {replied_at}, "
                f"customer has NOT responded\n"
                f"    Prospect said: \"{snippet}\""
            )
        elif hrs > 2:
            unreplied_lines.append(
                f"  ⚠ SLOW RESPONSE ({hrs}h — threshold: 2h): {name} ({co}) — "
                f"prospect replied {replied_at}\n"
                f"    Prospect said: \"{snippet}\""
            )
    unreplied_text = "\n".join(unreplied_lines) or "  None"

    # Open issues
    issues_text = "\n".join(
        f"  [{i['priority']}] {i['title']} ({i['status']})"
        for i in data["issues"]
    ) or "  None"

    # ── 1. Customer Slack silence (>5 days) ──────────────────────────────────
    silence_flag = ""
    last_slack_date = data.get("last_customer_slack_date")
    if last_slack_date:
        try:
            days_silent = (now.date() - datetime.fromisoformat(
                last_slack_date.replace("Z","+00:00")).date()).days
            if days_silent > 5:
                silence_flag = (f"🚨 CUSTOMER SLACK SILENCE: {days_silent} days since last customer "
                                f"message (threshold: 5 days). Last message: {last_slack_date[:10]}")
        except Exception:
            pass
    else:
        silence_flag = "🚨 CUSTOMER SLACK SILENCE: No customer messages on record in Slack channel"

    # ── 2. No CS call in >14 days ─────────────────────────────────────────────
    meeting_flag = ""
    last_meeting = c.get("last_meeting_date")
    if last_meeting:
        try:
            days_no_call = (now.date() - datetime.fromisoformat(
                last_meeting.replace("Z","+00:00")).date()).days
            if days_no_call > 14:
                meeting_flag = (f"🚨 NO CS CALL IN {days_no_call} DAYS (threshold: 14 days = every 2 weeks). "
                                f"Last call: {last_meeting[:10]}")
        except Exception:
            pass
    else:
        meeting_flag = "🚨 NO CS CALL ON RECORD for this account"

    # ── 3. Negative to positive reply ratio (>3:1, min 200 sent) ─────────────
    neg_pos_flag = ""
    total_pos_camp = sum(camp.get("positive_replies") or 0 for camp in data["campaigns"])
    total_neg_camp = sum(camp.get("negative_replies") or 0 for camp in data["campaigns"])
    total_sent_camp = sum(camp.get("emails_sent") or 0 for camp in data["campaigns"])
    if total_sent_camp >= 200 and total_pos_camp > 0 and total_neg_camp > 3 * total_pos_camp:
        neg_pos_flag = (f"🚨 NEGATIVE/POSITIVE RATIO: {total_neg_camp} negative vs {total_pos_camp} positive "
                        f"({round(total_neg_camp/total_pos_camp,1)}:1 — threshold: 3:1). "
                        f"Positioning issue — messaging needs review.")
    elif total_sent_camp >= 200 and total_pos_camp == 0 and total_neg_camp > 0:
        neg_pos_flag = (f"🚨 ZERO positive replies but {total_neg_camp} negatives across all campaigns "
                        f"({total_sent_camp} sent) — positioning problem")

    # ── 4. A/B variant performance gap (best prr > 2x worst, min 200 sent each) ──
    ab_flags = []
    camp_by_name: dict = {}
    for camp in data["campaigns"]:
        nm = camp.get("campaign_name", "")
        if nm not in camp_by_name:
            camp_by_name[nm] = []
        camp_by_name[nm].append(camp)
    for camp_name, variants in camp_by_name.items():
        eligible = [v for v in variants if (v.get("emails_sent") or 0) >= 200]
        if len(eligible) < 2:
            continue
        prrs = [(v.get("variant_name","?"), round(float(v.get("positive_reply_rate") or 0), 2))
                for v in eligible]
        best_v, best_prr   = max(prrs, key=lambda x: x[1])
        worst_v, worst_prr = min(prrs, key=lambda x: x[1])
        if worst_prr > 0 and best_prr > 2 * worst_prr:
            ab_flags.append(
                f"🚨 A/B GAP in '{camp_name}': '{best_v}' prr={best_prr}% vs '{worst_v}' prr={worst_prr}% "
                f"({round(best_prr/worst_prr,1)}x gap — threshold: 2x). Kill '{worst_v}'.")
        elif best_prr > 0 and worst_prr == 0:
            ab_flags.append(
                f"🚨 A/B GAP in '{camp_name}': '{best_v}' prr={best_prr}% vs '{worst_v}' prr=0% — kill '{worst_v}'")
    ab_flag = "\n  ".join(ab_flags) if ab_flags else ""

    # Combine engagement flags
    engagement_flags = "\n  ".join(f for f in [silence_flag, meeting_flag, neg_pos_flag, ab_flag] if f)
    engagement_text  = f"  {engagement_flags}" if engagement_flags else "  No engagement flags"

    return f"""
━━━ {c['name']} | {c.get('tier','?')} | Health:{c.get('health_score','?')} | Inboxes: {inbox_summary}{onboarding_flag}

ACCOUNT METRICS (last 7 days — pre-flagged against thresholds):
{metrics_text}

EMAIL INBOX DETAIL (pre-flagged):
{inbox_detail_text}

CAMPAIGN PERFORMANCE (last 14 days — pre-flagged):
{camp_text}

ENGAGEMENT SIGNALS:
{engagement_text}

SLACK MESSAGES (last 24h):
{slack_text}

STANDUP MEETINGS (last 48h — internal Stamina team only):
{standup_text}

CS CALLS / CUSTOMER MEETINGS (last 48h):
{cs_call_text}

KICKOFF CALLS:
{kickoff_text}

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


# ── Claude ticket generation — batched to respect rate limits ─────────────────

BATCH_SIZE = 10  # accounts per Claude call — keeps prompt under 25K tokens

def generate_new_tickets(pair: dict, accounts_signals: list) -> list:
    """
    Process accounts in batches of BATCH_SIZE to stay under Anthropic rate limits.
    All batches are merged into one ticket list → one PDF → one email.
    """
    all_tickets = []
    batches = [accounts_signals[i:i+BATCH_SIZE]
               for i in range(0, len(accounts_signals), BATCH_SIZE)]

    for batch_num, batch in enumerate(batches, 1):
        if len(batches) > 1:
            log(f"    Batch {batch_num}/{len(batches)} ({len(batch)} accounts)...")
            if batch_num > 1:
                time.sleep(30)  # brief pause between batches

        signals_block = "\n".join(format_signals_for_prompt(s) for s in batch)

        user_prompt = f"""Generate the daily ticket queue for {pair['pair_name']}.
Date: {today_str}
Pair: {pair['pair_name']} ({', '.join(pair['csm_emails'])})
Accounts in this batch: {len(batch)} of {len(accounts_signals)} total

Analyse every signal below and generate tickets. Classify each signal correctly.
Return ONLY a valid JSON array of ticket objects.

{signals_block}
"""

        try:
            response = claude.messages.create(
                model="claude-sonnet-4-5-20250929",
                system=QUEUE_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
                temperature=0,  # deterministic — reduces malformed JSON
                max_tokens=4000,
            )
            text = response.content[0].text
            try:
                from json_repair import repair_json
                # json-repair fixes unterminated strings, missing commas, etc.
                json_match = re.search(r'\{.*\}', text, re.DOTALL)
                candidate = json_match.group() if json_match else text
                raw = json.loads(repair_json(candidate))
            except Exception:
                # Final fallback: try array match with repair
                try:
                    from json_repair import repair_json
                    arr_match = re.search(r'\[.*\]', text, re.DOTALL)
                    if arr_match:
                        raw = json.loads(repair_json(arr_match.group()))
                    else:
                        log(f"    Warning: batch {batch_num} JSON parse failed — skipping")
                        continue
                except Exception:
                    log(f"    Warning: batch {batch_num} JSON parse failed — skipping")
                    continue

            # Extract ticket list from response
            if isinstance(raw, list):
                batch_tickets = raw
            elif "tickets" in raw:
                batch_tickets = raw["tickets"]
            elif "ticket_id" in raw:
                batch_tickets = [raw]
            else:
                batch_tickets = next((v for v in raw.values() if isinstance(v, list)), [])

            all_tickets.extend(batch_tickets)

        except Exception as e:
            log(f"    Warning: batch {batch_num} failed: {e}")
            continue

    return all_tickets


# ── Ticket persistence ────────────────────────────────────────────────────────

def upsert_tickets(new_tickets: list, pair_name: str, account_map: dict):
    """Store new tickets with globally unique IDs."""
    inserted = 0
    for t in new_tickets:
        # Always generate a globally unique ID — never rely on model's suggestion
        # which can clash across pairs (model reuses e.g. TKT-MC-0001)
        pair_prefix = "".join(w[0] for w in pair_name.split()[:2]).upper()
        tid = f"TKT-{pair_prefix}-{uuid.uuid4().hex[:8].upper()}"

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

    from collections import defaultdict

    def render_grouped_cards(tickets: list, accent: str, bg: str, border_color: str,
                              label1: str, field1: str, label2: str, field2: str,
                              label3: str = None, field3: str = None) -> str:
        """Render tickets grouped by account — one card per account."""
        grouped = defaultdict(list)
        for t in tickets:
            grouped[t["customer_name"]].append(t)

        html_parts = []
        for account_name, account_tickets in grouped.items():
            # Use highest priority across all tickets for this account
            priorities = [t.get("priority","this_week") for t in account_tickets]
            top_priority = "urgent" if "urgent" in priorities else ("this_week" if "this_week" in priorities else "this_month")
            top_status   = account_tickets[0].get("status","open")
            is_urgent    = top_priority == "urgent"
            card_border  = "#dc2626" if is_urgent else accent

            # Build combined issue/action/signal text
            parts1 = [t.get(field1,"") for t in account_tickets if t.get(field1)]
            parts2 = [t.get(field2,"") for t in account_tickets if t.get(field2)]
            combined1 = "<br>".join(f"• {p}" for p in parts1) if len(parts1) > 1 else (parts1[0] if parts1 else "—")
            combined2 = "<br>".join(f"• {p}" for p in parts2) if len(parts2) > 1 else (parts2[0] if parts2 else "—")

            owners   = list(dict.fromkeys(t.get("owner","CSM") for t in account_tickets))
            due_date = min((t.get("due_date","") for t in account_tickets if t.get("due_date")), default="TBD")
            ticket_ids = ", ".join(t.get("ticket_id","") for t in account_tickets[:3])

            extra_block = ""
            if label3 and field3:
                parts3 = [t.get(field3,"") for t in account_tickets if t.get(field3)]
                combined3 = "<br>".join(f"• {p}" for p in parts3) if len(parts3) > 1 else (parts3[0] if parts3 else "")
                if combined3:
                    extra_block = f'''<div style="margin-bottom:6px;">
                      <span style="color:{accent};font-weight:700;font-size:11px;">{label3}</span>
                      <div style="font-size:11px;color:#333;margin-top:3px;line-height:1.5;">{combined3}</div></div>'''

            html_parts.append(f"""
        <div style="border-left:4px solid {card_border};background:{bg};border-radius:0 8px 8px 0;
                    padding:12px 14px;margin-bottom:10px;page-break-inside:avoid;">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">
            <span style="font-weight:700;font-size:12px;">{account_name}</span>
            <span>{priority_pill(top_priority)} {status_pill(top_status)}</span>
          </div>
          <div style="margin-bottom:6px;">
            <span style="color:{accent};font-weight:700;font-size:11px;">{label1}</span>
            <div style="font-size:11px;color:#333;margin-top:3px;line-height:1.5;">{combined1}</div></div>
          <div style="margin-bottom:6px;">
            <span style="color:{accent};font-weight:700;font-size:11px;">{label2}</span>
            <div style="font-size:11px;color:#333;margin-top:3px;line-height:1.5;">{combined2}</div></div>
          {extra_block}
          <div style="font-size:9px;color:#aaa;border-top:1px solid {border_color};padding-top:5px;margin-top:6px;">
            Owner: {", ".join(owners)} · Due: {due_date} · {ticket_ids}</div>
        </div>""")
        return "".join(html_parts)

    def ticketing_item(t, closed=False) -> str:
        checkbox  = "☑" if closed else "☐"
        style     = "text-decoration:line-through;color:#9ca3af;" if closed else "color:#1a1a1a;"
        urgent_dot = '<span style="color:#dc2626;font-weight:700;">● </span>' if t.get("priority") == "urgent" and not closed else ""
        blocked   = f' <span style="color:#dc2626;font-size:10px;">[BLOCKED: {t.get("blocker","")}]</span>' if t.get("status") == "blocked" else ""
        lc        = ' <span style="color:#f59e0b;font-size:10px;">[low confidence]</span>' if t.get("low_confidence") else ""
        return f"""
        <div style="padding:7px 12px;border-bottom:1px solid #f0f9ff;font-size:11.5px;page-break-inside:avoid;">
          <span style="font-size:13px;">{checkbox}</span>
          {urgent_dot}<strong style="{style}">{t.get('customer_name','')}</strong>
          <span style="{style}"> — {t.get('request') or t.get('title','')}</span>{blocked}{lc}
          <div style="font-size:9.5px;color:#aaa;margin-top:2px;">
            Owner: {t.get('owner','CSM')} · Due: {t.get('due_date','TBD')} · {t.get('source_detail','')}</div>
        </div>"""

    # Build sections HTML — iteration and upsell grouped by account
    iter_html = render_grouped_cards(
        iteration, "#dc2626", "#fff5f5", "#fecaca",
        "↓ ISSUE", "issue", "→ ACTION", "action"
    ) or '<p style="color:#9ca3af;font-size:12px;padding:12px;">No iteration tickets today.</p>'

    upsell_html = render_grouped_cards(
        upsell, "#7c3aed", "#faf5ff", "#e9d5ff",
        "↑ LEVER", "lever", "⚡ SIGNAL", "signal", "→ ACTION", "upsell_action"
    ) or '<p style="color:#9ca3af;font-size:12px;padding:12px;">No upsell opportunities today.</p>'

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
        bcc_list  = []
        reply_to  = TEST_EMAIL
        log(f"  [TEST MODE] Sending to {TEST_EMAIL} only")
    else:
        cc_list  = [AMARTYA_EMAIL]
        bcc_list = BCC_EMAILS
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
    if bcc_list:
        payload["bcc"] = bcc_list

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

        # Deduplication — skip if already sent today
        already_sent = (sb.table("report_sends")
                        .select("id")
                        .eq("pair_name", pair_name)
                        .eq("report_type", "daily_queue")
                        .eq("send_date", today_str)
                        .execute().data)
        if already_sent:
            log(f"  ✓ Queue already sent to {pair_name} today — skipping")
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
            # Record as sent to prevent duplicates
            try:
                sb.table("report_sends").insert({
                    "pair_name": pair_name,
                    "report_type": "daily_queue",
                    "send_date": today_str
                }).execute()
            except Exception:
                pass  # unique constraint — already recorded
            log(f"  ✓ Queue PDF emailed to {pair.get('report_email')}")
        except Exception as e:
            log(f"  ERROR rendering/sending PDF: {e}")

        # Rate limit buffer between pairs — Anthropic has 30K input tokens/min limit
        log(f"  Waiting 60s before next pair to respect rate limits...")
        time.sleep(60)

    log("\nQueue generator complete.")


if __name__ == "__main__":
    main()
