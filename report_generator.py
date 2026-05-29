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

This report covers ALL accounts owned by this pair in one document (5–7 pages maximum, never more than 7).
It is INTERNAL ONLY — the CSM pair reads this before their Monday sprint. Never share with clients.

## Structure — same every {period}, no exceptions

### Header section
- Total accounts: [count] | Accounts needing attention: [count] | Upsell opportunities: [count]
- Overall pair performance vs prior {period}: [summary in 1 line]

### Per-account sections (one block per account, in priority order — worst performing first)

For EACH account, write a concise block covering:

**[Account Name]** | Tier: [tier] | Health: [score] | Inboxes: [active]/[total]

Performance vs {prior_period_label} (and vs measurement contract threshold where available):
- Key metrics: emails sent, reply rate, positive reply rate, opportunities, cost per opp
- Flag ✗ if below threshold, ✓ if above — bold any metric off-track for 2+ consecutive {period}s

What happened:
- Meetings this {period}: [type + date + 1-line summary]
- Slack activity: [key customer messages — requests, concerns, or positive signals]
- Notable replies: [any positive replies that came in]

Attention flags:
- [Any open issues, onboarding risks, missed SLAs, disconnected inboxes]
- [Accounts onboarded in last 2–7 days with 0 active inboxes OR 0 meetings — flag as onboarding risk]

[internal review only] block for each account:
- Renewal context if relevant (renewal window, promo expiry)
- Upsell signal: which lever, what data triggered it, forward commitment progress
- Customer bandwidth read: responsive vs slow, any red flags

### Closing section
- Top 3 priorities for the pair this {period}
- Accounts to escalate to Amartya (if any)
- Upsell conversations to have this {period} (lever + account + why now)

## Rules
- Aggressive, direct tone — surface problems clearly, don't soften
- [internal review only] blocks: content for CSM pair eyes only, never goes to client
- Never name a price
- Flag unreplied positive leads — name the account, the lead, and days elapsed
- Onboarding risk = account added to Pylon 2–7 days ago + (0 active inboxes OR 0 meetings)
- Maximum 7 pages — be concise. Cut filler. Every sentence must earn its place.
"""

EXTERNAL_REPORT_PROMPT = """
You are the Stamina CS Intelligence agent generating an external {period} report for one client.

This report is shared directly with the client. Professional, client-facing tone throughout.
Six fixed sections, same structure every {period}. Maximum 2 pages when rendered.

## The six sections (same every {period}, no exceptions)

### 1. Performance Metrics
Stamina-controlled metrics only (never meetings booked, pipeline, or revenue — those are client-owned):
- Emails sent | Deliverability rate / bounce rate | Open rate | Reply rate
- Positive reply rate | Opportunities generated | Cost per opportunity (cumulative)

Format each as: This {period}: [value] | Prior {period}: [value] | Change: [±%] | Threshold: [value] [✓/✗]
Lead with the metric the client cares most about (from their measurement contract if available).

### 2. Audience Visibility
- Unique contacts emailed this {period}, broken down by segment
- Reply sentiment: positive / neutral / negative / OOO / unsubscribe counts
- Named list of companies with positive replies (company name + contact name/title)
- Any anomalies (bounce spike, deliverability issue, segment that generated 0 replies)

### 3. What's Working — Performance Insights
Specific and actionable only. No vague observations.
- Which subject lines drove highest open rates (name the variant, name the %s)
- Which message variants drove highest positive reply rates
- Which segments outperformed others and by how much
- Which sequence touch converted best (touch 1 vs follow-up 1 vs follow-up 2)
- One concrete recommendation for next {period} based on the data

### 4. Business Intelligence — What the Data Means Beyond Outbound
Four sub-sections as callout blocks:
4.1 Positioning signal — what A/B performance reveals about which value props land. Tie winning
    outbound language directly to the client's existing homepage copy. Recommend a concrete test.
4.2 Where the real ICP is — what segment performance reveals about true audience fit vs kickoff assumption.
    Surface patterns that transcend industry if data supports it.
4.3 Objection analysis — cluster negative/neutral replies into 2–3 patterns. For each, name what it
    reveals about a positioning gap the client should address.
4.4 Cross-channel transfer — 2–3 concrete actions for homepage, ads, case studies, or sales calls
    based on this {period}'s outbound learnings. Use a table format.

### 5. Reply Coaching
Review every positive reply and the client's response from the Unibox.

Volume rules:
- ≤5 positive replies → per-reply coaching blocks
- ≥6 positive replies → pattern-level coaching (2–3 patterns with named examples)

Each block contains:
- The prospect's reply (or pattern with 2–3 examples)
- The client's response
- What worked
- What to change — aggressive, specific next-step recommendations
- What this prospect's reply suggests about their state

Coaching tone: aggressive, not polite. Soft replies that don't drive a next step are failure modes.
Always push: call within 2 hours, SMS layer, multi-touch follow-up, qualify before sending materials.

Flag unreplied positive leads by name, time elapsed, and recovery move recommended.

### 6. Next {Period_cap} — Priorities and Anomalies
- Top 1–2 priorities (from insights in section 3)
- Any anomalies needing client attention
- One decision question for the client (only if there's a real decision — don't force it)

## Rules
- Never include internal commentary, renewal context, pricing, or upsell signals
- Never name a price
- Stamina-controlled metrics only in section 1
- Insights must be specific: name variants, name %s, name segments
- Maximum 2 pages — every word must earn its place
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

    system = EXTERNAL_REPORT_PROMPT.format(
        period=period_label,
        Period_cap=period_label.capitalize(),
    )

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
