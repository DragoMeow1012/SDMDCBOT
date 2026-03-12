FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps

COPY . .
RUN mkdir -p /app/data && touch /app/data/chat_history.json

EXPOSE 8080

CMD ["python", "-u", "main.py"]
