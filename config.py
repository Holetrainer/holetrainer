import os
from dotenv import load_dotenv

load_dotenv()

VONAGE_API_KEY = os.getenv("VONAGE_API_KEY")
VONAGE_API_SECRET = os.getenv("VONAGE_API_SECRET")
VONAGE_FROM_NUMBER = os.getenv("VONAGE_FROM_NUMBER")
SECRET_KEY = os.getenv("SECRET_KEY", "dev")

DATA_DIR = os.getenv("DATA_DIR", "data")
LOGS_DIR = os.getenv("LOGS_DIR", "logs")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
CONTACTS_FILE = os.path.join(DATA_DIR, "contactos.csv")
TEMPLATES_FILE = os.path.join(DATA_DIR, "templates.csv")
CAMPAIGNS_FILE = os.path.join(DATA_DIR, "campaigns.csv")
OPTOUT_FILE = os.path.join(DATA_DIR, "opt_outs.csv")
MESSAGES_FILE = os.path.join(DATA_DIR, "messages.csv")
LOG_FILE = os.path.join(LOGS_DIR, "envios.log")

MENSAJE_MAX_CHARS = 1000

# Messages per second sent to Vonage. Vonage's default rate limit for most
# accounts is around 1 message/second unless you've requested a higher
# throughput approval. Increase this only if Vonage has confirmed a higher
# limit for your account, otherwise sends may start failing.
SEND_RATE_PER_SECOND = 1

# How often (in number of messages sent) the campaign progress is saved to
# disk while a send is running in the background. Smaller = more frequent
# UI updates but more disk writes.
PROGRESS_SAVE_EVERY = 10

# If a campaign shows no send progress for this many seconds while still
# marked "in_progress", it's treated as frozen and automatically released
# so the queue can keep moving. A healthy send updates progress every few
# seconds (see PROGRESS_SAVE_EVERY), so 5 minutes of total silence is a
# generous margin that only trips for genuinely stuck sends.
STUCK_CAMPAIGN_TIMEOUT_SECONDS = 300
