import os

from dotenv import load_dotenv


load_dotenv()


OPENPROJECT_URL = os.getenv("OPENPROJECT_URL", "").rstrip("/")
API_KEY = os.getenv("OPENPROJECT_API_KEY", "").strip()
APP_HOST = os.getenv("APP_HOST", "0.0.0.0")
APP_PORT = int(os.getenv("APP_PORT", "5001"))
