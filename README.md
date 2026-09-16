# Amazon Shift Catcher

Personal Amazon UK Candidate Self Service shift monitor.

## Current behaviour

- Reuses a dedicated Brave session through CDP.
- Login, OTP, CAPTCHA, security checks, identity checks and Right to Work steps stay manual.
- Monitors the Amazon available-schedule page.
- Does not impose an Amazon desired-hours filter during normal monitoring.
- Reads every shift Amazon makes visible to the application.
- The hours chosen at startup are a **priority**, not a maximum.
- Exact-priority shifts are highlighted locally without physically moving Amazon's cards.
- Amazon's original shift cards remain clickable.
- Stops refreshing immediately when a shift batch is detected.
- Plays an alarm, sends an ntfy notification and captures screenshots.
- After shift detection, captures the live shift page approximately once per second while the same shift page remains visible.
- Detects CloudFront/403 pages, pauses normal monitoring, waits 60 seconds and makes one recovery test.
- Stops on security/verification pages rather than attempting to bypass them.

## Monitor states

The application records explicit monitor states:

```text
STARTING
MONITORING
SHIFT_FOUND
PAUSED_403
RECOVERING
SECURITY_CHECK
ERROR
STOPPED
```

State changes are written to the normal log and to the session metrics file.

## Session metrics

Every run saves structured JSON metrics under `logs/`:

```text
logs/session-metrics-YYYYMMDD-HHMMSS.json
logs/latest-session-metrics.json
```

Metrics include:

- total monitoring checks
- average and maximum observed check interval
- reload count, average reload time and maximum reload time
- 403 count, measured recovery duration and recovery-test count
- shift batches seen
- priority-shift batches seen
- visible shifts seen
- screenshots saved
- notifications sent
- errors and final stop reason
- state-transition history

This makes it possible to tune the monitor from measured behaviour instead of guesses.

## Setup

1. Install Python 3.11+.
2. Open PowerShell in the project folder.
3. Create and activate a virtual environment if desired:

```powershell
python -m venv .venv
.venv\Scripts\activate
```

4. Install dependencies:

```powershell
pip install -r requirements.txt
python -m playwright install chromium
```

5. Copy the example files if this is a new setup:

```powershell
copy .env.example .env
copy config.example.json config.json
```

6. Configure notifications in `.env` and keep that file private.
7. Start the dedicated Brave session, log into Amazon manually, and leave it open.
8. Run:

```powershell
python main.py
```

At startup choose 10, 20, 30 or 40 hours as your **highest-priority weekly hours**. Other visible shifts are still detected.

## Tests

Run the monitoring unit tests with:

```powershell
python -m unittest discover -s tests -v
```

The tests currently cover priority ranking, state transitions and metrics persistence.

## Privacy and safety

Keep the repository private. Never commit `.env`, `config.json`, browser profiles, logs, screenshots, cookies, passwords, OTPs, security codes or personal documents.

This project does **not** automate assessments, CAPTCHAs, OTPs, identity checks, right-to-work checks, security challenges or unfamiliar declarations.
