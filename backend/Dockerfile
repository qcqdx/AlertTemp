FROM python:3.12-slim

WORKDIR /srv/coldwatch

COPY pyproject.toml ./
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./

RUN pip install --no-cache-dir .

EXPOSE 8000

# миграции применяются на старте: контейнер всегда работает с актуальной схемой
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
