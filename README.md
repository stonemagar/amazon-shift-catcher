# Amazon Shift Catcher

Version 1 starter for monitoring the Amazon UK Candidate Self Service shift-selection page.

## Version 1 goals

- Reuse a dedicated logged-in browser profile.
- Navigate from **My Jobs** to **Select Shift**.
- Repeatedly check for available shift cards.
- Play a loud Windows alarm as soon as a shift appears.
- Send a Telegram notification.
- Read shift-card text and compare it with your rules.
- Optionally click **Apply** on the best matching shift.
- Save screenshots and logs.
- Stop on security/verification pages instead of trying to bypass them.

## Important

This project does **not** automate assessments, CAPTCHAs, OTPs, identity checks, right-to-work checks, or unfamiliar declarations.

Start with `AUTO_APPLY=false` until your rules and selectors have been tested against the live page.

## Setup

1. Install Python 3.11+.
2. Open a terminal in this folder.
3. Create a virtual environment:

```powershell
python -m venv .venv
.venv\Scripts\activate
```

4. Install dependencies:

```powershell
pip install -r requirements.txt
python -m playwright install chromium
```

5. Copy the example files:

```powershell
copy .env.example .env
copy config.example.json config.json
```

6. Put your Telegram bot token and chat ID in `.env`.
7. Edit `config.json` with your shift rules.
8. Run:

```powershell
python main.py
```

On the first run, a browser opens using the local `browser-profile` folder. Log in to Amazon manually if needed. The profile is stored locally and is excluded from Git.

## GitHub privacy

Keep the repository private. Never commit `.env`, `config.json`, `browser-profile/`, logs, screenshots, cookies, passwords, OTPs, or personal documents.
