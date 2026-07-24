"""Branch-wide phone offline alert - Juvo internal test.

Polls VOXO device registration status for a single tenant, groups
extensions by branch, and emails an alert when a WHOLE BRANCH goes
dark - i.e. every physical phone in that branch is offline. Single
phones dropping do not fire alerts; that is intentional and matches
Republic Finance's actual ask (a branch outage, not a hardware blip).

Manual runs only for now. Defaults to --dry-run so nothing sends until
you pass --send.

For testing without disrupting the office, --test-threshold N treats
a branch as offline when N or fewer phones are online. In production
you leave the flag off, which reverts to strict "zero phones online."

Environment: reads .env sitting next to this script. See .env.example.

Endpoints used:
  GET /v2/admin/extensions/summary  (informational count)
  GET /v2/admin/extensions/         (extension list for this tenant)
  GET /v2/admin/extensions/{id}     (per-extension profile, for branch)
  GET /v2/admin/devices/            (per-device userAgent -> online/offline)

Softphone-only extensions have no device record and are excluded from
branch tallies - they're not part of the "site dark" signal.
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


def get_extension_profile(cfg: dict, extension_id: str) -> dict:
    """Full extension profile - the only list surface that returns branch info."""
    return api_get(cfg, f"/v2/admin/extensions/{extension_id}")


def device_is_online(device: dict) -> bool:
    """Per the docs, userAgent is an empty string when the phone is not
    currently registered. Anything else means an active SIP registration."""
    return bool((device.get("userAgent") or "").strip())


NO_BRANCH_KEY = "_none"


def branch_key(branch: dict | None) -> tuple[str, str]:
    """Return (key, display_name) for a branch object. Groups extensions
    that have no branch metadata into a single 'no branch' bucket so
    they still get tracked instead of silently dropped."""
    if not branch or branch.get("id") in (None, ""):
        return NO_BRANCH_KEY, "(no branch)"
    return str(branch.get("id")), branch.get("name") or f"branch {branch.get('id')}"


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


def build_current_state(cfg: dict, verbose: bool, test_threshold: int) -> dict:
    """Return a per-branch state snapshot for the current run.

    A branch is considered offline when its online-phone count is at or
    below `test_threshold` (default 0 = strict "all phones offline").
    Softphone-only extensions (no device record) are excluded from the
    branch tallies entirely.
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

    branches: dict[str, dict] = {}
    skipped = 0

    for ext in extensions:
        ext_id = str(ext.get("id"))
        name = ext.get("name") or ""
        number = ext.get("number") or ""

        ext_devices = devices_by_ext.get(ext_id, [])
        if not ext_devices:
            skipped += 1
            continue

        try:
            profile = get_extension_profile(cfg, ext_id)
        except requests.HTTPError as err:
            print(f"  WARN: profile lookup failed for ext {number or ext_id}: {err}")
            continue

        b_key, b_name = branch_key(profile.get("branch"))
        online = any(device_is_online(d) for d in ext_devices)
        device_labels = [d.get("name") or d.get("mac") or "?" for d in ext_devices]

        branch_entry = branches.setdefault(
            b_key,
            {"id": b_key, "name": b_name, "extensions": []},
        )
        branch_entry["extensions"].append(
            {
                "id": ext_id,
                "number": number,
                "name": name,
                "status": "online" if online else "offline",
                "devices": device_labels,
            }
        )

    for b in branches.values():
        online_count = sum(1 for e in b["extensions"] if e["status"] == "online")
        offline_count = len(b["extensions"]) - online_count
        b["online_count"] = online_count
        b["offline_count"] = offline_count
        b["total_count"] = len(b["extensions"])
        b["status"] = "offline" if online_count <= test_threshold else "online"

    if verbose:
        for b in sorted(branches.values(), key=lambda x: x["name"].lower()):
            print(
                f"  Branch: {b['name']}  ({b['online_count']}/{b['total_count']} online) "
                f"-> {b['status']}"
            )
            for e in sorted(b["extensions"], key=lambda x: (x["number"] or "")):
                print(
                    f"    {e['number'] or '(no num)':>8} {e['name'][:30]:30} "
                    f"-> {e['status']}  [{', '.join(e['devices'])}]"
                )
    if skipped:
        print(f"  ({skipped} extension(s) had no device record and were skipped)")
    return branches


def find_branch_transitions(prev: dict, curr: dict) -> list[dict]:
    """Branches that were online last time and are offline now."""
    dropped = []
    prev_branches = prev.get("branches", {}) if isinstance(prev, dict) else {}
    for b_key, curr_branch in curr.items():
        if curr_branch["status"] != "offline":
            continue
        prev_branch = prev_branches.get(b_key)
        if prev_branch and prev_branch.get("status") == "online":
            dropped.append(
                {
                    "id": b_key,
                    "name": curr_branch.get("name"),
                    "online_count": curr_branch["online_count"],
                    "offline_count": curr_branch["offline_count"],
                    "total_count": curr_branch["total_count"],
                    "extensions": curr_branch["extensions"],
                    "previously_seen_at": prev_branch.get("checked_at"),
                }
            )
    return dropped


def build_alert_email(
    cfg: dict, dropped_branches: list[dict], summary: dict, checked_at: str, test_threshold: int
) -> EmailMessage:
    lines = [
        "Juvo branch-offline alert",
        "",
        f"Checked at: {checked_at}",
        f"Tenant ID:  {cfg['tenant_id']}",
    ]
    if test_threshold > 0:
        lines.append(
            f"(TEST mode: a branch was flagged offline when {test_threshold} or fewer "
            f"phones were online. In production the threshold is 0.)"
        )
    lines += [
        "",
        f"Account summary: {summary.get('onlineExtensions', '?')} online / "
        f"{summary.get('offlineExtensions', '?')} offline / "
        f"{summary.get('totalExtensions', '?')} total",
        "",
        f"{len(dropped_branches)} branch(es) went from online to offline since the last check:",
        "",
    ]
    for b in dropped_branches:
        lines.append(
            f"  * {b.get('name')} "
            f"({b['online_count']}/{b['total_count']} phones online, "
            f"{b['offline_count']} offline)"
        )
        for e in b.get("extensions", []):
            label = " ".join(filter(None, [e.get("number"), e.get("name")]))
            devs = e.get("devices") or []
            dev_note = f"  [device: {', '.join(devs)}]" if devs else ""
            lines.append(f"      - {label or e['id']} -> {e['status']}{dev_note}")
    lines += [
        "",
        "This is an internal Juvo test. No external customer is affected by or notified of this alert.",
    ]

    msg = EmailMessage()
    msg["Subject"] = f"[Juvo test] {len(dropped_branches)} branch(es) offline"
    msg["From"] = cfg["smtp_from"] or cfg["smtp_username"] or "phone-alert@juvo.local"
    msg["To"] = ", ".join(cfg["recipients"]) if cfg["recipients"] else "no-recipients-configured@juvo.local"
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
    parser.add_argument(
        "--test-threshold",
        type=int,
        default=0,
        metavar="N",
        help=(
            "Test only: flag a branch as offline when N or fewer phones are online. "
            "Default 0 (strict 'all phones offline'). Leave off in production."
        ),
    )
    args = parser.parse_args()

    cfg = load_config()
    checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    print(f"[{checked_at}] Checking VOXO tenant {cfg['tenant_id']}")
    if args.test_threshold > 0:
        print(f"  TEST mode: branch offline when {args.test_threshold} or fewer phones online")

    try:
        summary = get_summary(cfg)
    except requests.HTTPError as err:
        sys.exit(f"Summary call failed: {err}")

    print(
        f"  Summary: {summary.get('onlineExtensions')} online / "
        f"{summary.get('offlineExtensions')} offline / "
        f"{summary.get('totalExtensions')} total"
    )

    current_branches = build_current_state(cfg, args.verbose, args.test_threshold)

    prev_state = load_state(args.state_file)
    dropped = find_branch_transitions(prev_state, current_branches)

    if not prev_state:
        print("  First run - no prior state, so no transitions can be reported.")
    elif dropped:
        print(f"  {len(dropped)} branch(es) went online -> offline since last check:")
        for b in dropped:
            print(
                f"    * {b['name']}  "
                f"({b['online_count']}/{b['total_count']} online, {b['offline_count']} offline)"
            )
    else:
        print("  No branches went from online to fully-offline since last check.")

    for b in current_branches.values():
        b["checked_at"] = checked_at

    save_state(
        args.state_file,
        {
            "tenant_id": cfg["tenant_id"],
            "checked_at": checked_at,
            "test_threshold": args.test_threshold,
            "branches": current_branches,
        },
    )
    print(f"  State written to {args.state_file}")

    if dropped:
        msg = build_alert_email(cfg, dropped, summary, checked_at, args.test_threshold)
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
