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

sb     = create_client(SUPABASE_URL, SUPABASE_KEY)
openai = OpenAI(api_key=OPENAI_KEY)

# ── Logo ──────────────────────────────────────────────────────────────────────

LOGO_PATH = os.path.join(os.path.dirname(__file__), "logo.png")
with open(LOGO_PATH, "rb") as _f:
    LOGO_B64 = base64.b64encode(_f.read()).decode()

# ── Logging ───────────────────────────────────────────────────────────────────

def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

# ── System prompt ─────────────────────────────────────────────────────────────

PASS2_SYSTEM_PROMPT = """
You are the Stamina CS Intelligence agent generating a Pass 2 post-kickoff execution plan.

## Context
You receive:
  - The Pass 1 pre-kickoff OS (three pillars: Expectations, Metrics, Expansion)
  - The kickoff call transcript/summary

Your job is to produce TWO documents in one response:

---

## INTERNAL document (full content, tagged internal blocks included)

Resolve every `needs confirmation` item from Pass 1 using the kickoff transcript.
For each item, either mark it `confirmed` with the answer, or note it as still open.

Structure:
1. **Expectations Alignment — CONFIRMED**
   - Final confirmed ICP, verticals, personas, geography, buying triggers
   - Confirmed success definition with specific numbers
   - Any expectation misalignments flagged inline

2. **Measurement Contract — LOCKED**
   - Exact metrics agreed on kickoff call
   - "Working" threshold and "we need to talk" threshold per metric
   - Reporting cadence confirmed
   - Who on customer side receives reports

3. **Expansion Paths — RECORDED**
   - Which of the two hypotheses the customer engaged with
   - Forward commitment agreed (or declined/deferred): exact KPI → timeframe → lever
   - Levers that fired during the call (flag each with the customer quote that triggered it)

4. **Commercial Context** (<!-- INTERNAL ONLY --> ... <!-- END INTERNAL ONLY -->)
   - Plan signed, Term, Price paid, Promo applied
   - Renewal narrative: what the renewal conversation looks like given commercial terms
   - Customer bandwidth signals from the call (responsive vs slow, solo vs team)
   - Referral potential signals if any

5. **Execution Plan**
   Seven milestones with day-level scheduling based on kickoff specifics:
   - Milestone 1: Email Accounts + Deliverability Setup
   - Milestone 2: TAM Sourcing
   - Milestone 3: List Segmentation
   - Milestone 4: Campaign Strategy
   - Milestone 5: Campaign Messaging (flag approval-cycle risk based on customer bandwidth)
   - Milestone 6: Sending Strategy
   - Milestone 7: Launch (Day 15)
   - Email warmup: parallel track days 3–14

   For each milestone: owner (CSM / GTM Engineer / Customer), due date, customer actions required.
   Flag the highest-risk slip point for this specific customer.

6. **Pass 2 Summary**
   End with: "Pass 2 update: [resolved fields]. Levers fired: [list].
   Forward commitment: [proposed/agreed/declined/deferred]. Remaining open: [if any].
   Launch date: [Day 15 from today]. Highest-risk slip point: [milestone + reason]."

---

## EXTERNAL document (customer-safe — strip all internal blocks)

Same structure as internal EXCEPT:
  - Remove all <!-- INTERNAL ONLY --> ... <!-- END INTERNAL ONLY --> blocks entirely
  - Remove renewal narrative, commercial context, bandwidth signals
  - Keep execution plan, confirmed expectations, measurement contract, expansion paths
  - Tone: collaborative and forward-looking — this is what the customer receives

---

## Output format (REQUIRED — return valid JSON only)

{
  "internal_md": "<full internal document in markdown>",
  "external_md": "<customer-safe document in markdown, internal blocks removed>"
}

## Rules
- Two confidence states: confirmed / needs confirmation
- Never name a price in either document
- Stamina-controlled metrics only in measurement contract
- Source every confirmed claim: [kickoff call], [Pass 1], [sales call]
- Internal blocks use <!-- INTERNAL ONLY --> ... <!-- END INTERNAL ONLY --> markers
- Keep execution plan concrete: specific days, specific owners, specific customer actions
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

    payload = {
        "from":    RESEND_FROM,
        "to":      to_emails,
        "cc":      [AMARTYA_EMAIL],
        "bcc":     BCC_EMAILS,
        "reply_to": AMARTYA_EMAIL,
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
            content   = generate_pass2_content(customer, pass1_md, meeting, contacts)
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
