from dotenv import load_dotenv

from amazon_shift_catcher.notifier import send_notification


load_dotenv()

ok = send_notification(
    "Test successful. Your Amazon Shift Catcher phone notification is working.",
    title="Amazon Shift Catcher TEST",
    priority=5,
)

if ok:
    print("Notification sent successfully.")
else:
    print("Notification failed or no provider is configured. Check your .env file.")
