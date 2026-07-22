# Juvo phone offline alert (internal test)

Step-one test of a possible future customer feature. Watches VOXO extension
registrations on **Juvo's own tenant only**, remembers what it saw last
time, and (when told to) emails you and Shane if any extension flipped
from online to offline between runs.

Not on a schedule yet. You run it manually while we build trust in it.

## What it does

1. Reads `.env` for the VOXO token, tenant ID, and email settings.
2. Calls `GET /v2/admin/extensions/summary` for a top-line count.
3. Lists voice extensions for the tenant and lists devices
   (`GET /v2/admin/devices/`). Each device has a `userAgent` field that
   VOXO leaves empty when the phone is not currently registered — that's
   the online/offline signal.
4. Matches devices to extensions (via each device's primary line) and
   marks an extension **online** if any of its devices is registered.
5. Loads `state.json` from the last run and compares.
6. For any extension that went **online -> offline**, drafts an alert email.
7. Writes the current status back to `state.json`.
8. Prints the email (dry-run) by default. Adds `--send` to actually deliver.

**Scope note:** this catches desk phones, ATAs, and paging horns —
anything that shows up as a device on VOXO. Softphone-only users
(no assigned device record) are skipped and logged as such. For the
Republic Finance use case (desk phones dropping offline), that's the
right target. If a customer later needs softphone tracking too, VOXO
will need to fix or clarify `/extensions/{id}/registrations` for us,
or we add device-less extension coverage another way.

## First-time setup

From this folder:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
notepad .env
```

Fill in `.env`:

- `VOXO_API_TOKEN` - the token you regenerated in PowerShell.
- `VOXO_TENANT_ID` - Juvo's tenant ID (numeric).
- `ALERT_RECIPIENTS` - your email and Shane's, comma-separated.
- SMTP settings - use a Microsoft 365 **app password** for this script,
  not your regular M365 password.

## Running it

Dry run (safe, no email sent):

```powershell
python check_phones.py
python check_phones.py --verbose
```

Actually deliver the alert email if a transition is found:

```powershell
python check_phones.py --send
```

The first run has no prior state to compare against, so it will just
write the baseline and report nothing. The second run onward is the
real test.

## What to test manually

1. Run once. Confirm the summary counts match what you see in the VOXO
   admin portal.
2. Pick a Juvo desk phone or softphone you can safely take offline
   (power it off, or sign out of the softphone).
3. Wait a minute for VOXO to see the registration drop, then run again
   with `--verbose`.
4. You should see that extension flagged as an online -> offline
   transition. Re-run with `--send` to confirm the email actually
   arrives at you and Shane.
5. Turn the phone back on and run again - it'll go back to online with
   no alert (offline -> online is not treated as an alert).

## Token expired?

If VOXO returns `401 Unauthorized`, the script stops with a clear
message. Regenerate the token in PowerShell and paste the new value
into `.env`. Re-run.

## Boundaries (intentional)

- Only reads Juvo's tenant. Nothing outside Juvo is touched.
- Emails only the recipients in `ALERT_RECIPIENTS`. No customer is
  contacted or notified.
- No credentials stored beyond what's in `.env` on your laptop, and
  `.env` is git-ignored.
- Nothing runs on a schedule. It only runs when you run it.

## Not built yet (later, if this proves out)

- Running on a schedule without your laptop staying open.
- Handling multiple tenants (Republic Finance has 260+). The current
  code assumes one tenant per run on purpose.
- Automatic token refresh (open question with VOXO).
