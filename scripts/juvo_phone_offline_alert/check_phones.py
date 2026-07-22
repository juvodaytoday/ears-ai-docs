"""Phone offline alert - Juvo internal test.

Polls VOXO extension registration status for a single tenant, compares it
against the previous run's state file, and emails an alert when any
extension transitions from online to offline.

Manual runs only for now. Defaults to --dry-run so nothing sends until
you pass --send.

Environment: reads .env sitting next to this script. See .env.example.

Endpoints used:
  GET /v2/admin/extensions/summary            (informational count)
  GET /v2/admin/extensions/                   (enumerate)
  GET /v2/admin/extensions/{id}/registrations (per-extension status)
"""

from __future__ import annotations

import argparse
import json
import os
import smtplib
import sys
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

import requests
from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_STATE_FILE = SCRIPT_DIR / "state.json"
DEFAULT_BASE_URL = "https://api.voxo.co"
REQUEST_TIMEOUT = 30


def load_config() -> dict:
    load_dotenv(SCRIPT_DIR / ".env")

    token = os.getenv("VOXO_API_TOKEN")
    tenant_id = os.getenv("VOXO_TENANT_ID")
    if not token or not tenant_id:
        sys.exit("Missing VOXO_API_TOKEN or VOXO_TENANT_ID in .env")

    recipients_raw = os.getenv("ALERT_RECIPIENTS", "")
    recipients = [r.strip() for r in recipients_raw.split(",") if r.strip()]

    return {
        "token": token,
        "tenant_id": tenant_id,
        "base_url": os.getenv("VOXO_API_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
        "include_types": [
            t.strip().lower()
            for t in os.getenv("INCLUDE_EXTENSION_TYPES", "voice").split(",")
            if t.strip()
        ],
        "smtp_host": os.getenv("SMTP_HOST"),
        "smtp_port": int(os.getenv("SMTP_PORT", "587")),
        "smtp_username": os.getenv("SMTP_USERNAME"),
        "smtp_password": os.getenv("SMTP_PASSWORD"),
        "smtp_from": os.getenv("SMTP_FROM"),
        "recipients": recipients,
    }


def api_get(cfg: dict, path: str, params: dict | None = None) -> dict:
    url = f"{cfg['base_url']}{path}"
    headers = {
        "Authorization": f"Bearer {cfg['token']}",
        "Accept": "application/json",
    }
    resp = requests.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
    if resp.status_code == 401:
        sys.exit(
            "401 Unauthorized from VOXO. The access token has probably expired — "
            "regenerate it in PowerShell and update VOXO_API_TOKEN in .env."
        )
    resp.raise_for_status()
    return resp.json()


def get_summary(cfg: dict) -> dict:
    return api_get(cfg, "/v2/admin/extensions/summary", {"tenantId": cfg["tenant_id"]})


def list_extensions(cfg: dict) -> list[dict]:
    data = api_get(
        cfg,
        "/v2/admin/extensions/",
        {"tenantId": cfg["tenant_id"], "paginated": 0},
    )
    if isinstance(data, list):
        records = data
    else:
        records = data.get("records", [])

    wanted = set(cfg["include_types"])
    if wanted:
        records = [r for r in records if (r.get("type") or "").lower() in wanted]
    return records


def extension_is_online(cfg: dict, extension_id: int | str) -> bool:
    """True if the extension has any active SIP registration."""
    data = api_get(cfg, f"/v2/admin/extensions/{extension_id}/registrations")
    if not data:
        return False
    # Response shape: { items: { Contact: {...} } } or a list of items.
    items = data.get("items") if isinstance(data, dict) else None
    if items is None:
        # Some endpoints return the object directly.
        items = data
    if isinstance(items, list):
        return any(bool(entry) for entry in items)
    if isinstance(items, dict):
        return bool(items.get("Contact") or items)
    return False


def load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        print(f"WARN: state file at {path} is unreadable, starting fresh")
        return {}


def save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, indent=2, sort_keys=True))


def build_current_state(cfg: dict, verbose: bool) -> tuple[dict, dict]:
    """Return (state_by_id, meta_by_id) for the current run."""
    extensions = list_extensions(cfg)
    if verbose:
        print(f"  Checking {len(extensions)} extension(s)...")

    state: dict[str, dict] = {}
    meta: dict[str, dict] = {}
    for ext in extensions:
        ext_id = str(ext.get("id"))
        name = ext.get("name") or ""
        number = ext.get("number") or ""
        ext_type = ext.get("type") or ""

        try:
            online = extension_is_online(cfg, ext_id)
        except requests.HTTPError as err:
            print(f"  WARN: registrations lookup failed for ext {ext_id} ({number}): {err}")
            continue

        status = "online" if online else "offline"
        state[ext_id] = {"status": status, "number": number, "name": name, "type": ext_type}
        meta[ext_id] = {"number": number, "name": name, "type": ext_type}
        if verbose:
            print(f"    {number or '(no number)':>8} {name[:30]:30} -> {status}")
    return state, meta


def find_transitions(prev: dict, curr: dict) -> list[dict]:
    """Extensions that were online last time and are offline now."""
    dropped = []
    for ext_id, curr_entry in curr.items():
        if curr_entry["status"] != "offline":
            continue
        prev_entry = prev.get(ext_id)
        if prev_entry and prev_entry.get("status") == "online":
            dropped.append(
                {
                    "id": ext_id,
                    "number": curr_entry.get("number"),
                    "name": curr_entry.get("name"),
                    "type": curr_entry.get("type"),
                    "previously_seen_at": prev_entry.get("checked_at"),
                }
            )
    return dropped


def build_alert_email(cfg: dict, dropped: list[dict], summary: dict, checked_at: str) -> EmailMessage:
    lines = [
        "Juvo phone offline alert",
        "",
        f"Checked at: {checked_at}",
        f"Tenant ID:  {cfg['tenant_id']}",
        "",
        f"Account summary: {summary.get('onlineExtensions', '?')} online / "
        f"{summary.get('offlineExtensions', '?')} offline / "
        f"{summary.get('totalExtensions', '?')} total",
        "",
        f"{len(dropped)} extension(s) went from online to offline since the last check:",
        "",
    ]
    for d in dropped:
        label = " ".join(filter(None, [d.get("number"), d.get("name")]))
        lines.append(f"  - {label or d['id']} (ext id {d['id']})")
    lines += [
        "",
        "This is an internal Juvo test of the VOXO extension-status API.",
        "No external customer is affected by or notified of this alert.",
    ]

    msg = EmailMessage()
    msg["Subject"] = f"[Juvo test] {len(dropped)} phone(s) offline"
    msg["From"] = cfg["smtp_from"] or cfg["smtp_username"]
    msg["To"] = ", ".join(cfg["recipients"])
    msg.set_content("\n".join(lines))
    return msg


def send_alert(cfg: dict, msg: EmailMessage) -> None:
    missing = [
        key
        for key in ("smtp_host", "smtp_username", "smtp_password", "smtp_from")
        if not cfg.get(key)
    ]
    if missing:
        sys.exit(f"Cannot send alert, missing SMTP config: {', '.join(missing)}")
    if not cfg["recipients"]:
        sys.exit("Cannot send alert, ALERT_RECIPIENTS is empty in .env")

    with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"], timeout=REQUEST_TIMEOUT) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.login(cfg["smtp_username"], cfg["smtp_password"])
        smtp.send_message(msg)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--send",
        action="store_true",
        help="Send an email alert if any transitions are found. Default is dry-run (log only).",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=DEFAULT_STATE_FILE,
        help=f"Path to the state file (default: {DEFAULT_STATE_FILE.name} next to this script).",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Print every extension checked.")
    args = parser.parse_args()

    cfg = load_config()
    checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    print(f"[{checked_at}] Checking VOXO tenant {cfg['tenant_id']}")

    try:
        summary = get_summary(cfg)
    except requests.HTTPError as err:
        sys.exit(f"Summary call failed: {err}")

    print(
        f"  Summary: {summary.get('onlineExtensions')} online / "
        f"{summary.get('offlineExtensions')} offline / "
        f"{summary.get('totalExtensions')} total"
    )

    current, _meta = build_current_state(cfg, args.verbose)

    prev_state = load_state(args.state_file)
    prev_extensions = prev_state.get("extensions", {})

    dropped = find_transitions(prev_extensions, current)

    if not prev_state:
        print("  First run - no prior state, so no transitions can be reported.")
    elif dropped:
        print(f"  {len(dropped)} extension(s) went online -> offline since last check:")
        for d in dropped:
            label = " ".join(filter(None, [d.get("number"), d.get("name")])) or d["id"]
            print(f"    - {label} (id {d['id']})")
    else:
        print("  No online -> offline transitions since last check.")

    for ext_id, entry in current.items():
        entry["checked_at"] = checked_at

    save_state(
        args.state_file,
        {"tenant_id": cfg["tenant_id"], "checked_at": checked_at, "extensions": current},
    )
    print(f"  State written to {args.state_file}")

    if dropped:
        msg = build_alert_email(cfg, dropped, summary, checked_at)
        if args.send:
            send_alert(cfg, msg)
            print(f"  Alert emailed to: {', '.join(cfg['recipients'])}")
        else:
            print("  Dry-run: alert email NOT sent. Preview:")
            print("  " + "-" * 60)
            for line in msg.get_content().splitlines():
                print(f"  {line}")
            print("  " + "-" * 60)
            print("  Re-run with --send to actually deliver the alert.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
