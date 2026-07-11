"""Аутентификация и роли."""

from httpx import ASGITransport, AsyncClient

from app.models.users import UserRole
from tests.conftest import build_app, login_client, make_user


async def anon_client(settings, ingest):
    return AsyncClient(
        transport=ASGITransport(app=build_app(settings, ingest)), base_url="http://test"
    )


async def test_api_requires_auth(engine, settings, ingest):
    client = await anon_client(settings, ingest)
    assert (await client.get("/api/v1/controllers")).status_code == 401
    assert (await client.get("/api/v1/incidents")).status_code == 401
    assert (await client.get("/api/v1/users")).status_code == 401
    # healthz открыт для watchdog
    assert (await client.get("/healthz")).status_code == 200


async def test_login_logout_me(engine, settings, ingest, session_factory):
    await make_user(session_factory, "op", "operator-pass", UserRole.OPERATOR)
    app = build_app(settings, ingest)

    client = await login_client(app, "op", "operator-pass")
    me = (await client.get("/api/v1/auth/me")).json()
    assert me["username"] == "op"
    assert me["role"] == "operator"

    await client.post("/api/v1/auth/logout")
    assert (await client.get("/api/v1/auth/me")).status_code == 401


async def test_wrong_password_rejected(engine, settings, ingest, session_factory):
    await make_user(session_factory, "op", "operator-pass", UserRole.OPERATOR)
    client = await anon_client(settings, ingest)
    response = await client.post(
        "/api/v1/auth/login", json={"username": "op", "password": "wrong"}
    )
    assert response.status_code == 401
    response = await client.post(
        "/api/v1/auth/login", json={"username": "ghost", "password": "wrong"}
    )
    assert response.status_code == 401


async def test_viewer_is_read_only(engine, settings, ingest, session_factory):
    await make_user(session_factory, "viewer", "viewer-pass1", UserRole.VIEWER)
    app = build_app(settings, ingest)
    client = await login_client(app, "viewer", "viewer-pass1")

    assert (await client.get("/api/v1/controllers")).status_code == 200
    assert (await client.get("/api/v1/incidents")).status_code == 200
    # мутации запрещены
    response = await client.post("/api/v1/controllers", json={"name": "X"})
    assert response.status_code == 403
    response = await client.post(
        "/api/v1/incidents/1/ack", json={"acknowledged_by": "viewer"}
    )
    assert response.status_code == 403
    assert (await client.get("/api/v1/users")).status_code == 403


async def test_operator_can_ack_but_not_manage(engine, settings, ingest, session_factory):
    await make_user(session_factory, "op", "operator-pass", UserRole.OPERATOR)
    app = build_app(settings, ingest)
    client = await login_client(app, "op", "operator-pass")

    # управлять устройствами нельзя
    assert (await client.post("/api/v1/controllers", json={"name": "X"})).status_code == 403
    # ack доступен (404 — инцидента нет, но роль пропустила)
    response = await client.post(
        "/api/v1/incidents/999/ack", json={"acknowledged_by": "op"}
    )
    assert response.status_code == 404


async def test_users_crud_and_self_protection(client):
    response = await client.post(
        "/api/v1/users",
        json={
            "username": "nurse",
            "password": "nurse-pass-1",
            "full_name": "Иванова А.А.",
            "role": "operator",
        },
    )
    assert response.status_code == 201
    user_id = response.json()["id"]

    # дубликат логина
    response = await client.post(
        "/api/v1/users",
        json={"username": "nurse", "password": "x" * 8, "full_name": "Д", "role": "viewer"},
    )
    assert response.status_code == 409

    response = await client.patch(f"/api/v1/users/{user_id}", json={"role": "viewer"})
    assert response.json()["role"] == "viewer"

    # нельзя отключить/разжаловать себя
    me = (await client.get("/api/v1/auth/me")).json()
    assert (
        await client.patch(f"/api/v1/users/{me['id']}", json={"enabled": False})
    ).status_code == 409
    assert (
        await client.patch(f"/api/v1/users/{me['id']}", json={"role": "viewer"})
    ).status_code == 409


async def test_disabling_user_revokes_sessions(client, engine, settings, ingest, session_factory):
    await make_user(session_factory, "temp", "temp-pass-123", UserRole.VIEWER)
    app = build_app(settings, ingest)
    temp_client = await login_client(app, "temp", "temp-pass-123")
    assert (await temp_client.get("/api/v1/auth/me")).status_code == 200

    users = (await client.get("/api/v1/users")).json()
    temp_id = next(u["id"] for u in users if u["username"] == "temp")
    await client.patch(f"/api/v1/users/{temp_id}", json={"enabled": False})

    assert (await temp_client.get("/api/v1/auth/me")).status_code == 401


async def test_password_change_keeps_current_session(client):
    response = await client.post(
        "/api/v1/auth/password",
        json={"current_password": "admin-pass-123", "new_password": "new-pass-456"},
    )
    assert response.status_code == 204
    # текущая сессия переустановлена, работа продолжается
    assert (await client.get("/api/v1/auth/me")).status_code == 200
