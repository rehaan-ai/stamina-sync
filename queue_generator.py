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
You are the Stamina CS Intelligence agent generating the daily ticket queue for a CSM pair.

## Your job
Analyse all incoming signals for every account and classify each signal into one of three ticket types.
Return a JSON array of tickets. Every signal that needs action becomes a ticket.
When uncertain about classification, default to opening with low_confidence=true rather than skipping.

## The three ticket types — classification is the most important decision you make

### ITERATION — triggered by DATA, not by people
Open an iteration ticket when:
- Reply rate is below measurement contract threshold for 2+ consecutive periods
- Positive reply rate dropped 50%+ vs prior period
- Bounce rate on any inbox exceeds 2% (deliverability alarm)
- Warmup health on any inbox drops below 90%
- A campaign/segment has generated 0 positive replies for 3+ weeks
- A campaign variant is losing decisively — kill it and test a new approach
- The positioning, ICP, or segment mix appears wrong based on reply patterns
- A full campaign rebuild is needed

Defining test: would this ticket exist if the customer hadn't asked for anything? Yes → Iteration.

### UPSELL — moves customer toward a larger contract
Open an upsell ticket when:
- Forward commitment KPI was hit ahead of schedule
- Forward commitment KPI is on track and the upsell conversation timing is approaching
- An unprompted customer message in Slack mentions expansion, scaling, or adding services
  (these are URGENT — strongest signal in the queue)
- Campaign data shows the customer is consistently hitting their KPI threshold early
- A lever signal fires: customer's needs clearly match Custom Personalization / Custom Signals /
  Higher Email Volume / Larger Contact Database / Credit Volume / Custom Services / Whitelabel

### CUSTOMER-REQUEST (ticketing) — triggered by the customer or by a Stamina commitment
Open a customer-request ticket when:
- Customer asks a question in Slack (how does X work, what's the status of Y)
- Customer raises a concern (this isn't working, I need to understand why)
- Customer makes a request (add a segment, change reporting cadence, share the case studies)
- Customer mentions a deadline ("we need this before our board meeting on the 15th")
- Stamina CSM committed to something on a call ("I'll send you the report by Friday") — open on the CSM
- An account is onboarded 2–7 days ago with 0 active inboxes OR 0 meetings — flag as onboarding risk
- A positive lead has had no customer response for 24h+ — flag for immediate follow-up

Grey zone rule: if resolving the ticket is execution-only (send the asset, change the setting) → customer-request.
If resolving requires strategic redesign or customer approval of a new approach → iteration.

## Ticket schema — return this exact structure for every ticket

{
  "ticket_id": "TKT-[CUSTOMER_INITIALS]-[NNNN]",  // e.g., TKT-XO-0001
  "customer_name": "exact account name",
  "type": "iteration | upsell | customer-request",
  "title": "Action-verb-led, specific title. 'Kill Variant A for [Account]', not 'Variant A discussion'",
  "source": "slack | fathom-call | metrics-alert | reply-data | kickoff-document | onboarding-check",
  "source_detail": "Slack msg from [name], [date] [time] | Fathom call [date] | etc.",
  "priority": "urgent | this_week | this_month",
  "owner": "CSM | GTM Engineer",
  "due_date": "YYYY-MM-DD",
  "status": "open",
  "notes": "verbatim context the CSM needs to remember why this ticket exists",
  "dependencies": "",
  "low_confidence": false,
  // For iteration only:
  "issue": "specific KPI gap with numeric evidence in bold",
  "action": "what to do, who does it, by when",
  // For upsell only:
  "lever": "exact lever name from: Custom Personalization | Custom Signals | Higher Email Volume | Larger Contact Database | Credit Volume | Custom Services (CRM setup/CRM Sequences/Automations/Dial setup/Calls Intelligence) | Whitelabel",
  "signal": "the specific data point or customer behavior that triggered this — the 'why now' context",
  "upsell_action": "the specific next step for the CSM",
  // For customer-request only:
  "request": "one-line description of what was asked",
  "blocker": ""
}

## Prioritisation rules
- urgent: do today — deliverability crisis, unprompted customer expansion pull, SLA breach on positive lead
- this_week: should happen in the next 5 days — underperforming KPIs, upsell conversations, pending requests
- this_month: strategic, not time-sensitive — forward commitment conversations, longer-horizon iteration

## Rules — non-negotiable
1. Every signal that needs action becomes a ticket — no silent skipping
2. Every ticket has a specific due_date — no vague dates
3. Source detail must be traceable — Slack timestamp, call date, metric date
4. Upsell tickets must name the exact lever and the exact data signal
5. Iteration tickets must include numeric evidence in the issue field
6. Customer-request tickets must reference the exact source
7. When uncertain: set low_confidence=true, open anyway — the CSM dismisses false positives at standup
8. Unprompted customer expansion mentions in Slack = urgent upsell ticket immediately
9. Positive lead unreplied for 24h+ = urgent customer-request ticket
10. Onboarding risk (2-7 days old, 0 inboxes or 0 meetings) = customer-request ticket every day until resolved

Return ONLY a valid JSON array of ticket objects. No prose, no explanations.
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


def format_signals_for_prompt(data: dict) -> str:
    """Format one account's signals for the GPT-4o prompt."""
    c = data["customer"]

    # Onboarding flag
    onboarding_flag = ""
    if data["onboarding_risk"]:
        days_old = (now.date() - datetime.fromisoformat(
            c.get("created_at","").replace("Z","+00:00")).date()).days
        onboarding_flag = (
            f"\n⚠ ONBOARDING RISK: Added {days_old} days ago. "
            f"Active inboxes: {c.get('active_inboxes',0)}. "
            f"Recent calls: {len(data['recent_calls'])}"
        )

    # Metrics trend (last 7 days)
    metrics = data["metrics"]
    metrics_text = ""
    if metrics:
        latest = metrics[-1] if metrics else {}
        prev   = metrics[-8] if len(metrics) >= 8 else (metrics[0] if metrics else {})
        def chg(a, b, field):
            av = a.get(field); bv = b.get(field)
            if av is None or bv is None or bv == 0: return "N/A"
            return f"{((av-bv)/bv*100):+.1f}%"
        metrics_text = (
            f"  Latest ({latest.get('date','?')}): "
            f"emails={latest.get('emails_sent_total') or latest.get('number_of_emails_sent',0)}, "
            f"reply_rate={latest.get('reply_rate')}%, "
            f"positive_replies={latest.get('positive_replies',0)}, "
            f"bounce={latest.get('bounce_rate')}%, "
            f"live_campaigns={latest.get('live_campaigns',0)}\n"
            f"  vs 7 days ago: reply_rate {chg(latest,prev,'reply_rate')}, "
            f"positive_replies {chg(latest,prev,'positive_replies')}"
        )
    else:
        metrics_text = "  No metrics data"

    # Inbox health (real-time alert source)
    inbox_alerts = []
    for inbox in data["inboxes"]:
        if not inbox.get("is_active"):
            inbox_alerts.append(f"  INACTIVE: {inbox['email_account']}")
        elif (inbox.get("bounce_rate") or 0) > 2:
            inbox_alerts.append(
                f"  BOUNCE SPIKE: {inbox['email_account']} "
                f"bounce={inbox['bounce_rate']}% (threshold: 2%)"
            )
        elif inbox.get("is_warming") and (inbox.get("health_score") or 100) < 90:
            inbox_alerts.append(
                f"  WARMUP HEALTH LOW: {inbox['email_account']} "
                f"health={inbox.get('health_score')}% (threshold: 90%)"
            )
    inbox_text = "\n".join(inbox_alerts) if inbox_alerts else "  All inboxes healthy"

    # Campaign performance (variant comparison)
    campaigns = sorted(
        data["campaigns"],
        key=lambda x: x.get("positive_reply_rate") or 0,
        reverse=True
    )
    camp_text = "\n".join(
        f"  [{c['snapshot_date'][:10]}] {c['campaign_name']} "
        f"[{c.get('segment','')}|{c.get('variant_name','')}]: "
        f"sent={c.get('emails_sent',0)}, rr={c.get('reply_rate',0)}%, "
        f"prr={c.get('positive_reply_rate',0)}%, "
        f"+replies={c.get('positive_replies',0)}, -replies={c.get('negative_replies',0)}"
        for c in campaigns[:10]
    ) or "  None"

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
━━━ {c['name']} | {c.get('tier','?')} | Health:{c.get('health_score','?')} | Inboxes:{c.get('active_inboxes',0)} active/{c.get('disconnected_inboxes',0)} disconnected{onboarding_flag}

METRICS (last 7 days trend):
{metrics_text}

INBOX HEALTH ALERTS:
{inbox_text}

CAMPAIGN PERFORMANCE (last 14 days):
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
  @page {{ size: A4; margin: 0; }}
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
  .footer {{ position: fixed; bottom: 0; left: 0; right: 0; padding: 8px 40px; border-top: 1px solid #e8eaed; display: flex; justify-content: space-between; font-size: 10px; color: #aaa; background: white; }}
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

    payload = {
        "from":    RESEND_FROM,
        "to":      to_emails,
        "cc":      [AMARTYA_EMAIL],
        "reply_to": AMARTYA_EMAIL,
        "template": {"id": "daily-ticket-queue", "variables": {}},
        "attachments": [{"filename": filename,
                         "content":  base64.b64encode(pdf_bytes).decode()}],
    }

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
