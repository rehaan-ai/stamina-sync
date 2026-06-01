#!/usr/bin/env python3
"""
Stamina CS Intelligence — pass1_generator.py

Generates Pass 1 (pre-kickoff OS) for every new customer that doesn't
have one yet. Runs after sync.py detects a new account.

Inputs per account:
  - closing_calls  → sales call transcript + summary
  - customers      → name, domain, tier, csm_owner, account_owner
  - contacts       → primary contacts
  - website        → live scrape of customer domain

Output:
  - kickoff_documents row (pass_number=1, content_md)
  - PDF emailed to CSM pair via Resend (prekickoff-context template)
  - Amartya CC'd on every email

Usage:
  python3 pass1_generator.py             # live run
  python3 pass1_generator.py --dry-run   # print what would happen, no writes
"""

import base64
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests
import anthropic
from supabase import create_client

DRY_RUN = "--dry-run" in sys.argv

# ── Credentials ───────────────────────────────────────────────────────────────

SUPABASE_URL   = os.environ.get("SUPABASE_URL", "https://jgvyeavyffenvuhphejg.supabase.co")
SUPABASE_KEY   = os.environ.get("SUPABASE_KEY")
ANTHROPIC_KEY  = os.environ.get("ANTHROPIC_KEY")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY")

sb     = create_client(SUPABASE_URL, SUPABASE_KEY)
claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

RESEND_FROM    = "Stamina <stamina@reports.stamina.io>"
AMARTYA_EMAIL  = "amartya@stamina.io"
TEST_EMAIL     = os.environ.get("TEST_EMAIL")  # If set, all emails go here only (no CC/BCC)

# ── Logo (base64 embedded) ────────────────────────────────────────────────────

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

# ── OS System Prompt ──────────────────────────────────────────────────────────

PASS1_SYSTEM_PROMPT = """
You are the Stamina CS Intelligence agent generating a Pass 1 pre-kickoff OS for a new customer.

INTERNAL ONLY. The CSM uses it to run the kickoff call. Never share with the customer.
Target: 2 pages rendered. Maximum 3 pages. Be detailed but efficient — no filler sentences.

---

## Three-pillar structure — use exactly, in this order

### 1. Expectations Alignment
Compressed business overview: 1–2 short paragraphs covering who they sell to and how they currently acquire.
Then a tight bulleted ICP/targeting block:
- Verticals
- Personas (title, seniority, function)
- Geography
- Key buying triggers
- Disqualifiers if mentioned

For anything not confirmed: generate a customer-facing expectation-quantification question — these are the exact words the CSM reads verbatim on the call. Expectation-quantification questions only — they force numbers on what success looks like:
- "How many qualified opportunities per month would make this clearly worth it for you?"
- "What does 'this is working' look like at end of quarter — in numbers?"
- "What's the smallest result that would justify renewing? The largest you'd realistically expect?"
- "If we deliver X opportunities/month, what does that translate to in revenue or pipeline for you?"

Use checkboxes (- [ ]) for every needs-confirmation item.

### 2. Key Metrics To Track — Measurement Contract
Define exactly what Stamina commits to reporting on. Stamina-controlled metrics only:
- Emails sent per month
- Inbox deliverability rate / bounce rate
- Open rate
- Reply rate
- Positive reply rate (auto-categorized intent replies)
- Opportunities generated per month (positive replies meeting the customer's stated qualification bar)
- Cost per opportunity (Stamina spend divided by opportunities)

Never commit to customer-owned outcomes: meetings booked, pipeline generated, closed-won revenue, MRR.
If customer says "I'll judge you on revenue" — redirect: Stamina commits to opportunity generation; customer owns conversion.

Standard proposal when customer has no preference: emails sent/month, reply rate, positive reply rate, opportunities/month.
Cadence default: biweekly. Weekly during month 1 if pilot/POC. Monthly only if sales cycle 3+ months AND customer prefers.

Measurement contract questions for the CSM:
- "Of these metrics — positive reply rate, opportunities/month — which matters most for how you'll evaluate us?"
- "What's the minimum opportunities/month that would make you say 'this is working'?"
- "What's the threshold where we'd need to have a strategy conversation?"
- "Reporting cadence: biweekly is default — does that work, or do you want weekly in month 1?"
- "Who else on your side should receive reports?"

Use checkboxes for anything not yet confirmed.

### 3. How the Customer Can Expand with Stamina
Exactly two hypotheses. Hard limit — never more, never fewer.
Format: [Vector → Lever] for each.

Selection rules:
- One near-term lever: likely adoption within 60–90 days based on what they already do
- One stretch lever: tied to their stated ambition, requires proving value first
- Never stack two hypotheses against the same lever
- If the sales call surfaced an explicit expansion signal, it becomes one of the two automatically

For each hypothesis write:
- The [Vector → Lever] label
- 2–3 sentences on why this hypothesis fits this specific customer based on what they said/showed

<!-- INTERNAL ONLY -->
Rationale for selection: explain precisely why you chose these two over alternatives. Not for customer eyes.
<!-- END INTERNAL ONLY -->

Suggested forward commitment for the SM to propose at end of kickoff:
Format exactly: "If we hit [KPI] in [60/90 days], can we plan to expand into [lever] in month [X]?"
Use the near-term hypothesis. The stretch is held for the renewal-cycle conversation.

---

## Commercial Context — always include, always internal-only tagged

<!-- INTERNAL ONLY -->
- Plan signed: [Base / Custom]
- Term: [Monthly / Quarterly / Custom]
- Price paid: [actual amount — note if discounted from standard]
- Promo applied: [yes/no — code and source if yes]
- Renewal pricing default: standard (no promo carries forward unless explicitly committed in writing)
- Renewal narrative: what the renewal conversation looks like given these commercial terms. If they signed at a discount, name the dynamic directly.
<!-- END INTERNAL ONLY -->

---

## Closing line — always end Pass 1 with exactly this format
"Pass 1 coverage: Expectations X% · Metrics X% · Expansion X%.
Kickoff call should focus on: [sections with most needs-confirmation items].
Suggested forward commitment: [the exact proposal]."

---

## Non-negotiable rules
1. Two confidence states only: confirmed [source] or needs confirmation
2. Open items are customer-facing questions the CSM reads verbatim — never internal field labels
3. Cite every confirmed claim inline: [sales call] [website] [CRM]
4. Never fabricate — missing data becomes a needs-confirmation question
5. Never name a price for upsells
6. Internal-only blocks: <!-- INTERNAL ONLY --> ... <!-- END INTERNAL ONLY -->
7. Execution details (domain counts, sender names, inbox volumes) belong in the execution plan, not here
8. Output: 2 pages ideal, 3 pages maximum

## Upsell levers
Custom Personalization | Custom Signals | Higher Email Volume | Larger Contact Database |
Credit Volume | Custom Services (CRM setup / CRM Sequences / Automations / Dial setup / Calls Intelligence) | Whitelabel
"""

# ── Data fetching ─────────────────────────────────────────────────────────────

def find_new_accounts() -> list:
    """Return customers that don't have a Pass 1 document yet."""
    existing = sb.table("kickoff_documents").select("customer_id").eq("pass_number", 1).execute().data
    existing_ids = {r["customer_id"] for r in existing}

    all_customers = sb.table("customers").select(
        "id, name, domain, tier, csm_owner, account_owner, brand_id, custom_fields"
    ).eq("status", "active").execute().data

    return [c for c in all_customers if c["id"] not in existing_ids]


def fetch_closing_call(customer_id: str) -> dict:
    """Get the most recent sales call transcript for this customer."""
    rows = (
        sb.table("closing_calls")
        .select("transcript_text, ai_summary, ae_name, call_date")
        .eq("customer_id", customer_id)
        .order("call_date", desc=True)
        .limit(1)
        .execute()
        .data
    )
    return rows[0] if rows else {}


def fetch_contacts(customer_id: str) -> list:
    """Get primary contacts for this customer."""
    rows = (
        sb.table("contacts")
        .select("name, email, role, is_primary")
        .eq("customer_id", customer_id)
        .limit(10)
        .execute()
        .data
    )
    return rows


def scrape_website(domain: str) -> str:
    """Scrape the customer's homepage for positioning context."""
    if not domain:
        return "Website not available."
    try:
        url = f"https://{domain}" if not domain.startswith("http") else domain
        resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        # Strip HTML tags, collapse whitespace, truncate to 3000 chars
        text = re.sub(r"<[^>]+>", " ", resp.text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:3000]
    except Exception as e:
        return f"Website scrape failed: {e}"


def find_csm_pair(customer: dict) -> dict:
    """Find the csm_pairs row that owns this customer."""
    csm_owner    = customer.get("csm_owner")
    account_owner = customer.get("account_owner")

    pairs = sb.table("csm_pairs").select("*").eq("is_active", True).execute().data

    for pair in pairs:
        ft = pair["filter_type"]
        fv = pair["filter_value"]
        if ft == "csm_owner" and csm_owner == fv:
            return pair
        if ft == "account_owner" and account_owner == fv:
            return pair

    return {}


# ── Prompt builder ────────────────────────────────────────────────────────────

def build_user_prompt(customer: dict, closing_call: dict, contacts: list, website: str) -> str:
    contacts_text = "\n".join(
        f"  - {c.get('name', 'Unknown')} ({c.get('role', 'N/A')}) — {c.get('email', 'N/A')}"
        + (" [primary]" if c.get("is_primary") else "")
        for c in contacts
    ) or "  No contacts on file"

    transcript = closing_call.get("transcript_text") or closing_call.get("ai_summary") or "No sales call transcript available."
    ae_name    = closing_call.get("ae_name", "Unknown AE")
    call_date  = closing_call.get("call_date", "Unknown date")

    return f"""Generate Pass 1 for the following new customer.

## Customer
- Name: {customer.get('name')}
- Domain: {customer.get('domain', 'Unknown')}
- Tier: {customer.get('tier', 'Unknown')}
- CSM Owner: {customer.get('csm_owner', 'Unknown')}
- Account Owner: {customer.get('account_owner', 'Unknown')}

## Contacts
{contacts_text}

## Sales Call Transcript
AE: {ae_name} | Date: {call_date}

{transcript}

## Customer Website (live scrape)
{website}

---
Generate the full Pass 1 OS document now. Follow all rules in your instructions exactly.
"""


# ── GPT-4o generation ─────────────────────────────────────────────────────────

def generate_pass1_content(customer: dict, closing_call: dict, contacts: list, website: str) -> str:
    user_prompt = build_user_prompt(customer, closing_call, contacts, website)

    response = claude.messages.create(
        model="claude-sonnet-4-5-20250929",
        system=PASS1_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
        temperature=1,
        max_tokens=4000,
    )
    return response.content[0].text


# ── PDF generation ────────────────────────────────────────────────────────────

def md_inline(text: str) -> str:
    """Apply inline markdown (bold, code) to any text fragment."""
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"`(.+?)`", r"<code>\1</code>", text)
    return text


def md_to_html_body(md: str) -> str:
    """Convert markdown to HTML for the PDF body."""
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


def generate_pdf(content_md: str, customer_name: str, doc_type: str = "Pass 1 — Pre-Kickoff OS") -> bytes:
    from weasyprint import HTML as WP_HTML

    body_html = md_to_html_body(content_md)
    today     = datetime.now().strftime("%B %d, %Y")

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

  /* ── Header (first page only, fixed height) ── */
  .header {{ background: #1a2035; padding: 22px 36px; display: flex; align-items: center;
             justify-content: space-between; }}
  .header img {{ height: 22px; filter: brightness(0) invert(1); }}
  .header-right {{ text-align: right; }}
  .header-right .label {{ color: #8892a4; font-size: 9px; letter-spacing: 1.5px; text-transform: uppercase; }}
  .header-right .title {{ color: white; font-size: 14px; font-weight: 700; margin-top: 3px; }}
  .header-right .sub {{ color: #8892a4; font-size: 10px; margin-top: 2px; }}
  .internal-banner {{ background: #b91c1c; color: white; text-align: center; font-size: 9px;
                      font-weight: 700; letter-spacing: 2px; text-transform: uppercase; padding: 4px; }}

  /* ── Body ── */
  .body {{ padding: 18px 0 60px; }}

  /* ── Typography ── */
  h1 {{ font-size: 14px; font-weight: 700; color: #1a2035; margin: 18px 0 7px;
        border-bottom: 2px solid #1a2035; padding-bottom: 4px;
        page-break-after: avoid; }}
  h2 {{ font-size: 12px; font-weight: 700; color: #1a2035; margin: 14px 0 5px;
        page-break-after: avoid; }}
  h3 {{ font-size: 10.5px; font-weight: 700; color: #555; margin: 10px 0 4px;
        text-transform: uppercase; letter-spacing: 0.5px; page-break-after: avoid; }}

  p {{ margin-bottom: 5px; color: #333; page-break-inside: avoid; }}
  .bullet {{ margin: 2px 0 2px 14px; color: #333; page-break-inside: avoid; }}
  .spacer {{ height: 5px; }}
  .checkbox {{ margin: 3px 0 3px 14px; color: #333; page-break-inside: avoid; }}
  .checkbox.checked {{ color: #888; text-decoration: line-through; }}
  code {{ background: #f4f4f4; padding: 1px 4px; border-radius: 3px;
          font-size: 10px; font-family: monospace; }}

  /* ── Internal block ── */
  .internal-block {{ background: #fff8f8; border: 1px solid #fecaca; border-radius: 5px;
                     padding: 10px 14px; margin: 10px 0; page-break-inside: avoid; }}
  .internal-tag {{ font-size: 8px; font-weight: 700; letter-spacing: 1.5px;
                   text-transform: uppercase; color: #dc2626; margin-bottom: 5px; }}

  /* ── Footer ── */
  .footer {{ position: fixed; bottom: 0; left: 0; right: 0; padding: 7px 36px;
             border-top: 1px solid #e8eaed; display: flex; justify-content: space-between;
             font-size: 9px; color: #bbb; background: white; }}
</style>
</head>
<body>
<div class="header">
  <img src="data:image/png;base64,{LOGO_B64}">
  <div class="header-right">
    <div class="label">Internal Only</div>
    <div class="title">{doc_type}</div>
    <div class="sub">{customer_name} · {today}</div>
  </div>
</div>
<div class="internal-banner">⚠ INTERNAL ONLY — DO NOT SHARE WITH CLIENT</div>
<div class="body">
{body_html}
</div>
<div class="footer">
  <span>Stamina CS Intelligence · {today}</span>
  <span>INTERNAL — {customer_name}</span>
</div>
</body>
</html>"""

    return WP_HTML(string=html).write_pdf()


# ── Email sending ─────────────────────────────────────────────────────────────

def send_email(pair: dict, pdf_bytes: bytes, customer_name: str):
    to_emails = pair.get("report_email") or pair.get("csm_emails") or []
    if not to_emails:
        log("  No emails found for pair — skipping send")
        return

    filename = f"{customer_name.replace(' ', '_')}_Pass1_PreKickoff.pdf"

    # Test mode: override all recipients
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
        "template": {"id": "prekickoff-context", "variables": {"newaccountpylon": customer_name}},
        "attachments": [{"filename": filename,
                          "content": base64.b64encode(pdf_bytes).decode()}],
    }
    if cc_list:
        payload["cc"] = cc_list

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
    log(f"Pass 1 generator started {'[DRY RUN] ' if DRY_RUN else ''}...")

    new_accounts = find_new_accounts()
    log(f"  {len(new_accounts)} accounts need Pass 1")

    if not new_accounts:
        log("Nothing to do.")
        return

    success, errors = 0, 0

    for customer in new_accounts:
        name = customer.get("name", "Unknown")
        log(f"  Processing: {name}")

        try:
            closing_call = fetch_closing_call(customer["id"])
            if not closing_call:
                log(f"    No sales call found for {name} — skipping")
                continue

            contacts = fetch_contacts(customer["id"])
            website  = scrape_website(customer.get("domain", ""))
            pair     = find_csm_pair(customer)

            if not pair:
                log(f"    No CSM pair found for {name} — skipping")
                continue

            log(f"    Generating Pass 1 via GPT-4o...")
            content_md = with_retry(
                lambda: generate_pass1_content(customer, closing_call, contacts, website),
                retries=3, delay=10, label=f"Pass 1 {name}"
            )

            if DRY_RUN:
                log(f"    [DRY RUN] Would store {len(content_md)} chars and send email to {pair.get('report_email')}")
                success += 1
                continue

            # Store in kickoff_documents
            sb.table("kickoff_documents").upsert({
                "customer_id":  customer["id"],
                "brand_id":     customer.get("brand_id"),
                "pass_number":  1,
                "content_md":   content_md,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }, on_conflict="customer_id,pass_number").execute()

            # Generate PDF and send
            pdf_bytes = generate_pdf(content_md, name)
            send_email(pair, pdf_bytes, name)

            log(f"    ✓ Pass 1 complete for {name}")
            success += 1

        except Exception as e:
            log(f"    ERROR for {name}: {e}")
            errors += 1

    log(f"Done. success={success}, errors={errors}")


if __name__ == "__main__":
    main()
