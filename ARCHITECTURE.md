# ScrapperAD — Architecture & Decision Log

Полное описание того, как устроен бот, какие решения принимались и почему.
Документ предназначен для разработчиков, контрибьюторов и форков.

---

## Обзор

ScrapperAD — Telegram-бот для слежки за рекламой конкурентов. Собирает активные объявления из **Meta Ad Library** (Facebook/Instagram) через Apify, делает **AI-разбор** через Gemini, генерирует **PDF/TXT отчёты**. Монетизация — Telegram Stars (встроенные платежи) + ручная выдача тарифов для Enterprise.

**Стек:**
- Python 3.12
- aiogram 3.28 (Telegram Bot API, async)
- Apify Client (парсинг Meta Ad Library)
- Google Gemini 2.5 Flash (AI-анализ)
- fpdf2 + DejaVuSans (генерация PDF с кириллицей)
- SQLite + WAL (база данных)
- Docker + docker-compose (деплой)

---

## Структура проекта

```
ScrapperAD/
├── main.py                  # точка входа: polling, роутеры, команды бота
├── config/
│   └── settings.py          # .env → Settings dataclass; тарифные планы (PLANS)
├── database/
│   └── db.py                # SQLite: users, competitors, analyses, WAL-mode
├── bot/
│   ├── keyboards.py         # все inline-клавиатуры (InlineKeyboardBuilder)
│   └── handlers/
│       ├── start.py         # /start, меню, тарифы, оплата, white-label, админка
│       └── spy.py           # добавление конкурентов, анализ, отчёты, AI
├── services/
│   ├── ad_spy.py            # интеграция с Apify; кэш результатов
│   ├── apify_pool.py        # пул Apify-ключей с автопереключением
│   └── ai_review.py         # Gemini: AI-разбор и анализ ниши
├── reports/
│   ├── ad_report.py         # TXT-отчёт
│   └── pdf_report.py        # PDF-отчёт (fpdf2)
├── assets/fonts/            # DejaVuSans для кириллицы в PDF
├── scripts/                 # dev-утилиты (выдать тариф, дамп данных)
├── Dockerfile
├── docker-compose.yml
└── .env.example
```

---

## Архитектурные решения

### 1. Polling, не Webhook

Бот работает в режиме **long polling** (`dp.start_polling`), не webhook. Причины:
- Не нужен публичный HTTPS-домен и SSL-сертификат
- Проще деплоить на любой VPS (`docker compose up -d` и всё)
- Для одного инстанса polling вполне достаточен по производительности
- Webhook усложняет локальную разработку (нужен ngrok или аналог)

### 2. Inline-клавиатуры вместо команд

Весь UX строится на **inline-кнопках** (callback_data), а не на текстовых командах. Пользователь нажимает кнопки, а не печатает `/addcompetitor`. Команды существуют как алиасы (`/start`, `/plan` и т.д.) но основной флоу — кнопочный.

Это сознательное решение: кнопки дают предсказуемый UX, меньше ошибок ввода, легче строить флоу с состояниями (FSM).

### 3. FSM (конечный автомат) для диалогов

Добавление конкурента и настройка white-label — многошаговые диалоги. Используется **aiogram FSM** (`StatesGroup`, `MemoryStorage`):

```python
class AddCompetitor(StatesGroup):
    waiting_for_name = State()
    waiting_for_facebook = State()

class BrandStates(StatesGroup):
    waiting_name = State()
    waiting_logo = State()
```

`MemoryStorage` — состояния хранятся в памяти процесса. При перезапуске бота незавершённые диалоги сбрасываются. Для продакшена достаточно: диалог короткий (2 шага), незавершённые состояния не критичны.

### 4. HTML parse_mode везде

Все сообщения используют `ParseMode.HTML` (задан глобально через `DefaultBotProperties`). Markdown в aiogram 3 требует экранирования спецсимволов, что неудобно. HTML предсказуемее: экранируем динамику через `html.escape()` (алиас `_e()`), статику пишем напрямую с тегами `<b>`, `<i>`, `<code>`.

```python
def _e(x) -> str:
    return escape(str(x)) if x is not None else ""
```

### 5. SQLite + WAL

База данных — **SQLite** с включённым WAL-режимом:

```python
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA foreign_keys=ON")
```

WAL позволяет параллельное чтение при записи — важно для async-бота, где несколько пользователей могут одновременно делать запросы. Для масштаба одного Telegram-бота SQLite полностью достаточен.

Файл базы: `data/scrapperad.db` (путь задаётся в `.env`). В Docker смонтирован как volume — переживает пересборку образа.

**Схема:**
- `users` — telegram_id, username, full_name, plan, plan_expires_at, brand_name, brand_logo
- `competitors` — user_id (FK), name, facebook_page, is_active
- `analyses` — user_id, competitor_id, platform, ad_count, created_at

### 6. Дневной кэш результатов Apify

Каждый запрос к Apify стоит денег (~$0.006 за объявление). Чтобы не тратить лимиты и бюджет:

- Результат анализа кэшируется в `data/cache/<competitor_id>.json` на **24 часа**
- Повторные просмотры (карточки, PDF, AI-разбор) берут данные из кэша — **бесплатно**
- Кнопка «🔍 Собрать рекламу» проверяет возраст кэша; если < 24ч — отдаёт из кэша
- При выборе нового рекламодателя (`pick:`) — кэш перезаписывается только его объявлениями

```python
CACHE_FRESH_HOURS = 24

cached = load_cached_report(competitor_id)
age = cache_age_hours(cached.cached_at) if cached else None
if cached and age is not None and age < CACHE_FRESH_HOURS:
    # отдаём из кэша, лимит не тратим
```

### 7. Пул Apify-ключей

**Проблема:** у бесплатного Apify-аккаунта ~$5/мес. На старте без бюджета — мало.

**Решение:** `ApifyKeyPool` — класс, который держит список ключей и автоматически переключается на следующий при исчерпании лимита текущего.

```python
class ApifyKeyPool:
    def __init__(self, tokens, state_path=STATE_PATH): ...
    def available(self) -> list  # ключи с оставшимся лимитом
    def is_exhausted(self, token) -> bool
    def mark_exhausted(self, token) -> None  # сохраняет на диск
    def _maybe_reset_month(self)  # сбрасывает в начале нового месяца
```

Состояние «какой ключ исчерпан» сохраняется в `data/apify_keys.json` — переживает перезапуски. В начале нового месяца (Apify сбрасывает лимиты ежемесячно) состояние автоматически очищается.

**Детекция исчерпания** — намеренно узкая, чтобы избежать false positives:

```python
# HTTP 402 → точно лимит
# HTTP 429 → НЕ лимит (rate limit, не исчерпание)
# type содержит "usage-hard-limit" или "monthly-usage" → лимит
# message содержит "usage hard limit" / "out of credit" / "payment required" → лимит
```

Широкие маркеры типа `"insufficient"`, `"exceeded"` намеренно исключены — они могут совпасть с ошибками прав доступа (403) и ложно заблокировать рабочий ключ.

Настройка в `.env`:
```env
APIFY_API_TOKENS=key1,key2,key3
```

### 8. Тарифные планы — единый источник правды

Все лимиты и цены — в одном месте: `config/settings.py` → `PLANS`. Нигде в коде нет хардкода `"pro"` с числами — только обращение к `plan_limits(plan)`.

```python
PLANS = {
    "free":       { "competitors": 1,  "analyses_per_month": 1,  "ads_per_scan": 20,  "stars": 0,    ... },
    "start":      { "competitors": 5,  "analyses_per_month": 50, "ads_per_scan": 30,  "stars": 1300, ... },
    "pro":        { "competitors": 30, "analyses_per_month": 80, "ads_per_scan": 50,  "stars": 3300, ... },
    "enterprise": { "competitors": 50, "analyses_per_month": 20, "ads_per_scan": 1000,"stars": 0,    ... },
}
```

`ads_per_scan` — ключевой параметр контроля расходов на Apify. Чем больше, тем дороже каждый анализ. Enterprise с `ads_per_scan=1000` и `analyses_per_month=20` в худшем случае стоит ~$120/мес на Apify.

### 9. Оплата — Telegram Stars

**Почему Stars, не карты:** Stars не требуют юрлица, эквайринга, интеграции с банком. Для старта без ИП — единственный легальный вариант.

```python
await message.answer_invoice(
    currency="XTR",  # XTR = Telegram Stars
    provider_token="",  # пусто для Stars
    prices=[LabeledPrice(label=..., amount=p["stars"])],
)
```

После успешной оплаты — хук `on_paid` активирует тариф на 30 дней.

**Enterprise — не через Stars** (`stars=0`). Продаётся по запросу: кнопка → DM с преднабранным сообщением → администратор выдаёт вручную через `/grant`.

### 10. Оплата картой — DM, не публичный чат

Реквизиты (номер карты/Kaspi) **никогда не появляются в сообщениях бота** — это потенциально публичный контент, индексируемый пересылками. Вместо этого кнопка генерирует Telegram deep-link с преднабранным сообщением:

```python
url = f"https://t.me/{SUPPORT_USERNAME}?text={quote(prefill)}"
```

Пользователь нажимает «💬 Написать для оплаты» → открывается диалог с преднабранным текстом → администратор получает запрос в личку и отправляет реквизиты там.

Переменная `SUPPORT_USERNAME` задаётся в `.env`, в коде не хранится.

### 11. White-label PDF (Pro/Enterprise)

На тарифах Pro и Enterprise пользователь может задать **название агентства и логотип** — они появляются в шапке PDF-отчётов. Полезно для SMM-агентств, которые сдают отчёты клиентам.

Настройка через FSM (2 шага: название → фото логотипа). Логотип хранится как `file_id` Telegram в базе, скачивается в момент генерации PDF.

```python
async def _resolve_brand(bot, user):
    if active_plan(user) not in ("pro", "enterprise"):
        return "ScrapperAD", None
    brand = (user["brand_name"] or "").strip() or "ScrapperAD"
    logo_bytes = None
    if user["brand_logo"]:
        buf = BytesIO()
        await bot.download(user["brand_logo"], destination=buf)
        logo_bytes = buf.getvalue()
    return brand, logo_bytes
```

### 12. Контекстное главное меню

Главное меню динамически адаптируется к пользователю:
- Кнопка «🛠 Админка» — только для администратора
- Кнопка «🏷 Мой бренд в PDF» — только для Pro/Enterprise

```python
def main_menu(is_admin=False, can_brand=False) -> InlineKeyboardMarkup:
    ...
    if can_brand:
        kb.button(text="🏷 Мой бренд в PDF", callback_data="brand")
    ...
    if is_admin:
        kb.button(text="🛠 Админка", callback_data="admin")
```

### 13. AI-разбор — Gemini 2.5 Flash

Для анализа рекламы используется **Gemini 2.5 Flash** (бесплатный тир достаточен). Перед отправкой в модель — скачиваем до 3 изображений из объявлений для мультимодального анализа.

`thinkingBudget=0` — отключает «размышления» модели (экономит токены, скорость выше).

Кэшируем результат AI-разбора в `data/cache/<competitor_id>_ai.txt`. При новом сборе рекламы — кэш AI удаляется (данные изменились → анализ устарел).

**AI-фолбэк:** если по Instagram-нику ничего не нашли в Meta Ad Library, Gemini угадывает официальное название бренда и делается второй запрос уже по нему.

### 14. Несколько рекламодателей в выдаче

Поиск по нику/названию в Meta Ad Library иногда возвращает объявления нескольких разных компаний. Вместо того чтобы мешать их в кучу — показываем **пикер**:

```python
pages = {}
for ad in ads:
    if ad.page_id:
        name, cnt = pages.get(ad.page_id, (ad.page_name or "—", 0))
        pages[ad.page_id] = (ad.page_name or name, cnt + 1)
if len(pages) > 1:
    # показать кнопки выбора рекламодателя
```

После выбора — URL конкурента обновляется на точную ссылку `view_all_page_id=<page_id>`. Следующие анализы уже идут напрямую без неоднозначности.

### 15. Асинхронность и блокирующие операции

Apify-клиент — синхронный. Вызов занимает 1–3 минуты. Если запустить его в основном цикле событий — бот зависнет для всех пользователей.

Решение: `asyncio.to_thread()` для всех блокирующих вызовов:

```python
report = await asyncio.to_thread(
    analyze_competitor,
    apify_tokens=_apify_pool,
    ...
)
```

То же — для генерации PDF (`build_pdf` блокирует на fpdf2).

### 16. Команды бота — разные наборы для разных пользователей

Используется `BotCommandScopeDefault` (для всех) и `BotCommandScopeChat` (для конкретного chat_id — администратора). Администратор видит расширенный список команд в меню «/».

```python
await bot.set_my_commands(common, scope=BotCommandScopeDefault())
await bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=admin_id))
```

---

## Поток данных

```
Пользователь нажимает «🔍 Собрать рекламу»
    │
    ├─▶ Проверка кэша (data/cache/<id>.json)
    │       └─ Свежий (< 24ч)? → отдать из кэша, конец
    │
    ├─▶ Проверка лимита тарифа (SQLite: count analyses this month)
    │       └─ Лимит исчерпан? → показать прошлые данные или ошибку
    │
    ├─▶ ApifyKeyPool.available() → выбрать рабочий ключ
    │
    ├─▶ asyncio.to_thread(analyze_competitor, ...)
    │       └─ Apify Actor: facebook-ads-scraper
    │       └─ Quota error? → mark_exhausted(token) → следующий ключ
    │
    ├─▶ AI-фолбэк (если 0 результатов + есть Gemini key)
    │       └─ suggest_brand_query() → повторный запрос
    │
    ├─▶ Успех: log_analysis() + cache_report() + delete_ai_review()
    │
    └─▶ Рендер результата пользователю
            ├─ Один рекламодатель → сводка + кнопки действий
            └─ Несколько рекламодателей → пикер
```

---

## Генерация PDF

`fpdf2` + шрифт **DejaVuSans** (поставляется в `assets/fonts/`). DejaVuSans необходим потому что стандартные шрифты fpdf не поддерживают кириллицу.

Структура PDF-отчёта:
1. Шапка (лого + название — ScrapperAD или white-label)
2. Заголовок: имя конкурента, дата, количество объявлений
3. Карточки объявлений: картинка (если есть) + текст + метаданные
4. Подвал на каждой странице

Для анализа ниши — `build_niche_pdf`: сводная таблица конкурентов + AI-текст.

PDF генерируется в памяти (`BytesIO`), на диск не пишется, сразу отправляется в Telegram как `BufferedInputFile`.

---

## Деплой

Docker-образ на основе `python:3.12-slim`. Бот не слушает порты (polling), поэтому `ports:` в docker-compose нет.

```yaml
services:
  bot:
    build: .
    restart: unless-stopped
    env_file: .env
    volumes:
      - ./data:/app/data   # база + кэш переживают пересборку
```

`data/` монтируется как volume — SQLite-база и кэш Apify сохраняются между обновлениями образа.

Обновление на сервере:
```bash
git pull
docker compose up -d --build
```

---

## Административные функции

Администратор определяется по `ADMIN_TELEGRAM_ID` в `.env`. Доступны:

| Команда | Действие |
|---|---|
| `/admin` | Открыть панель (также кнопка в меню) |
| `/grant <id> <план>` | Выдать тариф пользователю вручную |
| `/users` | Последние 20 пользователей с их тарифами |
| `/stats` | Статистика: пользователи, активность, анализы |

Enterprise выдаётся **исключительно** через `/grant` — в боте нет кнопки оплаты для него (`stars=0` блокирует Stars-платёж).

---

## Ограничения Meta Ad Library

Данные, которые **недоступны** через Ad Library (ни у кого, это политика Meta):
- Бюджет рекламы
- Количество показов
- Демографика (пол, возраст) для коммерческой рекламы
- Регионы показа для коммерческой рекламы

Доступны:
- Тексты и заголовки объявлений
- Картинки и видео (прямые ссылки, временные)
- Дата запуска и статус (активно/остановлено)
- Количество дней открутки
- Площадки (Facebook, Instagram, Messenger, Audience Network)
- Ссылка на Ad Library

---

## Известные тонкости

**Медиа-ссылки из Apify временные** — картинки и видео из Meta Ad Library имеют CDN-ссылки с TTL. Они валидны на момент сбора, но могут протухнуть через несколько часов/дней. Поэтому медиа скачиваем сразу при показе карточек, а не храним ссылки для отложенного скачивания.

**Instagram → Ad Library матчинг** — пользователи вводят Instagram-ник, но Apify-актор работает с Meta Ad Library URL. Конвертация через поиск: `instagram.com/brand` → `ads/library/?q=brand`. Иногда ник не совпадает с названием страницы → AI-фолбэк через Gemini.

**Лимит подписи Telegram — 1024 символа** — карточки объявлений с длинным текстом обрезаются бинарным поиском: ищем максимальную длину текста, при которой вся карточка влезает в 1024 символа. HTML-теги не рвутся.

**Видео из Meta** — иногда доступны HD/SD, иногда только превью. Пробуем в порядке: SD → HD → preview. Если совсем нет — отправляем только текст карточки.
