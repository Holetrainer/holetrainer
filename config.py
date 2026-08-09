import os
from dotenv import load_dotenv

load_dotenv()

VONAGE_API_KEY = os.getenv("VONAGE_API_KEY")
VONAGE_API_SECRET = os.getenv("VONAGE_API_SECRET")
VONAGE_FROM_NUMBER = os.getenv("VONAGE_FROM_NUMBER")
SECRET_KEY = os.getenv("SECRET_KEY", "dev")

DATA_DIR = "data"
LOGS_DIR = "logs"
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
CONTACTS_FILE = os.path.join(DATA_DIR, "contactos.csv")
TEMPLATES_FILE = os.path.join(DATA_DIR, "templates.csv")
CAMPAIGNS_FILE = os.path.join(DATA_DIR, "campaigns.csv")
OPTOUT_FILE = os.path.join(DATA_DIR, "opt_outs.csv")
LOG_FILE = os.path.join(LOGS_DIR, "envios.log")

MENSAJE_MAX_CHARS = 700
