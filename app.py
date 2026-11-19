"""Review app.  Run with:  streamlit run app.py

Nothing is written to the CRM unless a reviewer clicks an Approve button on a specific
proposal. Approve = record the decision, then apply that one proposal and verify it.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from bellhaven import display as D
from bellhaven import store as S
from bellhaven.applier import Applier, NotApproved
from bellhaven.config import load_settings
from bellhaven.crm import CRMClient, CRMError
from bellhaven.matcher import (AMBIGUOUS, CHOW_LINK_EXISTING, CHOW_NEW_SUCCESSOR, CREATE_ACCOUNT, MatchError)
from bellhaven.normalize import norm_street
from bellhaven.pipeline import run as run_pipeline
from bellhaven.scraper import ScrapeError, make_fetcher

st.set_page_config(page_title="Bellhaven CRM review", layout="wide")

settings = load_settings()
store = S.Store(settings.db_path)
client = CRMClient(settings.api_base, settings.token)
applier = Applier(client, store)

ACCOUNT_COLUMNS = ["name", "parent_name", "billing_street", "billing_city", "billing_state", "billing_zip",
                   "care_type", "status", "phone", "lifetime_revenue", "outstanding_ar", "contacts"]
TYPE_LABEL = {"UPDATE_FIELDS": "Field update", "SET_PARENT": "Re-parent", "CHOW_NEW_SUCCESSOR": "CHOW: new successor",
              "CHOW_LINK_EXISTING": "CHOW: link existing", "CREATE_ACCOUNT": "New account", "MARK_DUPLICATE": "Duplicate",
              "FLAG_REVIEW": "Flag for review", "AMBIGUOUS": "Ambiguous match"}
TYPE_ICON = {"UPDATE_FIELDS": ":material/edit:", "SET_PARENT": ":material/account_tree:",
             "CHOW_NEW_SUCCESSOR": ":material/swap_horiz:", "CHOW_LINK_EXISTING": ":material/link:",
             "CREATE_ACCOUNT": ":material/add_business:", "MARK_DUPLICATE": ":material/content_copy:",
             "FLAG_REVIEW": ":material/flag:", "AMBIGUOUS": ":material/help:"}
ID_FIELDS = ("parent_id", "chow_current_account", "duplicate_of_account")

def refresh_queue():
    """Read-only: scrape + read CRM + match. Never writes to the CRM."""
    try:
        summary = run_pipeline(store, client, make_fetcher(settings.site_base), settings.site_base)
        st.session_state["flash"] = ("success", f"Pipeline ran (read-only). Queue changes: {summary['queue']}")
    except (ScrapeError, CRMError, MatchError) as exc:
        store.record_run(False, {"error": str(exc)})
        st.session_state["flash"] = ("error", f"Pipeline failed, queue unchanged: {exc}")


def run_pipeline_button(key: str):
    if st.button("Run pipeline (read-only)", key=key, icon=":material/sync:", use_container_width=True,
                 help="Scrapes the website, reads the CRM with GET requests only, re-runs the matcher and refreshes "
                      "the review queue. It never writes to the CRM."):
        with st.spinner("Scraping the website and reading the CRM (read-only)..."):
            refresh_queue()
        st.rerun()


def local_time(utc_text: str) -> str:
    """'2026-11-19 14:45:47Z' (stored in UTC) -> 'Nov 19, 2026, 9:45 AM EST' in this computer's time zone."""
    try:
        moment = datetime.strptime(utc_text, "%Y-%m-%d %H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone()
    except (TypeError, ValueError):
        return str(utc_text)
    return moment.strftime("%b %-d, %Y, %-I:%M %p %Z")


def status_row():
    """Compact pipeline status under the Review queue heading, from the runs table."""
    ok_run = store.last_successful_run()
    last = store.last_run()
    summary = (ok_run or {}).get("summary", {})
    failed_since = last is not None and not last["ok"] and (ok_run is None or last["id"] > ok_run["id"])
    if failed_since:
        badge = ":red-badge[:material/error: Last run failed]"
        tip = f"Attempt at {local_time(last['at'])}: {last['summary'].get('error', '')[:200]}"
    elif ok_run:
        badge = ":green-badge[:material/check_circle: Pipeline OK]"
        tip = "The latest pipeline run completed. Runs are read-only: they never write to the CRM."
    else:
        badge, tip = ":gray-badge[No runs yet]", "Run the pipeline to build the review queue."
    parts = [f"**{summary.get('facilities_scraped', '–')}** facilities scraped",
             f"**{summary.get('crm_accounts', '–')}** CRM accounts",
             f"Last successful run {local_time(ok_run['at'])}" if ok_run else "No successful run yet"]
    info, button = st.columns([4, 1], vertical_alignment="center")
    info.markdown(" · ".join(parts) + f"  {badge}", help=tip)
    with button:
        run_pipeline_button("run_pipeline_main")


def write_outcome(proposal_id: int, result: dict):
    """Banner at the top of the page plus a toast, stating exactly what happened in the CRM."""
    proposal = store.get(proposal_id)
    names = D.build_names(store.list())
    facility, change = D.row_title(proposal, names)
    lines = []
    if proposal["target_id"]:
        lines.append(f"Account: {facility} (`{proposal['target_id']}`)")
    new_id = result.get("successor_id") or result.get("created_id")
    if new_id:
        lines.append(f"New / linked account: `{new_id}`")
    lines.append(f"Outcome: {D.humanize(result['message'], names)}")
    lines += [f"✔ {D.humanize(c, names)}" for c in result.get("checks", [])]
    body = "  \n".join(md(line) for line in lines)
    if result["verified"]:
        st.session_state["flash"] = ("success", f"**Applied and verified in the CRM: {md(facility)}** · {md(change)}  \n{body}")
        st.session_state["toast"] = (f"Applied and verified: {facility}", ":material/check_circle:")
    else:
        st.session_state["flash"] = ("error", f"**Not applied: {md(facility)}** (#{proposal_id})  \n{body}")
        st.session_state["toast"] = (f"Not applied: {facility}. See the message at the top.", ":material/error:")


def approve(proposal_id: int, chosen: str, note: str):
    try:
        store.decide(proposal_id, S.APPROVED, note, chosen)
        result = applier.apply(proposal_id)
    except (ValueError, NotApproved) as exc:
        st.session_state["flash"] = ("error", str(exc))
        return
    write_outcome(proposal_id, result)


def reject(proposal_id: int, note: str):
    try:
        store.decide(proposal_id, S.REJECTED, note)
        st.session_state["flash"] = ("info", f"Proposal #{proposal_id} rejected. It will not be proposed again.")
    except ValueError as exc:
        st.session_state["flash"] = ("error", str(exc))


def retry(proposal_id: int):
    write_outcome(proposal_id, applier.apply(proposal_id))


def resolve_ambiguous(proposal: dict, account_id: str, note: str):
    slug = proposal["target_key"].replace("site:", "", 1)
    store.decide(proposal["id"], S.RESOLVED, f"Reviewer chose {account_id}. {note}".strip())
    store.set_resolution(slug, account_id)
    refresh_queue()  # read-only; turns the choice into ordinary proposals that still need approval


def md(text) -> str:
    """Escape $ so Streamlit does not read dollar amounts as maths formulas."""
    return str(text).replace("$", "\\$")


def api_requests(proposal: dict, action: str, proposed: dict) -> list:
    """The API calls an approval of this action makes: (method, path, body, note)."""
    target = proposal["target_id"]
    if action == AMBIGUOUS:
        return []
    if action == CREATE_ACCOUNT:
        return [("POST", "/accounts", proposed,
                 "If an account already exists at this address under this parent, it is adopted instead and nothing is created.")]
    if action == CHOW_NEW_SUCCESSOR:
        return [("POST", "/accounts", proposed["new_account"],
                 "Creates the successor under the new parent, or adopts one that already exists at this address."),
                ("PATCH", f"/accounts/{target}", {"chow_current_account": "<id of the successor account>"},
                 "The only field sent to the old account. Its parent is never changed.")]
    return [("PATCH", f"/accounts/{target}", proposed, "Only these fields are sent. Every other field is left as it is.")]


def technical_details(proposal: dict, action: str, proposed: dict, related: list):
    if not st.toggle("Technical details", key=f"tech{proposal['id']}"):
        return
    with st.container(border=True):
        st.caption(f"Proposal #{proposal['id']} · action `{proposal['action']}` · confidence `{proposal['confidence']}` · "
                   f"fingerprint `{proposal['fingerprint']}` · state `{proposal['state']}`")
        ids = [{"Account": a.get("name"), "Account ID": a["account_id"], "Parent ID": a.get("parent_id") or ""}
               for a in related]
        if proposal["target_id"] and proposal["target_id"] not in {r["Account ID"] for r in ids}:
            ids.insert(0, {"Account": (proposal["before"] or {}).get("name"), "Account ID": proposal["target_id"],
                           "Parent ID": (proposal["before"] or {}).get("parent_id") or ""})
        if ids:
            st.dataframe(pd.DataFrame(ids), hide_index=True)

        requests = api_requests(proposal, action, proposed)
        if not requests:
            st.markdown("**No CRM write.** Choosing an account here only records your choice locally and generates "
                        "ordinary proposals for that account, each of which needs its own approval.")
        else:
            sent = proposal["state"] in (S.APPLIED, S.FAILED, S.APPLYING)
            st.markdown("**API requests made on approval**" + (" (see below for what was actually sent)" if sent else "") +
                        ". The account is re-read first; if anything changed since this proposal was made, nothing is sent.")
            for method, path, body, note in requests:
                st.markdown(f"`{method} {path}`" + (f" · {note}" if note else ""))
                st.json(body, expanded=True)
            if proposal.get("alt") and proposal["state"] == S.PENDING and action == proposal["action"]:
                alt = proposal["alt"]
                st.markdown(f"If you choose **{D.humanize(alt['label'], {})}** instead: `PATCH /accounts/{proposal['target_id']}`")
                st.json(alt["proposed"], expanded=True)

        calls = store.api_log(proposal["id"])
        if calls:
            st.markdown("**API calls actually sent** (also on the Audit log page)")
            st.dataframe(pd.DataFrame(calls)[["at", "method", "path", "body", "status"]], hide_index=True)


def diff_table(proposal: dict, action: str, proposed: dict, names: dict) -> pd.DataFrame:
    before = proposal["before"] or {}

    def show(field, value):
        if value in ("", None):
            return "(empty)"
        if field in ID_FIELDS:
            shown = names.get(value, value)
            if field == "duplicate_of_account" and shown == before.get("name"):
                shown += " (the other copy, which is kept)"
            return shown
        return value

    def label(field):
        return "parent" if field == "parent_id" else field

    current_parent = D.short_parent(before.get("parent_name")) or "(no parent)"
    if action == CHOW_NEW_SUCCESSOR:
        rows = [{"field": "OLD account: parent", "current": current_parent,
                 "proposed": "(unchanged, preserved for billing)"},
                {"field": "OLD account: chow_current_account", "current": show("chow_current_account", before.get("chow_current_account")),
                 "proposed": "(the new account below)"}]
        rows += [{"field": f"NEW account: {label(k)}", "current": "(does not exist)", "proposed": show(k, v)}
                 for k, v in proposed["new_account"].items()]
    elif action == CREATE_ACCOUNT:
        rows = [{"field": label(k), "current": "(does not exist)", "proposed": show(k, v)} for k, v in proposed.items()]
    else:
        rows = [{"field": label(k), "current": current_parent if k == "parent_id" else show(k, before.get(k)),
                 "proposed": show(k, v)} for k, v in proposed.items()]
    return pd.DataFrame(rows).astype(str)


def render(proposal: dict, names: dict):
    pid, state = proposal["id"], proposal["state"]
    is_chow = proposal["action"] in (CHOW_NEW_SUCCESSOR, CHOW_LINK_EXISTING)
    facility, change = D.row_title(proposal, names)
    # One status badge per row, most important first: a judgement call (the data does not settle it),
    # then billing-sensitive (CHOW), then confidence. Every proposal still needs approval.
    if proposal["action"] in (AMBIGUOUS, "FLAG_REVIEW"):
        badge = ":red-badge[Judgement call]"
    elif is_chow:
        badge = ":orange-badge[:material/account_balance: CHOW]"
    else:
        badge = {"high": ":green-badge[High confidence]", "medium": ":yellow-badge[Medium confidence]"}.get(
            proposal["confidence"], ":red-badge[Low confidence]")
    header = f"**{md(facility)}** · {md(change)}  {badge}  :gray[:small[#{pid}]]"
    with st.expander(header, expanded=False, icon=TYPE_ICON.get(proposal["action"])):
        for warning in proposal["warnings"] or []:
            (st.error if is_chow else st.warning)(md(D.humanize(warning, names)))

        history = store.history_for_target(proposal["target_key"], pid)
        if history and state == S.PENDING:
            st.info("Earlier decisions on this same record: " + "; ".join(
                f"#{h['id']} {h['action']} was {h['state']}" + (f" ({h['decision_note']})" if h.get("decision_note") else "")
                for h in history) + ". This is a different proposal and needs its own decision.")

        evidence = proposal["evidence"] or {}
        used_alt = proposal["chosen"] == "alt" and proposal.get("alt")
        action = proposal["alt"]["action"] if used_alt else proposal["action"]
        proposed = proposal["alt"]["proposed"] if used_alt else proposal["proposed"]

        if proposal["action"] != AMBIGUOUS:
            st.markdown("**Current vs proposed**" + (f" (reviewer chose: {action})" if used_alt else ""))
            st.table(diff_table(proposal, action, proposed, names).set_index("field"))
        left, right = st.columns(2) if evidence.get("facility") else (None, st.container())
        if left is not None:
            with left:
                fac = evidence["facility"]
                st.markdown("**Website listing**")
                st.markdown(f"[{fac['name']}]({fac['url']})  \n{fac['street']}, {fac['city']}, {fac['state']} {fac['zip']}  \n"
                            f"Care: {', '.join(fac['care_offerings'])} · Administrator: {fac['administrator']} · "
                            f"Phone: {fac['phone']} · Found via: {fac['source']}")
        with right:
            st.markdown("**Evidence**")
            for reason in evidence.get("reasons", []):
                mark = {"Y ": "✅ ", "N ": "❌ ", "~ ": "➖ "}.get(reason[:2])
                text = D.humanize(reason[2:] if mark else reason, names)
                st.markdown(f"- {md((mark or '') + text)}")

        related = evidence.get("related_accounts") or []
        if related:
            st.markdown("**CRM accounts involved** (as of the last pipeline run)")
            frame = pd.DataFrame(related)
            frame["parent_name"] = frame["parent_name"].map(D.short_parent)
            st.dataframe(frame[[c for c in ACCOUNT_COLUMNS if c in frame.columns]], hide_index=True)

        # ---- controls -------------------------------------------------------
        if state == S.PENDING and proposal["action"] == AMBIGUOUS:
            options = {f"{a['name']} · {D.short_parent(a.get('parent_name')) or '(no parent)'}": a["account_id"]
                       for a in related}
            technical_details(proposal, action, proposed, related)
            pick = st.selectbox("Which account is this facility?", ["(choose)"] + list(options), key=f"pick{pid}")
            note = st.text_input("Why (kept in the audit trail)", key=f"note{pid}")
            with st.container(horizontal=True):
                if st.button("Use this account", key=f"res{pid}", type="primary", disabled=pick == "(choose)"):
                    resolve_ambiguous(proposal, options[pick], note)
                    st.rerun()
                if st.button("Reject (leave all as they are)", key=f"rej{pid}"):
                    reject(pid, note)
                    st.rerun()
            st.caption("Choosing writes nothing. It generates normal proposals for this facility that still need approval.")
        elif state == S.PENDING:
            note = st.text_input("Reviewer note (optional)", key=f"note{pid}")
            technical_details(proposal, action, proposed, related)
            with st.container(horizontal=True):
                if st.button("Approve and apply", key=f"approve_{pid}", type="primary", icon=":material/check:"):
                    approve(pid, "primary", note)
                    st.rerun()
                if proposal.get("alt") and st.button(D.humanize(proposal["alt"]["label"], names), key=f"alt{pid}"):
                    approve(pid, "alt", note)
                    st.rerun()
                if st.button("Reject", key=f"rej{pid}"):
                    reject(pid, note)
                    st.rerun()
        else:
            result = proposal.get("result") or {}
            decided = f"Decided {proposal['decided_at']}" if proposal.get("decided_at") else ""
            if proposal.get("decision_note"):
                decided += f" · note: {proposal['decision_note']}"
            new_id = result.get("successor_id") or result.get("created_id")
            ids = (f"Account: {md(facility)} `{proposal['target_id']}`" if proposal["target_id"] else "") + \
                  (f" · new / linked account: {md(names.get(new_id, 'new account'))} `{new_id}`" if new_id else "")
            if state == S.APPLIED:
                outcome = D.humanize(result.get("message", ""), names)
                outcome = "" if outcome.startswith("Applied and verified") else f"  \n{md(outcome)}"
                st.success(f"**Applied and verified in the CRM.**  \n{ids}{outcome}", icon=":material/check_circle:")
                ids = ""
            elif state == S.REJECTED:
                st.info("**Rejected.** Nothing was written to the CRM. This proposal will not be raised again.",
                        icon=":material/block:")
            elif state == S.RESOLVED:
                st.info("**Resolved.** You chose an account; that choice produced ordinary proposals in the Pending list.",
                        icon=":material/task_alt:")
            elif state == S.SUPERSEDED:
                st.warning(f"**Superseded.** {md(D.humanize(result.get('message', 'The CRM or website changed since this was proposed.'), names))}",
                           icon=":material/update:")
            else:
                st.error(f"**Not applied ({state}).** {md(D.humanize(result.get('message', ''), names))}", icon=":material/error:")
            st.caption(" · ".join(x for x in (ids, decided) if x))
            for check in result.get("checks", []):
                st.markdown(f"- ✔ {md(D.humanize(check, names))}")
            technical_details(proposal, action, proposed, related)
            if state in (S.FAILED, S.APPLYING):
                if st.button("Retry (re-reads the CRM first)", key=f"retry{pid}"):
                    retry(pid)
                    st.rerun()


# ---------------------------------------------------------------------- look and feel
st.markdown("""
<style>
[data-testid="stMainBlockContainer"] { max-width: 1200px; padding-top: 4.5rem; padding-left: 2rem; padding-right: 2rem; }
[class*="st-key-approve_"] button { background: #16a34a; border-color: #16a34a; color: #fff; }
[class*="st-key-approve_"] button:hover { background: #15803d; border-color: #15803d; color: #fff; }
h1 { font-weight: 750 !important; letter-spacing: -0.02em; font-size: 2.1rem !important; padding-bottom: 0 !important; }
h3 { font-weight: 650 !important; font-size: 1.15rem !important; }
/* No hardcoded backgrounds or text colours: everything inherits, so light and dark themes both work. */
.sub { opacity: 0.65; font-size: 0.9rem; margin: -2px 0 14px; }
.kpi { border: 1px solid rgba(128,128,128,0.28); border-radius: 14px; padding: 16px 18px 14px; height: 100%; }
.kpi-label { opacity: 0.8; font-size: 0.9rem; font-weight: 500; }
.kpi-value { font-size: 2rem; font-weight: 750; letter-spacing: -0.02em; line-height: 1.25; }
.chip { display: inline-block; font-size: 0.75rem; font-weight: 600; border-radius: 6px; padding: 2px 8px; }
.chip-green { background: rgba(34,197,94,0.16); color: #1fa055; }
.chip-red { background: rgba(239,68,68,0.16); color: #e5484d; }
.chip-amber { background: rgba(245,158,11,0.18); color: #c98a0c; }
.chip-gray { background: rgba(128,128,128,0.18); }
.bar-row { display: flex; align-items: center; gap: 10px; margin: 0; font-size: 0.85rem; }
.st-key-typebars { gap: 0.1rem; }
.st-key-typebars .bar-row { height: 1.7rem; }
.st-key-typebars [data-testid="stMarkdownContainer"] { margin-bottom: 0; }
.st-key-typebars [data-testid="stColumn"] { display: flex; flex-direction: column; justify-content: center; }
.st-key-typebars [data-testid="stHorizontalBlock"] { gap: 0.6rem; }
.st-key-typebars button { justify-content: flex-end; min-height: 1.7rem; padding: 0 4px; }
.st-key-typebars button p { font-size: 0.85rem; white-space: nowrap; }
.st-key-typebars button:hover p { color: #2563eb; text-decoration: underline; }
.bar-label { width: 150px; text-align: right; opacity: 0.85; white-space: nowrap; }
.bar-track { flex: 1; height: 16px; border-radius: 5px; background: rgba(128,128,128,0.12); }
.bar-fill { height: 100%; border-radius: 5px; background: #2563eb; min-width: 4px; }
.bar-count { width: 22px; font-weight: 650; }
[data-testid="stExpander"] details { border: 1px solid rgba(128,128,128,0.28); border-radius: 14px; }
[data-testid="stExpander"] summary { padding: 12px 16px; }
summary:has(.stMarkdownBadge) > span { width: 100%; }
summary:has(.stMarkdownBadge) > span > div { flex: 1 1 auto; }
summary:has(.stMarkdownBadge) [data-testid="stMarkdownContainer"] { flex: 1 1 auto; width: 100%; }
summary:has(.stMarkdownBadge) [data-testid="stMarkdownContainer"] p { display: flex; align-items: center; gap: 6px; }
summary:has(.stMarkdownBadge) .stMarkdownBadge { margin-left: auto; white-space: nowrap; }
summary:has(.stMarkdownBadge) .stMarkdownColoredText { white-space: nowrap; min-width: 2.2rem; text-align: right; }
[data-testid="stTable"] th { white-space: nowrap; }
[data-testid="stTable"] td:first-of-type { min-width: 7rem; }
[data-testid="stVerticalBlockBorderWrapper"] { border-radius: 14px; }
.stButton button { border-radius: 9px; font-weight: 550; }
[data-testid="stDataFrame"] { border: 1px solid rgba(128,128,128,0.28); border-radius: 10px; overflow: hidden; }
[data-testid="stAlert"] { border-radius: 10px; }
</style>
""", unsafe_allow_html=True)


def kpi(column, label, value, chip_text, chip_class):
    column.markdown(
        f'<div class="kpi"><div class="kpi-label">{label}</div><div class="kpi-value">{value}</div>'
        f'<span class="chip {chip_class}">{chip_text}</span></div>',
        unsafe_allow_html=True)


def flash_message():
    flash = st.session_state.pop("flash", None)
    if flash:
        getattr(st, flash[0])(flash[1])
    toast = st.session_state.pop("toast", None)
    if toast:
        st.toast(toast[0], icon=toast[1])


def queue_page():
    counts = store.counts()
    st.title(":material/fact_check: Review queue")
    status_row()
    flash_message()

    attention = counts.get(S.FAILED, 0) + counts.get(S.APPLYING, 0) + counts.get(S.APPROVED, 0)
    pending = store.list(S.PENDING)
    chow_pending = sum(p["action"] in (CHOW_NEW_SUCCESSOR, CHOW_LINK_EXISTING) for p in pending)
    cards = st.columns(4)
    kpi(cards[0], "Pending review", len(pending), f"{chow_pending} CHOW", "chip-amber" if chow_pending else "chip-gray")
    kpi(cards[1], "Applied to CRM", counts.get(S.APPLIED, 0), "verified after write", "chip-green")
    kpi(cards[2], "Needs attention", attention, "failed / in progress", "chip-red" if attention else "chip-gray")
    kpi(cards[3], "Rejected", counts.get(S.REJECTED, 0), "never re-proposed", "chip-gray")
    st.write("")

    views = {"Pending": [S.PENDING], "Applied": [S.APPLIED], "Needs attention": [S.FAILED, S.APPLYING, S.APPROVED],
             "Rejected": [S.REJECTED], "Resolved / superseded": [S.RESOLVED, S.SUPERSEDED]}
    view = st.segmented_control("View", list(views), default="Pending", label_visibility="collapsed") or "Pending"
    proposals = [p for state in views[view] for p in store.list(state)]

    if not proposals:
        with st.container(border=True):
            st.write("Nothing here." + (" Run the pipeline to check for new differences." if view == "Pending" else ""))
        return

    names = D.build_names(store.list())
    with st.container(border=True):
        st.subheader(f"{view} by type")
        by_type = pd.Series([TYPE_LABEL.get(p["action"], p["action"]) for p in proposals]).value_counts()
        options = ["All types"] + list(by_type.index)
        if st.session_state.get("type_filter") not in options:
            st.session_state["type_filter"] = "All types"

        def toggle_type(label):
            # Clicking a row filters to that type; clicking it again shows everything.
            st.session_state["type_filter"] = "All types" if st.session_state.get("type_filter") == label else label

        chart, filt = st.columns([3, 2])
        picked = filt.selectbox(
            "Show", options, key="type_filter",
            format_func=lambda t: f"All types ({len(proposals)})" if t == "All types" else f"{t} ({by_type[t]})")
        chosen = list(by_type.index) if picked == "All types" else [picked]
        sort_by = filt.selectbox("Sort by", D.SORT_OPTIONS, key="sort_by",
                                 help="Confidence: surest matches first. Risk: easy-to-undo changes first; "
                                      "ownership changes (CHOW), new accounts and ambiguous matches last.")
        top = int(by_type.max())
        with chart.container(key="typebars"):
            for label, count in by_type.items():
                dim = picked not in ("All types", label)
                name, bar = st.columns([2, 5], vertical_alignment="center")
                name.button(label, key=f"type_{label}", type="tertiary", on_click=toggle_type, args=(label,), use_container_width=True)
                bar.markdown(
                    f'<div class="bar-row"><div class="bar-track"><div class="bar-fill" '
                    f'style="width:{100 * count / top:.0f}%;opacity:{0.25 if dim else 1}"></div></div>'
                    f'<div class="bar-count">{count}</div></div>', unsafe_allow_html=True)

    for proposal in D.sort_proposals(proposals, sort_by, names):
        if TYPE_LABEL.get(proposal["action"], proposal["action"]) in (chosen or []):
            render(proposal, names)

def money(value) -> str:
    try:
        return f"${float(value):,.0f}"
    except (TypeError, ValueError):
        return str(value)


def no_snapshot_yet(key: str):
    st.info("Nothing saved yet. Run the pipeline once (read-only) to fill this page.")
    run_pipeline_button(key)


def source_line(path: str, run_at: str) -> str:
    link = f" · [View source code]({settings.repo_url}/blob/main/{path})" if settings.repo_url else ""
    return f"Last successful run {run_at} · Source: `{path}`{link}"


def page_header(title: str, icon: str, summary: str, path: str, run_at: str, key: str):
    head, button = st.columns([4, 1], vertical_alignment="bottom")
    head.title(f"{icon} {title}")
    with button:
        run_pipeline_button(key)
    st.markdown(summary)
    st.caption(source_line(path, run_at))
    flash_message()


def scraper_page():
    snap = store.last_snapshot()
    if not snap:
        st.title(":material/travel_explore: Scraper")
        return no_snapshot_yet("run_pipeline_scraper")
    facilities = snap["facilities"]
    homepage_only = [f["name"] for f in facilities if f.get("source") == "homepage"]
    directory = len(facilities) - len(homepage_only)

    page_header("Scraper", ":material/travel_explore:",
                "Pulls every Bellhaven community from the public website (name, address, care offerings, administrator, "
                "phone) and refuses to save a partial list.",
                "bellhaven/scraper.py", snap["at"], "run_pipeline_scraper")
    with st.expander("How the scraper works"):
        st.markdown(
            "1. Walks the community directory page by page and stops at the site's own \"Page X of Y\". "
            "A page number past the end returns the last page again, so \"keep going until empty\" would never stop.\n"
            f"2. Checks the homepage for community links that are not in the directory. "
            f"Found: **{', '.join(homepage_only) or 'none'}**.\n"
            "3. Coverage check: the directory count must match \"N communities listed\", and directory + homepage-only "
            "must match the homepage's community count. A mismatch stops the run before the queue changes, "
            "so a saved scrape has always passed.\n"
            "4. Opens each community page and reads the name, street, city, state, ZIP, care offerings, "
            "administrator and phone.")
    cards = st.columns(4)
    kpi(cards[0], "Facilities scraped", len(facilities), "matches the homepage count", "chip-green")
    kpi(cards[1], "From the directory", directory, "paginated listing", "chip-gray")
    kpi(cards[2], "Homepage only", len(homepage_only), "not in the directory", "chip-amber" if homepage_only else "chip-gray")
    kpi(cards[3], "Coverage check", "Passed", "run stops if counts differ", "chip-green")
    st.write("")

    states = sorted({f["state"] for f in facilities})
    left, right = st.columns([1, 3])
    state = left.selectbox("State", ["All"] + states, key="scraper_state")
    search = right.text_input("Search", placeholder="Name, street or city", key="scraper_search")
    rows = [f for f in facilities if (state == "All" or f["state"] == state)
            and search.lower() in f"{f['name']} {f['street']} {f['city']}".lower()]
    st.dataframe(pd.DataFrame([{
        "Facility": f["name"], "Street": f["street"], "City": f["city"], "State": f["state"], "ZIP": f["zip"],
        "Care offerings": ", ".join(f["care_offerings"]), "Administrator": f["administrator"], "Phone": f["phone"],
        "Found via": f["source"], "Page": f["url"]} for f in rows]),
        hide_index=True, height=min(36 * (len(rows) + 1) + 4, 1300),
        column_config={"Page": st.column_config.LinkColumn("Page", display_text="open")})
    st.caption(f"{len(rows)} of {len(facilities)} facilities shown")


def match_category(row: dict) -> str:
    if "AMBIGUOUS" in row["actions"]:
        return "Ambiguous"
    if "CREATE_ACCOUNT" in row["actions"]:
        return "Not in CRM"
    return "Needs changes" if row["actions"] else "No change needed"


def matcher_page():
    snap = store.last_snapshot()
    rows = [r for r in (snap or {}).get("trace", []) if r["kind"] == "facility"]
    if not rows:
        st.title(":material/join_inner: Matcher")
        return no_snapshot_yet("run_pipeline_matcher")
    unexplained = [r for r in snap["trace"] if r["kind"] == "not_on_site"]
    names = D.build_names(store.list())

    page_header("Matcher", ":material/join_inner:",
                "Links each website facility to a CRM account **by address first** (street + ZIP). Name, phone and "
                "contacts are supporting evidence only, and when the evidence is tied it does not guess.",
                "bellhaven/matcher.py", snap["at"], "run_pipeline_matcher")
    with st.expander("How matching works"):
        st.markdown(
            "1. **Normalise** streets, ZIPs and names, so \"980 West Michigan Avenue\" equals \"980 W Michigan Ave\".\n"
            "2. **Find candidates by address:** same street + ZIP, or same street + city when the ZIP looks mistyped. "
            "Only if the address is unusable (a PO Box): exact name + city + a matching phone or administrator contact. "
            "Name similarity alone never matches.\n"
            "3. **One account per facility:** an account claimed by two facilities makes both ambiguous.\n"
            "4. **Pick the surviving account** when several share the address: the one under Bellhaven, else the only "
            "one, else the one uniquely backed by phone / administrator / exact name. Otherwise ambiguous, no guess.\n"
            "5. **Compare and propose:** name, address and parent differences become proposals; a parent change follows "
            "the CHOW billing rule. Then a reverse check: Bellhaven accounts that no website facility explains.")
    categories = [match_category(r) for r in rows]
    cards = st.columns(5)
    kpi(cards[0], "No change needed", categories.count("No change needed"), "CRM already matches", "chip-green")
    kpi(cards[1], "Matched, needs changes", categories.count("Needs changes"), "see review queue", "chip-amber")
    kpi(cards[2], "Not in CRM", categories.count("Not in CRM"), "new account proposed", "chip-gray")
    kpi(cards[3], "Ambiguous", categories.count("Ambiguous"), "a reviewer decides", "chip-red" if "Ambiguous" in categories else "chip-gray")
    kpi(cards[4], "Not on website", len(unexplained), "Bellhaven accounts", "chip-amber" if unexplained else "chip-gray")
    st.write("")

    show = st.selectbox("Show", ["All", "No change needed", "Needs changes", "Not in CRM", "Ambiguous"], key="matcher_show")
    table = [{
        "Website facility": f"{r['facility']} ({r['state']})",
        "Result": " · ".join(TYPE_LABEL.get(a, a) for a in r["actions"]) or "No change needed",
        "How matched": r["method"],
        "CRM account": r["account_name"] or ("— (several, see below)" if len(r["candidates"]) > 1 else "—"),
        "Current parent": D.short_parent(r["parent_name"]) or ("—" if not r["account_name"] else "(none)"),
        "Accounts at address": len(r["candidates"]), "Confidence": r["confidence"] or "—",
    } for r, c in zip(rows, categories) if show in ("All", c)]
    st.dataframe(pd.DataFrame(table), hide_index=True, height=min(36 * (len(table) + 1) + 4, 1300), column_config={
        "Website facility": st.column_config.TextColumn(width="medium"),
        "How matched": st.column_config.TextColumn(width="medium"),
        "CRM account": st.column_config.TextColumn(width="medium"),
        "Current parent": st.column_config.TextColumn(width="medium"),
        "Accounts at address": st.column_config.NumberColumn("At address", width="small"),
        "Confidence": st.column_config.TextColumn(width="small"),
        "Result": st.column_config.TextColumn(width="medium")})

    with st.container(border=True):
        st.markdown("**Trace one facility**")
        pick = st.selectbox("Facility", [r["facility"] for r in rows], key="matcher_pick", label_visibility="collapsed")
        r = next(x for x in rows if x["facility"] == pick)
        st.markdown(f"**{r['facility']}** · {r['city']}, {r['state']} {r['zip']} · found via {r['source']}  \n"
                    f"How matched: **{r['method']}** · candidates at this address: **{len(r['candidates'])}**")
        if r["candidates"]:
            st.dataframe(pd.DataFrame([{"Account": c["name"], "Parent": D.short_parent(c["parent_name"]) or "(none)",
                                        "Street": c["billing_street"], "ZIP": c["billing_zip"],
                                        "Chosen": "✅" if c["account_id"] == r["account_id"] else ""}
                                       for c in r["candidates"]]), hide_index=True)
        if r["why"]:
            st.markdown(f"Why this account: {r['why']} · confidence **{r['confidence']}**")
        elif len(r["candidates"]) > 1:
            st.markdown("No account chosen: nothing in the data separates the candidates, so a reviewer decides.")
        else:
            st.markdown("No CRM account at this address, so a new account is proposed.")
        st.markdown("Result: " + (" · ".join(TYPE_LABEL.get(a, a) for a in r["actions"]) or "no change needed"))

    st.markdown("#### Bellhaven accounts not on the website")
    st.caption("Reverse check: accounts under the Bellhaven parent in the CRM that no website facility explains.")
    if unexplained:
        st.dataframe(pd.DataFrame([{
            "Account": u["account_name"], "Address": u["address"], "Revenue": money(u["revenue"]),
            "Still owed (AR)": money(u["ar"]), "Result": " · ".join(TYPE_LABEL.get(a, a) for a in u["actions"]),
            "Why": D.humanize(u["detail"], names)} for u in unexplained]), hide_index=True)
    else:
        st.write("None.")

    with st.expander("Advanced: try the address normaliser"):
        a, b = st.columns(2)
        first = a.text_input("Address A", "980 West Michigan Avenue", key="norm_a")
        second = b.text_input("Address B", "980 W Michigan Ave", key="norm_b")
        na, nb = norm_street(first), norm_street(second)
        a.code(na or "(empty)")
        b.code(nb or "(empty)")
        (st.success if na == nb else st.error)("Same street after normalising" if na == nb else "Different streets")


def audit_page():
    st.title(":material/receipt_long: Audit log")
    st.markdown('<div class="sub">Every write sent to the CRM · method, path, body, status · no credentials stored</div>',
                unsafe_allow_html=True)
    log = store.api_log()
    with st.container(border=True):
        st.subheader("CRM writes")
        if log:
            st.dataframe(pd.DataFrame(log)[["at", "proposal_id", "method", "path", "body", "status", "response"]], hide_index=True)
        else:
            st.write("No CRM writes have been sent.")
    with st.container(border=True):
        st.subheader("Pipeline runs (read-only)")
        runs = [dict(r) for r in store.db.execute("SELECT at, ok, summary FROM runs ORDER BY id DESC LIMIT 30").fetchall()]
        if runs:
            st.dataframe(pd.DataFrame(runs), hide_index=True)
        else:
            st.write("No runs yet.")


def force_app_theme():
    """A theme picked earlier in Streamlit's settings is saved in the browser and overrides this app's theme.
    Remove such a saved choice once (and reload) so the light theme from .streamlit/config.toml always applies."""
    components.html("""<script>
    try {
      const store = window.parent.localStorage;
      let changed = false;
      for (const key of Object.keys(store)) {
        if (!key.startsWith("stActiveTheme")) continue;
        let name = "";
        try { name = JSON.parse(store.getItem(key)).name || ""; } catch (e) {}
        if (name !== "Custom Theme") { store.removeItem(key); changed = true; }
      }
      if (changed) window.parent.location.reload();
    } catch (e) {}
    </script>""", height=0)


st.navigation([st.Page(scraper_page, title="Scraper", icon=":material/travel_explore:", url_path="scraper"),
               st.Page(matcher_page, title="Matcher", icon=":material/join_inner:", url_path="matcher"),
               st.Page(queue_page, title="Review queue", icon=":material/fact_check:", default=True),
               st.Page(audit_page, title="Audit log", icon=":material/receipt_long:", url_path="audit")],
              position="top").run()
force_app_theme()
