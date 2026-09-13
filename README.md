# 🤖 Telegram Модератор Бот с ИИ

Telegram-бот для автоматической модерации супергрупп с использованием **Gemini 1.5 Flash** (Google AI Studio). Включает систему предупреждений, автоматический мут, а также встроенное **Telegram Web App** с игрой Тетрис и админ-панелью.

## ✨ Возможности

- **ИИ-модерация** — автоматический анализ сообщений на мат, токсичность, спам и рекламу через Gemini 1.5 Flash.
- **Система предупреждений** — 1-е нарушение = предупреждение, 2-е нарушение = мут на 24 часа.
- **Экономия API** — первичный фильтр пропускает короткие сообщения без ссылок.
- **Игнорирование админов** — сообщения администраторов и ботов не проверяются.
- **Web App (Mini App):**
  - 🎮 Полноценный Тетрис на HTML5 Canvas с сенсорным управлением.
  - 🛡️ Админ-панель для ручного управления (предупреждение, мут, размут).

## 🛠 Технологии

| Компонент | Технология |
|-----------|-----------|
| Бэкенд | Python 3.11+, aiogram 3.x |
| База данных | SQLite (aiosqlite) |
| ИИ | Gemini 1.5 Flash (google-genai SDK) |
| Фронтенд | Vanilla JS, HTML5 Canvas, Telegram WebApp SDK |

## 🚀 Установка и запуск

### 1. Клонируйте репозиторий
```bash
git clone <repo-url>
cd telegram-moderation-bot
```

### 2. Создайте виртуальное окружение
```bash
python -m venv venv
source venv/bin/activate        # Linux / macOS
# venv\Scripts\activate         # Windows
```

### 3. Установите зависимости
```bash
pip install -r requirements.txt
```

### 4. Настройте переменные окружения
```bash
cp .env.example .env   # Linux / macOS
# copy .env.example .env  # Windows
```

Заполните `.env`:
- **`BOT_TOKEN`** — получите у [@BotFather](https://t.me/BotFather).
- **`GEMINI_API_KEY`** — получите в [Google AI Studio](https://aistudio.google.com/apikey).
- **`ADMIN_IDS`** — через запятую ID администраторов (узнать свой ID: [@userinfobot](https://t.me/userinfobot)).

### 5. Настройте бота в группе
Добавьте бота в супергруппу как **администратора** с правами:
- ✅ Удаление сообщений
- ✅ Ограничение участников

### 6. Запустите
```bash
python -m bot.main
```

## 🌐 Развёртывание Web App

Web App — это статичный HTML-файл, который нужно разместить на HTTPS-хостинге.

### GitHub Pages
1. Создайте репозиторий и загрузите папку `webapp/`.
2. Включите GitHub Pages в настройках репозитория.
3. URL будет: `https://<username>.github.io/<repo>/webapp/index.html`

### Vercel
1. Установите [Vercel CLI](https://vercel.com/docs/cli): `npm i -g vercel`
2. Выполните `vercel` в директории `webapp/`.
3. Получите HTTPS-ссылку.

### Подключение к боту
1. Откройте [@BotFather](https://t.me/BotFather) → выберите бота.
2. **Bot Settings** → **Menu Button** → укажите URL вашего Web App.

## 📁 Структура проекта

```
telegram-moderation-bot/
├── .env.example            # Шаблон переменных окружения
├── requirements.txt        # Зависимости Python
├── README.md               # Документация
├── bot/                    # Бэкенд (Python)
│   ├── __init__.py
│   ├── main.py             # Точка входа, запуск polling
│   ├── config.py           # Загрузка конфигурации из .env
│   ├── database.py         # Асинхронная работа с SQLite
│   ├── ai_moderator.py     # Клиент Gemini API, анализ сообщений
│   └── handlers.py         # Обработчики сообщений и команд
└── webapp/                 # Фронтенд (Telegram Mini App)
    └── index.html          # Тетрис + Админ-панель
```

## 📝 Лицензия

MIT
