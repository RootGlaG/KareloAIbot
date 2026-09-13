import os
from dotenv import load_dotenv

load_dotenv(override=True)

BOT_TOKEN: str = os.getenv('BOT_TOKEN', '8784432458:AAFPtHphvY2_U8HxsTN54tfnyCz7zG11zrs')
GEMINI_API_KEY: str = os.getenv('GEMINI_API_KEY', 'AQ.Ab8RN6JrNfFV7Z0sWAPlZy8MN1CCB5OAdEPghpVfIwm91Pr2Qg')
ADMIN_IDS: list[int] = [int(x.strip()) for x in os.getenv('ADMIN_IDS', '5851158445,6883189908,5309937158').split(',') if x.strip()]
DB_PATH: str = os.getenv('DB_PATH', 'moderation.db')
WEB_APP_URL: str = os.getenv('WEB_APP_URL', 'https://kareloaibot.onrender.com').strip()
WEB_PORT: int = int(os.getenv('PORT') or os.getenv('WEB_PORT', '8080'))
WEB_HOST: str = os.getenv('WEB_HOST', '0.0.0.0')
MODERATE_ALL: bool = os.getenv('MODERATE_ALL', 'true').lower() in ('true', '1', 'yes')
