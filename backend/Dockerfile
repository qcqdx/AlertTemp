# контекст сборки — корень репозитория (см. deploy/docker-compose.yml)

# --- этап 1: сборка web-интерфейса ---
FROM node:22-slim AS ui
WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# --- этап 2: backend + статика ---
FROM python:3.12-slim

WORKDIR /srv/coldwatch

COPY backend/pyproject.toml ./
COPY backend/app ./app
COPY backend/migrations ./migrations
COPY backend/alembic.ini backend/entrypoint.sh ./
COPY --from=ui /ui/dist ./static

RUN pip install --no-cache-dir . && chmod +x entrypoint.sh

EXPOSE 8000

# entrypoint ждёт фактической готовности БД, применяет миграции и стартует API
CMD ["./entrypoint.sh"]
