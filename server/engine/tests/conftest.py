import os

# Operator notifications (ntfy/Telegram) must never fire from the test suite,
# even on a machine whose .env has NTFY_TOPIC/TELEGRAM_* configured.
os.environ["BIONICO_NOTIFY"] = "0"
