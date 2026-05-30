#!/usr/bin/env python3
"""
Stamina CS Intelligence — pass2_generator.py

Generates Pass 2 (post-kickoff execution plan) for accounts that:
  1. Have a real Pass 1 document in kickoff_documents
  2. Have a kickoff meeting recorded in Fathom (meeting_type = 'kickoff')
  3. Have no Pass 2 document yet

Two outputs per account:
  _internal PDF → emailed to CSM pair (Amartya CC, Arjun + Rehaan BCC)
  _external PDF → uploaded to Pylon account files

Usage:
  python3 pass2_generator.py             # live run
  python3 pass2_generator.py --dry-run   # print what would happen, no writes
"""

import base64
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests
from openai import OpenAI
from supabase import create_client

DRY_RUN = "--dry-run" in sys.argv

# ── Credentials ───────────────────────────────────────────────────────────────

SUPABASE_URL   = os.environ.get("SUPABASE_URL", "https://jgvyeavyffenvuhphejg.supabase.co")
SUPABASE_KEY   = os.environ.get("SUPABASE_KEY")
OPENAI_KEY     = os.environ.get("OPENAI_KEY")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
PYLON_KEY      = os.environ.get("PYLON_KEY", "pylon_api_85d658281b647d275a1b1e7dfc081e73de9ebfa9de87d563007eb3ab12251301")

PYLON_BASE   = "https://api.usepylon.com"
RESEND_FROM  = "Stamina <stamina@reports.stamina.io>"
AMARTYA_EMAIL = "amartya@stamina.io"
BCC_EMAILS    = ["arjun@stamina.io", "rehaan@stamina.io"]
TEST_EMAIL    = os.environ.get("TEST_EMAIL")  # If set, all emails go here only (no CC/BCC)

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
    for attempt in range(1, retries + 1):
        try:
            return fn()
        except Exception as e:
            if attempt == retries:
                raise
            log(f"  Retry {attempt}/{retries} for {label}: {e} — waiting {delay}s")
            time.sleep(delay)

# ── System prompt ─────────────────────────────────────────────────────────────

PASS2_SYSTEM_PROMPT = """
You are the Stamina CS Intelligence agent generating Pass 2 — the post-kickoff OS and execution plan.

## What Pass 2 is
Pass 2 is one document that does two things:
  1. Resolves every `needs confirmation` item from Pass 1 using the kickoff transcript
  2. Generates the full execution plan (7 milestones, day-level, customer-specific)

You produce TWO versions — internal and external — in one JSON response.

---

## INTERNAL version (full content, all internal blocks included)

### Section 1 — Expectations Alignment: CONFIRMED
Resolve all `needs confirmation` items from Pass 1 using the kickoff transcript.
  - Final confirmed ICP: verticals, personas, geography, buying triggers [kickoff call]
  - Confirmed success definition with specific numbers the customer named
  - Any expectation misalignments between Pass 1 assumptions and what customer said on call
  - Source every confirmed claim: [kickoff call], [Pass 1], [sales call]

### Section 2 — Measurement Contract: LOCKED
  - Exact Stamina-controlled metrics agreed on the call (emails sent, reply rate, positive reply rate,
    opportunities generated, cost per opportunity — only what Stamina controls)
  - "This is working" threshold per metric (the number the customer named)
  - "We need to talk" threshold per metric
  - Reporting cadence confirmed (weekly / biweekly / monthly)
  - Who on the customer side receives reports (name + email)
  - Never include customer-owned outcomes (meetings, pipeline, MRR) as committed metrics

### Section 3 — Expansion Paths: RECORDED
  - Which of the two Pass 1 hypotheses the customer engaged with on the call
  - Forward commitment status: proposed / agreed / declined / deferred
  - If agreed: exact format — "If we hit [KPI] by [date], we expand into [lever]"
  - All upsell levers that fired during the call, each with the customer quote that triggered it
  - Levers: Custom Personalization, Custom Signals, Higher Email Volume, Larger Contact Database,
    Credit Volume, Custom Services (CRM setup / CRM Sequences / Automations / Dial setup /
    Calls Intelligence), Whitelabel

### Section 4 — Commercial Context
<!-- INTERNAL ONLY -->
  - Plan signed, Term, Price paid, Promo applied (if any)
  - Renewal narrative: what the renewal conversation looks like given these commercial terms
  - Customer bandwidth signals: solo founder vs team, responsive vs slow approver
  - Referral potential: any signals the customer might refer others
  - Upsell signals beyond the two Pass 1 hypotheses — anything that fired unexpectedly
<!-- END INTERNAL ONLY -->

### Section 5 — Execution Plan (7 milestones, day-level)
Sequence all 7 milestones based on these customer-specific factors from the kickoff call:
  - Approval-cycle speed (solo founder = fast; committee = slow — adjust Milestone 5 buffer)
  - Stakeholder count (who needs to sign off on copy, sender identity, segments)
  - Bandwidth signals (how responsive/available the customer is)
  - Complexity of segments (simple single ICP vs multiple complex ICPs)

Pre-fill all decisions already locked on the call:
  - Sender identity (name and persona confirmed by customer)
  - ICP and target verticals/personas confirmed
  - Exclusion list (existing customers, competitors, prior outreach — confirmed on call)
  - CTA pattern (soft vs hard CTA based on customer's industry and cycle)
  - Reporting cadence and recipient confirmed

For each of the 7 milestones, provide:
  - Day range (e.g., Day 1–2)
  - Owner: CSM / GTM Engineer / Customer (or shared)
  - Specific deliverable
  - Customer action required (if any) with hard deadline
  - Any risk note for this customer [INTERNAL ONLY block if sensitive]

Milestones:
  1. Email Accounts + Deliverability Setup (lookalike domains, inboxes, DNS, 301 redirects, warmup start)
     - Confirm inbox count vs customer's opportunity volume target
     - Sender identity must be locked before domain purchase
  2. TAM Sourcing (pull raw lists per locked ICP — size to 30 days of sending volume)
     - Flag if Google Maps scrape needed for local/regional customers
  3. List Segmentation (split TAM, AI-qualify, apply exclusion list)
     - Customer must sign off on exclusion list explicitly — get confirmation in writing
  4. Campaign Strategy (pitch angle, CTA, A/B variants, sequence shape per segment)
     - Minimum 2 variants per segment for A/B testing
  5. Campaign Messaging (full copy drafted, sent for customer approval)
     - Flag approval-cycle risk: solo founder = 24h; committee = 3–4 days minimum
     - If bandwidth-constrained, propose a live 15-min review call instead of async
  6. Sending Strategy (daily volume per inbox, send windows by time zone, sequence cadence)
     - Default 25 emails/inbox/day
     - Send windows must match prospect's local business hours, not customer's
  7. Launch — Day 15 (first send the morning after warmup completes)
     - Bounce rate >2% in first 24h = pause immediately
     - Remind customer: positive replies hit their inbox within hours — 24–48h SLA

  Warmup: runs in parallel from Day 3 through Day 14 (non-negotiable, cannot be shortened)

### Section 6 — Pass 2 Closing
End with exactly:
"Execution plan locked. Launch day: [Day 15 date]. Customer-action items: [count].
Highest-risk slip point for this customer: [milestone name + specific reason based on kickoff signals]."

---

## EXTERNAL version (customer-safe — clean 2-week roadmap)
Strip ALL internal blocks (<!-- INTERNAL ONLY --> ... <!-- END INTERNAL ONLY -->) entirely.
The external version reads as a clean, collaborative 2-week roadmap showing:
  - Dates and deliverables per milestone
  - What the customer needs to do and when (their action items highlighted)
  - Confirmed expectations, measurement contract, expansion paths
  - Tone: forward-looking and professional — this is shared directly with the customer

---

## Output format — return valid JSON only
{
  "internal_md": "<full internal document in markdown>",
  "external_md": "<customer-safe 2-week roadmap in markdown>"
}

## Non-negotiable rules
- Never name a price in either version
- Stamina-controlled metrics only in the measurement contract
- Two confidence states: confirmed [source] or needs confirmation
- Internal blocks use <!-- INTERNAL ONLY --> ... <!-- END INTERNAL ONLY --> markers
- Never fabricate — if something wasn't confirmed on the call, mark it needs confirmation
- External version must never contain any internal block content whatsoever
"""

# ── Account detection ─────────────────────────────────────────────────────────

def find_accounts_needing_pass2() -> list:
    """
    Return customers that:
      - Have a real Pass 1 (not EXISTING_ACCOUNT_SKIP)
      - Have a kickoff meeting in Fathom
      - Have no Pass 2 yet
    """
    # Accounts with real Pass 1
    pass1_rows = (
        sb.table("kickoff_documents")
        .select("customer_id, content_md")
        .eq("pass_number", 1)
        .neq("content_md", "EXISTING_ACCOUNT_SKIP")
        .execute()
        .data
    )
    pass1_ids = {r["customer_id"] for r in pass1_rows}

    if not pass1_ids:
        return []

    # Accounts that already have Pass 2
    pass2_rows = (
        sb.table("kickoff_documents")
        .select("customer_id")
        .eq("pass_number", 2)
        .execute()
        .data
    )
    pass2_ids = {r["customer_id"] for r in pass2_rows}

    # Accounts with a kickoff meeting recorded
    kickoff_meetings = (
        sb.table("meetings")
        .select("customer_id")
        .eq("meeting_type", "kickoff")
        .execute()
        .data
    )
    has_kickoff = {r["customer_id"] for r in kickoff_meetings}

    # Need Pass 2 = has real Pass 1 + has kickoff meeting + no Pass 2 yet
    need_pass2 = pass1_ids & has_kickoff - pass2_ids

    if not need_pass2:
        return []

    customers = (
        sb.table("customers")
        .select("id, name, domain, tier, csm_owner, account_owner, brand_id, pylon_account_id")
        .in_("id", list(need_pass2))
        .eq("status", "active")
        .execute()
        .data
    )
    return customers


# ── Data fetching ─────────────────────────────────────────────────────────────

def fetch_pass1(customer_id: str) -> str:
    rows = (
        sb.table("kickoff_documents")
        .select("content_md")
        .eq("customer_id", customer_id)
        .eq("pass_number", 1)
        .execute()
        .data
    )
    return rows[0]["content_md"] if rows else ""


def fetch_kickoff_meeting(customer_id: str) -> dict:
    rows = (
        sb.table("meetings")
        .select("title, meeting_date, summary_text, attendees")
        .eq("customer_id", customer_id)
        .eq("meeting_type", "kickoff")
        .order("meeting_date", desc=False)
        .limit(1)
        .execute()
        .data
    )
    return rows[0] if rows else {}


def fetch_contacts(customer_id: str) -> list:
    return (
        sb.table("contacts")
        .select("name, email, role, is_primary")
        .eq("customer_id", customer_id)
        .limit(10)
        .execute()
        .data
    )


def find_csm_pair(customer: dict) -> dict:
    pairs = sb.table("csm_pairs").select("*").eq("is_active", True).execute().data
    for pair in pairs:
        ft = pair["filter_type"]
        fv = pair["filter_value"]
        if ft == "csm_owner" and customer.get("csm_owner") == fv:
            return pair
        if ft == "account_owner" and customer.get("account_owner") == fv:
            return pair
    return {}


# ── Prompt builder ────────────────────────────────────────────────────────────

def build_user_prompt(customer: dict, pass1_md: str, meeting: dict, contacts: list) -> str:
    contacts_text = "\n".join(
        f"  - {c.get('name')} ({c.get('role', 'N/A')}) — {c.get('email', '')}"
        + (" [primary]" if c.get("is_primary") else "")
        for c in contacts
    ) or "  No contacts on file"

    # Truncate transcript to ~8000 chars to stay well within context limits
    transcript = (meeting.get("summary_text") or "No kickoff transcript available.")[:8000]
    meeting_date = meeting.get("meeting_date", "Unknown")

    return f"""Generate Pass 2 for the following account.

## Customer
- Name: {customer.get('name')}
- Domain: {customer.get('domain', 'Unknown')}
- Tier: {customer.get('tier', 'Unknown')}
- CSM Owner: {customer.get('csm_owner', 'Unknown')}

## Contacts
{contacts_text}

## Pass 1 Document (pre-kickoff OS)
{pass1_md}

## Kickoff Call Transcript
Meeting date: {meeting_date}

{transcript}

---
Generate the Pass 2 JSON response now. Return ONLY valid JSON with internal_md and external_md keys.
"""


# ── GPT-4o generation ─────────────────────────────────────────────────────────

def generate_pass2_content(customer: dict, pass1_md: str, meeting: dict, contacts: list) -> dict:
    user_prompt = build_user_prompt(customer, pass1_md, meeting, contacts)

    response = openai.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": PASS2_SYSTEM_PROMPT},
            {"role": "user",   "content": user_prompt},
        ],
        temperature=0.3,
        max_tokens=6000,
        response_format={"type": "json_object"},
    )

    result = json.loads(response.choices[0].message.content)
    return {
        "internal_md": result.get("internal_md", ""),
        "external_md": result.get("external_md", ""),
    }


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
        elif line.startswith("- [ ] "):
            html_lines.append(f'<div class="checkbox">☐ {line[6:]}</div>')
        elif line.startswith("- "):
            html_lines.append(f'<div class="bullet">• {line[2:]}</div>')
        elif line.startswith("<!-- INTERNAL ONLY -->"):
            html_lines.append('<div class="internal-block"><div class="internal-tag">INTERNAL ONLY</div>')
        elif line.startswith("<!-- END INTERNAL ONLY -->"):
            html_lines.append('</div>')
        elif line.strip() == "":
            html_lines.append('<div class="spacer"></div>')
        else:
            line = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", line)
            line = re.sub(r"`(.+?)`", r'<code>\1</code>', line)
            html_lines.append(f'<p>{line}</p>')
    return "\n".join(html_lines)


def generate_pdf(content_md: str, customer_name: str, is_internal: bool) -> bytes:
    from weasyprint import HTML as WP_HTML

    body_html = md_to_html_body(content_md)
    today     = datetime.now().strftime("%B %d, %Y")
    doc_label = "Internal — Execution Plan" if is_internal else "Client Execution Plan"
    banner    = '<div class="internal-banner">⚠ INTERNAL ONLY — DO NOT SHARE WITH CLIENT</div>' if is_internal else ""

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  @page {{ size: A4; margin: 0; }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: "Helvetica Neue", Arial, sans-serif; background: white; color: #1a1a1a; font-size: 13px; line-height: 1.6; }}
  .header {{ background: #1a2035; padding: 32px 48px; display: flex; align-items: center; justify-content: space-between; }}
  .header img {{ height: 26px; filter: brightness(0) invert(1); }}
  .header-right {{ text-align: right; }}
  .header-right .label {{ color: #8892a4; font-size: 10px; letter-spacing: 1.5px; text-transform: uppercase; }}
  .header-right .title {{ color: white; font-size: 16px; font-weight: 700; margin-top: 4px; }}
  .header-right .sub {{ color: #8892a4; font-size: 11px; margin-top: 2px; }}
  .internal-banner {{ background: #b91c1c; color: white; text-align: center; font-size: 10px; font-weight: 700; letter-spacing: 2px; text-transform: uppercase; padding: 5px; }}
  .body {{ padding: 36px 48px 80px; }}
  h1 {{ font-size: 18px; font-weight: 700; color: #1a2035; margin: 24px 0 10px; border-bottom: 2px solid #1a2035; padding-bottom: 6px; }}
  h2 {{ font-size: 15px; font-weight: 700; color: #1a2035; margin: 20px 0 8px; }}
  h3 {{ font-size: 13px; font-weight: 700; color: #444; margin: 16px 0 6px; text-transform: uppercase; letter-spacing: 0.5px; }}
  p {{ margin-bottom: 8px; color: #333; }}
  .bullet {{ margin: 3px 0 3px 16px; color: #333; }}
  .spacer {{ height: 8px; }}
  .checkbox {{ margin: 4px 0 4px 16px; color: #333; }}
  code {{ background: #f4f4f4; padding: 1px 4px; border-radius: 3px; font-size: 12px; font-family: monospace; }}
  .internal-block {{ background: #fff8f8; border: 1px solid #fecaca; border-radius: 6px; padding: 12px 16px; margin: 16px 0; }}
  .internal-tag {{ font-size: 9px; font-weight: 700; letter-spacing: 1.5px; text-transform: uppercase; color: #dc2626; margin-bottom: 6px; }}
  .footer {{ position: fixed; bottom: 0; left: 0; right: 0; padding: 10px 48px; border-top: 1px solid #e8eaed; display: flex; justify-content: space-between; font-size: 10px; color: #aaa; background: white; }}
</style>
</head>
<body>
<div class="header">
  <img src="data:image/png;base64,{LOGO_B64}">
  <div class="header-right">
    <div class="label">Pass 2 — Post-Kickoff</div>
    <div class="title">{doc_label}</div>
    <div class="sub">{customer_name} · {today}</div>
  </div>
</div>
{banner}
<div class="body">{body_html}</div>
<div class="footer">
  <span>Stamina CS Intelligence · {today}</span>
  <span>{'INTERNAL' if is_internal else 'CONFIDENTIAL'} — {customer_name}</span>
</div>
</body>
</html>"""

    return WP_HTML(string=html).write_pdf()


# ── Pylon upload ──────────────────────────────────────────────────────────────

def upload_to_pylon(pdf_bytes: bytes, filename: str, pylon_account_id: str) -> str:
    """Upload external PDF to Pylon account files. Returns the file URL."""
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

def send_email(pair: dict, pdf_bytes: bytes, customer_name: str):
    to_emails = pair.get("report_email") or pair.get("csm_emails") or []
    if not to_emails:
        log("  No emails found for pair — skipping send")
        return

    filename = f"{customer_name.replace(' ', '_')}_Pass2_ExecutionPlan_Internal.pdf"

    # Test mode: override all recipients
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
        "from":    RESEND_FROM,
        "to":      to_emails,
        "cc":      cc_list,
        "bcc":     bcc_list,
        "reply_to": reply_to,
        "template": {
            "id":        "execution-plan",
            "variables": {},
        },
        "attachments": [
            {
                "filename": filename,
                "content":  base64.b64encode(pdf_bytes).decode(),
            }
        ],
    }

    resp = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type":  "application/json",
        },
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    log(f"  Email sent → {to_emails} (ID: {resp.json().get('id')})")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log(f"Pass 2 generator started {'[DRY RUN] ' if DRY_RUN else ''}...")

    accounts = find_accounts_needing_pass2()
    log(f"  {len(accounts)} accounts need Pass 2")

    if not accounts:
        log("Nothing to do.")
        return

    success, errors = 0, 0

    for customer in accounts:
        name = customer.get("name", "Unknown")
        log(f"  Processing: {name}")

        try:
            pass1_md  = fetch_pass1(customer["id"])
            meeting   = fetch_kickoff_meeting(customer["id"])
            contacts  = fetch_contacts(customer["id"])
            pair      = find_csm_pair(customer)

            if not pair:
                log(f"    No CSM pair found for {name} — skipping")
                continue

            if not meeting:
                log(f"    No kickoff meeting found for {name} — skipping")
                continue

            log(f"    Generating Pass 2 via GPT-4o...")
            content = with_retry(
                lambda: generate_pass2_content(customer, pass1_md, meeting, contacts),
                retries=3, delay=10, label=f"Pass 2 {name}"
            )
            internal_md = content["internal_md"]
            external_md = content["external_md"]

            if DRY_RUN:
                log(f"    [DRY RUN] internal={len(internal_md)} chars, external={len(external_md)} chars")
                log(f"    [DRY RUN] Would email to {pair.get('report_email')} and upload to Pylon")
                success += 1
                continue

            # Store combined content in kickoff_documents
            sb.table("kickoff_documents").upsert({
                "customer_id":  customer["id"],
                "brand_id":     customer.get("brand_id"),
                "pass_number":  2,
                "content_md":   internal_md,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }, on_conflict="customer_id,pass_number").execute()

            # Generate PDFs
            internal_pdf = generate_pdf(internal_md, name, is_internal=True)
            external_pdf = generate_pdf(external_md, name, is_internal=False)

            # Email internal PDF to CSM pair
            send_email(pair, internal_pdf, name)

            # Upload external PDF to Pylon
            pylon_id = customer.get("pylon_account_id")
            if pylon_id:
                today_str = datetime.now().strftime("%B %d, %Y")
                filename  = f"{name} Execution Plan — {today_str}.pdf"
                url = upload_to_pylon(external_pdf, filename, pylon_id)
                log(f"    External PDF uploaded to Pylon: {url[:60]}...")
            else:
                log(f"    No pylon_account_id for {name} — skipping Pylon upload")

            log(f"    ✓ Pass 2 complete for {name}")
            success += 1

        except Exception as e:
            log(f"    ERROR for {name}: {e}")
            errors += 1

    log(f"Done. success={success}, errors={errors}")


if __name__ == "__main__":
    main()
