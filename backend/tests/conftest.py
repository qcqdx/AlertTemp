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


async def make_user(session_factory, username, password, role):
    from app.core.security import hash_password
    from app.models.users import User

    async with session_factory() as session:
        session.add(
            User(
                username=username,
                password_hash=hash_password(password),
                full_name=username,
                role=role,
            )
        )
        await session.commit()


def build_app(settings, ingest):
    app = create_app(settings)
    # обходим lifespan (он инициализирует свой движок и MQTT) — в тестах
    # окружение собирается фикстурами
    app.state.ingest_service = ingest
    return app


async def login_client(app, username, password):
    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://test")
    response = await client.post(
        "/api/v1/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text
    return client


@pytest.fixture
async def client(engine, settings, ingest, session_factory):
    """Клиент, аутентифицированный администратором."""
    from app.models.users import UserRole

    await make_user(session_factory, "admin", "admin-pass-123", UserRole.ADMIN)
    app = build_app(settings, ingest)
    client = await login_client(app, "admin", "admin-pass-123")
    yield client
    await client.aclose()
