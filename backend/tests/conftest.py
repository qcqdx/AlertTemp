import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core import db as core_db
from app.core.bus import EventBus
from app.core.config import Settings
from app.ingest.service import IngestService
from app.main import create_app
from app.models import Base


@pytest.fixture
def settings() -> Settings:
    return Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        mqtt_enabled=False,
        ingest_flush_interval_s=0.05,
    )


@pytest.fixture
async def engine(settings):
    engine = create_async_engine(settings.database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # подменяем глобальный движок, чтобы FastAPI-зависимости работали с тестовой БД
    core_db._engine = engine
    core_db._session_factory = async_sessionmaker(engine, expire_on_commit=False)
    yield engine
    await engine.dispose()
    core_db._engine = None
    core_db._session_factory = None


@pytest.fixture
def session_factory(engine):
    return core_db.session_factory()


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def ingest(session_factory, settings, bus) -> IngestService:
    return IngestService(session_factory, settings, bus)


@pytest.fixture
async def client(engine, settings, ingest):
    app = create_app(settings)
    # обходим lifespan (он инициализирует свой движок и MQTT) — в тестах
    # окружение собирается фикстурами
    app.state.ingest_service = ingest
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
