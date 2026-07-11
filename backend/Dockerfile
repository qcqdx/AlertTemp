FROM python:3.12-slim

WORKDIR /srv/coldwatch

COPY pyproject.toml ./
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini entrypoint.sh ./

RUN pip install --no-cache-dir . && chmod +x entrypoint.sh

EXPOSE 8000

# entrypoint ждёт фактической готовности БД, применяет миграции и стартует API
CMD ["./entrypoint.sh"]
