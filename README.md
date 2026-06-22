# ScrapperAD — Telegram-бот для слежки за рекламой конкурентов

Шпионишь за конкурентами в Facebook и Instagram: тексты, картинки, видео, как давно крутится объявление. Данные из официальной библиотеки рекламы Meta — честно и легально.

Built by [Moody](https://technopriest.net/)

---

## Что умеет

| Функция | Free | Start | Pro | Enterprise |
|---|:---:|:---:|:---:|:---:|
| Конкурентов | 1 | 5 | 30 | 50 |
| Анализов в месяц | 1 | 50 | 80 | 20 |
| Объявлений за скан | 20 | 30 | 50 | 1000 |
| PDF-отчёт | — | ✓ | ✓ | ✓ |
| AI-разбор (Gemini) | — | ✓ | ✓ | ✓ |
| White-label PDF | — | — | ✓ | ✓ |

- **🔍 Собрать рекламу** — активные объявления конкурента из Meta Ad Library
- **📋 Карточки в чат** — текст + креатив каждого объявления
- **🧠 AI-разбор** — Gemini смотрит на рекламу и пишет: оффер, хуки, как обойти
- **📑 PDF-отчёт** — красивый файл для клиента или архива
- **📄 TXT-отчёт** — все тексты и ссылки одним файлом
- **🎬 Все креативы** — скачать все видео и картинки конкурента
- **📊 Сводка** — все конкуренты на одном экране + динамика изменений
- **🌐 Анализ ниши** — сводный AI-разбор нескольких конкурентов + PDF
- **🏷 White-label** — своё лого и название в шапке PDF-отчётов (Pro/Enterprise)

---

## Быстрый старт

### 1. Создать бота

Написать [@BotFather](https://t.me/BotFather), создать бота, получить токен.

### 2. Получить ключи

- **Apify** — регистрация на [apify.com](https://apify.com), ключ в [настройках аккаунта](https://console.apify.com/account/integrations). Бесплатный план даёт ~$5/мес — хватит на старт. Можно добавить несколько аккаунтов, бот сам переключится при исчерпании лимита.
- **Gemini** — бесплатный ключ на [aistudio.google.com](https://aistudio.google.com/apikey).

### 3. Настроить .env

```bash
cp .env.example .env
# Отредактируй .env — вставь токены
```

Минимальный `.env` для запуска:
```env
BOT_TOKEN=...
APIFY_API_TOKENS=...
GEMINI_API_KEY=...
SUPPORT_USERNAME=your_telegram_username
ADMIN_TELEGRAM_ID=123456789
```

### 4. Запустить

**Docker (рекомендуется для сервера):**
```bash
docker compose up -d --build
```

**Локально:**
```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

---

## Структура проекта

```
ScrapperAD/
├── bot/
│   ├── handlers/
│   │   ├── start.py      # /start, меню, тарифы, оплата, white-label, админка
│   │   └── spy.py        # слежка за конкурентами, отчёты, AI-разбор
│   └── keyboards.py      # все inline-клавиатуры
├── config/
│   └── settings.py       # настройки из .env, тарифные планы
├── database/
│   └── db.py             # SQLite: пользователи, конкуренты, логи
├── reports/
│   ├── ad_report.py      # TXT-отчёт
│   └── pdf_report.py     # PDF (fpdf2 + DejaVuSans)
├── services/
│   ├── ad_spy.py         # Apify: сбор рекламы из Meta Ad Library
│   ├── ai_review.py      # Gemini: AI-разбор и анализ ниши
│   └── apify_pool.py     # пул Apify-ключей с автопереключением
├── Dockerfile
├── docker-compose.yml
└── .env.example
```

---

## Команды бота

| Команда | Описание |
|---|---|
| `/start` | Главное меню |
| `/addcompetitor` | Добавить конкурента |
| `/competitors` | Мои конкуренты |
| `/plan` | Тариф и оплата |
| `/brand` | Свой бренд в PDF (Pro/Enterprise) |
| `/help` | Как это работает |
| `/cancel` | Отменить действие |

**Команды администратора:**

| Команда | Описание |
|---|---|
| `/admin` | Открыть админ-панель |
| `/grant <id или @ник> <тариф>` | Выдать тариф пользователю |
| `/users` | Последние пользователи |
| `/stats` | Статистика бота |

Тарифы для `/grant`: `free`, `start`, `pro`, `enterprise`

---

## Пул Apify-ключей

Бот поддерживает несколько Apify-аккаунтов одновременно. Когда у одного кончается бесплатный лимит ($5/мес), автоматически переключается на следующий. Состояние сохраняется между перезапусками, в начале нового месяца сбрасывается.

```env
APIFY_API_TOKENS=apify_api_key1,apify_api_key2,apify_api_key3
```

---

## Тарифы и оплата

- **Free / Start / Pro** — оплата через Telegram Stars прямо в боте (автоматически)
- **Enterprise** — по запросу, выдаётся вручную через `/grant`
- **Картой / Kaspi** — пользователь пишет в личку, ты выдаёшь тариф через `/grant`

Цены и лимиты настраиваются в `config/settings.py` → `PLANS`.

---

## Деплой на сервер

```bash
# На сервере: склонировать репо, создать .env, запустить
git clone https://github.com/yourusername/ScrapperAD.git
cd ScrapperAD
cp .env.example .env
nano .env   # заполнить токены

docker compose up -d --build
docker compose logs -f   # смотреть логи
```

Данные хранятся в `./data/` — Volume смонтирован в docker-compose.yml, переживает перезапуски и обновления образа.

---

## Технологии

- [aiogram 3](https://docs.aiogram.dev/) — Telegram Bot API
- [Apify](https://apify.com/) — парсинг Meta Ad Library
- [Gemini 2.5 Flash](https://aistudio.google.com/) — AI-анализ
- [fpdf2](https://py-pdf.github.io/fpdf2/) — генерация PDF
- SQLite — хранилище данных
- Docker — деплой

---

## License

MIT — см. [LICENSE](LICENSE)

---

Made by [Moody](https://technopriest.net/)
