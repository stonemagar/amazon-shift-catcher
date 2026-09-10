import os
import sys
import requests
from dotenv import load_dotenv

load_dotenv()

token = os.getenv("TELEGRAM_BOT_TOKEN")
if not token:
    print("TELEGRAM_BOT_TOKEN is missing from .env")
    sys.exit(1)

url = f"https://api.telegram.org/bot{token}/getUpdates"
response = requests.get(url, timeout=15)
response.raise_for_status()
data = response.json()

results = data.get("result", [])
if not results:
    print("No Telegram messages found yet.")
    print("Open your bot in Telegram, press Start, send a message such as: hello")
    print("Then run this script again.")
    sys.exit(0)

seen = set()
for item in results:
    message = item.get("message") or item.get("channel_post") or item.get("edited_message")
    if not message:
        continue
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    if chat_id in seen or chat_id is None:
        continue
    seen.add(chat_id)
    label = chat.get("username") or chat.get("title") or chat.get("first_name") or "unknown"
    print(f"Chat ID: {chat_id} | {label}")
