"""Phone offline alert - Juvo internal test.

Polls VOXO device registration status for a single tenant, compares it
against the previous run's state file, and emails an alert when any
extension transitions from online to offline.

Manual runs only for now. Defaults to --dry-run so nothing sends until
you pass --send.

Environment: reads .env sitting next to this script. See .env.example.

Endpoints used:
  GET /v2/admin/extensions/summary  (informational count)
  GET /v2/admin/extensions/         (extension metadata by id)
  GET /v2/admin/devices/            (per-device userAgent -> online/offline)

Why the devices endpoint: on Juvo's tenant, the documented
/extensions/{id}/registrations endpoint returns [] for every extension
regardless of the id form used, disagreeing with the summary. The
devices endpoint exposes a userAgent field per device that is
"Empty string when ... the phone is not currently registered" per the
docs, which is the same signal we need. Limitation: devices are
physical/registered endpoints (desk phones, ATAs, paging horns).
Softphone-only extensions have no device record and are skipped.
That is the right scope for the Republic Finance use case (desk-phone
drops), and it matches this script's intent.
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


def list_devices(cfg: dict) -> list[dict]:
    """All devices for the tenant."""
    data = api_get(cfg, "/v2/admin/devices/", {"tenantId": cfg["tenant_id"]})
    if isinstance(data, list):
        return data
    return data.get("records") or []


def device_is_online(device: dict) -> bool:
    """Per the docs, userAgent is an empty string when the phone is not
    currently registered. Anything else means an active SIP registration."""
    return bool((device.get("userAgent") or "").strip())


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
    """Return (state_by_id, meta_by_id) for the current run.

    Groups devices by their primary-line extension ID (phLine1ExId).
    Extension is online if any of its devices has a non-empty userAgent.
    Extensions with no assigned device are skipped (softphone-only users
    don't have a device record and can't be tracked via this endpoint).
    """
    extensions = list_extensions(cfg)
    devices = list_devices(cfg)
    if verbose:
        print(f"  Found {len(extensions)} extension(s) and {len(devices)} device(s)")

    devices_by_ext: dict[str, list[dict]] = {}
    for dev in devices:
        ext_id = dev.get("phLine1ExId")
        if ext_id is None:
            continue
        devices_by_ext.setdefault(str(ext_id), []).append(dev)

    state: dict[str, dict] = {}
    meta: dict[str, dict] = {}
    skipped = 0
    for ext in extensions:
        ext_id = str(ext.get("id"))
        name = ext.get("name") or ""
        number = ext.get("number") or ""
        ext_type = ext.get("type") or ""

        ext_devices = devices_by_ext.get(ext_id, [])
        if not ext_devices:
            skipped += 1
            if verbose:
                print(f"    {number or '(no num)':>8} {name[:30]:30} -> (no device, skipped)")
            continue

        online_devices = [d for d in ext_devices if device_is_online(d)]
        status = "online" if online_devices else "offline"
        device_labels = [d.get("name") or d.get("mac") or "?" for d in ext_devices]

        state[ext_id] = {
            "status": status,
            "number": number,
            "name": name,
            "type": ext_type,
            "devices": device_labels,
        }
        meta[ext_id] = {"number": number, "name": name, "type": ext_type}
        if verbose:
            print(
                f"    {number or '(no num)':>8} {name[:30]:30} -> {status}  "
                f"[{', '.join(device_labels)}]"
            )
    if skipped:
        print(f"  ({skipped} extension(s) had no device record and were skipped)")
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
                    "devices": curr_entry.get("devices", []),
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
        devs = d.get("devices") or []
        dev_note = f"  [device: {', '.join(devs)}]" if devs else ""
        lines.append(f"  - {label or d['id']} (ext id {d['id']}){dev_note}")
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
