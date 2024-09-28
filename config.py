# config.py
import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
TENANT_ID = os.getenv("TENANT_ID")
TEAM_ID = os.getenv("TEAM_ID")
AUTHORITY = f"https://login.microsoftonline.com/consumers"
SCOPE = ["Mail.Read"]
CACHE_FILE = os.getenv("CACHE_FILE", "token_cache.json")

LOGIN_URL = 'https://users.premierleague.com/accounts/login/'
LOGIN_EMAIL = os.getenv("LOGIN_EMAIL")
LOGIN_PASSWORD = os.getenv("LOGIN_PASSWORD")
REDIRECT_URI = os.getenv("REDIRECT_URI")
