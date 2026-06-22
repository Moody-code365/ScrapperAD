# ScrapperAD — образ Telegram-бота.
# Бот работает на long-polling: вебхуки и проброс портов не нужны.

FROM python:3.12-slim

# .pyc не пишем, логи сразу в stdout (видно в `docker compose logs`)
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Сначала зависимости — слой кэшируется, пока не меняется requirements.txt
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Затем весь код (шрифты для PDF в assets/fonts копируются вместе со всем)
COPY . .

# Каталог под базу и кэш (в проде обычно монтируется томом — см. docker-compose.yml)
RUN mkdir -p data/cache

CMD ["python", "main.py"]
