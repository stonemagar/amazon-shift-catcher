import sys
from pathlib import Path

from dotenv import load_dotenv


# When this file is executed directly with:
#   python tools\test_notification.py
# Python places the tools directory on sys.path, not the repository root.
# Add the project root explicitly so amazon_shift_catcher can be imported.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from amazon_shift_catcher.notifier import send_notification


load_dotenv(ROOT / ".env")

ok = send_notification(
    "Test successful. Your Amazon Shift Catcher phone notification is working.",
    title="Amazon Shift Catcher TEST",
    priority=5,
)

if ok:
    print("Notification sent successfully.")
else:
    print("Notification failed or no provider is configured. Check your .env file and the log output above.")
