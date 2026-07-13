# ColdWatch

Система контроля холодовой цепи: непрерывный мониторинг температуры в
холодильниках с медицинскими препаратами, визуализация, пороговые оповещения
и поддержка решения «можно ли применять препарат».

Полная переработка проекта AlertTemp — концепция и архитектура описаны в
[docs/CONCEPT.md](docs/CONCEPT.md), оставшиеся этапы и каталог паттернов —
в [docs/ROADMAP.md](docs/ROADMAP.md). Прежний код сохранён в [legacy/](legacy/)
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

По умолчанию порт 8000 слушается только на `127.0.0.1` — наружу приложение
выставляется через TLS-терминатор, а не напрямую.

### TLS (reverse-proxy)

Профиль `tls` поднимает Caddy перед приложением (порты 80/443, автоматический
сертификат Let's Encrypt для публичного домена; для закрытой техсети —
`tls internal` в `deploy/Caddyfile`). В `.env`:

```bash
COLDWATCH_PUBLIC_HOST=coldwatch.example.org
COLDWATCH_SESSION_COOKIE_SECURE=true   # cookie сессии только по HTTPS
```

```bash
docker compose --profile tls up -d      # app + db + mqtt + caddy
```

WebSocket (`/api/v1/ws`, живое обновление дашборда) проксируется прозрачно.

### Первый вход и смена пароля

Логин `admin`, пароль из `COLDWATCH_ADMIN_PASSWORD` (или сгенерированный в лог
при первом старте). После входа сразу смените его: «Администрирование» →
«Сменить пароль» — смена отзывает все прежние сессии.

## Разработка

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest                # тесты (SQLite, без внешних сервисов)
.venv/bin/ruff check app tests  # линтер
```

Локальный стек с брокером и БД: `docker compose -f deploy/docker-compose.yml up db mqtt`,
затем `uvicorn app.main:app --reload` c `COLDWATCH_DATABASE_URL` из `.env`.

Frontend (dev): `cd frontend && npm install && npm run dev` — vite на :5173
проксирует API в backend на :8000.

Эмулятор контроллеров (реалистичные данные + сценарии аварий):

```bash
backend/.venv/bin/python devtools/controller_sim.py --host localhost --controllers 2
backend/.venv/bin/python devtools/controller_sim.py --scenario overheat:1/2 --scenario door:1
```

## Развёртывание: Telegram

Боту нужен ВЫДЕЛЕННЫЙ токен: getUpdates (ack-кнопки) конфликтует с любым
другим потребителем того же токена (409 Conflict). В группах бот должен быть
администратором группы (или участникам разрешена отправка) — иначе доставка
молча не происходит; клиентские надписи о правах могут отставать, проверяйте
фактом отправки («Отправить тестовое» в админке).

## Статус

Готово и проверено стендом: сбор данных, обнаружение и управление
устройствами, rule engine (пороги с гистерезисом и задержками,
offline-детекция; **заморожен** на статистике реальных циклов разморозки —
см. ROADMAP §0), инциденты с подтверждением (в т.ч. ack-кнопкой в Telegram),
оповещения с эскалацией кругами, напоминаниями и heartbeat (SOCKS5),
качество хранения (MKT, бюджет стабильности, basis=current/historical),
печатные формы с отчётом по экскурсии и CSV-выгрузками, партии препаратов
(расчёт качества от даты загрузки), журнал аудита, аутентификация с ролями
(admin/operator/viewer), web-интерфейс.

Вход: логин `admin`, пароль из `COLDWATCH_ADMIN_PASSWORD` (или сгенерированный
в логе контейнера при первом старте: `docker compose logs app | grep password`).

Оставшееся и готовые протоколы приёмки — в
[docs/ROADMAP.md](docs/ROADMAP.md), §6 «Капсула преемника».
