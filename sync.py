#!/usr/bin/env python3
"""
Stamina CS Intelligence Pipeline — sync.py

Track A: Full Pylon sync   → customers, contacts, issues
Track B: Fathom sync       → meetings matched to customers by contact email then domain
Track C: Close + SOW       → match every Pylon account to Close (email/domain/name),
                             generate SOW from call transcript via GPT-4o,
                             PATCH to Pylon account notes field (Highlights),
                             store in Supabase. Skip if SOW already exists.

Usage:
  python3 sync.py             # live run
  python3 sync.py --dry-run   # print what would happen, write nothing
"""

import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import requests
from openai import OpenAI
from rapidfuzz import fuzz
from supabase import create_client

DRY_RUN = "--dry-run" in sys.argv

# ── Credentials (env vars on GitHub Actions, fallback for local) ──────────────

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://jgvyeavyffenvuhphejg.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImpndnllYXZ5ZmZlbnZ1aHBoZWpnIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc3NzA5MTgxMSwiZXhwIjoyMDkyNjY3ODExfQ.5wgmjcj5q2pWa0tosfaZhvv6buGyq9aj5E1cmAm5Nmk")
PYLON_KEY    = os.environ.get("PYLON_KEY",    "pylon_api_85d658281b647d275a1b1e7dfc081e73de9ebfa9de87d563007eb3ab12251301")
CLOSE_KEY    = os.environ.get("CLOSE_KEY",    "api_0FCex972M1uIx5VuDOWAhQ.2Cb9NFpnEwxdIFdOWKWzFH")
FATHOM_KEY   = os.environ.get("FATHOM_KEY",   "TsFqHkp3rX_-rj-7xt_sFg.QYh_uE7H8Prn9WjH5bHWX0AcdmRFKFX0bUTVkIvlJIA")
OPENAI_KEY   = os.environ.get("OPENAI_KEY",   "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")

PYLON_BASE  = "https://api.usepylon.com"
CLOSE_BASE  = "https://api.close.com/api/v1"
FATHOM_BASE = "https://api.fathom.ai/external/v1"

TIER_TAGS             = {"Base", "Custom", "Enterprise"}
FUZZY_MATCH_THRESHOLD = 80

# ── Clients ───────────────────────────────────────────────────────────────────

sb     = create_client(SUPABASE_URL, SUPABASE_KEY)
openai = OpenAI(api_key=OPENAI_KEY)

pylon_h  = {"Authorization": f"Bearer {PYLON_KEY}", "Content-Type": "application/json"}
fathom_h = {"X-Api-Key": FATHOM_KEY}
close_auth = (CLOSE_KEY, "")

# Persistent sessions — SSL handshake happens once, all subsequent requests reuse the connection
pylon_session  = requests.Session()
pylon_session.headers.update(pylon_h)
fathom_session = requests.Session()
fathom_session.headers.update(fathom_h)
close_session  = requests.Session()


# ── Utilities ─────────────────────────────────────────────────────────────────

def log(msg: str):
    prefix = "[DRY-RUN] " if DRY_RUN else ""
    print(f"[{datetime.utcnow().strftime('%H:%M:%S')}] {prefix}{msg}", flush=True)


def now_utc() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def domain_of(email: str) -> "str | None":
    return email.split("@")[1].lower().strip() if email and "@" in email else None


def pylon_get(path: str, params: dict = None) -> dict:
    r = pylon_session.get(f"{PYLON_BASE}/{path}", params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def close_get(path: str, params: dict = None) -> dict:
    r = close_session.get(f"{CLOSE_BASE}/{path}", auth=close_auth, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def fathom_get(path: str, params: dict = None) -> dict:
    r = fathom_session.get(f"{FATHOM_BASE}/{path}", params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def sb_upsert(table: str, row: dict, on_conflict: str):
    if DRY_RUN:
        log(f"  [WOULD UPSERT] {table}: {list(row.keys())}")
        return
    sb.table(table).upsert(row, on_conflict=on_conflict).execute()


def sb_insert(table: str, row: dict):
    if DRY_RUN:
        log(f"  [WOULD INSERT] {table}: {list(row.keys())}")
        return type("R", (), {"data": [{"id": "dry-run-id"}]})()
    return sb.table(table).insert(row).execute()


def sb_update(table: str, row: dict, match_col: str, match_val: str):
    if DRY_RUN:
        log(f"  [WOULD UPDATE] {table} WHERE {match_col}={match_val}: {list(row.keys())}")
        return
    sb.table(table).update(row).eq(match_col, match_val).execute()


def markdown_to_html(md: str) -> str:
    html = md
    html = re.sub(r"^### (.+)$", r"<h3>\1</h3>", html, flags=re.MULTILINE)
    html = re.sub(r"^## (.+)$",  r"<h2>\1</h2>",  html, flags=re.MULTILINE)
    html = re.sub(r"^# (.+)$",   r"<h1>\1</h1>",   html, flags=re.MULTILINE)
    html = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html)
    html = re.sub(r"\*(.+?)\*",     r"<em>\1</em>",         html)
    html = re.sub(r"^[-•] (.+)$",   r"<li>\1</li>", html, flags=re.MULTILINE)
    html = re.sub(r"^✅ (.+)$",     r"<li>✅ \1</li>", html, flags=re.MULTILINE)
    html = re.sub(r"^⬜ (.+)$",     r"<li>⬜ \1</li>", html, flags=re.MULTILINE)
    html = re.sub(r"^[-─]{3,}$",    r"<hr>",         html, flags=re.MULTILINE)
    parts = []
    for p in re.split(r"\n{2,}", html):
        p = p.strip()
        if not p:
            continue
        if p.startswith("<h") or p.startswith("<hr") or "<li>" in p:
            parts.append(p)
        else:
            parts.append(f"<p>{p.replace(chr(10), '<br>')}</p>")
    return "\n".join(parts)


# ── Track A: Pylon sync ───────────────────────────────────────────────────────

def build_user_cache() -> dict:
    data = pylon_get("users")
    return {u["id"]: u["name"] for u in data.get("data", [])}


def fetch_all_accounts() -> list:
    accounts, cursor = [], None
    while True:
        params = {"limit": 100}
        if cursor:
            params["cursor"] = cursor
        resp   = pylon_get("accounts", params)
        batch  = resp.get("data", [])
        accounts.extend(batch)
        pag = resp.get("pagination", {})
        if not pag.get("has_next_page"):
            break
        cursor = pag.get("cursor")
    return accounts


def upsert_account(account: dict, user_cache: dict) -> str:
    cf = account.get("custom_fields", {})

    def cfv(key):
        e = cf.get(key)
        return e["value"] if e else None

    tags     = account.get("tags", [])
    tier     = next((t for t in tags if t in TIER_TAGS), None)
    owner_id = (account.get("owner") or {}).get("id")

    row = {
        "pylon_account_id":      account["id"],
        "name":                  account.get("name"),
        "domain":                account.get("primary_domain"),
        "account_owner":         user_cache.get(owner_id) if owner_id else None,
        "status":                cfv("status"),
        "csm_owner":             cfv("team_assigned"),
        "active_inboxes":        int(cfv("active_inboxes"))        if cfv("active_inboxes")        is not None else None,
        "disconnected_inboxes":  int(cfv("disconnected_inboxes"))  if cfv("disconnected_inboxes")  is not None else None,
        "last_meeting_date":     cfv("calendar.last_meeting_date"),
        "next_meeting_date":     cfv("calendar.next_meeting_date"),
        "tier":                  tier,
        "custom_fields":         cf,
        "pylon_synced_at":       now_utc(),
    }
    if DRY_RUN:
        log(f"  [WOULD UPSERT] customers: {row['name']}")
        return "dry-run-uuid"
    result = sb.table("customers").upsert(row, on_conflict="pylon_account_id").execute()
    return result.data[0]["id"]


def sync_contacts(account_id: str, customer_uuid: str):
    resp     = pylon_get("contacts", {"account_id": account_id, "limit": 200})
    contacts = resp if isinstance(resp, list) else resp.get("data", [])
    if not contacts:
        return
    ts = now_utc()
    rows = [{
        "customer_id":      customer_uuid,
        "pylon_contact_id": c["id"],
        "name":             c.get("name"),
        "email":            c.get("email"),
        "synced_at":        ts,
    } for c in contacts]
    if not DRY_RUN:
        sb.table("contacts").upsert(rows, on_conflict="pylon_contact_id").execute()
    else:
        log(f"  [WOULD UPSERT] contacts: {len(rows)} rows")


def sync_issues(account_id: str, customer_uuid: str):
    """Single 30-day window — avoids rate limits and covers active issues."""
    end   = datetime.now(timezone.utc)
    start = end - timedelta(days=30)
    try:
        resp   = pylon_get("issues", {
            "account_id": account_id,
            "start_time": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end_time":   end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "limit":      500,
        })
        issues = resp.get("data", [])
        if not issues:
            return
        ts   = now_utc()
        rows = [{
            "customer_id":    customer_uuid,
            "pylon_issue_id": issue["id"],
            "title":          issue.get("title"),
            "status":         issue.get("state"),
            "priority":       None,
            "created_at":     issue.get("created_at"),
            "resolved_at":    issue.get("resolution_time"),
            "synced_at":      ts,
        } for issue in issues]
        if not DRY_RUN:
            sb.table("issues").upsert(rows, on_conflict="pylon_issue_id").execute()
        else:
            log(f"  [WOULD UPSERT] issues: {len(rows)} rows")
    except Exception as e:
        log(f"    Issues error for {account_id}: {e}")


def run_track_a(user_cache: dict) -> tuple:
    log("=== TRACK A: Pylon sync ===")
    accounts = fetch_all_accounts()
    log(f"  {len(accounts)} accounts found")
    synced, errors = 0, []
    for acct in accounts:
        try:
            uuid = upsert_account(acct, user_cache)
            sync_contacts(acct["id"], uuid)
            sync_issues(acct["id"], uuid)
            synced += 1
            log(f"  ✓ {acct.get('name')}")
        except Exception as e:
            errors.append({"account_id": acct.get("id"), "name": acct.get("name"), "error": str(e)})
            log(f"  ✗ {acct.get('name')}: {e}")
    log(f"Track A done — {synced}/{len(accounts)} synced")
    return synced, errors


# ── Track B: Fathom meetings ──────────────────────────────────────────────────

def build_fathom_lookup_caches() -> tuple[dict, dict]:
    """
    Returns:
      email_cache  → {email_lower: customer_row}   (from contacts table)
      domain_cache → {domain_lower: customer_row}  (from customers.domain)
    """
    customers = sb.table("customers").select("id, pylon_account_id, name, domain").execute().data
    domain_cache = {r["domain"].lower().strip(): r for r in customers if r.get("domain")}

    contacts = sb.table("contacts").select("email, customer_id").execute().data
    cust_by_id = {r["id"]: r for r in customers}
    email_cache = {}
    for c in contacts:
        if c.get("email"):
            cust = cust_by_id.get(c["customer_id"])
            if cust:
                email_cache[c["email"].lower().strip()] = cust

    return email_cache, domain_cache


def flatten_transcript(transcript: list) -> str:
    if not transcript:
        return ""
    lines = []
    for seg in transcript:
        speaker = seg.get("speaker", {}).get("display_name", "Unknown")
        lines.append(f"[{seg.get('timestamp', '')}] {speaker}: {seg.get('text', '')}")
    return "\n".join(lines)


def find_customer_for_meeting(invitees: list, email_cache: dict, domain_cache: dict) -> "dict | None":
    """
    Match Fathom meeting to a customer using external attendees.
    Priority: 1) exact email match in contacts table, 2) domain match in customers.
    """
    external = [i for i in invitees if i.get("is_external") and i.get("email")]
    # 1. Exact email match
    for i in external:
        cust = email_cache.get(i["email"].lower().strip())
        if cust:
            return cust
    # 2. Domain match
    for i in external:
        d = domain_of(i["email"])
        if d:
            cust = domain_cache.get(d)
            if cust:
                return cust
    return None


def run_track_b(email_cache: dict, domain_cache: dict) -> tuple:
    log("=== TRACK B: Fathom meetings ===")
    cursor, synced, errors = None, 0, []

    while True:
        params = {"limit": 50, "include_transcript": "true"}
        if cursor:
            params["cursor"] = cursor
        resp     = fathom_get("meetings", params)
        meetings = resp.get("items", [])

        for mtg in meetings:
            try:
                if mtg.get("calendar_invitees_domains_type") == "only_internal":
                    continue

                invitees        = mtg.get("calendar_invitees", [])
                customer        = find_customer_for_meeting(invitees, email_cache, domain_cache)
                transcript_text = flatten_transcript(mtg.get("transcript"))
                summary         = mtg.get("default_summary")

                ext_emails = [i["email"] for i in invitees if i.get("is_external") and i.get("email")]
                who = customer["name"] if customer else f"unmatched ({', '.join(ext_emails)})"

                sb_upsert("meetings", {
                    "customer_id":      customer["id"] if customer else None,
                    "pylon_meeting_id": str(mtg["recording_id"]),
                    "title":            mtg.get("title"),
                    "meeting_date":     mtg.get("scheduled_start_time") or mtg.get("recording_start_time"),
                    "summary_text":     summary or transcript_text,
                    "action_items":     mtg.get("action_items"),
                    "attendees":        invitees,
                    "recording_url":    mtg.get("share_url"),
                    "synced_at":        now_utc(),
                }, on_conflict="pylon_meeting_id")

                synced += 1
                log(f"  ✓ {mtg.get('title')} → {who}")
            except Exception as e:
                errors.append({"meeting_id": mtg.get("recording_id"), "title": mtg.get("title"), "error": str(e)})
                log(f"  ✗ {mtg.get('title')}: {e}")

        cursor = resp.get("next_cursor")
        if not cursor:
            break

    log(f"Track B done — {synced} meetings synced")
    return synced, errors


# ── Track C: Close CRM matching + SOW generation ──────────────────────────────

def _fetch_close_page(skip: int) -> list:
    session = requests.Session()
    try:
        r = session.get(f"{CLOSE_BASE}/lead/", auth=close_auth,
                        params={"_limit": 100, "_skip": skip, "_fields": "id,display_name,url,contacts"},
                        timeout=30)
        r.raise_for_status()
        return r.json().get("data", [])
    finally:
        session.close()


def build_close_cache() -> dict:
    """Load all Close leads in parallel pages: lead_id → {company_name, url, emails}"""
    # First page to get total count
    first = _fetch_close_page(0)
    if not first:
        return {}
    # Close doesn't expose total count — fetch pages until empty, 20 parallel at a time
    cache = {}
    skip, batch_size = 0, 5
    while True:
        skips = list(range(skip, skip + batch_size * 100, 100))
        with ThreadPoolExecutor(max_workers=batch_size) as pool:
            results = list(pool.map(_fetch_close_page, skips))
        got_any = False
        for page in results:
            if page:
                got_any = True
                for lead in page:
                    emails = [e.get("email", "") for c in lead.get("contacts", []) for e in c.get("emails", [])]
                    cache[lead["id"]] = {
                        "company_name": lead.get("display_name", ""),
                        "url":          lead.get("url", ""),
                        "emails":       emails,
                    }
        if not got_any:
            break
        # If last batch had a partial page, we're done
        if len(results[-1]) < 100:
            break
        skip += batch_size * 100
    return cache


def match_close_lead(customer: dict, contact_emails: list[str], close_cache: dict) -> tuple:
    """
    Match in priority order:
      1. Contact email domain  → exact match against Close lead contact emails
      2. Account domain        → match against Close lead URL or contact email domains
      3. Company name          → fuzzy match (rapidfuzz token_sort_ratio ≥ 80)
    Returns (lead_id, confidence) or (None, 0).
    """
    acct_domain  = (customer.get("domain") or "").lower().strip()
    acct_name    = (customer.get("name")   or "").lower().strip()
    all_domains  = {domain_of(e) for e in contact_emails if domain_of(e)} | ({acct_domain} if acct_domain else set())
    all_domains.discard(None)

    # 1 & 2 — domain / email exact match
    for lead_id, ld in close_cache.items():
        close_domains = {domain_of(e) for e in ld["emails"] if domain_of(e)}
        close_url_domain = domain_of("x@" + ld["url"].replace("https://", "").replace("http://", "").split("/")[0]) if ld["url"] else None
        if close_url_domain:
            close_domains.add(close_url_domain)
        if all_domains & close_domains:
            return lead_id, 95

    # 3 — fuzzy company name
    best_id, best_score = None, 0
    for lead_id, ld in close_cache.items():
        score = fuzz.token_sort_ratio(acct_name, ld["company_name"].lower().strip())
        if score > best_score:
            best_score, best_id = score, lead_id
    if best_score >= FUZZY_MATCH_THRESHOLD:
        return best_id, best_score

    return None, 0


def fetch_latest_close_meeting_with_summary(lead_id: str) -> "dict | None":
    """
    Return the most recent completed Close meeting that has a non-empty AI summary.
    Close populates meeting.summary.text via its AI notetaker.
    """
    resp = close_get("activity/meeting/", {
        "lead_id":   lead_id,
        "status":    "completed",
        "_order_by": "-date_created",
        "_limit":    10,
    })
    for mtg in resp.get("data", []):
        summary = mtg.get("summary") or {}
        if summary.get("text"):
            return mtg
    return None


SOW_PROMPT_TEMPLATE = """\
You are a senior customer success manager at Stamina preparing an internal handover document for the CS team.

This document is read by two audiences:
1. The CSM assigned to this account — needs to prep for the kickoff call without starting from scratch
2. Claude Code — an AI assistant that will use this document to plan the client's outbound campaigns in the GTM system

Write for both: structured enough for AI to parse, clear enough for a CSM to scan in two minutes.

ABOUT STAMINA:
Outbound sales platform + dedicated GTM engineer. Plans: Base ($499/month) and Custom/Enterprise (higher volume, Stamina sets up domains and inboxes, defined send volume). Includes data enrichment, AI-personalized email sequences, deliverability infrastructure, dedicated growth strategist, Slack support. No long-term contracts.

OUTPUT: Output ONLY clean markdown. No JSON. No preamble. No explanation. Start directly with the document.

————————————————————————————————————————

CS Handover — {company_name}
Plan: [plan type and volume] | Industry: [industry] | Location: [city/country]

Date: {today}
AE: {ae_name}
Contact: [contact_name] — [contact_email]

————————————————————————————————————————

Business Overview
2–3 sentences. What they sell, who they sell to, how they generate revenue. Include where they are based and, if relevant, what that means for how CS should engage — some regions are relationship-first (Middle East, Southern Europe), others are more transactional and direct (UK, US). Only add the cultural note if it's genuinely useful context, not as a box-checking exercise. Use specific details from the call — names, numbers, markets. Do not genericise.

————————————————————————————————————————

Outbound Readiness
Free text. Cover: how much cold email experience this client has, whether they have an in-house sales team, their sophistication level (do they understand deliverability, ICP logic, sequencing?), and what this means for how CS should approach them. Do not use fixed labels. Write naturally based on what came up on the call.

————————————————————————————————————————

Campaign Structure — [N] Stream(s)
For each distinct targeting segment or campaign goal mentioned, create one stream subsection.

Stream [N] — [Name]
Goal: One sentence — what this stream is trying to achieve.
Company Criteria: Firmographic filters — industries, sizes, revenue, funding stage, geography, company age, type.
People Criteria: Titles, seniority, departments.
Exclusions: Explicit do-not-contacts, carve-outs, and edge cases the client mentioned.
Nuances: Specific instructions that don't fit into standard filters. Omit if none.
Signals to Use: Only include signals explicitly mentioned on the call. If none were discussed, write: "Not discussed — CSM to explore on kickoff." Do not infer.
Data Sources: Name specific tools mentioned. If not discussed, write: "To confirm on kickoff."

————————————————————————————————————————

Plan & Infrastructure
Plan Type: [Base / Custom / Enterprise]
Monthly Volume: [from call — what was agreed, not requested. Note significant deltas.]
Infrastructure Responsibility: [client / Stamina / not discussed]

Free text: domain restrictions, inbox count, warmup timeline, AI lead scoring, integrations. If not discussed, state what is standard for this plan and flag what needs confirming.

————————————————————————————————————————

Communication Preference
State preferred channel based on what was discussed. If not discussed: "Not confirmed — defaulting to Slack. Confirm on kickoff."

————————————————————————————————————————

⚠️ Internal Notes
NOT shared with client. Be candid.

Include: client personality, red flags, commitments AE made that CS must honor, strategic importance.
If nothing notable: "No flags from sales call."

————————————————————————————————————————

Expected Questions & Topics to Prepare For
Only include topics from the transcript — questions raised, things deflected, areas of skepticism.

- [Topic] — [what the client asked, what the AE said, what's unresolved]

If nothing notable: "Nothing notable flagged from sales call."

————————————————————————————————————————

Kickoff Checklist

Standard Items
✅ = confirmed on sales call | ⬜ = still needed

- Block list
- Domain naming preferences and sender names/personas
- Copy direction (client writes / approves / contributes?)
- Approval workflow (list review + copy sign-off before launch?)
- Communication preference
- Timeline expectations

Client-Specific Items
Only add items directly implied by something said on the call. No industry-standard additions.

✅ [Item] — [what was confirmed]
⬜ [Item] — [what to ask]

————————————————————————————————————————

RULES:
1. Only use information present in the transcript or enrichment data. Do not invent or fill in with industry norms.
2. If something was not discussed, say so explicitly.
3. If the call was light, produce a shorter document. Accurate and sparse beats plausible and padded.
4. Signals: only include signals explicitly mentioned on the call.
5. Checklist: only add what the call actually raised.
6. Document agreed scope, not requests.
7. Expected Questions: only from the transcript.

————————————————————————————————————————

CALL DATA:
Company: {company_name}
AE: {ae_name}
Date: {today}
Duration: {duration}

TRANSCRIPT / NOTES:
{transcript}
"""


def generate_sow(meeting: dict, company_name: str, ae_name: str) -> str:
    # Use Close's AI meeting summary as the source material
    summary_obj = meeting.get("summary") or {}
    meeting_summary = summary_obj.get("text") or meeting.get("note") or ""
    today    = datetime.utcnow().strftime("%B %d, %Y")
    duration = f"{meeting.get('actual_duration') or meeting.get('duration', 'unknown')} seconds"

    prompt = SOW_PROMPT_TEMPLATE.format(
        company_name=company_name,
        ae_name=ae_name,
        today=today,
        duration=duration,
        transcript=meeting_summary,
    )
    resp = openai.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=4096,
        temperature=0.3,
    )
    return resp.choices[0].message.content


def post_sow_as_highlight(pylon_account_id: str, sow_md: str):
    """POST to /accounts/{id}/highlights — appears in the Highlights panel."""
    if DRY_RUN:
        log(f"  [WOULD POST] /accounts/{pylon_account_id}/highlights")
        return
    body_html = markdown_to_html(sow_md)
    r = requests.post(
        f"{PYLON_BASE}/accounts/{pylon_account_id}/highlights",
        headers=pylon_h,
        json={"content_html": body_html},
        timeout=30,
    )
    r.raise_for_status()


def accounts_with_existing_sow() -> set[str]:
    """Return set of customer UUIDs that already have an SOW (non-null ai_summary)."""
    rows = (
        sb.table("closing_calls")
        .select("customer_id")
        .not_.is_("ai_summary", "null")
        .execute()
        .data
    )
    return {r["customer_id"] for r in rows}


def run_track_c(close_cache: dict) -> tuple:
    log("=== TRACK C: Close matching + SOW generation ===")

    customers     = sb.table("customers").select("id, pylon_account_id, name, domain").execute().data
    existing_sows = accounts_with_existing_sow()
    log(f"  {len(customers)} accounts total, {len(existing_sows)} already have SOWs")

    enriched, errors = 0, []

    for customer in customers:
        cid = customer["id"]
        try:
            if cid in existing_sows:
                log(f"  – {customer['name']} (SOW exists, skipping)")
                continue

            contact_rows   = sb.table("contacts").select("email").eq("customer_id", cid).execute().data
            contact_emails = [r["email"] for r in contact_rows if r.get("email")]

            lead_id, confidence = match_close_lead(customer, contact_emails, close_cache)

            if not lead_id:
                if not DRY_RUN:
                    sb.table("unmatched_accounts").delete().eq("pylon_account_id", customer["pylon_account_id"]).execute()
                    sb.table("unmatched_accounts").insert({
                        "pylon_account_id": customer["pylon_account_id"],
                        "pylon_name":       customer["name"],
                        "attempted_at":     now_utc(),
                        "reason":           "No Close lead matched (domain, email, name all failed)",
                    }).execute()
                log(f"  ~ No Close match: {customer['name']}")
                continue

            sb_update("customers", {
                "close_lead_id":     lead_id,
                "match_confidence":  int(round(confidence)),
                "close_enriched_at": now_utc(),
            }, "id", cid)

            meeting = fetch_latest_close_meeting_with_summary(lead_id)
            if not meeting:
                log(f"  ✓ Matched {customer['name']} → Close (no AI summary yet, SOW skipped)")
                continue

            ae_name      = meeting.get("created_by_name", "Unknown AE")
            summary_text = (meeting.get("summary") or {}).get("text", "")
            log(f"  ✓ {customer['name']} matched (conf={confidence}%) — {len(summary_text)} chars — generating SOW...")

            if DRY_RUN:
                log(f"    [WOULD GENERATE SOW + POST highlight for {customer['name']}]")
                enriched += 1
                continue

            sow_md = generate_sow(meeting, customer["name"], ae_name)
            post_sow_as_highlight(customer["pylon_account_id"], sow_md)
            log(f"    Posted highlight to Pylon for {customer['name']}")

            sb.table("closing_calls").insert({
                "customer_id":      cid,
                "close_call_id":    meeting["id"],
                "call_date":        meeting.get("date_created"),
                "ae_name":          ae_name,
                "duration_seconds": meeting.get("actual_duration") or meeting.get("duration"),
                "raw_notes":        meeting.get("note"),
                "transcript_text":  summary_text,
                "ai_summary":       sow_md,
                "pylon_memory_id":  None,
                "summarized_at":    now_utc(),
            }).execute()

            enriched += 1

        except Exception as e:
            errors.append({"customer_id": cid, "name": customer.get("name"), "error": str(e)})
            log(f"  ✗ {customer.get('name')}: {e}")

    log(f"Track C done — {enriched} SOWs generated")
    return enriched, errors


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    run_start = now_utc()
    log(f"Sync started at {run_start}")

    run_id = None
    if not DRY_RUN:
        run_row = sb.table("sync_runs").insert({
            "started_at":        run_start,
            "accounts_synced":   0,
            "accounts_enriched": 0,
            "errors":            [],
        }).execute()
        run_id = run_row.data[0]["id"]

    all_errors, synced, enriched = [], 0, 0

    try:
        log("Building Pylon user cache...")
        user_cache = build_user_cache()
        log(f"  {len(user_cache)} users")

        synced, a_errors = run_track_a(user_cache)
        all_errors.extend(a_errors)

        log("Building Fathom lookup caches...")
        email_cache, domain_cache = build_fathom_lookup_caches()
        log(f"  email_cache={len(email_cache)}, domain_cache={len(domain_cache)}")

        _, b_errors = run_track_b(email_cache, domain_cache)
        all_errors.extend(b_errors)

        log("Building Close leads cache...")
        close_cache = build_close_cache()
        log(f"  {len(close_cache)} Close leads loaded")

        enriched, c_errors = run_track_c(close_cache)
        all_errors.extend(c_errors)

    except Exception as e:
        log(f"FATAL: {e}")
        all_errors.append({"stage": "main", "error": str(e)})

    if not DRY_RUN and run_id:
        sb.table("sync_runs").update({
            "finished_at":       now_utc(),
            "accounts_synced":   synced,
            "accounts_enriched": enriched,
            "errors":            all_errors,
        }).eq("id", run_id).execute()

    log(f"Done. synced={synced}, enriched={enriched}, errors={len(all_errors)}")


if __name__ == "__main__":
    main()
