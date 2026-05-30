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

DRY_RUN         = "--dry-run"       in sys.argv
FORCE_WEEKLY    = "--weekly"        in sys.argv
FORCE_MONTHLY   = "--monthly"       in sys.argv
INTERNAL_ONLY   = "--internal-only" in sys.argv  # skip external PDFs
EXTERNAL_ONLY   = "--external-only" in sys.argv  # skip internal email
PAIR_FILTER     = next((sys.argv[i+1] for i, a in enumerate(sys.argv) if a == "--pair"),    None)
ACCOUNT_FILTER  = next((sys.argv[i+1] for i, a in enumerate(sys.argv) if a == "--account"), None)

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

INTERNAL ONLY. 7–8 pages. The CSM pair and staff success manager use this to run their Monday sprint.
CRITICAL: Use ONLY the actual data provided below. Do not invent, estimate, or generalise.
Every number, every account name, every Slack message, every campaign stat must come from the data.
If data is missing for an account, say "no data this week" — do not fabricate.

---

## Data sources in the input (use ALL of them):
- MEETINGS: CS CALLS contain customer commitments, concerns, expansion signals — use the full summary.
  STANDUPS contain internal team status updates on this account's tickets and actions.
  KICKOFFS contain the agreed expectations and plan.
- METRICS: emails_sent_total, reply_rate, positive_replies, bounce_rate, live_campaigns, total_leads_contacted
  → Compare this week vs prior week. Flag if reply_rate or positive_replies dropped significantly.
- CAMPAIGNS: campaign_name, segment, variant_name, emails_sent, reply_rate, positive_reply_rate, positive_replies, negative_replies
  → Which variants are outperforming? Which segments are struggling? Name them with actual %s.
- MEETINGS: date, type, summary
  → What was discussed? Any commitments made? Flag kickoffs, CS calls, or missed cadence.
- SLACK: customer vs internal messages with timestamps
  → Extract actual customer questions, concerns, requests, or signals of disengagement.
  → Flag tone shifts (was positive, now silent). Quote specific messages where relevant.
- REPLIES: reply_label (positive/negative/neutral), reply_body, customer_responded, response_delay_hrs
  → Name every positive reply (prospect name, company). Flag unreplied leads with exact days elapsed.
  → Surface objection patterns from negative replies.
- INBOXES: email_account, is_active, health_score, bounce_rate
  → Any disconnected inboxes? Any bounce_rate > 2%? Any warmup health < 90%?
- ISSUES: title, priority, status
  → Open Pylon issues — how long have they been open?
- KICKOFF CONTEXT: measurement contract thresholds, forward commitment
  → Use thresholds to determine ✓/✗ status for each metric. Reference the forward commitment progress.

---

## SECTION 1 — Pair Scorecard

Fill every cell with actual numbers from the data:

| Metric | This week | Prior week | Change |
|---|---|---|---|
| Total accounts | [count] | [count] | [±n] |
| Accounts above threshold (all metrics) | [count] | [count] | [±n] |
| Accounts with 1+ metric below threshold | [count] | [count] | [±n] |
| Accounts with 2+ metrics below threshold | [count] | [count] | [±n] |
| Total positive replies | [count] | [count] | [±n] |
| Total unreplied positive leads | [count] | [count] | [±n] |
| Open Pylon issues | [count] | [count] | [±n] |
| Onboarding risks | [count] | [count] | [±n] |

One-line pair performance summary based on the actual numbers above.

---

## SECTION 2 — Accounts Needing Immediate Attention
List FIRST. Include every account where ANY of the following apply:
- reply_rate below 1% threshold, bounce_rate > 2%, disconnected inboxes, zero positive replies
- unreplied positive leads or slow response > 2h
- open escalation issues
- onboarding risk (0 inboxes or 0 meetings in first 7 days)
- campaign_progress > 65% (new campaigns needed — flagged in METRICS section)
- e_l_ratio outside 400–700 range (flagged in METRICS section)
- 🚨 ENGAGEMENT FLAGS in data: Slack silence, no CS call > 14 days, neg/pos ratio > 3:1, A/B variant gap
- CS call raised concerns, customer disengagement signals, or expansion discussion

For each:

**[EXACT Account Name]** | Tier: [tier] | Health: [n] | Inboxes: [n active] / [n disconnected]

| Metric | This week | Prior week | Threshold | Status |
|---|---|---|---|---|
| Emails sent | [actual number] | [actual number] | — | |
| Leads contacted | [actual number] | [actual number] | — | |
| Reply rate | [actual %] | [actual %] | [from measurement contract or —] | ✓/✗ |
| Positive replies | [actual count] | [actual count] | — | |
| Bounce rate | [actual %] | [actual %] | 2% | ✓/✗ |
| Live campaigns | [actual count] | — | — | |

What happened this week (use actual data from input):
- CS Calls: [for each CS call — date, what the customer said, any commitments made by either side,
  concerns raised, expansion signals. Quote directly from summary where possible.]
- Standups: [for each standup — what was discussed about this account, any status updates on
  existing tickets or actions, what the team agreed to do next.]
- Engagement flags: [reference the ENGAGEMENT FLAGS section in the data — Slack silence days,
  last CS call date, neg/pos reply ratio, A/B variant gaps. Use exact numbers from the flags.]
- Slack: [quote actual customer messages with timestamps — flag concerns, silence, or requests]
- Campaigns: [name actual campaigns/variants with actual reply_rate and positive_reply_rate %s.
  Flag campaign_progress > 65% and e_l_ratio outside 400–700 if present in data.]
- Replies: [name each positive reply prospect + company; for negative replies, name the objection pattern]
- Inbox issues: [list any disconnected or unhealthy inboxes with actual health_score and bounce_rate]
- Open issues: [list title, priority, days open]

Why it needs attention: [specific diagnosis using the actual data above — name exact metrics, exact accounts, exact numbers]

[internal review only]
- Forward commitment: [from kickoff context — current progress vs target]
- Upsell signal: [specific lever + specific data point that triggered it]
- Customer bandwidth: [infer from Slack response frequency and meeting attendance]
- Escalation: Yes/No — [specific reason if yes]
[end internal review only]

---

## SECTION 3 — All Other Accounts (performing at or above threshold)

For each account not in Section 2:

**[EXACT Account Name]** | Tier: [tier] | Health: [n] | [actual metric snapshot: X emails, Y% reply rate, Z positive replies]
- Metrics: [actual this week vs prior week with %s — name what moved]
- Campaigns: [actual top-performing variant or segment with real numbers]
- Activity: [any meetings, Slack activity, or positive replies this week — name them]
- Flag: [anything worth noting even if healthy — quote specific Slack, name specific reply]
[internal review only] Upsell: [lever + actual data signal if any] | Renewal: [window if known] [end internal review only]

---

## SECTION 4 — Onboarding Risk Accounts
Accounts 2–7 days old with 0 active inboxes OR 0 meetings.

For each: account name | days since created | what's missing | last Slack message from customer (quote it) | action needed

---

## SECTION 5 — Reply Coaching Summary

For every account with positive replies:
Account | [n] positive | [n] unreplied
For each unreplied: [prospect name] at [company] — replied [X] days ago — "[quote their reply body]" — Recovery: [call now / SMS / last-chance email with suggested text]

Pattern flags: if the same customer consistently ignores positive leads, name it with evidence.

---

## SECTION 6 — Sprint Priorities & Upsell Pipeline

Sprint priorities (top 3–5, ranked by urgency):
| Account | What needs to happen | Owner | Deadline |
|---|---|---|---|

Upsell conversations this week (use actual data signals):
| Account | Lever | Signal (actual data point) | Suggested opening |
|---|---|---|---|

Escalate to Amartya: [account | specific reason | urgency]

---

## Non-negotiable rules
- Use actual names, actual numbers, actual dates from the data. Never generic statements like "engagement is low."
- Every account in the data must appear in either Section 2 or Section 3.
- Tables must be filled with actual values — no empty cells with placeholders.
- [internal review only] ... [end internal review only] never goes to clients
- Never name a price
- Aggressive tone: "Reply rate is 0.4% for the 3rd consecutive week — this campaign is failing" not "reply rate could improve"
- Every severity claim must cite evidence: never write "severe inbox health issues" or "critical deliverability problem" without immediately following it with the specific data — e.g. "bounce rate: 4.2% (threshold: 2%), inbox health score: 67%" or "3 of 12 inboxes disconnected." If you can't cite a specific number from the data, do not make the claim.
- 7–8 pages. Every sentence must reference specific data.
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
- CS Calls: [list each CS call with date and key points — customer commitments, concerns raised,
  expansion signals, tone (engaged/disengaged). Quote from summary where relevant.]
- Standups: [recurring patterns from standups — what issues kept coming up for this account,
  what the team agreed, what was actioned vs outstanding.]
- Kickoffs: [if applicable — what was agreed, what the customer's goals are]
- Engagement flags: [from ENGAGEMENT FLAGS in the data — Slack silence, last CS call date,
  neg/pos ratio, A/B gaps. Use exact numbers. campaign_progress > 65% = new campaigns urgently needed.]
- Slack: [theme of customer Slack activity — engaged / requesting / quiet / concerned]
- Replies: [total positive, total unreplied, any notable reply patterns]
- Campaigns: [segment and variant performance highlights. Flag e_l_ratio outside 400–700 if present.]
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


# ── Shared thresholds (same as queue_generator) ───────────────────────────────
THRESH_RR_POOR     = 1.0   # reply_rate % — below = flag
THRESH_RR_GOOD     = 3.0   # reply_rate % — above = upsell signal
THRESH_PRR_POOR    = 0.5   # positive_reply_rate % — below = flag
THRESH_PRR_GOOD    = 1.5   # positive_reply_rate % — above = upsell signal
THRESH_BOUNCE      = 2.0   # bounce_rate % — above = flag
THRESH_BOUNCE_CRIT = 4.0   # bounce_rate % — above = urgent
THRESH_HEALTH_WARN = 90    # inbox health % — below = warn
THRESH_HEALTH_CRIT = 80    # inbox health % — below = flag
THRESH_CAMP_PROG   = 65    # campaign_progress % — above = new campaigns needed
THRESH_EL_MIN      = 400   # e_l_ratio lower bound
THRESH_EL_MAX      = 700   # e_l_ratio upper bound
THRESH_EL_SENT     = 1200  # minimum emails_sent_total to check e_l_ratio
THRESH_CAMP_SENT   = 800   # minimum campaign emails_sent to flag variant


def _pct(v) -> str:
    """Round float to 2dp and append %."""
    return f"{round(float(v), 2)}%" if v is not None else "N/A"


def _metrics_block(metrics: list, prior: list, period: str) -> str:
    """Compute and format the core metrics block — pre-flagged against thresholds."""
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

    # Threshold flags
    rr_flag = (f" 🚨 BELOW {THRESH_RR_POOR}% THRESHOLD"
               if cur_rr is not None and cur_rr < THRESH_RR_POOR else
               (f" ✅ ABOVE {THRESH_RR_GOOD}%" if cur_rr is not None and cur_rr > THRESH_RR_GOOD else ""))
    br_flag = (f" 🚨 CRITICAL — ABOVE {THRESH_BOUNCE_CRIT}%"
               if cur_br is not None and cur_br > THRESH_BOUNCE_CRIT else
               (f" 🚨 ABOVE {THRESH_BOUNCE}% THRESHOLD"
                if cur_br is not None and cur_br > THRESH_BOUNCE else ""))
    pr_flag = " 🚨 ZERO POSITIVE REPLIES" if cur_pr == 0 and cur_sent > 500 else ""

    return (
        f"  Emails sent:       {cur_sent} vs {prev_sent} ({delta(cur_sent, prev_sent)})\n"
        f"  Leads contacted:   {cur_leads} vs {prev_leads} ({delta(cur_leads, prev_leads)})\n"
        f"  Reply rate:        {_pct(cur_rr)} vs {_pct(prev_rr)} ({delta(cur_rr, prev_rr)}){rr_flag}\n"
        f"  Positive replies:  {cur_pr} vs {prev_pr} ({delta(cur_pr, prev_pr)}){pr_flag}\n"
        f"  Bounce rate:       {_pct(cur_br)}{br_flag}\n"
        f"  Live campaigns:    {cur_camps}"
    )


def format_account_for_internal(data: dict, period: str) -> str:
    """
    Compact account block for the internal report — pre-flagged against thresholds.
    Same benchmark logic as queue_generator but over the full report period.
    Budget: ~2500 chars per account.
    """
    c        = data["customer"]
    meetings = data["meetings"]
    slack    = data["slack"]
    issues   = data["issues"]
    replies  = data["reply_data"]
    metrics  = data["metrics"]
    inboxes  = data["inboxes"]

    onboarding_flag = " 🚨 ONBOARDING RISK" if data["onboarding_risk"] else ""

    # ── Metrics with threshold flags ──────────────────────────────────────────
    metrics_text = _metrics_block(metrics, data["prior_metrics"], period)

    # Extra metric-level flags
    extra_flags = []
    if metrics:
        # campaign_progress > 65%
        for m in metrics:
            cp = m.get("campaign_progress")
            if cp is not None:
                try:
                    cp_val = round(float(str(cp).replace("%", "")), 1)
                    if cp_val > THRESH_CAMP_PROG:
                        extra_flags.append(f"🚨 campaign_progress {cp_val}% — new campaigns required (threshold: {THRESH_CAMP_PROG}%)")
                        break
                except (ValueError, TypeError):
                    pass
        # e_l_ratio
        for m in metrics:
            el   = m.get("e_l_ratio")
            sent = m.get("emails_sent_total") or m.get("number_of_emails_sent", 0) or 0
            if el is not None and sent >= THRESH_EL_SENT:
                try:
                    el_val = round(float(el), 1)
                    if el_val < THRESH_EL_MIN:
                        extra_flags.append(f"🚨 e_l_ratio {el_val} BELOW {THRESH_EL_MIN} (range: {THRESH_EL_MIN}–{THRESH_EL_MAX})")
                    elif el_val > THRESH_EL_MAX:
                        extra_flags.append(f"🚨 e_l_ratio {el_val} ABOVE {THRESH_EL_MAX} (range: {THRESH_EL_MIN}–{THRESH_EL_MAX})")
                except (ValueError, TypeError):
                    pass

    extra_text = "\n  " + "\n  ".join(extra_flags) if extra_flags else ""

    # ── Inbox health with flags ────────────────────────────────────────────────
    disconnected = c.get("disconnected_inboxes", 0) or 0
    active_cnt   = c.get("active_inboxes", 0) or 0
    inbox_flags  = []
    if disconnected > 0:
        inbox_flags.append(f"🚨 {disconnected} DISCONNECTED (threshold: 0)")
    for inbox in inboxes:
        br  = round(float(inbox.get("bounce_rate") or 0), 2)
        hs  = inbox.get("health_score") or 100
        act = inbox.get("is_active")
        if not act:
            inbox_flags.append(f"🚨 INACTIVE: {inbox['email_account']}")
        elif br > THRESH_BOUNCE_CRIT:
            inbox_flags.append(f"🚨 CRITICAL bounce {br}%: {inbox['email_account']}")
        elif br > THRESH_BOUNCE:
            inbox_flags.append(f"🚨 bounce {br}%: {inbox['email_account']}")
        if hs < THRESH_HEALTH_CRIT:
            inbox_flags.append(f"🚨 health {hs}%: {inbox['email_account']}")
        elif hs < THRESH_HEALTH_WARN:
            inbox_flags.append(f"⚠ health {hs}%: {inbox['email_account']}")
    inbox_summary = (f"{active_cnt} active / {disconnected} disconnected"
                     + (" | " + " | ".join(inbox_flags[:4]) if inbox_flags else " ✓"))

    # ── Campaign flags ─────────────────────────────────────────────────────────
    campaigns = sorted(data["campaigns"], key=lambda x: x.get("positive_reply_rate") or 0, reverse=True)
    camp_lines = []
    for camp in campaigns[:8]:
        rr  = round(float(camp.get("reply_rate") or 0), 2)
        prr = round(float(camp.get("positive_reply_rate") or 0), 2)
        br  = round(float(camp.get("bounce_rate") or 0), 2)
        pr  = camp.get("positive_replies") or 0
        nr  = camp.get("negative_replies") or 0
        sent = camp.get("emails_sent") or 0
        flags = []
        if sent >= THRESH_CAMP_SENT:
            if rr < THRESH_RR_POOR:
                flags.append(f"🚨 rr {rr}% BELOW {THRESH_RR_POOR}%")
            if prr < THRESH_PRR_POOR:
                flags.append(f"🚨 prr {prr}% BELOW {THRESH_PRR_POOR}%")
            if pr == 0:
                flags.append("🚨 ZERO positive replies")
            if br > THRESH_BOUNCE_CRIT:
                flags.append(f"🚨 bounce {br}% CRITICAL")
            elif br > THRESH_BOUNCE:
                flags.append(f"🚨 bounce {br}%")
        if prr > THRESH_PRR_GOOD:
            flags.append(f"✅ prr {prr}% STRONG")
        elif rr > THRESH_RR_GOOD:
            flags.append(f"✅ rr {rr}% STRONG")
        flag_str = " | ".join(flags)
        camp_lines.append(
            f"  {camp['campaign_name']} [{camp.get('segment','')}|{camp.get('variant_name','')}]: "
            f"sent={sent}, rr={rr}%, prr={prr}%, +{pr}/-{nr}"
            + (f" → {flag_str}" if flag_str else "")
        )
    camp_text = "\n".join(camp_lines) or "  None"

    # ── Meetings — all in period, separated by type, fuller summaries ──────────
    standups_r = [m for m in meetings if m.get("meeting_type") == "standup"]
    cs_calls_r = [m for m in meetings if m.get("meeting_type") == "cs_call"]
    kickoffs_r = [m for m in meetings if m.get("meeting_type") == "kickoff"]

    def _fmt_m(m):
        return (f"  {m['meeting_date'][:10]} — {m['title']}: "
                f"{(m.get('summary_text') or 'No summary')[:600]}")

    meetings_text = ""
    if cs_calls_r:
        meetings_text += "  CS CALLS:\n" + "\n".join(_fmt_m(m) for m in cs_calls_r) + "\n"
    if standups_r:
        meetings_text += "  STANDUPS:\n" + "\n".join(_fmt_m(m) for m in standups_r) + "\n"
    if kickoffs_r:
        meetings_text += "  KICKOFFS:\n" + "\n".join(_fmt_m(m) for m in kickoffs_r) + "\n"
    meetings_text = meetings_text.strip() or "  None this period"

    # ── Slack ──────────────────────────────────────────────────────────────────
    slack_text = "\n".join(
        f"  {s['message_date'][:10]} ({'CSM' if s['is_internal'] else 'CUSTOMER'}): {s['text'][:120]}"
        for s in slack[-8:]
    ) or "  No messages"

    # ── Replies ────────────────────────────────────────────────────────────────
    label_counts, pos_names, unreplied, slow = {}, [], [], []
    for r in replies:
        lbl = r.get("reply_label", "unknown")
        label_counts[lbl] = label_counts.get(lbl, 0) + 1
        if lbl in ("positive", "interested"):
            name      = f"{r.get('prospect_first_name','')} {r.get('prospect_last_name','')}".strip()
            co        = r.get("prospect_company", "")
            hrs       = round(r.get("customer_response_delay_hrs") or 0, 1)
            responded = r.get("customer_responded", False)
            pos_names.append(f"{name} ({co})")
            if not responded:
                unreplied.append(f"🚨 NO RESPONSE: {name} ({co})")
            elif hrs > 2:
                slow.append(f"⚠ SLOW ({hrs}h > 2h threshold): {name} ({co})")

    reply_text = " | ".join(f"{lbl}: {cnt}" for lbl, cnt in label_counts.items()) or "none"
    if pos_names:
        reply_text += "\n  Positive: " + ", ".join(pos_names[:6])
    if unreplied:
        reply_text += "\n  " + " | ".join(unreplied[:5])
    if slow:
        reply_text += "\n  " + " | ".join(slow[:5])

    # ── Issues ─────────────────────────────────────────────────────────────────
    issues_text = " | ".join(f"[{i['priority']}] {i['title']}" for i in issues) or "None"

    # ── Kickoff context ────────────────────────────────────────────────────────
    kickoff = f"\nMeasurement contract: {data['kickoff_context'][:600]}" if data["kickoff_context"] else ""

    # ── 1. Customer Slack silence (>10 days in reports) ───────────────────────
    SILENCE_THRESH = 10  # days (>5 for queue, >10 for reports)
    silence_flag = ""
    cust_msgs = [s for s in slack if not s.get("is_internal")]
    if cust_msgs:
        try:
            last_msg_date = max(s["message_date"] for s in cust_msgs)
            days_silent   = (now.date() - datetime.fromisoformat(
                last_msg_date.replace("Z","+00:00")).date()).days
            if days_silent > SILENCE_THRESH:
                silence_flag = (f"🚨 CUSTOMER SLACK SILENCE: {days_silent} days since last customer "
                                f"message (threshold: {SILENCE_THRESH} days). Last: {last_msg_date[:10]}")
        except Exception:
            pass
    else:
        silence_flag = f"🚨 CUSTOMER SLACK SILENCE: No customer messages in Slack channel this {period}"

    # ── 2. No CS call in >14 days ─────────────────────────────────────────────
    MEETING_THRESH = 14  # days (CS call expected every 2 weeks)
    meeting_flag = ""
    cs_calls = [m for m in meetings if m.get("meeting_type") == "cs_call"]
    if cs_calls:
        try:
            last_call = max(m["meeting_date"] for m in cs_calls)
            days_no_call = (now.date() - datetime.fromisoformat(
                last_call.replace("Z","+00:00")).date()).days
            if days_no_call > MEETING_THRESH:
                meeting_flag = (f"🚨 NO CS CALL IN {days_no_call} DAYS (threshold: {MEETING_THRESH} days). "
                                f"Last call: {last_call[:10]}")
        except Exception:
            pass
    else:
        # No CS call found in the report period — check last_meeting_date from Pylon
        last_meeting = c.get("last_meeting_date")
        if last_meeting:
            try:
                days_no_call = (now.date() - datetime.fromisoformat(
                    last_meeting.replace("Z","+00:00")).date()).days
                if days_no_call > MEETING_THRESH:
                    meeting_flag = (f"🚨 NO CS CALL IN {days_no_call} DAYS (threshold: {MEETING_THRESH} days). "
                                    f"Last call on record: {last_meeting[:10]}")
            except Exception:
                pass
        else:
            meeting_flag = "🚨 NO CS CALL ON RECORD for this account"

    # ── 3. Negative to positive reply ratio (>3:1, min 200 sent) ─────────────
    neg_pos_flag = ""
    total_pos_r = sum(camp.get("positive_replies") or 0 for camp in data["campaigns"])
    total_neg_r = sum(camp.get("negative_replies") or 0 for camp in data["campaigns"])
    total_sent_r = sum(camp.get("emails_sent") or 0 for camp in data["campaigns"])
    if total_sent_r >= 200 and total_pos_r > 0 and total_neg_r > 3 * total_pos_r:
        neg_pos_flag = (f"🚨 NEG/POS RATIO: {total_neg_r} negative vs {total_pos_r} positive "
                        f"({round(total_neg_r/total_pos_r,1)}:1 — threshold 3:1) — messaging/positioning review needed")
    elif total_sent_r >= 200 and total_pos_r == 0 and total_neg_r > 0:
        neg_pos_flag = (f"🚨 ZERO positive replies, {total_neg_r} negatives across all campaigns — "
                        f"positioning problem")

    # ── 4. A/B variant performance gap (best prr > 2x worst, min 200 sent) ───
    ab_flags = []
    camp_by_name_r: dict = {}
    for camp in data["campaigns"]:
        nm = camp.get("campaign_name","")
        if nm not in camp_by_name_r:
            camp_by_name_r[nm] = []
        camp_by_name_r[nm].append(camp)
    for camp_name, variants in camp_by_name_r.items():
        eligible = [v for v in variants if (v.get("emails_sent") or 0) >= 200]
        if len(eligible) < 2:
            continue
        prrs = [(v.get("variant_name","?"), round(float(v.get("positive_reply_rate") or 0), 2))
                for v in eligible]
        best_v, best_prr   = max(prrs, key=lambda x: x[1])
        worst_v, worst_prr = min(prrs, key=lambda x: x[1])
        if worst_prr > 0 and best_prr > 2 * worst_prr:
            ab_flags.append(
                f"🚨 A/B GAP '{camp_name}': '{best_v}' {best_prr}% vs '{worst_v}' {worst_prr}% "
                f"({round(best_prr/worst_prr,1)}x — threshold 2x). Kill '{worst_v}'")
        elif best_prr > 0 and worst_prr == 0:
            ab_flags.append(
                f"🚨 A/B GAP '{camp_name}': '{best_v}' {best_prr}% vs '{worst_v}' 0% — kill '{worst_v}'")

    engagement_lines = [f for f in [silence_flag, meeting_flag, neg_pos_flag] + ab_flags if f]
    engagement_text  = "\n  ".join(engagement_lines) if engagement_lines else "No engagement flags"

    return f"""
━━━ {c['name'].upper()} | {c.get('tier','?')} | Health:{c.get('health_score','?')} | {inbox_summary}{onboarding_flag}

METRICS ({period}):
{metrics_text}{extra_text}

CAMPAIGNS:
{camp_text}

ENGAGEMENT FLAGS:
  {engagement_text}

MEETINGS: {meetings_text}

SLACK: {slack_text}

REPLIES: {reply_text}

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

def md_inline(text: str) -> str:
    """Apply inline markdown (bold, code, internal tag) to any text fragment."""
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"`(.+?)`", r"<code>\1</code>", text)
    text = re.sub(r"\[internal review only\]",
                  r'<span class="internal-tag">[internal review only]</span>', text)
    return text


def _is_table_row(line: str) -> bool:
    return line.startswith("|") and line.endswith("|") and line.count("|") >= 2


def _is_separator_row(line: str) -> bool:
    return _is_table_row(line) and all(c in "|-:— " for c in line)


def _parse_cells(line: str) -> list:
    cells = [c.strip() for c in line.split("|")]
    return [c for c in cells if c != ""]


def _build_html_table(header: list, rows: list, css_class: str = "md-table") -> str:
    th = "".join(f"<th>{md_inline(h)}</th>" for h in header)
    body = ""
    for i, row in enumerate(rows):
        cells = "".join(f"<td>{md_inline(c)}</td>" for c in row)
        cls = ' class="alt"' if i % 2 == 1 else ""
        body += f"<tr{cls}>{cells}</tr>"
    return f'<table class="{css_class}"><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table>'


def md_to_html_body(md: str) -> str:
    lines = md.split("\n")
    html_lines = []
    i = 0
    while i < len(lines):
        line = lines[i]

        # ── Markdown table detection ──────────────────────────────────────────
        if (_is_table_row(line) and i + 1 < len(lines)
                and _is_separator_row(lines[i + 1])):
            header = _parse_cells(line)
            i += 2  # skip header + separator
            rows = []
            while i < len(lines) and _is_table_row(lines[i]):
                rows.append(_parse_cells(lines[i]))
                i += 1
            html_lines.append(_build_html_table(header, rows))
            continue

        # ── Normal elements ───────────────────────────────────────────────────
        stripped = line.lstrip()
        indent   = len(line) - len(stripped)
        ml       = 14 + min(indent, 4) * 4  # indent up to 2 levels

        if stripped.startswith("# "):
            html_lines.append(f'<h1>{md_inline(stripped[2:])}</h1>')
        elif stripped.startswith("## "):
            html_lines.append(f'<h2>{md_inline(stripped[3:])}</h2>')
        elif stripped.startswith("### "):
            html_lines.append(f'<h3>{md_inline(stripped[4:])}</h3>')
        elif stripped.startswith("- ") or stripped.startswith("* "):
            html_lines.append(f'<div class="bullet" style="margin-left:{ml}px">• {md_inline(stripped[2:])}</div>')
        elif stripped.startswith("━") or stripped.startswith("---"):
            html_lines.append('<hr class="divider">')
        elif stripped == "":
            html_lines.append('<div class="spacer"></div>')
        else:
            html_lines.append(f'<p style="margin-left:{min(indent,2)*8}px">{md_inline(line)}</p>')
        i += 1

    return "\n".join(html_lines)


def generate_pdf(content_md: str, title: str, subtitle: str, is_internal: bool) -> bytes:
    """Route to internal or external PDF renderer based on is_internal flag."""
    if is_internal:
        return _generate_internal_pdf(content_md, title, subtitle)
    else:
        return _generate_external_pdf(content_md, title, subtitle)


def _generate_internal_pdf(content_md: str, title: str, subtitle: str) -> bytes:
    from weasyprint import HTML as WP_HTML
    body_html = md_to_html_body(content_md)
    today = datetime.now().strftime("%B %d, %Y")
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
  @page {{ size: A4; margin: 18mm 14mm 25mm 14mm; }}
  @page :first {{ margin-top: 0; }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: "Helvetica Neue", Arial, sans-serif; background: white; color: #1a1a1a;
          font-size: 11px; line-height: 1.55; orphans: 3; widows: 3; }}
  .header {{ background: #1a2035; padding: 22px 36px; display: flex; align-items: center;
             justify-content: space-between; }}
  .header img {{ height: 22px; filter: brightness(0) invert(1); }}
  .header-right {{ text-align: right; }}
  .header-right .label {{ color: #8892a4; font-size: 9px; letter-spacing: 1.5px; text-transform: uppercase; }}
  .header-right .title {{ color: white; font-size: 14px; font-weight: 700; margin-top: 3px; }}
  .header-right .sub {{ color: #8892a4; font-size: 10px; margin-top: 2px; }}
  .internal-banner {{ background: #b91c1c; color: white; text-align: center; font-size: 9px;
                      font-weight: 700; letter-spacing: 2px; text-transform: uppercase; padding: 4px; }}
  .body {{ padding: 16px 0 0; }}
  h1 {{ font-size: 13px; font-weight: 700; color: #1a2035; margin: 16px 0 6px;
        border-bottom: 2px solid #1a2035; padding-bottom: 4px; page-break-after: avoid; }}
  h2 {{ font-size: 11.5px; font-weight: 700; color: #1a2035; margin: 12px 0 4px;
        page-break-after: avoid; }}
  h3 {{ font-size: 10px; font-weight: 700; color: #555; margin: 10px 0 3px;
        text-transform: uppercase; letter-spacing: 0.5px; page-break-after: avoid; }}
  p {{ margin-bottom: 4px; color: #333; page-break-inside: avoid; }}
  .bullet {{ margin: 2px 0 2px 12px; color: #333; page-break-inside: avoid; }}
  .spacer {{ height: 5px; }}
  hr.divider {{ border: none; border-top: 1px solid #e8eaed; margin: 10px 0; }}
  .internal-tag {{ color: #dc2626; font-weight: 700; font-size: 9px; }}
  strong {{ color: #111; }}
  code {{ background: #f4f4f4; padding: 1px 3px; border-radius: 3px; font-size: 9px; font-family: monospace; }}
  table.md-table {{ width: 100%; border-collapse: collapse; margin: 10px 0; font-size: 10px; page-break-inside: avoid; }}
  table.md-table thead tr {{ background: #1a2035; color: white; }}
  table.md-table thead th {{ padding: 6px 8px; text-align: left; font-weight: 600; font-size: 9.5px; }}
  table.md-table tbody td {{ padding: 5px 8px; border-bottom: 1px solid #e8eaed; color: #333; }}
  table.md-table tbody tr.alt {{ background: #f8f9fb; }}
  .footer {{ position: fixed; bottom: 0; left: 0; right: 0; padding: 6px 36px;
             border-top: 1px solid #e8eaed; display: flex; justify-content: space-between;
             font-size: 9px; color: #bbb; background: white; }}
</style></head>
<body>
<div class="header">
  <img src="data:image/png;base64,{LOGO_B64}">
  <div class="header-right">
    <div class="label">Internal Report</div>
    <div class="title">{title}</div>
    <div class="sub">{subtitle}</div>
  </div>
</div>
<div class="internal-banner">⚠ INTERNAL ONLY — DO NOT SHARE WITH CLIENTS</div>
<div class="body">{body_html}</div>
<div class="footer">
  <span>Stamina CS Intelligence · {today}</span>
  <span>INTERNAL — {title}</span>
</div>
</body></html>"""
    return WP_HTML(string=html).write_pdf()


def _generate_external_pdf(content_md: str, title: str, subtitle: str) -> bytes:
    """Enterprise-grade external PDF — customer-ready, clean white design."""
    from weasyprint import HTML as WP_HTML
    body_html = md_to_html_body(content_md)
    today = datetime.now().strftime("%B %d, %Y")
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
  @page {{ size: A4; margin: 16mm 16mm 22mm 16mm; }}
  @page :first {{ margin-top: 14mm; }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: "Helvetica Neue", Arial, sans-serif; background: white; color: #1a1a1a;
          font-size: 11px; line-height: 1.55; orphans: 3; widows: 3; }}

  /* ── White header — logo left, company + period right ── */
  .header {{ background: white; padding: 16px 0 14px; display: flex;
             align-items: center; justify-content: space-between;
             border-bottom: 2px solid #1a2035; margin-bottom: 4px; }}
  .header img {{ height: 22px; }}
  .header-meta {{ text-align: right; }}
  .header-meta .company {{ color: #1a1a1a; font-size: 14px; font-weight: 700; }}
  .header-meta .period {{ color: #888; font-size: 9.5px; margin-top: 2px; }}

  /* ── Body ── */
  .body {{ padding: 10px 0 0; }}

  /* ── Section headings ── */
  h1 {{ font-size: 13px; font-weight: 700; color: #1a1a1a; margin: 16px 0 6px;
        padding-bottom: 4px; border-bottom: 1.5px solid #1a1a1a; page-break-after: avoid; }}
  h2 {{ font-size: 11.5px; font-weight: 700; color: #1a1a1a; margin: 12px 0 4px;
        page-break-after: avoid; }}
  h3 {{ font-size: 10.5px; font-weight: 700; color: #333; margin: 9px 0 3px;
        page-break-after: avoid; }}

  /* ── Body text — no dashes, proper bullets ── */
  p {{ margin-bottom: 4px; color: #2d2d2d; page-break-inside: avoid; line-height: 1.6; }}
  .bullet {{ margin: 2px 0 2px 14px; color: #2d2d2d; page-break-inside: avoid; line-height: 1.5; }}
  .spacer {{ height: 4px; }}
  hr.divider {{ border: none; border-top: 1px solid #e8eaed; margin: 10px 0; }}
  strong {{ color: #111; font-weight: 600; }}
  code {{ background: #f5f6f8; padding: 1px 4px; border-radius: 3px;
          font-size: 9.5px; font-family: monospace; color: #333; }}

  /* ── Tables — neutral header, no blue ── */
  table.md-table {{ width: 100%; border-collapse: collapse; margin: 8px 0;
                    font-size: 10.5px; page-break-inside: avoid; }}
  table.md-table thead tr {{ background: #1a1a1a; color: white; }}
  table.md-table thead th {{ padding: 6px 9px; text-align: left; font-weight: 600; }}
  table.md-table tbody td {{ padding: 5px 9px; border-bottom: 1px solid #eaecf0; color: #2d2d2d; }}
  table.md-table tbody tr.alt {{ background: #f9f9f9; }}

  /* ── Footer ── */
  .footer {{ position: fixed; bottom: 0; left: 0; right: 0; padding: 7px 16mm;
             border-top: 1px solid #e8eaed; display: flex; justify-content: space-between;
             font-size: 9px; color: #aaa; background: white; }}
  .footer .brand {{ font-weight: 600; color: #1a1a1a; }}
</style></head>
<body>
<div class="header">
  <img src="data:image/png;base64,{LOGO_B64}">
  <div class="header-meta">
    <div class="company">{title}</div>
    <div class="period">{subtitle}</div>
  </div>
</div>
<div class="body">{body_html}</div>
<div class="footer">
  <span class="brand">Stamina</span>
  <span>{subtitle} · Prepared {today}</span>
</div>
</body></html>"""
    return WP_HTML(string=html).write_pdf()


# ── Pylon upload ──────────────────────────────────────────────────────────────

def upload_to_pylon(pdf_bytes: bytes, filename: str, pylon_account_id: str) -> str:
    resp = requests.post(
        f"{PYLON_BASE}/accounts/{pylon_account_id}/files",
        headers={"Authorization": f"Bearer {PYLON_KEY}"},
        files={"file": (filename, pdf_bytes, "application/pdf")},
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
        if not EXTERNAL_ONLY:
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
        if INTERNAL_ONLY:
            log(f"  Skipping external reports (--internal-only)")
        else:
            # Apply account filter if set
            external_data = accounts_data
            if ACCOUNT_FILTER:
                external_data = [d for d in accounts_data
                                 if d["customer"]["name"].lower() == ACCOUNT_FILTER.lower()]
                log(f"  Filtered to account: {ACCOUNT_FILTER} ({len(external_data)} match)")
            log(f"  Generating {len(external_data)} external reports...")

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
                futures = {executor.submit(process_external, d): d for d in external_data}
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
