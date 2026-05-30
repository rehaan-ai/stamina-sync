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

Pass 2 is one document that does two things:
1. Resolves every `needs confirmation` item from Pass 1 using the kickoff transcript
2. Generates the full 14-day execution plan (7 milestones, day-level, customer-specific)

You produce TWO versions in one JSON response.
Internal version: 2 pages ideal, 3 pages maximum.
External version: 1–2 pages. Customer-safe roadmap only.

---

## INTERNAL VERSION — full content, all internal blocks included

### Section 1 — Expectations Alignment: CONFIRMED
Resolve every needs-confirmation item from Pass 1 using the kickoff transcript.
- Final confirmed ICP: verticals, personas, geography, buying triggers [kickoff call]
- Confirmed success definition with the specific numbers the customer named
- Any expectation misalignments between Pass 1 assumptions and what customer said on call — flag inline
- Source every confirmed claim: [kickoff call] [Pass 1] [sales call]
- Items still unresolved → keep as needs confirmation checkbox with the verbatim CSM question

### Section 2 — Measurement Contract: LOCKED
- Exact Stamina-controlled metrics agreed on the call:
  emails sent/month | deliverability rate / bounce rate | open rate | reply rate |
  positive reply rate | opportunities generated/month | cost per opportunity
- "This is working" threshold per metric (the specific number the customer named)
- "We need to talk" threshold per metric
- Reporting cadence confirmed: weekly / biweekly / monthly
- Who receives reports: name + email confirmed on call
- Never include customer-owned outcomes: meetings booked, pipeline, MRR, closed-won revenue

### Section 3 — Expansion Paths: RECORDED
- Which of the two Pass 1 hypotheses the customer engaged with on the call
- Forward commitment status: proposed / agreed / declined / deferred
- If agreed: exact format — "If we hit [KPI] by [date], we expand into [lever]"
- Every upsell lever that fired during the call + the customer quote that triggered it
- Levers: Custom Personalization | Custom Signals | Higher Email Volume | Larger Contact Database |
  Credit Volume | Custom Services (CRM setup / CRM Sequences / Automations / Dial setup / Calls Intelligence) | Whitelabel

### Section 4 — Commercial Context
<!-- INTERNAL ONLY -->
- Plan signed, Term, Price paid, Promo applied (if any)
- Renewal narrative: what the renewal conversation looks like given these commercial terms
- Customer bandwidth signals: solo founder vs team, responsive vs slow approver — implications for execution
- Referral potential: any signals the customer might refer others
- Unexpected upsell signals from the call beyond the two Pass 1 hypotheses
<!-- END INTERNAL ONLY -->

### Section 5 — Execution Plan: 7 Milestones, Day-Level
Sequence all 7 milestones based on customer-specific factors from the kickoff call:
- Approval-cycle speed (solo founder = fast, committee = slow — adjust Milestone 5 accordingly)
- Stakeholder count (who needs to sign off)
- Bandwidth signals (how available/responsive the customer is)
- Segment complexity (simple single ICP vs multiple ICPs)

Pre-fill every decision already locked on the call:
sender identity | ICP and verticals | exclusion list | CTA pattern | reporting cadence and recipient

For each milestone include: day range, owner (CSM / GTM Engineer / Customer), deliverable, customer action + deadline, and any risk note specific to this customer.

Milestones:
1. Email Accounts + Deliverability Setup (Day 1–2) — lookalike domains, inboxes, DNS, redirects, warmup start
   - Sender identity must be locked before domain purchase — customer action Day 2
   - Inbox count should match opportunity volume target; flag if customer wants fewer and explain the math
2. TAM Sourcing (Day 3–4) — pull raw prospect lists per locked ICP, sized for 30 days of sending
   - Flag if Google Maps scrape needed for local/regional ICP
3. List Segmentation (Day 5–6) — split TAM, AI-qualify, apply exclusion list
   - Customer must sign off on exclusion list in writing by Day 6 — brand safety risk
4. Campaign Strategy (Day 7–8) — pitch angle, CTA, A/B variant design, sequence shape
   - Minimum 2 variants per segment; soft CTAs for long-cycle industries
5. Campaign Messaging (Day 9–11) — full copy drafted, sent for customer approval
   <!-- INTERNAL ONLY -->Approval-cycle risk: solo founder = 24h turnaround, committee = 3–4 days minimum. If bandwidth-constrained, propose a live 15-min review call instead of async.<!-- END INTERNAL ONLY -->
   - Customer approves copy by Day 11 — hard block on Milestone 6
6. Sending Strategy (Day 12–13) — daily volume per inbox (default 25), send windows matched to prospect TZ, sequence cadence
   - Send windows must match prospect's local business hours, not the customer's
7. Launch — Day 15 — first send the morning after warmup completes
   - Bounce rate >2% in first 24h: pause and investigate, do not push through
   - Remind customer: positive replies start flowing within hours — 24–48h response SLA starts now

Warmup: runs in parallel Day 3 through Day 14. Non-negotiable — cannot be shortened.

### Section 6 — Pass 2 Closing
End with exactly:
"Execution plan locked. Launch day: [Day 15 date]. Customer-action items: [count].
Highest-risk slip point for this customer: [milestone name + specific reason from kickoff signals]."

---

## EXTERNAL VERSION — customer-safe, internal blocks fully stripped

Strip ALL <!-- INTERNAL ONLY --> ... <!-- END INTERNAL ONLY --> blocks completely.

The external version reads as a clean, collaborative 2-week plan:
- Opening: 2–3 sentences confirming what was agreed on the kickoff call (expectations + measurement contract)
- 7-milestone roadmap: date range, deliverable, what the customer needs to do and when
- Tone: forward-looking and professional — this goes directly to the customer
- Close with: "Launch is Day 15. Any questions before then? Reply to this email."
- 1–2 pages maximum

---

## JSON output format — return valid JSON only
{"internal_md": "<full internal document in markdown>", "external_md": "<customer-safe roadmap in markdown>"}

## Non-negotiable rules
1. Never name a price in either version
2. Stamina-controlled metrics only in measurement contract
3. confirmed [source] or needs confirmation — no other confidence states
4. Internal blocks: <!-- INTERNAL ONLY --> ... <!-- END INTERNAL ONLY -->
5. Never fabricate — unconfirmed items stay as needs confirmation
6. External version must never contain any internal block content whatsoever
7. Internal: 2 pages ideal, 3 pages maximum. External: 1–2 pages maximum.
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

def md_inline(text: str) -> str:
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"`(.+?)`", r"<code>\1</code>", text)
    return text


def md_to_html_body(md: str) -> str:
    lines = md.split("\n")
    html_lines = []
    for line in lines:
        if line.startswith("# "):
            html_lines.append(f'<h1>{md_inline(line[2:])}</h1>')
        elif line.startswith("## "):
            html_lines.append(f'<h2>{md_inline(line[3:])}</h2>')
        elif line.startswith("### "):
            html_lines.append(f'<h3>{md_inline(line[4:])}</h3>')
        elif line.startswith("- [ ] "):
            html_lines.append(f'<div class="checkbox">☐ {md_inline(line[6:])}</div>')
        elif line.startswith("- [x] ") or line.startswith("- [X] "):
            html_lines.append(f'<div class="checkbox checked">☑ {md_inline(line[6:])}</div>')
        elif line.startswith("- "):
            html_lines.append(f'<div class="bullet">• {md_inline(line[2:])}</div>')
        elif line.startswith("<!-- INTERNAL ONLY -->"):
            html_lines.append('<div class="internal-block"><div class="internal-tag">INTERNAL ONLY</div>')
        elif line.startswith("<!-- END INTERNAL ONLY -->"):
            html_lines.append('</div>')
        elif line.strip() == "":
            html_lines.append('<div class="spacer"></div>')
        else:
            html_lines.append(f'<p>{md_inline(line)}</p>')
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
  .body {{ padding: 18px 0 60px; }}
  h1 {{ font-size: 14px; font-weight: 700; color: #1a2035; margin: 18px 0 7px;
        border-bottom: 2px solid #1a2035; padding-bottom: 4px; page-break-after: avoid; }}
  h2 {{ font-size: 12px; font-weight: 700; color: #1a2035; margin: 14px 0 5px;
        page-break-after: avoid; }}
  h3 {{ font-size: 10.5px; font-weight: 700; color: #555; margin: 10px 0 4px;
        text-transform: uppercase; letter-spacing: 0.5px; page-break-after: avoid; }}
  p {{ margin-bottom: 5px; color: #333; page-break-inside: avoid; }}
  .bullet {{ margin: 2px 0 2px 14px; color: #333; page-break-inside: avoid; }}
  .spacer {{ height: 5px; }}
  .checkbox {{ margin: 3px 0 3px 14px; color: #333; page-break-inside: avoid; }}
  code {{ background: #f4f4f4; padding: 1px 4px; border-radius: 3px; font-size: 10px; font-family: monospace; }}
  .internal-block {{ background: #fff8f8; border: 1px solid #fecaca; border-radius: 5px;
                     padding: 10px 14px; margin: 10px 0; page-break-inside: avoid; }}
  .internal-tag {{ font-size: 8px; font-weight: 700; letter-spacing: 1.5px;
                   text-transform: uppercase; color: #dc2626; margin-bottom: 5px; }}
  .footer {{ position: fixed; bottom: 0; left: 0; right: 0; padding: 7px 36px;
             border-top: 1px solid #e8eaed; display: flex; justify-content: space-between;
             font-size: 9px; color: #bbb; background: white; }}
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
        "from":     RESEND_FROM,
        "to":       to_emails,
        "reply_to": reply_to,
        "template": {"id": "execution-plan", "variables": {"newaccountpylon": customer_name}},
        "attachments": [{"filename": filename,
                          "content": base64.b64encode(pdf_bytes).decode()}],
    }
    if cc_list:
        payload["cc"] = cc_list
    if bcc_list:
        payload["bcc"] = bcc_list

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
