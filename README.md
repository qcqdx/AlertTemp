# ColdWatch

Система контроля холодовой цепи: непрерывный мониторинг температуры в
холодильниках с медицинскими препаратами, визуализация, пороговые оповещения
и поддержка решения «можно ли применять препарат».

Полная переработка проекта AlertTemp — концепция и архитектура описаны в
[docs/CONCEPT.md](docs/CONCEPT.md). Прежний код сохранён в [legacy/](legacy/)
как справочный материал по взаимодействию с железом.

## Как это устроено

Контроллеры (один контроллер = три датчика температуры) публикуют показания
в MQTT-брокер. ColdWatch принимает их, хранит в PostgreSQL/TimescaleDB и
отдаёт через REST API и web-интерфейс.

```
контроллеры ──MQTT──► ingest ──► PostgreSQL/TimescaleDB ◄── API/UI
                        │
                        └─► очередь обнаружения новых датчиков
```

Контроллер в эфире себя не обозначает — видны только id датчиков. Новые id
попадают в очередь обнаружения, администратор привязывает их к логическому
контроллеру и назначает человекочитаемые псевдонимы.

## Запуск (Docker)

```bash
cd deploy
cp .env.example .env    # заполнить COLDWATCH_DB_PASSWORD и адрес брокера
docker compose up -d --build
curl http://localhost:8000/healthz
```

API-документация: http://localhost:8000/docs

## Разработка

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest                # тесты (SQLite, без внешних сервисов)
.venv/bin/ruff check app tests  # линтер
```

Локальный стек с брокером и БД: `docker compose -f deploy/docker-compose.yml up db mqtt`,
затем `uvicorn app.main:app --reload` c `COLDWATCH_DATABASE_URL` из `.env`.

Эмулятор контроллеров (реалистичные данные + сценарии аварий):

```bash
backend/.venv/bin/python devtools/controller_sim.py --host localhost --controllers 2
backend/.venv/bin/python devtools/controller_sim.py --scenario overheat:1/2 --scenario door:1
```

## Статус

Идёт фаза 1 (см. [план](docs/CONCEPT.md#12-план-реализации)): сбор данных,
обнаружение датчиков, управление устройствами, API измерений. Детекция
инцидентов, оповещения и web-интерфейс — фазы 2–3.

⚠️ Аутентификация появляется в фазе 2: пока API должен быть доступен только
из доверенной сети (в docker-compose порт открыт только на localhost).
