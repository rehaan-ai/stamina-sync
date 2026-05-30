#!/usr/bin/env python3
"""
Stamina CS Intelligence — report_generator.py

Generates weekly and monthly reports for all CSM pairs.

Weekly (every Monday):
  - One internal PDF per pair (all accounts, 5-7 pages) → emailed to pair + Amartya CC + Arjun/Rehaan BCC
  - One external PDF per account → uploaded to Pylon

Monthly (1st of month):
  - Same structure, prior calendar month altitude

Usage:
  python3 report_generator.py                # auto-detects weekly vs monthly from date
  python3 report_generator.py --weekly       # force weekly
  python3 report_generator.py --monthly      # force monthly
  python3 report_generator.py --dry-run      # print what would happen, no writes/sends
  python3 report_generator.py --pair "Bala & Raswant"  # run for one pair only (testing)
"""

import base64
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import requests
from openai import OpenAI
from supabase import create_client

# ── Mode detection ────────────────────────────────────────────────────────────

DRY_RUN    = "--dry-run" in sys.argv
FORCE_WEEKLY  = "--weekly"  in sys.argv
FORCE_MONTHLY = "--monthly" in sys.argv
PAIR_FILTER   = next((sys.argv[i+1] for i, a in enumerate(sys.argv) if a == "--pair"), None)

now    = datetime.now(timezone.utc)
is_weekly  = FORCE_WEEKLY  or (not FORCE_MONTHLY and now.weekday() == 0)
is_monthly = FORCE_MONTHLY or (not FORCE_WEEKLY  and now.day == 1)

# ── Credentials ───────────────────────────────────────────────────────────────

SUPABASE_URL   = os.environ.get("SUPABASE_URL", "https://jgvyeavyffenvuhphejg.supabase.co")
SUPABASE_KEY   = os.environ.get("SUPABASE_KEY")
OPENAI_KEY     = os.environ.get("OPENAI_KEY")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
PYLON_KEY      = os.environ.get("PYLON_KEY", "pylon_api_85d658281b647d275a1b1e7dfc081e73de9ebfa9de87d563007eb3ab12251301")

PYLON_BASE    = "https://api.usepylon.com"
RESEND_FROM   = "Stamina <stamina@reports.stamina.io>"
AMARTYA_EMAIL = "amartya@stamina.io"
BCC_EMAILS    = ["arjun@stamina.io", "rehaan@stamina.io"]
TEST_EMAIL    = os.environ.get("TEST_EMAIL")

sb     = create_client(SUPABASE_URL, SUPABASE_KEY)
openai = OpenAI(api_key=OPENAI_KEY)

# ── Logo ──────────────────────────────────────────────────────────────────────

LOGO_PATH = os.path.join(os.path.dirname(__file__), "logo.png")
with open(LOGO_PATH, "rb") as _f:
    LOGO_B64 = base64.b64encode(_f.read()).decode()

# ── Logging ───────────────────────────────────────────────────────────────────

def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def with_retry(fn, retries=3, delay=5, label=""):
    """Call fn() up to `retries` times, waiting `delay` seconds between attempts."""
    for attempt in range(1, retries + 1):
        try:
            return fn()
        except Exception as e:
            if attempt == retries:
                raise
            log(f"  Retry {attempt}/{retries} for {label}: {e} — waiting {delay}s")
            time.sleep(delay)

# ── Date windows ──────────────────────────────────────────────────────────────

def get_weekly_window():
    """Mon–Sun of the prior week."""
    today      = now.date()
    last_monday = today - timedelta(days=today.weekday() + 7)
    last_sunday = last_monday + timedelta(days=6)
    return last_monday, last_sunday

def get_monthly_window():
    """Prior calendar month start and end."""
    first_this_month = now.date().replace(day=1)
    last_month_end   = first_this_month - timedelta(days=1)
    last_month_start = last_month_end.replace(day=1)
    return last_month_start, last_month_end

# ── System prompts ────────────────────────────────────────────────────────────

INTERNAL_WEEKLY_PROMPT = """
You are the Stamina CS Intelligence agent generating an internal weekly report for a CSM pair.

This is a tactical sprint document covering ALL accounts for this pair — 7 to 8 pages.
INTERNAL ONLY. The CSM pair and staff success manager read this before their Monday sprint.
Never share with clients under any circumstances.

## Document structure — same every week, no exceptions

---

### SECTION 1 — Pair Scorecard (This Week vs Prior Week)

| Metric | This week | Prior week | Change |
|---|---|---|---|
| Total accounts | | | |
| Accounts above threshold (all metrics) | | | |
| Accounts with 1+ metric below threshold | | | |
| Accounts with 2+ metrics below threshold (critical) | | | |
| Total positive replies | | | |
| Total unreplied positive leads | | | |
| Open issues across all accounts | | | |
| Upsell signals active | | | |
| Onboarding risks | | | |

One-line overall pair performance summary for the week.

---

### SECTION 2 — Accounts Needing Immediate Attention
Accounts with critical underperformance, onboarding risks, or escalation flags — full detail blocks, listed first.

For each critical account:

**[Account Name]** | Tier: [tier] | Health: [n] | Inboxes: [active] active / [disconnected] disconnected

| Metric | This week | Prior week | Threshold | Status |
|---|---|---|---|---|
| Emails sent | | | | |
| Leads contacted | | | | |
| Reply rate | | | | ✓/✗ |
| Positive replies | | | | ✓/✗ |
| Bounce rate | | | | ✓/✗ |
| Live campaigns | | | | |

What happened this week:
- Meetings: [date] [type] — [1-line summary of what was discussed/decided]
- Slack: [key customer messages — flag requests, complaints, tone shifts, or silence]
- Replies: [named positive replies with company + prospect name; unreplied leads with days elapsed]
- Campaigns: [which variants are running, any notable segment performance this week]
- Open issues: [title, priority, days open]

Why it needs attention: [specific diagnosis — don't soften. Name the exact problem.]

[internal review only]
- Renewal window: [days until renewal if known]
- Upsell signal: [which lever, what data triggered it, forward commitment progress %]
- Customer bandwidth: [responsive vs slow, solo vs team, any concerns]
- Escalation: [should Amartya be looped in? Yes/No + why]
[end internal review only]

---

### SECTION 3 — All Other Accounts
Accounts performing at or above threshold — concise blocks.

**[Account Name]** | Tier: [tier] | Health: [n] | [one-line metric snapshot]
- Performance: [2–3 bullets on key metrics vs prior week]
- This week: [meetings, Slack, replies — 2–3 bullets max]
- Flag if any: [single notable item even if account is healthy]
[internal review only] Upsell: [lever + signal] | Renewal: [window] [end internal review only]

---

### SECTION 4 — Onboarding Risk Accounts
Accounts added 2–7 days ago with 0 active inboxes OR 0 meetings. Flag every week until resolved.

For each:
- Account name | Days since onboarded | What's missing (inboxes / meetings / both)
- Last Slack activity from customer (if any)
- Action: who does what, by when

---

### SECTION 5 — Reply Coaching Summary
For every account with positive replies this week:
- Account | [n] positive replies | [n] unreplied
- Unreplied leads: name, company, days elapsed, recovery move (call now / SMS / last-chance email)
- Coaching flag: if the customer's reply handling is consistently poor, name it for the sprint discussion

---

### SECTION 6 — This Week's Sprint Priorities & Upsell Pipeline

**Sprint priorities (top 3–5, ranked):**
Account | What needs to happen | Owner | Deadline

**Upsell conversations to have this week:**
Account | Lever | Why now (specific data signal) | Suggested opening line for the CSM

**Accounts to loop Amartya into:**
Account | Why | Urgency level

---

## Rules — non-negotiable
- Aggressive, direct tone. Surface problems clearly. Never soften bad news.
- Every account must appear — no skipping. If no data, write "no data this week."
- [internal review only] ... [end internal review only] marks content never shared with clients
- Never name a price
- Unreplied positive leads: name the lead, the account, days elapsed, the exact recovery move
- Onboarding risk: flag every week until resolved — never let it quietly drop off
- Tables for scorecard and per-account metrics. Prose + bullets for commentary.
- 7–8 pages. Be thorough. This is the document the pair uses to run their week.
"""

INTERNAL_MONTHLY_PROMPT = """
You are the Stamina CS Intelligence agent generating an internal monthly report for a CSM pair.

This is a strategic management review covering ALL accounts for this pair — 7 to 8 pages.
INTERNAL ONLY. The CSM pair, staff success manager, and Amartya use this to assess where each
account is in its lifecycle, plan renewal and upsell conversations, and identify systemic issues.
Never share with clients under any circumstances.

## Document structure — same every month, no exceptions

---

### SECTION 1 — Pair Monthly Scorecard

| Metric | This month | Prior month | Change | Trend (3 months) |
|---|---|---|---|---|
| Total accounts | | | | |
| Accounts above threshold (all metrics) | | | | ↑/↓/→ |
| Accounts with 1+ metric below threshold | | | | ↑/↓/→ |
| Accounts with 2+ metrics below threshold | | | | ↑/↓/→ |
| Total positive replies | | | | |
| Total unreplied positive leads | | | | |
| Accounts with active forward commitment | | | | |
| Forward commitments hit / on track / behind | | | | |
| Upsell conversations pending | | | | |
| Accounts within 90 days of renewal | | | | |
| Open issues across all accounts | | | | |

Pair performance score for the month: [n/10 with 1-line rationale]
Key wins this month: [1–2 sentences]
Key risks this month: [1–2 sentences]

---

### SECTION 2 — Account Lifecycle Review
Every account gets a full lifecycle block this month — not just critical ones.
Order: renewal-risk accounts first, then upsell-ready, then healthy, then onboarding.

For EACH account:

**[Account Name]** | Tier: [tier] | Health: [n] | Inboxes: [active]/[total] | Stage: [onboarding / active / renewal-risk / churning]

Month-over-month performance:
| Metric | This month | Prior month | Change | Threshold | Status |
|---|---|---|---|---|---|
| Emails sent | | | | | |
| Reply rate | | | | | ✓/✗ |
| Positive replies | | | | | ✓/✗ |
| Bounce rate | | | | | ✓/✗ |
| Live campaigns | | | | | |

What happened this month:
- Meetings: [list of CS calls + kickoff if applicable, 1-line each]
- Slack: [theme of customer Slack activity — engaged / requesting / quiet / concerned]
- Replies: [total positive, total unreplied, any notable reply patterns]
- Campaigns: [segment and variant performance highlights for the month]
- Issues: [open issues count + any resolved this month]

Account health assessment:
- Performance trend: [improving / stable / declining — with evidence]
- Customer engagement: [responsive / moderate / disengaged — with evidence]
- Risk level: [low / medium / high] and why

[internal review only]
- Renewal: [days until renewal | renewal probability: high/medium/low | narrative — what the renewal conversation looks like, any promo expiry, pricing change implications]
- Forward commitment: [KPI committed | target date | current progress | on track / at risk / hit early | what this means for the upsell timing]
- Upsell readiness: [which lever | what signal triggered it | how strong the signal is | recommended timing for the conversation]
- Customer bandwidth: [solo vs team | response patterns this month | any bandwidth concerns for execution]
- Referral potential: [any signals this customer would refer others]
- Escalation: [does Amartya need to be involved this month? Yes/No + why]
[end internal review only]

---

### SECTION 3 — Onboarding Risk Accounts
Accounts added 2–7 days ago with 0 active inboxes OR 0 meetings. Flag every month until resolved.

---

### SECTION 4 — Forward Commitment Tracker
Every account with an active forward commitment gets a row.

| Account | KPI committed | Target date | Current progress | Status | Upsell conversation timing |
|---|---|---|---|---|---|
| | | | | On track / At risk / Hit / Missed | |

Accounts where the forward commitment has been hit or is close: flag for immediate upsell conversation.
Accounts where the forward commitment is behind: flag for the next sprint — what's blocking it?

---

### SECTION 5 — Renewal Pipeline (Next 90 Days)
Every account renewing within 90 days gets a block.

**[Account Name]** | Renewal in: [n days] | Current tier: [tier]
- Performance vs their measurement contract this month: [on track / behind]
- Commercial context: [promo expiring? price change at renewal? any special terms?]
- Renewal probability: [high / medium / low]
- Recommended approach: [specific renewal narrative for the CSM]
- Upsell opportunity at renewal: [lever + why this renewal is the right moment]

---

### SECTION 6 — Monthly Priorities & Strategic Actions

**Upsell conversations to have this month (ranked by urgency):**
Account | Lever | Signal strength | Why this month specifically | Suggested opening

**Accounts to escalate to Amartya:**
Account | Why | Recommended action

**Systemic issues across the book:**
If 3+ accounts share the same problem (e.g., reply rate dropping across multiple accounts,
inbox health degrading across the book), flag it here as a systemic issue rather than per-account noise.

**Pair development note (optional):**
One observation about the CSM pair's performance this month — what they did well, what to improve.

---

## Rules — non-negotiable
- Aggressive, direct tone. Surface problems and risks clearly. Never soften.
- Every account must appear in Section 2 — no exceptions, no skipping.
- [internal review only] ... [end internal review only] never goes to clients
- Never name a price
- Renewal pipeline must be accurate — if renewal is within 90 days, it must appear in Section 5
- Forward commitment tracking must be complete — every active commitment gets a row in Section 4
- Tables for scorecard, per-account metrics, forward commitment, renewal pipeline
- 7–8 pages. Be strategic and thorough. This is the monthly management review.
"""

EXTERNAL_WEEKLY_PROMPT = """
You are the Stamina CS Intelligence agent generating an external weekly report for one client.

This report is shared directly with the client. Professional, collaborative tone.
Six fixed sections, same structure every week. Maximum 2 pages when rendered.
Lead with the headline metric from the client's measurement contract (most important to them).

## Six sections — same every week, no exceptions

### 1. Performance Metrics
Stamina-controlled metrics only. Never include meetings booked, pipeline, or revenue (client-owned outcomes).
Use a table. Render the client's primary measurement contract metric first and emphasise it.

| Metric | This week | Prior week | Change | Threshold | Status |
|---|---|---|---|---|---|
| Emails sent | | | | | |
| Leads contacted | | | | | |
| Reply rate | | | | | ✓/✗ |
| Positive replies | | | | | ✓/✗ |
| Bounce rate | | | | | ✓/✗ |
| Live campaigns | | | | | |

Note: Lead with the metric from the client's measurement contract. Flag below the table if any metric has been below threshold for 2+ consecutive weeks.

### 2. Audience Visibility
- Unique contacts emailed this week, broken down by campaign segment
- Reply sentiment breakdown: positive / neutral / negative / out-of-office / unsubscribe (counts)
- Named list of positive-reply companies: [Company] — [Prospect Name, Title] — [one-line reply summary]
- Any anomalies the client should know about (bounce spike, deliverability issue, segment with 0 replies, inbox disconnect)

### 3. What's Working — Performance Insights
Specific and actionable only. "Variant B drove a 3.2% positive reply rate vs Variant A's 1.1% — recommend killing A" is good.
"Engagement is up" is not acceptable.
- Which campaign variants drove the highest positive reply rates (name the variant, name the %s)
- Which campaign segments outperformed others and by how much (name the segments)
- Which campaigns have the best reply-to-bounce ratio (most efficient sending)
- Reply velocity insight: did faster customer responses convert better? (use response delay data)
- One concrete test to run next week based on this week's data

### 4. Business Intelligence — What the Data Means Beyond Outbound
Four callout blocks. This section separates Stamina from any reporting platform — cold email is the fastest
market signal available. Surface what this data reveals about the client's broader GTM.

**4.1 Positioning Signal**
What A/B subject line and message performance reveals about which value propositions actually land with
the client's audience. Reference the specific winning and losing language. Recommend a concrete test the
client can run on their homepage or ad copy based on this week's outbound winners.

**4.2 Where the Real ICP Is**
What segment performance differential reveals about the client's true best-fit audience vs what they
assumed at kickoff. Surface patterns that transcend industry when the data supports it (e.g., "ops-led
organizations regardless of vertical respond at 2× the rate of marketing-led ones").

**4.3 Objection Analysis**
Cluster negative replies and "not now" replies into 2–3 patterns. For each pattern:
- Name what the objection actually is
- Name what it reveals about a positioning gap the client should address in their broader GTM
- One concrete fix (messaging change, homepage update, sales talk track)

**4.4 Cross-Channel Transfer**
Concrete actions the client should take in slower channels based on this week's outbound learnings.
Framing: outbound moves at the speed of weekly tests — transfer winners to slower channels before
competitors catch up.

| Channel | Insight from outbound | Recommended action |
|---|---|---|
| Homepage | | |
| Ads | | |
| Case studies | | |
| Sales call opening | | |

### 5. Reply Coaching
Review every positive reply this week and the client's Unibox response (if any).

**Volume rule:**
- ≤5 positive replies → one coaching block per reply
- ≥6 positive replies → pattern-level coaching (2–3 patterns, named examples from the week)
Never mix both modes.

**Each coaching block must contain all five of:**
1. The prospect's reply (verbatim or close summary)
2. The client's response (verbatim or close summary) — if unreplied, state it and days elapsed
3. What worked — specific elements that increase conversion probability
4. What to change — aggressive, specific. Always answer: what should the client do RIGHT NOW with this lead?
5. What this prospect's reply suggests about their state (e.g., "price-shopping — lead with ROI not features")

**Coaching standards applied to every block:**
- Response speed: positive replies should get a response within 2 hours (business hours) or 4 hours (outside).
  Flag anything beyond 24h explicitly — this is a strong negative signal, name it directly.
- Conversation-first: replies that send links, PDFs, or long descriptions instead of proposing a meeting time
  are failure modes. The first reply should propose a specific next step — not deliver content.
- Multi-channel follow-up: when a prospect goes quiet after a positive reply, push the client to layer
  SMS, phone, and LinkedIn touches — not just a follow-up email.
- Qualifying questions early: "yes let's chat" replies without qualifying waste the meeting slot.
  Coach the client to qualify before booking: "What are you trying to solve right now?"
- Tone match: short direct prospect → short direct reply. Warm conversational → match it.

**Unreplied positive leads — flag separately and aggressively:**
Name the lead, the company, days elapsed since their reply, and the specific recovery move recommended
(call now / SMS / last-chance email). Stamina generated the opportunity — the client's job is to catch it.

### 6. Next Week — Priorities and Anomalies
- Top 1–2 priorities for next week (driven by section 3 insights)
- Any anomalies needing client attention this week
- One decision question for the client — only include if there is a real decision to surface
  (e.g., "Variant B is winning by 8 points — should we kill A and double send volume on B?")
  Do not force a question if there is no real decision.

This section keeps the report active. Without it, reports become routine and clients stop reading.

## Non-negotiable rules
- Never include internal commentary, renewal context, upsell signals, or pricing
- Never name a price
- Stamina-controlled metrics only in section 1 — never meetings, pipeline, MRR
- Insights must be specific: name the variant, name the %, name the segment
- Tables for section 1 and 4.4. Prose + bullets for everything else.
- Maximum 2 pages when rendered
"""

EXTERNAL_MONTHLY_PROMPT = """
You are the Stamina CS Intelligence agent generating an external monthly report for one client.

This report is shared directly with the client. Same six-section structure as the weekly report,
but operating at month altitude. Maximum 2 pages when rendered. Professional, collaborative tone.
Note any partial months explicitly (e.g., "May reflects 18 sending days from May 12 launch").

## Six sections — same structure as weekly, month-level shifts noted below

### 1. Performance Metrics
Same table format as weekly. Month-over-month comparison instead of week-over-week.
Note partial months explicitly.

| Metric | This month | Prior month | Change | Threshold | Status |
|---|---|---|---|---|---|
| Emails sent | | | | | |
| Leads contacted | | | | | |
| Reply rate | | | | | ✓/✗ |
| Positive replies | | | | | ✓/✗ |
| Bounce rate | | | | | ✓/✗ |
| Live campaigns | | | | | |

Note partial months explicitly. Lead with the client's primary measurement contract metric.

### 2. Audience Visibility
Monthly totals by segment plus cumulative figures since launch.
Sentiment breakdown across the full month. Named positive-reply companies for the month.

### 3. What's Working — Month-Level Insights
Variant performance across ALL campaigns tested this month (more data = stronger conclusions).
Which segments consistently outperformed others across the month — name them with %s.
Reply velocity correlation: did faster customer responses convert better? Show the pattern using response delay data.
Campaign efficiency: which campaigns generated the best ratio of positive replies to emails sent.

### 4. Business Intelligence — Month-Level Conclusions
A month of signal is enough to draw real conclusions — not just signals, but theses.

**4.1 Positioning Thesis**
With a full month of A/B variant data, name the winning positioning pattern — not just which variant
won, but what it reveals about how the client's audience thinks about their problem. Recommend specific
homepage hero copy rewrites based on the winning language patterns.

**4.2 Real ICP Read**
Segment performance differential at month scale is statistically meaningful. Surface patterns that
transcend the client's kickoff ICP assumption. If "comms-led roles regardless of industry respond at
2× rate", say so and recommend the client expand targeting accordingly.

**4.3 Objection Analysis**
Cluster ALL negative and "not now" replies from the month into 3–4 patterns with counts.
For each: name the pattern, name what it reveals about a GTM positioning gap, name one concrete fix.

**4.4 Cross-Channel Transfer**
Concrete actions for next month across slower channels based on this month's outbound learnings.
Use structured table format.

| Channel | This month's outbound learning | Recommended action for next month |
|---|---|---|
| Homepage | | |
| Ads | | |
| Case studies | | |
| Sales call opening | | |
| Vertical expansion | | |

### 5. Reply Coaching — Pattern Level
Always pattern-level for monthlies (volume is always ≥6 over a month).
3–4 named patterns with 2–3 examples drawn from the month's positive replies.
Must include reply velocity correlation as one of the patterns — show what response speed did to conversion.
Same five-part block structure as weekly coaching blocks.

### 6. Forward Commitment Progress Check
(Replaces "Next week" in monthly reports)

**Forward commitment status:**
- KPI committed at kickoff: [metric and target]
- Target date: [date]
- Current progress: [where they are now vs target]
- Status: On track / At risk / Hit early / Behind
- What this means for upsell conversation timing: [specific recommendation]

**Looking ahead — next month and renewal:**
Map the next 4 weeks to renewal if renewal is within 90 days. What needs to happen by when.
If renewal is not imminent, name the one thing that would most improve their results next month.

## Non-negotiable rules
- Same rules as weekly — no internal content, no pricing, Stamina-controlled metrics only
- Tables for metrics (section 1) and cross-channel transfer (4.4)
- Maximum 2 pages when rendered
- Note partial months explicitly in section 1
"""

# ── Data gathering ────────────────────────────────────────────────────────────

def get_accounts_for_pair(pair: dict) -> list:
    ft = pair["filter_type"]
    fv = pair["filter_value"]

    q = sb.table("customers").select(
        "id, name, domain, tier, pylon_account_id, brand_id, csm_owner, account_owner, "
        "health_score, active_inboxes, disconnected_inboxes, created_at, tags"
    ).eq("status", "active")  # Active accounts only

    if ft == "csm_owner":
        q = q.eq("csm_owner", fv)
    elif ft == "account_owner":
        q = q.eq("account_owner", fv)
    elif ft == "tag":
        q = q.contains("tags", [fv])

    return q.execute().data


def gather_account_data(customer: dict, start_date: str, end_date: str) -> dict:
    cid      = customer["id"]
    brand_id = customer.get("brand_id")

    # Metrics this period
    metrics = (
        sb.table("account_metrics_daily")
        .select("date, emails_sent_total, number_of_emails_sent, reply_rate, positive_replies, bounce_rate, live_campaigns, total_leads_contacted, campaign_progress")
        .eq("customer_id", cid)
        .gte("date", start_date)
        .lte("date", end_date)
        .order("date")
        .execute()
        .data
    )

    # Prior period metrics (same length, shifted back)
    start_dt      = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt        = datetime.strptime(end_date, "%Y-%m-%d")
    period_days   = (end_dt - start_dt).days + 1
    prior_start   = (start_dt - timedelta(days=period_days)).strftime("%Y-%m-%d")
    prior_end     = (start_dt - timedelta(days=1)).strftime("%Y-%m-%d")

    prior_metrics = (
        sb.table("account_metrics_daily")
        .select("date, emails_sent_total, number_of_emails_sent, reply_rate, positive_replies, bounce_rate, live_campaigns, total_leads_contacted, campaign_progress")
        .eq("customer_id", cid)
        .gte("date", prior_start)
        .lte("date", prior_end)
        .order("date")
        .execute()
        .data
    )

    # Meetings this period
    meetings = (
        sb.table("meetings")
        .select("title, meeting_date, meeting_type, summary_text")
        .eq("customer_id", cid)
        .gte("meeting_date", start_date)
        .lte("meeting_date", end_date + "T23:59:59")
        .order("meeting_date")
        .execute()
        .data
    )

    # Slack messages this period
    slack = (
        sb.table("slack_messages")
        .select("user_name, is_internal, text, message_date")
        .eq("customer_id", cid)
        .gte("message_date", start_date)
        .lte("message_date", end_date + "T23:59:59")
        .order("message_date")
        .limit(50)
        .execute()
        .data
    )

    # Reply data (via brand_id) — individual replies with coaching context
    reply_data = []
    if brand_id:
        reply_data = (
            sb.table("reply_data")
            .select("prospect_email, prospect_first_name, prospect_last_name, prospect_company, "
                    "reply_body, reply_label, replied_at, campaign_name, "
                    "customer_responded, customer_response_text, customer_response_delay_hrs")
            .eq("brand_id", brand_id)
            .gte("replied_at", start_date)
            .lte("replied_at", end_date + "T23:59:59")
            .order("replied_at", desc=True)
            .limit(50)
            .execute()
            .data
        )

    # Email inboxes latest snapshot
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

    # Campaign stats
    campaigns = []
    if brand_id:
        campaigns = (
            sb.table("campaign_stats")
            .select("campaign_name, segment, variant_name, emails_sent, replies, positive_replies, negative_replies, reply_rate, positive_reply_rate, bounce_rate, campaign_progress, snapshot_date")
            .eq("brand_id", brand_id)
            .gte("snapshot_date", start_date)
            .lte("snapshot_date", end_date)
            .execute()
            .data
        )

    # Open issues
    issues = (
        sb.table("issues")
        .select("title, status, priority, created_at")
        .eq("customer_id", cid)
        .in_("status", ["open", "in_progress"])
        .limit(10)
        .execute()
        .data
    )

    # Kickoff document for measurement contract context
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
            kickoff_context += f"\n[Pass {k['pass_number']}]\n{k['content_md'][:1500]}\n"

    # Onboarding risk flag (added 2–7 days ago, 0 active inboxes or 0 meetings)
    created_at  = customer.get("created_at", "")
    onboarding_risk = False
    if created_at:
        days_old = (now.date() - datetime.fromisoformat(created_at.replace("Z", "+00:00")).date()).days
        if 2 <= days_old <= 7:
            if customer.get("active_inboxes", 0) == 0 or len(meetings) == 0:
                onboarding_risk = True

    return {
        "customer":        customer,
        "metrics":         metrics,
        "prior_metrics":   prior_metrics,
        "meetings":        meetings,
        "slack":           slack,
        "reply_data":      reply_data,
        "inboxes":         inboxes,
        "campaigns":       campaigns,
        "issues":          issues,
        "kickoff_context": kickoff_context,
        "onboarding_risk": onboarding_risk,
    }


def _metrics_block(metrics: list, prior: list, period: str) -> str:
    """Compute and format the core metrics block for an account."""
    def avg(rows, field):
        vals = [r[field] for r in rows if r.get(field) is not None]
        return round(sum(vals) / len(vals), 2) if vals else None

    def total(rows, field):
        vals = [r[field] for r in rows if r.get(field) is not None]
        return sum(vals) if vals else 0

    def delta(cur, prev):
        if cur is None or prev is None or prev == 0:
            return "N/A"
        return f"{((cur - prev) / prev * 100):+.1f}%"

    cur_sent  = total(metrics, "emails_sent_total") or total(metrics, "number_of_emails_sent")
    cur_rr    = avg(metrics, "reply_rate")
    cur_pr    = total(metrics, "positive_replies")
    cur_br    = avg(metrics, "bounce_rate")
    cur_leads = total(metrics, "total_leads_contacted")
    cur_camps = avg(metrics, "live_campaigns")

    prev_sent  = total(prior, "emails_sent_total") or total(prior, "number_of_emails_sent")
    prev_rr    = avg(prior, "reply_rate")
    prev_pr    = total(prior, "positive_replies")
    prev_leads = total(prior, "total_leads_contacted")

    return (
        f"  Emails sent:       {cur_sent} vs {prev_sent} ({delta(cur_sent, prev_sent)})\n"
        f"  Leads contacted:   {cur_leads} vs {prev_leads} ({delta(cur_leads, prev_leads)})\n"
        f"  Reply rate:        {cur_rr}% vs {prev_rr}% ({delta(cur_rr, prev_rr)})\n"
        f"  Positive replies:  {cur_pr} vs {prev_pr} ({delta(cur_pr, prev_pr)})\n"
        f"  Bounce rate:       {cur_br}%\n"
        f"  Live campaigns:    {cur_camps}"
    )


def format_account_for_internal(data: dict, period: str) -> str:
    """
    Compact account block for the internal report (multiple accounts in one GPT call).
    Budget: ~2000 chars per account to keep total prompt within GPT-4o context.
    CSMs lived through these meetings — they need highlights and flags, not full transcripts.
    """
    c        = data["customer"]
    meetings = data["meetings"]
    slack    = data["slack"]
    issues   = data["issues"]
    replies  = data["reply_data"]

    onboarding_flag = " ⚠ ONBOARDING RISK" if data["onboarding_risk"] else ""

    # Metrics
    metrics_text = _metrics_block(data["metrics"], data["prior_metrics"], period)

    # Inboxes summary
    inboxes = data["inboxes"]
    inbox_issues = [i for i in inboxes if not i.get("is_active") or (i.get("bounce_rate") or 0) > 2]
    inbox_summary = f"{c.get('active_inboxes',0)} active / {c.get('disconnected_inboxes',0)} disconnected"
    if inbox_issues:
        inbox_summary += " | INBOX ISSUES: " + ", ".join(
            f"{i['email_account']} (active={i.get('is_active')}, bounce={i.get('bounce_rate')}%)"
            for i in inbox_issues[:3]
        )

    # Meetings (200 chars summary each, max 3)
    meetings_text = "\n".join(
        f"  {m['meeting_date'][:10]} [{m['meeting_type']}] {m['title']}: "
        f"{(m.get('summary_text') or '')[:200]}"
        for m in meetings[:3]
    ) or "  None"

    # Slack (8 messages, 120 chars each — customer messages only for internal)
    slack_text = "\n".join(
        f"  {s['message_date'][:10]} ({'CSM' if s['is_internal'] else 'customer'}): {s['text'][:120]}"
        for s in slack[-8:]
    ) or "  No messages"

    # Reply summary — counts + named positive + unreplied flags
    label_counts = {}
    pos_names, unreplied = [], []
    for r in replies:
        lbl = r.get("reply_label", "unknown")
        label_counts[lbl] = label_counts.get(lbl, 0) + 1
        if lbl in ("positive", "interested"):
            name = f"{r.get('prospect_first_name','')} {r.get('prospect_last_name','')}".strip()
            co   = r.get("prospect_company", "")
            pos_names.append(f"{name} ({co})")
            if not r.get("customer_responded"):
                hrs = r.get("customer_response_delay_hrs") or 999
                unreplied.append(f"{name} ({co}) — {round(hrs/24)}d unreplied")

    reply_text = " | ".join(f"{lbl}: {cnt}" for lbl, cnt in label_counts.items()) or "none"
    if pos_names:
        reply_text += "\n  Positive: " + ", ".join(pos_names[:8])
    if unreplied:
        reply_text += "\n  ⚠ UNREPLIED: " + " | ".join(unreplied)

    # Campaigns top 5 by positive reply rate
    campaigns = sorted(data["campaigns"], key=lambda x: x.get("positive_reply_rate") or 0, reverse=True)
    camp_text = "\n".join(
        f"  {camp['campaign_name']} [{camp.get('segment','')}|{camp.get('variant_name','')}]: "
        f"sent={camp.get('emails_sent',0)}, rr={camp.get('reply_rate',0)}%, "
        f"+replies={camp.get('positive_replies',0)}, -replies={camp.get('negative_replies',0)}"
        for camp in campaigns[:5]
    ) or "  None"

    issues_text = " | ".join(
        f"[{i['priority']}] {i['title']}" for i in issues
    ) or "None"

    kickoff = f"\nMeasurement contract: {data['kickoff_context'][:600]}" if data["kickoff_context"] else ""

    return f"""
━━━ {c['name'].upper()} | {c.get('tier','?')} | Health:{c.get('health_score','?')} | {inbox_summary}{onboarding_flag}

METRICS ({period}):
{metrics_text}

MEETINGS: {meetings_text}

SLACK: {slack_text}

REPLIES: {reply_text}

CAMPAIGNS:
{camp_text}

ISSUES: {issues_text}{kickoff}
"""


def format_account_for_external(data: dict, period: str) -> str:
    """
    Full account data block for external report (one account per GPT call).
    Passes all available data — full reply bodies, full meeting summaries, all inboxes, all campaigns.
    """
    c        = data["customer"]
    meetings = data["meetings"]
    slack    = data["slack"]
    issues   = data["issues"]
    replies  = data["reply_data"]
    inboxes  = data["inboxes"]

    # Metrics
    metrics_text = _metrics_block(data["metrics"], data["prior_metrics"], period)

    # All inboxes with full health data
    inbox_text = "\n".join(
        f"  {i['email_account']}: active={i.get('is_active')}, warming={i.get('is_warming')}, "
        f"health={i.get('health_score')}, bounce={i.get('bounce_rate')}%"
        for i in inboxes
    ) or "  No inbox data"

    # All campaigns with full variant detail
    campaigns = sorted(data["campaigns"], key=lambda x: x.get("positive_reply_rate") or 0, reverse=True)
    camp_text = "\n".join(
        f"  [{camp.get('snapshot_date','')[:10]}] {camp['campaign_name']} "
        f"[segment: {camp.get('segment','')} | variant: {camp.get('variant_name','')}]\n"
        f"    sent={camp.get('emails_sent',0)}, reply_rate={camp.get('reply_rate',0)}%, "
        f"positive_reply_rate={camp.get('positive_reply_rate',0)}%, "
        f"positive={camp.get('positive_replies',0)}, negative={camp.get('negative_replies',0)}, "
        f"bounce={camp.get('bounce_rate',0)}%, progress={camp.get('campaign_progress','')}"
        for camp in campaigns
    ) or "  No campaign data"

    # Full meeting summaries
    meetings_text = "\n\n".join(
        f"  [{m['meeting_date'][:10]}] [{m['meeting_type']}] {m['title']}\n"
        f"  {(m.get('summary_text') or 'No summary')[:3000]}"
        for m in meetings
    ) or "  None this period"

    # Slack — 20 messages at 300 chars each
    slack_text = "\n".join(
        f"  {s['message_date'][:16]} ({'CSM/internal' if s['is_internal'] else 'CUSTOMER'}): "
        f"{s['text'][:300]}"
        for s in slack[-20:]
    ) or "  No messages"

    # Full reply coaching data
    # Separate positive (need coaching) from negative/neutral (need objection analysis)
    positive_replies = [r for r in replies if r.get("reply_label") in ("positive", "interested")]
    other_replies    = [r for r in replies if r.get("reply_label") not in ("positive", "interested")]

    label_counts = {}
    for r in replies:
        lbl = r.get("reply_label", "unknown")
        label_counts[lbl] = label_counts.get(lbl, 0) + 1

    reply_counts = " | ".join(f"{lbl}: {cnt}" for lbl, cnt in label_counts.items()) or "none"

    # Positive replies with full coaching context
    pos_text = ""
    for r in positive_replies:
        name = f"{r.get('prospect_first_name','')} {r.get('prospect_last_name','')}".strip()
        co   = r.get("prospect_company", "unknown company")
        replied_at = (r.get("replied_at") or "")[:16]
        delay_hrs  = r.get("customer_response_delay_hrs")
        responded  = r.get("customer_responded", False)
        cust_resp  = r.get("customer_response_text") or ""
        campaign   = r.get("campaign_name", "")

        delay_str = (
            f"{round(delay_hrs)}hrs to respond" if delay_hrs and responded
            else "⚠ NO RESPONSE FROM CUSTOMER YET"
        )

        pos_text += (
            f"\n  [{replied_at}] {name} | {co} | Campaign: {campaign}\n"
            f"  PROSPECT REPLIED: \"{r.get('reply_body', '')}\"\n"
            f"  CUSTOMER RESPONSE ({delay_str}): \"{cust_resp if cust_resp else 'No response sent yet'}\"\n"
        )

    # Negative/neutral replies for objection analysis (reply body only)
    neg_text = "\n".join(
        f"  [{(r.get('replied_at') or '')[:10]}] [{r.get('reply_label','')}] "
        f"{r.get('prospect_first_name','')} {r.get('prospect_last_name','')} "
        f"({r.get('prospect_company','')}): \"{(r.get('reply_body') or '')[:300]}\""
        for r in other_replies[:20]
    ) or "  None"

    # Issues
    issues_text = "\n".join(
        f"  [{i['priority']}] {i['title']} — {i['status']} (opened {i.get('created_at','')[:10]})"
        for i in issues
    ) or "  None"

    # Full kickoff context
    kickoff = f"\n{data['kickoff_context'][:2500]}" if data["kickoff_context"] else "\n  No kickoff document available."

    return f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ACCOUNT: {c['name']}
Tier: {c.get('tier','?')} | Health score: {c.get('health_score','?')} | Tags: {c.get('tags',[])}
{"⚠ ONBOARDING RISK: Added recently with 0 inboxes or 0 meetings" if data['onboarding_risk'] else ""}

=== PERFORMANCE METRICS (this {period} vs prior {period}) ===
{metrics_text}

=== EMAIL INBOXES ===
{inbox_text}

=== CAMPAIGNS & VARIANTS ===
{camp_text}

=== MEETINGS ===
{meetings_text}

=== SLACK MESSAGES (last 20) ===
{slack_text}

=== REPLY SUMMARY ===
Counts: {reply_counts}

POSITIVE REPLIES — full coaching context:
(prospect reply = what the prospect wrote; customer response = what our customer wrote back)
{pos_text if pos_text else "  None this period"}

NEGATIVE / NEUTRAL REPLIES — for objection analysis:
{neg_text}

=== OPEN ISSUES ===
{issues_text}

=== KICKOFF CONTEXT (measurement contract, forward commitment, expansion paths) ===
{kickoff}
"""


# ── GPT-4o generation ─────────────────────────────────────────────────────────

def generate_internal_report(pair: dict, accounts_data: list, period: str,
                              start_date: str, end_date: str) -> str:
    period_label = "week" if period == "weekly" else "month"
    system = INTERNAL_WEEKLY_PROMPT if period == "weekly" else INTERNAL_MONTHLY_PROMPT

    accounts_block = "\n".join(
        format_account_for_internal(d, period_label) for d in accounts_data
    )

    report_type = "weekly sprint document" if period == "weekly" else "monthly strategic review"
    user = f"""Generate the internal {period_label} report ({report_type}) for {pair['pair_name']}.

Period: {start_date} to {end_date}
Pair: {pair['pair_name']} ({', '.join(pair['csm_emails'])})
Total accounts: {len(accounts_data)}

{accounts_block}
"""

    response = openai.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
        temperature=0.3,
        max_tokens=6000,
    )
    return response.choices[0].message.content


def generate_external_report(account_data: dict, period: str,
                              start_date: str, end_date: str) -> str:
    period_label = "week" if period == "weekly" else "month"
    system = EXTERNAL_WEEKLY_PROMPT if period == "weekly" else EXTERNAL_MONTHLY_PROMPT

    account_block = format_account_for_external(account_data, period_label)
    name          = account_data["customer"]["name"]

    user = f"""Generate the external {period_label} report for {name}.

Period: {start_date} to {end_date}

{account_block}
"""

    response = openai.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
        temperature=0.3,
        max_tokens=3000,
    )
    return response.choices[0].message.content


# ── PDF generation ────────────────────────────────────────────────────────────

def md_to_html_body(md: str) -> str:
    lines = md.split("\n")
    html_lines = []
    for line in lines:
        if line.startswith("# "):
            html_lines.append(f'<h1>{line[2:]}</h1>')
        elif line.startswith("## "):
            html_lines.append(f'<h2>{line[3:]}</h2>')
        elif line.startswith("### "):
            html_lines.append(f'<h3>{line[4:]}</h3>')
        elif line.startswith("- "):
            html_lines.append(f'<div class="bullet">• {line[2:]}</div>')
        elif line.startswith("━") or line.startswith("---"):
            html_lines.append('<hr class="divider">')
        elif line.strip() == "":
            html_lines.append('<div class="spacer"></div>')
        else:
            line = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", line)
            line = re.sub(r"\[internal review only\]", r'<span class="internal-tag">[internal review only]</span>', line)
            html_lines.append(f'<p>{line}</p>')
    return "\n".join(html_lines)


def generate_pdf(content_md: str, title: str, subtitle: str, is_internal: bool) -> bytes:
    from weasyprint import HTML as WP_HTML

    body_html = md_to_html_body(content_md)
    today     = datetime.now().strftime("%B %d, %Y")
    banner    = '<div class="internal-banner">⚠ INTERNAL ONLY — DO NOT SHARE WITH CLIENTS</div>' if is_internal else ""

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  @page {{ size: A4; margin: 0; }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: "Helvetica Neue", Arial, sans-serif; background: white; color: #1a1a1a; font-size: 12px; line-height: 1.6; }}
  .header {{ background: #1a2035; padding: 28px 48px; display: flex; align-items: center; justify-content: space-between; }}
  .header img {{ height: 24px; filter: brightness(0) invert(1); }}
  .header-right {{ text-align: right; }}
  .header-right .label {{ color: #8892a4; font-size: 10px; letter-spacing: 1.5px; text-transform: uppercase; }}
  .header-right .title {{ color: white; font-size: 15px; font-weight: 700; margin-top: 4px; }}
  .header-right .sub {{ color: #8892a4; font-size: 11px; margin-top: 2px; }}
  .internal-banner {{ background: #b91c1c; color: white; text-align: center; font-size: 10px; font-weight: 700; letter-spacing: 2px; text-transform: uppercase; padding: 5px; }}
  .body {{ padding: 32px 48px 72px; }}
  h1 {{ font-size: 16px; font-weight: 700; color: #1a2035; margin: 20px 0 8px; border-bottom: 2px solid #1a2035; padding-bottom: 5px; }}
  h2 {{ font-size: 14px; font-weight: 700; color: #1a2035; margin: 16px 0 6px; }}
  h3 {{ font-size: 12px; font-weight: 700; color: #444; margin: 12px 0 4px; text-transform: uppercase; letter-spacing: 0.5px; }}
  p {{ margin-bottom: 6px; color: #333; font-size: 12px; }}
  .bullet {{ margin: 2px 0 2px 14px; color: #333; font-size: 12px; }}
  .spacer {{ height: 6px; }}
  hr.divider {{ border: none; border-top: 1px solid #e8eaed; margin: 12px 0; }}
  .internal-tag {{ color: #dc2626; font-weight: 700; font-size: 10px; }}
  strong {{ color: #1a1a1a; }}
  .footer {{ position: fixed; bottom: 0; left: 0; right: 0; padding: 8px 48px; border-top: 1px solid #e8eaed; display: flex; justify-content: space-between; font-size: 10px; color: #aaa; background: white; }}
</style>
</head>
<body>
<div class="header">
  <img src="data:image/png;base64,{LOGO_B64}">
  <div class="header-right">
    <div class="label">{'Internal' if is_internal else 'Client'} Report</div>
    <div class="title">{title}</div>
    <div class="sub">{subtitle}</div>
  </div>
</div>
{banner}
<div class="body">{body_html}</div>
<div class="footer">
  <span>Stamina CS Intelligence · {today}</span>
  <span>{'INTERNAL' if is_internal else 'CONFIDENTIAL'} — {title}</span>
</div>
</body>
</html>"""

    return WP_HTML(string=html).write_pdf()


# ── Pylon upload ──────────────────────────────────────────────────────────────

def upload_to_pylon(pdf_bytes: bytes, filename: str, pylon_account_id: str) -> str:
    resp = requests.post(
        f"{PYLON_BASE}/attachments",
        headers={"Authorization": f"Bearer {PYLON_KEY}"},
        files={"file": (filename, pdf_bytes, "application/pdf")},
        data={"account_id": pylon_account_id},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["data"]["url"]


# ── Email sending ─────────────────────────────────────────────────────────────

def send_internal_email(pair: dict, pdf_bytes: bytes, period: str,
                         label: str, filename: str):
    to_emails = pair.get("report_email") or pair.get("csm_emails") or []
    if not to_emails:
        log(f"  No emails for {pair['pair_name']} — skipping")
        return

    template_alias = "weekly-internal-report" if period == "weekly" else "monthly-internal-report"

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
        "template": {"id": template_alias, "variables": {}},
        "attachments": [{"filename": filename, "content": base64.b64encode(pdf_bytes).decode()}],
    }
    if cc_list:
        payload["cc"] = cc_list
    if bcc_list:
        payload["bcc"] = bcc_list

    resp = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    log(f"  Email sent to {to_emails} (ID: {resp.json().get('id')})")


# ── Main report run ───────────────────────────────────────────────────────────

def run_reports(period: str):
    if period == "weekly":
        start_date, end_date = get_weekly_window()
        label = f"Week of {start_date.strftime('%B %d, %Y')}"
    else:
        start_date, end_date = get_monthly_window()
        label = start_date.strftime("%B %Y")

    start_str = start_date.isoformat()
    end_str   = end_date.isoformat()

    log(f"Running {period} reports for {label}...")

    pairs = sb.table("csm_pairs").select("*").eq("is_active", True).execute().data
    if PAIR_FILTER:
        pairs = [p for p in pairs if p["pair_name"] == PAIR_FILTER]

    for pair in pairs:
        pair_name = pair["pair_name"]
        log(f"\nPair: {pair_name}")

        accounts = get_accounts_for_pair(pair)
        if not accounts:
            log(f"  No accounts found — skipping")
            continue
        log(f"  {len(accounts)} accounts")

        # Gather data for all accounts
        accounts_data = []
        for account in accounts:
            try:
                data = gather_account_data(account, start_str, end_str)
                accounts_data.append(data)
            except Exception as e:
                log(f"  Data error for {account.get('name')}: {e}")

        if not accounts_data:
            continue

        # ── Internal report (one per pair) ────────────────────────────────────
        log(f"  Generating internal {period} report...")
        if not DRY_RUN:
            try:
                internal_md  = with_retry(
                    lambda: generate_internal_report(pair, accounts_data, period, start_str, end_str),
                    retries=3, delay=10, label=f"internal report {pair_name}"
                )
                internal_pdf = generate_pdf(
                    internal_md,
                    title    = f"{pair_name} — All Accounts",
                    subtitle = label,
                    is_internal = True,
                )
                filename = f"{pair_name.replace(' ', '_')}_{period}_{start_str}_internal.pdf"
                send_internal_email(pair, internal_pdf, period, label, filename)
                log(f"  ✓ Internal report sent")
            except Exception as e:
                log(f"  ERROR internal report: {e}")
        else:
            log(f"  [DRY RUN] Would generate internal report for {len(accounts_data)} accounts")

        # ── External reports (one per account, parallelised) ──────────────────
        log(f"  Generating {len(accounts_data)} external reports...")

        def process_external(data: dict):
            name      = data["customer"]["name"]
            pylon_id  = data["customer"].get("pylon_account_id")
            today_str = datetime.now().strftime("%B %d, %Y")
            filename  = f"{name} Report — {today_str}.pdf"

            if DRY_RUN:
                return name, "dry-run"

            try:
                external_md  = with_retry(
                    lambda: generate_external_report(data, period, start_str, end_str),
                    retries=3, delay=8, label=f"external report {name}"
                )
                external_pdf = generate_pdf(
                    external_md,
                    title    = name,
                    subtitle = label,
                    is_internal = False,
                )
                if pylon_id:
                    url = with_retry(
                        lambda: upload_to_pylon(external_pdf, filename, pylon_id),
                        retries=3, delay=5, label=f"Pylon upload {name}"
                    )
                    return name, url
                else:
                    return name, "no-pylon-id"
            except Exception as e:
                return name, f"ERROR: {e}"

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(process_external, d): d for d in accounts_data}
            for future in as_completed(futures):
                name, result = future.result()
                if "ERROR" in str(result):
                    log(f"    ✗ {name}: {result}")
                else:
                    log(f"    ✓ {name}: uploaded" if result != "dry-run" else f"    [DRY RUN] {name}")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    log(f"Report generator started {'[DRY RUN] ' if DRY_RUN else ''}...")
    log(f"  is_weekly={is_weekly}, is_monthly={is_monthly}")

    if not is_weekly and not is_monthly:
        log("Not Monday and not 1st of month — nothing to do.")
        return

    if is_weekly:
        run_reports("weekly")

    if is_monthly:
        run_reports("monthly")

    log("\nAll reports complete.")


if __name__ == "__main__":
    main()
