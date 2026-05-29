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

sb     = create_client(SUPABASE_URL, SUPABASE_KEY)
openai = OpenAI(api_key=OPENAI_KEY)

# ── Logo ──────────────────────────────────────────────────────────────────────

LOGO_PATH = os.path.join(os.path.dirname(__file__), "logo.png")
with open(LOGO_PATH, "rb") as _f:
    LOGO_B64 = base64.b64encode(_f.read()).decode()

# ── Logging ───────────────────────────────────────────────────────────────────

def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

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

INTERNAL_REPORT_PROMPT = """
You are the Stamina CS Intelligence agent generating an internal {period} report for a CSM pair.

This is a comprehensive internal document covering ALL accounts for this pair — 7 to 8 pages.
INTERNAL ONLY. The CSM pair and staff success manager read this before the Monday sprint.
Never share with clients under any circumstances.

## Document structure — same every {period}, no exceptions

---

### SECTION 1 — Pair Scorecard

| Metric | This {period} | Prior {period} | Change |
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

One-line overall pair performance summary.

---

### SECTION 2 — Accounts Needing Immediate Attention
(Accounts with critical underperformance, onboarding risks, or escalation flags — list first, full detail)

For each critical account:

**[Account Name]** | Tier: [tier] | Health score: [n] | Inboxes: [active] active / [disconnected] disconnected

Performance this {period} vs prior {period} vs measurement contract threshold:
| Metric | This {period} | Prior {period} | Threshold | Status |
|---|---|---|---|---|
| Emails sent | | | | ✓/✗ |
| Reply rate | | | | ✓/✗ |
| Positive replies | | | | ✓/✗ |
| Bounce rate | | | | ✓/✗ |
| Live campaigns | | | | |

What happened this {period}:
- Meetings: [date] [type] — [1-line summary of what was discussed/decided]
- Slack: [key customer messages — flag requests, complaints, tone shifts, or silence]
- Replies: [named positive replies with company + prospect name; unreplied leads with days elapsed]
- Campaigns: [which campaigns/variants are running, any notable segment performance]
- Open issues: [title, priority, days open]

Why it needs attention: [specific diagnosis — don't soften. Name the exact problem.]

[internal review only]
- Renewal window: [days until renewal if known]
- Upsell signal: [which lever, what data triggered it, forward commitment status and progress %]
- Customer bandwidth: [responsive vs slow, solo vs team, any bandwidth concerns]
- Escalation recommendation: [should Amartya be looped in? Why?]
[end internal review only]

---

### SECTION 3 — All Other Accounts
(Accounts performing at or above threshold — shorter blocks, same structure)

For each account:

**[Account Name]** | Tier: [tier] | Health: [n] | [key metric snapshot in one line]

- Performance: [2-3 bullet summary of key metrics vs prior {period}]
- This {period}: [meetings, notable Slack, replies — 2-3 bullets max]
- Flag if any: [any single issue worth noting even if account is healthy]
[internal review only] Upsell: [lever + signal if any] | Renewal: [window if relevant] [end internal review only]

---

### SECTION 4 — Onboarding Risk Accounts
(Accounts added 2–7 days ago with 0 active inboxes OR 0 meetings — must be flagged every {period} until resolved)

For each:
- Account name, days since onboarded, what's missing (inboxes / meetings / both)
- Last Slack activity from customer (if any)
- Recommended action: who does what, by when

---

### SECTION 5 — Reply Coaching Summary (Internal)
For every account with positive replies this {period}:
- Account name | [n] positive replies | [n] unreplied
- Unreplied leads: name, company, days elapsed, recommended recovery move (call / SMS / last-chance email)
- Pattern note: if the customer's reply behavior is consistently poor, flag it here for the Monday sprint discussion

---

### SECTION 6 — Sprint Priorities & Upsell Pipeline

**This {period}'s sprint priorities (top 3–5, ranked):**
For each: account + what needs to happen + owner (CSM / GTM Engineer) + deadline

**Upsell conversations to have this {period}:**
For each: account + lever + why now (the specific data signal) + suggested opening line for the CSM

**Accounts to loop Amartya into:**
For each: account + why + urgency

**Forward commitment tracking (monthly only):**
For each account with an active forward commitment:
- Account | KPI committed | Target date | Current progress | On track? | Upsell conversation timing

---

## Rules — non-negotiable
- Aggressive, direct tone throughout. Surface problems clearly. Never soften bad news.
- Every account must appear — no skipping accounts with no data (note "no data this period" instead)
- [internal review only] ... [end internal review only] marks content that never goes to client
- Never name a price in any block
- Unreplied positive leads: name the lead, name the account, state days elapsed, state the recovery move
- Onboarding risk flag must appear every {period} until resolved — don't let it drop off
- Tables for scorecard and metrics. Prose + bullets for commentary.
- 7–8 pages. Be thorough. This is the document the pair uses to run their week.
- For monthly: Section 6 must include forward commitment tracking table for every account that has one
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
| Reply rate | | | | | ✓/✗ |
| Positive reply rate | | | | | ✓/✗ |
| Positive replies (count) | | | | | |
| Bounce rate | | | | | ✓/✗ |
| Live campaigns | | | | | |
| Leads contacted | | | | | |

Note: If a metric has been below threshold for 2+ consecutive weeks, flag it explicitly below the table.

### 2. Audience Visibility
- Unique contacts emailed this week, broken down by campaign segment
- Reply sentiment breakdown: positive / neutral / negative / out-of-office / unsubscribe (counts)
- Named list of positive-reply companies: [Company] — [Prospect Name, Title] — [one-line reply summary]
- Any anomalies the client should know about (bounce spike, deliverability issue, segment with 0 replies, inbox disconnect)

### 3. What's Working — Performance Insights
Specific and actionable only. "Variant B drove 22% open rate vs Variant A's 14% — recommend killing A" is good.
"Engagement is up" is not acceptable.
- Which subject line variants drove highest open rates (name variants, name the %s)
- Which message variants drove highest positive reply rates (name them, name the %s)
- Which campaign segments outperformed others and by how much (name the segments)
- Which sequence touch converted best: touch 1 vs follow-up 1 vs follow-up 2 (with counts)
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
| Reply rate | | | | | ✓/✗ |
| Positive reply rate | | | | | ✓/✗ |
| Positive replies (count) | | | | | |
| Bounce rate | | | | | ✓/✗ |
| Live campaigns | | | | | |
| Leads contacted | | | | | |

### 2. Audience Visibility
Monthly totals by segment plus cumulative figures since launch.
Sentiment breakdown across the full month. Named positive-reply companies for the month.

### 3. What's Working — Month-Level Insights
Subject line learnings across ALL variants tested this month (more data = stronger conclusions).
Sequence touch analysis across the full month's sample — which touch number drives conversion at what rate.
Reply velocity correlation: which response times converted at what rate (reply within 2h vs 24h vs 48h+).

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
    )

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


def format_account_for_prompt(data: dict, period: str) -> str:
    c        = data["customer"]
    metrics  = data["metrics"]
    prior    = data["prior_metrics"]
    meetings = data["meetings"]
    slack    = data["slack"]
    issues   = data["issues"]

    # Aggregate metrics
    def avg(rows, field):
        vals = [r[field] for r in rows if r.get(field) is not None]
        return round(sum(vals) / len(vals), 2) if vals else None

    def total(rows, field):
        vals = [r[field] for r in rows if r.get(field) is not None]
        return sum(vals) if vals else 0

    cur_sent  = total(metrics, "emails_sent_total") or total(metrics, "number_of_emails_sent")
    cur_rr    = avg(metrics, "reply_rate")
    cur_pr    = total(metrics, "positive_replies")
    cur_br    = avg(metrics, "bounce_rate")
    cur_leads = total(metrics, "total_leads_contacted")
    cur_camps = avg(metrics, "live_campaigns")

    prev_sent = total(prior, "emails_sent_total") or total(prior, "number_of_emails_sent")
    prev_rr   = avg(prior, "reply_rate")
    prev_pr   = total(prior, "positive_replies")
    prev_leads = total(prior, "total_leads_contacted")

    def delta(cur, prev):
        if cur is None or prev is None or prev == 0:
            return "N/A"
        return f"{((cur - prev) / prev * 100):+.1f}%"

    meetings_text = "\n".join(
        f"  - {m['meeting_date'][:10]} [{m['meeting_type']}] {m['title']}: "
        f"{(m.get('summary_text') or '')[:300]}"
        for m in meetings
    ) or "  None this period"

    slack_text = "\n".join(
        f"  - {s['message_date'][:10]} ({'internal' if s['is_internal'] else 'customer'}): "
        f"{s['text'][:150]}"
        for s in slack[-10:]
    ) or "  No messages"

    # Aggregate reply data by label
    replies       = data["reply_data"]
    label_counts  = {}
    positive_list = []
    unreplied_pos = []
    for r in replies:
        lbl = r.get("reply_label", "unknown")
        label_counts[lbl] = label_counts.get(lbl, 0) + 1
        if lbl in ("positive", "interested"):
            name = f"{r.get('prospect_first_name','')} {r.get('prospect_last_name','')}".strip()
            co   = r.get("prospect_company", "")
            positive_list.append(f"{name} ({co}): {(r.get('reply_body') or '')[:120]}")
            if not r.get("customer_responded"):
                days = round((r.get("customer_response_delay_hrs") or 999) / 24)
                unreplied_pos.append(f"{name} ({co}) — {days}+ days unreplied")

    reply_summary = "\n".join(f"  {lbl}: {cnt}" for lbl, cnt in label_counts.items()) or "  No replies this period"
    if positive_list:
        reply_summary += "\n  Positive replies:\n" + "\n".join(f"    - {p}" for p in positive_list[:10])
    if unreplied_pos:
        reply_summary += "\n  ⚠ UNREPLIED POSITIVE LEADS:\n" + "\n".join(f"    - {p}" for p in unreplied_pos)

    issues_text = "\n".join(
        f"  - [{i['priority']}] {i['title']} ({i['status']})"
        for i in issues
    ) or "  None"

    campaign_text = "\n".join(
        f"  - {camp['campaign_name']} [{camp.get('segment','')} / {camp.get('variant_name','')}]: "
        f"sent={camp.get('emails_sent',0)}, reply_rate={camp.get('reply_rate',0)}%, "
        f"positive={camp.get('positive_replies',0)}, negative={camp.get('negative_replies',0)}"
        for camp in data["campaigns"][:8]
    ) or "  No campaign data"

    onboarding_flag = "\n⚠ ONBOARDING RISK: Account added in last 2–7 days with 0 active inboxes or 0 meetings." if data["onboarding_risk"] else ""

    kickoff_snippet = f"\nMeasurement contract context:\n{data['kickoff_context'][:800]}" if data["kickoff_context"] else ""

    return f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ACCOUNT: {c['name']} | Tier: {c.get('tier','?')} | Health: {c.get('health_score','?')} | Inboxes: {c.get('active_inboxes',0)} active / {c.get('disconnected_inboxes',0)} disconnected{onboarding_flag}

PERFORMANCE (this {period} vs prior {period}):
  Emails sent:          {cur_sent} vs {prev_sent} ({delta(cur_sent, prev_sent)})
  Leads contacted:      {cur_leads} vs {prev_leads} ({delta(cur_leads, prev_leads)})
  Reply rate:           {cur_rr}% vs {prev_rr}% ({delta(cur_rr, prev_rr)})
  Positive replies:     {cur_pr} vs {prev_pr} ({delta(cur_pr, prev_pr)})
  Bounce rate:          {cur_br}%
  Live campaigns:       {cur_camps}

MEETINGS:
{meetings_text}

SLACK MESSAGES:
{slack_text}

REPLY DATA:
{reply_summary}

CAMPAIGNS:
{campaign_text}

OPEN ISSUES:
{issues_text}
{kickoff_snippet}
"""


# ── GPT-4o generation ─────────────────────────────────────────────────────────

def generate_internal_report(pair: dict, accounts_data: list, period: str,
                              start_date: str, end_date: str) -> str:
    period_label = "week" if period == "weekly" else "month"
    prior_label  = "prior week" if period == "weekly" else "prior month"

    system = INTERNAL_REPORT_PROMPT.format(
        period=period_label,
        prior_period_label=prior_label,
    )

    accounts_block = "\n".join(
        format_account_for_prompt(d, period_label) for d in accounts_data
    )

    user = f"""Generate the internal {period_label} report for {pair['pair_name']}.

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

    account_block = format_account_for_prompt(account_data, period_label)
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

    payload = {
        "from":    RESEND_FROM,
        "to":      to_emails,
        "cc":      [AMARTYA_EMAIL],
        "bcc":     BCC_EMAILS,
        "reply_to": AMARTYA_EMAIL,
        "template": {"id": template_alias, "variables": {}},
        "attachments": [{"filename": filename, "content": base64.b64encode(pdf_bytes).decode()}],
    }

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
                internal_md  = generate_internal_report(pair, accounts_data, period, start_str, end_str)
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
                external_md  = generate_external_report(data, period, start_str, end_str)
                external_pdf = generate_pdf(
                    external_md,
                    title    = name,
                    subtitle = label,
                    is_internal = False,
                )
                if pylon_id:
                    url = upload_to_pylon(external_pdf, filename, pylon_id)
                    return name, url
                else:
                    return name, "no-pylon-id"
            except Exception as e:
                return name, f"ERROR: {e}"

        with ThreadPoolExecutor(max_workers=5) as executor:
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
