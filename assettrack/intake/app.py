# assettrack/intake/app.py
"""
Issue 4-1+: Local Intake UI (Keyboard Wedge)

Feynman-brief:
- Scanner acts like a keyboard.
- Browser input box receives the "typed" barcode + Enter.
- We store scans in an in-memory list (queue) and echo them back.
- Preview/validate/commit are separate steps; commit is atomic.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import sqlite3
import smtplib
import tempfile
import time
from email.message import EmailMessage
from email.utils import getaddresses
from io import BytesIO, TextIOWrapper
from pathlib import Path
from typing import Optional
from datetime import datetime, timezone, date, timedelta

from flask import Flask, abort, flash, jsonify, make_response, redirect, render_template, request, send_file, session, url_for
from reportlab.lib import colors
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from assettrack import ASSETTRACK_VERSION
import assettrack.db as db_module
from assettrack.assets import (
    APPROVED_NEW_EQUIPMENT_TYPES,
    EQUIPMENT_TYPE_LABELS,
    SUPPORTED_EQUIPMENT_TYPE_MESSAGE,
    equipment_type_label,
    get_asset_table_columns,
    is_approved_new_equipment_type,
    normalize_equipment_type,
    validate_new_equipment_type,
)
from assettrack.barcodes import barcode_lookup_key
from assettrack.cases import CASE_SIZE_OPTIONS, save_case_size
from assettrack.custody_accountability import (
    CustodyAccountabilityReport,
    HolderSummary,
    build_custody_accountability_report,
)
from assettrack.custody_analytics import (
    AnalyticsDataset,
    SUPPORTED_ANALYTICS,
    build_analytics_dataset,
)
from assettrack.dashboard import build_dashboard_data, get_custody_days_threshold
from assettrack.db import bootstrap_db, get_connection
from assettrack.drilldowns import (
    get_case_inventory,
    get_case_slot_detail,
    get_holder_custody_detail,
    list_case_summaries,
    list_holders_in_custody,
)
from assettrack.ingest.validator import validate_rows
from assettrack.ingest.committer import BatchCommitError, commit_batch
from assettrack.intake.scan import Scan
from assettrack.intake.to_ingest import scan_to_ingest_row
from assettrack.auth import (
    SESSION_ABSOLUTE_TIMEOUT_SECONDS,
    SESSION_IDLE_TIMEOUT_SECONDS,
    begin_auth_session,
    clear_auth_session,
    current_user,
    require_login,
    require_role,
)
from assettrack.holders import create_holder, get_holder, list_holders, search_holders, set_holder_active, update_holder
from assettrack.holder_import import (
    HolderImportAuditContext,
    HolderImportPreview,
    HolderImportReport,
    import_holders_csv,
    preview_holders_csv,
)
from assettrack.import_analysis import analyze_asset_import_csv, analyze_asset_import_xlsx
from assettrack.import_reconciliation import build_asset_import_preview
from assettrack.reconciliation_dispositions import (
    insert_reconciliation_disposition_event,
    latest_reconciliation_dispositions,
)
from scripts.reconcile_government_inventory import (
    active_discrepancies,
    discrepancy_key_from_snapshot,
    reconcile_inventory,
)
from assettrack.natural_sort import natural_identifier_sort_key
from assettrack.reference_data import (
    create_building,
    create_organization,
    create_organization_building_mapping,
    list_buildings,
    list_organization_building_mappings,
    list_organizations,
    set_building_active,
    update_building_name,
)
from assettrack.slots import move_asset_between_slots_in_tx
from assettrack.settings import (
    active_receipt_cc_setting,
    read_receipt_cc_setting,
    save_receipt_cc_addresses,
)
from assettrack.audit import ACTIVE_EVENTS_WHERE, record_event
from assettrack.event_types import (
    ISSUE_EVENT_TYPE,
    RETURN_EVENT_TYPE,
    issue_event_type_values,
    normalize_event_type,
    return_event_type_values,
)
from assettrack.restore import (
    RestoreError,
    RestoreOperationError,
    RestoreValidationError,
    clear_recovery_state,
    inspect_uploaded_database,
    load_recovery_state,
    load_restore_history,
    recovery_mode_is_active,
    restore_history_path_for,
    recovery_state_path_for,
    restore_database,
    rollback_artifact_path_for,
)
from assettrack.users import (
    change_own_password,
    count_users,
    create_user,
    get_user_by_id,
    get_user_by_username,
    is_temporary_password,
    list_users,
    reset_user_password,
    set_user_active,
    set_user_role,
    verify_password,
)

app = Flask(__name__)
app.secret_key = os.getenv("ASSETTRACK_SECRET_KEY", "dev-not-secret")

bootstrap_db(db_module.DB_PATH)

# In-memory only: wiped on restart
SCAN_QUEUE: list[Scan] = []
CASE_DETAIL_QUEUE_WORKFLOW_SESSION_KEY = "case_detail_queue_workflow"

INTAKE_TIMEOUT_SECONDS = int(os.getenv("ASSETTRACK_INTAKE_TIMEOUT_SECONDS", str(SESSION_IDLE_TIMEOUT_SECONDS)))
TERMINAL_LOCATION_TYPE = "DISPOSED"
TERMINAL_LOCATION_TYPES = {"DISPOSED", "RETIRED"}
RETIRE_FAILURE_TYPES = {"HARDWARE", "LOST", "STOLEN", "DESTROYED", "OTHER"}
ASSET_EQUIPMENT_TYPE_OPTIONS = APPROVED_NEW_EQUIPMENT_TYPES
ASSET_EQUIPMENT_TYPE_LABELS = EQUIPMENT_TYPE_LABELS
ASSET_IMPORT_TEMPFILE_SUFFIXES = {
    ".csv": ".csv",
    ".xlsx": ".xlsx",
}
ASSET_IMPORT_ALLOWED_EXTENSIONS = set(ASSET_IMPORT_TEMPFILE_SUFFIXES)
DEMO_SUMMARY = {
    "assets_in_custody": 18,
    "pending_receipts": 2,
    "holders": 7,
    "cases": 4,
}
DEMO_HOLDERS = [
    {"name": "Signal Platoon", "organization": "Operations", "email": "signal.platoon@example.demo", "asset_count": 6},
    {"name": "Maintenance Shop", "organization": "Support", "email": "maintenance.shop@example.demo", "asset_count": 4},
    {"name": "Forward Team Alpha", "organization": "Field Team", "email": "fta@example.demo", "asset_count": 3},
]
DEMO_RECEIPTS = [
    {
        "title": "Issue Receipt - Signal Platoon - Apr 3, 2026",
        "status": "Queued",
        "recipient_email": "signal.platoon@example.demo",
        "receipt_key": "ISSUE-2026-0042",
    },
    {
        "title": "Return Receipt - Maintenance Shop - Apr 2, 2026",
        "status": "Sent",
        "recipient_email": "maintenance.shop@example.demo",
        "receipt_key": "RETURN-2026-0017",
    },
]
DEMO_AUDIT = [
    {"event_date": "2026-04-03T09:14:00Z", "event_type": "ISSUE", "asset_tag": "LT-4421", "actor": "operator-demo"},
    {"event_date": "2026-04-03T09:18:00Z", "event_type": "ISSUE", "asset_tag": "TB-1188", "actor": "operator-demo"},
    {"event_date": "2026-04-02T16:42:00Z", "event_type": "RETURN", "asset_tag": "LT-3010", "actor": "operator-demo"},
]
DEMO_RECEIPT_SEND_LIMIT = 2
DEMO_RECEIPT_COOLDOWN_SECONDS = 30
LOGIN_RATE_LIMIT_WINDOW_SECONDS = 5 * 60
LOGIN_RATE_LIMIT_MAX_FAILURES = 5
LOGIN_FAILURE_ATTEMPTS: dict[str, list[int]] = {}
ADMIN_ROUTE_RATE_LIMIT_WINDOW_SECONDS = 60
ADMIN_ROUTE_RATE_LIMIT_MAX_ACTIONS = 10
ADMIN_ROUTE_ATTEMPTS: dict[str, list[int]] = {}
PENDING_DB_RESTORE_SESSION_KEY = "pending_db_restore"


def _equipment_type_form_options(current_value: object = "") -> list[dict[str, object]]:
    current = str(current_value or "").strip()
    options = [
        {
            "value": equipment_type,
            "label": ASSET_EQUIPMENT_TYPE_LABELS[equipment_type],
            "is_legacy": False,
        }
        for equipment_type in ASSET_EQUIPMENT_TYPE_OPTIONS
    ]
    if current and current not in ASSET_EQUIPMENT_TYPE_OPTIONS:
        options.append(
            {
                "value": current,
                "label": f"{current} (existing value)",
                "is_legacy": True,
            }
        )
    return options


def _equipment_type_is_allowed(value: object, *, allow_current: object = "") -> bool:
    selected_raw = str(value or "").strip()
    selected = normalize_equipment_type(selected_raw)
    if not selected:
        return False
    if is_approved_new_equipment_type(selected):
        return True
    current_raw = str(allow_current or "").strip()
    return bool(current_raw) and selected_raw == current_raw


@app.after_request
def refresh_session_activity(response):
    if _should_refresh_session_activity():
        touch_session()
    if current_user() is not None:
        if response.headers.pop("X-AssetTrack-Sensitive-Reveal", None) == "1":
            response.headers["Cache-Control"] = "no-store, max-age=0"
        else:
            response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


@app.before_request
def enforce_required_password_change():
    endpoint = str(request.endpoint or "").strip()
    if endpoint in {"account_change_password", "account_change_password_submit", "logout", "static"}:
        return None

    user = current_user()
    if user is None or not is_temporary_password(user):
        return None

    if _prefers_json_error_response():
        return {"ok": False, "error": "Password change required"}, 403
    return redirect(url_for("account_change_password"))


@app.context_processor
def inject_auth_user():
    user = current_user()
    recovery_mode = _recovery_mode_context()
    return {
        "authenticated_user": user,
        "authenticated_role": None if user is None else user.get("role"),
        "password_change_required": is_temporary_password(user),
        "recovery_mode": recovery_mode,
        "case_status_summary": _case_status_summary,
        "asset_state_label": _asset_state_label,
        "asset_is_terminal": _is_terminal_location_type,
        "equipment_type_label": equipment_type_label,
        "holder_display_name": _holder_display_name,
        "holder_display_type": _holder_display_type,
        "format_duration_label": _format_duration_label,
        "session_absolute_timeout_seconds": SESSION_ABSOLUTE_TIMEOUT_SECONDS,
        "assettrack_version": ASSETTRACK_VERSION,
    }


# Helpers

def now_seconds() -> int:
    return int(time.time())


def _format_duration_label(total_seconds: object) -> str:
    try:
        seconds = max(0, int(total_seconds))
    except (TypeError, ValueError):
        return "0 seconds"

    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts: list[str] = []

    if hours:
        parts.append(f"{hours} hour" + ("" if hours == 1 else "s"))
    if minutes:
        parts.append(f"{minutes} minute" + ("" if minutes == 1 else "s"))
    if seconds or not parts:
        parts.append(f"{seconds} second" + ("" if seconds == 1 else "s"))

    return " ".join(parts)


def touch_session() -> None:
    session["last_seen"] = now_seconds()


def _clear_pending_db_restore(*, unlink_temp_file: bool = True) -> None:
    pending_restore = session.pop(PENDING_DB_RESTORE_SESSION_KEY, None)
    if not unlink_temp_file or not isinstance(pending_restore, dict):
        return

    temp_path_value = str(pending_restore.get("temp_path") or "").strip()
    if not temp_path_value:
        return
    Path(temp_path_value).unlink(missing_ok=True)


def _load_pending_db_restore() -> dict[str, object] | None:
    pending_restore = session.get(PENDING_DB_RESTORE_SESSION_KEY)
    if not isinstance(pending_restore, dict):
        return None

    temp_path_value = str(pending_restore.get("temp_path") or "").strip()
    if not temp_path_value:
        _clear_pending_db_restore(unlink_temp_file=False)
        return None

    temp_path = Path(temp_path_value)
    if not temp_path.exists() or not temp_path.is_file():
        _clear_pending_db_restore(unlink_temp_file=False)
        return None

    return pending_restore


def _should_refresh_session_activity() -> bool:
    if current_user() is None:
        return False

    endpoint = str(request.endpoint or "").strip()
    if endpoint in {"logout", "static"}:
        return False

    return True


def _prefers_json_error_response() -> bool:
    if request.is_json:
        return True

    best = request.accept_mimetypes.best_match(["application/json", "text/html"])
    return best == "application/json" and (
        request.accept_mimetypes["application/json"] >= request.accept_mimetypes["text/html"]
    )


@app.errorhandler(404)
def not_found_page(_error):
    if _prefers_json_error_response():
        return {"ok": False, "error": "Not Found"}, 404
    return render_template("404.html"), 404


def sanitize_scan(raw: str) -> str:
    """Keep only letters and numbers; drop tabs/newlines/suffix junk."""
    return "".join(ch for ch in raw if ch.isalnum()).upper()


def _identifier_lookup_key(value: object) -> str:
    return barcode_lookup_key(value)


def _single_identifier_match(
    rows: list[sqlite3.Row],
    *,
    label: str,
    input_value: str,
    row_key: str,
) -> Optional[sqlite3.Row]:
    if not rows:
        return None

    distinct_values = {str(row[row_key] or "") for row in rows}
    if len(distinct_values) > 1:
        raise ValueError(f"Ambiguous {label} match for scan '{input_value}'")

    return rows[0]


def _demo_page_context() -> dict[str, object]:
    demo_token = str(request.args.get("token") or "").strip()
    demo_send_enabled = _demo_token_is_valid(demo_token)
    return {
        "summary": DEMO_SUMMARY,
        "holders": DEMO_HOLDERS,
        "receipts": DEMO_RECEIPTS,
        "audit_rows": DEMO_AUDIT,
        "demo_token": demo_token if demo_send_enabled else "",
        "demo_send_enabled": demo_send_enabled,
        "demo_send_limit": DEMO_RECEIPT_SEND_LIMIT,
        "workflow_steps": [
            "Issue starts with the asset scan, then confirms holder and location prerequisites.",
            "Return stages assets back to storage and previews home-slot readiness before commit.",
            "Receipts and proof views show custody history; email delivery is notification only.",
            "Reports summarize stored custody state without replacing admin-only tools or backups.",
        ],
    }


def _configured_demo_tokens() -> set[str]:
    raw = str(os.getenv("ASSETTRACK_DEMO_TOKENS") or "").strip()
    if raw:
        return {
            token
            for token in (part.strip().upper() for part in raw.split(","))
            if token
        }

    legacy = str(os.getenv("ASSETTRACK_DEMO_TOKEN") or "").strip().upper()
    return {legacy} if legacy else set()


def _parse_demo_token(submitted_token: object) -> Optional[dict[str, object]]:
    normalized = str(submitted_token or "").strip().upper()
    if not normalized:
        return None

    match = re.fullmatch(r"([A-Z0-9-]+)\.(\d{8})\.EXP(\d{1,3})", normalized)
    if not match:
        return None

    recipient_key, issued_on_raw, ttl_raw = match.groups()
    ttl_days = int(ttl_raw)
    if ttl_days <= 0:
        return None

    try:
        invite_date = datetime.strptime(issued_on_raw, "%Y%m%d").date()
    except ValueError:
        return None

    today = datetime.now(timezone.utc).date()
    expires_on = invite_date + timedelta(days=ttl_days)
    return {
        "normalized_token": normalized,
        "recipient_key": recipient_key,
        "invite_date": invite_date,
        "ttl_days": ttl_days,
        "expires_on": expires_on,
        "is_expired": today >= expires_on,
    }


def _demo_token_is_valid(submitted_token: object) -> bool:
    parsed = _parse_demo_token(submitted_token)
    if parsed is None:
        return False

    configured = _configured_demo_tokens()
    if not configured:
        return False

    normalized = str(parsed["normalized_token"])
    return normalized in configured and not bool(parsed["is_expired"])


def _normalize_demo_email(email: object) -> str:
    normalized = str(email or "").strip().lower()
    if not normalized:
        raise ValueError("Enter an email address.")
    if len(normalized) > 254 or not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", normalized):
        raise ValueError("Enter a valid email address.")
    return normalized


def _demo_receipt_send_state() -> dict[str, object]:
    count = int(session.get("demo_receipt_send_count") or 0)
    last_sent_at = str(session.get("demo_receipt_last_sent_at") or "").strip()
    return {
        "count": max(0, count),
        "last_sent_at": last_sent_at,
    }


def _demo_receipt_sample() -> dict[str, object]:
    return {
        "title": "DEMO RECEIPT",
        "subtitle": "Sample receipt only. No operational data.",
        "receipt_key": "DEMO-RECEIPT-0001",
        "commit_at": "2026-04-03T09:18:00Z",
        "holder": "Signal Platoon",
        "organization": "Operations",
        "location": "HQ North / 210",
        "assets": ["LT-4421", "TB-1188"],
    }


def _demo_receipt_recorded_at_display(commit_at: object) -> str:
    raw = str(commit_at or "").strip()
    if not raw:
        return "unknown"
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%MZ")


def _build_demo_receipt_email_body(sample: dict[str, object]) -> str:
    recorded_at_display = _demo_receipt_recorded_at_display(sample.get("commit_at"))
    asset_lines = "\n".join(
        f"- {asset_tag} (recorded at {recorded_at_display})" for asset_tag in sample["assets"]
    )
    return (
        f"{sample['title']}\n"
        f"{sample['subtitle']}\n\n"
        f"Receipt key: {sample['receipt_key']}\n"
        f"Recorded at: {sample['commit_at']}\n"
        f"Holder: {sample['holder']}\n"
        f"Organization: {sample['organization']}\n"
        f"Location: {sample['location']}\n"
        f"Assets:\n{asset_lines}\n\n"
        f"This demo does not retain your email or any submitted data. All demo actions are stateless.\n\n"
        f"---\n"
        f"This is a demo receipt generated by AssetTrack. No operational data.\n"
    )


def _build_demo_receipt_pdf(sample: dict[str, object]) -> bytes:
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("DemoReceiptTitle", parent=styles["Heading1"], fontSize=18, leading=21, spaceAfter=6)
    subtitle_style = ParagraphStyle(
        "DemoReceiptSubtitle",
        parent=styles["BodyText"],
        textColor=colors.HexColor("#4f5d6b"),
        spaceAfter=10,
    )
    label_style = ParagraphStyle("DemoReceiptLabel", parent=styles["BodyText"], fontName="Helvetica-Bold", spaceAfter=4)
    body_style = ParagraphStyle("DemoReceiptBody", parent=styles["BodyText"], spaceAfter=4)

    def _text(value: object) -> str:
        return str(value or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    story: list[object] = [
        Paragraph(_text(sample["title"]), title_style),
        Paragraph(_text(sample["subtitle"]), subtitle_style),
        Paragraph("DEMO ONLY", label_style),
        Paragraph(f"Receipt key: {_text(sample['receipt_key'])}", body_style),
        Paragraph(f"Recorded at: {_text(sample['commit_at'])}", body_style),
        Paragraph(f"Holder: {_text(sample['holder'])}", body_style),
        Paragraph(f"Organization: {_text(sample['organization'])}", body_style),
        Paragraph(f"Location: {_text(sample['location'])}", body_style),
        Paragraph(
            "This demo does not retain your email or any submitted data. All demo actions are stateless.",
            body_style,
        ),
        Spacer(1, 0.12 * inch),
        Paragraph("Assets", label_style),
    ]
    recorded_at_display = _demo_receipt_recorded_at_display(sample.get("commit_at"))
    for asset_tag in sample["assets"]:
        story.append(Paragraph(f"- {_text(asset_tag)} (recorded at {_text(recorded_at_display)})", body_style))

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        leftMargin=0.6 * inch,
        rightMargin=0.6 * inch,
        topMargin=0.6 * inch,
        bottomMargin=0.6 * inch,
        title="AssetTrack Demo Receipt",
        author="AssetTrack",
    )

    def _invariant_canvas(*args, **kwargs):
        kwargs.setdefault("invariant", 1)
        return canvas.Canvas(*args, **kwargs)

    doc.build(story, canvasmaker=_invariant_canvas)
    pdf_bytes = buffer.getvalue()
    stable_digest = hashlib.md5(json.dumps(sample, sort_keys=True).encode("utf-8")).hexdigest().encode("ascii")
    return re.sub(
        rb"/ID\s*\[\s*<[^>]+>\s*<[^>]+>\s*\]",
        b"/ID [<" + stable_digest + b"><" + stable_digest + b">]",
        pdf_bytes,
        count=1,
    )


def _send_email_message(message: EmailMessage) -> None:
    smtp_host = str(os.getenv("ASSETTRACK_SMTP_HOST") or "").strip()
    if not smtp_host:
        raise ValueError("Receipt email delivery is not configured.")

    raw_smtp_port = str(os.getenv("ASSETTRACK_SMTP_PORT") or "25").strip() or "25"
    try:
        smtp_port = int(raw_smtp_port)
    except ValueError as exc:
        raise ValueError("SMTP port must be a number.") from exc
    smtp_username = str(os.getenv("ASSETTRACK_SMTP_USERNAME") or "").strip()
    smtp_password = str(os.getenv("ASSETTRACK_SMTP_PASSWORD") or "")
    smtp_starttls = str(os.getenv("ASSETTRACK_SMTP_STARTTLS") or "").strip().lower() in {"1", "true", "yes", "on"}
    smtp_use_ssl = str(os.getenv("ASSETTRACK_SMTP_USE_SSL") or "").strip().lower() in {"1", "true", "yes", "on"}

    if smtp_starttls and smtp_use_ssl:
        raise ValueError("SMTP TLS config is invalid: use STARTTLS or implicit SSL, not both.")
    if smtp_port == 465 and not smtp_use_ssl:
        raise ValueError("SMTP TLS config is invalid for port 465: set ASSETTRACK_SMTP_USE_SSL=true.")
    if smtp_port in {587, 2525} and smtp_use_ssl:
        raise ValueError(f"SMTP TLS config is invalid for port {smtp_port}: use STARTTLS, not implicit SSL.")

    smtp_cls = smtplib.SMTP_SSL if smtp_use_ssl else smtplib.SMTP
    with smtp_cls(smtp_host, smtp_port, timeout=10) as smtp:
        if smtp_starttls and not smtp_use_ssl:
            smtp.starttls()
        if smtp_username:
            smtp.login(smtp_username, smtp_password)
        smtp.send_message(message)


def _send_demo_receipt_email(recipient_email: str) -> str:
    sample = _demo_receipt_sample()
    from_address = str(os.getenv("ASSETTRACK_RECEIPT_FROM_EMAIL") or "assettrack@local").strip() or "assettrack@local"
    message = EmailMessage()
    message["Subject"] = "DEMO RECEIPT - AssetTrack sample"
    message["From"] = from_address
    message["To"] = recipient_email
    message.set_content(_build_demo_receipt_email_body(sample))
    message.add_attachment(
        _build_demo_receipt_pdf(sample),
        maintype="application",
        subtype="pdf",
        filename="AssetTrack DEMO RECEIPT.pdf",
    )
    _send_email_message(message)
    return recipient_email


def _send_holder_followup_email(*, holder: dict, note: str, actor_username: str) -> str:
    recipient_email = str(holder.get("email") or "").strip().lower()
    if not recipient_email:
        raise ValueError("Holder follow-up email requires a holder email address.")

    smtp_host = str(os.getenv("ASSETTRACK_SMTP_HOST") or "").strip()
    if not smtp_host:
        raise ValueError("Follow-up email delivery is not configured.")

    holder_name = _holder_display_name(holder) or "Holder"
    organization = str(holder.get("organization") or "").strip()
    identifier = str(holder.get("identifier") or "").strip()

    subject = f"AssetTrack Holder Follow-Up: {holder_name}"
    lines = [
        f"Holder follow-up for: {holder_name}",
    ]
    if organization:
        lines.append(f"Organization: {organization}")
    if identifier:
        lines.append(f"Identifier: {identifier}")
    lines.extend(
        [
            "",
            "This is a manual follow-up reminder from an operator.",
            "It does not record, prove, or change custody.",
        ]
    )
    if note:
        lines.extend(["", "Operator note:", note])
    lines.extend(["", f"Sent by: {actor_username}"])

    from_address = str(os.getenv("ASSETTRACK_RECEIPT_FROM_EMAIL") or "assettrack@local").strip() or "assettrack@local"
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = from_address
    message["To"] = recipient_email
    message.set_content("\n".join(lines))
    _send_email_message(message)
    return recipient_email


def _queue_contains_asset_tag(asset_tag: str) -> bool:
    normalized = sanitize_scan(asset_tag or "")
    if not normalized:
        return False

    for queued in SCAN_QUEUE:
        if sanitize_scan(queued.asset_tag or "") == normalized:
            return True
    return False


def auth_enabled() -> bool:
    return False


def is_authed() -> bool:
    return current_user() is not None


def set_authed(value: bool) -> None:
    if value:
        return
    clear_auth_session()


def auth_ok(submitted: str | None) -> bool:
    return False


def enforce_inactivity_timeout() -> bool:
    return is_authed()


def seconds_since_last_seen() -> Optional[int]:
    last_seen = session.get("last_seen")
    if last_seen is None:
        return None
    return max(0, now_seconds() - int(last_seen))


def _login_rate_limit_key() -> str:
    remote_addr = str(request.remote_addr or "unknown").strip() or "unknown"
    username = (request.form.get("username") or "").strip().lower()
    if username:
        return f"{remote_addr}|{username}"
    return remote_addr


def _prune_login_failures(rate_limit_key: str, *, current_time: Optional[int] = None) -> list[int]:
    now = now_seconds() if current_time is None else int(current_time)
    attempts = LOGIN_FAILURE_ATTEMPTS.get(rate_limit_key, [])
    window_start = now - LOGIN_RATE_LIMIT_WINDOW_SECONDS
    pruned = [attempt for attempt in attempts if attempt > window_start]
    if pruned:
        LOGIN_FAILURE_ATTEMPTS[rate_limit_key] = pruned
    else:
        LOGIN_FAILURE_ATTEMPTS.pop(rate_limit_key, None)
    return pruned


def _login_is_rate_limited(rate_limit_key: str) -> bool:
    attempts = _prune_login_failures(rate_limit_key)
    return len(attempts) >= LOGIN_RATE_LIMIT_MAX_FAILURES


def _record_login_failure(rate_limit_key: str) -> None:
    attempts = _prune_login_failures(rate_limit_key)
    attempts.append(now_seconds())
    LOGIN_FAILURE_ATTEMPTS[rate_limit_key] = attempts


def _clear_login_failures(rate_limit_key: str) -> None:
    LOGIN_FAILURE_ATTEMPTS.pop(rate_limit_key, None)


def _admin_route_rate_limit_key() -> Optional[str]:
    user = current_user()
    endpoint = str(request.endpoint or "").strip()
    if user is None or not endpoint:
        return None
    return f"{int(user['id'])}|{endpoint}"


def _prune_admin_route_attempts(rate_limit_key: str, *, current_time: Optional[int] = None) -> list[int]:
    now = now_seconds() if current_time is None else int(current_time)
    attempts = ADMIN_ROUTE_ATTEMPTS.get(rate_limit_key, [])
    window_start = now - ADMIN_ROUTE_RATE_LIMIT_WINDOW_SECONDS
    pruned = [attempt for attempt in attempts if attempt > window_start]
    if pruned:
        ADMIN_ROUTE_ATTEMPTS[rate_limit_key] = pruned
    else:
        ADMIN_ROUTE_ATTEMPTS.pop(rate_limit_key, None)
    return pruned


def _consume_admin_route_rate_limit_slot() -> bool:
    rate_limit_key = _admin_route_rate_limit_key()
    if not rate_limit_key:
        return True

    attempts = _prune_admin_route_attempts(rate_limit_key)
    if len(attempts) >= ADMIN_ROUTE_RATE_LIMIT_MAX_ACTIONS:
        return False

    attempts.append(now_seconds())
    ADMIN_ROUTE_ATTEMPTS[rate_limit_key] = attempts
    return True


def _enforce_admin_route_rate_limit(*, html_redirect_endpoint: Optional[str] = None):
    if _consume_admin_route_rate_limit_slot():
        return None

    message = "Too many requests. Wait and try again."
    if html_redirect_endpoint:
        flash(message, "error")
        return redirect(url_for(html_redirect_endpoint))
    return {"ok": False, "error": message}, 429


def build_parsed_rows_from_queue() -> list[dict]:
    """
    Build rows in the validator/committer format:
      [{"row_number": 1, "data": {...}}, ...]
    Each queued Scan already carries its own equipment_type.
    """
    rows: list[dict] = []

    for idx, s in enumerate(SCAN_QUEUE):
        data = scan_to_ingest_row(s)
        rows.append({"row_number": idx + 1, "data": data})

    return rows


def wants_json() -> bool:
    """
    Simple switch so curl/automation can still get JSON:
      /preview?json=1
      /preview/commit?json=1 (POST)
    """
    return (request.args.get("json") or "").strip() == "1"


def _normalize_location_type(value: object) -> str:
    return str(value or "").strip().upper()


def _is_terminal_location_type(value: object) -> bool:
    return _normalize_location_type(value) in TERMINAL_LOCATION_TYPES


def _get_event_by_id(conn: sqlite3.Connection, event_id: int) -> dict | None:
    row = conn.execute(
        """
        SELECT
            id,
            asset_tag,
            event_type,
            event_date,
            actor,
            notes,
            payload,
            supersedes_event_id,
            correction_reason
        FROM asset_events
        WHERE id = ?;
        """,
        (event_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def _event_already_superseded(conn: sqlite3.Connection, event_id: int) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM asset_events
        WHERE supersedes_event_id = ?
        LIMIT 1;
        """,
        (event_id,),
    ).fetchone()
    return row is not None


def _find_asset_for_scan_tag(conn, scan_tag: str) -> Optional[dict]:
    t = (scan_tag or "").strip()
    if not t:
        return None

    rows = conn.execute(
        """
        SELECT *
        FROM assets
        WHERE UPPER(asset_tag) = UPPER(?)
        LIMIT 2;
        """,
        (t,),
    ).fetchall()

    row = _single_identifier_match(rows, label="asset_tag", input_value=t, row_key="asset_tag")
    if row is not None:
        return dict(row)

    rows = conn.execute(
        """
        SELECT *
        FROM assets
        WHERE REPLACE(REPLACE(UPPER(asset_tag), '-', ''), ' ', '') = ?
        LIMIT 2;
        """,
        (_identifier_lookup_key(t),),
    ).fetchall()

    row = _single_identifier_match(rows, label="asset_tag", input_value=t, row_key="asset_tag")
    return None if row is None else dict(row)


def _find_case_assets_for_scan_tag(conn, scan_tag: str) -> Optional[dict]:
    t = (scan_tag or "").strip()
    if not t:
        return None

    slot_rows = conn.execute(
        """
        SELECT id, case_name, slot_position, current_asset_tag
        FROM slots
        WHERE UPPER(case_name) = UPPER(?)
        ORDER BY slot_position ASC, id ASC;
        """,
        (t,),
    ).fetchall()
    if not slot_rows:
        slot_rows = conn.execute(
            """
            SELECT id, case_name, slot_position, current_asset_tag
            FROM slots
            WHERE REPLACE(REPLACE(UPPER(case_name), '-', ''), ' ', '') = ?
            ORDER BY slot_position ASC, id ASC;
            """,
            (_identifier_lookup_key(t),),
        ).fetchall()
    if not slot_rows:
        return None

    case_names = {str(row["case_name"] or "").strip().upper() for row in slot_rows if str(row["case_name"] or "").strip()}
    if len(case_names) > 1:
        raise ValueError(f"Ambiguous case match for scan '{t}'")

    case_name = str(slot_rows[0]["case_name"] or "").strip().upper()
    assets: list[dict] = []
    seen_asset_tags: set[str] = set()

    for slot_row in slot_rows:
        slot_id = int(slot_row["id"])
        slot_position = int(slot_row["slot_position"])
        asset_tag = ""

        occupancy_row = conn.execute(
            """
            SELECT a.asset_tag
            FROM slot_occupancy so
            JOIN assets a ON a.id = so.asset_id
            WHERE so.slot_id = ?
            ORDER BY so.id ASC
            LIMIT 1;
            """,
            (slot_id,),
        ).fetchone()
        if occupancy_row is not None:
            asset_tag = sanitize_scan(str(occupancy_row["asset_tag"] or ""))
        else:
            legacy_asset_tag = str(slot_row["current_asset_tag"] or "").strip()
            if legacy_asset_tag:
                asset_row = _find_asset_for_scan_tag(conn, legacy_asset_tag)
                if asset_row is not None:
                    asset_tag = sanitize_scan(str(asset_row["asset_tag"] or ""))

        if not asset_tag or asset_tag in seen_asset_tags:
            continue

        seen_asset_tags.add(asset_tag)
        assets.append(
            {
                "asset_tag": asset_tag,
                "home_slot_id": slot_id,
                "case_name": case_name,
                "slot_position": slot_position,
            }
        )

    return {
        "case_name": case_name,
        "assets": assets,
    }


def _find_return_case_assets_for_scan_tag(conn, scan_tag: str) -> Optional[dict]:
    t = (scan_tag or "").strip()
    if not t:
        return None

    slot_rows = conn.execute(
        """
        SELECT id, case_name, slot_position
        FROM slots
        WHERE UPPER(case_name) = UPPER(?)
        ORDER BY slot_position ASC, id ASC;
        """,
        (t,),
    ).fetchall()
    if not slot_rows:
        slot_rows = conn.execute(
            """
            SELECT id, case_name, slot_position
            FROM slots
            WHERE REPLACE(REPLACE(UPPER(case_name), '-', ''), ' ', '') = ?
            ORDER BY slot_position ASC, id ASC;
            """,
            (_identifier_lookup_key(t),),
        ).fetchall()
    if not slot_rows:
        return None

    case_names = {str(row["case_name"] or "").strip().upper() for row in slot_rows if str(row["case_name"] or "").strip()}
    if len(case_names) > 1:
        raise ValueError(f"Ambiguous case match for scan '{t}'")

    case_name = str(slot_rows[0]["case_name"] or "").strip().upper()
    assets: list[dict] = []
    seen_asset_tags: set[str] = set()

    for slot_row in slot_rows:
        slot_id = int(slot_row["id"])
        slot_position = int(slot_row["slot_position"])
        asset_rows = conn.execute(
            """
            SELECT asset_tag
            FROM assets
            WHERE home_slot_id = ?
              AND UPPER(COALESCE(location_type, '')) = 'IN_CUSTODY'
            ORDER BY UPPER(asset_tag) ASC, id ASC;
            """,
            (slot_id,),
        ).fetchall()

        for asset_row in asset_rows:
            asset_tag = sanitize_scan(str(asset_row["asset_tag"] or ""))
            if not asset_tag or asset_tag in seen_asset_tags:
                continue

            seen_asset_tags.add(asset_tag)
            assets.append(
                {
                    "asset_tag": asset_tag,
                    "home_slot_id": slot_id,
                    "case_name": case_name,
                    "slot_position": slot_position,
                }
            )

    return {
        "case_name": case_name,
        "assets": assets,
    }


def _selected_case_asset_tags() -> list[str]:
    seen: set[str] = set()
    selected: list[str] = []
    for raw in request.form.getlist("asset_tag"):
        normalized = sanitize_scan(str(raw or ""))
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        selected.append(str(raw or "").strip())
    return selected


def _case_detail_existing_queue_workflow() -> str:
    if not SCAN_QUEUE:
        return ""

    session_workflow = str(session.get(CASE_DETAIL_QUEUE_WORKFLOW_SESSION_KEY) or "").strip().lower()
    if session_workflow in {"issue", "return"}:
        return session_workflow

    if bool(session.get("issue_mode")):
        return "issue"

    return ""


def _queue_case_detail_issue_selection(conn: sqlite3.Connection, case_name: str, selected_tags: list[str]) -> tuple[int, list[str]]:
    invalid: list[str] = []
    queued_rows: list[dict[str, object]] = []

    for selected_tag in selected_tags:
        normalized = sanitize_scan(selected_tag)
        row = conn.execute(
            """
            SELECT
                a.id AS asset_id,
                a.asset_tag,
                a.location_type,
                s.id AS slot_id,
                s.case_name,
                s.slot_position
            FROM assets a
            JOIN slot_occupancy so
              ON so.asset_id = a.id
            JOIN slots s
              ON s.id = so.slot_id
            WHERE s.case_name = ?
              AND (
                UPPER(a.asset_tag) = UPPER(?)
                OR REPLACE(REPLACE(UPPER(a.asset_tag), '-', ''), ' ', '') = UPPER(?)
              )
            LIMIT 1;
            """,
            (case_name, selected_tag, normalized),
        ).fetchone()
        if row is None:
            invalid.append(normalized)
            continue

        location_type = str(row["location_type"] or "").strip().upper()
        if location_type != "STORAGE" or _is_terminal_location_type(location_type):
            invalid.append(str(row["asset_tag"] or normalized))
            continue

        queued_rows.append(dict(row))

    if invalid:
        return 0, invalid

    added_count = 0
    for row in queued_rows:
        asset_tag = str(row["asset_tag"] or "").strip().upper()
        if _queue_contains_asset_tag(asset_tag):
            continue
        SCAN_QUEUE.append(
            Scan.now(
                asset_tag,
                equipment_type=(session.get("equipment_type") or "laptop").strip() or "laptop",
                home_slot_id=int(row["slot_id"]),
                case_name=str(row["case_name"] or ""),
                slot_position=int(row["slot_position"]),
            )
        )
        added_count += 1

    return added_count, []


def _queue_case_detail_return_selection(conn: sqlite3.Connection, case_name: str, selected_tags: list[str]) -> tuple[int, list[str]]:
    invalid: list[str] = []
    queued_rows: list[dict[str, object]] = []

    for selected_tag in selected_tags:
        normalized = sanitize_scan(selected_tag)
        row = conn.execute(
            """
            SELECT
                a.id AS asset_id,
                a.asset_tag,
                a.location_type,
                a.home_slot_id,
                s.case_name,
                s.slot_position,
                s.current_asset_tag,
                so.asset_id AS occupied_asset_id
            FROM assets a
            JOIN slots s
              ON s.id = a.home_slot_id
            LEFT JOIN slot_occupancy so
              ON so.slot_id = s.id
            WHERE s.case_name = ?
              AND (
                UPPER(a.asset_tag) = UPPER(?)
                OR REPLACE(REPLACE(UPPER(a.asset_tag), '-', ''), ' ', '') = UPPER(?)
              )
            LIMIT 1;
            """,
            (case_name, selected_tag, normalized),
        ).fetchone()
        if row is None:
            invalid.append(normalized)
            continue

        location_type = str(row["location_type"] or "").strip().upper()
        home_slot_occupied = row["current_asset_tag"] is not None or row["occupied_asset_id"] is not None
        if location_type != "IN_CUSTODY" or _is_terminal_location_type(location_type) or home_slot_occupied:
            invalid.append(str(row["asset_tag"] or normalized))
            continue

        queued_rows.append(dict(row))

    if invalid:
        return 0, invalid

    added_count = 0
    for row in queued_rows:
        asset_tag = str(row["asset_tag"] or "").strip().upper()
        if _queue_contains_asset_tag(asset_tag):
            continue
        SCAN_QUEUE.append(
            Scan.now(
                asset_tag,
                equipment_type=(session.get("equipment_type") or "laptop").strip() or "laptop",
                home_slot_id=int(row["home_slot_id"]),
                case_name=str(row["case_name"] or ""),
                slot_position=int(row["slot_position"]),
            )
        )
        added_count += 1

    return added_count, []


def _asset_current_slot(conn, asset_id: int, asset_tag: str) -> Optional[dict]:
    occupancy_row = conn.execute(
        """
        SELECT s.id AS slot_id, s.case_name, s.slot_position
        FROM slot_occupancy so
        JOIN slots s ON s.id = so.slot_id
        WHERE so.asset_id = ?
        LIMIT 1;
        """,
        (asset_id,),
    ).fetchone()
    if occupancy_row:
        return dict(occupancy_row)

    legacy_row = conn.execute(
        """
        SELECT id AS slot_id, case_name, slot_position
        FROM slots
        WHERE UPPER(current_asset_tag) = UPPER(?)
           OR REPLACE(REPLACE(UPPER(current_asset_tag), '-', ''), ' ', '') = UPPER(?)
        LIMIT 1;
        """,
        (asset_tag, asset_tag),
    ).fetchone()
    return dict(legacy_row) if legacy_row else None


def _asset_home_slot(conn, home_slot_id: object) -> Optional[dict]:
    if home_slot_id is None:
        return None
    row = conn.execute(
        """
        SELECT id AS slot_id, case_name, slot_position
        FROM slots
        WHERE id = ?
        LIMIT 1;
        """,
        (int(home_slot_id),),
    ).fetchone()
    return dict(row) if row else None


def _slot_occupancy_status(conn, slot_id: object) -> Optional[dict]:
    row = conn.execute(
        """
        SELECT
            s.id,
            s.case_name,
            s.slot_position,
            s.current_asset_tag,
            so.asset_id AS occupied_asset_id,
            a.asset_tag AS occupied_asset_tag
        FROM slots s
        LEFT JOIN slot_occupancy so ON so.slot_id = s.id
        LEFT JOIN assets a ON a.id = so.asset_id
        WHERE s.id = ?
        LIMIT 1;
        """,
        (int(slot_id),),
    ).fetchone()
    if row is None:
        return None

    marker_present = row["current_asset_tag"] is not None
    occupied_by = str(row["occupied_asset_tag"] or row["current_asset_tag"] or "").strip()
    if not occupied_by and row["occupied_asset_id"] is not None:
        occupied_by = f"asset_id {row['occupied_asset_id']}"

    return {
        "id": int(row["id"]),
        "case_name": str(row["case_name"] or ""),
        "slot_position": int(row["slot_position"]),
        "current_asset_tag": row["current_asset_tag"],
        "occupied_asset_id": row["occupied_asset_id"],
        "occupied_asset_tag": str(row["occupied_asset_tag"] or ""),
        "occupied": bool(row["occupied_asset_id"] is not None or marker_present),
        "occupied_by": occupied_by or "unknown asset",
    }


def _list_slot_options(conn, *, empty_only: bool = False) -> list[dict]:
    occupancy_filter = """
        WHERE so.asset_id IS NULL
          AND TRIM(COALESCE(s.current_asset_tag, '')) = ''
    """ if empty_only else ""
    rows = conn.execute(
        f"""
        SELECT
            s.id,
            s.case_name,
            s.slot_position,
            so.asset_id AS occupied_asset_id,
            a.asset_tag AS occupied_asset_tag,
            s.current_asset_tag AS legacy_asset_tag
        FROM slots s
        LEFT JOIN slot_occupancy so ON so.slot_id = s.id
        LEFT JOIN assets a ON a.id = so.asset_id
        {occupancy_filter}
        ORDER BY UPPER(s.case_name) ASC, s.slot_position ASC, s.id ASC;
        """
    ).fetchall()
    slot_options = [
        {
            "id": int(row["id"]),
            "case_name": str(row["case_name"] or ""),
            "slot_position": int(row["slot_position"]),
            "occupied_asset_id": None if row["occupied_asset_id"] is None else int(row["occupied_asset_id"]),
            "occupied_asset_tag": str(row["occupied_asset_tag"] or row["legacy_asset_tag"] or ""),
        }
        for row in rows
    ]
    return sorted(
        slot_options,
        key=lambda row: (
            natural_identifier_sort_key(row["case_name"]),
            natural_identifier_sort_key(row["slot_position"]),
            int(row["id"]),
        ),
    )


def _slot_case_options(slot_options: list[dict]) -> list[str]:
    case_names = {str(row["case_name"]) for row in slot_options if str(row["case_name"]).strip()}
    return sorted(case_names, key=natural_identifier_sort_key)

def _resolve_slot_selection(
    conn: sqlite3.Connection,
    *,
    case_name: str,
    slot_id_raw: str,
) -> tuple[Optional[dict], list[str]]:
    case_name_clean = str(case_name or "").strip().upper()
    slot_id_text = str(slot_id_raw or "").strip()
    errors: list[str] = []

    if bool(case_name_clean) != bool(slot_id_text):
        errors.append("case and slot must both be selected.")
        return None, errors

    if not case_name_clean and not slot_id_text:
        return None, errors

    try:
        slot_id = int(slot_id_text)
    except ValueError:
        errors.append("slot selection is invalid.")
        return None, errors

    slot_row = conn.execute(
        """
        SELECT id, case_name, slot_position, current_asset_tag
        FROM slots
        WHERE id = ?
        LIMIT 1;
        """,
        (slot_id,),
    ).fetchone()
    if slot_row is None:
        errors.append("selected slot does not exist.")
        return None, errors

    resolved_case = str(slot_row["case_name"] or "").strip().upper()
    if resolved_case != case_name_clean:
        errors.append("selected slot does not belong to the selected case.")
        return None, errors

    return dict(slot_row), errors


def _validate_admin_new_asset_form(
    conn: sqlite3.Connection,
    form_state: dict[str, str],
) -> tuple[Optional[dict], list[str]]:
    errors: list[str] = []

    if not form_state["asset_tag"]:
        errors.append("Enter an asset tag.")
    if not form_state["serial_number"]:
        errors.append("Enter a serial number.")
    if not form_state["equipment_type"]:
        errors.append("Choose an asset type.")
    else:
        try:
            form_state["equipment_type"] = validate_new_equipment_type(form_state["equipment_type"])
        except ValueError as exc:
            errors.append(str(exc))
    selected_slot, slot_errors = _resolve_slot_selection(
        conn,
        case_name=form_state["case_name"],
        slot_id_raw=form_state["slot_id"],
    )
    slot_error_map = {
        "case and slot must both be selected.": "Choose both a case and a slot, or leave both blank.",
        "slot selection is invalid.": "Choose a valid slot.",
        "selected slot does not exist.": "Choose a slot that exists.",
        "selected slot does not belong to the selected case.": "Choose a slot in the selected case.",
    }
    errors.extend(slot_error_map.get(error, error) for error in slot_errors)

    return selected_slot, errors


def _humanize_admin_asset_create_error(error_message: str) -> str:
    error_map = {
        "asset_tag already exists.": "Asset tag already exists.",
        "serial_number already exists.": "Serial number already exists.",
        "Slot not found for case_number + slot_number.": "The selected slot no longer exists.",
        "Selected slot is already occupied.": "The selected slot is already occupied.",
    }
    return error_map.get(error_message, error_message)


def _asset_state_label(location_type: object) -> str:
    normalized = _normalize_location_type(location_type)
    if normalized == "STORAGE":
        return "In storage"
    if normalized == "IN_CUSTODY":
        return "In custody"
    if normalized in TERMINAL_LOCATION_TYPES:
        return "RETIRED — Not in service"
    if not normalized:
        return "Unknown"
    return normalized.replace("_", " ").title()


def _resolved_runtime_db_path() -> Path:
    return db_module.DB_PATH.expanduser().resolve()


def _recovery_mode_context() -> dict[str, object]:
    state = load_recovery_state(_resolved_runtime_db_path())
    restored_at = str(state.get("restored_at") or "").strip()
    return {
        **state,
        "status_label": "Active" if state.get("active") else "Inactive",
        "acknowledgment_label": "Required" if state.get("acknowledgment_required") else "Cleared",
        "restored_at_display": _receipt_display_timestamp(restored_at),
    }


def _recovery_mode_send_block_message() -> str:
    return "Receipt resend is blocked during recovery mode. Admin acknowledgment is required before email delivery resumes."


def _restore_history_context() -> dict[str, object]:
    recovery_state = load_recovery_state(_resolved_runtime_db_path())
    history_data = load_restore_history(_resolved_runtime_db_path())
    active_restored_at = str(recovery_state.get("restored_at") or "").strip()
    entries: list[dict[str, object]] = []
    for entry in reversed(list(history_data.get("entries") or [])):
        restored_at = str(entry.get("restored_at") or "").strip()
        acknowledgment_state = "Cleared"
        if recovery_state.get("active") and active_restored_at and restored_at == active_restored_at:
            acknowledgment_state = "Required"
        entries.append(
            {
                "restored_at": restored_at,
                "restored_at_display": _receipt_display_timestamp(restored_at),
                "source_filename": str(entry.get("source_filename") or "").strip(),
                "rollback_db_path": str(entry.get("rollback_db_path") or "").strip(),
                "result": str(entry.get("result") or "unknown").strip().title() or "Unknown",
                "acknowledgment_state": acknowledgment_state,
            }
        )
    return {
        "entries": entries,
        "parse_error": str(history_data.get("parse_error") or "").strip(),
        "history_path": str(history_data.get("history_path") or restore_history_path_for(_resolved_runtime_db_path())),
    }


def _case_status_summary(total_slots: object, occupied_slots: object) -> dict[str, object]:
    total = int(total_slots or 0)
    occupied = int(occupied_slots or 0)
    available = max(0, total - occupied)
    utilization_percent = int((occupied * 100.0 / total) + 0.5) if total > 0 else 0

    if available == 0:
        return {
            "label": "FULL",
            "text": "FULL - No space",
            "class_name": "full",
            "available_slots": available,
            "utilization_percent": utilization_percent,
        }
    if available <= 3:
        return {
            "label": "LOW",
            "text": "LOW - Getting tight",
            "class_name": "low",
            "available_slots": available,
            "utilization_percent": utilization_percent,
        }
    return {
        "label": "OPEN",
        "text": "OPEN - Use now",
        "class_name": "open",
        "available_slots": available,
        "utilization_percent": utilization_percent,
    }


def _queue_redirect_target(return_to: str) -> str:
    target = str(return_to or "").strip()
    if not target.startswith("/") or target.startswith("//"):
        return target

    path, sep, fragment = target.partition("#")
    if path == "/issue" and not fragment:
        return f"{path}#queue-actions"
    if path == "/return" and not fragment:
        return f"{path}#queue-section"
    return target


def _return_to_path(return_to: str) -> str:
    target = str(return_to or "").strip()
    if not target.startswith("/") or target.startswith("//"):
        return target
    path, _, _ = target.partition("#")
    return path


def _safe_local_return_to(return_to: str) -> str | None:
    target = str(return_to or "").strip()
    if target.startswith("/") and not target.startswith("//"):
        return target
    return None


def _safe_report_return_to(return_to: str) -> str | None:
    target = _safe_local_return_to(return_to)
    if target and target.startswith("/report"):
        return target
    return None


def _safe_receipt_context_return_to(return_to: str) -> str | None:
    target = _safe_local_return_to(return_to)
    if target and (target.startswith("/report") or target.startswith("/assets/search")):
        return target
    return None


def _holder_form_error_message(exc: ValueError) -> str:
    message = str(exc)
    if message == "organization is required":
        return "Choose an organization for this holder."
    if message == "name is required":
        return "Enter a person or group name when using Ad Hoc."
    if message == "email is required":
        return "Enter an email address so this holder can receive receipts."
    if message == "email already exists":
        return "A holder with that email already exists."
    if message == "email is invalid":
        return "Enter a valid email address so this holder can receive receipts."
    return message


def _holder_display_name(holder: Optional[dict]) -> str:
    if not holder:
        return ""

    name = str(holder.get("name") or "").strip()
    organization = str(holder.get("organization") or "").strip()
    return name or organization


def _holder_display_type(holder: Optional[dict]) -> str:
    if not holder:
        return ""

    holder_type = str(holder.get("holder_type") or "").strip().upper()
    if holder_type == "ORGANIZATION":
        return "Group / organization"
    if holder_type == "PERSON":
        return "Person"
    return holder_type.replace("_", " ").title()


def _lookup_asset_for_verification(
    conn: sqlite3.Connection,
    *,
    asset_tag: str,
    serial_number: str,
) -> tuple[list[dict], Optional[str], str]:
    asset_tag_clean = str(asset_tag or "").strip().upper()
    asset_tag_key = _identifier_lookup_key(asset_tag_clean)
    serial_clean = str(serial_number or "").strip()
    has_asset_tag = bool(asset_tag_clean)
    has_serial_number = bool(serial_clean)

    if not has_asset_tag and not has_serial_number:
        return [], "Enter an asset tag or serial number.", "none"

    if has_asset_tag and has_serial_number:
        lookup_mode = "asset_tag"
        rows = conn.execute(
            """
            SELECT
                a.*,
                h.id AS holder_record_id,
                h.holder_type AS holder_record_type,
                h.name AS holder_record_name,
                h.organization AS holder_record_organization
            FROM assets a
            LEFT JOIN holders h
              ON h.id = a.current_holder_id
            WHERE (
                UPPER(a.asset_tag) LIKE UPPER(?)
                OR REPLACE(REPLACE(UPPER(a.asset_tag), '-', ''), ' ', '') = ?
            )
              AND TRIM(COALESCE(a.serial_number, '')) <> ''
              AND UPPER(a.serial_number) LIKE UPPER(?)
            ORDER BY
                CASE
                    WHEN UPPER(a.asset_tag) = UPPER(?) AND UPPER(a.serial_number) = UPPER(?) THEN 0
                    WHEN REPLACE(REPLACE(UPPER(a.asset_tag), '-', ''), ' ', '') = ? AND UPPER(a.serial_number) = UPPER(?) THEN 1
                    WHEN UPPER(a.asset_tag) = UPPER(?) THEN 2
                    WHEN REPLACE(REPLACE(UPPER(a.asset_tag), '-', ''), ' ', '') = ? THEN 3
                    WHEN UPPER(a.serial_number) = UPPER(?) THEN 4
                    ELSE 5
                END,
                UPPER(a.asset_tag) ASC,
                UPPER(a.serial_number) ASC,
                a.id ASC
            LIMIT 25;
            """,
            (
                f"%{asset_tag_clean}%",
                asset_tag_key,
                f"%{serial_clean}%",
                asset_tag_clean,
                serial_clean,
                asset_tag_key,
                serial_clean,
                asset_tag_clean,
                asset_tag_key,
                serial_clean,
            ),
        ).fetchall()
        if not rows:
            return [], "Asset not found.", lookup_mode
    elif has_asset_tag:
        lookup_mode = "asset_tag"
        like_pattern = f"%{asset_tag_clean}%"
        rows = conn.execute(
            """
            SELECT
                a.*,
                h.id AS holder_record_id,
                h.holder_type AS holder_record_type,
                h.name AS holder_record_name,
                h.organization AS holder_record_organization
            FROM assets a
            LEFT JOIN holders h
              ON h.id = a.current_holder_id
            WHERE UPPER(a.asset_tag) LIKE UPPER(?)
               OR REPLACE(REPLACE(UPPER(a.asset_tag), '-', ''), ' ', '') = ?
            ORDER BY
                CASE
                    WHEN UPPER(a.asset_tag) = UPPER(?) THEN 0
                    WHEN REPLACE(REPLACE(UPPER(a.asset_tag), '-', ''), ' ', '') = ? THEN 1
                    ELSE 2
                END,
                UPPER(a.asset_tag) ASC,
                a.id ASC
            LIMIT 25;
            """,
            (like_pattern, asset_tag_key, asset_tag_clean, asset_tag_key),
        ).fetchall()
        if not rows:
            return [], "Asset not found.", lookup_mode
    else:
        lookup_mode = "serial_number"
        like_pattern = f"%{serial_clean}%"
        rows = conn.execute(
            """
            SELECT
                a.*,
                h.id AS holder_record_id,
                h.holder_type AS holder_record_type,
                h.name AS holder_record_name,
                h.organization AS holder_record_organization
            FROM assets a
            LEFT JOIN holders h
              ON h.id = a.current_holder_id
            WHERE TRIM(COALESCE(serial_number, '')) <> ''
              AND UPPER(a.serial_number) LIKE UPPER(?)
            ORDER BY
                CASE WHEN UPPER(a.serial_number) = UPPER(?) THEN 0 ELSE 1 END,
                UPPER(a.serial_number) ASC,
                UPPER(a.asset_tag) ASC,
                a.id ASC
            LIMIT 25;
            """,
            (like_pattern, serial_clean),
        ).fetchall()
        if not rows:
            return [], "Asset not found.", lookup_mode

    results: list[dict] = []
    for raw_row in rows:
        asset = dict(raw_row)
        home_slot = _asset_home_slot(conn, asset.get("home_slot_id"))
        current_slot = _asset_current_slot(conn, int(asset["id"]), str(asset.get("asset_tag") or ""))
        holder_row = None
        if asset.get("holder_record_id") is not None:
            holder_row = {
                "id": int(asset["holder_record_id"]),
                "holder_type": str(asset.get("holder_record_type") or ""),
                "name": str(asset.get("holder_record_name") or ""),
                "organization": str(asset.get("holder_record_organization") or ""),
            }
        holder_label = ""
        if holder_row is not None:
            holder_name = str(holder_row.get("name") or "").strip()
            holder_org = str(holder_row.get("organization") or "").strip()
            if holder_name and holder_org and holder_org != holder_name:
                holder_label = f"{holder_name} ({holder_org})"
            else:
                holder_label = _holder_display_name(holder_row)

        location_type = _normalize_location_type(asset.get("location_type"))
        equipment_type = str(asset.get("equipment_type") or "").strip()
        movement_proof = _asset_last_movement_proof(conn, str(asset.get("asset_tag") or ""))
        results.append(
            {
                "id": int(asset["id"]),
                "asset_tag": str(asset.get("asset_tag") or ""),
                "serial_number": str(asset.get("serial_number") or ""),
                "equipment_type": equipment_type,
                "equipment_type_label": ASSET_EQUIPMENT_TYPE_LABELS.get(
                    equipment_type,
                    equipment_type.replace("_", " ").title() if equipment_type else "",
                ),
                "location_type": location_type,
                "state_label": _asset_state_label(asset.get("location_type")),
                "holder_label": holder_label,
                "home_case_name": "" if home_slot is None else str(home_slot.get("case_name") or ""),
                "home_slot_position": None if home_slot is None else int(home_slot["slot_position"]),
                "current_case_name": "" if current_slot is None else str(current_slot.get("case_name") or ""),
                "current_slot_position": None if current_slot is None else int(current_slot["slot_position"]),
                "movement_proof": movement_proof,
                "status_cue": _asset_search_status_cue(
                    location_type=location_type,
                    holder_label=holder_label,
                    movement_proof=movement_proof,
                ),
            }
        )

    return (results, None, lookup_mode)


def _lookup_cases_for_asset_search(conn: sqlite3.Connection, query: str) -> list[dict[str, object]]:
    query_clean = str(query or "").strip()
    query_key = _identifier_lookup_key(query_clean)
    if not query_key:
        return []

    rows = conn.execute(
        """
        SELECT
            case_name,
            COUNT(*) AS slot_count,
            CASE
                WHEN UPPER(case_name) = UPPER(?) THEN 0
                WHEN REPLACE(REPLACE(UPPER(case_name), '-', ''), ' ', '') = ? THEN 1
                ELSE 2
            END AS match_rank
        FROM slots
        WHERE REPLACE(REPLACE(UPPER(case_name), '-', ''), ' ', '') LIKE ?
        GROUP BY case_name
        ORDER BY match_rank ASC, UPPER(case_name) ASC;
        """,
        (query_clean, query_key, f"{query_key}%"),
    ).fetchall()

    case_matches = [
        {
            "case_name": str(row["case_name"] or ""),
            "slot_count": int(row["slot_count"] or 0),
            "match_rank": int(row["match_rank"] or 0),
        }
        for row in rows
    ]
    return sorted(
        case_matches,
        key=lambda row: (int(row["match_rank"]), natural_identifier_sort_key(row["case_name"])),
    )


def _asset_search_status_cue(*, location_type: str, holder_label: str, movement_proof: dict[str, object]) -> dict[str, str]:
    normalized_location = _normalize_location_type(location_type)
    has_holder = bool(str(holder_label or "").strip())
    has_movement_proof = bool(movement_proof.get("event_id"))

    if _is_terminal_location_type(normalized_location):
        return {
            "label": "Unavailable",
            "detail": "Retired or not in service.",
            "tone": "terminal",
        }

    if normalized_location == "IN_CUSTODY":
        if has_holder:
            return {
                "label": "Out with holder",
                "detail": f"Assigned to {holder_label}.",
                "tone": "issued",
            }
        return {
            "label": "Problem: holder not recorded",
            "detail": "Current state says in custody, but no holder is assigned.",
            "tone": "problem",
        }

    if normalized_location == "STORAGE":
        if has_holder:
            return {
                "label": "Problem: holder still assigned",
                "detail": "Current state says stored, but a holder is still assigned.",
                "tone": "problem",
            }
        return {
            "label": "Stored / returned",
            "detail": "Current state is storage.",
            "tone": "stored",
        }

    if not normalized_location:
        if has_movement_proof:
            return {
                "label": "Unknown current status",
                "detail": "Movement proof exists, but current state is not recorded.",
                "tone": "unknown",
            }
        return {
            "label": "No known custody/status proof",
            "detail": "No current state or movement proof is recorded.",
            "tone": "unknown",
        }

    return {
        "label": _asset_state_label(normalized_location),
        "detail": f"Current state is {normalized_location}.",
        "tone": "unknown",
    }


def _asset_last_movement_proof(conn: sqlite3.Connection, asset_tag: str) -> dict[str, object]:
    event_type_values = tuple(issue_event_type_values() + return_event_type_values() + ("SLOT_MOVE",))
    placeholders = ", ".join("?" for _ in event_type_values)
    active_events_where = ACTIVE_EVENTS_WHERE.replace("id NOT IN", "e.id NOT IN", 1)
    event_row = conn.execute(
        f"""
        SELECT e.id, e.event_type, e.event_date, e.payload
        FROM asset_events e
        WHERE UPPER(e.asset_tag) = UPPER(?)
          AND UPPER(e.event_type) IN ({placeholders})
          AND {active_events_where}
        ORDER BY e.event_date DESC, e.id DESC
        LIMIT 1;
        """,
        (asset_tag, *event_type_values),
    ).fetchone()

    if event_row is None:
        return {
            "event_id": None,
            "event_type": "",
            "event_date": "",
            "event_date_display": "",
            "receipt_id": None,
            "receipt_key": "",
            "source_case": "",
            "source_slot": "",
            "destination_case": "",
            "destination_slot": "",
        }

    event_id = int(event_row["id"])
    payload: dict[str, object] = {}
    try:
        payload = json.loads(str(event_row["payload"] or "{}"))
    except json.JSONDecodeError:
        payload = {}
    from_slot = payload.get("from_slot") if isinstance(payload.get("from_slot"), dict) else {}
    to_slot = payload.get("to_slot") if isinstance(payload.get("to_slot"), dict) else {}
    receipt_row = conn.execute(
        """
        SELECT id, receipt_key
        FROM receipt_queue
        WHERE EXISTS (
            SELECT 1
            FROM json_each(receipt_queue.source_event_ids_json)
            WHERE CAST(json_each.value AS INTEGER) = ?
        )
        ORDER BY commit_at DESC, id DESC
        LIMIT 1;
        """,
        (event_id,),
    ).fetchone()

    return {
        "event_id": event_id,
        "event_type": normalize_event_type(event_row["event_type"]),
        "event_date": str(event_row["event_date"] or ""),
        "event_date_display": _report_event_display_timestamp(event_row["event_date"]),
        "receipt_id": None if receipt_row is None else int(receipt_row["id"]),
        "receipt_key": "" if receipt_row is None else str(receipt_row["receipt_key"] or ""),
        "source_case": str(from_slot.get("case_number") or ""),
        "source_slot": str(from_slot.get("slot_number") or ""),
        "destination_case": str(to_slot.get("case_number") or ""),
        "destination_slot": str(to_slot.get("slot_number") or ""),
    }


def _asset_history_payload_items(payload_text: object) -> list[dict[str, str]]:
    try:
        payload = json.loads(str(payload_text or "{}"))
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, dict):
        return []
    items: list[dict[str, str]] = []
    for key in sorted(payload.keys()):
        value = payload.get(key)
        if isinstance(value, (dict, list)):
            display_value = json.dumps(value, sort_keys=True)
        else:
            display_value = str(value if value is not None else "")
        items.append({"key": str(key), "value": display_value})
    return items


def _build_asset_history_view(conn: sqlite3.Connection, asset_tag: str) -> Optional[dict[str, object]]:
    asset = conn.execute(
        """
        SELECT
            a.*,
            h.id AS holder_record_id,
            h.holder_type AS holder_record_type,
            h.name AS holder_record_name,
            h.organization AS holder_record_organization
        FROM assets a
        LEFT JOIN holders h
          ON h.id = a.current_holder_id
        WHERE UPPER(a.asset_tag) = UPPER(?)
        LIMIT 1;
        """,
        (asset_tag,),
    ).fetchone()
    if asset is None:
        return None

    asset_dict = dict(asset)
    holder = None
    if asset_dict.get("holder_record_id") is not None:
        holder = {
            "id": int(asset_dict["holder_record_id"]),
            "holder_type": str(asset_dict.get("holder_record_type") or ""),
            "name": str(asset_dict.get("holder_record_name") or ""),
            "organization": str(asset_dict.get("holder_record_organization") or ""),
        }
    current_slot = _asset_current_slot(conn, int(asset_dict["id"]), str(asset_dict["asset_tag"]))
    home_slot = _asset_home_slot(conn, asset_dict.get("home_slot_id"))

    event_rows = conn.execute(
        """
        SELECT
            e.id,
            e.asset_tag,
            e.event_type,
            e.event_date,
            e.actor,
            e.notes,
            e.payload,
            e.holder_id,
            e.supersedes_event_id,
            e.correction_reason,
            superseder.id AS superseded_by_event_id,
            h.name AS holder_name,
            h.organization AS holder_organization
        FROM asset_events e
        LEFT JOIN asset_events superseder
          ON superseder.supersedes_event_id = e.id
        LEFT JOIN holders h
          ON h.id = e.holder_id
        WHERE UPPER(e.asset_tag) = UPPER(?)
        ORDER BY e.event_date ASC, e.id ASC;
        """,
        (asset_dict["asset_tag"],),
    ).fetchall()

    events: list[dict[str, object]] = []
    for event_row in event_rows:
        receipts = [
            {
                "id": int(receipt_row["id"]),
                "receipt_key": str(receipt_row["receipt_key"] or ""),
                "receipt_type": str(receipt_row["receipt_type"] or ""),
            }
            for receipt_row in conn.execute(
                """
                SELECT id, receipt_key, receipt_type
                FROM receipt_queue
                WHERE EXISTS (
                    SELECT 1
                    FROM json_each(receipt_queue.source_event_ids_json)
                    WHERE CAST(json_each.value AS INTEGER) = ?
                )
                ORDER BY commit_at ASC, id ASC;
                """,
                (int(event_row["id"]),),
            ).fetchall()
        ]
        events.append(
            {
                "id": int(event_row["id"]),
                "event_type": normalize_event_type(event_row["event_type"]),
                "event_date": str(event_row["event_date"] or ""),
                "event_date_display": _report_event_display_timestamp(event_row["event_date"]),
                "actor": str(event_row["actor"] or ""),
                "notes": str(event_row["notes"] or ""),
                "holder_id": event_row["holder_id"],
                "holder_name": str(event_row["holder_name"] or ""),
                "holder_organization": str(event_row["holder_organization"] or ""),
                "supersedes_event_id": event_row["supersedes_event_id"],
                "superseded_by_event_id": event_row["superseded_by_event_id"],
                "correction_reason": str(event_row["correction_reason"] or ""),
                "payload_items": _asset_history_payload_items(event_row["payload"]),
                "receipts": receipts,
            }
        )

    equipment_type = str(asset_dict.get("equipment_type") or "").strip()
    return {
        "asset": {
            "asset_tag": str(asset_dict.get("asset_tag") or ""),
            "serial_number": str(asset_dict.get("serial_number") or ""),
            "equipment_type": equipment_type,
            "equipment_type_label": ASSET_EQUIPMENT_TYPE_LABELS.get(
                equipment_type,
                equipment_type.replace("_", " ").title() if equipment_type else "",
            ),
            "manufacturer": str(asset_dict.get("manufacturer") or ""),
            "model": str(asset_dict.get("model") or ""),
            "location_type": _normalize_location_type(asset_dict.get("location_type")),
            "state_label": _asset_state_label(asset_dict.get("location_type")),
            "building_room": str(asset_dict.get("building_room") or ""),
            "building": str(asset_dict.get("building") or ""),
            "room": str(asset_dict.get("room") or ""),
            "case_number": str(asset_dict.get("case_number") or ""),
            "slot_number": str(asset_dict.get("slot_number") or ""),
        },
        "holder": holder,
        "current_slot": current_slot,
        "home_slot": home_slot,
        "events": events,
    }


def _build_admin_assign_asset_view(conn, scan_tag: str) -> tuple[Optional[dict], list[str]]:
    errors: list[str] = []
    asset = _find_asset_for_scan_tag(conn, scan_tag)
    if not asset:
        return None, ["asset_tag not found"]

    holder_label = "None"
    holder_id = asset.get("current_holder_id")
    if holder_id is not None:
        holder = conn.execute(
            """
            SELECT id, name, identifier
            FROM holders
            WHERE id = ?;
            """,
            (holder_id,),
        ).fetchone()
        if holder:
            identifier = (holder["identifier"] or "").strip()
            holder_label = f"{holder['name']} ({identifier})" if identifier else str(holder["name"])
        else:
            holder_label = f"ID {holder_id}"

    location_type = _normalize_location_type(asset.get("location_type"))
    if _is_terminal_location_type(location_type):
        errors.append("Asset is retired/disposed and cannot be assigned to a slot.")
    if location_type != "STORAGE":
        errors.append("Asset must be location_type=STORAGE.")
    if location_type == "IN_CUSTODY":
        errors.append("Asset is IN_CUSTODY and cannot be assigned to a slot.")

    current_slot = _asset_current_slot(conn, int(asset["id"]), str(asset["asset_tag"]))
    if current_slot:
        errors.append("Asset is already slotted.")

    view = {
        "id": int(asset["id"]),
        "asset_tag": str(asset.get("asset_tag") or ""),
        "manufacturer": str(asset.get("manufacturer") or ""),
        "model": str(asset.get("model") or ""),
        "serial": str(asset.get("serial_number") or ""),
        "equipment_type": str(asset.get("equipment_type") or ""),
        "location_type": str(asset.get("location_type") or ""),
        "current_holder": holder_label,
        "current_slot": current_slot,
        "home_slot_id": asset.get("home_slot_id"),
    }
    return view, errors


def _list_unslotted_storage_assets(conn: sqlite3.Connection) -> list[dict]:
    asset_columns = get_asset_table_columns(conn)
    select_fields = [
        "a.asset_tag AS asset_tag",
        "a.serial_number AS serial_number",
        "a.equipment_type AS equipment_type",
        "a.created_date AS created_date",
    ]
    if "updated_date" in asset_columns:
        select_fields.append("a.updated_date AS updated_date")
    else:
        select_fields.append("NULL AS updated_date")

    rows = conn.execute(
        f"""
        SELECT {", ".join(select_fields)}
        FROM assets a
        LEFT JOIN slot_occupancy so ON so.asset_id = a.id
        WHERE UPPER(COALESCE(a.location_type, '')) = 'STORAGE'
          AND a.home_slot_id IS NULL
          AND so.asset_id IS NULL
        ORDER BY COALESCE(NULLIF(a.updated_date, ''), a.created_date, '') DESC, a.asset_tag ASC
        LIMIT 200;
        """
    ).fetchall()
    return [
        {
            "asset_tag": str(row["asset_tag"] or ""),
            "serial_number": str(row["serial_number"] or ""),
            "equipment_type": str(row["equipment_type"] or ""),
            "created_date": str(row["created_date"] or ""),
            "updated_date": str(row["updated_date"] or ""),
        }
        for row in rows
    ]


def _assign_slot_location_context(form: Optional[dict[str, str]] = None) -> dict:
    normalized_form = {
        "building": str((form or {}).get("building") or "").strip(),
        "room": str((form or {}).get("room") or "").strip(),
    }
    building_names = _ordered_location_names([str(row.get("name") or "") for row in list_buildings(active_only=True)])
    return {
        "form": normalized_form,
        "building_options": building_names,
    }


def _validate_assign_slot_location_form(form: dict[str, str]) -> tuple[dict[str, str], list[str], dict]:
    context = _assign_slot_location_context(form)
    normalized_form = context["form"]
    errors: list[str] = []

    building = normalized_form["building"]
    building_options = list(context["building_options"])
    building_name_map = {name.upper(): name for name in building_options}

    if building:
        if building_name_map:
            matched_name = building_name_map.get(building.upper())
            if matched_name is None:
                errors.append("Choose a valid building.")
            else:
                normalized_form["building"] = matched_name
        else:
            errors.append("No buildings are configured. Add a building in Admin Reference Data first.")

    return normalized_form, errors, context


def _assign_slot_building_room_label(building: object, room: object) -> str:
    parts = [str(value or "").strip() for value in (building, room)]
    return "/".join(part for part in parts if part)



ASSIGN_SLOT_BATCH_SESSION_KEY = "assign_slot_batch"
ASSIGN_SLOT_PENDING_SESSION_KEY = "assign_slot_pending_preview"


def _assign_slot_batch_tags() -> list[str]:
    raw_tags = session.get(ASSIGN_SLOT_BATCH_SESSION_KEY, [])
    if not isinstance(raw_tags, list):
        return []

    tags: list[str] = []
    seen: set[str] = set()
    for raw_tag in raw_tags:
        tag = str(raw_tag or "").strip().upper()
        if tag and tag not in seen:
            tags.append(tag)
            seen.add(tag)
    return tags


def _save_assign_slot_batch_tags(tags: list[str]) -> None:
    session[ASSIGN_SLOT_BATCH_SESSION_KEY] = tags
    session.pop(ASSIGN_SLOT_PENDING_SESSION_KEY, None)


def _clear_assign_slot_workflow_state() -> None:
    session.pop(ASSIGN_SLOT_BATCH_SESSION_KEY, None)
    session.pop(ASSIGN_SLOT_PENDING_SESSION_KEY, None)


def _build_assign_slot_batch_assets(conn: sqlite3.Connection, tags: list[str]) -> tuple[list[dict], list[str]]:
    assets: list[dict] = []
    errors: list[str] = []
    for tag in tags:
        asset_view, asset_errors = _build_admin_assign_asset_view(conn, tag)
        if asset_errors:
            errors.extend(f"{tag}: {error}" for error in asset_errors)
            continue
        if asset_view is None:
            errors.append(f"{tag}: asset_tag not found")
            continue
        assets.append(asset_view)
    return assets, errors


def _assign_slot_empty_slot_count(slot_options: list[dict], case_name: str) -> int:
    selected_case = str(case_name or "").strip().upper()
    return sum(
        1
        for slot in slot_options
        if str(slot.get("case_name") or "").strip().upper() == selected_case
        and not str(slot.get("occupied_asset_tag") or "").strip()
        and slot.get("occupied_asset_id") is None
    )


def _assign_slot_form_assignments(tags: list[str], case_name: str, slot_ids: list[str], building: str, room: str) -> list[dict]:
    assignments: list[dict] = []
    for index, tag in enumerate(tags):
        assignments.append(
            {
                "asset_tag": tag,
                "case_name": case_name,
                "slot_id": slot_ids[index] if index < len(slot_ids) else "",
                "building": building,
                "room": room,
            }
        )
    return assignments


def _assign_slot_preview_rows(prepared_assignments: list[dict]) -> list[dict]:
    rows: list[dict] = []
    for prepared in prepared_assignments:
        asset = prepared["asset"]
        slot = prepared["slot"]
        rows.append(
            {
                "asset_tag": str(asset["asset_tag"]),
                "serial": str(asset.get("serial_number") or ""),
                "equipment_type": str(asset.get("equipment_type") or ""),
                "case_name": str(slot["case_name"]),
                "slot_id": int(slot["id"]),
                "slot_position": int(slot["slot_position"]),
            }
        )
    return rows

def _write_assign_slot_occupancy_in_tx(
    conn: sqlite3.Connection,
    *,
    slot_id: int,
    asset_id: int,
    asset_tag: str,
    assigned_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO slot_occupancy (slot_id, asset_id, assigned_at)
        VALUES (?, ?, ?);
        """,
        (slot_id, asset_id, assigned_at),
    )
    conn.execute(
        """
        UPDATE slots
        SET current_asset_tag = ?
        WHERE id = ?;
        """,
        (asset_tag, slot_id),
    )


def _append_slot_assign_event_in_tx(
    conn: sqlite3.Connection,
    *,
    asset_tag: str,
    event_date: str,
    actor: str,
    notes: str,
    payload: dict,
) -> None:
    conn.execute(
        """
        INSERT INTO asset_events (
            asset_tag,
            event_type,
            event_date,
            actor,
            notes,
            payload,
            holder_id
        )
        VALUES (?, ?, ?, ?, ?, ?, ?);
        """,
        (
            asset_tag,
            "SLOT_ASSIGN",
            event_date,
            actor,
            notes or None,
            json.dumps(payload),
            None,
        ),
    )


def _validate_assign_slot_asset_row(conn: sqlite3.Connection, asset_tag: str):
    asset_row = _find_asset_for_scan_tag(conn, asset_tag)
    if not asset_row:
        raise ValueError("asset_tag not found")

    location_type = _normalize_location_type(asset_row.get("location_type"))
    if _is_terminal_location_type(location_type):
        raise ValueError("Asset is retired/disposed and cannot be assigned to a slot.")
    if location_type != "STORAGE":
        raise ValueError("Asset must be location_type=STORAGE.")
    if location_type == "IN_CUSTODY":
        raise ValueError("Asset is IN_CUSTODY and cannot be assigned to a slot.")

    occupied_by_asset = conn.execute(
        """
        SELECT 1
        FROM slot_occupancy
        WHERE asset_id = ?
        LIMIT 1;
        """,
        (asset_row["id"],),
    ).fetchone()
    legacy_occupied_by_asset = conn.execute(
        """
        SELECT 1
        FROM slots
        WHERE UPPER(current_asset_tag) = UPPER(?)
           OR REPLACE(REPLACE(UPPER(current_asset_tag), '-', ''), ' ', '') = UPPER(?)
        LIMIT 1;
        """,
        (asset_row["asset_tag"], asset_row["asset_tag"]),
    ).fetchone()
    if occupied_by_asset or legacy_occupied_by_asset:
        raise ValueError("Asset is already slotted.")
    return asset_row


def _validate_assign_slot_destination_row(
    conn: sqlite3.Connection,
    *,
    slot_id: int,
    case_name: str,
):
    slot = conn.execute(
        """
        SELECT id, case_name, slot_position, current_asset_tag
        FROM slots
        WHERE id = ?
        LIMIT 1;
        """,
        (slot_id,),
    ).fetchone()
    if not slot:
        raise ValueError("Selected slot does not exist.")
    if str(slot["case_name"] or "").strip().upper() != str(case_name or "").strip().upper():
        raise ValueError("Selected slot does not belong to selected case.")

    occupied_by_slot = conn.execute(
        """
        SELECT 1
        FROM slot_occupancy
        WHERE slot_id = ?
        LIMIT 1;
        """,
        (slot["id"],),
    ).fetchone()
    if occupied_by_slot:
        raise ValueError("Selected slot is already occupied.")
    legacy_slot_occupied = str(slot["current_asset_tag"] or "").strip()
    if legacy_slot_occupied:
        raise ValueError("Selected slot is already occupied.")
    return slot


def _prepare_assign_slot_batch_in_tx(
    conn: sqlite3.Connection,
    assignments: list[dict],
) -> list[dict]:
    if not assignments:
        raise ValueError("Batch assignment requires at least one assignment.")

    prepared: list[dict] = []
    seen_asset_ids: set[int] = set()
    seen_slot_ids: set[int] = set()
    for assignment in assignments:
        asset_tag = str(assignment.get("asset_tag") or "").strip()
        case_name = str(assignment.get("case_name") or "").strip()
        slot_id_raw = assignment.get("slot_id")
        building = str(assignment.get("building") or "").strip()
        room = str(assignment.get("room") or "").strip()

        if not asset_tag:
            raise ValueError("asset_tag is required.")
        if not case_name:
            raise ValueError("case is required.")
        if slot_id_raw in (None, ""):
            raise ValueError("slot is required.")
        try:
            slot_id = int(slot_id_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("Select a valid slot.") from exc

        asset_row = _validate_assign_slot_asset_row(conn, asset_tag)
        asset_id = int(asset_row["id"])
        if asset_id in seen_asset_ids:
            raise ValueError("Each asset may appear only once in a batch.")
        seen_asset_ids.add(asset_id)

        slot = _validate_assign_slot_destination_row(conn, slot_id=slot_id, case_name=case_name)
        resolved_slot_id = int(slot["id"])
        if resolved_slot_id in seen_slot_ids:
            raise ValueError("Each destination slot may appear only once in a batch.")
        seen_slot_ids.add(resolved_slot_id)

        prepared.append(
            {
                "asset": asset_row,
                "slot": slot,
                "building": building,
                "room": room,
            }
        )
    return prepared


def _commit_prepared_assign_slot_in_tx(
    conn: sqlite3.Connection,
    *,
    prepared_assignment: dict,
    actor: str,
    notes: str,
    event_date: str,
) -> dict:
    asset_row = prepared_assignment["asset"]
    slot = prepared_assignment["slot"]
    asset_id = int(asset_row["id"])
    slot_id = int(slot["id"])
    asset_tag = str(asset_row["asset_tag"])
    building = str(prepared_assignment["building"])
    room = str(prepared_assignment["room"])
    building_room = _assign_slot_building_room_label(building, room)

    _write_assign_slot_occupancy_in_tx(
        conn,
        slot_id=slot_id,
        asset_id=asset_id,
        asset_tag=asset_tag,
        assigned_at=event_date,
    )

    asset_columns = get_asset_table_columns(conn)
    update_clauses: list[str] = []
    update_values: list[object] = []
    if "home_slot_id" not in asset_columns:
        raise ValueError("Assets table missing required column: home_slot_id.")
    update_clauses.append("home_slot_id = ?")
    update_values.append(slot_id)
    if "building_room" in asset_columns:
        update_clauses.append("building_room = ?")
        update_values.append(building_room)
    if "case_number" in asset_columns:
        update_clauses.append("case_number = ?")
        update_values.append(str(slot["case_name"]))
    if "slot_number" in asset_columns:
        update_clauses.append("slot_number = ?")
        update_values.append(str(slot["slot_position"]))
    if "updated_date" in asset_columns:
        update_clauses.append("updated_date = ?")
        update_values.append(event_date)
    update_values.append(asset_id)
    conn.execute(
        f"UPDATE assets SET {', '.join(update_clauses)} WHERE id = ?;",
        tuple(update_values),
    )

    payload = {
        "slot_id": slot_id,
        "building_room": building_room,
        "case_number": str(slot["case_name"]),
        "slot_number": int(slot["slot_position"]),
    }
    _append_slot_assign_event_in_tx(
        conn,
        asset_tag=asset_tag,
        event_date=event_date,
        actor=actor,
        notes=notes,
        payload=payload,
    )

    canonical_asset = conn.execute(
        """
        SELECT home_slot_id, case_number, slot_number
        FROM assets
        WHERE id = ?
        LIMIT 1;
        """,
        (asset_id,),
    ).fetchone()
    if canonical_asset is None:
        raise ValueError("Asset disappeared during assignment.")
    if canonical_asset["home_slot_id"] is None or int(canonical_asset["home_slot_id"]) != slot_id:
        raise ValueError("Slot assignment failed to persist home_slot_id.")
    occupancy_link = conn.execute(
        """
        SELECT 1
        FROM slot_occupancy
        WHERE slot_id = ? AND asset_id = ?
        LIMIT 1;
        """,
        (slot_id, asset_id),
    ).fetchone()
    if occupancy_link is None:
        raise ValueError("Slot assignment failed to persist slot_occupancy link.")
    event_link = conn.execute(
        """
        SELECT 1
        FROM asset_events
        WHERE asset_tag = ?
          AND event_type = 'SLOT_ASSIGN'
          AND event_date = ?
        LIMIT 1;
        """,
        (asset_tag, event_date),
    ).fetchone()
    if event_link is None:
        raise ValueError("Slot assignment failed to append SLOT_ASSIGN event.")
    if "case_number" in asset_columns and str(canonical_asset["case_number"] or "") != str(slot["case_name"]):
        raise ValueError("Slot assignment failed to persist canonical case_number.")
    if "slot_number" in asset_columns and str(canonical_asset["slot_number"] or "") != str(slot["slot_position"]):
        raise ValueError("Slot assignment failed to persist canonical slot_number.")

    return {
        "asset_tag": asset_tag,
        "slot_id": slot_id,
        "case_name": str(slot["case_name"]),
        "slot_position": int(slot["slot_position"]),
    }


def _assign_slot_batch(
    conn: sqlite3.Connection,
    assignments: list[dict],
    *,
    actor: str = "admin",
    notes: str = "",
    event_date: Optional[str] = None,
) -> list[dict]:
    conn.execute("BEGIN;")
    try:
        prepared = _prepare_assign_slot_batch_in_tx(conn, assignments)
        now_iso = event_date or datetime.now(timezone.utc).isoformat()
        results = [
            _commit_prepared_assign_slot_in_tx(
                conn,
                prepared_assignment=prepared_assignment,
                actor=actor,
                notes=notes,
                event_date=now_iso,
            )
            for prepared_assignment in prepared
        ]
        conn.commit()
        return results
    except Exception:
        conn.rollback()
        raise


def _assign_single_asset_to_slot(
    conn: sqlite3.Connection,
    *,
    asset_tag: str,
    case_name: str,
    slot_id: int,
    building: str,
    room: str,
    actor: str = "admin",
    notes: str = "",
) -> dict:
    results = _assign_slot_batch(
        conn,
        [
            {
                "asset_tag": asset_tag,
                "case_name": case_name,
                "slot_id": slot_id,
                "building": building,
                "room": room,
            }
        ],
        actor=actor,
        notes=notes,
    )
    return results[0]


def _build_admin_edit_asset_view(conn, scan_tag: str) -> tuple[Optional[dict], list[str]]:
    asset = _find_asset_for_scan_tag(conn, scan_tag)
    if not asset:
        return None, ["asset_tag not found"]

    location_type = _normalize_location_type(asset.get("location_type"))
    if _is_terminal_location_type(location_type):
        return None, [f"Asset is {_asset_state_label(location_type)} and cannot be edited."]

    current_slot = _asset_current_slot(conn, int(asset["id"]), str(asset["asset_tag"]))
    home_slot = _asset_home_slot(conn, asset.get("home_slot_id"))
    cleanup_state = _build_admin_asset_cleanup_state(conn, asset, current_slot=current_slot)

    return (
        {
            "id": int(asset["id"]),
            "asset_tag": str(asset.get("asset_tag") or ""),
            "serial_number": str(asset.get("serial_number") or ""),
            "manufacturer": str(asset.get("manufacturer") or ""),
            "equipment_type": str(asset.get("equipment_type") or ""),
            "building": str(asset.get("building") or ""),
            "room": str(asset.get("room") or ""),
            "model": str(asset.get("model") or ""),
            "model_code": str(asset.get("model_code") or ""),
            "notes": str(asset.get("notes") or ""),
            "location_type": location_type,
            "current_holder_id": asset.get("current_holder_id"),
            "home_slot_id": asset.get("home_slot_id"),
            "current_slot": current_slot,
            "home_slot": home_slot,
            "cleanup": cleanup_state,
        },
        [],
    )


def _build_admin_asset_cleanup_state(
    conn: sqlite3.Connection,
    asset: dict,
    *,
    current_slot: Optional[dict] = None,
) -> dict:
    reasons: list[str] = []
    asset_tag = str(asset.get("asset_tag") or "").strip()
    if not asset_tag:
        return {"allowed": False, "reasons": ["asset_tag not found"]}

    if conn.execute("SELECT 1 FROM asset_events WHERE asset_tag = ? LIMIT 1;", (asset_tag,)).fetchone():
        reasons.append("Asset has event history and cannot be removed.")

    if current_slot is None:
        current_slot = _asset_current_slot(conn, int(asset["id"]), asset_tag)
    if current_slot is not None:
        reasons.append("Asset has a current slot placement and cannot be removed.")

    if asset.get("home_slot_id") is not None:
        reasons.append("Asset has a home slot assignment and cannot be removed.")

    asset_columns = get_asset_table_columns(conn)
    case_number = str(asset.get("case_number") or "").strip()
    slot_number = str(asset.get("slot_number") or "").strip()
    if "case_number" in asset_columns and case_number:
        reasons.append("Asset still has a case assignment and cannot be removed.")
    if "slot_number" in asset_columns and slot_number:
        reasons.append("Asset still has a slot assignment field and cannot be removed.")

    if asset.get("current_holder_id") is not None:
        reasons.append("Asset is assigned to a holder and cannot be removed.")

    location_type = _normalize_location_type(asset.get("location_type"))
    if location_type in {"STORAGE", "IN_CUSTODY"}:
        reasons.append(f"Asset is in active inventory state {location_type} and cannot be removed.")

    return {"allowed": not reasons, "reasons": reasons}


def _build_admin_slot_move_source_view(conn, slot_id: int) -> Optional[dict]:
    slot_row = conn.execute(
        """
        SELECT id, case_name, slot_position, current_asset_tag
        FROM slots
        WHERE id = ?;
        """,
        (slot_id,),
    ).fetchone()
    if not slot_row:
        return None

    occupancy_row = conn.execute(
        """
        SELECT so.asset_id, a.asset_tag, a.location_type, a.building_room, a.home_slot_id
        FROM slot_occupancy so
        JOIN assets a ON a.id = so.asset_id
        WHERE so.slot_id = ?
        LIMIT 1;
        """,
        (slot_id,),
    ).fetchone()

    occupied = occupancy_row is not None
    asset_view = None
    if occupancy_row:
        asset_view = {
            "asset_id": int(occupancy_row["asset_id"]),
            "asset_tag": str(occupancy_row["asset_tag"] or ""),
            "location_type": str(occupancy_row["location_type"] or ""),
            "building_room": str(occupancy_row["building_room"] or ""),
            "home_slot_id": occupancy_row["home_slot_id"],
        }

    return {
        "slot_id": int(slot_row["id"]),
        "case_name": str(slot_row["case_name"] or ""),
        "slot_position": int(slot_row["slot_position"]),
        "current_asset_tag": str(slot_row["current_asset_tag"] or ""),
        "occupied": occupied,
        "asset": asset_view,
    }


def _list_admin_slot_move_sources(conn) -> list[dict]:
    rows = conn.execute(
        """
        SELECT
            s.id AS slot_id,
            s.case_name,
            s.slot_position,
            a.asset_tag,
            a.serial_number,
            a.equipment_type,
            a.building_room,
            a.location_type
        FROM slots s
        JOIN slot_occupancy so ON so.slot_id = s.id
        JOIN assets a ON a.id = so.asset_id
        ORDER BY UPPER(s.case_name), s.slot_position, a.asset_tag
        LIMIT 200;
        """
    ).fetchall()
    return [
        {
            "slot_id": int(row["slot_id"]),
            "case_name": str(row["case_name"] or ""),
            "slot_position": int(row["slot_position"]),
            "asset_tag": str(row["asset_tag"] or ""),
            "serial_number": str(row["serial_number"] or ""),
            "equipment_type": str(row["equipment_type"] or ""),
            "building_room": str(row["building_room"] or ""),
            "location_type": str(row["location_type"] or ""),
        }
        for row in rows
    ]


def _list_admin_slot_move_destinations(conn, *, source_slot_id: int, building_room: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT s.id AS slot_id, s.case_name, s.slot_position
        FROM slots s
        LEFT JOIN slot_occupancy so ON so.slot_id = s.id
        WHERE s.id <> ?
          AND so.asset_id IS NULL
          AND TRIM(COALESCE(s.current_asset_tag, '')) = ''
        ORDER BY UPPER(s.case_name), s.slot_position
        LIMIT 200;
        """,
        (source_slot_id,),
    ).fetchall()
    return [
        {
            "slot_id": int(row["slot_id"]),
            "case_name": str(row["case_name"] or ""),
            "slot_position": int(row["slot_position"]),
            "building_room": building_room,
        }
        for row in rows
    ]


def _build_admin_slot_move_destination_view(conn, slot_id: int, *, building_room: str) -> Optional[dict]:
    slot_row = conn.execute(
        """
        SELECT id, case_name, slot_position
        FROM slots
        WHERE id = ?
        LIMIT 1;
        """,
        (slot_id,),
    ).fetchone()
    if not slot_row:
        return None
    return {
        "slot_id": int(slot_row["id"]),
        "case_name": str(slot_row["case_name"] or ""),
        "slot_position": int(slot_row["slot_position"]),
        "building_room": building_room,
    }


def _split_building_room(value: object) -> dict[str, str]:
    text = str(value or "").strip()
    if "/" not in text:
        return {"building": text, "room": ""}
    building, room = text.split("/", 1)
    return {"building": building.strip(), "room": room.strip()}


def _build_admin_slot_move_preview(
    conn,
    *,
    source_slot_id: int,
    building_room: str,
    case_number: str,
    slot_number: str,
) -> dict:
    if not building_room:
        raise ValueError("building/room is required.")
    if not case_number:
        raise ValueError("case_number is required.")
    if not slot_number:
        raise ValueError("slot_number is required.")

    try:
        destination_slot_position = int(slot_number)
    except ValueError as exc:
        raise ValueError("slot_number must be an integer.") from exc

    source_slot = conn.execute(
        """
        SELECT
            s.id,
            s.case_name,
            s.slot_position,
            so.asset_id,
            a.asset_tag,
            a.serial_number,
            a.equipment_type,
            a.location_type,
            a.current_holder_id,
            a.building_room,
            a.home_slot_id
        FROM slots s
        LEFT JOIN slot_occupancy so ON so.slot_id = s.id
        LEFT JOIN assets a ON a.id = so.asset_id
        WHERE s.id = ?
        LIMIT 1;
        """,
        (source_slot_id,),
    ).fetchone()
    if not source_slot or source_slot["asset_id"] is None:
        raise ValueError("Source slot is missing or empty.")

    asset_id = int(source_slot["asset_id"])
    asset_tag = str(source_slot["asset_tag"] or "")
    location_type = _normalize_location_type(source_slot["location_type"])
    if _is_terminal_location_type(location_type):
        raise ValueError("Asset is retired/disposed and cannot be moved.")
    if location_type != "STORAGE":
        raise ValueError("Asset must be location_type=STORAGE.")
    if location_type == "IN_CUSTODY":
        raise ValueError("Asset is IN_CUSTODY and cannot be moved.")

    destination_slot = conn.execute(
        """
        SELECT id, case_name, slot_position, current_asset_tag
        FROM slots
        WHERE UPPER(case_name) = UPPER(?)
          AND slot_position = ?
        LIMIT 1;
        """,
        (case_number, destination_slot_position),
    ).fetchone()
    if not destination_slot:
        raise ValueError("Destination slot does not exist.")

    destination_slot_id = int(destination_slot["id"])
    if destination_slot_id == int(source_slot["id"]):
        raise ValueError("Moving to the same slot is not allowed.")

    destination_occupied = conn.execute(
        """
        SELECT 1
        FROM slot_occupancy
        WHERE slot_id = ?
        LIMIT 1;
        """,
        (destination_slot_id,),
    ).fetchone()
    if destination_occupied:
        raise ValueError("Destination slot is already occupied.")
    if str(destination_slot["current_asset_tag"] or "").strip():
        raise ValueError("Destination slot is already occupied.")

    extra_asset_slot = conn.execute(
        """
        SELECT slot_id
        FROM slot_occupancy
        WHERE asset_id = ? AND slot_id <> ?
        LIMIT 1;
        """,
        (asset_id, source_slot_id),
    ).fetchone()
    if extra_asset_slot:
        raise ValueError("Asset already appears in another slot.")

    source_building_room = str(source_slot["building_room"] or "")
    source_location = _split_building_room(source_building_room)
    destination_location = _split_building_room(building_room)

    return {
        "asset": {
            "asset_id": asset_id,
            "asset_tag": asset_tag,
            "serial_number": str(source_slot["serial_number"] or ""),
            "equipment_type": str(source_slot["equipment_type"] or ""),
            "location_type": location_type,
            "current_holder_id": source_slot["current_holder_id"],
            "home_slot_id": source_slot["home_slot_id"],
        },
        "source": {
            "slot_id": int(source_slot["id"]),
            "case_number": str(source_slot["case_name"] or ""),
            "slot_number": int(source_slot["slot_position"]),
            "building_room": source_building_room,
            "building": source_location["building"],
            "room": source_location["room"],
        },
        "destination": {
            "slot_id": destination_slot_id,
            "case_number": str(destination_slot["case_name"] or ""),
            "slot_number": int(destination_slot["slot_position"]),
            "building_room": building_room,
            "building": destination_location["building"],
            "room": destination_location["room"],
        },
    }


def _validate_admin_slot_move_expected(
    move_preview: dict,
    *,
    expected_asset_id_raw: str,
    expected_destination_slot_id_raw: str,
) -> None:
    if not expected_asset_id_raw or not expected_destination_slot_id_raw:
        return
    try:
        expected_asset_id = int(expected_asset_id_raw)
        expected_destination_slot_id = int(expected_destination_slot_id_raw)
    except ValueError as exc:
        raise ValueError("Move preview is stale. Preview the move again.") from exc
    if int(move_preview["asset"]["asset_id"]) != expected_asset_id:
        raise ValueError("Source slot changed. Preview the move again.")
    if int(move_preview["destination"]["slot_id"]) != expected_destination_slot_id:
        raise ValueError("Destination slot changed. Preview the move again.")


def _normalize_case_identifier(value: object) -> str:
    return str(value or "").strip().upper()


def _case_identifier_in_payload(value: object, case_name: str) -> bool:
    normalized_case = _normalize_case_identifier(case_name)
    if isinstance(value, dict):
        return any(_case_identifier_in_payload(item, normalized_case) for item in value.values())
    if isinstance(value, list):
        return any(_case_identifier_in_payload(item, normalized_case) for item in value)
    if isinstance(value, str):
        return _normalize_case_identifier(value) == normalized_case
    return False


def _asset_event_payload_references_case(conn: sqlite3.Connection, case_name: str) -> bool:
    normalized_case = _normalize_case_identifier(case_name)
    if not normalized_case:
        return False
    rows = conn.execute(
        """
        SELECT payload
        FROM asset_events
        WHERE payload IS NOT NULL
          AND TRIM(payload) <> ''
          AND UPPER(payload) LIKE ?;
        """,
        (f"%{normalized_case}%",),
    ).fetchall()
    for row in rows:
        payload_text = str(row["payload"] or "")
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError:
            if normalized_case in payload_text.upper():
                return True
            continue
        if _case_identifier_in_payload(payload, normalized_case):
            return True
    return False


def _case_identifier_exists_in_slots(conn: sqlite3.Connection, case_name: str) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM slots
        WHERE UPPER(case_name) = UPPER(?)
        LIMIT 1;
        """,
        (case_name,),
    ).fetchone()
    return row is not None


def _case_identifier_exists_in_assets(conn: sqlite3.Connection, case_name: str) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM assets
        WHERE UPPER(COALESCE(case_number, '')) = UPPER(?)
        LIMIT 1;
        """,
        (case_name,),
    ).fetchone()
    return row is not None


def _case_identifier_is_completely_unused(conn: sqlite3.Connection, case_name: str) -> bool:
    return (
        not _case_identifier_exists_in_slots(conn, case_name)
        and not _case_identifier_exists_in_assets(conn, case_name)
        and not _asset_event_payload_references_case(conn, case_name)
    )


def _case_correction_source_slots(conn: sqlite3.Connection, case_name: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT id, case_name, slot_position, current_asset_tag
        FROM slots
        WHERE UPPER(case_name) = UPPER(?)
        ORDER BY slot_position ASC;
        """,
        (case_name,),
    ).fetchall()


def _slot_id_placeholders(slot_ids: list[int]) -> str:
    return ", ".join("?" for _ in slot_ids)


def _count_assets_tied_to_slots(conn: sqlite3.Connection, slot_ids: list[int]) -> int:
    if not slot_ids:
        return 0
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS c
        FROM assets
        WHERE home_slot_id IN ({_slot_id_placeholders(slot_ids)});
        """,
        tuple(slot_ids),
    ).fetchone()
    return int(row["c"] or 0)


def _case_has_occupancy(conn: sqlite3.Connection, slot_ids: list[int]) -> bool:
    if not slot_ids:
        return False
    row = conn.execute(
        f"""
        SELECT 1
        FROM slot_occupancy
        WHERE slot_id IN ({_slot_id_placeholders(slot_ids)})
        LIMIT 1;
        """,
        tuple(slot_ids),
    ).fetchone()
    return row is not None


def _case_has_slot_marker(slots: list[sqlite3.Row]) -> bool:
    return any(str(row["current_asset_tag"] or "").strip() for row in slots)


def _build_case_correction_preview(
    conn: sqlite3.Connection,
    *,
    event_type: str,
    old_case_name: str,
    new_case_name: str = "",
) -> dict:
    normalized_event_type = str(event_type or "").strip().upper()
    old_case = _normalize_case_identifier(old_case_name)
    new_case = _normalize_case_identifier(new_case_name)
    if normalized_event_type not in {"CASE_RENAME", "CASE_REMOVE"}:
        raise ValueError("Select rename or removal.")
    if not old_case:
        raise ValueError("Current Case is required.")

    source_slots = _case_correction_source_slots(conn, old_case)
    if not source_slots:
        raise ValueError("Case not found.")
    slot_ids = [int(row["id"]) for row in source_slots]
    affected_slot_count = len(slot_ids)
    affected_asset_count = _count_assets_tied_to_slots(conn, slot_ids)

    if normalized_event_type == "CASE_RENAME":
        if not new_case:
            raise ValueError("New Case is required.")
        if old_case == new_case:
            raise ValueError("New Case must be different.")
        if not _case_identifier_is_completely_unused(conn, new_case):
            raise ValueError("New Case identifier is already used.")
    else:
        if new_case:
            raise ValueError("Removal does not use a new Case.")
        if affected_asset_count > 0:
            raise ValueError("Cannot remove a Case referenced by assets.")
        if _case_has_occupancy(conn, slot_ids):
            raise ValueError("Cannot remove a Case with slot occupancy.")
        if _case_has_slot_marker(source_slots):
            raise ValueError("Cannot remove a Case with slot asset markers.")
        if _case_identifier_exists_in_assets(conn, old_case):
            raise ValueError("Cannot remove a Case referenced by assets.")
        if _asset_event_payload_references_case(conn, old_case):
            raise ValueError("Cannot remove a Case referenced by event history.")

    return {
        "event_type": normalized_event_type,
        "old_case_name": old_case,
        "new_case_name": new_case if normalized_event_type == "CASE_RENAME" else "",
        "affected_slot_count": affected_slot_count,
        "affected_asset_count": affected_asset_count,
        "slots": [
            {
                "id": int(row["id"]),
                "case_name": str(row["case_name"] or ""),
                "slot_position": int(row["slot_position"]),
                "current_asset_tag": str(row["current_asset_tag"] or ""),
            }
            for row in source_slots
        ],
    }


def _validate_case_correction_expected(
    preview: dict,
    *,
    expected_event_type: str,
    expected_old_case_name: str,
    expected_new_case_name: str,
    expected_slot_count: str,
    expected_asset_count: str,
) -> None:
    try:
        slot_count = int(expected_slot_count)
        asset_count = int(expected_asset_count)
    except ValueError as exc:
        raise ValueError("Case correction preview is stale. Preview again.") from exc
    if preview["event_type"] != str(expected_event_type or "").strip().upper():
        raise ValueError("Case correction action changed. Preview again.")
    if preview["old_case_name"] != _normalize_case_identifier(expected_old_case_name):
        raise ValueError("Case correction source changed. Preview again.")
    if preview["new_case_name"] != _normalize_case_identifier(expected_new_case_name):
        raise ValueError("Case correction target changed. Preview again.")
    if int(preview["affected_slot_count"]) != slot_count:
        raise ValueError("Case slot count changed. Preview again.")
    if int(preview["affected_asset_count"]) != asset_count:
        raise ValueError("Case asset count changed. Preview again.")


def _commit_case_correction(
    conn: sqlite3.Connection,
    *,
    preview: dict,
    actor_user: dict,
    created_at: str,
) -> None:
    event_type = str(preview["event_type"])
    old_case = str(preview["old_case_name"])
    new_case = str(preview["new_case_name"] or "")
    slot_ids = [int(row["id"]) for row in preview["slots"]]

    if event_type == "CASE_RENAME":
        conn.execute(
            """
            UPDATE slots
            SET case_name = ?
            WHERE UPPER(case_name) = UPPER(?);
            """,
            (new_case, old_case),
        )
        conn.execute(
            f"""
            UPDATE assets
            SET case_number = ?
            WHERE home_slot_id IN ({_slot_id_placeholders(slot_ids)});
            """,
            (new_case, *slot_ids),
        )
        conn.execute(
            """
            UPDATE case_metadata
            SET case_name = ?
            WHERE UPPER(case_name) = UPPER(?);
            """,
            (new_case, old_case),
        )
    else:
        conn.execute(
            f"""
            DELETE FROM slots
            WHERE id IN ({_slot_id_placeholders(slot_ids)});
            """,
            tuple(slot_ids),
        )
        conn.execute(
            """
            DELETE FROM case_metadata
            WHERE UPPER(case_name) = UPPER(?);
            """,
            (old_case,),
        )

    conn.execute(
        """
        INSERT INTO case_correction_events (
            event_type,
            created_at,
            actor_user_id,
            actor_username,
            old_case_name,
            new_case_name,
            affected_slot_count,
            affected_asset_count
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?);
        """,
        (
            event_type,
            created_at,
            int(actor_user["id"]),
            str(actor_user.get("username") or ""),
            old_case,
            new_case if event_type == "CASE_RENAME" else None,
            int(preview["affected_slot_count"]),
            int(preview["affected_asset_count"]),
        ),
    )


def _list_case_correction_history(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT
            event_type,
            created_at,
            actor_username,
            old_case_name,
            new_case_name,
            affected_slot_count,
            affected_asset_count
        FROM case_correction_events
        ORDER BY id DESC;
        """
    ).fetchall()
    return [
        {
            "event_type": str(row["event_type"] or ""),
            "created_at": str(row["created_at"] or ""),
            "actor_username": str(row["actor_username"] or ""),
            "old_case_name": str(row["old_case_name"] or ""),
            "new_case_name": str(row["new_case_name"] or ""),
            "affected_slot_count": int(row["affected_slot_count"]),
            "affected_asset_count": int(row["affected_asset_count"]),
        }
        for row in rows
    ]


def _list_case_correction_case_options(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        """
        SELECT case_name
        FROM slots
        WHERE TRIM(COALESCE(case_name, '')) <> ''
        GROUP BY UPPER(case_name)
        ORDER BY UPPER(case_name);
        """
    ).fetchall()
    return [str(row["case_name"] or "") for row in rows]


def _build_admin_retire_asset_view(conn, scan_tag: str) -> tuple[Optional[dict], list[str]]:
    asset = _find_asset_for_scan_tag(conn, scan_tag)
    if not asset:
        return None, ["asset_tag not found"]

    location_type = _normalize_location_type(asset.get("location_type"))
    errors: list[str] = []
    if _is_terminal_location_type(location_type):
        errors.append("Asset is already retired/disposed.")
    if location_type not in {"STORAGE", "IN_CUSTODY"}:
        errors.append("Asset must be in STORAGE or IN_CUSTODY to retire.")

    holder_label = "None"
    holder_id = asset.get("current_holder_id")
    if holder_id is not None:
        holder = conn.execute(
            """
            SELECT id, name, identifier
            FROM holders
            WHERE id = ?;
            """,
            (holder_id,),
        ).fetchone()
        if holder:
            identifier = str(holder["identifier"] or "").strip()
            holder_label = f"{holder['name']} ({identifier})" if identifier else str(holder["name"])
        else:
            holder_label = f"ID {holder_id}"

    current_slot = _asset_current_slot(conn, int(asset["id"]), str(asset["asset_tag"]))
    view = {
        "id": int(asset["id"]),
        "asset_tag": str(asset.get("asset_tag") or ""),
        "location_type": location_type,
        "serial_number": str(asset.get("serial_number") or ""),
        "manufacturer": str(asset.get("manufacturer") or ""),
        "model": str(asset.get("model") or ""),
        "current_holder": holder_label,
        "current_holder_id": holder_id,
        "home_slot_id": asset.get("home_slot_id"),
        "current_slot": current_slot,
    }
    return view, errors


def _lookup_retire_asset_matches(conn: sqlite3.Connection, asset_tag: str) -> tuple[list[dict], Optional[str]]:
    matches, error_message, _ = _lookup_asset_for_verification(
        conn,
        asset_tag=asset_tag,
        serial_number="",
    )
    return matches, error_message


def _lookup_admin_edit_asset_matches(conn: sqlite3.Connection, asset_tag: str) -> tuple[list[dict], Optional[str]]:
    matches, error_message, _ = _lookup_asset_for_verification(
        conn,
        asset_tag=asset_tag,
        serial_number="",
    )
    return matches, error_message


def _resolve_replacement_target_slot(
    conn: sqlite3.Connection,
    *,
    failed_asset_id: int,
    failed_asset_tag: str,
    failed_home_slot_id: Optional[int],
) -> tuple[int, dict]:
    occupancy_slot = conn.execute(
        """
        SELECT s.id, s.case_name, s.slot_position, s.current_asset_tag
        FROM slot_occupancy so
        JOIN slots s ON s.id = so.slot_id
        WHERE so.asset_id = ?
        LIMIT 1;
        """,
        (failed_asset_id,),
    ).fetchone()
    if occupancy_slot:
        return int(occupancy_slot["id"]), dict(occupancy_slot)

    if failed_home_slot_id is None:
        raise ValueError("Asset has no slot. Assign a slot first.")

    home_slot = conn.execute(
        """
        SELECT id, case_name, slot_position, current_asset_tag
        FROM slots
        WHERE id = ?
        LIMIT 1;
        """,
        (failed_home_slot_id,),
    ).fetchone()
    if not home_slot:
        raise ValueError("Target slot does not exist.")

    return int(home_slot["id"]), dict(home_slot)


def _validate_swap_target_slot_integrity(
    conn: sqlite3.Connection,
    *,
    target_slot_id: int,
    failed_asset_id: int,
    failed_asset_tag: str,
) -> None:
    occupied = conn.execute(
        """
        SELECT asset_id
        FROM slot_occupancy
        WHERE slot_id = ?
        LIMIT 1;
        """,
        (target_slot_id,),
    ).fetchone()
    if occupied and int(occupied["asset_id"]) != failed_asset_id:
        raise ValueError("Target slot is occupied by another asset.")

    slot_row = conn.execute(
        """
        SELECT current_asset_tag
        FROM slots
        WHERE id = ?
        LIMIT 1;
        """,
        (target_slot_id,),
    ).fetchone()
    if not slot_row:
        raise ValueError("Target slot does not exist.")

    marker = str(slot_row["current_asset_tag"] or "").strip()
    if marker:
        is_failed_asset_marker = marker.upper() == failed_asset_tag.upper() or marker.upper() == failed_asset_tag.upper().replace("-", "")
        if not is_failed_asset_marker:
            raise ValueError("Target slot is occupied by another asset.")


def _build_admin_replace_asset_view(conn, scan_tag: str) -> tuple[Optional[dict], list[str]]:
    asset = _find_asset_for_scan_tag(conn, scan_tag)
    if not asset:
        return None, ["asset_tag not found"]

    location_type = _normalize_location_type(asset.get("location_type"))
    errors: list[str] = []
    if _is_terminal_location_type(location_type):
        errors.append("Asset is already retired/disposed.")
    if location_type not in {"STORAGE", "IN_CUSTODY"}:
        errors.append("Asset must be in STORAGE or IN_CUSTODY to replace.")

    holder_label = "None"
    holder_id = asset.get("current_holder_id")
    if holder_id is not None:
        holder = conn.execute(
            """
            SELECT id, name, identifier
            FROM holders
            WHERE id = ?;
            """,
            (holder_id,),
        ).fetchone()
        if holder:
            identifier = str(holder["identifier"] or "").strip()
            holder_label = f"{holder['name']} ({identifier})" if identifier else str(holder["name"])
        else:
            holder_label = f"ID {holder_id}"

    target_slot_id = None
    target_slot = None
    try:
        target_slot_id, target_slot = _resolve_replacement_target_slot(
            conn,
            failed_asset_id=int(asset["id"]),
            failed_asset_tag=str(asset["asset_tag"]),
            failed_home_slot_id=asset.get("home_slot_id"),
        )
    except ValueError as e:
        errors.append(str(e))

    view = {
        "id": int(asset["id"]),
        "asset_tag": str(asset.get("asset_tag") or ""),
        "location_type": location_type,
        "serial_number": str(asset.get("serial_number") or ""),
        "manufacturer": str(asset.get("manufacturer") or ""),
        "model": str(asset.get("model") or ""),
        "building_room": str(asset.get("building_room") or ""),
        "current_holder": holder_label,
        "current_holder_id": holder_id,
        "home_slot_id": asset.get("home_slot_id"),
        "target_slot_id": target_slot_id,
        "target_slot": target_slot,
    }
    return view, errors


def _build_admin_force_vacate_view(conn, slot_id: int) -> Optional[dict]:
    slot_row = conn.execute(
        """
        SELECT id, case_name, slot_position, current_asset_tag
        FROM slots
        WHERE id = ?;
        """,
        (slot_id,),
    ).fetchone()
    if not slot_row:
        return None

    occupancy_row = conn.execute(
        """
        SELECT
            so.asset_id,
            a.asset_tag,
            a.manufacturer,
            a.model,
            a.serial_number,
            a.location_type,
            a.home_slot_id,
            a.building_room
        FROM slot_occupancy so
        JOIN assets a ON a.id = so.asset_id
        WHERE so.slot_id = ?
        LIMIT 1;
        """,
        (slot_id,),
    ).fetchone()

    occupied = occupancy_row is not None
    asset_view: Optional[dict] = None
    if occupancy_row:
        asset_view = {
            "asset_id": int(occupancy_row["asset_id"]),
            "asset_tag": str(occupancy_row["asset_tag"] or ""),
            "manufacturer": str(occupancy_row["manufacturer"] or ""),
            "model": str(occupancy_row["model"] or ""),
            "serial": str(occupancy_row["serial_number"] or ""),
            "location_type": str(occupancy_row["location_type"] or ""),
            "home_slot_id": occupancy_row["home_slot_id"],
            "building_room": str(occupancy_row["building_room"] or ""),
        }
    else:
        legacy_asset_tag = str(slot_row["current_asset_tag"] or "").strip()
        if legacy_asset_tag:
            occupied = True
            legacy_asset = _find_asset_for_scan_tag(conn, legacy_asset_tag)
            if legacy_asset:
                asset_view = {
                    "asset_id": int(legacy_asset["id"]),
                    "asset_tag": str(legacy_asset.get("asset_tag") or legacy_asset_tag),
                    "manufacturer": str(legacy_asset.get("manufacturer") or ""),
                    "model": str(legacy_asset.get("model") or ""),
                    "serial": str(legacy_asset.get("serial_number") or ""),
                    "location_type": str(legacy_asset.get("location_type") or ""),
                    "home_slot_id": legacy_asset.get("home_slot_id"),
                    "building_room": str(legacy_asset.get("building_room") or ""),
                }
            else:
                asset_view = {
                    "asset_id": None,
                    "asset_tag": legacy_asset_tag,
                    "manufacturer": "",
                    "model": "",
                    "serial": "",
                    "location_type": "",
                    "home_slot_id": None,
                    "building_room": "",
                }

    return {
        "slot_id": int(slot_row["id"]),
        "case_name": str(slot_row["case_name"] or ""),
        "slot_position": int(slot_row["slot_position"]),
        "occupied": occupied,
        "current_asset_tag": str(slot_row["current_asset_tag"] or ""),
        "asset": asset_view,
    }


def _is_truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _combine_building_room(building: object, room: object) -> str:
    building_text = str(building or "").strip()
    room_text = str(room or "").strip()
    if building_text and room_text:
        return f"{building_text}/{room_text}"
    return building_text or room_text


def _require_admin_for_route():
    user = current_user()
    role = str((user or {}).get("role") or "").strip().lower()
    if role != "admin":
        return {"ok": False, "error": "Forbidden"}, 403
    return None


def _require_admin_for_api():
    return _require_admin_for_route()


def _create_admin_asset_in_tx(
    conn: sqlite3.Connection,
    *,
    asset_tag: str,
    actor: str,
    equipment_type: str,
    serial_number: str,
    manufacturer: str,
    building: str,
    room: str,
    model: Optional[str],
    model_code: Optional[str],
    notes: Optional[str],
    assign_case_number: Optional[str],
    assign_slot_number: Optional[int],
) -> dict:
    equipment_type = validate_new_equipment_type(equipment_type)
    existing_asset = conn.execute(
        """
        SELECT 1
        FROM assets
        WHERE UPPER(asset_tag) = UPPER(?)
        LIMIT 1;
        """,
        (asset_tag,),
    ).fetchone()
    if existing_asset:
        raise ValueError("asset_tag already exists.")

    existing_serial = conn.execute(
        """
        SELECT 1
        FROM assets
        WHERE TRIM(COALESCE(serial_number, '')) <> ''
          AND UPPER(serial_number) = UPPER(?)
        LIMIT 1;
        """,
        (serial_number,),
    ).fetchone()
    if existing_serial:
        raise ValueError("serial_number already exists.")

    slot_row = None
    if assign_case_number is not None and assign_slot_number is not None:
        slot_row = conn.execute(
            """
            SELECT id, case_name, slot_position, current_asset_tag
            FROM slots
            WHERE UPPER(case_name) = UPPER(?)
              AND slot_position = ?
            LIMIT 1;
            """,
            (assign_case_number, assign_slot_number),
        ).fetchone()
        if slot_row is None:
            raise ValueError("Slot not found for case_number + slot_number.")

        occupied_row = conn.execute(
            """
            SELECT 1
            FROM slot_occupancy
            WHERE slot_id = ?
            LIMIT 1;
            """,
            (int(slot_row["id"]),),
        ).fetchone()
        if occupied_row:
            raise ValueError("Selected slot is already occupied.")

        if str(slot_row["current_asset_tag"] or "").strip():
            raise ValueError("Selected slot is already occupied.")

    now_iso = datetime.now(timezone.utc).isoformat()
    created_date = now_iso.split("T", 1)[0]
    building_room = _combine_building_room(building, room)

    home_slot_id = int(slot_row["id"]) if slot_row else None
    asset_columns = get_asset_table_columns(conn)
    insert_values: dict[str, object] = {"asset_tag": asset_tag}

    if "equipment_type" in asset_columns:
        insert_values["equipment_type"] = equipment_type
    if "serial_number" in asset_columns:
        insert_values["serial_number"] = serial_number
    if "manufacturer" in asset_columns:
        insert_values["manufacturer"] = manufacturer
    if "building" in asset_columns:
        insert_values["building"] = building
    if "room" in asset_columns:
        insert_values["room"] = room
    if "building_room" in asset_columns:
        insert_values["building_room"] = building_room
    if "model" in asset_columns:
        insert_values["model"] = model
    if "model_code" in asset_columns:
        insert_values["model_code"] = model_code
    if "notes" in asset_columns:
        insert_values["notes"] = notes
    if "case_number" in asset_columns and slot_row:
        insert_values["case_number"] = str(slot_row["case_name"])
    if "slot_number" in asset_columns and slot_row:
        insert_values["slot_number"] = str(slot_row["slot_position"])
    if "custody_state" in asset_columns:
        insert_values["custody_state"] = "in_stock"
    if "accountability_status" in asset_columns:
        insert_values["accountability_status"] = "accountable"
    if "condition" in asset_columns:
        insert_values["condition"] = "serviceable"
    if "retired" in asset_columns:
        insert_values["retired"] = 0
    if "created_date" in asset_columns:
        insert_values["created_date"] = created_date
    if "updated_date" in asset_columns:
        insert_values["updated_date"] = now_iso
    if "location_type" in asset_columns:
        insert_values["location_type"] = "STORAGE"
    if "current_holder_id" in asset_columns:
        insert_values["current_holder_id"] = None
    if "home_slot_id" in asset_columns:
        insert_values["home_slot_id"] = home_slot_id

    column_names = list(insert_values.keys())
    placeholders = ", ".join("?" for _ in column_names)
    cursor = conn.execute(
        f"INSERT INTO assets ({', '.join(column_names)}) VALUES ({placeholders});",
        tuple(insert_values[col] for col in column_names),
    )
    asset_id = int(cursor.lastrowid)

    if slot_row:
        conn.execute(
            """
            INSERT INTO slot_occupancy (slot_id, asset_id, assigned_at)
            VALUES (?, ?, ?);
            """,
            (home_slot_id, asset_id, now_iso),
        )
        conn.execute(
            """
            UPDATE slots
            SET current_asset_tag = ?
            WHERE id = ?;
            """,
            (asset_tag, home_slot_id),
        )

    created_payload: dict[str, object] = {}
    if equipment_type:
        created_payload["equipment_type"] = equipment_type
    if serial_number:
        created_payload["serial_number"] = serial_number
    if manufacturer:
        created_payload["manufacturer"] = manufacturer
    if building:
        created_payload["building"] = building
    if room:
        created_payload["room"] = room
    if model:
        created_payload["model"] = model
    if model_code:
        created_payload["model_code"] = model_code

    conn.execute(
        """
        INSERT INTO asset_events (
            asset_tag,
            event_type,
            event_date,
            actor,
            notes,
            payload,
            holder_id
        )
        VALUES (?, ?, ?, ?, ?, ?, ?);
        """,
        (
            asset_tag,
            "ASSET_CREATED",
            now_iso,
            actor,
            notes,
            json.dumps(created_payload) if created_payload else None,
            None,
        ),
    )

    if slot_row:
        slot_payload = {
            "slot_id": home_slot_id,
            "case_number": str(slot_row["case_name"]),
            "slot_number": int(slot_row["slot_position"]),
            "building": building,
            "room": room,
            "equipment_type": equipment_type,
        }
        conn.execute(
            """
            INSERT INTO asset_events (
                asset_tag,
                event_type,
                event_date,
                actor,
                notes,
                payload,
                holder_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?);
            """,
            (
                asset_tag,
                "SLOT_ASSIGN",
                now_iso,
                actor,
                notes,
                json.dumps(slot_payload),
                None,
            ),
        )

    return {
        "asset_id": asset_id,
        "asset_tag": asset_tag,
        "home_slot_id": home_slot_id,
        "location_type": "STORAGE",
        "current_holder_id": None,
    }


def _update_admin_asset_in_tx(
    conn: sqlite3.Connection,
    *,
    asset_id: int,
    actor: str,
    serial_number: str,
    manufacturer: str,
    equipment_type: str,
    building: str,
    room: str,
    model: Optional[str],
    model_code: Optional[str],
    notes: Optional[str],
    selected_slot: Optional[dict],
) -> dict:
    locked = conn.execute(
        """
        SELECT *
        FROM assets
        WHERE id = ?
        LIMIT 1;
        """,
        (asset_id,),
    ).fetchone()
    if not locked:
        raise ValueError("asset not found.")

    asset = dict(locked)
    asset_tag = str(asset.get("asset_tag") or "")
    location_type = _normalize_location_type(asset.get("location_type"))
    if _is_terminal_location_type(location_type):
        raise ValueError("Asset is retired/disposed and cannot be edited.")

    if serial_number:
        duplicate_serial = conn.execute(
            """
            SELECT id
            FROM assets
            WHERE id <> ?
              AND TRIM(COALESCE(serial_number, '')) <> ''
              AND UPPER(serial_number) = UPPER(?)
            LIMIT 1;
            """,
            (asset_id, serial_number),
        ).fetchone()
        if duplicate_serial:
            raise ValueError("serial_number already exists.")

    current_slot = _asset_current_slot(conn, asset_id, asset_tag)
    current_slot_id = None if current_slot is None else int(current_slot["slot_id"])
    target_slot_id = None if selected_slot is None else int(selected_slot["id"])

    if target_slot_id is None and asset.get("home_slot_id") is not None:
        raise ValueError("Clearing an existing home slot is not supported here.")

    if current_slot is not None and location_type != "STORAGE":
        raise ValueError("Asset slot occupancy is inconsistent with its location_type.")

    if selected_slot is not None:
        occupied_row = conn.execute(
            """
            SELECT asset_id
            FROM slot_occupancy
            WHERE slot_id = ?
            LIMIT 1;
            """,
            (target_slot_id,),
        ).fetchone()
        if occupied_row and int(occupied_row["asset_id"]) != asset_id:
            raise ValueError("Selected slot is already occupied.")

        legacy_occupied = str(selected_slot.get("current_asset_tag") or "").strip()
        if legacy_occupied and legacy_occupied.upper() != asset_tag.upper():
            raise ValueError("Selected slot is already occupied.")

    now_iso = datetime.now(timezone.utc).isoformat()
    building_room = _combine_building_room(building, room)
    asset_columns = get_asset_table_columns(conn)

    changed_fields: dict[str, object] = {}
    field_values = {
        "serial_number": serial_number,
        "manufacturer": manufacturer,
        "equipment_type": equipment_type,
        "building": building,
        "room": room,
        "building_room": building_room,
        "model": model,
        "model_code": model_code,
        "notes": notes,
    }
    for key, value in field_values.items():
        if key in asset_columns and asset.get(key) != value:
            changed_fields[key] = value

    if "home_slot_id" in asset_columns and asset.get("home_slot_id") != target_slot_id:
        changed_fields["home_slot_id"] = target_slot_id
    if "case_number" in asset_columns:
        next_case_number = None if selected_slot is None else str(selected_slot["case_name"])
        if asset.get("case_number") != next_case_number:
            changed_fields["case_number"] = next_case_number
    if "slot_number" in asset_columns:
        next_slot_number = None if selected_slot is None else str(selected_slot["slot_position"])
        if asset.get("slot_number") != next_slot_number:
            changed_fields["slot_number"] = next_slot_number

    if location_type not in {"STORAGE", "IN_CUSTODY", ""}:
        raise ValueError("Asset location_type is not supported for admin edit.")

    update_clauses: list[str] = []
    update_values: list[object] = []
    for key, value in changed_fields.items():
        update_clauses.append(f"{key} = ?")
        update_values.append(value)
    if "updated_date" in asset_columns:
        update_clauses.append("updated_date = ?")
        update_values.append(now_iso)
    if update_clauses:
        update_values.append(asset_id)
        conn.execute(
            f"UPDATE assets SET {', '.join(update_clauses)} WHERE id = ?;",
            tuple(update_values),
        )

    metadata_payload = dict(changed_fields)
    if metadata_payload:
        metadata_payload["asset_id"] = asset_id
        conn.execute(
            """
            INSERT INTO asset_events (
                asset_tag,
                event_type,
                event_date,
                actor,
                notes,
                payload,
                holder_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?);
            """,
            (
                asset_tag,
                "ASSET_UPDATED",
                now_iso,
                actor,
                notes,
                json.dumps(metadata_payload),
                asset.get("current_holder_id"),
            ),
        )

    return {
        "asset_id": asset_id,
        "asset_tag": asset_tag,
        "location_type": location_type,
        "home_slot_id": target_slot_id,
        "current_holder_id": asset.get("current_holder_id"),
    }


def _retire_admin_asset_in_tx(
    conn: sqlite3.Connection,
    *,
    asset_id: int,
    asset_tag: str,
    failure_type: str,
    notes: str,
    actor: str,
) -> dict:
    locked_row = conn.execute(
        """
        SELECT id, asset_tag, location_type, current_holder_id, home_slot_id
        FROM assets
        WHERE id = ?
        LIMIT 1;
        """,
        (asset_id,),
    ).fetchone()
    if not locked_row:
        raise ValueError("asset_tag not found")

    origin_location = _normalize_location_type(locked_row["location_type"])
    if _is_terminal_location_type(origin_location):
        raise ValueError("Asset is already retired/disposed.")
    if origin_location not in {"STORAGE", "IN_CUSTODY"}:
        raise ValueError("Asset must be in STORAGE or IN_CUSTODY to retire.")

    now_iso = datetime.now(timezone.utc).isoformat()
    event_type = "ASSET_RETIRED_IN_FIELD" if origin_location == "IN_CUSTODY" else "ASSET_RETIRED"

    occupied_slots = conn.execute(
        """
        SELECT slot_id
        FROM slot_occupancy
        WHERE asset_id = ?;
        """,
        (asset_id,),
    ).fetchall()
    cleared_slot_ids = [int(row["slot_id"]) for row in occupied_slots]

    conn.execute(
        """
        DELETE FROM slot_occupancy
        WHERE asset_id = ?;
        """,
        (asset_id,),
    )
    conn.execute(
        """
        UPDATE slots
        SET current_asset_tag = NULL
        WHERE UPPER(current_asset_tag) = UPPER(?)
           OR REPLACE(REPLACE(UPPER(current_asset_tag), '-', ''), ' ', '') = UPPER(?);
        """,
        (asset_tag, asset_tag),
    )

    asset_columns = get_asset_table_columns(conn)
    update_clauses = [
        "location_type = ?",
        "current_holder_id = NULL",
        "home_slot_id = NULL",
    ]
    update_values: list[object] = [TERMINAL_LOCATION_TYPE]
    if "updated_date" in asset_columns:
        update_clauses.append("updated_date = ?")
        update_values.append(now_iso)

    update_values.append(asset_id)
    conn.execute(
        f"UPDATE assets SET {', '.join(update_clauses)} WHERE id = ?;",
        tuple(update_values),
    )

    payload = {
        "failure_type": failure_type,
        "notes": notes,
        "from_location_type": origin_location,
        "to_location_type": TERMINAL_LOCATION_TYPE,
        "cleared_slot_ids": cleared_slot_ids,
        "previous_holder_id": locked_row["current_holder_id"],
        "previous_home_slot_id": locked_row["home_slot_id"],
    }
    conn.execute(
        """
        INSERT INTO asset_events (
            asset_tag,
            event_type,
            event_date,
            actor,
            notes,
            payload,
            holder_id
        )
        VALUES (?, ?, ?, ?, ?, ?, ?);
        """,
        (
            str(locked_row["asset_tag"]),
            event_type,
            now_iso,
            actor,
            notes,
            json.dumps(payload),
            locked_row["current_holder_id"],
        ),
    )

    return {
        "asset_tag": str(locked_row["asset_tag"]),
        "event_type": event_type,
        "from_location_type": origin_location,
        "to_location_type": TERMINAL_LOCATION_TYPE,
    }


def _holder_is_active(holder: Optional[dict]) -> bool:
    if holder is None:
        return False
    return int(holder.get("is_active") or 0) == 1


def _selected_holder_from_session(*, require_active: bool = False) -> Optional[dict]:
    holder_id = session.get("holder_id")
    if holder_id is None:
        return None

    holder = get_holder(holder_id)
    if holder is None or (require_active and not _holder_is_active(holder)):
        session.pop("holder_id", None)
        return None
    return holder


def _holder_selection_requires_active_filter(return_to: Optional[str]) -> bool:
    normalized = str(return_to or "").strip()
    return normalized.startswith("/issue")


def _holder_directory_status_filter(value: object) -> str:
    normalized = str(value or "all").strip().lower()
    if normalized in {"active", "inactive"}:
        return normalized
    return "all"


def _issue_location_form_from_session() -> dict[str, str]:
    return {
        "building": str(session.get("issue_building") or "").strip(),
        "room": str(session.get("issue_room") or "").strip(),
    }


def _last_issue_location_form_from_session() -> dict[str, str]:
    return {
        "building": str(session.get("last_issue_building") or "").strip(),
        "room": str(session.get("last_issue_room") or "").strip(),
    }


def _remember_last_issue_location(form: dict[str, str]) -> None:
    building = str(form.get("building") or "").strip()
    room = str(form.get("room") or "").strip()
    if building and room:
        session["last_issue_building"] = building
        session["last_issue_room"] = room


def _ordered_location_names(names: list[str]) -> list[str]:
    normalized_names = {str(name or "").strip() for name in names if str(name or "").strip()}
    return sorted(normalized_names, key=lambda name: (name.casefold(), name))


def _issue_location_context(selected_holder: Optional[dict], form: Optional[dict[str, str]] = None) -> dict:
    normalized_form = {
        "building": str((form or {}).get("building") or "").strip(),
        "room": str((form or {}).get("room") or "").strip(),
    }
    all_building_names = _ordered_location_names([str(row.get("name") or "") for row in list_buildings(active_only=True)])
    allowed_building_names = list(all_building_names)
    constrained_by_org = False

    holder_org_id = None if not selected_holder else selected_holder.get("organization_id")
    try:
        normalized_holder_org_id = None if holder_org_id in {None, ""} else int(holder_org_id)
    except (TypeError, ValueError):
        normalized_holder_org_id = None

    if normalized_holder_org_id is not None:
        holder_mappings = [
            mapping
            for mapping in list_organization_building_mappings()
            if int(mapping["organization_id"]) == normalized_holder_org_id
        ]
        if holder_mappings:
            constrained_by_org = True
            mapped_buildings = [
                str(mapping.get("building_name") or "").strip()
                for mapping in holder_mappings
                if int(mapping.get("building_is_active") or 0) == 1
            ]
            mapped_buildings = _ordered_location_names(mapped_buildings)
            allowed_building_names = mapped_buildings

    return {
        "form": normalized_form,
        "building_options": allowed_building_names,
        "has_reference_buildings": bool(all_building_names),
        "constrained_by_org": constrained_by_org,
    }


def _validate_issue_location_form(selected_holder: Optional[dict], form: dict[str, str]) -> tuple[dict[str, str], list[str], dict]:
    context = _issue_location_context(selected_holder, form)
    normalized_form = context["form"]
    errors: list[str] = []

    if selected_holder is None:
        errors.append("Select a holder before choosing the current location.")
        return normalized_form, errors, context

    building = normalized_form["building"]
    room = normalized_form["room"]
    building_options = list(context["building_options"])
    building_name_map = {name.upper(): name for name in building_options}

    if not building:
        errors.append("Choose the current building.")
    elif building_name_map:
        matched_name = building_name_map.get(building.upper())
        if matched_name is None:
            if context["constrained_by_org"]:
                errors.append("Choose a building allowed for the selected organization.")
            else:
                errors.append("Choose a valid building.")
        else:
            normalized_form["building"] = matched_name
    elif context["constrained_by_org"]:
        errors.append("Choose a building allowed for the selected organization.")
    elif context["has_reference_buildings"]:
        errors.append("Choose a valid building.")

    if not room:
        errors.append("Enter the current room or area.")

    return normalized_form, errors, context


def _issue_location_form_for_holder(selected_holder: Optional[dict]) -> dict[str, str]:
    current_form = _issue_location_form_from_session()
    if current_form["building"] or current_form["room"]:
        return current_form

    last_form = _last_issue_location_form_from_session()
    if not last_form["building"] and not last_form["room"]:
        return current_form

    last_normalized, last_errors, _ = _validate_issue_location_form(selected_holder, last_form)
    if last_errors:
        return current_form

    session["issue_building"] = last_normalized["building"]
    session["issue_room"] = last_normalized["room"]
    return last_normalized


def _issue_location_label(form: dict[str, str]) -> str:
    building = str(form.get("building") or "").strip()
    room = str(form.get("room") or "").strip()
    if building and room:
        return f"{building} / {room}"
    if building:
        return building
    return ""


def _queue_asset_tags() -> list[str]:
    tags: list[str] = []
    for s in SCAN_QUEUE:
        tag = (s.asset_tag or "").strip()
        if tag:
            tags.append(tag)
    return tags


def _build_issue_preview_state(asset_tags: list[str], selected_holder: Optional[dict]) -> dict:
    holder_label = None
    if selected_holder:
        identifier = (selected_holder.get("identifier") or "").strip()
        display_name = _holder_display_name(selected_holder)
        holder_label = display_name if not identifier else f"{display_name} ({identifier})"
    issue_location_form, issue_location_errors, _ = _validate_issue_location_form(
        selected_holder,
        _issue_location_form_for_holder(selected_holder),
    )
    issue_location_label = _issue_location_label(issue_location_form)

    assets: list[dict] = []
    unknown_tags: list[str] = []
    not_storage: list[str] = []
    retired_assets: list[str] = []
    not_slotted: list[str] = []
    blocking_issues: list[str] = []

    if not asset_tags:
        blocking_issues.append("Queue is empty. Scan assets before committing.")
        return {
            "assets": assets,
            "ready_count": 0,
            "blocking_issues": blocking_issues,
            "holder_label": holder_label,
        }

    if selected_holder is None:
        blocking_issues.append("No holder selected. Select a holder before issuing assets.")
    for error in issue_location_errors:
        blocking_issues.append(error)

    def _canon_asset_row_for_scan_tag(conn, scan_tag: str) -> Optional[dict]:
        return _find_asset_for_scan_tag(conn, scan_tag)

    conn = get_connection()
    try:
        asset_columns = get_asset_table_columns(conn)
        required_columns = {"location_type", "current_holder_id", "building_room"}
        missing_columns = sorted(required_columns - asset_columns)
        if missing_columns:
            blocking_issues.append(f"Assets table missing columns: {', '.join(missing_columns)}")
            return {
                "assets": assets,
                "ready_count": 0,
                "blocking_issues": blocking_issues,
                "holder_label": holder_label,
            }

        for scan_tag in asset_tags:
            row: dict = {
                "scanned_tag": scan_tag,
                "asset_tag": scan_tag,
                "canonical_tag": None,
                "before_location_type": "UNKNOWN",
                "after_location_type": "IN_CUSTODY",
                "before_current_location": "null",
                "after_current_location": issue_location_label or "(choose current location)",
                "before_holder": "null",
                "after_holder": holder_label or "(select holder)",
                "before_home_location": "null",
                "after_home_location": "null",
                "before_slot_occupancy": "unknown",
                "after_slot_occupancy": "vacated",
                "ready": False,
                "asset_issues": [],
            }

            asset_row = _canon_asset_row_for_scan_tag(conn, scan_tag)
            if not asset_row:
                row["asset_issues"].append("Unknown asset tag")
                unknown_tags.append(scan_tag)
                assets.append(row)
                continue

            canon_tag = str(asset_row["asset_tag"])
            row["asset_tag"] = canon_tag
            row["canonical_tag"] = canon_tag

            before_location = str(asset_row["location_type"] or "").strip().upper()
            row["before_location_type"] = before_location or "UNKNOWN"
            row["before_current_location"] = str(asset_row["building_room"] or "").strip() or "null"
            if _is_terminal_location_type(before_location):
                row["asset_issues"].append("Asset is retired/disposed")
                retired_assets.append(canon_tag)

            before_holder_id = asset_row["current_holder_id"]
            row["before_holder"] = "null" if before_holder_id is None else str(before_holder_id)

            current_slot = _asset_current_slot(conn, int(asset_row["id"]), canon_tag)
            slotted = current_slot is not None
            home_slot_id = asset_row.get("home_slot_id")
            if home_slot_id is not None:
                home_slot = conn.execute(
                    """
                    SELECT case_name, slot_position
                    FROM slots
                    WHERE id = ?;
                    """,
                    (int(home_slot_id),),
                ).fetchone()
                if home_slot is not None:
                    row["before_home_location"] = f"{home_slot['case_name']} / {home_slot['slot_position']}"
            row["before_slot_occupancy"] = "occupied" if slotted else "vacant"
            row["after_home_location"] = row["before_home_location"]

            if before_location != "STORAGE":
                row["asset_issues"].append("Asset is not in STORAGE")
                not_storage.append(canon_tag)

            if not slotted:
                row["asset_issues"].append("Asset is not currently slotted")
                not_slotted.append(canon_tag)

            row["ready"] = bool(selected_holder is not None and not row["asset_issues"])
            assets.append(row)
    finally:
        conn.close()

    if unknown_tags:
        blocking_issues.append(f"Unknown asset_tag(s): {', '.join(unknown_tags)}")
    if not_storage:
        blocking_issues.append(f"Not in STORAGE: {', '.join(not_storage)}")
    if retired_assets:
        blocking_issues.append(f"Retired/disposed: {', '.join(retired_assets)}")
    if not_slotted:
        blocking_issues.append(f"Not currently slotted: {', '.join(not_slotted)}")

    ready_count = sum(1 for row in assets if row["ready"])
    return {
        "assets": assets,
        "ready_count": ready_count,
        "blocking_issues": blocking_issues,
        "holder_label": holder_label,
    }


RETURN_DESTINATIONS_SESSION_KEY = "return_destination_slot_ids"


def _return_destination_slot_ids() -> dict[str, int]:
    raw = session.get(RETURN_DESTINATIONS_SESSION_KEY, {})
    if not isinstance(raw, dict):
        return {}
    destinations: dict[str, int] = {}
    for asset_tag, slot_id in raw.items():
        try:
            destinations[str(asset_tag).strip().upper()] = int(slot_id)
        except (TypeError, ValueError):
            continue
    return destinations


def _build_return_preview_state(asset_tags: list[str], destination_slot_ids: Optional[dict[str, int]] = None) -> dict:
    assets: list[dict] = []
    unknown_tags: list[str] = []
    not_in_custody: list[str] = []
    retired_assets: list[str] = []
    no_home_slot: list[str] = []
    missing_destination: list[str] = []
    occupied_destination: list[str] = []
    blocking_issues: list[str] = []

    if not asset_tags:
        blocking_issues.append("Queue is empty. Scan assets before returning.")
        return {"assets": assets, "ready_count": 0, "blocking_issues": blocking_issues}

    def _canon_asset_row_for_scan_tag(conn, scan_tag: str) -> Optional[dict]:
        return _find_asset_for_scan_tag(conn, scan_tag)

    conn = get_connection()
    try:
        asset_columns = get_asset_table_columns(conn)
        required_columns = {"location_type", "current_holder_id", "home_slot_id"}
        missing_columns = sorted(required_columns - asset_columns)
        if missing_columns:
            blocking_issues.append(f"Assets table missing columns: {', '.join(missing_columns)}")
            return {"assets": assets, "ready_count": 0, "blocking_issues": blocking_issues}

        selections = destination_slot_ids or {}
        empty_slot_options = _list_slot_options(conn, empty_only=True)
        selected_destinations: dict[int, str] = {}
        for scan_tag in asset_tags:
            row: dict = {
                "scanned_tag": scan_tag,
                "canonical_tag": None,
                "before_location_type": "UNKNOWN",
                "after_location_type": "STORAGE",
                "before_holder": "null",
                "after_holder": "null",
                "home_slot": "unknown",
                "destination_slot": "unknown",
                "destination_case_name": None,
                "before_slot_occupancy": "empty",
                "after_slot_occupancy": "occupied",
                "ready": False,
                "asset_issues": [],
            }

            asset_row = _canon_asset_row_for_scan_tag(conn, scan_tag)
            if not asset_row:
                row["asset_issues"].append("Unknown asset tag")
                unknown_tags.append(scan_tag)
                assets.append(row)
                continue

            canon_tag = str(asset_row["asset_tag"])
            row["canonical_tag"] = canon_tag

            location_type = str(asset_row["location_type"] or "").strip().upper()
            row["before_location_type"] = location_type or "UNKNOWN"
            if _is_terminal_location_type(location_type):
                row["asset_issues"].append("Asset is retired/disposed")
                retired_assets.append(canon_tag)
            if location_type != "IN_CUSTODY":
                row["asset_issues"].append("Asset is not in IN_CUSTODY")
                not_in_custody.append(canon_tag)

            current_holder_id = asset_row["current_holder_id"]
            if current_holder_id is not None:
                holder = get_holder(current_holder_id)
                row["before_holder"] = (
                    holder["name"] if holder is not None else f"holder_id {current_holder_id}"
                )

            home_slot_id = asset_row["home_slot_id"]
            if home_slot_id is None:
                row["asset_issues"].append("No assigned home slot. Return cannot commit until a home slot is assigned.")
                no_home_slot.append(canon_tag)
                assets.append(row)
                continue

            slot = _slot_occupancy_status(conn, home_slot_id)
            if not slot:
                row["asset_issues"].append("Assigned home slot not found. Return cannot commit until a valid home slot is assigned.")
                no_home_slot.append(canon_tag)
                assets.append(row)
                continue

            row["home_slot"] = f"{slot['case_name']} / {slot['slot_position']}"
            selected_slot_id = selections.get(canon_tag.upper())
            if selected_slot_id is None and not slot["occupied"]:
                selected_slot_id = int(slot["id"])
            row["destination_options"] = [
                {
                    "slot_id": int(option["id"]),
                    "label": f"{option['case_name']} / {option['slot_position']}",
                    "selected": int(option["id"]) == selected_slot_id,
                }
                for option in empty_slot_options
            ]
            if selected_slot_id is None:
                row["asset_issues"].append("Choose an empty return destination.")
                missing_destination.append(canon_tag)
                row["before_slot_occupancy"] = "occupied"
                assets.append(row)
                continue

            destination = _slot_occupancy_status(conn, selected_slot_id)
            if destination is None:
                row["asset_issues"].append("Selected return destination no longer exists.")
                missing_destination.append(canon_tag)
                assets.append(row)
                continue
            row["destination_case_name"] = str(destination["case_name"])
            row["destination_slot"] = f"{destination['case_name']} / {destination['slot_position']}"
            if destination["occupied"]:
                row["asset_issues"].append(
                    f"Selected return destination {row['destination_slot']} is occupied by {destination['occupied_by']}."
                )
                occupied_destination.append(canon_tag)
                row["before_slot_occupancy"] = "occupied"
            prior_tag = selected_destinations.get(int(destination["id"]))
            if prior_tag and prior_tag != canon_tag:
                row["asset_issues"].append(f"Selected return destination is also selected for {prior_tag}.")
                occupied_destination.append(canon_tag)
            else:
                selected_destinations[int(destination["id"])] = canon_tag

            row["ready"] = not row["asset_issues"]
            assets.append(row)
    finally:
        conn.close()

    if unknown_tags:
        blocking_issues.append(f"Unknown asset_tag(s): {', '.join(unknown_tags)}")
    if not_in_custody:
        blocking_issues.append(f"Not in IN_CUSTODY: {', '.join(not_in_custody)}")
    if retired_assets:
        blocking_issues.append(f"Retired/disposed: {', '.join(retired_assets)}")
    if no_home_slot:
        blocking_issues.append(f"No assigned home slot: {', '.join(no_home_slot)}")
    if missing_destination:
        blocking_issues.append(f"Return destination required: {', '.join(missing_destination)}")
    if occupied_destination:
        blocking_issues.append(f"Selected return destination occupied: {', '.join(occupied_destination)}")

    ready_count = sum(1 for row in assets if row["ready"])
    return {"assets": assets, "ready_count": ready_count, "blocking_issues": blocking_issues}


def _receipt_key(receipt_type: str, source_event_ids: list[int]) -> str:
    return f"{receipt_type}:{'-'.join(str(event_id) for event_id in source_event_ids)}"


def _receipt_holder_snapshot(conn, holder_id: Optional[int]) -> Optional[dict]:
    if holder_id is None:
        return None

    row = conn.execute(
        """
        SELECT id, holder_type, name, organization, organization_id, identifier, email, contact_info
        FROM holders
        WHERE id = ?
        LIMIT 1;
        """,
        (int(holder_id),),
    ).fetchone()
    if row is None:
        return None

    return {
        "id": int(row["id"]),
        "holder_type": str(row["holder_type"] or ""),
        "name": str(row["name"] or ""),
        "organization": str(row["organization"] or ""),
        "organization_id": None if row["organization_id"] is None else int(row["organization_id"]),
        "identifier": str(row["identifier"] or ""),
        "email": str(row["email"] or ""),
        "contact_info": str(row["contact_info"] or ""),
    }


def _receipt_recipient_email(holder_snapshot: object) -> str:
    if not isinstance(holder_snapshot, dict):
        return ""
    return str(holder_snapshot.get("email") or "").strip().lower()


def _receipt_operator_snapshot(conn, user_id: int) -> Optional[dict]:
    row = conn.execute(
        """
        SELECT id, username, role, active
        FROM users
        WHERE id = ?
        LIMIT 1;
        """,
        (int(user_id),),
    ).fetchone()
    if row is None:
        return None

    return {
        "id": int(row["id"]),
        "username": str(row["username"] or ""),
        "role": str(row["role"] or ""),
        "active": bool(int(row["active"] or 0)),
    }


def _receipt_slot_snapshot(conn, slot_id: Optional[int]) -> Optional[dict]:
    if slot_id is None:
        return None

    row = conn.execute(
        """
        SELECT id, case_name, slot_position
        FROM slots
        WHERE id = ?
        LIMIT 1;
        """,
        (int(slot_id),),
    ).fetchone()
    if row is None:
        return None

    return {
        "slot_id": int(row["id"]),
        "case_name": str(row["case_name"] or ""),
        "slot_position": int(row["slot_position"]),
    }


def _receipt_delivery_snapshot(
    *,
    state: str = "pending",
    sent_at: Optional[str] = None,
    last_attempt_at: Optional[str] = None,
    last_error: Optional[str] = None,
    cc_recipients: Optional[list[str]] = None,
) -> dict[str, object]:
    normalized_state = str(state or "").strip().lower()
    if normalized_state not in {"pending", "sent", "failed"}:
        normalized_state = "pending"
    delivery: dict[str, object] = {
        "state": normalized_state,
        "sent_at": sent_at,
        "last_attempt_at": last_attempt_at,
        "last_error": last_error,
    }
    if cc_recipients is not None:
        delivery["cc_recipients"] = _normalized_email_addresses(",".join(cc_recipients))
    return delivery


def _receipt_delivery_cc_recipients(snapshot_delivery: dict[str, object]) -> list[str]:
    raw_cc_recipients = snapshot_delivery.get("cc_recipients")
    if isinstance(raw_cc_recipients, str):
        return _normalized_email_addresses(raw_cc_recipients)
    if isinstance(raw_cc_recipients, list):
        return _normalized_email_addresses(
            ",".join(str(recipient or "") for recipient in raw_cc_recipients)
        )
    return []


def _receipt_delivery_from_row(row: sqlite3.Row, snapshot: dict[str, object]) -> dict[str, object]:
    snapshot_delivery = snapshot.get("delivery")
    has_snapshot_delivery = isinstance(snapshot_delivery, dict)
    if not has_snapshot_delivery:
        snapshot_delivery = {}

    sent_at = str(row["sent_at"] or snapshot_delivery.get("sent_at") or "").strip() or None
    last_attempt_at = str(row["last_attempt_at"] or snapshot_delivery.get("last_attempt_at") or "").strip() or None
    last_error = str(row["last_error"] or snapshot_delivery.get("last_error") or "").strip() or None

    if not has_snapshot_delivery and not sent_at and not last_attempt_at and not last_error:
        return {
            "state": None,
            "sent_at": None,
            "last_attempt_at": None,
            "last_error": None,
            "cc_recipients": [],
        }

    if sent_at:
        state = "sent"
    elif last_error:
        state = "failed"
    else:
        state = str(snapshot_delivery.get("state") or "pending").strip().lower() or "pending"

    delivery = _receipt_delivery_snapshot(
        state=state,
        sent_at=sent_at,
        last_attempt_at=last_attempt_at,
        last_error=last_error,
    )
    delivery["cc_recipients"] = _receipt_delivery_cc_recipients(snapshot_delivery)
    return delivery


def _receipt_row_snapshot(row: sqlite3.Row) -> dict[str, object]:
    snapshot = json.loads(str(row["snapshot_json"] or "{}"))
    if not isinstance(snapshot, dict):
        snapshot = {}
    return snapshot


def _receipt_queue_row_by_id(conn, receipt_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        """
        SELECT
            id,
            receipt_key,
            receipt_type,
            source_event_ids_json,
            snapshot_json,
            commit_at,
            commit_operator_user_id,
            holder_id,
            sent_at,
            last_attempt_at,
            last_error
        FROM receipt_queue
        WHERE id = ?
        LIMIT 1;
        """,
        (int(receipt_id),),
    ).fetchone()


def _receipt_asset_row_snapshot(conn, asset_tag: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        """
        SELECT id, asset_tag, serial_number, equipment_type, manufacturer, model, model_code, notes, building_room
        FROM assets
        WHERE UPPER(asset_tag) = UPPER(?)
           OR REPLACE(REPLACE(UPPER(asset_tag), '-', ''), ' ', '') = UPPER(?)
        LIMIT 1;
        """,
        (asset_tag, asset_tag),
    ).fetchone()


def _receipt_event_rows(conn, source_event_ids: list[int]) -> list[sqlite3.Row]:
    if not source_event_ids:
        raise ValueError("Receipt queue rows require at least one source event.")

    placeholders = ", ".join("?" for _ in source_event_ids)
    rows = conn.execute(
        f"""
        SELECT id, asset_tag, payload, holder_id
        FROM asset_events
        WHERE id IN ({placeholders});
        """,
        tuple(int(event_id) for event_id in source_event_ids),
    ).fetchall()
    rows_by_id = {int(row["id"]): row for row in rows}
    ordered_rows = [rows_by_id[int(event_id)] for event_id in source_event_ids if int(event_id) in rows_by_id]
    if len(ordered_rows) != len(source_event_ids):
        raise ValueError("Receipt queue rows must be derived from stored event history.")
    return ordered_rows


def _receipt_location_context_from_building_room(building_room: str) -> dict[str, str]:
    normalized = str(building_room or "").strip()
    if not normalized:
        return {"building": "", "room": "", "building_room": ""}
    building, separator, room = normalized.partition("/")
    return {
        "building": building,
        "room": room if separator else "",
        "building_room": normalized,
    }


def _build_receipt_snapshot_from_stored_facts(
    conn,
    *,
    receipt_type: str,
    source_event_ids: list[int],
    commit_at: str,
    commit_operator_user_id: int,
) -> dict[str, object]:
    event_rows = _receipt_event_rows(conn, source_event_ids)
    commit_operator_snapshot = _receipt_operator_snapshot(conn, commit_operator_user_id)
    asset_snapshots: list[dict[str, object]] = []

    if receipt_type == "ISSUE":
        holder_ids = {
            int(row["holder_id"])
            for row in event_rows
            if row["holder_id"] is not None
        }
        batch_holder_id = next(iter(holder_ids)) if len(holder_ids) == 1 else None
        batch_holder_snapshot = _receipt_holder_snapshot(conn, batch_holder_id)
        first_payload: dict[str, object] = {}

        for event_row in event_rows:
            payload = json.loads(str(event_row["payload"] or "{}"))
            if not isinstance(payload, dict):
                payload = {}
            if not first_payload:
                first_payload = payload

            asset_tag = str(event_row["asset_tag"] or "").strip()
            asset_row = _receipt_asset_row_snapshot(conn, asset_tag)
            holder_id = None if event_row["holder_id"] is None else int(event_row["holder_id"])
            home_slot_id = payload.get("home_slot_id")
            asset_snapshots.append(
                {
                    "asset_id": None if asset_row is None else int(asset_row["id"]),
                    "asset_tag": str(asset_row["asset_tag"] if asset_row is not None else asset_tag),
                    "serial_number": "" if asset_row is None else str(asset_row["serial_number"] or ""),
                    "equipment_type": "" if asset_row is None else str(asset_row["equipment_type"] or ""),
                    "manufacturer": "" if asset_row is None else str(asset_row["manufacturer"] or ""),
                    "model": "" if asset_row is None else str(asset_row["model"] or ""),
                    "model_code": "" if asset_row is None else str(asset_row["model_code"] or ""),
                    "notes": "" if asset_row is None else str(asset_row["notes"] or ""),
                    "from_location_type": str(payload.get("from_location_type") or ""),
                    "to_location_type": str(payload.get("to_location_type") or ""),
                    "from_building_room": str(payload.get("from_building_room") or ""),
                    "to_building_room": str(payload.get("to_building_room") or ""),
                    "holder_id": holder_id,
                    "holder_snapshot": _receipt_holder_snapshot(conn, holder_id),
                    "home_slot": _receipt_slot_snapshot(
                        conn,
                        int(home_slot_id) if home_slot_id is not None else None,
                    ),
                }
            )

        location_context = _receipt_location_context_from_building_room(str(first_payload.get("to_building_room") or ""))
        return {
            "receipt_type": "ISSUE",
            "commit_at": commit_at,
            "commit_operator_user_id": int(commit_operator_user_id),
            "commit_operator": commit_operator_snapshot,
            "holder_id": batch_holder_id,
            "holder_snapshot": batch_holder_snapshot,
            "recipient_email": _receipt_recipient_email(batch_holder_snapshot),
            "organization_snapshot": None if batch_holder_snapshot is None else {
                "organization": str(batch_holder_snapshot.get("organization") or ""),
                "organization_id": batch_holder_snapshot.get("organization_id"),
            },
            "acknowledgment": (
                dict(first_payload.get("responsibility_ack"))
                if isinstance(first_payload.get("responsibility_ack"), dict)
                else None
            ),
            "location_context": location_context,
            "assets": asset_snapshots,
            "source_event_ids": list(source_event_ids),
            "delivery": _receipt_delivery_snapshot(),
        }

    if receipt_type != "RETURN":
        raise ValueError(f"Unsupported receipt type: {receipt_type}")

    holder_ids: set[int] = set()
    top_level_ack: Optional[dict[str, object]] = None

    for event_row in event_rows:
        payload = json.loads(str(event_row["payload"] or "{}"))
        if not isinstance(payload, dict):
            payload = {}
        responsibility_ack = payload.get("responsibility_ack")
        if not isinstance(responsibility_ack, dict):
            responsibility_ack = {}
        if top_level_ack is None:
            top_level_ack = dict(responsibility_ack)

        from_holder_id = responsibility_ack.get("ack_holder_id")
        normalized_holder_id = int(from_holder_id) if from_holder_id is not None else None
        if normalized_holder_id is not None:
            holder_ids.add(normalized_holder_id)

        asset_tag = str(event_row["asset_tag"] or "").strip()
        asset_row = _receipt_asset_row_snapshot(conn, asset_tag)
        home_slot_id = payload.get("home_slot_id")
        return_slot_id = payload.get("return_slot_id", home_slot_id)
        building_room = "" if asset_row is None else str(asset_row["building_room"] or "")
        asset_snapshots.append(
            {
                "asset_id": None if asset_row is None else int(asset_row["id"]),
                "asset_tag": str(asset_row["asset_tag"] if asset_row is not None else asset_tag),
                "serial_number": "" if asset_row is None else str(asset_row["serial_number"] or ""),
                "equipment_type": "" if asset_row is None else str(asset_row["equipment_type"] or ""),
                "manufacturer": "" if asset_row is None else str(asset_row["manufacturer"] or ""),
                "model": "" if asset_row is None else str(asset_row["model"] or ""),
                "model_code": "" if asset_row is None else str(asset_row["model_code"] or ""),
                "notes": "" if asset_row is None else str(asset_row["notes"] or ""),
                "from_location_type": str(payload.get("from_location_type") or ""),
                "to_location_type": str(payload.get("to_location_type") or ""),
                "from_holder_id": normalized_holder_id,
                "from_holder_snapshot": _receipt_holder_snapshot(conn, normalized_holder_id),
                "to_holder_id": None,
                "from_building_room": building_room,
                "to_building_room": building_room,
                "home_slot": _receipt_slot_snapshot(
                    conn,
                    int(home_slot_id) if home_slot_id is not None else None,
                ),
                "return_slot": _receipt_slot_snapshot(
                    conn,
                    int(return_slot_id) if return_slot_id is not None else None,
                ),
            }
        )

    batch_holder_id = next(iter(holder_ids)) if len(holder_ids) == 1 else None
    batch_holder_snapshot = _receipt_holder_snapshot(conn, batch_holder_id)
    if top_level_ack is not None and len(holder_ids) != 1:
        top_level_ack.pop("ack_holder_id", None)

    return {
        "receipt_type": "RETURN",
        "commit_at": commit_at,
        "commit_operator_user_id": int(commit_operator_user_id),
        "commit_operator": commit_operator_snapshot,
        "holder_id": batch_holder_id,
        "holder_snapshot": batch_holder_snapshot,
        "recipient_email": _receipt_recipient_email(batch_holder_snapshot),
        "organization_snapshot": None if batch_holder_snapshot is None else {
            "organization": str(batch_holder_snapshot.get("organization") or ""),
            "organization_id": batch_holder_snapshot.get("organization_id"),
        },
        "acknowledgment": top_level_ack,
        "assets": asset_snapshots,
        "source_event_ids": list(source_event_ids),
        "delivery": _receipt_delivery_snapshot(),
    }


def _insert_receipt_queue_row(
    conn,
    *,
    receipt_type: str,
    source_event_ids: list[int],
    snapshot: dict[str, object],
    commit_at: str,
    commit_operator_user_id: int,
    holder_id: Optional[int],
) -> int:
    now_iso = datetime.now(timezone.utc).isoformat()
    cursor = conn.execute(
        """
        INSERT INTO receipt_queue (
            receipt_key,
            receipt_type,
            source_event_ids_json,
            snapshot_json,
            commit_at,
            commit_operator_user_id,
            holder_id,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
        """,
        (
            _receipt_key(receipt_type, source_event_ids),
            receipt_type,
            json.dumps(source_event_ids),
            json.dumps(snapshot, sort_keys=True),
            commit_at,
            int(commit_operator_user_id),
            None if holder_id is None else int(holder_id),
            now_iso,
            now_iso,
        ),
    )
    return int(cursor.lastrowid)


def _issue_batch(
    asset_tags: list[str],
    holder_id: int,
    issue_location: dict[str, str],
    responsibility_ack: dict[str, object],
    *,
    commit_operator_user_id: int,
) -> tuple[int, int]:
    if not asset_tags:
        raise ValueError("No assets in the queue to issue.")

    def _canon_asset_row_for_scan_tag(conn, scan_tag: str) -> Optional[dict]:
        return _find_asset_for_scan_tag(conn, scan_tag)

    conn = get_connection()
    try:
        with conn:
            asset_columns = get_asset_table_columns(conn)
            required_columns = {"location_type", "current_holder_id", "building_room"}
            missing_columns = sorted(required_columns - asset_columns)
            if missing_columns:
                raise ValueError(f"Assets table missing columns: {', '.join(missing_columns)}")

            building = str(issue_location.get("building") or "").strip()
            room = str(issue_location.get("room") or "").strip()
            building_room = f"{building}/{room}"

            unknown_tags: list[str] = []
            not_storage: list[str] = []
            retired_assets: list[str] = []
            not_slotted: list[str] = []

            # Map scan tags -> canonical DB rows (so we update/vacate consistently)
            canon_assets: list[tuple[int, str, Optional[int]]] = []

            for scan_tag in asset_tags:
                asset_row = _canon_asset_row_for_scan_tag(conn, scan_tag)
                if not asset_row:
                    unknown_tags.append(scan_tag)
                    continue

                canon_tag = str(asset_row["asset_tag"])
                asset_id = int(asset_row["id"])
                home_slot_id = None if asset_row.get("home_slot_id") is None else int(asset_row["home_slot_id"])
                canon_assets.append((asset_id, canon_tag, home_slot_id))

                location_type = str(asset_row["location_type"] or "").strip().upper()
                if _is_terminal_location_type(location_type):
                    retired_assets.append(canon_tag)
                if location_type != "STORAGE":
                    not_storage.append(canon_tag)

                if _asset_current_slot(conn, int(asset_row["id"]), canon_tag) is None:
                    not_slotted.append(canon_tag)

            if unknown_tags or not_storage or retired_assets or not_slotted:
                parts: list[str] = []
                if unknown_tags:
                    parts.append(f"Unknown asset_tag(s): {', '.join(unknown_tags)}")
                if not_storage:
                    parts.append(f"Not in STORAGE: {', '.join(not_storage)}")
                if retired_assets:
                    parts.append(f"Retired/disposed: {', '.join(retired_assets)}")
                if not_slotted:
                    parts.append(f"Not currently slotted: {', '.join(not_slotted)}")
                raise ValueError("; ".join(parts))

            now_iso = datetime.now(timezone.utc).isoformat()
            event_ids: list[int] = []

            for asset_id, canon_tag, home_slot_id in canon_assets:
                asset_row = conn.execute(
                    """
                    SELECT serial_number, equipment_type, manufacturer, model, model_code, notes, building_room
                    FROM assets
                    WHERE id = ?
                    LIMIT 1;
                    """,
                    (asset_id,),
                ).fetchone()
                previous_building_room = "" if asset_row is None else str(asset_row["building_room"] or "").strip()
                home_slot_snapshot = _receipt_slot_snapshot(conn, home_slot_id)

                update_clauses = ["location_type = ?", "current_holder_id = ?"]
                update_values: list[object] = ["IN_CUSTODY", holder_id]
                if "building" in asset_columns:
                    update_clauses.append("building = ?")
                    update_values.append(building)
                if "room" in asset_columns:
                    update_clauses.append("room = ?")
                    update_values.append(room)
                if "building_room" in asset_columns:
                    update_clauses.append("building_room = ?")
                    update_values.append(building_room)
                update_values.extend([canon_tag, canon_tag])

                conn.execute(
                    f"""
                    UPDATE assets
                    SET {', '.join(update_clauses)}
                    WHERE UPPER(asset_tag) = UPPER(?)
                       OR REPLACE(REPLACE(UPPER(asset_tag), '-', ''), ' ', '') = UPPER(?);
                    """,
                    tuple(update_values),
                )

                current_slot = _asset_current_slot(conn, asset_id, canon_tag)
                if current_slot is None:
                    raise ValueError(f"Not currently slotted: {canon_tag}")

                conn.execute(
                    """
                    DELETE FROM slot_occupancy
                    WHERE asset_id = ?;
                    """,
                    (asset_id,),
                )
                conn.execute(
                    """
                    UPDATE slots
                    SET current_asset_tag = NULL
                    WHERE id = ?;
                    """,
                    (int(current_slot["slot_id"]),),
                )

                event_cursor = conn.execute(
                    """
                    INSERT INTO asset_events (
                        asset_tag,
                        event_type,
                        event_date,
                        actor,
                        notes,
                    payload,
                    holder_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?);
                """,
                    (
                        canon_tag,
                        ISSUE_EVENT_TYPE,
                        now_iso,
                        "system",
                        None,
                        json.dumps(
                            {
                                "from_location_type": "STORAGE",
                                "to_location_type": "IN_CUSTODY",
                                "from_building_room": previous_building_room,
                                "to_building_room": building_room,
                                "home_slot_id": home_slot_id,
                                "responsibility_ack": responsibility_ack,
                            }
                        ),
                        holder_id,
                    ),
                )
                event_ids.append(int(event_cursor.lastrowid))
            snapshot = _build_receipt_snapshot_from_stored_facts(
                conn,
                receipt_type="ISSUE",
                source_event_ids=event_ids,
                commit_at=now_iso,
                commit_operator_user_id=commit_operator_user_id,
            )
            receipt_id = _insert_receipt_queue_row(
                conn,
                receipt_type="ISSUE",
                source_event_ids=event_ids,
                snapshot=snapshot,
                commit_at=now_iso,
                commit_operator_user_id=commit_operator_user_id,
                holder_id=holder_id,
            )

            return len(canon_assets), receipt_id
    finally:
        conn.close()


def _return_batch(
    asset_tags: list[str],
    responsibility_ack: dict[str, object],
    *,
    destination_slot_ids: Optional[dict[str, int]] = None,
    commit_operator_user_id: int,
) -> tuple[int, int]:
    if not asset_tags:
        raise ValueError("No assets in the queue to return")

    def _canon_asset_row_for_scan_tag(conn, scan_tag: str) -> Optional[dict]:
        return _find_asset_for_scan_tag(conn, scan_tag)

    conn = get_connection()
    try:
        with conn:
            asset_columns = get_asset_table_columns(conn)
            required_columns = {"location_type", "current_holder_id", "home_slot_id"}
            missing_columns = sorted(required_columns - asset_columns)
            if missing_columns:
                raise ValueError(f"Assets table missing columns: {', '.join(missing_columns)}")

            unknown_tags: list[str] = []
            not_in_custody: list[str] = []
            retired_assets: list[str] = []
            no_home_slot: list[str] = []
            missing_destination: list[str] = []
            occupied_destination: list[str] = []
            validated_rows: list[dict] = []
            selections = destination_slot_ids or {}
            selected_destinations: dict[int, str] = {}

            for scan_tag in asset_tags:
                asset_row = _canon_asset_row_for_scan_tag(conn, scan_tag)
                if not asset_row:
                    unknown_tags.append(scan_tag)
                    continue

                canon_tag = str(asset_row["asset_tag"])

                location_type = str(asset_row["location_type"] or "").strip().upper()
                if _is_terminal_location_type(location_type):
                    retired_assets.append(canon_tag)
                if location_type != "IN_CUSTODY":
                    not_in_custody.append(canon_tag)

                home_slot_id = asset_row["home_slot_id"]
                if home_slot_id is None:
                    no_home_slot.append(canon_tag)
                    continue

                home_slot = _slot_occupancy_status(conn, home_slot_id)
                if not home_slot:
                    no_home_slot.append(canon_tag)
                    continue
                destination_slot_id = selections.get(canon_tag.upper())
                if destination_slot_id is None and not home_slot["occupied"]:
                    destination_slot_id = int(home_slot["id"])
                if destination_slot_id is None:
                    missing_destination.append(canon_tag)
                    continue
                destination = _slot_occupancy_status(conn, destination_slot_id)
                if destination is None:
                    missing_destination.append(canon_tag)
                    continue
                if destination["occupied"]:
                    occupied_destination.append(canon_tag)
                    continue
                if int(destination["id"]) in selected_destinations:
                    occupied_destination.append(canon_tag)
                    continue
                selected_destinations[int(destination["id"])] = canon_tag

                validated_rows.append(
                    {
                        "asset_id": int(asset_row["id"]),
                        "asset_tag": canon_tag,
                        "home_slot_id": int(home_slot["id"]),
                        "destination_slot_id": int(destination["id"]),
                        "current_holder_id": None if asset_row["current_holder_id"] is None else int(asset_row["current_holder_id"]),
                    }
                )

            if unknown_tags or not_in_custody or retired_assets or no_home_slot or missing_destination or occupied_destination:
                parts: list[str] = []
                if unknown_tags:
                    parts.append(f"Unknown asset_tag(s): {', '.join(unknown_tags)}")
                if not_in_custody:
                    parts.append(f"Not in IN_CUSTODY: {', '.join(not_in_custody)}")
                if retired_assets:
                    parts.append(f"Retired/disposed: {', '.join(retired_assets)}")
                if no_home_slot:
                    parts.append(f"No assigned home slot: {', '.join(no_home_slot)}")
                if missing_destination:
                    parts.append(f"Return destination required: {', '.join(missing_destination)}")
                if occupied_destination:
                    parts.append(f"Selected return destination occupied: {', '.join(occupied_destination)}")
                raise ValueError("; ".join(parts))

            now_iso = datetime.now(timezone.utc).isoformat()
            event_ids: list[int] = []

            for row in validated_rows:
                asset_id = row["asset_id"]
                canon_tag = row["asset_tag"]
                home_slot_id = row["home_slot_id"]
                destination_slot_id = row["destination_slot_id"]
                current_holder_id = row["current_holder_id"]

                destination = _slot_occupancy_status(conn, destination_slot_id)
                if destination is None or destination["occupied"]:
                    raise ValueError(f"Selected return destination became occupied for {canon_tag}")

                conn.execute(
                    """
                    UPDATE assets
                    SET location_type = ?, current_holder_id = NULL
                    WHERE UPPER(asset_tag) = UPPER(?)
                       OR REPLACE(REPLACE(UPPER(asset_tag), '-', ''), ' ', '') = UPPER(?);
                    """,
                    ("STORAGE", canon_tag, canon_tag),
                )

                conn.execute(
                    """
                    INSERT INTO slot_occupancy (slot_id, asset_id, assigned_at)
                    VALUES (?, ?, ?);
                    """,
                    (destination_slot_id, asset_id, now_iso),
                )

                cursor = conn.execute(
                    """
                    UPDATE slots
                    SET current_asset_tag = ?
                    WHERE id = ? AND current_asset_tag IS NULL;
                    """,
                    (canon_tag, destination_slot_id),
                )
                if cursor.rowcount != 1:
                    raise ValueError(f"Selected return destination became occupied for {canon_tag}")

                event_cursor = conn.execute(
                    """
                    INSERT INTO asset_events (
                        asset_tag,
                        event_type,
                        event_date,
                        actor,
                        notes,
                        payload,
                        holder_id
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        canon_tag,
                        RETURN_EVENT_TYPE,
                        now_iso,
                        "system",
                        None,
                        json.dumps(
                            {
                                "from_location_type": "IN_CUSTODY",
                                "to_location_type": "STORAGE",
                                "home_slot_id": home_slot_id,
                                "return_slot_id": destination_slot_id,
                                "responsibility_ack": {
                                    **responsibility_ack,
                                    "ack_holder_id": current_holder_id,
                                },
                            }
                        ),
                        None,
                    ),
                )
                event_ids.append(int(event_cursor.lastrowid))
            snapshot = _build_receipt_snapshot_from_stored_facts(
                conn,
                receipt_type="RETURN",
                source_event_ids=event_ids,
                commit_at=now_iso,
                commit_operator_user_id=commit_operator_user_id,
            )
            receipt_id = _insert_receipt_queue_row(
                conn,
                receipt_type="RETURN",
                source_event_ids=event_ids,
                snapshot=snapshot,
                commit_at=now_iso,
                commit_operator_user_id=commit_operator_user_id,
                holder_id=snapshot.get("holder_id"),
            )

            return len(validated_rows), receipt_id
    finally:
        conn.close()

# Routes

@app.route("/", methods=["GET", "POST"])
def intake():
    if request.method == "GET":
        if current_user() is not None:
            return redirect("/dashboard")
        return render_template("splash.html", error=None)

    if current_user() is not None:
        action = (request.form.get("action") or "").strip().lower()
        scan_text = (request.form.get("scan_text") or "").strip()
        return_to = (request.form.get("return_to") or "").strip()
        return_to_path = _return_to_path(return_to)
        redirect_target = _queue_redirect_target(return_to)
        queue_index_raw = (request.form.get("queue_index") or "").strip()
        submitted_equipment_type = request.form.get("equipment_type")
        current_equipment_type = (session.get("equipment_type") or "laptop").strip() or "laptop"
        if submitted_equipment_type is None:
            selected_equipment_type = current_equipment_type
        else:
            selected_equipment_type = normalize_equipment_type(submitted_equipment_type) or "laptop"
        if not is_approved_new_equipment_type(selected_equipment_type):
            flash(SUPPORTED_EQUIPMENT_TYPE_MESSAGE, "error")
            touch_session()
            if redirect_target.startswith("/") and not redirect_target.startswith("//"):
                return redirect(redirect_target)
            return redirect(url_for("add_assets"))
        session["equipment_type"] = selected_equipment_type

        if action == "clear":
            SCAN_QUEUE.clear()
            session.pop(CASE_DETAIL_QUEUE_WORKFLOW_SESSION_KEY, None)
        elif action == "remove":
            try:
                queue_index = int(queue_index_raw)
            except ValueError:
                queue_index = -1

            if 0 <= queue_index < len(SCAN_QUEUE):
                SCAN_QUEUE.pop(queue_index)

        should_validate_empty_scan = (
            action == ""
            and not scan_text
            and return_to_path in {"", "/add-assets"}
        )
        if should_validate_empty_scan:
            flash("Enter or scan an asset tag before adding it to the queue.", "error")
            touch_session()
            if redirect_target.startswith("/") and not redirect_target.startswith("//"):
                return redirect(redirect_target)
            return redirect(url_for("add_assets"))

        if scan_text:
            if return_to_path == "/issue":
                selected_holder = _selected_holder_from_session(require_active=True)
                if selected_holder is not None:
                    issue_location_form, issue_location_errors, _ = _validate_issue_location_form(
                        selected_holder,
                        _issue_location_form_for_holder(selected_holder),
                    )
                    if not issue_location_errors:
                        session["issue_building"] = issue_location_form["building"]
                        session["issue_room"] = issue_location_form["room"]
                        _remember_last_issue_location(issue_location_form)

            value = sanitize_scan(scan_text)
            if not value:
                flash("Scan rejected. Enter a valid asset tag.", "error")
                touch_session()
                if redirect_target.startswith("/") and not redirect_target.startswith("//"):
                    return redirect(redirect_target)
                return redirect(url_for("add_assets"))

            case_name = (request.form.get("case_name") or "").strip().upper()
            slot_id_raw = (request.form.get("slot_id") or "").strip()
            home_slot_id: Optional[int] = None
            slot_position: Optional[int] = None
            requires_inventory_validation = return_to_path in {"/issue", "/return"}
            if requires_inventory_validation or case_name or slot_id_raw:
                conn = get_connection()
                try:
                    case_match = None
                    if return_to_path in {"/issue", "/return"}:
                        try:
                            if return_to_path == "/issue":
                                case_match = _find_case_assets_for_scan_tag(conn, value)
                            else:
                                case_match = _find_return_case_assets_for_scan_tag(conn, value)
                        except ValueError as e:
                            flash(str(e), "error")
                            touch_session()
                            if redirect_target.startswith("/") and not redirect_target.startswith("//"):
                                return redirect(redirect_target)
                            return redirect(url_for("add_assets"))

                        if case_match is not None:
                            matched_case_name = str(case_match["case_name"] or value).strip().upper()
                            case_assets = list(case_match["assets"])
                            if not case_assets:
                                flash(f"Case {matched_case_name} has no assets to add.", "error")
                                touch_session()
                                if redirect_target.startswith("/") and not redirect_target.startswith("//"):
                                    return redirect(redirect_target)
                                return redirect(url_for("add_assets"))

                            added_count = 0
                            skipped_count = 0
                            for row in case_assets:
                                asset_tag = str(row["asset_tag"] or "").strip().upper()
                                if _queue_contains_asset_tag(asset_tag):
                                    skipped_count += 1
                                    continue
                                SCAN_QUEUE.append(
                                    Scan.now(
                                        asset_tag,
                                        equipment_type=selected_equipment_type,
                                        home_slot_id=int(row["home_slot_id"]),
                                        case_name=str(row["case_name"] or ""),
                                        slot_position=int(row["slot_position"]),
                                    )
                                )
                                added_count += 1

                            if added_count > 0:
                                message = f"Case {matched_case_name} added {added_count} asset"
                                if added_count != 1:
                                    message += "s"
                                message += " to queue."
                                if skipped_count > 0:
                                    message += f" Skipped {skipped_count} already queued."
                                flash(message, "success")
                            else:
                                flash(f"Case {matched_case_name} is already fully queued.", "error")

                            touch_session()
                            if redirect_target.startswith("/") and not redirect_target.startswith("//"):
                                return redirect(redirect_target)
                            return redirect(url_for("add_assets"))

                    if _queue_contains_asset_tag(value):
                        flash(f"Asset {value} is already queued.", "error")
                        touch_session()
                        if redirect_target.startswith("/") and not redirect_target.startswith("//"):
                            return redirect(redirect_target)
                        return redirect(url_for("add_assets"))

                    if requires_inventory_validation and _find_asset_for_scan_tag(conn, value) is None:
                        flash("Scan rejected. Asset tag not found in inventory.", "error")
                        touch_session()
                        if redirect_target.startswith("/") and not redirect_target.startswith("//"):
                            return redirect(redirect_target)
                        return redirect(url_for("add_assets"))

                    if case_name or slot_id_raw:
                        selected_slot, slot_errors = _resolve_slot_selection(
                            conn,
                            case_name=case_name,
                            slot_id_raw=slot_id_raw,
                        )
                        if slot_errors:
                            flash("; ".join(slot_errors), "error")
                            touch_session()
                            if redirect_target.startswith("/") and not redirect_target.startswith("//"):
                                return redirect(redirect_target)
                            return redirect(url_for("add_assets"))
                        if selected_slot is not None:
                            home_slot_id = int(selected_slot["id"])
                            case_name = str(selected_slot["case_name"])
                            slot_position = int(selected_slot["slot_position"])
                finally:
                    conn.close()
            elif _queue_contains_asset_tag(value):
                flash(f"Asset {value} is already queued.", "error")
                touch_session()
                if redirect_target.startswith("/") and not redirect_target.startswith("//"):
                    return redirect(redirect_target)
                return redirect(url_for("add_assets"))

            SCAN_QUEUE.append(
                Scan.now(
                    value,
                    equipment_type=selected_equipment_type,
                    home_slot_id=home_slot_id,
                    case_name=case_name,
                    slot_position=slot_position,
                )
            )

        touch_session()

        if redirect_target.startswith("/") and not redirect_target.startswith("//"):
            return redirect(redirect_target)
        return redirect(url_for("add_assets"))

    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    login_rate_limit_key = _login_rate_limit_key()
    if _login_is_rate_limited(login_rate_limit_key):
        return render_template("splash.html", error="Too many login attempts. Wait and try again."), 403

    user = get_user_by_username(username)
    if user is None or not verify_password(user, password):
        _record_login_failure(login_rate_limit_key)
        return render_template("splash.html", error="Invalid login"), 403

    role = str(user.get("role") or "").strip().lower()
    active = int(user.get("active") or 0) == 1
    if role not in {"admin", "operator"} or not active:
        session.pop("user_id", None)
        _record_login_failure(login_rate_limit_key)
        return render_template("splash.html", error="Access denied"), 403

    _clear_login_failures(login_rate_limit_key)
    begin_auth_session(int(user["id"]))
    return redirect("/dashboard")


@app.get("/add-assets")
def add_assets():
    if current_user() is None:
        return redirect(url_for("intake"))

    if session.get("last_seen") is None:
        touch_session()

    slot_options: list[dict] = []
    case_options: list[str] = []
    conn = get_connection()
    try:
        slot_options = _list_slot_options(conn)
        case_options = _slot_case_options(slot_options)
    finally:
        conn.close()

    return render_template(
        "index.html",
        auth_enabled=auth_enabled(),
        authed=is_authed(),
        last_seen_age_seconds=seconds_since_last_seen(),
        timeout_seconds=INTAKE_TIMEOUT_SECONDS,
        queue=SCAN_QUEUE,
        queue_len=len(SCAN_QUEUE),
        latest=(SCAN_QUEUE[-1].asset_tag if SCAN_QUEUE else ""),
        equipment_type=(
            normalize_equipment_type(session.get("equipment_type"))
            if is_approved_new_equipment_type(session.get("equipment_type"))
            else "laptop"
        ),
        equipment_type_options=_equipment_type_form_options(),
        slot_options=slot_options,
        case_options=case_options,
    )


@app.get("/demo")
def demo():
    return render_template("demo.html", **_demo_page_context())


@app.post("/demo/send-sample-receipt")
def demo_send_sample_receipt():
    token = str(request.form.get("token") or request.args.get("token") or "").strip()
    if not _demo_token_is_valid(token):
        abort(404)

    send_state = _demo_receipt_send_state()
    if int(send_state["count"]) >= DEMO_RECEIPT_SEND_LIMIT:
        flash("Demo send limit reached for this session.", "error")
        return redirect(url_for("demo", token=token))

    now_utc = datetime.now(timezone.utc)
    last_sent_at = str(send_state["last_sent_at"] or "").strip()
    if last_sent_at:
        try:
            last_sent_dt = datetime.fromisoformat(last_sent_at)
        except ValueError:
            last_sent_dt = None
        if last_sent_dt is not None and (now_utc - last_sent_dt).total_seconds() < DEMO_RECEIPT_COOLDOWN_SECONDS:
            flash("Please wait before sending another sample receipt.", "error")
            return redirect(url_for("demo", token=token))

    try:
        recipient_email = _normalize_demo_email(request.form.get("email"))
        sent_to = _send_demo_receipt_email(recipient_email)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("demo", token=token))
    except Exception as exc:
        flash(f"Demo receipt email failed: {exc}", "error")
        return redirect(url_for("demo", token=token))

    session["demo_receipt_send_count"] = int(send_state["count"]) + 1
    session["demo_receipt_last_sent_at"] = now_utc.isoformat()
    touch_session()
    flash(f"Demo receipt sent to {sent_to}.", "success")
    return redirect(url_for("demo", token=token))


@app.post("/add-assets/review")
@require_login
@require_role("admin")
def add_assets_review():
    if len(SCAN_QUEUE) == 0:
        flash("Queue is empty. Add at least one asset to the queue before reviewing the batch.", "error")
        return redirect(url_for("add_assets"))

    return redirect(url_for("preview"))


@app.route("/bootstrap/admin", methods=["GET", "POST"])
def bootstrap_admin():
    if count_users() != 0:
        return render_template("bootstrap_disabled.html"), 403

    if request.method == "GET":
        return render_template("bootstrap_admin.html", error=None)

    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    confirm_password = request.form.get("confirm_password") or ""

    if not username or not password:
        return render_template("bootstrap_admin.html", error="Username and password are required."), 400
    if password != confirm_password:
        return render_template("bootstrap_admin.html", error="Passwords do not match."), 400

    try:
        user = create_user(username=username, password=password, role="admin", active=True)
    except ValueError as e:
        return render_template("bootstrap_admin.html", error=str(e)), 400
    except sqlite3.IntegrityError:
        return render_template("bootstrap_admin.html", error="Username already exists."), 400

    begin_auth_session(int(user["id"]))
    return redirect("/dashboard")


@app.get("/logout")
def logout():
    clear_auth_session()
    return redirect("/")


@app.get("/account/change-password")
@require_login
def account_change_password():
    return render_template("account_change_password.html")


@app.post("/account/change-password")
@require_login
def account_change_password_submit():
    user = current_user()
    if user is None:
        return {"ok": False, "error": "Forbidden"}, 403

    password_was_temporary = is_temporary_password(user)
    current_password = request.form.get("current_password") or ""
    new_password = request.form.get("new_password") or ""
    confirm_new_password = request.form.get("confirm_new_password") or ""

    if new_password != confirm_new_password:
        flash("New password and confirmation must match.", "error")
        return redirect(url_for("account_change_password"))

    try:
        change_own_password(int(user["id"]), current_password, new_password)
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("account_change_password"))

    flash("Password updated.", "success")
    if password_was_temporary:
        return redirect(url_for("dashboard"))
    return redirect(url_for("account_change_password"))


FIRST_RUN_OPERATIONAL_TABLES = ("assets", "holders", "slots", "asset_events")


def _database_has_operational_data(conn: sqlite3.Connection) -> bool:
    for table_name in FIRST_RUN_OPERATIONAL_TABLES:
        row = conn.execute(f"SELECT 1 FROM {table_name} LIMIT 1;").fetchone()
        if row is not None:
            return True
    return False


@app.get("/dashboard")
@require_login
def dashboard():
    threshold_days = get_custody_days_threshold(
        os.getenv("ASSETTRACK_CUSTODY_DAYS_THRESHOLD"),
        default=30,
    )

    conn = get_connection()
    try:
        dashboard_data = build_dashboard_data(
            conn,
            custody_days_threshold=threshold_days,
        )
        user = current_user()
        user_role = str((user or {}).get("role") or "").strip().lower()
        show_first_run_guide = user_role == "admin" and not _database_has_operational_data(conn)
    finally:
        conn.close()

    return render_template(
        "dashboard.html",
        dashboard=dashboard_data,
        custody_days_threshold=threshold_days,
        show_first_run_guide=show_first_run_guide,
    )


@app.get("/dashboard/holders")
@require_login
def dashboard_holders():
    return_to = _safe_local_return_to(request.args.get("return_to") or "")
    conn = get_connection()
    try:
        holders = list_holders_in_custody(conn)
    finally:
        conn.close()

    return render_template(
        "dashboard_holders.html",
        holders=holders,
        return_to=return_to,
    )


@app.get("/dashboard/holders/<int:holder_id>")
@require_login
def dashboard_holder_detail(holder_id: int):
    return_to = _safe_local_return_to(request.args.get("return_to") or "")
    conn = get_connection()
    try:
        detail = get_holder_custody_detail(conn, holder_id)
    finally:
        conn.close()

    if detail is None:
        abort(404)

    return render_template(
        "dashboard_holder_detail.html",
        holder=detail,
        return_to=return_to,
    )


@app.get("/dashboard/cases")
@require_login
def dashboard_cases():
    return_to = _safe_local_return_to(request.args.get("return_to") or "")
    case_query = (request.args.get("q") or "").strip()
    conn = get_connection()
    try:
        cases = list_case_summaries(conn)
    finally:
        conn.close()

    if case_query:
        query_upper = case_query.upper()
        cases = [row for row in cases if query_upper in str(row.get("case_name") or "").upper()]

    return render_template(
        "dashboard_cases.html",
        cases=cases,
        return_to=return_to,
        case_query=case_query,
    )


@app.get("/dashboard/cases/<case_name>")
@require_login
def dashboard_case_detail(case_name: str):
    return_to = _safe_local_return_to(request.args.get("return_to") or "")
    conn = get_connection()
    try:
        detail = get_case_slot_detail(conn, case_name)
    finally:
        conn.close()

    if detail is None:
        abort(404)

    return render_template(
        "dashboard_case_detail.html",
        case_detail=detail,
        case_size_options=CASE_SIZE_OPTIONS,
        return_to=return_to,
    )


@app.post("/admin/cases/<case_name>/case-size")
@require_login
@require_role("admin")
def admin_case_size_update(case_name: str):
    guard_result = _require_admin_for_route()
    if guard_result:
        return guard_result

    return_to = _safe_local_return_to(request.form.get("return_to") or "")
    conn = get_connection()
    try:
        with conn:
            exists_row = conn.execute(
                """
                SELECT case_name
                FROM slots
                WHERE UPPER(case_name) = UPPER(?)
                ORDER BY case_name COLLATE NOCASE ASC
                LIMIT 1;
                """,
                (case_name,),
            ).fetchone()
            if exists_row is None:
                abort(404)
            canonical_case = str(exists_row["case_name"] or case_name)
            saved_size = save_case_size(conn, canonical_case, request.form.get("case_size"))
    except ValueError as exc:
        flash(str(exc), "error")
        canonical_case = case_name
    finally:
        conn.close()

    if saved_size:
        flash(f"Saved Case Size for {canonical_case}: {saved_size}.", "success")
    elif "saved_size" in locals():
        flash(f"Cleared Case Size for {canonical_case}.", "success")
    if return_to:
        return redirect(url_for("dashboard_case_detail", case_name=canonical_case, return_to=return_to))
    return redirect(url_for("dashboard_case_detail", case_name=canonical_case))


@app.post("/dashboard/cases/<case_name>/queue")
@require_login
def dashboard_case_queue_start(case_name: str):
    action = str(request.form.get("workflow_action") or "").strip().lower()
    return_to = _safe_local_return_to(request.form.get("return_to") or "")
    selected_tags = _selected_case_asset_tags()

    def _case_detail_redirect():
        if return_to is not None:
            return redirect(url_for("dashboard_case_detail", case_name=case_name, return_to=return_to))
        return redirect(url_for("dashboard_case_detail", case_name=case_name))

    if action not in {"issue", "return"}:
        flash("Choose Start Issue or Start Return.", "error")
        return _case_detail_redirect()

    if not selected_tags:
        flash("Select at least one asset before starting a queue.", "error")
        return _case_detail_redirect()

    existing_queue_workflow = _case_detail_existing_queue_workflow()
    if existing_queue_workflow and existing_queue_workflow != action:
        flash("Finish or clear the current queue before starting a different action.", "error")
        return _case_detail_redirect()

    conn = get_connection()
    try:
        detail = get_case_slot_detail(conn, case_name)
        if detail is None:
            abort(404)
        if action == "issue":
            added_count, invalid_tags = _queue_case_detail_issue_selection(conn, case_name, selected_tags)
        else:
            added_count, invalid_tags = _queue_case_detail_return_selection(conn, case_name, selected_tags)
    finally:
        conn.close()

    if invalid_tags:
        flash(
            "Queue not started. These assets are no longer eligible: "
            + ", ".join(invalid_tags)
            + ".",
            "error",
        )
        return _case_detail_redirect()

    if added_count == 0:
        flash("Selected assets are already queued.", "error")
        return _case_detail_redirect()

    touch_session()
    session[CASE_DETAIL_QUEUE_WORKFLOW_SESSION_KEY] = action
    if action == "issue":
        session["issue_mode"] = True
        flash(f"Added {added_count} selected assets to the Issue queue.", "success")
        return redirect(url_for("issue"))

    flash(f"Added {added_count} selected assets to the Return queue.", "success")
    return redirect(url_for("return_queue"))


@app.get("/assets/search")
@require_login
def asset_search():
    authed = enforce_inactivity_timeout()
    if auth_enabled() and not authed:
        flash("Locked. Re-enter access code.", "error")
        return redirect(url_for("intake"))

    return_to = _safe_local_return_to(request.args.get("return_to") or "")
    form_state = {
        "asset_tag": (request.args.get("asset_tag") or "").strip().upper(),
        "serial_number": (request.args.get("serial_number") or "").strip(),
    }
    assets: list[dict] = []
    case_matches: list[dict[str, object]] = []
    error_message: Optional[str] = None
    lookup_mode = "none"

    if form_state["asset_tag"] or form_state["serial_number"]:
        conn = get_connection()
        try:
            assets, error_message, lookup_mode = _lookup_asset_for_verification(
                conn,
                asset_tag=form_state["asset_tag"],
                serial_number=form_state["serial_number"],
            )
            if form_state["asset_tag"] and not form_state["serial_number"]:
                case_matches = _lookup_cases_for_asset_search(conn, form_state["asset_tag"])
                if case_matches:
                    error_message = None
        finally:
            conn.close()

    receipt_return_args: dict[str, str] = {}
    if form_state["asset_tag"]:
        receipt_return_args["asset_tag"] = form_state["asset_tag"]
    if form_state["serial_number"]:
        receipt_return_args["serial_number"] = form_state["serial_number"]
    if _safe_report_return_to(return_to):
        receipt_return_args["return_to"] = return_to
    receipt_detail_return_to = url_for("asset_search", **receipt_return_args)

    return render_template(
        "asset_search.html",
        form=form_state,
        assets=assets,
        case_matches=case_matches,
        error_message=error_message,
        lookup_mode=lookup_mode,
        return_to=return_to,
        receipt_detail_return_to=receipt_detail_return_to,
    )


@app.get("/assets/history")
@require_login
def asset_history():
    authed = enforce_inactivity_timeout()
    if auth_enabled() and not authed:
        flash("Locked. Re-enter access code.", "error")
        return redirect(url_for("intake"))

    asset_tag = (request.args.get("asset_tag") or "").strip().upper()
    return_to = _safe_local_return_to(request.args.get("return_to") or "")
    if not asset_tag:
        return redirect(url_for("asset_search"))

    conn = get_connection()
    try:
        history = _build_asset_history_view(conn, asset_tag)
    finally:
        conn.close()

    if history is None:
        abort(404)

    return render_template("asset_history.html", history=history, return_to=return_to)


@app.get("/preview")
@require_login
def preview():
    parsed_rows = build_parsed_rows_from_queue()
    validation = validate_rows(parsed_rows)
    is_valid = bool(validation.get("valid")) if isinstance(validation, dict) else False

    if wants_json():
        rows = [r["data"] for r in parsed_rows]
        return {"count": len(rows), "valid": is_valid, "result": validation, "rows": rows}

    return render_template(
        "preview.html",
        row_count=len(parsed_rows),
        parsed_rows=parsed_rows,
        valid=is_valid,
        validation=validation,
        equipment_type=(session.get("equipment_type") or "laptop").strip() or "laptop",
        selected_holder=_selected_holder_from_session(require_active=bool(session.get("issue_mode"))),
        issue_mode=bool(session.get("issue_mode")),
        last_seen_age_seconds=seconds_since_last_seen(),
        timeout_seconds=INTAKE_TIMEOUT_SECONDS,
    )


@app.get("/preview/validate")
@require_login
def preview_validate():
    parsed_rows = build_parsed_rows_from_queue()
    result = validate_rows(parsed_rows)

    return {
        "row_count": len(parsed_rows),
        "valid": bool(result.get("valid")) if isinstance(result, dict) else False,
        "result": result,
    }

@app.post("/preview/mode")
@require_login
@require_role("admin")
def preview_mode():
    authed = enforce_inactivity_timeout()
    if auth_enabled() and not authed:
        flash("Locked. Re-enter access code.", "error")
        return redirect(url_for("intake"))

    enabled = (request.form.get("issue_mode") or "").strip().lower() in {"on", "true", "1", "yes"}
    session["issue_mode"] = bool(enabled)

    # If turning off issue mode, clear holder selection to avoid confusion.
    if not enabled:
        session.pop("holder_id", None)

    touch_session()
    return redirect(url_for("preview"))

@app.post("/preview/discard")
@require_login
@require_role("admin")
def preview_discard():
    # Enforce auth and inactivity timeout for discard requests.
    authed = enforce_inactivity_timeout()
    return_to = (request.form.get("return_to") or "").strip()
    return_to_path = _return_to_path(return_to)
    if auth_enabled() and not authed:
        if wants_json():
            return {"ok": False, "discarded": 0, "error": "Locked"}, 401
        flash("Locked. Re-enter access code.", "error")
        return redirect(url_for("intake"))

    discarded = len(SCAN_QUEUE)
    SCAN_QUEUE.clear()
    session.pop(CASE_DETAIL_QUEUE_WORKFLOW_SESSION_KEY, None)
    if return_to_path != "/issue":
        session.pop("holder_id", None)

    # Reset UI defaults back to laptop (same invariant as intake()).
    session["equipment_type"] = "laptop"
    touch_session()

    if wants_json():
        return {"ok": True, "discarded": discarded}

    flash("Batch discarded.", "success")
    if return_to.startswith("/") and not return_to.startswith("//"):
        return redirect(return_to)
    return redirect(url_for("add_assets"))

@app.post("/preview/commit")
@require_login
@require_role("admin")
def preview_commit():
    # Enforce auth and inactivity timeout for commit requests.
    authed = enforce_inactivity_timeout()
    if auth_enabled() and not authed:
        if wants_json():
            return {"ok": False, "committed": 0, "error": "Locked"}, 401
        flash("Locked. Re-enter access code.", "error")
        return redirect(url_for("intake"))

    # Require deliberate confirmation before adding to the database.
    confirmed = (request.form.get("confirm_reviewed") or "").strip().lower() in {"on", "true", "1", "yes"}
    if not confirmed:
        if wants_json():
            return {
                "ok": False,
                "committed": 0,
                "error": "Please confirm you reviewed the batch before adding it.",
            }, 400
        flash("Please confirm you reviewed the batch before adding it.", "error")
        return redirect(url_for("preview"))

    issue_mode = bool(session.get("issue_mode"))

    # Normal intake commit mode

    if not issue_mode:
        parsed_rows = build_parsed_rows_from_queue()

        validation = validate_rows(parsed_rows)
        is_valid = bool(validation.get("valid")) if isinstance(validation, dict) else False
        if not is_valid:
            if wants_json():
                return {
                    "ok": False,
                    "committed": 0,
                    "error": "Validation failed",
                    "result": validation,
                }, 400
            flash("Fix the batch before adding to the database.", "error")
            return redirect(url_for("preview"))

        equipment_type = (session.get("equipment_type") or "").strip()
        if not equipment_type:
            if wants_json():
                return {
                    "ok": False,
                    "committed": 0,
                    "error": "Equipment type is required to create new assets",
                }, 400
            flash("Equipment type is required before adding new assets to the database.", "error")
            return redirect(url_for("preview"))

        try:
            result = commit_batch(parsed_rows)
        except BatchCommitError as e:
            if wants_json():
                return {"ok": False, "committed": 0, "error": str(e)}, 400
            flash(f"Could not add items to the database: {e}", "error")
            return redirect(url_for("preview"))

        SCAN_QUEUE.clear()
        session.pop(CASE_DETAIL_QUEUE_WORKFLOW_SESSION_KEY, None)
        session.pop("holder_id", None)  # keep tidy; holder is only meaningful for issue mode
        touch_session()

        if wants_json():
            return {"ok": True, "committed": result.committed_count}

        count = result.committed_count
        noun = "item" if count == 1 else "items"
        flash(f"Added {count} {noun} to the database.", "success")
        return redirect(url_for("add_assets"))

    # Issue commit mode

    holder = _selected_holder_from_session(require_active=True)
    if holder is None:
        if wants_json():
            return {
                "ok": False,
                "committed": 0,
                "error": "Select a holder before issuing assets.",
            }, 400
        flash("Select a holder before issuing assets.", "error")
        return redirect(url_for("preview"))

    issue_location_form, issue_location_errors, _ = _validate_issue_location_form(
        holder,
        _issue_location_form_for_holder(holder),
    )
    if issue_location_errors:
        if wants_json():
            return {"ok": False, "committed": 0, "error": "; ".join(issue_location_errors)}, 400
        flash("; ".join(issue_location_errors), "error")
        return redirect(url_for("issue"))
    _remember_last_issue_location(issue_location_form)

    asset_tags = _queue_asset_tags()
    user = current_user()
    if user is None:
        if wants_json():
            return {"ok": False, "committed": 0, "error": "Authenticated operator not found."}, 400
        flash("Authenticated operator not found.", "error")
        return redirect(url_for("preview"))
    responsibility_ack = {
        "acknowledged": True,
        "ack_holder_id": int(holder["id"]),
        "ack_operator_user_id": int(user["id"]),
        "ack_at": datetime.now(timezone.utc).isoformat(),
        "ack_scope": "batch",
    }

    try:
        committed_count, receipt_id = _issue_batch(
            asset_tags,
            holder["id"],
            issue_location_form,
            responsibility_ack,
            commit_operator_user_id=int(user["id"]),
        )
    except ValueError as e:
        if wants_json():
            return {"ok": False, "committed": 0, "error": str(e)}, 400
        flash(f"Issue failed: {e}", "error")
        return redirect(url_for("preview"))

    SCAN_QUEUE.clear()
    session.pop(CASE_DETAIL_QUEUE_WORKFLOW_SESSION_KEY, None)
    touch_session()

    if wants_json():
        return {"ok": True, "committed": committed_count}

    flash(f"Issue {committed_count} assets.", "success")
    return redirect(url_for("issue"))


@app.get("/issue")
@require_login
def issue():
    authed = enforce_inactivity_timeout()
    if auth_enabled() and not authed:
        flash("Locked. Re-enter access code.", "error")
        return redirect(url_for("add_assets"))

    if not bool(session.get("issue_mode")):
        session["issue_mode"] = True

    if session.get("last_seen") is None:
        touch_session()

    selected_holder = _selected_holder_from_session(require_active=True)
    issue_location_form, issue_location_errors, issue_location_context = _validate_issue_location_form(
        selected_holder,
        _issue_location_form_for_holder(selected_holder),
    )
    session["issue_building"] = issue_location_form["building"]
    session["issue_room"] = issue_location_form["room"]
    if not issue_location_errors:
        _remember_last_issue_location(issue_location_form)
    asset_tags = _queue_asset_tags()
    issue_state = _build_issue_preview_state(asset_tags, selected_holder)
    issued_count_raw = (request.args.get("issued") or "").strip()
    issued_count = 0
    if issued_count_raw:
        try:
            issued_count = max(0, int(issued_count_raw))
        except ValueError:
            issued_count = 0
    workflow_banner_outcome = None
    if issued_count > 0 and not asset_tags:
        workflow_banner_outcome = (
            f"Issued {issued_count} asset successfully."
            if issued_count == 1
            else f"Issued {issued_count} assets successfully."
        )

    return render_template(
        "issue_queue.html",
        page_title="Issue",
        page_heading="Issue",
        scan_heading="Add to Queue",
        workflow_banner_title="Issue",
        workflow_banner_queued_count=len(asset_tags),
        workflow_banner_outcome=workflow_banner_outcome,
        workflow_banner_change_holder_href=url_for("holders_search", return_to=url_for("issue")),
        return_to=url_for("issue"),
        preview_url=url_for("issue_preview"),
        preview_label="Review Before Issue",
        auth_enabled=auth_enabled(),
        authed=is_authed(),
        last_seen_age_seconds=seconds_since_last_seen(),
        timeout_seconds=INTAKE_TIMEOUT_SECONDS,
        queue=SCAN_QUEUE,
        queue_len=len(SCAN_QUEUE),
        latest=(SCAN_QUEUE[-1].asset_tag if SCAN_QUEUE else ""),
        equipment_type=(session.get("equipment_type") or "laptop").strip() or "laptop",
        queued_count=len(asset_tags),
        ready_count=issue_state["ready_count"],
        blocking_issues=issue_state["blocking_issues"],
        selected_holder=selected_holder,
        issue_location_form=issue_location_form,
        issue_location_errors=issue_location_errors,
        issue_location_building_options=issue_location_context["building_options"],
        issue_location_constrained_by_org=issue_location_context["constrained_by_org"],
        issue_location_ready=not issue_location_errors,
        issue_location_label=_issue_location_label(issue_location_form),
    )


@app.post("/issue/location")
@require_login
def issue_location_update():
    authed = enforce_inactivity_timeout()
    if auth_enabled() and not authed:
        flash("Locked. Re-enter access code.", "error")
        return redirect(url_for("add_assets"))

    selected_holder = _selected_holder_from_session(require_active=True)
    if selected_holder is None:
        flash("Select a holder before issuing assets.", "error")
        return redirect(url_for("holders_search", return_to=url_for("issue")))

    issue_location_form, issue_location_errors, _ = _validate_issue_location_form(
        selected_holder,
        {
            "building": (request.form.get("building") or "").strip(),
            "room": (request.form.get("room") or "").strip(),
        },
    )
    session["issue_building"] = issue_location_form["building"]
    session["issue_room"] = issue_location_form["room"]
    touch_session()

    if issue_location_errors:
        flash("; ".join(issue_location_errors), "error")
    else:
        _remember_last_issue_location(issue_location_form)
        flash(f"Current location set to {_issue_location_label(issue_location_form)}.", "success")

    return redirect(url_for("issue"))


@app.get("/issue/preview")
@require_login
def issue_preview():
    issue_mode = bool(session.get("issue_mode"))
    if not issue_mode:
        flash("Use the Issue workflow before opening Issue Assets Preview.", "error")
        return redirect(url_for("issue"))

    selected_holder = _selected_holder_from_session(require_active=True)
    issue_location_form, issue_location_errors, issue_location_context = _validate_issue_location_form(
        selected_holder,
        _issue_location_form_for_holder(selected_holder),
    )
    session["issue_building"] = issue_location_form["building"]
    session["issue_room"] = issue_location_form["room"]
    if not issue_location_errors:
        _remember_last_issue_location(issue_location_form)
    asset_tags = _queue_asset_tags()
    if not asset_tags:
        flash("Queue is empty. Scan assets before opening Issue Preview.", "error")
        return redirect(url_for("issue"))
    issue_preview_state = _build_issue_preview_state(asset_tags, selected_holder)

    return render_template(
        "issue_preview.html",
        issue_mode=issue_mode,
        selected_holder=selected_holder,
        workflow_banner_title="Issue Preview",
        workflow_banner_queued_count=len(asset_tags),
        queued_count=len(asset_tags),
        assets=issue_preview_state["assets"],
        ready_count=issue_preview_state["ready_count"],
        blocking_issues=issue_preview_state["blocking_issues"],
        last_seen_age_seconds=seconds_since_last_seen(),
        timeout_seconds=INTAKE_TIMEOUT_SECONDS,
        issue_location_form=issue_location_form,
        issue_location_errors=issue_location_errors,
        issue_location_building_options=issue_location_context["building_options"],
        issue_location_constrained_by_org=issue_location_context["constrained_by_org"],
        issue_location_label=_issue_location_label(issue_location_form),
    )


@app.post("/issue/commit")
@require_login
def issue_commit():
    authed = enforce_inactivity_timeout()
    if auth_enabled() and not authed:
        if wants_json():
            return {"ok": False, "committed": 0, "error": "Locked"}, 401
        flash("Locked. Re-enter access code.", "error")
        return redirect(url_for("issue"))

    issue_mode = bool(session.get("issue_mode"))
    if not issue_mode:
        if wants_json():
            return {"ok": False, "committed": 0, "error": "Issue mode is not enabled."}, 400
        flash("Enable issue mode before issuing assets.", "error")
        return redirect(url_for("preview"))

    confirmed = (request.form.get("confirm_reviewed") or "").strip().lower() in {"on", "true", "1", "yes"}
    if not confirmed:
        if wants_json():
            return {
                "ok": False,
                "committed": 0,
                "error": "Please confirm you reviewed the batch before adding it.",
            }, 400
        flash("Please confirm you reviewed the batch before adding it.", "error")
        return redirect(url_for("issue_preview"))

    acknowledged = (request.form.get("confirm_responsibility_ack") or "").strip().lower() in {"on", "true", "1", "yes"}
    if not acknowledged:
        if wants_json():
            return {
                "ok": False,
                "committed": 0,
                "error": "Confirm responsibility acknowledgment before issuing assets.",
            }, 400
        flash("Confirm responsibility acknowledgment before issuing assets.", "error")
        preview_response = issue_preview()
        return preview_response, 400

    holder = _selected_holder_from_session(require_active=True)
    if holder is None:
        if wants_json():
            return {"ok": False, "committed": 0, "error": "Select a holder before issuing assets."}, 400
        flash("Select a holder before issuing assets.", "error")
        return redirect(url_for("issue_preview"))

    user = current_user()
    if user is None:
        if wants_json():
            return {"ok": False, "committed": 0, "error": "Authenticated operator not found."}, 400
        flash("Authenticated operator not found.", "error")
        return redirect(url_for("issue_preview"))

    issue_location_form, issue_location_errors, _ = _validate_issue_location_form(
        holder,
        _issue_location_form_for_holder(holder),
    )
    if issue_location_errors:
        if wants_json():
            return {"ok": False, "committed": 0, "error": "; ".join(issue_location_errors)}, 400
        flash("; ".join(issue_location_errors), "error")
        return redirect(url_for("issue"))
    _remember_last_issue_location(issue_location_form)

    asset_tags = _queue_asset_tags()
    if not asset_tags:
        if wants_json():
            return {"ok": False, "committed": 0, "error": "No assets in the queue to issue."}, 400
        flash("No assets in the queue to issue.", "error")
        return redirect(url_for("issue_preview"))

    responsibility_ack = {
        "acknowledged": True,
        "ack_holder_id": int(holder["id"]),
        "ack_operator_user_id": int(user["id"]),
        "ack_at": datetime.now(timezone.utc).isoformat(),
        "ack_scope": "batch",
    }

    try:
        committed_count, receipt_id = _issue_batch(
            asset_tags,
            holder["id"],
            issue_location_form,
            responsibility_ack,
            commit_operator_user_id=int(user["id"]),
        )
    except ValueError as e:
        if wants_json():
            return {"ok": False, "committed": 0, "error": str(e)}, 400
        flash(f"Issue failed: {e}", "error")
        return redirect(url_for("issue_preview"))

    SCAN_QUEUE.clear()
    session.pop(CASE_DETAIL_QUEUE_WORKFLOW_SESSION_KEY, None)
    touch_session()

    if wants_json():
        return {"ok": True, "committed": committed_count, "receipt_id": receipt_id, "error": None}

    flash(f"Issued {committed_count} assets.", "success")
    return redirect(url_for("receipt_detail", receipt_id=receipt_id))


@app.get("/return")
@require_login
def return_queue():
    authed = enforce_inactivity_timeout()
    if auth_enabled() and not authed:
        if wants_json():
            return {"ok": False, "committed": 0, "error": "Locked"}, 401
        flash("Locked. Re-enter access code.", "error")
        return redirect(url_for("intake"))

    asset_tags = _queue_asset_tags()
    state = _build_return_preview_state(asset_tags, _return_destination_slot_ids())
    recent_return_cases_raw = session.pop("recent_return_cases", [])
    recent_return_cases = [str(case_name) for case_name in recent_return_cases_raw if str(case_name or "").strip()]

    if wants_json():
        return {
            "ok": len(state["blocking_issues"]) == 0,
            "committed": 0,
            "error": "; ".join(state["blocking_issues"]) if state["blocking_issues"] else None,
            "queued": asset_tags,
            "ready_count": state["ready_count"],
            "items": state["assets"],
        }

    return render_template(
        "return_queue.html",
        workflow_banner_title="Return",
        workflow_banner_destination="Home slots",
        workflow_banner_queued_count=len(asset_tags),
        queued_count=len(asset_tags),
        ready_count=state["ready_count"],
        blocking_issues=state["blocking_issues"],
        return_destination_rows=state["assets"],
        recent_return_cases=recent_return_cases,
        queue=SCAN_QUEUE,
        queue_len=len(SCAN_QUEUE),
        last_seen_age_seconds=seconds_since_last_seen(),
        timeout_seconds=INTAKE_TIMEOUT_SECONDS,
    )


@app.post("/return/destination")
@require_login
def return_destination_update():
    authed = enforce_inactivity_timeout()
    if auth_enabled() and not authed:
        flash("Locked. Re-enter access code.", "error")
        return redirect(url_for("intake"))
    asset_tag = str(request.form.get("asset_tag") or "").strip()
    try:
        destination_slot_id = int(str(request.form.get("destination_slot_id") or "").strip())
    except ValueError:
        flash("Select a valid return destination.", "error")
        return redirect(url_for("return_queue"))

    conn = get_connection()
    try:
        asset = _find_asset_for_scan_tag(conn, asset_tag)
        destination = _slot_occupancy_status(conn, destination_slot_id)
    finally:
        conn.close()
    if asset is None or not _queue_contains_asset_tag(str(asset["asset_tag"])):
        flash("Asset is no longer in the Return queue.", "error")
    elif destination is None or destination["occupied"]:
        flash("Selected return destination is no longer empty.", "error")
    else:
        destinations = _return_destination_slot_ids()
        destinations[str(asset["asset_tag"]).strip().upper()] = destination_slot_id
        session[RETURN_DESTINATIONS_SESSION_KEY] = destinations
        touch_session()
        flash(f"Return destination set to {destination['case_name']} / {destination['slot_position']}.", "success")
    return redirect(url_for("return_queue") + "#queue-section")


@app.get("/return/preview")
@require_login
def return_preview():
    authed = enforce_inactivity_timeout()
    if auth_enabled() and not authed:
        if wants_json():
            return {"ok": False, "committed": 0, "error": "Locked"}, 401
        flash("Locked. Re-enter access code.", "error")
        return redirect(url_for("intake"))

    asset_tags = _queue_asset_tags()
    state = _build_return_preview_state(asset_tags, _return_destination_slot_ids())

    return render_template(
        "return_preview.html",
        workflow_banner_title="Return Preview",
        workflow_banner_destination="Home slots",
        workflow_banner_queued_count=len(asset_tags),
        queued_count=len(asset_tags),
        preview_rows=state["assets"],
        ready_count=state["ready_count"],
        blocking_issues=state["blocking_issues"],
        last_seen_age_seconds=seconds_since_last_seen(),
        timeout_seconds=INTAKE_TIMEOUT_SECONDS,
    )


@app.post("/return/commit")
@require_login
def return_commit():
    authed = enforce_inactivity_timeout()
    if auth_enabled() and not authed:
        if wants_json():
            return {"ok": False, "committed": 0, "error": "Locked"}, 401
        flash("Locked. Re-enter access code.", "error")
        return redirect(url_for("intake"))

    confirmed = (request.form.get("confirm_reviewed") or "").strip().lower() in {"on", "true", "1", "yes"}
    if not confirmed:
        if wants_json():
            return {
                "ok": False,
                "committed": 0,
                "error": "Please confirm you reviewed the batch before returning assets.",
            }, 400
        flash("Please confirm you reviewed the batch before returning assets.", "error")
        return redirect(url_for("return_preview"))

    acknowledged = (request.form.get("confirm_responsibility_ack") or "").strip().lower() in {"on", "true", "1", "yes"}
    if not acknowledged:
        if wants_json():
            return {
                "ok": False,
                "committed": 0,
                "error": "Confirm responsibility acknowledgment before returning assets.",
            }, 400
        flash("Confirm responsibility acknowledgment before returning assets.", "error")
        return redirect(url_for("return_preview"))

    asset_tags = _queue_asset_tags()
    destination_slot_ids = _return_destination_slot_ids()
    state = _build_return_preview_state(asset_tags, destination_slot_ids)
    if state["blocking_issues"]:
        message = "; ".join(state["blocking_issues"])
        if wants_json():
            return {"ok": False, "committed": 0, "error": message}, 400
        flash(f"Return failed: {message}", "error")
        return redirect(url_for("return_preview"))

    user = current_user()
    if user is None:
        if wants_json():
            return {"ok": False, "committed": 0, "error": "Authenticated operator not found."}, 400
        flash("Authenticated operator not found.", "error")
        return redirect(url_for("return_preview"))

    responsibility_ack = {
        "acknowledged": True,
        "ack_operator_user_id": int(user["id"]),
        "ack_at": datetime.now(timezone.utc).isoformat(),
        "ack_scope": "batch",
    }

    try:
        committed_count, receipt_id = _return_batch(
            asset_tags,
            responsibility_ack,
            destination_slot_ids=destination_slot_ids,
            commit_operator_user_id=int(user["id"]),
        )
    except ValueError as e:
        if wants_json():
            return {"ok": False, "committed": 0, "error": str(e)}, 400
        flash(f"Return failed: {e}", "error")
        return redirect(url_for("return_preview"))

    SCAN_QUEUE.clear()
    session.pop(RETURN_DESTINATIONS_SESSION_KEY, None)
    session.pop(CASE_DETAIL_QUEUE_WORKFLOW_SESSION_KEY, None)
    touch_session()
    returned_cases: list[str] = []
    for row in state["assets"]:
        case_name = str(row.get("destination_case_name") or "").strip()
        if case_name and case_name not in returned_cases:
            returned_cases.append(case_name)
    session["recent_return_cases"] = returned_cases

    if wants_json():
        return {"ok": True, "committed": committed_count, "receipt_id": receipt_id, "error": None}

    if committed_count == 1 and len(state["assets"]) == 1:
        returned_asset = state["assets"][0]
        flash(
            "Returned "
            f"{returned_asset['canonical_tag'] or returned_asset['scanned_tag']}. "
            f"Location: {returned_asset['after_location_type']}. "
            f"Slot: {returned_asset['destination_slot']}.",
            "success",
        )
    else:
        flash(f"Returned {committed_count} assets.", "success")
    return redirect(url_for("receipt_detail", receipt_id=receipt_id))

@app.get("/lock")
@require_login
@require_role("admin")
def lock():
    set_authed(False)
    return redirect("/")

@app.get("/holders")
@require_login
def holders_search():
    authed = enforce_inactivity_timeout()
    if auth_enabled() and not authed:
        flash("Locked. Re-enter access code.", "error")
        return redirect(url_for("intake"))

    query = (request.args.get("q") or "").strip()
    return_to = _safe_local_return_to(request.args.get("return_to") or "")
    active_only = _holder_selection_requires_active_filter(return_to)
    status_filter = "active" if active_only else _holder_directory_status_filter(request.args.get("status"))
    results = (
        search_holders(query, active_only=active_only, status=status_filter)
        if query
        else list_holders(active_only=active_only, status=status_filter)
    )

    return render_template(
        "holders_search.html",
        query=query,
        return_to=return_to,
        results=results,
        selected_holder=_selected_holder_from_session(require_active=active_only),
        selection_active_only=active_only,
        status_filter=status_filter,
    )


@app.get("/holders/list")
@require_login
def holders_list():
    return redirect(url_for("holders_search"))


@app.get("/holders/<int:holder_id>")
@require_login
def holder_detail(holder_id: int):
    authed = enforce_inactivity_timeout()
    if auth_enabled() and not authed:
        flash("Locked. Re-enter access code.", "error")
        return redirect(url_for("intake"))

    return_to = _safe_local_return_to(request.args.get("return_to") or "")
    holder = get_holder(holder_id)
    if holder is None:
        abort(404)

    conn = get_connection()
    try:
        detail = get_holder_custody_detail(conn, holder_id)
    finally:
        conn.close()

    assigned_assets = detail["assets"] if detail is not None else []
    return render_template(
        "holder_detail.html",
        holder=holder,
        assigned_assets=assigned_assets,
        asset_count=len(assigned_assets),
        return_to=return_to,
    )


@app.post("/holders/<int:holder_id>/follow-up-email")
@require_login
def holder_followup_email_send(holder_id: int):
    authed = enforce_inactivity_timeout()
    if auth_enabled() and not authed:
        flash("Locked. Re-enter access code.", "error")
        return redirect(url_for("intake"))

    user = current_user()
    if user is None:
        return {"ok": False, "error": "Forbidden"}, 403
    role = str(user.get("role") or "").strip().lower()
    if role not in {"admin", "operator"}:
        return {"ok": False, "error": "Forbidden"}, 403

    holder = get_holder(holder_id)
    if holder is None:
        abort(404)

    return_to = _safe_local_return_to(request.form.get("return_to") or "") or ""
    note = (request.form.get("followup_note") or "").strip()
    if len(note) > 2000:
        flash("Follow-up note must be 2000 characters or fewer.", "error")
        return redirect(url_for("holder_detail", holder_id=holder_id, return_to=return_to))

    try:
        sent_to = _send_holder_followup_email(
            holder=holder,
            note=note,
            actor_username=str(user.get("username") or "unknown"),
        )
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("holder_detail", holder_id=holder_id, return_to=return_to))
    except Exception as exc:
        flash(f"Holder follow-up email failed: {exc}", "error")
        return redirect(url_for("holder_detail", holder_id=holder_id, return_to=return_to))

    flash(f"Holder follow-up email sent to {sent_to}.", "success")
    return redirect(url_for("holder_detail", holder_id=holder_id, return_to=return_to))


@app.get("/receipts/<int:receipt_id>")
@require_login
def receipt_detail(receipt_id: int):
    authed = enforce_inactivity_timeout()
    if auth_enabled() and not authed:
        flash("Locked. Re-enter access code.", "error")
        return redirect(url_for("intake"))

    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT
                id,
                receipt_key,
                receipt_type,
                source_event_ids_json,
                snapshot_json,
                commit_at,
                commit_operator_user_id,
                holder_id,
                sent_at,
                last_attempt_at,
                last_error
            FROM receipt_queue
            WHERE id = ?
            LIMIT 1;
            """,
            (receipt_id,),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        abort(404)

    receipt = _receipt_from_queue_row(row)
    return_to = _safe_receipt_context_return_to(request.args.get("return_to") or "")
    return_to_label = ""
    if return_to:
        return_to_label = "Back to Report" if return_to.startswith("/report") else "Back to Asset Search"

    return render_template(
        "receipt_detail.html",
        receipt=receipt,
        return_to=return_to,
        return_to_label=return_to_label,
    )


def _receipt_from_queue_row(row: sqlite3.Row) -> dict[str, object]:
    source_event_ids = json.loads(str(row["source_event_ids_json"] or "[]"))
    if not isinstance(source_event_ids, list):
        source_event_ids = []

    snapshot = json.loads(str(row["snapshot_json"] or "{}"))
    if not isinstance(snapshot, dict):
        snapshot = {}

    snapshot_assets = snapshot.get("assets")
    assets = snapshot_assets if isinstance(snapshot_assets, list) else []
    holder_snapshot = snapshot.get("holder_snapshot")
    if not isinstance(holder_snapshot, dict):
        holder_snapshot = None
    location_context = snapshot.get("location_context")
    if not isinstance(location_context, dict):
        location_context = None
    acknowledgment = snapshot.get("acknowledgment")
    if not isinstance(acknowledgment, dict):
        acknowledgment = None

    snapshot_receipt_type = str(snapshot.get("receipt_type") or "").strip().upper()
    row_receipt_type = str(row["receipt_type"] or "").strip().upper()
    receipt_type = snapshot_receipt_type or row_receipt_type

    snapshot_commit_at = str(snapshot.get("commit_at") or "").strip()
    commit_at = snapshot_commit_at or str(row["commit_at"] or "")
    delivery = _receipt_delivery_from_row(row, snapshot)

    snapshot_operator_id = snapshot.get("commit_operator_user_id")
    commit_operator_user_id = (
        int(snapshot_operator_id)
        if snapshot_operator_id is not None
        else int(row["commit_operator_user_id"])
    )
    holder_display_name = _receipt_display_holder_name(
        holder_snapshot,
        holder_id=snapshot.get("holder_id") if snapshot.get("holder_id") is not None else row["holder_id"],
        receipt_type=receipt_type,
        assets=assets,
    )
    display_date = _receipt_display_date(commit_at)
    delivery_display = {
        "sent_at": _receipt_display_timestamp(delivery.get("sent_at")),
        "last_attempt_at": _receipt_display_timestamp(delivery.get("last_attempt_at")),
    }

    return {
        "id": int(row["id"]),
        "receipt_key": str(row["receipt_key"] or ""),
        "receipt_type": receipt_type,
        "receipt_type_label": _receipt_type_label(receipt_type),
        "holder_display_name": holder_display_name,
        "commit_at": commit_at,
        "commit_at_display": _receipt_display_timestamp(commit_at),
        "display_date": display_date,
        "display_title": _receipt_display_title(receipt_type, holder_display_name, display_date),
        "commit_operator_user_id": commit_operator_user_id,
        "commit_operator": snapshot.get("commit_operator") if isinstance(snapshot.get("commit_operator"), dict) else None,
        "holder_id": snapshot.get("holder_id") if snapshot.get("holder_id") is not None else row["holder_id"],
        "holder_snapshot": holder_snapshot,
        "recipient_email": str(snapshot.get("recipient_email") or "").strip().lower(),
        "organization_snapshot": (
            snapshot.get("organization_snapshot") if isinstance(snapshot.get("organization_snapshot"), dict) else None
        ),
        "delivery": delivery,
        "delivery_display": delivery_display,
        "acknowledgment": acknowledgment,
        "location_context": location_context,
        "assets": assets,
        "source_event_ids": source_event_ids,
    }


def _receipt_summary_from_row(
    row: sqlite3.Row,
    asset_tag_filter: str = "",
    holder_name_filter: str = "",
    building_room_filter: str = "",
) -> dict[str, object]:
    snapshot = json.loads(str(row["snapshot_json"] or "{}"))
    if not isinstance(snapshot, dict):
        snapshot = {}

    assets = snapshot.get("assets")
    asset_list = assets if isinstance(assets, list) else []
    operator = snapshot.get("commit_operator") if isinstance(snapshot.get("commit_operator"), dict) else None
    holder_snapshot = snapshot.get("holder_snapshot") if isinstance(snapshot.get("holder_snapshot"), dict) else None
    organization_snapshot = (
        snapshot.get("organization_snapshot") if isinstance(snapshot.get("organization_snapshot"), dict) else None
    )
    location_context = snapshot.get("location_context") if isinstance(snapshot.get("location_context"), dict) else None

    receipt_type = str(snapshot.get("receipt_type") or row["receipt_type"] or "").strip().upper()
    commit_at = str(snapshot.get("commit_at") or row["commit_at"] or "")
    holder_id = snapshot.get("holder_id") if snapshot.get("holder_id") is not None else row["holder_id"]
    delivery = _receipt_delivery_from_row(row, snapshot)

    if holder_snapshot and str(holder_snapshot.get("name") or "").strip():
        holder_summary = str(holder_snapshot.get("name") or "").strip()
    elif holder_id is not None:
        holder_summary = f"holder_id {holder_id}"
    elif receipt_type == "RETURN" and len(asset_list) > 1:
        holder_summary = "Multiple holders"
    else:
        holder_summary = "Unknown"

    if organization_snapshot and str(organization_snapshot.get("organization") or "").strip():
        organization_summary = str(organization_snapshot.get("organization") or "").strip()
    else:
        organization_summary = ""

    if location_context and str(location_context.get("building_room") or "").strip():
        location_summary = str(location_context.get("building_room") or "").strip()
    elif receipt_type == "RETURN":
        location_summary = "Return location varies by asset"
    else:
        location_summary = "Unknown"

    if operator and str(operator.get("username") or "").strip():
        committed_by = str(operator.get("username") or "").strip()
    else:
        committed_by = f"user_id {int(row['commit_operator_user_id'])}"

    display_date = _receipt_display_date(commit_at)
    display_holder_name = _receipt_display_holder_name(
        holder_snapshot,
        holder_id=holder_id,
        receipt_type=receipt_type,
        assets=asset_list,
    )

    try:
        commit_at_display = datetime.fromisoformat(commit_at).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        commit_at_display = commit_at or "Unknown"

    def _append_unique(values: list[str], value: object) -> None:
        text = str(value or "").strip()
        if text and text not in values:
            values.append(text)

    normalized_asset_tag_filter = asset_tag_filter.strip().upper()
    normalized_holder_name_filter = holder_name_filter.strip().upper()
    normalized_building_room_filter = building_room_filter.strip().upper()
    matched_asset_tags: list[str] = []
    asset_tags: list[str] = []
    matched_holder_names: list[str] = []
    matched_locations: list[str] = []
    if normalized_asset_tag_filter:
        for asset in asset_list:
            if not isinstance(asset, dict):
                continue
            asset_tag = str(asset.get("asset_tag") or "").strip()
            if asset_tag and normalized_asset_tag_filter in asset_tag.upper():
                _append_unique(matched_asset_tags, asset_tag)

    if normalized_holder_name_filter:
        if holder_summary != "Unknown" and normalized_holder_name_filter in holder_summary.upper():
            _append_unique(matched_holder_names, holder_summary)
        for asset in asset_list:
            if not isinstance(asset, dict):
                continue
            _append_unique(
                matched_holder_names,
                asset.get("holder_snapshot", {}).get("name") if isinstance(asset.get("holder_snapshot"), dict) else "",
            )
            _append_unique(
                matched_holder_names,
                asset.get("from_holder_snapshot", {}).get("name")
                if isinstance(asset.get("from_holder_snapshot"), dict)
                else "",
            )
        matched_holder_names = [
            value for value in matched_holder_names if normalized_holder_name_filter in value.upper()
        ]

    if normalized_building_room_filter:
        _append_unique(matched_locations, location_summary if location_summary != "Unknown" else "")
        for asset in asset_list:
            if not isinstance(asset, dict):
                continue
            _append_unique(matched_locations, asset.get("from_building_room"))
            _append_unique(matched_locations, asset.get("to_building_room"))
        matched_locations = [value for value in matched_locations if normalized_building_room_filter in value.upper()]

    for asset in asset_list:
        if not isinstance(asset, dict):
            continue
        asset_tag = str(asset.get("asset_tag") or "").strip()
        if asset_tag:
            _append_unique(asset_tags, asset_tag)

    visible_asset_tags = matched_asset_tags or asset_tags[:1]
    additional_asset_tag_count = max(len(asset_tags) - len(visible_asset_tags), 0)

    return {
        "id": int(row["id"]),
        "receipt_key": str(row["receipt_key"] or ""),
        "receipt_type": receipt_type,
        "receipt_type_label": _receipt_type_label(receipt_type),
        "display_title": _receipt_display_title(receipt_type, display_holder_name, display_date),
        "display_date": display_date,
        "delivery_state": delivery.get("state"),
        "commit_at": commit_at,
        "commit_at_display": commit_at_display,
        "committed_by": committed_by,
        "holder_summary": holder_summary,
        "holder_display_name": display_holder_name,
        "organization_summary": organization_summary,
        "location_summary": location_summary,
        "asset_count": len(asset_list),
        "visible_asset_tags": visible_asset_tags,
        "additional_asset_tag_count": additional_asset_tag_count,
        "matched_asset_tags": matched_asset_tags,
        "matched_holder_names": matched_holder_names,
        "matched_locations": matched_locations,
    }


def _receipt_type_label(receipt_type: str) -> str:
    normalized = str(receipt_type or "").strip().upper()
    if normalized == "ISSUE":
        return "Issue Receipt"
    if normalized == "RETURN":
        return "Return Receipt"
    return "Receipt"


def _receipt_display_date(commit_at: str) -> str:
    value = str(commit_at or "").strip()
    if not value:
        return "Unknown Date"

    parsed = _parse_display_timestamp(value)
    if parsed is None:
        return value

    month = parsed.strftime("%b")
    day = parsed.day
    year = parsed.year
    return f"{month} {day}, {year}"


def _parse_display_timestamp(value: object) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None

    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _receipt_display_timestamp(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""

    parsed = _parse_display_timestamp(text)
    if parsed is None:
        return text

    month = parsed.strftime("%b")
    day = parsed.day
    year = parsed.year
    return f"{month} {day}, {year} at {parsed.strftime('%H:%M')} UTC"


def _report_event_display_timestamp(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""

    parsed = _parse_display_timestamp(text)
    if parsed is None:
        return text

    month = parsed.strftime("%b")
    day = parsed.day
    year = parsed.year
    hour = parsed.strftime("%I").lstrip("0") or "0"
    minute = parsed.strftime("%M")
    meridiem = parsed.strftime("%p")
    return f"{month} {day}, {year} {hour}:{minute} {meridiem}"


def _receipt_display_holder_name(
    holder_snapshot: Optional[dict[str, object]],
    *,
    holder_id: object,
    receipt_type: str,
    assets: list[object],
) -> str:
    if isinstance(holder_snapshot, dict):
        holder_name = str(holder_snapshot.get("name") or "").strip()
        if holder_name:
            return holder_name

    unique_names: list[str] = []
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        for field_name in ("holder_snapshot", "from_holder_snapshot"):
            snapshot = asset.get(field_name)
            if not isinstance(snapshot, dict):
                continue
            holder_name = str(snapshot.get("name") or "").strip()
            if holder_name and holder_name not in unique_names:
                unique_names.append(holder_name)

    if len(unique_names) == 1:
        return unique_names[0]
    if len(unique_names) > 1:
        return "Multiple Holders"
    if holder_id is not None:
        return f"Holder {holder_id}"
    if str(receipt_type or "").strip().upper() == "RETURN":
        return "Returning Holder"
    return "Unknown Holder"


def _receipt_display_title(receipt_type: str, holder_name: str, display_date: str) -> str:
    return f"{_receipt_type_label(receipt_type)} — {holder_name or 'Unknown Holder'} — {display_date or 'Unknown Date'}"


def _receipt_email_recipients(receipt: dict[str, object]) -> list[str]:
    recipient_email = str(receipt.get("recipient_email") or "").strip().lower()
    return _normalized_email_addresses(recipient_email)


def _receipt_cc_recipients() -> list[str]:
    configured_cc = active_receipt_cc_setting().lower()
    return _normalized_email_addresses(configured_cc)


def _normalized_email_addresses(raw_addresses: str) -> list[str]:
    if not raw_addresses:
        return []

    recipients: list[str] = []
    for _, email_address in getaddresses([raw_addresses.replace("\n", ",")]):
        normalized = str(email_address or "").strip().lower()
        if normalized and normalized not in recipients:
            recipients.append(normalized)

    return recipients


def _receipt_pdf_download_name(receipt: dict[str, object]) -> str:
    title = str(receipt.get("display_title") or "").strip() or "Receipt"
    sanitized = title.replace(" — ", " - ")
    sanitized = re.sub(r'[<>:"/\\|?*]', " ", sanitized)
    sanitized = re.sub(r"\s+", " ", sanitized).strip().rstrip(". ")
    return f"{sanitized or 'Receipt'}.pdf"


def _build_receipt_email_body(receipt: dict[str, object]) -> str:
    asset_tags: list[str] = []
    for asset in receipt.get("assets", []):
        if not isinstance(asset, dict):
            continue
        asset_tag = str(asset.get("asset_tag") or "").strip()
        if asset_tag:
            asset_tags.append(asset_tag)

    asset_lines = "\n".join(f"- {asset_tag}" for asset_tag in asset_tags) or "- None recorded"
    return (
        f"{receipt.get('display_title')}\n\n"
        f"Receipt key: {receipt.get('receipt_key')}\n"
        f"Committed at: {receipt.get('commit_at')}\n"
        f"Holder: {receipt.get('holder_display_name')}\n"
        f"Assets:\n{asset_lines}\n"
    )


def _send_receipt_email(receipt: dict[str, object]) -> list[str]:
    recipients = _receipt_email_recipients(receipt)
    if not recipients:
        raise ValueError("Receipt has no stored email recipient.")
    cc_recipients = _receipt_cc_recipients()
    from_address = str(os.getenv("ASSETTRACK_RECEIPT_FROM_EMAIL") or "assettrack@local").strip() or "assettrack@local"

    message = EmailMessage()
    message["Subject"] = str(receipt.get("display_title") or "AssetTrack Receipt")
    message["From"] = from_address
    message["To"] = ", ".join(recipients)
    if cc_recipients:
        message["Cc"] = ", ".join(cc_recipients)
    message.set_content(_build_receipt_email_body(receipt))
    message.add_attachment(
        _build_receipt_pdf(receipt, for_email=True),
        maintype="application",
        subtype="pdf",
        filename=_receipt_pdf_download_name(receipt),
    )
    _send_email_message(message)

    return recipients


def _receipt_pdf_ack_name(receipt: dict[str, object]) -> str:
    holder_snapshot = receipt.get("holder_snapshot")
    if isinstance(holder_snapshot, dict):
        holder_name = str(holder_snapshot.get("name") or "").strip()
        if holder_name:
            return holder_name

    names: list[str] = []
    for asset in receipt.get("assets", []):
        if not isinstance(asset, dict):
            continue
        for field_name in ("holder_snapshot", "from_holder_snapshot"):
            snapshot = asset.get(field_name)
            if not isinstance(snapshot, dict):
                continue
            holder_name = str(snapshot.get("name") or "").strip()
            if holder_name and holder_name not in names:
                names.append(holder_name)

    if names:
        return ", ".join(names)

    return "Unknown"


def _receipt_pdf_initials(name: str) -> str:
    parts = [part for part in name.replace(",", " ").split() if part]
    initials = "".join(part[0].upper() for part in parts[:4] if part and part[0].isalnum())
    return initials or "N/A"


def _receipt_pdf_location_summary(receipt: dict[str, object]) -> str:
    location_context = receipt.get("location_context")
    if isinstance(location_context, dict):
        building_room = str(location_context.get("building_room") or "").strip()
        if building_room:
            return building_room
        building = str(location_context.get("building") or "").strip()
        room = str(location_context.get("room") or "").strip()
        if building and room:
            return f"{building}/{room}"
        if building or room:
            return building or room

    locations: list[str] = []
    for asset in receipt.get("assets", []):
        if not isinstance(asset, dict):
            continue
        for field_name in ("to_building_room", "from_building_room"):
            location = str(asset.get(field_name) or "").strip()
            if location and location not in locations:
                locations.append(location)

    if locations:
        return ", ".join(locations)

    return "Unknown"


def _receipt_acknowledgment_statement(receipt_type: str) -> str:
    if receipt_type == "RETURN":
        return "Custody return was reviewed and confirmed from the stored receipt record."
    return "Custody issue was reviewed and confirmed from the stored receipt record."


def _receipt_pdf_location_type_label(value: object) -> str:
    return str(value or "").strip().replace("_", " ")


def _build_receipt_pdf(receipt: dict[str, object], *, for_email: bool = False) -> bytes:
    styles = getSampleStyleSheet()
    body = styles["BodyText"]
    heading = styles["Heading2"]
    title = styles["Title"]
    hero = ParagraphStyle(
        "ReceiptPdfHero",
        parent=styles["Heading1"],
        fontSize=16,
        leading=19,
        spaceAfter=4,
    )
    status = ParagraphStyle(
        "ReceiptPdfStatus",
        parent=body,
        fontName="Helvetica-Bold",
        textColor=colors.HexColor("#335c81"),
        spaceAfter=4,
    )
    supporting = ParagraphStyle(
        "ReceiptPdfSupporting",
        parent=body,
        textColor=colors.HexColor("#4f5d6b"),
        spaceAfter=4,
    )
    table_body = ParagraphStyle(
        "ReceiptPdfTableBody",
        parent=body,
        fontSize=8.5,
        leading=10,
        splitLongWords=False,
        wordWrap="LTR",
    )
    table_header = ParagraphStyle(
        "ReceiptPdfTableHeader",
        parent=table_body,
        fontName="Helvetica-Bold",
    )

    def _text(value: object) -> str:
        return str(value or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def _p(value: object, style: ParagraphStyle = table_body) -> Paragraph:
        return Paragraph(_text(value), style)

    def _render_table(headers: list[str], rows: list[list[object]], column_widths: list[float]) -> Table:
        data = [[Paragraph(_text(header), table_header) for header in headers]]
        for row in rows:
            data.append([_p(value) for value in row])

        table = Table(data, colWidths=column_widths, repeatRows=1)
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dbe7f3")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.black),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#c8d2dc")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 6),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ]
            )
        )
        return table

    receipt_type = str(receipt.get("receipt_type") or "").strip().upper() or "UNKNOWN"
    organization_name = "Unknown"
    organization_snapshot = receipt.get("organization_snapshot")
    if isinstance(organization_snapshot, dict):
        organization_name = str(organization_snapshot.get("organization") or "").strip() or "Unknown"
    acknowledgment = receipt.get("acknowledgment")
    ack_timestamp = ""
    if isinstance(acknowledgment, dict):
        ack_timestamp = str(acknowledgment.get("ack_at") or "").strip()
    if not ack_timestamp:
        ack_timestamp = str(receipt.get("commit_at") or "").strip()

    typed_name = _receipt_pdf_ack_name(receipt)
    location_summary = _receipt_pdf_location_summary(receipt)
    receipt_type_label = _receipt_type_label(receipt_type)
    asset_count = sum(1 for asset in receipt.get("assets", []) if isinstance(asset, dict))
    asset_count_label = f"{asset_count} asset" if asset_count == 1 else f"{asset_count} assets"
    action_phrase = "issued to"
    if receipt_type == "RETURN":
        action_phrase = "returned from"
    elif receipt_type not in {"ISSUE", "RETURN"}:
        action_phrase = "recorded for"

    delivery = receipt.get("delivery")
    delivery_state = ""
    delivery_error = ""
    if isinstance(delivery, dict):
        delivery_state = str(delivery.get("state") or "").strip().lower()
        delivery_error = str(delivery.get("last_error") or "").strip()

    if for_email:
        status_text = "Receipt attached for your records."
    elif delivery_state == "failed":
        status_text = "Receipt delivery failed. Custody is already recorded."
    elif delivery_state == "pending":
        status_text = "Receipt delivery queued. Custody is already recorded."
    else:
        status_text = "Custody is already recorded."

    recorded_at = _receipt_display_timestamp(ack_timestamp or receipt.get("commit_at"))
    summary_rows = [
        ["Action", receipt_type_label],
        ["Assets in this receipt", str(asset_count)],
        ["Recorded at", recorded_at or "Unknown"],
        ["Holder", typed_name],
        ["Organization", organization_name],
        ["Location", location_summary],
    ]
    audit_rows = [
        ["Receipt ID", str(receipt.get("id") or "Unknown")],
        ["Receipt key", str(receipt.get("receipt_key") or "Unknown")],
        ["Acknowledgment", _receipt_acknowledgment_statement(receipt_type)],
        ["Typed name", typed_name],
        ["Initials", _receipt_pdf_initials(typed_name)],
    ]
    recipient_email = str(receipt.get("recipient_email") or "").strip().lower()
    if recipient_email:
        audit_rows.append(["Recipient email", recipient_email])
    if delivery_error:
        audit_rows.append(["Delivery issue", delivery_error])

    asset_rows: list[list[object]] = []

    for asset in receipt.get("assets", []):
        if not isinstance(asset, dict):
            continue
        make_model = str(asset.get("manufacturer") or "").strip()
        model = str(asset.get("model") or "").strip()
        model_code = str(asset.get("model_code") or "").strip()
        if model:
            make_model = f"{make_model} / {model}" if make_model else model
        if model_code:
            make_model = f"{make_model} ({model_code})" if make_model else model_code
        asset_rows.append(
            [
                str(asset.get("asset_tag") or "").strip(),
                str(asset.get("equipment_type") or "").strip(),
                str(asset.get("serial_number") or "").strip(),
                make_model,
                _receipt_pdf_location_type_label(asset.get("from_location_type")),
                _receipt_pdf_location_type_label(asset.get("to_location_type")),
            ]
        )

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        leftMargin=0.5 * inch,
        rightMargin=0.5 * inch,
        topMargin=0.5 * inch,
        bottomMargin=0.5 * inch,
        title=f"AssetTrack Receipt {receipt.get('receipt_key') or receipt.get('id')}",
        author="AssetTrack",
    )

    story: list[object] = [
        Paragraph(receipt_type_label, title),
        Spacer(1, 0.1 * inch),
        Paragraph(f"{_text(asset_count_label)} {action_phrase} {_text(typed_name)}", hero),
        Paragraph(_text(status_text), status),
        Paragraph(
            _text(" · ".join(part for part in [recorded_at or "Unknown", organization_name, location_summary] if part)),
            supporting,
        ),
        Spacer(1, 0.08 * inch),
        Paragraph("What Happened", heading),
        Spacer(1, 0.05 * inch),
        _render_table(
            ["Question", "Answer"],
            summary_rows,
            [1.8 * inch, 5.2 * inch],
        ),
        Spacer(1, 0.16 * inch),
        Spacer(1, 0.18 * inch),
        Paragraph("Assets", heading),
        Spacer(1, 0.08 * inch),
        _render_table(
            ["Tag", "Type", "Serial", "Item", "From", "To"],
            asset_rows or [["No assets captured.", "", "", "", "", ""]],
            [1.15 * inch, 1.0 * inch, 1.25 * inch, 1.95 * inch, 0.95 * inch, 1.0 * inch],
        ),
    ]

    if not for_email:
        story.extend(
            [
                Spacer(1, 0.16 * inch),
                Paragraph("Audit Details", heading),
                Spacer(1, 0.05 * inch),
                _render_table(
                    ["Detail", "Recorded value"],
                    audit_rows,
                    [1.8 * inch, 5.2 * inch],
                ),
            ]
        )

    def _invariant_canvas(*args, **kwargs):
        kwargs.setdefault("invariant", 1)
        return canvas.Canvas(*args, **kwargs)

    doc.build(story, canvasmaker=_invariant_canvas)
    pdf_bytes = buffer.getvalue()
    stable_digest = hashlib.md5(json.dumps(receipt, sort_keys=True).encode("utf-8")).hexdigest().encode("ascii")
    return re.sub(
        rb"/ID\s*\[\s*<[^>]+>\s*<[^>]+>\s*\]",
        b"/ID [<" + stable_digest + b"><" + stable_digest + b">]",
        pdf_bytes,
        count=1,
    )


def _update_receipt_delivery_state(
    conn,
    *,
    receipt_id: int,
    snapshot: dict[str, object],
    state: str,
    last_attempt_at: Optional[str],
    sent_at: Optional[str],
    last_error: Optional[str],
    cc_recipients: Optional[list[str]] = None,
) -> None:
    updated_snapshot = dict(snapshot)
    updated_snapshot["delivery"] = _receipt_delivery_snapshot(
        state=state,
        sent_at=sent_at,
        last_attempt_at=last_attempt_at,
        last_error=last_error,
        cc_recipients=cc_recipients,
    )
    conn.execute(
        """
        UPDATE receipt_queue
        SET snapshot_json = ?, sent_at = ?, last_attempt_at = ?, last_error = ?
        WHERE id = ?;
        """,
        (
            json.dumps(updated_snapshot, sort_keys=True),
            sent_at,
            last_attempt_at,
            last_error,
            int(receipt_id),
        ),
    )


def _send_queued_receipt(receipt_id: int) -> dict[str, object]:
    conn = get_connection()
    try:
        row = _receipt_queue_row_by_id(conn, receipt_id)
        if row is None:
            raise ValueError("Receipt not found.")

        snapshot = _receipt_row_snapshot(row)
        delivery = _receipt_delivery_from_row(row, snapshot)
        delivery_state = str(delivery.get("state") or "").strip().lower()
        if delivery_state not in {"pending", "failed"}:
            raise ValueError("Receipt is not queued for email.")

        receipt = _receipt_from_queue_row(row)
        attempt_at = datetime.now(timezone.utc).isoformat()
        cc_recipients = _receipt_cc_recipients()

        try:
            recipients = _send_receipt_email(receipt)
        except Exception as exc:
            with conn:
                _update_receipt_delivery_state(
                    conn,
                    receipt_id=int(row["id"]),
                    snapshot=snapshot,
                    state="failed",
                    last_attempt_at=attempt_at,
                    sent_at=None,
                    last_error=str(exc),
                )
            raise

        with conn:
            _update_receipt_delivery_state(
                conn,
                receipt_id=int(row["id"]),
                snapshot=snapshot,
                state="sent",
                last_attempt_at=attempt_at,
                sent_at=attempt_at,
                last_error=None,
                cc_recipients=cc_recipients,
            )

        return {
            "receipt_id": int(row["id"]),
            "recipients": recipients,
            "sent_at": attempt_at,
        }
    finally:
        conn.close()


def _resend_existing_receipt(receipt_id: int) -> dict[str, object]:
    conn = get_connection()
    try:
        row = _receipt_queue_row_by_id(conn, receipt_id)
        if row is None:
            raise ValueError("Receipt not found.")

        snapshot = _receipt_row_snapshot(row)
        delivery = _receipt_delivery_from_row(row, snapshot)
        if str(delivery.get("state") or "").strip().lower() != "sent":
            raise ValueError("Receipt is not eligible for resend.")

        receipt = _receipt_from_queue_row(row)
        cc_recipients = _receipt_cc_recipients()
        recipients = _send_receipt_email(receipt)
        delivery = _receipt_delivery_from_row(row, snapshot)
        delivery_state = str(delivery.get("state") or "sent")
        last_attempt_at = delivery.get("last_attempt_at")
        sent_at = delivery.get("sent_at")
        last_error = delivery.get("last_error")
        with conn:
            _update_receipt_delivery_state(
                conn,
                receipt_id=int(row["id"]),
                snapshot=snapshot,
                state=delivery_state,
                last_attempt_at=str(last_attempt_at) if last_attempt_at is not None else None,
                sent_at=str(sent_at) if sent_at is not None else None,
                last_error=str(last_error) if last_error is not None else None,
                cc_recipients=cc_recipients,
            )
        return {
            "receipt_id": int(row["id"]),
            "recipients": recipients,
        }
    finally:
        conn.close()


@app.get("/receipts")
@require_login
def receipts_list():
    authed = enforce_inactivity_timeout()
    if auth_enabled() and not authed:
        flash("Locked. Re-enter access code.", "error")
        return redirect(url_for("intake"))

    return_to = _safe_local_return_to(request.args.get("return_to") or "")
    asset_tag = (request.args.get("asset_tag") or "").strip()
    holder_name = (request.args.get("holder_name") or "").strip()
    building_room = (request.args.get("building_room") or "").strip()

    clauses: list[str] = []
    params: list[object] = []

    if asset_tag:
        clauses.append(
            """
            EXISTS (
                SELECT 1
                FROM json_each(receipt_queue.snapshot_json, '$.assets') AS asset
                WHERE UPPER(COALESCE(json_extract(asset.value, '$.asset_tag'), '')) LIKE UPPER(?)
            )
            """
        )
        params.append(f"%{asset_tag}%")

    if holder_name:
        clauses.append(
            """
            (
                UPPER(COALESCE(json_extract(receipt_queue.snapshot_json, '$.holder_snapshot.name'), '')) LIKE UPPER(?)
                OR EXISTS (
                    SELECT 1
                    FROM json_each(receipt_queue.snapshot_json, '$.assets') AS asset
                    WHERE UPPER(COALESCE(json_extract(asset.value, '$.holder_snapshot.name'), '')) LIKE UPPER(?)
                       OR UPPER(COALESCE(json_extract(asset.value, '$.from_holder_snapshot.name'), '')) LIKE UPPER(?)
                )
            )
            """
        )
        like_value = f"%{holder_name}%"
        params.extend([like_value, like_value, like_value])

    if building_room:
        clauses.append(
            """
            (
                UPPER(COALESCE(json_extract(receipt_queue.snapshot_json, '$.location_context.building_room'), '')) LIKE UPPER(?)
                OR EXISTS (
                    SELECT 1
                    FROM json_each(receipt_queue.snapshot_json, '$.assets') AS asset
                    WHERE UPPER(COALESCE(json_extract(asset.value, '$.from_building_room'), '')) LIKE UPPER(?)
                       OR UPPER(COALESCE(json_extract(asset.value, '$.to_building_room'), '')) LIKE UPPER(?)
                )
            )
            """
        )
        like_value = f"%{building_room}%"
        params.extend([like_value, like_value, like_value])

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    conn = get_connection()
    try:
        rows = conn.execute(
            f"""
            SELECT
                id,
                receipt_key,
                receipt_type,
                snapshot_json,
                commit_at,
                commit_operator_user_id,
                holder_id,
                sent_at,
                last_attempt_at,
                last_error
            FROM receipt_queue
            {where_sql}
            ORDER BY commit_at DESC, id DESC;
            """,
            tuple(params),
        ).fetchall()
    finally:
        conn.close()

    receipts = [
        _receipt_summary_from_row(
            row,
            asset_tag_filter=asset_tag,
            holder_name_filter=holder_name,
            building_room_filter=building_room,
        )
        for row in rows
    ]

    return render_template(
        "receipts_list.html",
        receipts=receipts,
        filters={
            "asset_tag": asset_tag,
            "holder_name": holder_name,
            "building_room": building_room,
        },
        return_to=return_to,
    )


@app.post("/receipts/<int:receipt_id>/send")
@require_login
def receipt_send(receipt_id: int):
    authed = enforce_inactivity_timeout()
    if auth_enabled() and not authed:
        if wants_json():
            return {"ok": False, "error": "Locked"}, 401
        flash("Locked. Re-enter access code.", "error")
        return redirect(url_for("intake"))

    if recovery_mode_is_active(_resolved_runtime_db_path()):
        message = _recovery_mode_send_block_message()
        if wants_json():
            return {"ok": False, "error": message}, 409
        flash(message, "error")
        return redirect(url_for("receipt_detail", receipt_id=receipt_id))

    try:
        result = _send_queued_receipt(receipt_id)
    except ValueError as exc:
        if wants_json():
            return {"ok": False, "error": str(exc)}, 400
        flash(str(exc), "error")
        return redirect(url_for("receipt_detail", receipt_id=receipt_id))
    except Exception as exc:
        message = f"Receipt email failed after custody was recorded: {exc}"
        if wants_json():
            return {"ok": False, "error": message}, 500
        flash(message, "error")
        return redirect(url_for("receipt_detail", receipt_id=receipt_id))

    if wants_json():
        return {
            "ok": True,
            "receipt_id": result["receipt_id"],
            "recipients": result["recipients"],
            "sent_at": result["sent_at"],
        }

    flash(f"Receipt email sent to {', '.join(result['recipients'])}.", "success")
    return redirect(url_for("receipt_detail", receipt_id=receipt_id))


@app.post("/receipts/<int:receipt_id>/resend")
@require_role("admin")
def receipt_resend(receipt_id: int):
    authed = enforce_inactivity_timeout()
    if auth_enabled() and not authed:
        if wants_json():
            return {"ok": False, "error": "Locked"}, 401
        flash("Locked. Re-enter access code.", "error")
        return redirect(url_for("intake"))

    if recovery_mode_is_active(_resolved_runtime_db_path()):
        message = _recovery_mode_send_block_message()
        if wants_json():
            return {"ok": False, "error": message}, 409
        flash(message, "error")
        return redirect(url_for("receipt_detail", receipt_id=receipt_id))

    try:
        result = _resend_existing_receipt(receipt_id)
    except Exception:
        message = "Receipt email could not be resent. Custody and receipt records were not changed."
        if wants_json():
            return {"ok": False, "error": message}, 500
        flash(message, "error")
        return redirect(url_for("receipt_detail", receipt_id=receipt_id))

    if wants_json():
        return {
            "ok": True,
            "receipt_id": result["receipt_id"],
            "recipients": result["recipients"],
        }

    flash(f"Receipt email resent to {', '.join(result['recipients'])}.", "success")
    return redirect(url_for("receipt_detail", receipt_id=receipt_id))


@app.get("/receipts/<int:receipt_id>/resend")
@require_role("admin")
def receipt_resend_get(receipt_id: int):
    flash(
        "No receipt email was resent. Use the receipt detail page button to resend receipt email; custody and receipt records were not changed.",
        "error",
    )
    return redirect(url_for("receipt_detail", receipt_id=receipt_id))


@app.get("/receipts/<int:receipt_id>/pdf")
@require_login
def receipt_pdf(receipt_id: int):
    authed = enforce_inactivity_timeout()
    if auth_enabled() and not authed:
        flash("Locked. Re-enter access code.", "error")
        return redirect(url_for("intake"))

    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT
                id,
                receipt_key,
                receipt_type,
                source_event_ids_json,
                snapshot_json,
                commit_at,
                commit_operator_user_id,
                holder_id,
                sent_at,
                last_attempt_at,
                last_error
            FROM receipt_queue
            WHERE id = ?
            LIMIT 1;
            """,
            (receipt_id,),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        abort(404)

    receipt = _receipt_from_queue_row(row)
    pdf_bytes = _build_receipt_pdf(receipt)
    download_name = _receipt_pdf_download_name(receipt)
    return send_file(
        BytesIO(pdf_bytes),
        as_attachment=True,
        download_name=download_name,
        mimetype="application/pdf",
        conditional=False,
    )


@app.get("/holders/new")
@require_login
def holders_new():
    authed = enforce_inactivity_timeout()
    if auth_enabled() and not authed:
        flash("Locked. Re-enter access code.", "error")
        return redirect(url_for("intake"))

    return_to = _safe_local_return_to(request.args.get("return_to") or "")
    form = session.pop("holder_new_form", None)
    if not isinstance(form, dict):
        form = {"name": "", "organization_id": "", "email": ""}

    return render_template(
        "holder_new.html",
        form=form,
        return_to=return_to,
        organization_options=list_organizations(),
    )


@app.post("/holders/new")
@require_login
def holders_create():
    authed = enforce_inactivity_timeout()
    if auth_enabled() and not authed:
        flash("Locked. Re-enter access code.", "error")
        return redirect(url_for("intake"))

    return_to = _safe_local_return_to(request.form.get("return_to") or "")
    name = (request.form.get("name") or "").strip()
    organization_id_raw = (request.form.get("organization_id") or "").strip()
    email = (request.form.get("email") or "").strip()
    form = {"name": name, "organization_id": organization_id_raw, "email": email}

    if not email:
        session["holder_new_form"] = form
        flash(_holder_form_error_message(ValueError("email is required")), "error")
        if return_to is not None:
            return redirect(url_for("holders_new", return_to=return_to))
        return redirect(url_for("holders_new"))

    try:
        created = create_holder(
            name,
            organization_id=None if not organization_id_raw else int(organization_id_raw),
            email=email,
        )
    except ValueError as e:
        session["holder_new_form"] = form
        flash(_holder_form_error_message(e), "error")
        if return_to is not None:
            return redirect(url_for("holders_new", return_to=return_to))
        return redirect(url_for("holders_new"))

    flash(f"Created holder: {_holder_display_name(created)}", "success")
    if return_to is not None:
        return redirect(return_to)
    return redirect(url_for("holders_search"))


@app.get("/holders/edit/<int:holder_id>")
@require_login
def holders_edit(holder_id: int):
    authed = enforce_inactivity_timeout()
    if auth_enabled() and not authed:
        flash("Locked. Re-enter access code.", "error")
        return redirect(url_for("intake"))

    return_to = _safe_local_return_to(request.args.get("return_to") or "")
    holder = get_holder(holder_id)
    if holder is None:
        abort(404)

    form = session.pop(f"holder_edit_form:{holder_id}", None)
    if not isinstance(form, dict):
        form = {
            "name": str(holder.get("name") or ""),
            "organization_id": "" if holder.get("organization_id") is None else str(holder.get("organization_id")),
            "email": str(holder.get("email") or ""),
        }

    return render_template(
        "holder_edit.html",
        holder=holder,
        form=form,
        return_to=return_to,
        organization_options=list_organizations(),
    )


@app.post("/holders/edit/<int:holder_id>")
@require_login
def holders_edit_submit(holder_id: int):
    authed = enforce_inactivity_timeout()
    if auth_enabled() and not authed:
        flash("Locked. Re-enter access code.", "error")
        return redirect(url_for("intake"))

    return_to = _safe_local_return_to(request.form.get("return_to") or "")
    form = {
        "name": (request.form.get("name") or "").strip(),
        "organization_id": (request.form.get("organization_id") or "").strip(),
        "email": (request.form.get("email") or "").strip(),
    }

    holder = get_holder(holder_id)
    if holder is None:
        abort(404)

    if not form["email"]:
        session[f"holder_edit_form:{holder_id}"] = form
        flash(_holder_form_error_message(ValueError("email is required")), "error")
        if return_to is not None:
            return redirect(url_for("holders_edit", holder_id=holder_id, return_to=return_to))
        return redirect(url_for("holders_edit", holder_id=holder_id))

    try:
        updated = update_holder(
            holder_id,
            name=form["name"],
            organization_id=None if not form["organization_id"] else int(form["organization_id"]),
            email=form["email"],
        )
    except ValueError as e:
        session[f"holder_edit_form:{holder_id}"] = form
        flash(_holder_form_error_message(e), "error")
        if return_to is not None:
            return redirect(url_for("holders_edit", holder_id=holder_id, return_to=return_to))
        return redirect(url_for("holders_edit", holder_id=holder_id))

    flash(f"Updated holder: {_holder_display_name(updated)}", "success")
    if return_to is not None:
        return redirect(return_to)
    return redirect(url_for("holders_search"))


@app.post("/holders/select")
@require_login
def holders_select():
    authed = enforce_inactivity_timeout()
    if auth_enabled() and not authed:
        flash("Locked. Re-enter access code.", "error")
        return redirect(url_for("intake"))

    return_to = _safe_local_return_to(request.form.get("return_to") or request.args.get("return_to") or "")
    holder_id_raw = (request.form.get("holder_id") or "").strip()
    if not holder_id_raw:
        flash("Select a holder first.", "error")
        if return_to is not None:
            return redirect(url_for("holders_search", return_to=return_to))
        return redirect(url_for("holders_search"))

    holder = get_holder(holder_id_raw)
    if holder is None:
        flash("Selected holder not found.", "error")
        if return_to is not None:
            return redirect(url_for("holders_search", return_to=return_to))
        return redirect(url_for("holders_search"))
    if not _holder_is_active(holder):
        session.pop("holder_id", None)
        flash("Inactive holders cannot be selected for assignment.", "error")
        if return_to is not None:
            return redirect(url_for("holders_search", return_to=return_to))
        return redirect(url_for("holders_search"))

    session["holder_id"] = holder["id"]
    touch_session()
    flash(f"Selected holder: {_holder_display_name(holder)}", "success")
    if return_to is not None:
        return redirect(return_to)
    return redirect(url_for("holders_search"))


@app.post("/holders/clear")
@require_login
def holders_clear():
    authed = enforce_inactivity_timeout()
    if auth_enabled() and not authed:
        flash("Locked. Re-enter access code.", "error")
        return redirect(url_for("intake"))

    session.pop("holder_id", None)
    touch_session()
    flash("Cleared holder selection.", "success")
    return redirect(url_for("holders_search"))


@app.post("/holders/<int:holder_id>/toggle-active")
@require_login
@require_role("admin")
def holders_toggle_active(holder_id: int):
    return_to = _safe_local_return_to(request.form.get("return_to") or "")
    requested = request.form.get("is_active")
    next_active = _is_truthy(requested)

    try:
        updated = set_holder_active(holder_id, next_active)
    except ValueError as exc:
        flash(str(exc), "error")
    else:
        state = "active" if _holder_is_active(updated) else "inactive"
        flash(f"Holder { _holder_display_name(updated) } is now {state}.", "success")
        if not _holder_is_active(updated) and session.get("holder_id") == int(updated["id"]):
            session.pop("holder_id", None)

    if return_to is not None:
        return redirect(return_to)
    return redirect(url_for("holder_detail", holder_id=holder_id))


@app.get("/admin/users")
@require_login
@require_role("admin")
def admin_users():
    return render_template("admin_users.html", **_admin_users_context())


def _admin_users_context(**extra_context):
    users = list_users()
    for user in users:
        user["created_at_display"] = _receipt_display_timestamp(user.get("created_at"))
        user["updated_at_display"] = _receipt_display_timestamp(user.get("updated_at"))
    return {"users": users, **extra_context}


@app.get("/admin/system")
@require_login
@require_role("admin")
def admin_system():
    resolved_db_path = _resolved_runtime_db_path()
    holder_count: int | None = None
    asset_count: int | None = None
    schema_warning: str | None = None
    restore_history = _restore_history_context()

    try:
        conn = sqlite3.connect(f"file:{resolved_db_path}?mode=ro", uri=True)
        try:
            holder_count = int(conn.execute("SELECT COUNT(*) FROM holders;").fetchone()[0])
            asset_count = int(conn.execute("SELECT COUNT(*) FROM assets;").fetchone()[0])
        finally:
            conn.close()
    except sqlite3.Error as exc:
        schema_warning = f"Could not read system health data: {exc}"

    return render_template(
        "admin_system.html",
        db_path=str(resolved_db_path),
        holder_count=holder_count,
        asset_count=asset_count,
        schema_warning=schema_warning,
        restore_history=restore_history,
    )


def _receipt_cc_settings_context(form_value: str | None = None, error_message: str = "") -> dict[str, object]:
    conn = get_connection()
    try:
        local_value = read_receipt_cc_setting(conn)
    finally:
        conn.close()

    active_value = active_receipt_cc_setting()
    active_addresses = _normalized_email_addresses(active_value)
    if local_value is not None:
        source_label = "Local app setting"
    elif active_addresses:
        source_label = "Environment fallback"
    else:
        source_label = "No CC configured"
    textarea_value = form_value if form_value is not None else (local_value or "")
    return {
        "active_addresses": active_addresses,
        "active_source": source_label,
        "error_message": error_message,
        "has_local_setting": local_value is not None,
        "textarea_value": textarea_value,
    }


@app.route("/admin/receipt-cc", methods=["GET", "POST"])
@require_login
@require_role("admin")
def admin_receipt_cc_settings():
    if request.method == "POST":
        raw_addresses = request.form.get("cc_addresses") or ""
        try:
            conn = get_connection()
            try:
                with conn:
                    saved_addresses = save_receipt_cc_addresses(conn, raw_addresses)
            finally:
                conn.close()
        except ValueError as exc:
            return (
                render_template(
                    "admin_receipt_cc.html",
                    **_receipt_cc_settings_context(form_value=raw_addresses, error_message=str(exc)),
                ),
                400,
            )

        if saved_addresses:
            flash(f"Receipt CC saved: {', '.join(saved_addresses)}.", "success")
        else:
            flash("Local receipt CC cleared. Environment fallback applies when configured.", "success")
        return redirect(url_for("admin_receipt_cc_settings"))

    return render_template("admin_receipt_cc.html", **_receipt_cc_settings_context())


def _parse_slot_provision_identifiers(raw_value: str, errors: list[str]) -> list[int]:
    tokens = [token for token in re.split(r"[\s,]+", raw_value.strip()) if token]
    if not tokens:
        errors.append("slot_identifiers is required.")
        return []

    parsed: list[int] = []
    seen: set[int] = set()
    duplicates: list[int] = []
    for token in tokens:
        try:
            slot_position = int(token)
        except ValueError:
            errors.append("slot_identifiers must contain only integer slot positions.")
            continue
        if slot_position <= 0:
            errors.append("slot_identifiers must be greater than 0.")
            continue
        if slot_position in seen and slot_position not in duplicates:
            duplicates.append(slot_position)
        seen.add(slot_position)
        parsed.append(slot_position)

    if duplicates:
        errors.append(f"Duplicate slot identifiers in request: {', '.join(str(value) for value in duplicates)}.")
    return parsed


def _parse_slot_provision_count(raw_value: str, errors: list[str]) -> list[int]:
    value = str(raw_value or "").strip()
    if not value:
        errors.append("slot_count is required.")
        return []
    try:
        slot_count = int(value)
    except ValueError:
        errors.append("slot_count must be an integer.")
        return []
    if slot_count <= 0:
        errors.append("slot_count must be greater than 0.")
        return []
    return list(range(1, slot_count + 1))


def _validate_slot_provision_request(
    conn: sqlite3.Connection,
    case_number: str,
    proposed_slots: list[int],
    errors: list[str],
    *,
    require_existing_case: bool = True,
) -> None:
    if not proposed_slots:
        return

    case_row = conn.execute(
        """
        SELECT 1
        FROM slots
        WHERE UPPER(case_name) = UPPER(?)
        LIMIT 1;
        """,
        (case_number,),
    ).fetchone()
    if case_row is None:
        if require_existing_case:
            errors.append("Select an existing case before provisioning empty slots.")
        return
    if not require_existing_case:
        errors.append(f"Case {case_number} already exists. Select existing-case mode to add slots.")
        return

    placeholders = ", ".join("?" for _ in proposed_slots)
    existing_rows = conn.execute(
        f"""
        SELECT slot_position
        FROM slots
        WHERE UPPER(case_name) = UPPER(?)
          AND slot_position IN ({placeholders})
        ORDER BY slot_position ASC;
        """,
        (case_number, *proposed_slots),
    ).fetchall()
    existing_positions = [int(row["slot_position"]) for row in existing_rows]
    if existing_positions:
        errors.append(
            f"Slot identifiers already exist in case {case_number}: "
            f"{', '.join(str(value) for value in existing_positions)}."
        )


@app.route("/admin/slots/provision", methods=["GET", "POST"])
@require_login
@require_role("admin")
def admin_slot_provision():
    guard_result = _require_admin_for_route()
    if guard_result:
        return guard_result

    case_number = ""
    new_case_number = ""
    slot_identifiers = ""
    slot_count = ""
    provision_mode = "existing"
    proposed_slots: list[int] = []

    def render_slot_provision_template():
        conn = get_connection()
        try:
            case_options = _slot_case_options(_list_slot_options(conn))
        finally:
            conn.close()
        return render_template(
            "admin_slot_provision.html",
            case_number=case_number,
            new_case_number=new_case_number,
            slot_identifiers=slot_identifiers,
            slot_count=slot_count,
            provision_mode=provision_mode,
            proposed_slots=proposed_slots,
            case_options=case_options,
        )

    if request.method == "POST":
        action = (request.form.get("action") or "preview").strip().lower()
        provision_mode = (request.form.get("provision_mode") or "existing").strip().lower()
        if provision_mode not in {"existing", "new"}:
            flash("Unsupported slot provisioning mode.", "error")
            return render_slot_provision_template()
        case_number = (request.form.get("case_number") or "").strip().upper()
        new_case_number = (request.form.get("new_case_number") or "").strip().upper()
        if provision_mode == "new":
            case_number = new_case_number
        slot_identifiers = (request.form.get("slot_identifiers") or "").strip()
        slot_count = (request.form.get("slot_count") or "").strip()

        if not case_number:
            flash("case_number is required.", "error")
        if provision_mode == "existing" and not slot_identifiers:
            flash("slot_identifiers is required.", "error")
        if provision_mode == "new" and not slot_count:
            flash("slot_count is required.", "error")
        missing_existing_slots = provision_mode == "existing" and not slot_identifiers
        missing_new_count = provision_mode == "new" and not slot_count
        if not case_number or missing_existing_slots or missing_new_count:
            return render_slot_provision_template()

        conn = get_connection()
        try:
            errors: list[str] = []
            if provision_mode == "new":
                proposed_slots = _parse_slot_provision_count(slot_count, errors)
            else:
                proposed_slots = _parse_slot_provision_identifiers(slot_identifiers, errors)
            if not errors:
                _validate_slot_provision_request(
                    conn,
                    case_number,
                    proposed_slots,
                    errors,
                    require_existing_case=provision_mode == "existing",
                )
            if errors:
                for error in errors:
                    flash(error, "error")
                return render_slot_provision_template()

            if action == "preview":
                flash("Slot provisioning preview ready. Review before committing.", "success")
                return render_slot_provision_template()

            if action != "commit":
                flash("Unsupported slot provisioning action.", "error")
                return render_slot_provision_template()

            expected_case_number = (request.form.get("expected_case_number") or "").strip().upper()
            expected_slot_identifiers = (request.form.get("expected_slot_identifiers") or "").strip()
            expected_slot_count = (request.form.get("expected_slot_count") or "").strip()
            expected_provision_mode = (request.form.get("expected_provision_mode") or "existing").strip().lower()
            if request.form.get("confirm_slot_provision") != "yes":
                flash("Please confirm you reviewed the slot provisioning preview before committing.", "error")
                return render_slot_provision_template()
            if (
                expected_case_number != case_number
                or (provision_mode == "existing" and expected_slot_identifiers != slot_identifiers)
                or (provision_mode == "new" and expected_slot_count != slot_count)
                or expected_provision_mode != provision_mode
            ):
                flash("Preview changed. Review the slot provisioning preview again before committing.", "error")
                proposed_slots = []
                return render_slot_provision_template()

            try:
                conn.execute("BEGIN;")
                commit_errors: list[str] = []
                _validate_slot_provision_request(
                    conn,
                    case_number,
                    proposed_slots,
                    commit_errors,
                    require_existing_case=provision_mode == "existing",
                )
                if commit_errors:
                    raise ValueError("; ".join(commit_errors))
                conn.executemany(
                    """
                    INSERT INTO slots (case_name, slot_position, current_asset_tag)
                    VALUES (?, ?, ?);
                    """,
                    [(case_number, slot_position, None) for slot_position in proposed_slots],
                )
                conn.commit()
            except ValueError as e:
                conn.rollback()
                flash(str(e), "error")
                return render_slot_provision_template()
            except sqlite3.IntegrityError as e:
                conn.rollback()
                flash(f"Could not create empty slots: {e}", "error")
                return render_slot_provision_template()
            except Exception:
                conn.rollback()
                raise
        finally:
            conn.close()

        flash(
            f"Created {len(proposed_slots)} empty slots for case {case_number}: "
            f"{', '.join(str(slot) for slot in proposed_slots)}.",
            "success",
        )
        return redirect(url_for("admin_slot_provision"))

    return render_slot_provision_template()


@app.route("/admin/case-corrections", methods=["GET", "POST"])
@require_login
@require_role("admin")
def admin_case_corrections():
    guard_result = _require_admin_for_route()
    if guard_result:
        return guard_result

    event_type = (request.form.get("event_type") or "CASE_RENAME").strip().upper()
    old_case_name = _normalize_case_identifier(request.form.get("old_case_name"))
    new_case_name = _normalize_case_identifier(request.form.get("new_case_name"))
    confirmed = _is_truthy(request.form.get("confirm_correction"))
    correction_preview: Optional[dict] = None

    conn = get_connection()
    try:
        def render_case_corrections():
            return render_template(
                "admin_case_corrections.html",
                event_type=event_type,
                old_case_name=old_case_name,
                new_case_name=new_case_name,
                case_options=_list_case_correction_case_options(conn),
                correction_preview=correction_preview,
                correction_history=_list_case_correction_history(conn),
                confirmed=confirmed,
            )

        if request.method == "POST":
            action = (request.form.get("action") or "preview").strip().lower()

            if action == "preview":
                try:
                    correction_preview = _build_case_correction_preview(
                        conn,
                        event_type=event_type,
                        old_case_name=old_case_name,
                        new_case_name=new_case_name,
                    )
                    event_type = str(correction_preview["event_type"])
                    old_case_name = str(correction_preview["old_case_name"])
                    new_case_name = str(correction_preview["new_case_name"] or "")
                except ValueError as e:
                    flash(str(e), "error")
                return render_case_corrections()
            if action != "commit":
                flash("Unknown action.", "error")
                return render_case_corrections()
            if not confirmed:
                flash("You must confirm the Case correction before commit.", "error")
                return render_case_corrections()

            actor_user = current_user()
            if actor_user is None:
                return _require_admin_for_route()

            try:
                conn.execute("BEGIN IMMEDIATE;")
                correction_preview = _build_case_correction_preview(
                    conn,
                    event_type=event_type,
                    old_case_name=old_case_name,
                    new_case_name=new_case_name,
                )
                _validate_case_correction_expected(
                    correction_preview,
                    expected_event_type=request.form.get("expected_event_type") or "",
                    expected_old_case_name=request.form.get("expected_old_case_name") or "",
                    expected_new_case_name=request.form.get("expected_new_case_name") or "",
                    expected_slot_count=request.form.get("expected_slot_count") or "",
                    expected_asset_count=request.form.get("expected_asset_count") or "",
                )
                event_type = str(correction_preview["event_type"])
                old_case_name = str(correction_preview["old_case_name"])
                new_case_name = str(correction_preview["new_case_name"] or "")
                _commit_case_correction(
                    conn,
                    preview=correction_preview,
                    actor_user=actor_user,
                    created_at=datetime.now(timezone.utc).isoformat(),
                )
                conn.commit()
            except ValueError as e:
                conn.rollback()
                flash(str(e), "error")
                return render_case_corrections()
            except Exception:
                conn.rollback()
                raise

            if event_type == "CASE_RENAME":
                flash(f"Renamed Case {old_case_name} to {new_case_name}.", "success")
            else:
                flash(f"Removed never-used Case {old_case_name}.", "success")
            return redirect(url_for("admin_case_corrections"))
        return render_case_corrections()
    finally:
        conn.close()


@app.post("/admin/recovery/acknowledge")
@require_login
@require_role("admin")
def admin_recovery_acknowledge():
    rate_limit_response = _enforce_admin_route_rate_limit(html_redirect_endpoint="admin_system")
    if rate_limit_response is not None:
        return rate_limit_response

    cleared = clear_recovery_state(_resolved_runtime_db_path())
    if cleared:
        flash("Recovery mode acknowledged and cleared. Normal receipt delivery actions may resume.", "success")
    else:
        flash("Recovery mode is already inactive.", "success")
    return redirect(url_for("admin_system"))


HOLDER_IMPORT_PENDING_SESSION_KEY = "pending_holder_import"


def _clear_pending_holder_import() -> None:
    pending = session.pop(HOLDER_IMPORT_PENDING_SESSION_KEY, None)
    if not isinstance(pending, dict):
        return
    pending_path = pending.get("path")
    if not pending_path:
        return
    try:
        Path(str(pending_path)).unlink(missing_ok=True)
    except OSError:
        pass


def _pending_holder_import_path() -> Path | None:
    pending = session.get(HOLDER_IMPORT_PENDING_SESSION_KEY)
    if not isinstance(pending, dict):
        return None
    pending_path = pending.get("path")
    if not pending_path:
        return None
    path = Path(str(pending_path))
    if not path.exists():
        session.pop(HOLDER_IMPORT_PENDING_SESSION_KEY, None)
        return None
    return path


def _holder_import_flash(report: HolderImportReport) -> None:
    summary = report.summary()
    if report.errors:
        flash(
            (
                f"Holder import failed. Processed {summary['processed']} row"
                f"{'' if summary['processed'] == 1 else 's'} with {summary['errors']} error"
                f"{'' if summary['errors'] == 1 else 's'}."
            ),
            "error",
        )
    else:
        flash(
            (
                f"Holder import complete. Processed {summary['processed']} row"
                f"{'' if summary['processed'] == 1 else 's'}: created {summary['created']}, "
                f"updated {summary['updated']}."
            ),
            "success",
        )


def _list_holder_import_history() -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT
                created_at,
                actor_username,
                source_filename,
                processed_count,
                created_count,
                updated_count
            FROM holder_import_events
            ORDER BY id DESC;
            """
        ).fetchall()
        history = []
        for row in rows:
            processed_count = int(row["processed_count"])
            created_count = int(row["created_count"])
            updated_count = int(row["updated_count"])
            history.append(
                {
                    "created_at": str(row["created_at"] or ""),
                    "actor_username": str(row["actor_username"] or ""),
                    "source_filename": str(row["source_filename"] or ""),
                    "processed_count": processed_count,
                    "created_count": created_count,
                    "updated_count": updated_count,
                    "unchanged_count": max(0, processed_count - created_count - updated_count),
                }
            )
        return history
    finally:
        conn.close()


@app.route("/admin/holders/import", methods=["GET", "POST"])
@require_login
@require_role("admin")
def admin_holder_import():
    preview: HolderImportPreview | None = None
    report: HolderImportReport | None = None
    pending_filename = ""

    def render_holder_import():
        return render_template(
            "admin_holder_import.html",
            preview=preview,
            report=report,
            pending_filename=pending_filename,
            holder_import_history=_list_holder_import_history(),
        )

    if request.method == "POST":
        action = (request.form.get("action") or "preview").strip().lower()

        if action == "cancel":
            _clear_pending_holder_import()
            flash("Holder import preview cleared.", "success")
            return render_holder_import()

        if action == "commit":
            temp_path = _pending_holder_import_path()
            if temp_path is None:
                flash("Upload and preview a Holder CSV before confirming import.", "error")
                return render_holder_import()

            preview = preview_holders_csv(temp_path, db_path=_resolved_runtime_db_path())
            pending = session.get(HOLDER_IMPORT_PENDING_SESSION_KEY)
            pending_filename = str((pending or {}).get("filename") or "") if isinstance(pending, dict) else ""
            if not preview.can_commit:
                flash("Holder import preview has blocked rows. Fix the CSV and upload again.", "error")
                return render_holder_import()

            user = current_user()
            if user is None:
                return _require_admin_for_route()
            report = import_holders_csv(
                temp_path,
                db_path=_resolved_runtime_db_path(),
                audit_context=HolderImportAuditContext(
                    actor_user_id=int(user["id"]),
                    actor_username=str(user.get("username") or ""),
                    source_filename=pending_filename,
                ),
            )
            _holder_import_flash(report)
            _clear_pending_holder_import()
            preview = None
            pending_filename = ""
            return render_holder_import()

        _clear_pending_holder_import()
        upload = request.files.get("csv_file")
        filename = str((upload.filename if upload is not None else "") or "").strip()
        if upload is None or not filename:
            flash("Choose a CSV file to preview.", "error")
            return render_holder_import()

        with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as handle:
            upload.save(handle)
            temp_path = Path(handle.name)

        session[HOLDER_IMPORT_PENDING_SESSION_KEY] = {"path": str(temp_path), "filename": filename}
        preview = preview_holders_csv(temp_path, db_path=_resolved_runtime_db_path())
        pending_filename = filename
        if preview.can_commit:
            flash("Holder import preview ready. Review and confirm one batch to commit.", "success")
        else:
            flash("Holder import preview found blocked rows. Fix the CSV and upload again.", "error")

    return render_holder_import()

def _analyze_asset_import_csv(temp_path: Path, *, filename: str) -> dict:
    return analyze_asset_import_csv(temp_path, filename=filename, collect_row_errors=True).to_template_result()


def _analyze_asset_import_xlsx(temp_path: Path, *, filename: str) -> dict:
    return analyze_asset_import_xlsx(temp_path, filename=filename, collect_row_errors=True).to_template_result()


def _analyze_asset_import_upload(
    temp_path: Path,
    *,
    filename: str,
    suffix: str,
    unslotted_acknowledged: bool = False,
) -> dict:
    if suffix == ".csv":
        analysis = analyze_asset_import_csv(temp_path, filename=filename, collect_row_errors=True)
    elif suffix == ".xlsx":
        analysis = analyze_asset_import_xlsx(temp_path, filename=filename, collect_row_errors=True)
    else:
        raise ValueError("Unsupported file type. Upload a .csv or .xlsx file.")

    resolved_db_path = _resolved_runtime_db_path()
    conn = sqlite3.connect(f"file:{resolved_db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        preview = build_asset_import_preview(
            conn,
            analysis,
            unslotted_acknowledged=unslotted_acknowledged,
        )
    finally:
        conn.close()
    return preview.to_template_result()



ASSET_IMPORT_PENDING_SESSION_KEY = "pending_asset_import"


def _clear_pending_asset_import() -> None:
    pending = session.pop(ASSET_IMPORT_PENDING_SESSION_KEY, None)
    if not isinstance(pending, dict):
        return
    temp_path_value = str(pending.get("temp_path") or "").strip()
    if temp_path_value:
        Path(temp_path_value).unlink(missing_ok=True)


def _asset_import_file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _asset_import_preview_token(*, result: dict, file_sha256: str, suffix: str, filename: str, unslotted_acknowledged: bool) -> str:
    payload = {
        "result": result,
        "file_sha256": file_sha256,
        "suffix": suffix,
        "filename": filename,
        "unslotted_acknowledged": unslotted_acknowledged,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_pending_asset_import() -> dict[str, object] | None:
    pending = session.get(ASSET_IMPORT_PENDING_SESSION_KEY)
    if not isinstance(pending, dict):
        return None
    temp_path_value = str(pending.get("temp_path") or "").strip()
    if not temp_path_value:
        _clear_pending_asset_import()
        return None
    temp_path = Path(temp_path_value)
    if not temp_path.exists() or not temp_path.is_file():
        _clear_pending_asset_import()
        return None
    return pending


def _analyze_asset_import_to_preview(conn: sqlite3.Connection, temp_path: Path, *, filename: str, suffix: str, unslotted_acknowledged: bool):
    if suffix == ".csv":
        analysis = analyze_asset_import_csv(temp_path, filename=filename, collect_row_errors=True)
    elif suffix == ".xlsx":
        analysis = analyze_asset_import_xlsx(temp_path, filename=filename, collect_row_errors=True)
    else:
        raise ValueError("Unsupported file type. Upload a .csv or .xlsx file.")
    return build_asset_import_preview(conn, analysis, unslotted_acknowledged=unslotted_acknowledged), analysis


def _store_pending_asset_import(*, temp_path: Path, filename: str, suffix: str, result: dict, unslotted_acknowledged: bool) -> str:
    _clear_pending_asset_import()
    file_sha256 = _asset_import_file_sha256(temp_path)
    token = _asset_import_preview_token(
        result=result,
        file_sha256=file_sha256,
        suffix=suffix,
        filename=filename,
        unslotted_acknowledged=unslotted_acknowledged,
    )
    session[ASSET_IMPORT_PENDING_SESSION_KEY] = {
        "temp_path": str(temp_path),
        "filename": filename,
        "suffix": suffix,
        "file_sha256": file_sha256,
        "preview_token": token,
        "unslotted_acknowledged": unslotted_acknowledged,
    }
    return token


def _asset_import_row_by_number(analysis) -> dict[int, object]:
    return {int(row.row_number): row for row in analysis.rows}


def _asset_import_existing_asset(conn: sqlite3.Connection, asset_tag: str) -> sqlite3.Row | None:
    exact = conn.execute(
        """
        SELECT *
        FROM assets
        WHERE asset_tag = ?
        LIMIT 1;
        """,
        (asset_tag,),
    ).fetchone()
    if exact is not None:
        return exact
    case_match = conn.execute(
        """
        SELECT *
        FROM assets
        WHERE UPPER(asset_tag) = UPPER(?)
        LIMIT 1;
        """,
        (asset_tag,),
    ).fetchone()
    if case_match is not None:
        return case_match
    lookup_key = barcode_lookup_key(asset_tag)
    if not lookup_key:
        return None
    for row in conn.execute("SELECT * FROM assets;").fetchall():
        if barcode_lookup_key(row["asset_tag"]) == lookup_key:
            return row
    return None


def _asset_import_slot_for_row(conn: sqlite3.Connection, row) -> sqlite3.Row | None:
    if row.storage_intent != "slotted":
        return None
    slot_position = int(row.slot_identifier)
    exact = conn.execute(
        """
        SELECT id, case_name, slot_position, current_asset_tag
        FROM slots
        WHERE case_name = ?
          AND slot_position = ?
        LIMIT 1;
        """,
        (row.case_identifier, slot_position),
    ).fetchone()
    if exact is not None:
        return exact
    return conn.execute(
        """
        SELECT id, case_name, slot_position, current_asset_tag
        FROM slots
        WHERE UPPER(case_name) = UPPER(?)
          AND slot_position = ?
        LIMIT 1;
        """,
        (row.case_identifier, slot_position),
    ).fetchone()


def _asset_import_get_or_create_slot_for_row(conn: sqlite3.Connection, row) -> sqlite3.Row | None:
    if row.storage_intent != "slotted":
        return None
    slot = _asset_import_slot_for_row(conn, row)
    if slot is not None:
        return slot
    try:
        slot_position = int(row.slot_identifier)
    except ValueError as exc:
        raise ValueError(f"Row {row.row_number}: slot_identifier must be numeric.") from exc
    conn.execute(
        """
        INSERT INTO slots (case_name, slot_position, current_asset_tag)
        VALUES (?, ?, NULL);
        """,
        (row.case_identifier, slot_position),
    )
    slot = _asset_import_slot_for_row(conn, row)
    if slot is None:
        raise ValueError(f"Row {row.row_number}: requested slot could not be created.")
    return slot


def _asset_import_provision_case_plans(conn: sqlite3.Connection, case_plans: tuple[dict[str, object], ...]) -> None:
    for plan in case_plans:
        case_name = str(plan.get("case_name") or "").strip().upper()
        if not case_name:
            continue
        quantity = int(plan.get("quantity") or 0)
        assigned_count = int(plan.get("assigned_count") or 0)
        if assigned_count > quantity:
            continue
        save_case_size(conn, case_name, plan.get("case_size") or "")
        for slot_position in range(1, quantity + 1):
            conn.execute(
                """
                INSERT OR IGNORE INTO slots (case_name, slot_position, current_asset_tag)
                VALUES (?, ?, NULL);
                """,
                (case_name, slot_position),
            )


def _asset_import_slot_is_available_for(conn: sqlite3.Connection, *, slot_id: int, asset_tag: str) -> bool:

    occupant = conn.execute(
        """
        SELECT a.asset_tag
        FROM slot_occupancy so
        JOIN assets a ON a.id = so.asset_id
        WHERE so.slot_id = ?
        LIMIT 1;
        """,
        (slot_id,),
    ).fetchone()
    if occupant is not None and barcode_lookup_key(occupant["asset_tag"]) != barcode_lookup_key(asset_tag):
        return False
    slot = conn.execute("SELECT current_asset_tag FROM slots WHERE id = ? LIMIT 1;", (slot_id,)).fetchone()
    current_tag = str(slot["current_asset_tag"] or "").strip() if slot else ""
    return not current_tag or barcode_lookup_key(current_tag) == barcode_lookup_key(asset_tag)


def _asset_import_new_asset_values(conn: sqlite3.Connection, row, *, now_iso: str, slot: sqlite3.Row | None) -> dict[str, object]:
    values: dict[str, object] = {
        "asset_tag": row.asset_tag,
        "equipment_type": row.equipment_type,
        "custody_state": "in_stock",
        "accountability_status": "accountable",
        "condition": "serviceable",
        "created_date": now_iso,
        "location_type": "STORAGE",
        "current_holder_id": None,
    }
    optional_fields = {
        "serial_number": row.serial_number,
        "manufacturer": row.manufacturer,
        "model": row.model,
        "model_code": row.model_code,
        "building_room": row.building_room,
        "building": row.location_building,
        "notes": row.notes,
    }
    values.update({field: value for field, value in optional_fields.items() if value})
    if slot is not None:
        values.update(
            {
                "home_slot_id": int(slot["id"]),
                "case_number": str(slot["case_name"]),
                "slot_number": str(slot["slot_position"]),
            }
        )
    return {field: value for field, value in values.items() if field in get_asset_table_columns(conn)}


def _asset_import_insert_asset(conn: sqlite3.Connection, values: dict[str, object]) -> int:
    column_names = list(values)
    cursor = conn.execute(
        f"INSERT INTO assets ({', '.join(column_names)}) VALUES ({', '.join('?' for _ in column_names)});",
        [values[column] for column in column_names],
    )
    return int(cursor.lastrowid)


def _asset_import_append_event(
    conn: sqlite3.Connection,
    *,
    asset_tag: str,
    event_type: str,
    event_date: str,
    actor: str,
    payload: dict,
    notes: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO asset_events (asset_tag, event_type, event_date, actor, notes, payload, holder_id)
        VALUES (?, ?, ?, ?, ?, ?, ?);
        """,
        (asset_tag, event_type, event_date, actor, notes, json.dumps(payload), None),
    )


def _asset_import_create_new_asset(conn: sqlite3.Connection, row, *, now_iso: str, actor: str, slot: sqlite3.Row | None) -> None:
    values = _asset_import_new_asset_values(conn, row, now_iso=now_iso, slot=slot)
    asset_id = _asset_import_insert_asset(conn, values)
    _asset_import_append_event(
        conn,
        asset_tag=row.asset_tag,
        event_type="ASSET_CREATED",
        event_date=now_iso,
        actor=actor,
        payload={"asset": values, "row_number": row.row_number},
    )
    if slot is None:
        return
    slot_id = int(slot["id"])
    if not _asset_import_slot_is_available_for(conn, slot_id=slot_id, asset_tag=row.asset_tag):
        raise ValueError(f"Row {row.row_number}: requested slot is no longer available.")
    conn.execute("INSERT INTO slot_occupancy (slot_id, asset_id, assigned_at) VALUES (?, ?, ?);", (slot_id, asset_id, now_iso))
    conn.execute("UPDATE slots SET current_asset_tag = ? WHERE id = ?;", (row.asset_tag, slot_id))
    _asset_import_append_event(
        conn,
        asset_tag=row.asset_tag,
        event_type="SLOT_ASSIGN",
        event_date=now_iso,
        actor=actor,
        payload={
            "slot": {
                "slot_id": slot_id,
                "case_number": str(slot["case_name"]),
                "slot_number": int(slot["slot_position"]),
            },
            "row_number": row.row_number,
        },
    )


def _asset_import_metadata_updates(row) -> dict[str, object]:
    values: dict[str, object] = {}
    for row_field, asset_field, source_field in (
        ("serial_number", "serial_number", "serial_number"),
        ("equipment_type", "equipment_type", "equipment_type"),
        ("manufacturer", "manufacturer", "manufacturer"),
        ("model", "model", "model"),
        ("model_code", "model_code", "model_code"),
        ("building_room", "building_room", "building_room"),
        ("location_building", "building", "location_building"),
        ("notes", "notes", "notes_comments"),
    ):
        if row.has_source_field(source_field):
            value = str(getattr(row, row_field) or "").strip()
            if value:
                values[asset_field] = value
    return values


def _asset_import_apply_metadata_update(
    conn: sqlite3.Connection,
    *,
    asset: sqlite3.Row,
    row,
    values: dict[str, object],
    now_iso: str,
    actor: str,
) -> bool:
    asset_columns = get_asset_table_columns(conn)
    changed = {
        field: value
        for field, value in values.items()
        if field in asset_columns and str((asset[field] if field in asset.keys() else "") or "").strip() != str(value).strip()
    }
    if not changed:
        return False
    if "updated_date" in asset_columns:
        changed["updated_date"] = now_iso
    conn.execute(
        f"UPDATE assets SET {', '.join(f'{field} = ?' for field in changed)} WHERE id = ?;",
        [changed[field] for field in changed] + [int(asset["id"])],
    )
    _asset_import_append_event(
        conn,
        asset_tag=str(asset["asset_tag"]),
        event_type="ASSET_UPDATED",
        event_date=now_iso,
        actor=actor,
        payload={"fields": changed, "row_number": row.row_number},
    )
    return True


def _asset_import_move_existing_asset_to_slot(
    conn: sqlite3.Connection,
    *,
    asset: sqlite3.Row,
    source_slot_id: int,
    destination_slot: sqlite3.Row,
    row,
    now_iso: str,
    actor: str,
) -> None:
    destination_slot_id = int(destination_slot["id"])
    source_slot = conn.execute(
        """
        SELECT id, case_name, slot_position
        FROM slots
        WHERE id = ?
        LIMIT 1;
        """,
        (source_slot_id,),
    ).fetchone()
    if source_slot is None:
        raise ValueError(f"Row {row.row_number}: source slot is missing.")
    delete_source = conn.execute(
        """
        DELETE FROM slot_occupancy
        WHERE slot_id = ? AND asset_id = ?;
        """,
        (source_slot_id, int(asset["id"])),
    )
    if delete_source.rowcount != 1:
        raise ValueError(f"Row {row.row_number}: source slot is missing or empty.")
    conn.execute(
        """
        INSERT INTO slot_occupancy (slot_id, asset_id, assigned_at)
        VALUES (?, ?, ?);
        """,
        (destination_slot_id, int(asset["id"]), now_iso),
    )
    conn.execute("UPDATE slots SET current_asset_tag = NULL WHERE id = ?;", (source_slot_id,))
    conn.execute("UPDATE slots SET current_asset_tag = ? WHERE id = ?;", (str(asset["asset_tag"]), destination_slot_id))
    update_values: dict[str, object] = {
        "home_slot_id": destination_slot_id,
        "case_number": str(destination_slot["case_name"]),
        "slot_number": str(destination_slot["slot_position"]),
        "location_type": "STORAGE",
        "updated_date": now_iso,
    }
    asset_columns = get_asset_table_columns(conn)
    filtered = {field: value for field, value in update_values.items() if field in asset_columns}
    if filtered:
        conn.execute(
            f"UPDATE assets SET {', '.join(f'{field} = ?' for field in filtered)} WHERE id = ?;",
            [filtered[field] for field in filtered] + [int(asset["id"])],
        )
    _asset_import_append_event(
        conn,
        asset_tag=str(asset["asset_tag"]),
        event_type="SLOT_MOVE",
        event_date=now_iso,
        actor=actor,
        notes="Asset import slot move",
        payload={
            "from_slot": {
                "slot_id": source_slot_id,
                "case_number": str(source_slot["case_name"]),
                "slot_number": int(source_slot["slot_position"]),
            },
            "to_slot": {
                "slot_id": destination_slot_id,
                "case_number": str(destination_slot["case_name"]),
                "slot_number": int(destination_slot["slot_position"]),
            },
            "row_number": row.row_number,
        },
    )


def _asset_import_apply_existing_update(conn: sqlite3.Connection, row, preview_row, *, now_iso: str, actor: str) -> tuple[bool, bool]:
    asset = _asset_import_existing_asset(conn, row.asset_tag)
    if asset is None:
        raise ValueError(f"Row {row.row_number}: asset changed since preview.")
    movement_change = any(change.field == "home_slot" for change in preview_row.changed_fields)
    metadata_values = _asset_import_metadata_updates(row)
    moved = False
    if movement_change:
        slot = _asset_import_get_or_create_slot_for_row(conn, row)
        if slot is None:
            raise ValueError(f"Row {row.row_number}: destination slot is unavailable.")
        if not _asset_import_slot_is_available_for(conn, slot_id=int(slot["id"]), asset_tag=row.asset_tag):
            raise ValueError(f"Row {row.row_number}: requested slot is no longer available.")
        source_slot_id = asset["home_slot_id"]
        if source_slot_id is None:
            conn.execute(
                """
                INSERT INTO slot_occupancy (slot_id, asset_id, assigned_at)
                VALUES (?, ?, ?);
                """,
                (int(slot["id"]), int(asset["id"]), now_iso),
            )
            conn.execute("UPDATE slots SET current_asset_tag = ? WHERE id = ?;", (row.asset_tag, int(slot["id"])))
            update_values: dict[str, object] = {
                "home_slot_id": int(slot["id"]),
                "case_number": str(slot["case_name"]),
                "slot_number": str(slot["slot_position"]),
                "location_type": "STORAGE",
                "updated_date": now_iso,
            }
            asset_columns = get_asset_table_columns(conn)
            filtered = {field: value for field, value in update_values.items() if field in asset_columns}
            conn.execute(
                f"UPDATE assets SET {', '.join(f'{field} = ?' for field in filtered)} WHERE id = ?;",
                [filtered[field] for field in filtered] + [int(asset["id"])],
            )
            _asset_import_append_event(
                conn,
                asset_tag=str(asset["asset_tag"]),
                event_type="SLOT_ASSIGN",
                event_date=now_iso,
                actor=actor,
                payload={
                    "slot": {
                        "slot_id": int(slot["id"]),
                        "case_number": str(slot["case_name"]),
                        "slot_number": int(slot["slot_position"]),
                    },
                    "row_number": row.row_number,
                },
            )
        else:
            _asset_import_move_existing_asset_to_slot(
                conn,
                asset=asset,
                source_slot_id=int(source_slot_id),
                destination_slot=slot,
                row=row,
                now_iso=now_iso,
                actor=actor,
            )
        moved = True
        asset = _asset_import_existing_asset(conn, row.asset_tag)
        if asset is None:
            raise ValueError(f"Row {row.row_number}: asset changed during update.")
        metadata_values.pop("building_room", None)
    metadata_updated = _asset_import_apply_metadata_update(
        conn,
        asset=asset,
        row=row,
        values=metadata_values,
        now_iso=now_iso,
        actor=actor,
    )
    return moved, metadata_updated


def _commit_asset_import_pending(*, submitted_token: str) -> tuple[dict, dict]:
    pending = _load_pending_asset_import()
    if pending is None:
        raise ValueError("Import preview expired. Upload and preview the file again.")
    expected_token = str(pending.get("preview_token") or "")
    if not submitted_token or submitted_token != expected_token:
        _clear_pending_asset_import()
        raise ValueError("Import confirmation is stale or tampered. Preview the file again.")

    temp_path = Path(str(pending["temp_path"]))
    filename = str(pending["filename"])
    suffix = str(pending["suffix"])
    unslotted_acknowledged = bool(pending.get("unslotted_acknowledged"))
    file_sha256 = _asset_import_file_sha256(temp_path)
    if file_sha256 != str(pending.get("file_sha256") or ""):
        _clear_pending_asset_import()
        raise ValueError("Uploaded file changed after preview. Upload and preview the file again.")

    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE;")
        preview, analysis = _analyze_asset_import_to_preview(
            conn,
            temp_path,
            filename=filename,
            suffix=suffix,
            unslotted_acknowledged=unslotted_acknowledged,
        )
        result = preview.to_template_result()
        current_token = _asset_import_preview_token(
            result=result,
            file_sha256=file_sha256,
            suffix=suffix,
            filename=filename,
            unslotted_acknowledged=unslotted_acknowledged,
        )
        if current_token != expected_token:
            _clear_pending_asset_import()
            raise ValueError("Import preview is stale. Upload and preview the file again.")
        if preview.blocks_without_unslotted_acknowledgment:
            _clear_pending_asset_import()
            raise ValueError("Unslotted acknowledgment is required before commit.")

        rows_by_number = _asset_import_row_by_number(analysis)
        now_iso = datetime.now(timezone.utc).isoformat()
        actor = "admin"
        summary = {
            "created": 0,
            "updated": 0,
            "slot_assigned": 0,
            "slot_moved": 0,
            "unchanged": 0,
            "blocked": 0,
            "committed_rows": 0,
        }
        _asset_import_provision_case_plans(conn, analysis.case_plans)
        safe_unslotted_categories = {"unslotted_import", "slot_conflict_unslotted"}
        for preview_row in preview.rows:
            row = rows_by_number.get(int(preview_row.row_number))
            if row is None:
                summary["blocked"] += 1
                continue
            if preview_row.category == "unchanged_exact_match":
                summary["unchanged"] += 1
                continue
            if preview_row.category == "new_asset":
                slot = _asset_import_get_or_create_slot_for_row(conn, row)
                if slot is None:
                    raise ValueError(f"Row {row.row_number}: requested slot is unavailable.")
                _asset_import_create_new_asset(conn, row, now_iso=now_iso, actor=actor, slot=slot)
                summary["created"] += 1
                summary["slot_assigned"] += 1
                summary["committed_rows"] += 1
                continue
            if preview_row.category in safe_unslotted_categories and preview.unslotted_acknowledged:
                if _asset_import_existing_asset(conn, row.asset_tag) is None:
                    _asset_import_create_new_asset(conn, row, now_iso=now_iso, actor=actor, slot=None)
                    summary["created"] += 1
                    summary["committed_rows"] += 1
                else:
                    summary["blocked"] += 1
                continue
            if preview_row.category == "proposed_update":
                moved, metadata_updated = _asset_import_apply_existing_update(conn, row, preview_row, now_iso=now_iso, actor=actor)
                if moved:
                    summary["slot_moved"] += 1
                if metadata_updated:
                    summary["updated"] += 1
                if moved or metadata_updated:
                    summary["committed_rows"] += 1
                continue
            summary["blocked"] += 1

        conn.commit()
        _clear_pending_asset_import()
        return result, summary
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _asset_import_reconciliation_csv_response() -> object:
    pending = _load_pending_asset_import()
    if pending is None:
        flash("Import preview expired. Upload and preview the file again.", "error")
        return redirect(url_for("admin_asset_import"))
    temp_path = Path(str(pending["temp_path"]))
    resolved_db_path = _resolved_runtime_db_path()
    conn = sqlite3.connect(f"file:{resolved_db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        preview, _analysis = _analyze_asset_import_to_preview(
            conn,
            temp_path,
            filename=str(pending["filename"]),
            suffix=str(pending["suffix"]),
            unslotted_acknowledged=bool(pending.get("unslotted_acknowledged")),
        )
    finally:
        conn.close()

    output = BytesIO()
    wrapper = TextIOWrapper(output, encoding="utf-8", newline="")
    writer = csv.writer(wrapper)
    writer.writerow(["row_number", "asset_tag", "category", "message", "changed_fields", "warnings"])
    for row in preview.rows:
        writer.writerow(
            [
                row.row_number,
                row.asset_tag,
                row.category,
                row.message,
                "; ".join(f"{change.field}: {change.current} -> {change.proposed}" for change in row.changed_fields),
                "; ".join(row.warnings),
            ]
        )
    if preview.obsolete_assets:
        writer.writerow([])
        writer.writerow(["active_network_assets_absent_from_workbook"])
        writer.writerow(["asset_tag", "serial_number", "equipment_type", "location_type"])
        for asset in preview.obsolete_assets:
            writer.writerow(
                [
                    asset["asset_tag"],
                    asset["serial_number"],
                    asset["equipment_type"],
                    asset["location_type"],
                ]
            )
    wrapper.flush()
    response = make_response(output.getvalue())
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = "attachment; filename=asset-import-reconciliation.csv"
    return response


@app.route("/admin/assets/import", methods=["GET", "POST"])
@require_login
@require_role("admin")
def admin_asset_import():
    result: dict | None = None
    commit_summary: dict | None = None

    if request.method == "POST":
        action = (request.form.get("action") or "analyze").strip().lower()
        if action == "commit":
            if (request.form.get("confirm_import") or "").strip() != "1":
                _clear_pending_asset_import()
                flash("Confirm the preview before committing approved rows.", "error")
                return render_template("admin_asset_import.html", result=None, commit_summary=None)
            try:
                result, commit_summary = _commit_asset_import_pending(
                    submitted_token=(request.form.get("preview_token") or "").strip()
                )
                flash(
                    "Asset import committed. Safe rows were written atomically; blocked rows were left unchanged.",
                    "success",
                )
            except ValueError as exc:
                flash(str(exc), "error")
            return render_template("admin_asset_import.html", result=result, commit_summary=commit_summary)

        upload = request.files.get("asset_file")
        filename = str((upload.filename if upload is not None else "") or "").strip()
        if upload is None or not filename:
            flash("Choose a .csv or .xlsx file to analyze.", "error")
            return render_template("admin_asset_import.html", result=None, commit_summary=None)

        suffix = Path(filename).suffix.lower()
        tempfile_suffix = ASSET_IMPORT_TEMPFILE_SUFFIXES.get(suffix)
        if tempfile_suffix is None:
            flash("Unsupported file type. Upload a .csv or .xlsx file.", "error")
            return render_template("admin_asset_import.html", result=None, commit_summary=None)

        temp_path: Path | None = None
        keep_temp_path = False
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=tempfile_suffix) as handle:
                upload.save(handle)
                temp_path = Path(handle.name)

            unslotted_acknowledged = (request.form.get("acknowledge_unslotted") or "").strip() == "1"
            result = _analyze_asset_import_upload(
                temp_path,
                filename=filename,
                suffix=suffix,
                unslotted_acknowledged=unslotted_acknowledged,
            )
            preview_token = _store_pending_asset_import(
                temp_path=temp_path,
                filename=filename,
                suffix=suffix,
                result=result,
                unslotted_acknowledged=unslotted_acknowledged,
            )
            result["preview_token"] = preview_token
            keep_temp_path = True
            flash("Asset import preview complete. No database changes were made.", "success")
        except ValueError as exc:
            flash(str(exc), "error")
        finally:
            if temp_path is not None and not keep_temp_path:
                temp_path.unlink(missing_ok=True)

    return render_template("admin_asset_import.html", result=result, commit_summary=commit_summary)


@app.get("/admin/assets/import/reconciliation.csv")
@require_login
@require_role("admin")
def admin_asset_import_reconciliation_csv():
    return _asset_import_reconciliation_csv_response()

@app.get("/admin/network-assets/import")
@require_login
@require_role("admin")
def admin_network_asset_import():
    return render_template("admin_network_asset_import.html")


@app.get("/admin/network-assets/import/template.csv")
@require_login
@require_role("admin")
def admin_network_asset_import_template():
    template_path = Path(__file__).resolve().parents[2] / "docs/fixtures/imports/asset_import_template.csv"
    return send_file(
        template_path,
        mimetype="text/csv",
        as_attachment=True,
        download_name="asset_import_template.csv",
    )


def _duration_label(value: timedelta) -> str:
    total_seconds = max(0, int(value.total_seconds()))
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _seconds = divmod(remainder, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes or not parts:
        parts.append(f"{minutes}m")
    return " ".join(parts)


def _timestamp_label(value: datetime | None) -> str:
    if value is None:
        return "OUTSTANDING"
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _holder_summary_label(holder: HolderSummary) -> str:
    return holder.label or "Unknown holder"


def _holder_identifier_label(holder: HolderSummary) -> str:
    return "No holder ID" if holder.holder_id is None else str(holder.holder_id)


def _accountability_state_label(value: object) -> str:
    labels = {
        "confirmed_checked_in": "Checked in",
        "not_checked_in": "Checked out",
        "unresolved": "Unresolved / inconsistent",
    }
    return labels.get(str(value or "").strip(), str(value or "").strip() or "Unknown")


def _custody_outstanding_rows(report: CustodyAccountabilityReport) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for asset in report.assets:
        for interval in asset.intervals:
            if interval.outstanding:
                rows.append({"asset": asset, "interval": interval})
    return sorted(rows, key=lambda row: str(row["asset"].asset_tag).upper())


def _custody_interval_rows(report: CustodyAccountabilityReport) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for asset in report.assets:
        for interval in asset.intervals:
            rows.append({"asset": asset, "interval": interval})
    return sorted(
        rows,
        key=lambda row: (
            str(row["asset"].asset_tag).upper(),
            row["interval"].issue_timestamp,
            row["interval"].issue_event_id,
        ),
    )


def _custody_exception_rows(report: CustodyAccountabilityReport) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for asset in report.assets:
        for exception in asset.exceptions:
            rows.append({"asset": asset, "exception": exception})
    return sorted(rows, key=lambda row: str(row["asset"].asset_tag).upper())


ANALYTICS_CHART_TYPES: dict[tuple[str, str], tuple[tuple[str, str], ...]] = {
    ("total_time_checked_out", "holder"): (("bar", "Bar"),),
    ("checkout_transactions", "holder"): (("bar", "Bar"),),
    ("number_of_assets", "asset_type"): (("bar", "Bar"),),
    ("checkout_duration", "duration_range"): (("histogram", "Histogram"),),
    ("current_accountability", "accountability_state"): (("bar", "Bar"),),
    ("checkout_transactions", "checkout_date"): (("line", "Line"),),
}

ANALYTICS_MEASURE_LABELS: dict[str, str] = {
    "total_time_checked_out": "Total Time Checked Out",
    "checkout_transactions": "Checkout Transactions",
    "number_of_assets": "Number of Assets",
    "checkout_duration": "Checkout Duration",
    "current_accountability": "Current Accountability",
}

ANALYTICS_GROUPING_LABELS: dict[str, str] = {
    "holder": "MA / Holder",
    "asset_type": "Asset Type",
    "duration_range": "Duration Range",
    "accountability_state": "Accountability State",
    "checkout_date": "Checkout Date",
}


def _analytics_unique_options(values: list[tuple[str, str]]) -> list[tuple[str, str]]:
    seen: set[str] = set()
    options: list[tuple[str, str]] = []
    for value, label in values:
        if value in seen:
            continue
        seen.add(value)
        options.append((value, label))
    return options


def _analytics_measure_options() -> list[tuple[str, str]]:
    return _analytics_unique_options(
        [(selection.measure, ANALYTICS_MEASURE_LABELS[selection.measure]) for selection in SUPPORTED_ANALYTICS]
    )


def _analytics_grouping_options() -> list[tuple[str, str]]:
    return _analytics_unique_options(
        [(selection.grouping, ANALYTICS_GROUPING_LABELS[selection.grouping]) for selection in SUPPORTED_ANALYTICS]
    )


def _analytics_supported_pairs() -> list[dict[str, str]]:
    return [
        {
            "measure": selection.measure,
            "grouping": selection.grouping,
            "label": selection.label,
        }
        for selection in SUPPORTED_ANALYTICS
    ]


def _analytics_selector_mapping() -> list[dict[str, object]]:
    return [
        {
            "measure": selection.measure,
            "measure_label": ANALYTICS_MEASURE_LABELS[selection.measure],
            "grouping": selection.grouping,
            "grouping_label": ANALYTICS_GROUPING_LABELS[selection.grouping],
            "charts": [
                {"value": value, "label": label}
                for value, label in _analytics_chart_types(selection.measure, selection.grouping)
            ],
        }
        for selection in SUPPORTED_ANALYTICS
    ]


def _analytics_chart_types(measure: str, grouping: str) -> tuple[tuple[str, str], ...]:
    return ANALYTICS_CHART_TYPES.get((measure, grouping), ())


def _analytics_value_label(row, dataset: AnalyticsDataset) -> str:
    if dataset.selection.measure == "total_time_checked_out":
        return _duration_label(timedelta(seconds=row.value))
    return str(row.value)


def _analytics_chart_rows(dataset: AnalyticsDataset) -> list[dict[str, object]]:
    max_value = max((row.value for row in dataset.rows), default=0)
    row_count = len(dataset.rows)
    chart_rows: list[dict[str, object]] = []
    for index, row in enumerate(dataset.rows):
        percent = 0 if max_value <= 0 else max(2, round((row.value / max_value) * 100))
        x = 40 if row_count <= 1 else 40 + round((index / (row_count - 1)) * 440)
        chart_rows.append(
            {
                "index": index,
                "key": row.key,
                "label": row.label,
                "value": row.value,
                "value_label": _analytics_value_label(row, dataset),
                "percent": percent,
                "x": x,
                "y": 180 - (0 if max_value <= 0 else round((row.value / max_value) * 140)),
            }
        )
    return chart_rows


def _analytics_line_points(chart_rows: list[dict[str, object]]) -> str:
    return " ".join(f"{row['x']},{row['y']}" for row in chart_rows)


def _load_custody_accountability_report(resolved_db_path: Path, generated_at: datetime) -> CustodyAccountabilityReport:
    conn = sqlite3.connect(f"file:{resolved_db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON;")
    try:
        return build_custody_accountability_report(conn, generated_at=generated_at)
    finally:
        conn.close()


def _build_custody_accountability_pdf(report: CustodyAccountabilityReport) -> bytes:
    styles = getSampleStyleSheet()
    body = ParagraphStyle(
        "CustodyPdfBody",
        parent=styles["BodyText"],
        fontSize=8,
        leading=9.5,
        splitLongWords=False,
        wordWrap="LTR",
    )
    title = styles["Title"]
    heading = styles["Heading2"]
    table_header_style = ParagraphStyle("CustodyPdfHeader", parent=body, fontName="Helvetica-Bold")

    def _text(value: object) -> str:
        return str(value or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def _p(value: object, style: ParagraphStyle = body) -> Paragraph:
        return Paragraph(_text(value), style)

    def _table(headers: list[str], rows: list[list[object]], widths: list[float]) -> Table:
        data = [[_p(header, table_header_style) for header in headers]]
        data.extend([[_p(value) for value in row] for row in rows])
        if len(data) == 1:
            data.append([_p("None")] + [_p("") for _ in headers[1:]])
        table = Table(data, colWidths=widths, repeatRows=1)
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8eef5")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.black),
                    ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#b8c4d0")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 4),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ]
            )
        )
        return table

    def _page_number(canvas_obj, doc):
        canvas_obj.saveState()
        canvas_obj.setFont("Helvetica", 8)
        canvas_obj.drawRightString(doc.pagesize[0] - 0.45 * inch, 0.28 * inch, f"Page {doc.page}")
        canvas_obj.restoreState()

    status_text = (
        "ALL ACTIVE ASSETS ACCOUNTED FOR"
        if report.checked_out == 0 and report.unresolved == 0
        else "OUTSTANDING ASSETS REQUIRE ATTENTION"
    )
    story: list[object] = [
        Paragraph("Asset Custody / Accountability", title),
        Spacer(1, 0.08 * inch),
        Paragraph(f"Generated: {_timestamp_label(report.generated_at)}", body),
        Paragraph(
            "Historical holder evidence is the holder ID stored on the ISSUE event. Names and organizations are current lookups for that ID.",
            body,
        ),
        Paragraph(
            "Historical storage labels are not invented; current storage comes from current slot/building-room records.",
            body,
        ),
        Spacer(1, 0.1 * inch),
        Paragraph(status_text, heading),
        _table(
            ["Active", "Checked In", "Checked Out", "Unresolved"],
            [[report.active_assets, report.checked_in, report.checked_out, report.unresolved]],
            [0.9 * inch, 1.0 * inch, 1.0 * inch, 1.0 * inch],
        ),
        Spacer(1, 0.18 * inch),
        Paragraph("Holder / MA Accountability", heading),
        _table(
            ["Holder ID", "Current Name / Org", "Unique Assets", "Issues", "Total Time", "Longest", "Outstanding", "Outstanding Tags"],
            [
                [
                    _holder_identifier_label(row.holder),
                    _holder_summary_label(row.holder),
                    len(row.unique_asset_tags),
                    row.issue_transaction_count,
                    _duration_label(row.total_custody_time),
                    _duration_label(row.longest_custody_interval),
                    row.currently_outstanding_count,
                    ", ".join(row.outstanding_asset_tags),
                ]
                for row in report.holders
            ],
            [0.6 * inch, 1.45 * inch, 0.65 * inch, 0.55 * inch, 0.8 * inch, 0.8 * inch, 0.7 * inch, 2.35 * inch],
        ),
        Spacer(1, 0.18 * inch),
        Paragraph("Outstanding Assets", heading),
        _table(
            ["Asset", "Holder", "Checkout", "Elapsed", "Storage Evidence"],
            [
                [
                    row["asset"].asset_tag,
                    _holder_summary_label(row["interval"].holder),
                    _timestamp_label(row["interval"].issue_timestamp),
                    _duration_label(row["interval"].elapsed),
                    row["asset"].current_storage_location,
                ]
                for row in _custody_outstanding_rows(report)
            ],
            [1.15 * inch, 1.6 * inch, 1.35 * inch, 0.85 * inch, 2.8 * inch],
        ),
        Spacer(1, 0.18 * inch),
        Paragraph("Asset Custody / Accountability Details", heading),
        _table(
            ["Asset", "Serial", "Type", "State", "Current Holder", "Storage Evidence", "Issues", "Total", "Longest"],
            [
                [
                    asset.asset_tag,
                    asset.serial_number,
                    asset.equipment_type,
                    _accountability_state_label(asset.current_accountability_state),
                    _holder_summary_label(asset.current_holder) if asset.current_holder.holder_id is not None else "",
                    asset.current_storage_location,
                    asset.issue_count,
                    _duration_label(asset.total_custody_duration),
                    _duration_label(asset.longest_custody_interval),
                ]
                for asset in report.assets
            ],
            [0.95 * inch, 0.95 * inch, 0.7 * inch, 1.0 * inch, 1.2 * inch, 1.45 * inch, 0.45 * inch, 0.6 * inch, 0.6 * inch],
        ),
        Spacer(1, 0.18 * inch),
        Paragraph("Custody Intervals", heading),
        _table(
            ["Asset", "Issue", "Holder ID / Current Label", "Return", "Elapsed"],
            [
                [
                    row["asset"].asset_tag,
                    _timestamp_label(row["interval"].issue_timestamp),
                    f"{_holder_identifier_label(row['interval'].holder)} / {_holder_summary_label(row['interval'].holder)}",
                    _timestamp_label(row["interval"].return_timestamp),
                    _duration_label(row["interval"].elapsed),
                ]
                for row in _custody_interval_rows(report)
            ],
            [1.05 * inch, 1.35 * inch, 2.15 * inch, 1.35 * inch, 0.75 * inch],
        ),
        Spacer(1, 0.18 * inch),
        Paragraph("Exceptions", heading),
        _table(
            ["Asset", "Exception"],
            [[row["asset"].asset_tag, row["exception"]] for row in _custody_exception_rows(report)],
            [1.25 * inch, 6.4 * inch],
        ),
    ]

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=landscape(letter),
        leftMargin=0.4 * inch,
        rightMargin=0.4 * inch,
        topMargin=0.45 * inch,
        bottomMargin=0.45 * inch,
        title="AssetTrack Custody Accountability",
        author="AssetTrack",
    )
    doc.build(story, onFirstPage=_page_number, onLaterPages=_page_number)
    return buffer.getvalue()


def _load_admin_human_report_data(resolved_db_path: Path, *, include_retired_assets: bool = True) -> dict:
    recent_events_limit = 10
    conn = sqlite3.connect(f"file:{resolved_db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    try:
        asset_summary_row = conn.execute(
            """
            SELECT
                COUNT(*) AS total_assets,
                SUM(CASE WHEN COALESCE(location_type, '') NOT IN ('DISPOSED', 'RETIRED') THEN 1 ELSE 0 END) AS active_assets,
                SUM(CASE WHEN location_type = 'STORAGE' THEN 1 ELSE 0 END) AS storage_assets,
                SUM(CASE WHEN location_type = 'IN_CUSTODY' THEN 1 ELSE 0 END) AS in_custody_assets,
                SUM(CASE WHEN location_type IN ('DISPOSED', 'RETIRED') THEN 1 ELSE 0 END) AS disposed_assets
            FROM assets;
            """
        ).fetchone()

        asset_where = ""
        if not include_retired_assets:
            asset_where = "WHERE COALESCE(a.location_type, '') NOT IN ('DISPOSED', 'RETIRED')"

        asset_order_by = "a.asset_tag COLLATE NOCASE ASC, a.id ASC"
        if include_retired_assets:
            asset_order_by = (
                "CASE WHEN COALESCE(a.location_type, '') IN ('DISPOSED', 'RETIRED') THEN 1 ELSE 0 END ASC, "
                "a.asset_tag COLLATE NOCASE ASC, a.id ASC"
            )

        assets = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT
                    a.asset_tag,
                    COALESCE(a.equipment_type, '') AS equipment_type,
                    COALESCE(a.manufacturer, '') AS manufacturer,
                    COALESCE(a.model, '') AS model,
                    COALESCE(a.location_type, '') AS location_type,
                    h.id AS holder_detail_id,
                    COALESCE(h.name, '') AS holder_name,
                    COALESCE(h.organization, '') AS holder_organization,
                    COALESCE(s.case_name, '') AS home_case_name,
                    s.slot_position AS home_slot_position,
                    COALESCE(s.case_name || ' / ' || s.slot_position, '') AS home_slot
                FROM assets a
                LEFT JOIN holders h
                  ON h.id = a.current_holder_id
                LEFT JOIN slots s
                  ON s.id = a.home_slot_id
                {asset_where}
                ORDER BY {asset_order_by};
                """
            ).fetchall()
        ]

        holders = [
            dict(row)
            for row in conn.execute(
                """
                SELECT
                    h.id,
                    h.holder_type,
                    h.name,
                    COALESCE(h.organization, '') AS organization,
                    COALESCE(h.identifier, '') AS identifier,
                    COUNT(a.id) AS assets_in_custody
                FROM holders h
                LEFT JOIN assets a
                  ON a.current_holder_id = h.id
                 AND a.location_type = 'IN_CUSTODY'
                GROUP BY h.id, h.holder_type, h.name, h.organization, h.identifier
                ORDER BY h.name COLLATE NOCASE ASC, h.id ASC;
                """
            ).fetchall()
        ]

        organizations = [
            dict(row)
            for row in conn.execute(
                """
                SELECT
                    o.id,
                    o.name,
                    COUNT(DISTINCT ob.building_id) AS building_count
                FROM organizations o
                LEFT JOIN organization_buildings ob
                  ON ob.organization_id = o.id
                GROUP BY o.id, o.name
                ORDER BY o.name COLLATE NOCASE ASC, o.id ASC;
                """
            ).fetchall()
        ]

        organization_building_mappings = [
            dict(row)
            for row in conn.execute(
                """
                SELECT
                    o.name AS organization_name,
                    b.name AS building_name
                FROM organization_buildings ob
                JOIN organizations o
                  ON o.id = ob.organization_id
                JOIN buildings b
                  ON b.id = ob.building_id
                ORDER BY o.name COLLATE NOCASE ASC, b.name COLLATE NOCASE ASC;
                """
            ).fetchall()
        ]

        current_custody = [
            dict(row)
            for row in conn.execute(
                """
                SELECT
                    h.id AS holder_detail_id,
                    h.name AS holder_name,
                    COALESCE(h.organization, '') AS organization,
                    a.asset_tag,
                    COALESCE(a.equipment_type, '') AS equipment_type,
                    COALESCE(a.building_room, '') AS current_location
                FROM assets a
                JOIN holders h
                  ON h.id = a.current_holder_id
                WHERE a.location_type = 'IN_CUSTODY'
                ORDER BY h.name COLLATE NOCASE ASC, a.asset_tag COLLATE NOCASE ASC, a.id ASC;
                """
            ).fetchall()
        ]

        recent_active_events = [
            {
                **dict(row),
                "event_date_display": _report_event_display_timestamp(row["event_date"]),
            }
            for row in conn.execute(
                f"""
                SELECT
                    e.id,
                    e.event_date,
                    e.asset_tag,
                    e.event_type,
                    h.id AS holder_detail_id,
                    COALESCE(h.name, '') AS holder_name,
                    COALESCE(h.organization, '') AS holder_organization
                FROM asset_events e
                LEFT JOIN holders h
                  ON h.id = e.holder_id
                WHERE {ACTIVE_EVENTS_WHERE.replace("id NOT IN", "e.id NOT IN", 1)}
                ORDER BY e.id DESC
                LIMIT ?;
                """
                ,
                (recent_events_limit,),
            ).fetchall()
        ]

        cases = [
            dict(row)
            for row in conn.execute(
                """
                SELECT
                    s.case_name,
                    COALESCE(cm.case_size, '') AS case_size,
                    COUNT(*) AS total_slots,
                    COUNT(DISTINCT so.slot_id) AS occupied_slots
                FROM slots s
                LEFT JOIN slot_occupancy so
                  ON so.slot_id = s.id
                LEFT JOIN case_metadata cm
                  ON UPPER(cm.case_name) = UPPER(s.case_name)
                GROUP BY s.case_name
                ORDER BY s.case_name COLLATE NOCASE ASC;
                """
            ).fetchall()
        ]

        return {
            "asset_summary": {
                "total_assets": int(
                    asset_summary_row["total_assets" if include_retired_assets else "active_assets"] or 0
                ),
                "active_assets": int(asset_summary_row["active_assets"] or 0),
                "storage_assets": int(asset_summary_row["storage_assets"] or 0),
                "in_custody_assets": int(asset_summary_row["in_custody_assets"] or 0),
                "disposed_assets": int(asset_summary_row["disposed_assets"] or 0),
            },
            "assets": assets,
            "holders": holders,
            "organizations": organizations,
            "organization_building_mappings": organization_building_mappings,
            "current_custody": current_custody,
            "recent_active_events": recent_active_events,
            "cases": cases,
        }
    finally:
        conn.close()


def _build_admin_human_report_pdf(report_data: dict, db_path: str) -> bytes:
    styles = getSampleStyleSheet()
    body = styles["BodyText"]
    heading = styles["Heading1"]
    section_heading = styles["Heading2"]
    title = styles["Title"]

    def _p(value: object) -> Paragraph:
        text = str(value or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return Paragraph(text, body)

    def _render_table(headers: list[str], rows: list[list[object]], column_widths: list[float]) -> Table:
        data = [[Paragraph(f"<b>{header}</b>", body) for header in headers]]
        for row in rows:
            data.append([_p(value) for value in row])

        table = Table(data, colWidths=column_widths, repeatRows=1)
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dbe7f3")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.black),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#c8d2dc")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 6),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ]
            )
        )
        return table

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        leftMargin=0.5 * inch,
        rightMargin=0.5 * inch,
        topMargin=0.5 * inch,
        bottomMargin=0.5 * inch,
    )

    story: list[object] = [
        Paragraph("Admin Human-Readable Report", title),
        Spacer(1, 0.15 * inch),
        Paragraph("Read-only report. The raw SQLite database backup remains authoritative.", body),
        Paragraph(f"Database path: {db_path}", body),
        Paragraph("Recent events section: recent active events only.", body),
        Spacer(1, 0.2 * inch),
        Paragraph("Assets", heading),
        Spacer(1, 0.08 * inch),
        Paragraph(
            (
                f"Total assets: {report_data['asset_summary']['total_assets']} | "
                f"In storage: {report_data['asset_summary']['storage_assets']} | "
                f"In custody: {report_data['asset_summary']['in_custody_assets']} | "
                f"Disposed: {report_data['asset_summary']['disposed_assets']}"
            ),
            body,
        ),
        Spacer(1, 0.1 * inch),
        _render_table(
            ["Asset Tag", "Type", "Make / Model", "Location Type", "Current Holder", "Home Slot"],
            [
                [
                    row["asset_tag"],
                    row["equipment_type"],
                    f"{row['manufacturer']}{' / ' + row['model'] if row['model'] else ''}",
                    row["location_type"],
                    (
                        f"{row['holder_name']} ({row['holder_organization']})"
                        if row["holder_name"] and row["holder_organization"] and row["holder_organization"] != row["holder_name"]
                        else row["holder_name"]
                    ),
                    row["home_slot"],
                ]
                for row in report_data["assets"]
            ]
            or [["No assets found.", "", "", "", "", ""]],
            [1.1 * inch, 0.8 * inch, 1.4 * inch, 1.1 * inch, 1.6 * inch, 1.0 * inch],
        ),
        Spacer(1, 0.2 * inch),
        Paragraph("Holders", section_heading),
        Spacer(1, 0.08 * inch),
        _render_table(
            ["ID", "Type", "Name", "Organization", "Identifier", "Assets In Custody"],
            [
                [
                    row["id"],
                    row["holder_type"],
                    row["name"],
                    row["organization"],
                    row["identifier"],
                    row["assets_in_custody"],
                ]
                for row in report_data["holders"]
            ]
            or [["No holders found.", "", "", "", "", ""]],
            [0.5 * inch, 0.9 * inch, 1.6 * inch, 1.5 * inch, 1.0 * inch, 0.9 * inch],
        ),
        Spacer(1, 0.2 * inch),
        Paragraph("Organizations and Building Access", section_heading),
        Spacer(1, 0.08 * inch),
        _render_table(
            ["Organization", "Mapped Buildings"],
            [[row["name"], row["building_count"]] for row in report_data["organizations"]]
            or [["No organizations found.", ""]],
            [4.5 * inch, 2.0 * inch],
        ),
        Spacer(1, 0.08 * inch),
        _render_table(
            ["Organization", "Building"],
            [
                [row["organization_name"], row["building_name"]]
                for row in report_data["organization_building_mappings"]
            ]
            or [["No organization-to-building mappings found.", ""]],
            [3.5 * inch, 3.0 * inch],
        ),
        Spacer(1, 0.2 * inch),
        Paragraph("Current Custody", section_heading),
        Spacer(1, 0.08 * inch),
        _render_table(
            ["Holder", "Organization", "Asset Tag", "Type", "Current Location"],
            [
                [
                    row["holder_name"],
                    row["organization"],
                    row["asset_tag"],
                    row["equipment_type"],
                    row["current_location"],
                ]
                for row in report_data["current_custody"]
            ]
            or [["No assets are currently in custody.", "", "", "", ""]],
            [1.5 * inch, 1.5 * inch, 1.1 * inch, 0.8 * inch, 2.0 * inch],
        ),
        Spacer(1, 0.2 * inch),
        Paragraph("Recent Active Events", section_heading),
        Spacer(1, 0.08 * inch),
        _render_table(
            ["ID", "When", "Asset Tag", "Event Type", "Holder"],
            [
                [
                    row["id"],
                    row["event_date"],
                    row["asset_tag"],
                    row["event_type"],
                    (
                        f"{row['holder_name']} ({row['holder_organization']})"
                        if row["holder_name"] and row["holder_organization"] and row["holder_organization"] != row["holder_name"]
                        else row["holder_name"]
                    ),
                ]
                for row in report_data["recent_active_events"]
            ]
            or [["No active events found.", "", "", "", ""]],
            [0.5 * inch, 1.8 * inch, 1.1 * inch, 1.0 * inch, 2.6 * inch],
        ),
        Spacer(1, 0.2 * inch),
        Paragraph("Location and Case Data", section_heading),
        Spacer(1, 0.08 * inch),
        _render_table(
            ["Case", "Case Size", "Total Slots", "Occupied Slots"],
            [
                [row["case_name"], row["case_size"] or "", row["total_slots"], row["occupied_slots"]]
                for row in report_data["cases"]
            ]
            or [["No case or slot data found.", "", "", ""]],
            [2.5 * inch, 2.0 * inch, 1.0 * inch, 1.0 * inch],
        ),
    ]

    doc.build(story)
    return buffer.getvalue()


def _case_inventory_case_options(conn: sqlite3.Connection) -> list[str]:
    return [str(row["case_name"]) for row in list_case_summaries(conn)]


def _selected_case_inventory_name() -> str:
    typed_case = str(request.args.get("case_name") or "").strip()
    selected_case = str(request.args.get("case_select") or "").strip()
    return typed_case or selected_case


def _build_case_inventory_pdf(inventory: dict, generated_at: datetime) -> bytes:
    styles = getSampleStyleSheet()
    body = styles["BodyText"]
    title = styles["Title"]
    heading = styles["Heading2"]

    def _p(value: object) -> Paragraph:
        text = str(value or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return Paragraph(text, body)

    data = [[Paragraph(f"<b>{header}</b>", body) for header in ["Asset Tag", "Type", "Description / Model", "Slot"]]]
    for asset in inventory["assets"]:
        make_model = str(asset["manufacturer"] or "")
        model = str(asset["model"] or "")
        if model:
            make_model = f"{make_model} / {model}" if make_model else model
        data.append(
            [
                _p(asset["asset_tag"]),
                _p(equipment_type_label(asset["equipment_type"])),
                _p(make_model),
                _p(f"Slot {asset['slot_position']}"),
            ]
        )
    if not inventory["assets"]:
        data.append([_p("No assets currently in this case."), _p(""), _p(""), _p("")])

    table = Table(
        data,
        colWidths=[1.25 * inch, 1.0 * inch, 3.3 * inch, 1.0 * inch],
        repeatRows=1,
    )
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dbe7f3")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.black),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#c8d2dc")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        leftMargin=0.5 * inch,
        rightMargin=0.5 * inch,
        topMargin=0.5 * inch,
        bottomMargin=0.5 * inch,
    )
    story: list[object] = [
        Paragraph("Case Inventory", title),
        Spacer(1, 0.15 * inch),
        Paragraph(f"Case number: {inventory['case_name']}", heading),
        Paragraph(f"Case Size: {inventory['case_size'] or 'Not recorded'}", body),
        Paragraph(f"Generated: {generated_at.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}", body),
        Paragraph(f"Asset count: {inventory['asset_count']}", body),
        Spacer(1, 0.15 * inch),
        table,
    ]
    doc.build(story)
    return buffer.getvalue()


@app.route("/admin/reference-data", methods=["GET", "POST"])
@require_login
@require_role("admin")
def admin_reference_data():
    error_message: str | None = None

    if request.method == "POST":
        action = (request.form.get("action") or "").strip().lower()
        try:
            if action == "create_organization":
                create_organization((request.form.get("organization_name") or "").strip())
                flash("Created organization.", "success")
            elif action == "create_building":
                create_building((request.form.get("building_name") or "").strip())
                flash("Created building.", "success")
            elif action == "update_building_name":
                update_building_name(
                    int((request.form.get("building_id") or "").strip()),
                    (request.form.get("building_name") or "").strip(),
                )
                flash("Updated building name.", "success")
            elif action == "set_building_active":
                updated_building = set_building_active(
                    int((request.form.get("building_id") or "").strip()),
                    _is_truthy(request.form.get("is_active")),
                )
                status_label = "Reactivated" if int(updated_building["is_active"]) == 1 else "Deactivated"
                flash(f"{status_label} building.", "success")
            elif action == "map_organization_building":
                create_organization_building_mapping(
                    int((request.form.get("organization_id") or "").strip()),
                    int((request.form.get("building_id") or "").strip()),
                )
                flash("Created organization to building mapping.", "success")
            else:
                error_message = "Unknown action."
        except ValueError as e:
            error_message = str(e)

    return render_template(
        "admin_reference_data.html",
        organizations=list_organizations(),
        buildings=list_buildings(),
        mapping_buildings=list_buildings(active_only=True),
        mappings=list_organization_building_mappings(),
        error_message=error_message,
    )


@app.get("/admin/db/export")
@require_login
@require_role("admin")
def admin_db_export():
    resolved_db_path = _resolved_runtime_db_path()
    if not resolved_db_path.exists() or not resolved_db_path.is_file():
        return "Database file not found.", 404

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    download_name = f"assettrack-backup-{timestamp}.db"
    return send_file(
        resolved_db_path,
        as_attachment=True,
        download_name=download_name,
        mimetype="application/octet-stream",
        conditional=False,
    )


@app.route("/admin/db/restore", methods=["GET", "POST"])
@require_login
@require_role("admin")
def admin_db_restore():
    user = current_user()
    resolved_db_path = _resolved_runtime_db_path()
    rollback_path = rollback_artifact_path_for(resolved_db_path)
    recovery_state_path = recovery_state_path_for(resolved_db_path)
    error_message: str | None = None
    success_message: str | None = None
    restore_result: dict[str, str] | None = None
    validation_summary: dict[str, object] | None = None
    status_code = 200

    pending_restore = _load_pending_db_restore()
    if pending_restore is not None and int(pending_restore.get("validated_by_user_id") or 0) == int(user["id"]):
        validation_summary = dict(pending_restore.get("validation_summary") or {})
    elif pending_restore is not None:
        _clear_pending_db_restore()
        pending_restore = None

    if request.method == "POST":
        if not _consume_admin_route_rate_limit_slot():
            error_message = "Too many requests. Wait and try again."
            status_code = 429
        else:
            action = str(request.form.get("action") or "validate").strip().lower()
            if action == "cancel_validation":
                _clear_pending_db_restore()
                validation_summary = None
                pending_restore = None
                success_message = "Pending restore validation was cleared."
            elif action == "confirm_restore":
                pending_restore = _load_pending_db_restore()
                if pending_restore is None:
                    error_message = "Validate a SQLite backup before replacing the live database."
                    status_code = 400
                elif int(pending_restore.get("validated_by_user_id") or 0) != int(user["id"]):
                    _clear_pending_db_restore()
                    error_message = "Pending restore validation does not match the current admin session."
                    status_code = 403
                else:
                    admin_password = str(request.form.get("admin_password") or "")
                    if not verify_password(user, admin_password):
                        validation_summary = dict(pending_restore.get("validation_summary") or {})
                        error_message = "Admin password is incorrect."
                        status_code = 403
                    else:
                        temp_path = Path(str(pending_restore.get("temp_path") or "")).expanduser().resolve()
                        source_filename = str(pending_restore.get("source_filename") or "").strip()
                        try:
                            restore_result = restore_database(
                                uploaded_db_path=temp_path,
                                live_db_path=resolved_db_path,
                                source_filename=source_filename,
                            )
                            success_message = (
                                "Database restore complete. Recovery mode is active and the previous database copy was preserved."
                            )
                            _clear_pending_db_restore()
                            pending_restore = None
                            validation_summary = None
                        except RestoreValidationError as exc:
                            validation_summary = dict(pending_restore.get("validation_summary") or {})
                            error_message = str(exc)
                            status_code = 400
                        except RestoreOperationError as exc:
                            validation_summary = dict(pending_restore.get("validation_summary") or {})
                            error_message = str(exc)
                            status_code = 500
                        except RestoreError as exc:
                            validation_summary = dict(pending_restore.get("validation_summary") or {})
                            error_message = str(exc)
                            status_code = 500
            else:
                upload = request.files.get("db_file")
                filename = str((upload.filename if upload is not None else "") or "").strip()

                if upload is None or not filename:
                    error_message = "Choose a SQLite database file to validate."
                    status_code = 400
                else:
                    suffix = Path(filename).suffix or ".db"
                    temp_path: Path | None = None
                    try:
                        _clear_pending_db_restore()
                        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as handle:
                            upload.save(handle)
                            temp_path = Path(handle.name)

                        validation_summary = {
                            "source_filename": filename,
                            "live_db_path": str(resolved_db_path),
                            "rollback_db_path": str(rollback_path),
                            **inspect_uploaded_database(temp_path),
                        }
                        session[PENDING_DB_RESTORE_SESSION_KEY] = {
                            "temp_path": str(temp_path),
                            "source_filename": filename,
                            "validated_by_user_id": int(user["id"]),
                            "validation_summary": validation_summary,
                        }
                        pending_restore = _load_pending_db_restore()
                    except RestoreValidationError as exc:
                        error_message = str(exc)
                        status_code = 400
                        if temp_path is not None:
                            temp_path.unlink(missing_ok=True)
                    except RestoreError as exc:
                        error_message = str(exc)
                        status_code = 500
                        if temp_path is not None:
                            temp_path.unlink(missing_ok=True)

    return (
        render_template(
            "admin_db_restore.html",
            db_path=str(resolved_db_path),
            rollback_path=str(rollback_path),
            recovery_state_path=str(recovery_state_path),
            error_message=error_message,
            success_message=success_message,
            restore_result=restore_result,
            validation_summary=validation_summary,
        ),
        status_code,
    )



def _inventory_reconciliation_view(result, conn: sqlite3.Connection) -> dict[str, object]:
    counts = result.summary_counts()
    discrepancy_count = (
        counts["government_only_assets"]
        + counts["assettrack_only_active_assets"]
        + counts["identity_conflicts"]
        + counts["ambiguous_normalized_tags"]
        + counts["duplicate_serial_warnings"]
        + counts["duplicate_mac_warnings"]
        + counts["retired_disposed_tag_matches"]
        + counts["retired_disposed_assettrack_only"]
    )
    discrepancies = active_discrepancies(result)
    latest_by_key = latest_reconciliation_dispositions(conn, tuple(item.key for item in discrepancies))
    discrepancy_rows = []
    reviewed_count = 0
    for discrepancy in discrepancies:
        latest = latest_by_key.get(discrepancy.key)
        is_reviewed = bool(latest and latest.is_reviewed)
        if is_reviewed:
            reviewed_count += 1
        discrepancy_rows.append(
            {
                "key": discrepancy.key,
                "category": discrepancy.category,
                "label": discrepancy.label,
                "normalized_asset_key": discrepancy.normalized_asset_key,
                "snapshot": discrepancy.snapshot,
                "snapshot_json": discrepancy.snapshot_json,
                "latest_disposition": latest,
                "is_reviewed": is_reviewed,
            }
        )
    if discrepancy_count == 0:
        reconciliation_state = "CLEAN RECONCILIATION"
    elif reviewed_count == discrepancy_count:
        reconciliation_state = "RECONCILIATION COMPLETE"
    else:
        reconciliation_state = "RECONCILIATION REQUIRES ATTENTION"
    return {
        "counts": counts,
        "matched": counts["exact_or_normalized_tag_matches"],
        "discrepancies": discrepancy_count,
        "clean": discrepancy_count == 0,
        "reconciliation_state": reconciliation_state,
        "reviewed_discrepancies": reviewed_count,
        "active_discrepancies": discrepancy_rows,
        "government_only": result.government_only,
        "assettrack_only_active": result.assettrack_only_active,
        "identity_conflicts": result.identity_conflicts,
        "ambiguous_government_tags": result.ambiguous_government_tags,
        "ambiguous_assettrack_tags": result.ambiguous_assettrack_tags,
        "duplicate_serial_warnings": result.duplicate_serial_warnings,
        "duplicate_mac_warnings": result.duplicate_mac_warnings,
        "terminal_matches": result.terminal_matches,
        "terminal_assettrack_only": result.terminal_assettrack_only,
    }


@app.route("/report/inventory-reconciliation", methods=["GET", "POST"])
@require_login
def inventory_reconciliation():
    result: dict[str, object] | None = None
    error_message: str | None = None
    status_code = 200

    if request.method == "POST":
        action = (request.form.get("action") or "analyze").strip().lower()
        if action == "save_disposition":
            note = str(request.form.get("disposition_note") or "").strip()
            reviewed = request.form.get("is_reviewed") == "1"
            submitted_key = str(request.form.get("discrepancy_key") or "").strip()
            snapshot_json = str(request.form.get("discrepancy_snapshot_json") or "").strip()
            try:
                snapshot = json.loads(snapshot_json)
                if not isinstance(snapshot, dict):
                    raise ValueError("Disposition evidence is invalid.")
                expected_key = discrepancy_key_from_snapshot(snapshot)
                if not submitted_key or submitted_key != expected_key:
                    raise ValueError("Disposition evidence does not match the discrepancy key.")
                if not note:
                    raise ValueError("Enter a disposition note before saving Reviewed / Dispositioned.")
                user = current_user()
                if user is None:
                    return abort(403)
                conn = get_connection()
                try:
                    conn.execute("BEGIN IMMEDIATE;")
                    insert_reconciliation_disposition_event(
                        conn,
                        created_at=datetime.now(timezone.utc).isoformat(),
                        actor_user_id=int(user["id"]),
                        actor_username=str(user.get("username") or ""),
                        discrepancy_key=submitted_key,
                        discrepancy_category=str(snapshot.get("category") or ""),
                        normalized_asset_key=str(snapshot.get("normalized_asset_key") or ""),
                        discrepancy_snapshot_json=snapshot_json,
                        disposition_note=note,
                        is_reviewed=reviewed,
                    )
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
                finally:
                    conn.close()
                flash(
                    "Disposition saved. Re-run the same inventory analysis to see the latest disposition on active discrepancies.",
                    "success",
                )
                return redirect(url_for("inventory_reconciliation"))
            except (ValueError, json.JSONDecodeError, sqlite3.Error) as exc:
                error_message = str(exc)
                status_code = 400
        elif action != "analyze":
            error_message = "Unknown inventory reconciliation action."
            status_code = 400
        if error_message is not None:
            return (
                render_template(
                    "inventory_reconciliation.html",
                    result=result,
                    error_message=error_message,
                ),
                status_code,
            )

        upload = request.files.get("inventory_file")
        filename = str((upload.filename if upload is not None else "") or "").strip()
        if upload is None or not filename:
            error_message = "Choose a .csv or .xlsx inventory file to analyze."
            status_code = 400
        else:
            suffix = Path(filename).suffix.lower()
            tempfile_suffix = ASSET_IMPORT_TEMPFILE_SUFFIXES.get(suffix)
            if tempfile_suffix is None:
                error_message = "Unsupported file type. Upload a .csv or .xlsx file."
                status_code = 400
            else:
                temp_path: Path | None = None
                conn: sqlite3.Connection | None = None
                try:
                    with tempfile.NamedTemporaryFile(delete=False, suffix=tempfile_suffix) as handle:
                        upload.save(handle)
                        temp_path = Path(handle.name)

                    conn = get_connection()
                    result = _inventory_reconciliation_view(reconcile_inventory(conn, temp_path), conn)
                    result["filename"] = filename
                except ValueError as exc:
                    error_message = str(exc)
                    status_code = 400
                except sqlite3.Error as exc:
                    error_message = f"Could not read AssetTrack database: {exc}"
                    status_code = 500
                finally:
                    if conn is not None:
                        conn.close()
                    if temp_path is not None:
                        temp_path.unlink(missing_ok=True)

    return (
        render_template(
            "inventory_reconciliation.html",
            result=result,
            error_message=error_message,
        ),
        status_code,
    )

@app.get("/report")
@require_login
def human_report():
    resolved_db_path = _resolved_runtime_db_path()
    report_error: str | None = None
    include_retired_assets = request.args.get("include_retired") == "1"
    report_data = {
        "asset_summary": {
            "total_assets": 0,
            "active_assets": 0,
            "storage_assets": 0,
            "in_custody_assets": 0,
            "disposed_assets": 0,
        },
        "assets": [],
        "holders": [],
        "organizations": [],
        "organization_building_mappings": [],
        "current_custody": [],
        "recent_active_events": [],
        "cases": [],
    }

    try:
        report_data = _load_admin_human_report_data(
            resolved_db_path,
            include_retired_assets=include_retired_assets,
        )
    except sqlite3.Error as exc:
        report_error = f"Could not read report data: {exc}"

    return render_template(
        "report_readonly.html",
        report_error=report_error,
        include_retired_assets=include_retired_assets,
        **report_data,
    )


@app.get("/report/custody-accountability")
@require_login
def custody_accountability_preview():
    generated_at = datetime.now(timezone.utc)
    try:
        report = _load_custody_accountability_report(_resolved_runtime_db_path(), generated_at)
    except sqlite3.Error as exc:
        return f"Could not read custody accountability report: {exc}", 500

    return render_template(
        "custody_accountability.html",
        report=report,
        generated_at=generated_at,
        outstanding_rows=_custody_outstanding_rows(report),
        interval_rows=_custody_interval_rows(report),
        exception_rows=_custody_exception_rows(report),
        duration_label=_duration_label,
        timestamp_label=_timestamp_label,
        holder_summary_label=_holder_summary_label,
        holder_identifier_label=_holder_identifier_label,
        accountability_state_label=_accountability_state_label,
    )


@app.get("/report/custody-accountability/pdf")
@require_login
def custody_accountability_pdf():
    generated_at = datetime.now(timezone.utc)
    try:
        report = _load_custody_accountability_report(_resolved_runtime_db_path(), generated_at)
        pdf_bytes = _build_custody_accountability_pdf(report)
    except sqlite3.Error as exc:
        return f"Could not build custody accountability PDF: {exc}", 500

    download_name = f"assettrack-custody-accountability-{generated_at.strftime('%Y%m%d-%H%M%S')}.pdf"
    return send_file(
        BytesIO(pdf_bytes),
        as_attachment=True,
        download_name=download_name,
        mimetype="application/pdf",
        conditional=False,
    )


@app.get("/report/custody-analytics")
@require_login
def custody_analytics_dashboard():
    default_selection = SUPPORTED_ANALYTICS[0]
    measure = str(request.args.get("measure") or default_selection.measure).strip()
    grouping = str(request.args.get("grouping") or default_selection.grouping).strip()
    chart_types = _analytics_chart_types(measure, grouping)
    chart_type = str(request.args.get("chart_type") or (chart_types[0][0] if chart_types else "")).strip()
    should_generate = request.args.get("generate") == "1"
    error_message: str | None = None
    status_code = 200
    dataset: AnalyticsDataset | None = None
    chart_rows: list[dict[str, object]] = []

    if not chart_types:
        error_message = "Unsupported Measure and Group By combination."
        status_code = 400
    elif chart_type not in {value for value, _label in chart_types}:
        error_message = "Unsupported chart type for the selected analytics dataset."
        status_code = 400
    elif should_generate:
        generated_at = datetime.now(timezone.utc)
        try:
            report = _load_custody_accountability_report(_resolved_runtime_db_path(), generated_at)
            dataset = build_analytics_dataset(report, measure=measure, grouping=grouping)
            chart_rows = _analytics_chart_rows(dataset)
        except ValueError as exc:
            error_message = str(exc)
            status_code = 400
        except sqlite3.Error as exc:
            error_message = f"Could not read custody analytics data: {exc}"
            status_code = 500

    return (
        render_template(
            "custody_analytics.html",
            supported_pairs=_analytics_supported_pairs(),
            selector_mapping=_analytics_selector_mapping(),
            measure_options=_analytics_measure_options(),
            grouping_options=_analytics_grouping_options(),
            chart_types=chart_types,
            selected_measure=measure,
            selected_grouping=grouping,
            selected_chart_type=chart_type,
            dataset=dataset,
            chart_rows=chart_rows,
            line_points=_analytics_line_points(chart_rows),
            should_generate=should_generate,
            error_message=error_message,
        ),
        status_code,
    )


@app.get("/report/case-inventory")
@require_login
def case_inventory():
    conn = get_connection()
    try:
        case_options = _case_inventory_case_options(conn)
    finally:
        conn.close()

    return render_template(
        "case_inventory.html",
        case_options=case_options,
        selected_case="",
        inventory=None,
        generated_at=None,
        not_found_case="",
    )


@app.get("/report/case-inventory/preview")
@require_login
def case_inventory_preview():
    selected_case = _selected_case_inventory_name()
    if not selected_case:
        return redirect(url_for("case_inventory"))

    generated_at = datetime.now(timezone.utc)
    conn = get_connection()
    try:
        case_options = _case_inventory_case_options(conn)
        inventory = get_case_inventory(conn, selected_case)
    finally:
        conn.close()

    status_code = 200 if inventory is not None else 404
    return (
        render_template(
            "case_inventory.html",
            case_options=case_options,
            selected_case=selected_case,
            inventory=inventory,
            generated_at=generated_at,
            not_found_case="" if inventory is not None else selected_case,
        ),
        status_code,
    )


@app.get("/report/case-inventory/pdf")
@require_login
def case_inventory_pdf():
    selected_case = _selected_case_inventory_name()
    if not selected_case:
        abort(404)

    generated_at = datetime.now(timezone.utc)
    conn = get_connection()
    try:
        inventory = get_case_inventory(conn, selected_case)
    finally:
        conn.close()

    if inventory is None:
        abort(404)

    pdf_bytes = _build_case_inventory_pdf(inventory, generated_at)
    filename_case = re.sub(r"[^A-Za-z0-9_-]+", "-", str(inventory["case_name"])).strip("-") or "case"
    download_name = f"assettrack-case-inventory-{filename_case}-{generated_at.strftime('%Y%m%d-%H%M%S')}.pdf"
    return send_file(
        BytesIO(pdf_bytes),
        as_attachment=True,
        download_name=download_name,
        mimetype="application/pdf",
        conditional=False,
    )


@app.get("/admin/report")
@require_login
@require_role("admin")
def admin_human_report():
    resolved_db_path = _resolved_runtime_db_path()
    report_error: str | None = None
    report_data = {
        "asset_summary": {
            "total_assets": 0,
            "active_assets": 0,
            "storage_assets": 0,
            "in_custody_assets": 0,
            "disposed_assets": 0,
        },
        "assets": [],
        "holders": [],
        "organizations": [],
        "organization_building_mappings": [],
        "current_custody": [],
        "recent_active_events": [],
        "cases": [],
    }

    try:
        report_data = _load_admin_human_report_data(resolved_db_path)
    except sqlite3.Error as exc:
        report_error = f"Could not read admin report data: {exc}"

    return render_template(
        "admin_human_report.html",
        db_path=str(resolved_db_path),
        report_error=report_error,
        **report_data,
    )


@app.get("/admin/report/pdf")
@require_login
@require_role("admin")
def admin_human_report_pdf():
    resolved_db_path = _resolved_runtime_db_path()
    try:
        report_data = _load_admin_human_report_data(resolved_db_path)
        pdf_bytes = _build_admin_human_report_pdf(report_data, str(resolved_db_path))
    except sqlite3.Error as exc:
        return f"Could not build admin PDF report: {exc}", 500

    download_name = f"assettrack-human-report-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.pdf"
    return send_file(
        BytesIO(pdf_bytes),
        as_attachment=True,
        download_name=download_name,
        mimetype="application/pdf",
        conditional=False,
    )


@app.post("/admin/users/create")
@require_login
@require_role("admin")
def admin_users_create():
    rate_limit_response = _enforce_admin_route_rate_limit(html_redirect_endpoint="admin_users")
    if rate_limit_response is not None:
        return rate_limit_response

    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    role = (request.form.get("role") or "").strip().lower()
    active = True if request.form.get("active") is None else _is_truthy(request.form.get("active"))

    try:
        create_user(username=username, password=password, role=role, active=active)
    except ValueError as e:
        flash(str(e), "error")
    except sqlite3.IntegrityError:
        flash("Username already exists.", "error")
    else:
        flash(f"Created user: {username}", "success")

    return redirect(url_for("admin_users"))


@app.post("/admin/users/<int:user_id>/toggle-active")
@require_login
@require_role("admin")
def admin_users_toggle_active(user_id: int):
    rate_limit_response = _enforce_admin_route_rate_limit(html_redirect_endpoint="admin_users")
    if rate_limit_response is not None:
        return rate_limit_response

    target = get_user_by_id(user_id)
    if target is None:
        flash("User not found.", "error")
        return redirect(url_for("admin_users"))

    requested = request.form.get("active")
    next_active = (not bool(int(target.get("active") or 0))) if requested is None else _is_truthy(requested)

    try:
        updated = set_user_active(user_id, next_active)
    except ValueError as e:
        flash(str(e), "error")
    else:
        state = "enabled" if int(updated.get("active") or 0) == 1 else "disabled"
        flash(f"User {updated['username']} is now {state}.", "success")

    return redirect(url_for("admin_users"))


@app.post("/admin/users/<int:user_id>/reset-password")
@require_login
@require_role("admin")
def admin_users_reset_password(user_id: int):
    rate_limit_response = _enforce_admin_route_rate_limit(html_redirect_endpoint="admin_users")
    if rate_limit_response is not None:
        return rate_limit_response

    try:
        reset_result = reset_user_password(user_id)
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("admin_users"))
    else:
        updated = reset_result["user"]
        flash(f"Password reset for {updated['username']}.", "success")
        response = make_response(render_template(
            "admin_users.html",
            **_admin_users_context(
                temporary_password=reset_result["temporary_password"],
                temporary_password_username=updated["username"],
                temporary_password_user_active=int(updated.get("active") or 0) == 1,
            ),
        ))
        response.headers["X-AssetTrack-Sensitive-Reveal"] = "1"
        return response


@app.post("/admin/users/<int:user_id>/set-role")
@require_login
@require_role("admin")
def admin_users_set_role(user_id: int):
    rate_limit_response = _enforce_admin_route_rate_limit(html_redirect_endpoint="admin_users")
    if rate_limit_response is not None:
        return rate_limit_response

    role = (request.form.get("role") or "").strip().lower()

    try:
        updated = set_user_role(user_id, role)
    except ValueError as e:
        flash(str(e), "error")
    else:
        flash(f"Updated role for {updated['username']} to {updated['role']}.", "success")

    return redirect(url_for("admin_users"))


@app.route("/admin/assets/new", methods=["GET", "POST"])
@require_login
@require_role("admin")
def admin_new_asset():
    guard_result = _require_admin_for_route()
    if guard_result:
        return guard_result

    form_state = {
        "asset_tag": "",
        "serial_number": "",
        "manufacturer": "",
        "equipment_type": "laptop",
        "building": "",
        "room": "",
        "model": "",
        "model_code": "",
        "notes": "",
        "case_name": "",
        "slot_id": "",
    }
    error_message: Optional[str] = None
    conn = get_connection()
    try:
        slot_options = _list_slot_options(conn)
        case_options = _slot_case_options(slot_options)

        if request.method == "POST":
            form_state = {
                "asset_tag": (request.form.get("asset_tag") or "").strip().upper(),
                "serial_number": (request.form.get("serial_number") or "").strip(),
                "manufacturer": (request.form.get("manufacturer") or "").strip(),
                "equipment_type": normalize_equipment_type(request.form.get("equipment_type")) or "laptop",
                "building": (request.form.get("building") or "").strip(),
                "room": (request.form.get("room") or "").strip(),
                "model": (request.form.get("model") or "").strip(),
                "model_code": (request.form.get("model_code") or "").strip(),
                "notes": (request.form.get("notes") or "").strip(),
                "case_name": (request.form.get("case_name") or "").strip().upper(),
                "slot_id": (request.form.get("slot_id") or "").strip(),
            }

            selected_slot, errors = _validate_admin_new_asset_form(conn, form_state)

            if errors:
                error_message = "; ".join(errors)
                return render_template(
                    "admin_new_asset.html",
                    form=form_state,
                    error_message=error_message,
                    equipment_type_options=ASSET_EQUIPMENT_TYPE_OPTIONS,
                    slot_options=slot_options,
                    case_options=case_options,
                )

            try:
                conn.execute("BEGIN;")
                _create_admin_asset_in_tx(
                    conn,
                    asset_tag=form_state["asset_tag"],
                    actor="admin",
                    equipment_type=form_state["equipment_type"],
                    serial_number=form_state["serial_number"],
                    manufacturer=form_state["manufacturer"],
                    building=form_state["building"],
                    room=form_state["room"],
                    model=form_state["model"] or None,
                    model_code=form_state["model_code"] or None,
                    notes=form_state["notes"] or None,
                    assign_case_number=None if selected_slot is None else str(selected_slot["case_name"]),
                    assign_slot_number=None if selected_slot is None else int(selected_slot["slot_position"]),
                )
                conn.commit()
            except ValueError as e:
                conn.rollback()
                error_message = _humanize_admin_asset_create_error(str(e))
                return render_template(
                    "admin_new_asset.html",
                    form=form_state,
                    error_message=error_message,
                    equipment_type_options=ASSET_EQUIPMENT_TYPE_OPTIONS,
                    slot_options=slot_options,
                    case_options=case_options,
                )
            except sqlite3.IntegrityError as e:
                conn.rollback()
                error_message = f"create failed: {e}"
                return render_template(
                    "admin_new_asset.html",
                    form=form_state,
                    error_message=error_message,
                    equipment_type_options=ASSET_EQUIPMENT_TYPE_OPTIONS,
                    slot_options=slot_options,
                    case_options=case_options,
                )
            except Exception:
                conn.rollback()
                raise

            if selected_slot is None:
                flash(f"Created asset {form_state['asset_tag']} as Unslotted.", "success")
                if not session.get("admin_unslotted_asset_warning_shown"):
                    flash("This asset is Unslotted. Storage can be assigned later.", "warning")
                    session["admin_unslotted_asset_warning_shown"] = True
            else:
                flash(f"Created asset {form_state['asset_tag']}.", "success")
            return redirect(url_for("admin_new_asset"))
    finally:
        conn.close()

    return render_template(
        "admin_new_asset.html",
        form=form_state,
        error_message=error_message,
        equipment_type_options=ASSET_EQUIPMENT_TYPE_OPTIONS,
        slot_options=slot_options,
        case_options=case_options,
    )


@app.route("/admin/assets/edit", methods=["GET", "POST"])
@require_login
@require_role("admin")
def admin_edit_asset():
    guard_result = _require_admin_for_route()
    if guard_result:
        return guard_result

    form_state = {
        "lookup_asset_tag": (request.args.get("asset_tag") or "").strip().upper(),
        "asset_tag": "",
        "serial_number": "",
        "manufacturer": "",
        "equipment_type": "",
        "building": "",
        "room": "",
        "model": "",
        "model_code": "",
        "notes": "",
        "case_name": "",
        "slot_id": "",
    }
    asset_view: Optional[dict] = None
    asset_matches: list[dict] = []
    error_message: Optional[str] = None

    conn = get_connection()
    try:
        slot_options = _list_slot_options(conn)
        case_options = _slot_case_options(slot_options)

        if form_state["lookup_asset_tag"]:
            try:
                exact_asset = _find_asset_for_scan_tag(conn, form_state["lookup_asset_tag"])
            except ValueError as exc:
                exact_asset = None
                error_message = str(exc)

            if exact_asset is not None:
                asset_view, blocking_errors = _build_admin_edit_asset_view(conn, form_state["lookup_asset_tag"])
                if asset_view:
                    selected_home_slot = asset_view["home_slot"] or asset_view["current_slot"]
                    form_state.update(
                        {
                            "asset_tag": asset_view["asset_tag"],
                            "serial_number": asset_view["serial_number"],
                            "manufacturer": asset_view["manufacturer"],
                            "equipment_type": asset_view["equipment_type"],
                            "building": asset_view["building"],
                            "room": asset_view["room"],
                            "model": asset_view["model"],
                            "model_code": asset_view["model_code"],
                            "notes": asset_view["notes"],
                            "case_name": "" if selected_home_slot is None else str(selected_home_slot["case_name"]),
                            "slot_id": "" if selected_home_slot is None else str(selected_home_slot["slot_id"]),
                        }
                    )
                elif blocking_errors:
                    error_message = "; ".join(blocking_errors)
            elif error_message is None:
                asset_matches, error_message = _lookup_admin_edit_asset_matches(conn, form_state["lookup_asset_tag"])

        if request.method == "POST":
            action = (request.form.get("action") or "lookup").strip().lower()
            lookup_asset_tag = (request.form.get("lookup_asset_tag") or "").strip().upper()
            form_state["lookup_asset_tag"] = lookup_asset_tag

            if action == "lookup":
                if not lookup_asset_tag:
                    error_message = "asset_tag is required."
                else:
                    try:
                        exact_asset = _find_asset_for_scan_tag(conn, lookup_asset_tag)
                    except ValueError as exc:
                        exact_asset = None
                        error_message = str(exc)

                    if exact_asset is not None:
                        asset_view, blocking_errors = _build_admin_edit_asset_view(conn, lookup_asset_tag)
                        if asset_view:
                            selected_home_slot = asset_view["home_slot"] or asset_view["current_slot"]
                            form_state.update(
                                {
                                    "asset_tag": asset_view["asset_tag"],
                                    "serial_number": asset_view["serial_number"],
                                    "manufacturer": asset_view["manufacturer"],
                                    "equipment_type": asset_view["equipment_type"],
                                    "building": asset_view["building"],
                                    "room": asset_view["room"],
                                    "model": asset_view["model"],
                                    "model_code": asset_view["model_code"],
                                    "notes": asset_view["notes"],
                                    "case_name": "" if selected_home_slot is None else str(selected_home_slot["case_name"]),
                                    "slot_id": "" if selected_home_slot is None else str(selected_home_slot["slot_id"]),
                                }
                            )
                        elif blocking_errors:
                            error_message = "; ".join(blocking_errors)
                    elif error_message is None:
                        asset_matches, error_message = _lookup_admin_edit_asset_matches(conn, lookup_asset_tag)
            elif action == "update":
                form_state.update(
                    {
                        "asset_tag": (request.form.get("asset_tag") or "").strip().upper(),
                        "serial_number": (request.form.get("serial_number") or "").strip(),
                        "manufacturer": (request.form.get("manufacturer") or "").strip(),
                        "equipment_type": (request.form.get("equipment_type") or "").strip(),
                        "building": (request.form.get("building") or "").strip(),
                        "room": (request.form.get("room") or "").strip(),
                        "model": (request.form.get("model") or "").strip(),
                        "model_code": (request.form.get("model_code") or "").strip(),
                        "notes": (request.form.get("notes") or "").strip(),
                        "case_name": (request.form.get("case_name") or "").strip().upper(),
                        "slot_id": (request.form.get("slot_id") or "").strip(),
                    }
                )
                asset_view, blocking_errors = _build_admin_edit_asset_view(conn, lookup_asset_tag or form_state["asset_tag"])
                if asset_view is None:
                    error_message = "; ".join(blocking_errors or ["asset_tag not found"])
                    return render_template(
                        "admin_edit_asset.html",
                        form=form_state,
                        asset=asset_view,
                        asset_matches=asset_matches,
                        error_message=error_message,
                        equipment_type_options=_equipment_type_form_options(form_state["equipment_type"]),
                        slot_options=slot_options,
                        case_options=case_options,
                    )

                errors: list[str] = []
                if not form_state["serial_number"]:
                    errors.append("serial_number is required.")
                if not form_state["equipment_type"]:
                    errors.append("equipment_type is required.")
                elif not _equipment_type_is_allowed(
                    form_state["equipment_type"],
                    allow_current=asset_view["equipment_type"],
                ):
                    errors.append(SUPPORTED_EQUIPMENT_TYPE_MESSAGE)
                selected_slot, slot_errors = _resolve_slot_selection(
                    conn,
                    case_name=form_state["case_name"],
                    slot_id_raw=form_state["slot_id"],
                )
                errors.extend(slot_errors)

                if errors:
                    error_message = "; ".join(errors)
                    return render_template(
                        "admin_edit_asset.html",
                        form=form_state,
                        asset=asset_view,
                        asset_matches=asset_matches,
                        error_message=error_message,
                        equipment_type_options=_equipment_type_form_options(form_state["equipment_type"]),
                        slot_options=slot_options,
                        case_options=case_options,
                    )

                try:
                    conn.execute("BEGIN;")
                    _update_admin_asset_in_tx(
                        conn,
                        asset_id=int(asset_view["id"]),
                        actor="admin",
                        serial_number=form_state["serial_number"],
                        manufacturer=form_state["manufacturer"],
                        equipment_type=form_state["equipment_type"],
                        building=form_state["building"],
                        room=form_state["room"],
                        model=form_state["model"] or None,
                        model_code=form_state["model_code"] or None,
                        notes=form_state["notes"] or None,
                        selected_slot=selected_slot,
                    )
                    conn.commit()
                except ValueError as e:
                    conn.rollback()
                    error_message = str(e)
                    return render_template(
                        "admin_edit_asset.html",
                        form=form_state,
                        asset=asset_view,
                        asset_matches=asset_matches,
                        error_message=error_message,
                        equipment_type_options=_equipment_type_form_options(form_state["equipment_type"]),
                        slot_options=slot_options,
                        case_options=case_options,
                    )
                except sqlite3.IntegrityError as e:
                    conn.rollback()
                    error_message = f"update failed: {e}"
                    return render_template(
                        "admin_edit_asset.html",
                        form=form_state,
                        asset=asset_view,
                        asset_matches=asset_matches,
                        error_message=error_message,
                        equipment_type_options=_equipment_type_form_options(form_state["equipment_type"]),
                        slot_options=slot_options,
                        case_options=case_options,
                    )
                except Exception:
                    conn.rollback()
                    raise

                flash(f"Updated asset {form_state['asset_tag']}.", "success")
                return redirect(url_for("admin_edit_asset", asset_tag=form_state["asset_tag"]))
            elif action == "cleanup":
                target_asset_tag = lookup_asset_tag or (request.form.get("asset_tag") or "").strip().upper()
                asset_view, blocking_errors = _build_admin_edit_asset_view(conn, target_asset_tag)
                if asset_view is None:
                    error_message = "; ".join(blocking_errors or ["asset_tag not found"])
                    return render_template(
                        "admin_edit_asset.html",
                        form=form_state,
                        asset=asset_view,
                        asset_matches=asset_matches,
                        error_message=error_message,
                        equipment_type_options=_equipment_type_form_options(form_state["equipment_type"]),
                        slot_options=slot_options,
                        case_options=case_options,
                    )

                selected_home_slot = asset_view["home_slot"] or asset_view["current_slot"]
                form_state.update(
                    {
                        "asset_tag": asset_view["asset_tag"],
                        "serial_number": asset_view["serial_number"],
                        "manufacturer": asset_view["manufacturer"],
                        "equipment_type": asset_view["equipment_type"],
                        "building": asset_view["building"],
                        "room": asset_view["room"],
                        "model": asset_view["model"],
                        "model_code": asset_view["model_code"],
                        "notes": asset_view["notes"],
                        "case_name": "" if selected_home_slot is None else str(selected_home_slot["case_name"]),
                        "slot_id": "" if selected_home_slot is None else str(selected_home_slot["slot_id"]),
                    }
                )

                try:
                    conn.execute("BEGIN;")
                    asset_row = _find_asset_for_scan_tag(conn, asset_view["asset_tag"])
                    if asset_row is None:
                        raise ValueError("asset_tag not found")

                    cleanup_state = _build_admin_asset_cleanup_state(conn, asset_row)
                    if not cleanup_state["allowed"]:
                        raise ValueError("; ".join(cleanup_state["reasons"]))

                    deleted = conn.execute(
                        "DELETE FROM assets WHERE id = ?;",
                        (int(asset_row["id"]),),
                    )
                    if deleted.rowcount != 1:
                        raise ValueError("Asset could not be removed.")

                    conn.commit()
                except ValueError as e:
                    conn.rollback()
                    error_message = str(e)
                    asset_view, _ = _build_admin_edit_asset_view(conn, target_asset_tag)
                    return render_template(
                        "admin_edit_asset.html",
                        form=form_state,
                        asset=asset_view,
                        asset_matches=asset_matches,
                        error_message=error_message,
                        equipment_type_options=_equipment_type_form_options(form_state["equipment_type"]),
                        slot_options=slot_options,
                        case_options=case_options,
                    )
                except Exception:
                    conn.rollback()
                    raise

                flash(f"Removed junk asset {asset_view['asset_tag']}.", "success")
                return redirect(url_for("admin_edit_asset"))
            else:
                error_message = "Unknown action."
    finally:
        conn.close()

    return render_template(
        "admin_edit_asset.html",
        form=form_state,
        asset=asset_view,
        asset_matches=asset_matches,
        error_message=error_message,
        equipment_type_options=_equipment_type_form_options(form_state["equipment_type"]),
        slot_options=slot_options,
        case_options=case_options,
    )


@app.route("/admin/assets/retire", methods=["GET", "POST"])
@require_login
@require_role("admin")
def admin_retire_asset():
    guard_result = _require_admin_for_route()
    if guard_result:
        return guard_result

    form_state = {
        "asset_tag": "",
        "failure_type": "",
        "notes": "",
        "confirm_physical": False,
        "confirm_in_field": False,
    }
    asset_view: Optional[dict] = None
    asset_matches: list[dict] = []
    error_message: Optional[str] = None

    if request.method == "POST":
        action = (request.form.get("action") or "lookup").strip().lower()
        form_state = {
            "asset_tag": (request.form.get("asset_tag") or "").strip().upper(),
            "failure_type": (request.form.get("failure_type") or "").strip().upper(),
            "notes": (request.form.get("notes") or "").strip(),
            "confirm_physical": _is_truthy(request.form.get("confirm_physical")),
            "confirm_in_field": _is_truthy(request.form.get("confirm_in_field")),
        }

        conn = get_connection()
        try:
            if action == "lookup":
                if not form_state["asset_tag"]:
                    error_message = "asset_tag is required."
                else:
                    try:
                        exact_asset = _find_asset_for_scan_tag(conn, form_state["asset_tag"])
                    except ValueError as exc:
                        exact_asset = None
                        error_message = str(exc)

                    if exact_asset is not None:
                        asset_view, blocking_errors = _build_admin_retire_asset_view(conn, form_state["asset_tag"])
                        if blocking_errors:
                            error_message = "; ".join(blocking_errors)
                    elif error_message is None:
                        asset_matches, error_message = _lookup_retire_asset_matches(conn, form_state["asset_tag"])
            elif action == "retire":
                asset_view, blocking_errors = _build_admin_retire_asset_view(conn, form_state["asset_tag"])
                errors: list[str] = []
                if not form_state["asset_tag"]:
                    errors.append("asset_tag is required.")
                if not form_state["failure_type"]:
                    errors.append("failure_type is required.")
                elif form_state["failure_type"] not in RETIRE_FAILURE_TYPES:
                    errors.append(
                        f"failure_type must be one of: {', '.join(sorted(RETIRE_FAILURE_TYPES))}."
                    )
                if not form_state["notes"]:
                    errors.append("notes is required.")
                if not form_state["confirm_physical"]:
                    errors.append("You must confirm physical reality before retiring.")
                if asset_view and asset_view["location_type"] == "IN_CUSTODY" and not form_state["confirm_in_field"]:
                    errors.append("You must confirm the in-custody asset is not recoverable.")
                if blocking_errors:
                    errors.extend(blocking_errors)
                if errors:
                    error_message = "; ".join(errors)
                    return render_template(
                        "admin_retire_asset.html",
                        form=form_state,
                        asset=asset_view,
                        error_message=error_message,
                        failure_type_options=sorted(RETIRE_FAILURE_TYPES),
                    )

                try:
                    conn.execute("BEGIN;")
                    result = _retire_admin_asset_in_tx(
                        conn,
                        asset_id=int(asset_view["id"]),
                        asset_tag=str(asset_view["asset_tag"]),
                        failure_type=form_state["failure_type"],
                        notes=form_state["notes"],
                        actor="admin",
                    )
                    conn.commit()
                except ValueError as e:
                    conn.rollback()
                    error_message = str(e)
                    return render_template(
                        "admin_retire_asset.html",
                        form=form_state,
                        asset=asset_view,
                        error_message=error_message,
                        failure_type_options=sorted(RETIRE_FAILURE_TYPES),
                    )
                except sqlite3.IntegrityError as e:
                    conn.rollback()
                    error_message = f"retire failed: {e}"
                    return render_template(
                        "admin_retire_asset.html",
                        form=form_state,
                        asset=asset_view,
                        error_message=error_message,
                        failure_type_options=sorted(RETIRE_FAILURE_TYPES),
                    )
                except Exception:
                    conn.rollback()
                    raise

                flash(
                    f"Retired asset {result['asset_tag']} with status {result['to_location_type']}.",
                    "success",
                )
                return redirect(url_for("admin_retire_asset"))
            else:
                error_message = "Unknown action."
        finally:
            conn.close()

    return render_template(
        "admin_retire_asset.html",
        form=form_state,
        asset=asset_view,
        asset_matches=asset_matches,
        error_message=error_message,
        failure_type_options=sorted(RETIRE_FAILURE_TYPES),
    )


@app.route("/admin/assets/replace", methods=["GET", "POST"])
@require_login
@require_role("admin")
def admin_replace_asset():
    guard_result = _require_admin_for_route()
    if guard_result:
        return guard_result

    form_state = {
        "failed_asset_tag": "",
        "failure_type": "",
        "failure_notes": "",
        "replacement_asset_tag": "",
        "replacement_serial_number": "",
        "replacement_manufacturer": "",
        "replacement_equipment_type": "laptop",
        "replacement_model": "",
        "replacement_model_code": "",
        "replacement_notes": "",
        "confirm_retire": False,
        "confirm_slot": False,
    }
    failed_asset_view: Optional[dict] = None
    error_message: Optional[str] = None

    if request.method == "POST":
        action = (request.form.get("action") or "lookup").strip().lower()
        form_state = {
            "failed_asset_tag": (request.form.get("failed_asset_tag") or "").strip().upper(),
            "failure_type": (request.form.get("failure_type") or "").strip().upper(),
            "failure_notes": (request.form.get("failure_notes") or "").strip(),
            "replacement_asset_tag": (request.form.get("replacement_asset_tag") or "").strip().upper(),
            "replacement_serial_number": (request.form.get("replacement_serial_number") or "").strip(),
            "replacement_manufacturer": (request.form.get("replacement_manufacturer") or "").strip(),
            "replacement_equipment_type": normalize_equipment_type(request.form.get("replacement_equipment_type")) or "laptop",
            "replacement_model": (request.form.get("replacement_model") or "").strip(),
            "replacement_model_code": (request.form.get("replacement_model_code") or "").strip(),
            "replacement_notes": (request.form.get("replacement_notes") or "").strip(),
            "confirm_retire": _is_truthy(request.form.get("confirm_retire")),
            "confirm_slot": _is_truthy(request.form.get("confirm_slot")),
        }

        conn = get_connection()
        try:
            failed_asset_view, blocking_errors = _build_admin_replace_asset_view(conn, form_state["failed_asset_tag"])
            if action == "lookup":
                if not form_state["failed_asset_tag"]:
                    error_message = "failed asset_tag is required."
                elif blocking_errors:
                    error_message = "; ".join(blocking_errors)
            elif action == "replace":
                errors: list[str] = []
                if not form_state["failed_asset_tag"]:
                    errors.append("failed asset_tag is required.")
                if not form_state["failure_type"]:
                    errors.append("failure_type is required.")
                elif form_state["failure_type"] not in RETIRE_FAILURE_TYPES:
                    errors.append(
                        f"failure_type must be one of: {', '.join(sorted(RETIRE_FAILURE_TYPES))}."
                    )
                if not form_state["failure_notes"]:
                    errors.append("failure notes are required.")
                if not form_state["replacement_asset_tag"]:
                    errors.append("replacement asset_tag is required.")
                if not form_state["replacement_serial_number"]:
                    errors.append("replacement serial_number is required.")
                if not form_state["replacement_equipment_type"]:
                    errors.append("replacement equipment_type is required.")
                elif not is_approved_new_equipment_type(form_state["replacement_equipment_type"]):
                    errors.append(SUPPORTED_EQUIPMENT_TYPE_MESSAGE)
                if not form_state["confirm_retire"]:
                    errors.append("You must confirm the failed asset is being retired.")
                if not form_state["confirm_slot"]:
                    errors.append("You must confirm the replacement will go into the target slot.")
                if blocking_errors:
                    errors.extend(blocking_errors)
                if errors:
                    error_message = "; ".join(errors)
                    return render_template(
                        "admin_replace_asset.html",
                        form=form_state,
                        failed_asset=failed_asset_view,
                        error_message=error_message,
                        failure_type_options=sorted(RETIRE_FAILURE_TYPES),
                        equipment_type_options=_equipment_type_form_options(form_state["replacement_equipment_type"]),
                    )

                try:
                    conn.execute("BEGIN;")
                    locked_failed = conn.execute(
                        """
                        SELECT id, asset_tag, location_type, current_holder_id, home_slot_id
                        FROM assets
                        WHERE id = ?
                        LIMIT 1;
                        """,
                        (int(failed_asset_view["id"]),),
                    ).fetchone()
                    if not locked_failed:
                        raise ValueError("failed asset_tag not found.")

                    locked_location = _normalize_location_type(locked_failed["location_type"])
                    if _is_terminal_location_type(locked_location):
                        raise ValueError("Failed asset is already retired/disposed.")
                    if locked_location not in {"STORAGE", "IN_CUSTODY"}:
                        raise ValueError("Failed asset must be in STORAGE or IN_CUSTODY.")

                    target_slot_id, target_slot = _resolve_replacement_target_slot(
                        conn,
                        failed_asset_id=int(locked_failed["id"]),
                        failed_asset_tag=str(locked_failed["asset_tag"]),
                        failed_home_slot_id=locked_failed["home_slot_id"],
                    )

                    replacement_tag_exists = conn.execute(
                        """
                        SELECT 1
                        FROM assets
                        WHERE UPPER(asset_tag) = UPPER(?)
                        LIMIT 1;
                        """,
                        (form_state["replacement_asset_tag"],),
                    ).fetchone()
                    if replacement_tag_exists:
                        raise ValueError("replacement asset_tag already exists.")

                    replacement_serial_exists = conn.execute(
                        """
                        SELECT 1
                        FROM assets
                        WHERE TRIM(COALESCE(serial_number, '')) <> ''
                          AND UPPER(serial_number) = UPPER(?)
                        LIMIT 1;
                        """,
                        (form_state["replacement_serial_number"],),
                    ).fetchone()
                    if replacement_serial_exists:
                        raise ValueError("replacement serial_number already exists.")

                    _validate_swap_target_slot_integrity(
                        conn,
                        target_slot_id=target_slot_id,
                        failed_asset_id=int(locked_failed["id"]),
                        failed_asset_tag=str(locked_failed["asset_tag"]),
                    )

                    _retire_admin_asset_in_tx(
                        conn,
                        asset_id=int(locked_failed["id"]),
                        asset_tag=str(locked_failed["asset_tag"]),
                        failure_type=form_state["failure_type"],
                        notes=form_state["failure_notes"],
                        actor="admin",
                    )

                    _create_admin_asset_in_tx(
                        conn,
                        asset_tag=form_state["replacement_asset_tag"],
                        actor="admin",
                        equipment_type=form_state["replacement_equipment_type"],
                        serial_number=form_state["replacement_serial_number"],
                        manufacturer=form_state["replacement_manufacturer"],
                        building=str(failed_asset_view.get("building_room") or "").split("/", 1)[0],
                        room=str(failed_asset_view.get("building_room") or "").split("/", 1)[1]
                        if "/" in str(failed_asset_view.get("building_room") or "")
                        else "",
                        model=form_state["replacement_model"] or None,
                        model_code=form_state["replacement_model_code"] or None,
                        notes=form_state["replacement_notes"] or None,
                        assign_case_number=str(target_slot["case_name"]),
                        assign_slot_number=int(target_slot["slot_position"]),
                    )

                    conn.commit()
                except ValueError as e:
                    conn.rollback()
                    error_message = str(e)
                    return render_template(
                        "admin_replace_asset.html",
                        form=form_state,
                        failed_asset=failed_asset_view,
                        error_message=error_message,
                        failure_type_options=sorted(RETIRE_FAILURE_TYPES),
                        equipment_type_options=_equipment_type_form_options(form_state["replacement_equipment_type"]),
                    )
                except sqlite3.IntegrityError as e:
                    conn.rollback()
                    error_message = f"replace failed: {e}"
                    return render_template(
                        "admin_replace_asset.html",
                        form=form_state,
                        failed_asset=failed_asset_view,
                        error_message=error_message,
                        failure_type_options=sorted(RETIRE_FAILURE_TYPES),
                        equipment_type_options=_equipment_type_form_options(form_state["replacement_equipment_type"]),
                    )
                except Exception:
                    conn.rollback()
                    raise

                flash(
                    f"Replaced {form_state['failed_asset_tag']} with {form_state['replacement_asset_tag']} "
                    f"in case {target_slot['case_name']} slot {target_slot['slot_position']}.",
                    "success",
                )
                return redirect(url_for("admin_replace_asset"))
            else:
                error_message = "Unknown action."
        finally:
            conn.close()

    return render_template(
        "admin_replace_asset.html",
        form=form_state,
        failed_asset=failed_asset_view,
        error_message=error_message,
        failure_type_options=sorted(RETIRE_FAILURE_TYPES),
        equipment_type_options=_equipment_type_form_options(form_state["replacement_equipment_type"]),
    )


@app.post("/admin/events/correct")
@require_login
@require_role("admin")
def admin_correct_event():
    guard_result = _require_admin_for_api()
    if guard_result:
        return guard_result

    rate_limit_response = _enforce_admin_route_rate_limit()
    if rate_limit_response is not None:
        return rate_limit_response

    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return {"ok": False, "error": "JSON body must be an object"}, 400

    raw_supersedes = data.get("supersedes_event_id")
    correction_reason = str(data.get("correction_reason") or "").strip()

    try:
        supersedes_event_id = int(str(raw_supersedes).strip())
    except (TypeError, ValueError):
        return {"ok": False, "error": "supersedes_event_id must be an integer"}, 400

    if not correction_reason:
        return {"ok": False, "error": "correction_reason is required"}, 400

    conn = get_connection()
    try:
        original = _get_event_by_id(conn, supersedes_event_id)
        if original is None:
            return {"ok": False, "error": f"event {supersedes_event_id} not found"}, 404

        if _event_already_superseded(conn, supersedes_event_id):
            return {"ok": False, "error": f"event {supersedes_event_id} is already superseded"}, 409

        # Copy-from-original defaults, with explicit override support
        asset_tag = str(data.get("asset_tag") or original.get("asset_tag") or "").strip()
        event_type = str(data.get("event_type") or original.get("event_type") or "").strip()
        event_date = str(data.get("event_date") or original.get("event_date") or "").strip()
        actor = str(data.get("actor") or original.get("actor") or "admin").strip()

        notes_value = data.get("notes", None)
        if notes_value is None:
            notes_value = original.get("notes")
        notes = str(notes_value) if notes_value is not None else None

        payload = data.get("payload", None)
        if payload is None:
            try:
                payload = json.loads(original.get("payload") or "null")
            except (TypeError, ValueError):
                payload = None

        if not asset_tag:
            return {"ok": False, "error": "asset_tag is required"}, 400
        if not event_type:
            return {"ok": False, "error": "event_type is required"}, 400
        if not event_date:
            return {"ok": False, "error": "event_date is required"}, 400

        # Keep payload predictable: only dicts become JSON; everything else => None
        payload_dict = payload if isinstance(payload, dict) else None

        try:
            conn.execute("BEGIN;")
            record_event(
                conn,
                asset_tag=asset_tag,
                event_type=event_type,
                event_date=event_date,
                actor=actor or "admin",
                notes=notes,
                payload=payload_dict,
                supersedes_event_id=supersedes_event_id,
                correction_reason=correction_reason,
            )
            conn.commit()
        except sqlite3.IntegrityError as e:
            conn.rollback()
            return {"ok": False, "error": f"correction failed: {e}"}, 400
        except Exception:
            conn.rollback()
            raise

        return (
            {
                "ok": True,
                "supersedes_event_id": supersedes_event_id,
                "asset_tag": asset_tag,
                "event_type": event_type,
            },
            201,
        )
    finally:
        conn.close()


@app.post("/admin/assets/create")
@require_login
@require_role("admin")
def admin_create_asset():
    guard_result = _require_admin_for_api()
    if guard_result:
        return guard_result

    raw_data = request.get_json(silent=True)
    if not isinstance(raw_data, dict):
        raw_data = request.form.to_dict()

    asset_tag = str(raw_data.get("asset_tag") or "").strip().upper()
    actor = str(raw_data.get("actor") or "").strip()
    equipment_type_raw = str(raw_data.get("equipment_type") or "").strip()
    serial_number = str(raw_data.get("serial_number") or "").strip()
    manufacturer = str(raw_data.get("manufacturer") or "").strip()
    building = str(raw_data.get("building") or "").strip()
    room = str(raw_data.get("room") or "").strip()
    model = str(raw_data.get("model") or "").strip() or None
    model_code = str(raw_data.get("model_code") or "").strip() or None
    notes_raw = str(raw_data.get("notes") or "").strip()
    home_slot_raw = raw_data.get("home_slot_id")

    equipment_type = equipment_type_raw or ""
    notes = notes_raw or None

    errors: list[str] = []
    if not asset_tag:
        errors.append("asset_tag is required.")
    if not actor:
        errors.append("actor is required.")

    home_slot_id: Optional[int] = None
    if home_slot_raw is not None and str(home_slot_raw).strip() != "":
        try:
            home_slot_id = int(str(home_slot_raw).strip())
        except ValueError:
            errors.append("home_slot_id must be an integer.")

    if errors:
        return {"ok": False, "error": "; ".join(errors)}, 400

    conn = get_connection()
    try:
        try:
            conn.execute("BEGIN;")
            assign_case_number: Optional[str] = None
            assign_slot_number: Optional[int] = None
            if home_slot_id is not None:
                slot_row = conn.execute(
                    """
                    SELECT id, case_name, slot_position
                    FROM slots
                    WHERE id = ?
                    LIMIT 1;
                    """,
                    (home_slot_id,),
                ).fetchone()
                if slot_row is None:
                    raise ValueError("home_slot_id does not reference an existing slot.")
                assign_case_number = str(slot_row["case_name"])
                assign_slot_number = int(slot_row["slot_position"])

            created = _create_admin_asset_in_tx(
                conn,
                asset_tag=asset_tag,
                actor=actor,
                equipment_type=equipment_type,
                serial_number=serial_number,
                manufacturer=manufacturer,
                building=building,
                room=room,
                model=model,
                model_code=model_code,
                notes=notes,
                assign_case_number=assign_case_number,
                assign_slot_number=assign_slot_number,
            )

            conn.commit()
        except ValueError as e:
            conn.rollback()
            return {"ok": False, "error": str(e)}, 400
        except sqlite3.IntegrityError as e:
            conn.rollback()
            return {"ok": False, "error": f"create failed: {e}"}, 400
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()

    return {
        "ok": True,
        "asset_tag": asset_tag,
        "location_type": str(created["location_type"]),
        "current_holder_id": created["current_holder_id"],
        "home_slot_id": created["home_slot_id"],
        "home_slot_label": "Unslotted" if created["home_slot_id"] is None else str(created["home_slot_id"]),
        "storage_status": "Unslotted" if created["home_slot_id"] is None else "Slotted",
        "event_type": "ASSET_CREATED",
    }


@app.route("/admin/assign-slot", methods=["GET", "POST"])
@require_login
@require_role("admin")
def admin_assign_slot():
    guard_result = _require_admin_for_route()
    if guard_result:
        return guard_result

    action = ""
    asset_tag = ""
    building = ""
    room = ""
    case_name = ""
    slot_id = ""
    notes = ""
    asset_view: Optional[dict] = None
    preview_rows: list[dict] = []
    selected_slot_ids: list[str] = []
    unslotted_assets: list[dict] = []
    slot_options: list[dict] = []
    case_options: list[str] = []
    building_options: list[str] = []

    conn = get_connection()
    try:
        unslotted_assets = _list_unslotted_storage_assets(conn)
        slot_options = _list_slot_options(conn, empty_only=True)
        case_options = _slot_case_options(slot_options)
        location_context = _assign_slot_location_context()
        building_options = list(location_context["building_options"])

        def render_assign_slot_template():
            batch_tags = _assign_slot_batch_tags()
            batch_assets, batch_errors = _build_assign_slot_batch_assets(conn, batch_tags)
            if not batch_assets and asset_view is not None and action == "assign":
                batch_assets = [asset_view]
            return render_template(
                "admin_assign_slot.html",
                asset_tag=asset_tag,
                building=building,
                room=room,
                case_name=case_name,
                slot_id=slot_id,
                notes=notes,
                asset=asset_view,
                batch_assets=batch_assets,
                batch_errors=batch_errors,
                preview_rows=preview_rows,
                selected_slot_ids=selected_slot_ids,
                unslotted_assets=unslotted_assets,
                slot_options=slot_options,
                case_options=case_options,
                building_options=building_options,
            )

        if request.method == "POST":
            action = (request.form.get("action") or "lookup").strip().lower()
            asset_tag = (request.form.get("asset_tag") or "").strip()
            remove_asset_tag = (request.form.get("remove_asset_tag") or "").strip().upper()
            building = (request.form.get("building") or "").strip()
            room = (request.form.get("room") or "").strip()
            case_name = (request.form.get("case_name") or "").strip().upper()
            slot_id = (request.form.get("slot_id") or "").strip()
            selected_slot_ids = [str(value or "").strip() for value in request.form.getlist("slot_id")]
            notes = (request.form.get("notes") or "").strip()

            if action == "lookup":
                asset_view, blocking_errors = _build_admin_assign_asset_view(conn, asset_tag)
                if not asset_tag:
                    flash("asset_tag is required.", "error")
                elif blocking_errors:
                    for msg in blocking_errors:
                        flash(msg, "error")
                else:
                    canonical_tag = str(asset_view["asset_tag"]).strip().upper()
                    batch_tags = _assign_slot_batch_tags()
                    if canonical_tag in batch_tags:
                        flash(f"Asset {canonical_tag} is already in this assignment batch.", "error")
                    else:
                        batch_tags.append(canonical_tag)
                        _save_assign_slot_batch_tags(batch_tags)
                        flash(f"Asset {canonical_tag} is eligible for slot assignment.", "success")
                        flash(f"Added asset {canonical_tag} to the assignment batch.", "success")
            elif action == "remove":
                batch_tags = _assign_slot_batch_tags()
                if remove_asset_tag and remove_asset_tag in batch_tags:
                    batch_tags = [tag for tag in batch_tags if tag != remove_asset_tag]
                    _save_assign_slot_batch_tags(batch_tags)
                    flash(f"Removed asset {remove_asset_tag} from the assignment batch.", "success")
                else:
                    flash("Asset was not in the assignment batch.", "error")
            elif action == "clear":
                _clear_assign_slot_workflow_state()
                flash("Cleared the assignment batch.", "success")
                return redirect(url_for("admin_assign_slot"))
            elif action == "preview":
                session.pop(ASSIGN_SLOT_PENDING_SESSION_KEY, None)
                batch_tags = _assign_slot_batch_tags()
                batch_assets, batch_errors = _build_assign_slot_batch_assets(conn, batch_tags)
                errors: list[str] = list(batch_errors)
                structurally_complete = True
                if not batch_tags:
                    errors.append("Add at least one eligible asset before previewing the assignment batch.")
                    structurally_complete = False
                if not case_name:
                    errors.append("case is required.")
                    structurally_complete = False
                if len(selected_slot_ids) < len(batch_tags) or any(not value for value in selected_slot_ids[: len(batch_tags)]):
                    errors.append("Select one empty destination slot for each asset.")
                    structurally_complete = False

                normalized_location, location_errors, _ = _validate_assign_slot_location_form(
                    {"building": building, "room": room}
                )
                building = normalized_location["building"]
                room = normalized_location["room"]
                errors.extend(location_errors)
                if batch_errors or location_errors:
                    structurally_complete = False

                if case_name and batch_tags and _assign_slot_empty_slot_count(slot_options, case_name) < len(batch_tags):
                    errors.append("Selected case does not have enough empty slots for this assignment batch.")

                assignments = _assign_slot_form_assignments(batch_tags, case_name, selected_slot_ids, building, room)
                if structurally_complete:
                    try:
                        prepared = _prepare_assign_slot_batch_in_tx(conn, assignments)
                        if not errors:
                            preview_rows = _assign_slot_preview_rows(prepared)
                            session[ASSIGN_SLOT_PENDING_SESSION_KEY] = {
                                "assignments": assignments,
                                "building": building,
                                "room": room,
                                "case_name": case_name,
                                "notes": notes,
                                "rows": preview_rows,
                            }
                    except ValueError as e:
                        errors.append(str(e))
                for error in errors:
                    flash(error, "error")
                if not errors:
                    flash("Assignment batch preview ready. Review and confirm one batch to commit.", "success")
            elif action == "commit":
                if request.form.get("confirm_assignment") != "yes":
                    flash("Please confirm you reviewed the assignment batch before committing.", "error")
                    pending_preview = session.get(ASSIGN_SLOT_PENDING_SESSION_KEY)
                    if isinstance(pending_preview, dict):
                        preview_rows = list(pending_preview.get("rows") or [])
                        building = str(pending_preview.get("building") or "")
                        room = str(pending_preview.get("room") or "")
                        case_name = str(pending_preview.get("case_name") or "")
                        notes = str(pending_preview.get("notes") or "")
                        selected_slot_ids = [
                            str(assignment.get("slot_id") or "")
                            for assignment in list(pending_preview.get("assignments") or [])
                        ]
                    return render_assign_slot_template()

                pending_preview = session.get(ASSIGN_SLOT_PENDING_SESSION_KEY)
                if not isinstance(pending_preview, dict) or not pending_preview.get("assignments"):
                    flash("Preview the complete mapping before committing.", "error")
                    return render_assign_slot_template()

                assignments = list(pending_preview.get("assignments") or [])
                building = str(pending_preview.get("building") or "")
                room = str(pending_preview.get("room") or "")
                case_name = str(pending_preview.get("case_name") or "")
                notes = str(pending_preview.get("notes") or "")
                preview_rows = list(pending_preview.get("rows") or [])
                selected_slot_ids = [
                    str(assignment.get("slot_id") or "")
                    for assignment in list(pending_preview.get("assignments") or [])
                ]
                try:
                    assignment_results = _assign_slot_batch(
                        conn,
                        assignments,
                        actor="admin",
                        notes=notes,
                    )
                except ValueError as e:
                    conn.rollback()
                    flash(str(e), "error")
                    return render_assign_slot_template()
                except Exception:
                    conn.rollback()
                    raise

                _clear_assign_slot_workflow_state()
                unslotted_assets = _list_unslotted_storage_assets(conn)
                if len(assignment_results) == 1:
                    result = assignment_results[0]
                    flash(
                        f"Assigned asset {result['asset_tag']} to {result['case_name']} slot {result['slot_position']}.",
                        "success",
                    )
                else:
                    flash(f"Assigned {len(assignment_results)} assets to slots in {case_name}.", "success")
                return redirect(url_for("admin_assign_slot"))
            elif action == "assign":
                asset_view, blocking_errors = _build_admin_assign_asset_view(conn, asset_tag)
                if not asset_tag:
                    flash("asset_tag is required.", "error")
                if not case_name:
                    flash("case is required.", "error")
                if not slot_id:
                    flash("slot is required.", "error")

                if not asset_tag or not case_name or not slot_id:
                    return render_assign_slot_template()

                if blocking_errors:
                    for msg in blocking_errors:
                        flash(msg, "error")
                    return render_assign_slot_template()

                normalized_location, location_errors, _ = _validate_assign_slot_location_form(
                    {"building": building, "room": room}
                )
                building = normalized_location["building"]
                room = normalized_location["room"]
                if location_errors:
                    for error in location_errors:
                        flash(error, "error")
                    return render_assign_slot_template()

                selected_slot, slot_errors = _resolve_slot_selection(
                    conn,
                    case_name=case_name,
                    slot_id_raw=slot_id,
                )
                slot_error_map = {
                    "case and slot must both be selected.": "Select both case and slot.",
                    "slot selection is invalid.": "Select a valid slot.",
                    "selected slot does not exist.": "Selected slot does not exist.",
                    "selected slot does not belong to the selected case.": "Selected slot does not belong to selected case.",
                }
                if slot_errors:
                    for error in slot_errors:
                        flash(slot_error_map.get(error, error), "error")
                    return render_assign_slot_template()

                try:
                    assignment_result = _assign_single_asset_to_slot(
                        conn,
                        asset_tag=asset_tag,
                        case_name=case_name,
                        slot_id=int(selected_slot["id"]),
                        building=building,
                        room=room,
                        actor="admin",
                        notes=notes,
                    )
                except ValueError as e:
                    conn.rollback()
                    flash(str(e), "error")
                    asset_view, _ = _build_admin_assign_asset_view(conn, asset_tag)
                    unslotted_assets = _list_unslotted_storage_assets(conn)
                    return render_assign_slot_template()
                except Exception:
                    conn.rollback()
                    raise

                _clear_assign_slot_workflow_state()
                flash(
                    f"Assigned asset {assignment_result['asset_tag']} to {assignment_result['case_name']} slot {assignment_result['slot_position']}.",
                    "success",
                )
                return redirect(url_for("admin_assign_slot"))
            else:
                flash("Unknown action.", "error")

        return render_assign_slot_template()
    finally:
        conn.close()

@app.route("/admin/slot-move", methods=["GET", "POST"])
@require_login
@require_role("admin")
def admin_slot_move():
    guard_result = _require_admin_for_route()
    if guard_result:
        return guard_result

    source_slot_id_raw = (request.args.get("slot_id") or request.form.get("source_slot_id") or "").strip()
    source_slot_id: Optional[int] = None
    source_slot: Optional[dict] = None
    building_room = ""
    case_number = ""
    slot_number = ""
    notes = ""
    move_preview: Optional[dict] = None
    source_slots: list[dict] = []
    destination_slots: list[dict] = []

    if source_slot_id_raw:
        try:
            source_slot_id = int(source_slot_id_raw)
        except ValueError:
            flash("Select a valid source slot.", "error")

    conn = get_connection()
    try:
        source_slots = _list_admin_slot_move_sources(conn)
        if source_slot_id is not None:
            source_slot = _build_admin_slot_move_source_view(conn, source_slot_id)
            if source_slot:
                asset = source_slot.get("asset") or {}
                building_room = str(asset.get("building_room") or "")
                case_number = str(source_slot.get("case_name") or "")
                slot_number = str(source_slot.get("slot_position") or "")
                destination_slots = _list_admin_slot_move_destinations(
                    conn,
                    source_slot_id=source_slot_id,
                    building_room=building_room,
                )
            elif request.method == "GET":
                flash("Source slot not found.", "error")

        if request.method == "POST":
            action = (request.form.get("action") or "preview").strip().lower()
            source_asset = (source_slot or {}).get("asset") or {}
            building_room = str(source_asset.get("building_room") or "").strip()
            case_number = (request.form.get("case_number") or "").strip().upper()
            slot_number = (request.form.get("slot_number") or "").strip()
            notes = (request.form.get("notes") or "").strip()
            destination_slot_id_raw = (request.form.get("destination_slot_id") or "").strip()
            expected_asset_id_raw = (request.form.get("expected_asset_id") or "").strip()
            expected_destination_slot_id_raw = (request.form.get("expected_destination_slot_id") or "").strip()

            if source_slot_id is None:
                flash("Select a source slot.", "error")
                return render_template(
                    "admin_slot_move.html",
                    source_slot=source_slot,
                    source_slots=source_slots,
                    destination_slots=destination_slots,
                    source_slot_id=source_slot_id_raw,
                    building_room=building_room,
                    case_number=case_number,
                    slot_number=slot_number,
                    notes=notes,
                    move_preview=move_preview,
                )

            if not source_slot or not source_slot.get("occupied"):
                flash("Source slot is missing or empty.", "error")

            if not source_slot or not source_slot.get("occupied"):
                return render_template(
                    "admin_slot_move.html",
                    source_slot=source_slot,
                    source_slots=source_slots,
                    destination_slots=destination_slots,
                    source_slot_id=source_slot_id,
                    building_room=building_room,
                    case_number=case_number,
                    slot_number=slot_number,
                    notes=notes,
                    move_preview=move_preview,
                )

            try:
                if action in {"preview", "commit"}:
                    if not destination_slot_id_raw:
                        raise ValueError("Select a destination slot.")
                    try:
                        destination_slot_id = int(destination_slot_id_raw)
                    except ValueError as exc:
                        raise ValueError("Select a valid destination slot.") from exc
                    destination_slot = _build_admin_slot_move_destination_view(
                        conn,
                        destination_slot_id,
                        building_room=building_room,
                    )
                    if not destination_slot:
                        raise ValueError("Destination slot does not exist.")
                    building_room = str(destination_slot["building_room"] or "")
                    case_number = str(destination_slot["case_name"] or "")
                    slot_number = str(destination_slot["slot_position"])
                move_preview = _build_admin_slot_move_preview(
                    conn,
                    source_slot_id=source_slot_id,
                    building_room=building_room,
                    case_number=case_number,
                    slot_number=slot_number,
                )
                if action == "commit":
                    _validate_admin_slot_move_expected(
                        move_preview,
                        expected_asset_id_raw=expected_asset_id_raw,
                        expected_destination_slot_id_raw=expected_destination_slot_id_raw,
                    )
            except ValueError as e:
                flash(str(e), "error")
                return render_template(
                    "admin_slot_move.html",
                    source_slot=source_slot,
                    source_slots=source_slots,
                    destination_slots=destination_slots,
                    source_slot_id=source_slot_id,
                    building_room=building_room,
                    case_number=case_number,
                    slot_number=slot_number,
                    notes=notes,
                    move_preview=move_preview,
                )

            if action == "preview":
                return render_template(
                    "admin_slot_move.html",
                    source_slot=source_slot,
                    source_slots=source_slots,
                    destination_slots=destination_slots,
                    source_slot_id=source_slot_id,
                    building_room=building_room,
                    case_number=case_number,
                    slot_number=slot_number,
                    notes=notes,
                    move_preview=move_preview,
                )
            if action != "commit":
                flash("Unknown action.", "error")
                return render_template(
                    "admin_slot_move.html",
                    source_slot=source_slot,
                    source_slots=source_slots,
                    destination_slots=destination_slots,
                    source_slot_id=source_slot_id,
                    building_room=building_room,
                    case_number=case_number,
                    slot_number=slot_number,
                    notes=notes,
                    move_preview=move_preview,
                )

            try:
                conn.execute("BEGIN;")

                if not destination_slot_id_raw:
                    raise ValueError("Select a destination slot.")
                try:
                    destination_slot_id = int(destination_slot_id_raw)
                except ValueError as exc:
                    raise ValueError("Select a valid destination slot.") from exc
                destination_slot = _build_admin_slot_move_destination_view(
                    conn,
                    destination_slot_id,
                    building_room=building_room,
                )
                if not destination_slot:
                    raise ValueError("Destination slot does not exist.")
                building_room = str(destination_slot["building_room"] or "")
                case_number = str(destination_slot["case_name"] or "")
                slot_number = str(destination_slot["slot_position"])
                move_preview = _build_admin_slot_move_preview(
                    conn,
                    source_slot_id=source_slot_id,
                    building_room=building_room,
                    case_number=case_number,
                    slot_number=slot_number,
                )
                _validate_admin_slot_move_expected(
                    move_preview,
                    expected_asset_id_raw=expected_asset_id_raw,
                    expected_destination_slot_id_raw=expected_destination_slot_id_raw,
                )
                asset_tag = str(move_preview["asset"]["asset_tag"])
                destination_slot_id = int(move_preview["destination"]["slot_id"])
                move_asset_between_slots_in_tx(
                    conn,
                    move_preview=move_preview,
                    notes=notes,
                    actor="admin",
                )

                conn.commit()
            except ValueError as e:
                conn.rollback()
                flash(str(e), "error")
                source_slot = _build_admin_slot_move_source_view(conn, source_slot_id)
                return render_template(
                    "admin_slot_move.html",
                    source_slot=source_slot,
                    source_slots=source_slots,
                    destination_slots=destination_slots,
                    source_slot_id=source_slot_id,
                    building_room=building_room,
                    case_number=case_number,
                    slot_number=slot_number,
                    notes=notes,
                    move_preview=move_preview,
                )
            except Exception:
                conn.rollback()
                raise

            flash(
                f"Moved asset {asset_tag} from case {move_preview['source']['case_number']}, slot {move_preview['source']['slot_number']} "
                f"to case {move_preview['destination']['case_number']}, slot {move_preview['destination']['slot_number']}.",
                "success",
            )
            return redirect(url_for("admin_slot_move", slot_id=destination_slot_id))
    finally:
        conn.close()

    return render_template(
        "admin_slot_move.html",
        source_slot=source_slot,
        source_slots=source_slots,
        destination_slots=destination_slots,
        source_slot_id=source_slot_id,
        building_room=building_room,
        case_number=case_number,
        slot_number=slot_number,
        notes=notes,
        move_preview=move_preview,
    )


@app.route("/admin/force-vacate", methods=["GET", "POST"])
@require_login
@require_role("admin")
def admin_force_vacate():
    guard_result = _require_admin_for_route()
    if guard_result:
        return guard_result

    slot_id_raw = (request.args.get("slot_id") or request.form.get("slot_id") or "").strip()
    slot_id: Optional[int] = None
    slot_view: Optional[dict] = None
    reason = ""
    notes = ""
    confirmed = False

    if slot_id_raw:
        try:
            slot_id = int(slot_id_raw)
        except ValueError:
            flash("slot_id must be an integer.", "error")

    conn = get_connection()
    try:
        if slot_id is not None:
            slot_view = _build_admin_force_vacate_view(conn, slot_id)
            if slot_view is None and request.method == "GET":
                flash("Slot not found.", "error")
        elif request.method == "GET":
            flash("slot_id is required.", "error")

        if request.method == "POST":
            reason = (request.form.get("reason") or "").strip()
            notes = (request.form.get("notes") or "").strip()
            confirmed = (request.form.get("confirm_empty") or "").strip().lower() in {"on", "true", "1", "yes"}

            if slot_id is None:
                flash("slot_id is required.", "error")
            if slot_view is None:
                flash("Slot not found.", "error")
            if slot_view and not slot_view.get("occupied"):
                flash("Cannot force vacate an empty slot.", "error")

            asset_for_view = (slot_view or {}).get("asset") if slot_view else None
            if asset_for_view and _normalize_location_type(asset_for_view.get("location_type")) == "IN_CUSTODY":
                flash("Cannot force vacate: occupied asset is IN_CUSTODY.", "error")
            if asset_for_view and _is_terminal_location_type(asset_for_view.get("location_type")):
                flash("Cannot force vacate: occupied asset is retired/disposed.", "error")
            if not reason:
                flash("Reason is required.", "error")
            if not confirmed:
                flash("You must confirm physical verification before force vacate.", "error")

            if (
                slot_id is None
                or slot_view is None
                or not slot_view.get("occupied")
                or not reason
                or not confirmed
                or (asset_for_view and _normalize_location_type(asset_for_view.get("location_type")) == "IN_CUSTODY")
                or (asset_for_view and _is_terminal_location_type(asset_for_view.get("location_type")))
            ):
                return render_template(
                    "admin_force_vacate.html",
                    slot=slot_view,
                    slot_id=slot_id_raw,
                    reason=reason,
                    notes=notes,
                    confirmed=confirmed,
                )

            try:
                conn.execute("BEGIN;")

                locked = conn.execute(
                    """
                    SELECT
                        s.id AS slot_id,
                        s.case_name,
                        s.slot_position,
                        s.current_asset_tag,
                        so.asset_id AS occupancy_asset_id,
                        a.asset_tag AS occ_asset_tag,
                        a.manufacturer AS occ_manufacturer,
                        a.model AS occ_model,
                        a.serial_number AS occ_serial,
                        a.location_type AS occ_location_type,
                        a.building_room AS occ_building_room
                    FROM slots s
                    LEFT JOIN slot_occupancy so ON so.slot_id = s.id
                    LEFT JOIN assets a ON a.id = so.asset_id
                    WHERE s.id = ?
                    LIMIT 1;
                    """,
                    (slot_id,),
                ).fetchone()

                if not locked:
                    raise ValueError("Slot not found.")

                legacy_asset_tag = str(locked["current_asset_tag"] or "").strip()
                occupied = locked["occupancy_asset_id"] is not None or bool(legacy_asset_tag)
                if not occupied:
                    raise ValueError("Cannot force vacate an empty slot.")

                asset_id: Optional[int] = None
                asset_tag = ""
                asset_manufacturer = ""
                asset_model = ""
                asset_serial = ""
                asset_location_type = ""
                asset_building_room = ""

                if locked["occupancy_asset_id"] is not None:
                    asset_id = int(locked["occupancy_asset_id"])
                    asset_tag = str(locked["occ_asset_tag"] or "")
                    asset_manufacturer = str(locked["occ_manufacturer"] or "")
                    asset_model = str(locked["occ_model"] or "")
                    asset_serial = str(locked["occ_serial"] or "")
                    asset_location_type = str(locked["occ_location_type"] or "").strip().upper()
                    asset_building_room = str(locked["occ_building_room"] or "")
                elif legacy_asset_tag:
                    legacy_asset = _find_asset_for_scan_tag(conn, legacy_asset_tag)
                    if legacy_asset:
                        asset_id = int(legacy_asset["id"])
                        asset_tag = str(legacy_asset.get("asset_tag") or legacy_asset_tag)
                        asset_manufacturer = str(legacy_asset.get("manufacturer") or "")
                        asset_model = str(legacy_asset.get("model") or "")
                        asset_serial = str(legacy_asset.get("serial_number") or "")
                        asset_location_type = str(legacy_asset.get("location_type") or "").strip().upper()
                        asset_building_room = str(legacy_asset.get("building_room") or "")
                    else:
                        raise ValueError("Occupied asset record not found.")

                if _is_terminal_location_type(asset_location_type):
                    raise ValueError("Cannot force vacate: occupied asset is retired/disposed.")
                if asset_location_type == "IN_CUSTODY":
                    raise ValueError("Cannot force vacate: occupied asset is IN_CUSTODY.")

                conn.execute(
                    """
                    DELETE FROM slot_occupancy
                    WHERE slot_id = ?;
                    """,
                    (slot_id,),
                )

                conn.execute(
                    """
                    UPDATE slots
                    SET current_asset_tag = NULL
                    WHERE id = ?;
                    """,
                    (slot_id,),
                )

                now_iso = datetime.now(timezone.utc).isoformat()
                if asset_id is not None:
                    asset_columns = get_asset_table_columns(conn)
                    update_clauses: list[str] = []
                    update_values: list[object] = []
                    if "home_slot_id" in asset_columns:
                        update_clauses.append("home_slot_id = NULL")
                    if "location_type" in asset_columns:
                        update_clauses.append("location_type = ?")
                        update_values.append("STORAGE")
                    if "updated_date" in asset_columns:
                        update_clauses.append("updated_date = ?")
                        update_values.append(now_iso)
                    if update_clauses:
                        update_values.append(asset_id)
                        conn.execute(
                            f"UPDATE assets SET {', '.join(update_clauses)} WHERE id = ?;",
                            tuple(update_values),
                        )

                payload = {
                    "slot": {
                        "slot_id": int(locked["slot_id"]),
                        "building_room": asset_building_room,
                        "case_number": str(locked["case_name"] or ""),
                        "slot_number": int(locked["slot_position"]),
                    },
                    "asset": {
                        "asset_id": asset_id,
                        "asset_tag": asset_tag,
                        "building_room": asset_building_room,
                        "manufacturer": asset_manufacturer,
                        "model": asset_model,
                        "serial": asset_serial,
                    },
                    "reason": reason,
                    "notes": notes or None,
                }
                conn.execute(
                    """
                    INSERT INTO asset_events (
                        asset_tag,
                        event_type,
                        event_date,
                        actor,
                        notes,
                        payload,
                        holder_id
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        asset_tag,
                        "FORCE_VACATE",
                        now_iso,
                        "admin",
                        reason,
                        json.dumps(payload),
                        None,
                    ),
                )

                conn.commit()
            except ValueError as e:
                conn.rollback()
                flash(str(e), "error")
                slot_view = _build_admin_force_vacate_view(conn, slot_id)
                return render_template(
                    "admin_force_vacate.html",
                    slot=slot_view,
                    slot_id=slot_id,
                    reason=reason,
                    notes=notes,
                    confirmed=confirmed,
                )
            except Exception:
                conn.rollback()
                raise

            flash(
                f"Force vacated slot {locked['case_name']} slot {locked['slot_position']} for asset {asset_tag}.",
                "success",
            )
            return redirect(url_for("admin_force_vacate", slot_id=slot_id))
    finally:
        conn.close()

    return render_template(
        "admin_force_vacate.html",
        slot=slot_view,
        slot_id=slot_id,
        reason=reason,
        notes=notes,
        confirmed=confirmed,
    )


if __name__ == "__main__":
    # Local dev run (and container run).
    app.run(host="0.0.0.0", port=8000, debug=False)
