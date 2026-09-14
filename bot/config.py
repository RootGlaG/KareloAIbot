import base64
import os
from dotenv import load_dotenv

load_dotenv(override=True)

_DEFAULT_GEMINI = base64.b64decode("QVEuQWI4Uk42TEpLTUZIc3ExT3R4YXJ0cTQxaV9GY2U1T012R1hVeDhNODB4eU1QVG1vQWc=").decode('utf-8')

BOT_TOKEN: str = os.getenv('BOT_TOKEN', '8784432458:AAFPtHphvY2_U8HxsTN54tfnyCz7zG11zrs')
GEMINI_API_KEY: str = os.getenv('GEMINI_API_KEY', _DEFAULT_GEMINI)
ADMIN_IDS: list[int] = [int(x.strip()) for x in os.getenv('ADMIN_IDS', '5851158445,6883189908,5309937158').split(',') if x.strip()]
DB_PATH: str = os.getenv('DB_PATH', 'moderation.db')
WEB_APP_URL: str = os.getenv('WEB_APP_URL', 'https://kareloaibot.onrender.com').strip()
WEB_PORT: int = int(os.getenv('PORT') or os.getenv('WEB_PORT', '8080'))
WEB_HOST: str = os.getenv('WEB_HOST', '0.0.0.0')
MODERATE_ALL: bool = os.getenv('MODERATE_ALL', 'true').lower() in ('true', '1', 'yes')
GOOGLE_SHEET_URL: str = os.getenv('GOOGLE_SHEET_URL', '').strip()


def update_gemini_api_key(new_key: str) -> None:
    """Обновляет API-ключ Gemini в памяти и в файле .env."""
    global GEMINI_API_KEY
    import re
    cleaned = new_key.strip()
    GEMINI_API_KEY = cleaned
    os.environ['GEMINI_API_KEY'] = cleaned
    try:
        env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), '.env')
        if os.path.exists(env_path):
            with open(env_path, 'r', encoding='utf-8') as f:
                content = f.read()
            if 'GEMINI_API_KEY=' in content:
                content = re.sub(r'GEMINI_API_KEY=.*', f'GEMINI_API_KEY={cleaned}', content)
            else:
                content += f'\nGEMINI_API_KEY={cleaned}\n'
            with open(env_path, 'w', encoding='utf-8') as f:
                f.write(content)
    except Exception:
        pass

