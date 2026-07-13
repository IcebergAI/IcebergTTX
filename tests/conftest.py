import atexit
import os

# Must be set before app.config.Settings is instantiated at import time.
os.environ.setdefault("DEV_MODE", "true")  # relax SECRET_KEY/cookie checks (#9, #10)
os.environ.setdefault("AUDIT_PERSIST", "false")  # don't write audit rows to the dev DB (#23)

# Enable an Authentik OIDC provider so the SSO routes are registered for tests
# (#25). The provider is only ever exercised via a stubbed Authlib client — the
# discovery/JWKS URLs are never fetched. AUTH_MODE stays the default "both".
os.environ.setdefault("OIDC_AUTHENTIK_ENABLED", "true")
os.environ.setdefault("OIDC_AUTHENTIK_BASE_URL", "https://authentik.test")
os.environ.setdefault("OIDC_AUTHENTIK_APP_SLUG", "ttx")
os.environ.setdefault("OIDC_AUTHENTIK_CLIENT_ID", "test-client")
os.environ.setdefault("OIDC_AUTHENTIK_CLIENT_SECRET", "test-secret")

# Spin up a real Postgres before importing the app so that the module-level
# async engine (app.database.engine, bound at import from DATABASE_URL) and the
# test session both target the same database. Import and construct testcontainers
# only when no external database was supplied; its constructor initializes the
# Docker client, which made the documented no-Docker override unusable.
_database_override = os.environ.get("DATABASE_URL_OVERRIDE_FOR_TESTS")
if not _database_override:
    from testcontainers.postgres import PostgresContainer  # noqa: E402

    _POSTGRES = PostgresContainer("postgres:17", driver="asyncpg")
    _POSTGRES.start()
    atexit.register(_POSTGRES.stop)
    os.environ["DATABASE_URL"] = _POSTGRES.get_connection_url()
else:
    _POSTGRES = None
    os.environ["DATABASE_URL"] = _database_override

import asyncio  # noqa: E402
from collections.abc import Iterator  # noqa: E402
from contextlib import contextmanager  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import AsyncClient  # noqa: E402
from httpx_ws.transport import ASGIWebSocketTransport  # noqa: E402
from sqlalchemy import event  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402
from sqlmodel.ext.asyncio.session import AsyncSession  # noqa: E402

import app.database as app_database  # noqa: E402
import app.services.llm_service as llm_service  # noqa: E402
from app.database import get_session  # noqa: E402
from app.main import app  # noqa: E402
from app.routers.ws import get_heartbeat_session_factory  # noqa: E402

# Each test runs on its own event loop (function scope), so the engine must not
# pool connections across loops — NullPool opens a fresh asyncpg connection per
# checkout, bound to the current loop. Patch the module-level engine the app and
# its background tasks use so they target the test database too.
engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
app_database.engine = engine
llm_service.engine = engine
from app.models.scenario import Scenario  # noqa: E402
from app.models.user import User, UserRole  # noqa: E402
from app.schemas.scenario_json import InjectNode, InjectOption, ScenarioDefinition  # noqa: E402
from app.services.auth_service import create_access_token, hash_password  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def seed_playwright_users():
    """Seed facilitator@deep.test and participant@deep.test into the live dev server.

    These are used by Playwright UI tests in test_ui.py. Silently no-ops if the
    server is not running on port 8765 (409 Conflict if users already exist is ignored).
    """
    try:
        import httpx
        base = os.environ.get("ICEBERG_TTX_UI_BASE", "http://localhost:8765").rstrip("/")
        for email, name, role, team in [
            ("facilitator@deep.test", "Test Facilitator", "facilitator", None),
            ("participant@deep.test", "Test Participant", "participant", "it_ops"),
        ]:
            httpx.post(
                f"{base}/api/auth/register",
                json={"email": email, "display_name": name, "password": "password1234",
                      "role": role, **({"team": team} if team else {})},
                timeout=2.0,
            )

        async def promote_facilitator() -> None:
            from sqlmodel import select

            async with AsyncSession(engine) as ui_session:
                facilitator = (
                    await ui_session.exec(
                        select(User).where(User.email == "facilitator@deep.test")
                    )
                ).one()
                facilitator.role = UserRole.facilitator
                facilitator.is_admin = True
                ui_session.add(facilitator)
                await ui_session.commit()

        asyncio.run(promote_facilitator())
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _reset_login_rate_limiter():
    """Isolate the in-memory login/registration limiters between tests (#11, #67)."""
    from app.services.rate_limit import (
        login_rate_limiter,
        password_reset_rate_limiter,
        registration_rate_limiter,
    )

    login_rate_limiter.clear()
    registration_rate_limiter.clear()
    password_reset_rate_limiter.clear()
    yield
    login_rate_limiter.clear()
    registration_rate_limiter.clear()
    password_reset_rate_limiter.clear()


@pytest.fixture(autouse=True)
def _reset_llm_provider_cache():
    """Drop the cached active AI provider so tests that monkeypatch LLM_PROVIDER /
    settings see a freshly-built provider (#26)."""
    from app.services.llm.service import reset_provider_cache

    reset_provider_cache()
    yield
    reset_provider_cache()


@pytest.fixture(autouse=True)
def _reset_mail_config_cache():
    """Keep the process-global runtime email snapshot isolated between tests."""
    from app.services import mail_service

    mail_service.set_config(None)
    yield
    mail_service.set_config(None)


@pytest.fixture(autouse=True)
def _reset_general_config_cache():
    """Keep runtime policy snapshots and limiter thresholds isolated between tests."""
    from app.services import general_settings_service

    general_settings_service.set_config(None)
    yield
    general_settings_service.set_config(None)


@pytest.fixture(scope="session", autouse=True)
def _create_schema():
    # The test suite builds a throwaway schema directly from the models rather
    # than running Alembic migrations; create_db_and_tables uses the (already
    # reassigned) module engine, so it targets the test database.
    from app.database import create_db_and_tables

    asyncio.run(create_db_and_tables())
    yield


@pytest_asyncio.fixture(name="session")
async def session_fixture():
    """A transaction-scoped async session, rolled back after each test.

    The session joins an outer transaction on a dedicated connection and uses
    SAVEPOINTs (``join_transaction_mode="create_savepoint"``) so that the
    application code's own ``commit()`` calls do not leak between tests.
    """
    async with engine.connect() as connection:
        trans = await connection.begin()
        async_session = AsyncSession(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            yield async_session
        finally:
            await async_session.close()
            await trans.rollback()


@pytest.fixture(name="count_statements")
def count_statements_fixture():
    """Record every SQL statement issued inside the context, for N+1 regression tests.

    Yields the list of statements, so a test can assert the count stays flat as the
    number of rows grows rather than pinning a brittle absolute number.
    """

    @contextmanager
    def _counter() -> Iterator[list[str]]:
        statements: list[str] = []

        def _record(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
            statements.append(statement)

        event.listen(engine.sync_engine, "before_cursor_execute", _record)
        try:
            yield statements
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", _record)

    return _counter


@pytest_asyncio.fixture(name="client", scope="session")
async def client_fixture():
    """A single websocket-capable client for the whole session.

    Opened once on the session loop. The per-test DB session is wired in via the
    autouse ``_override_session`` fixture below, not here. pytest-asyncio runs
    this session fixture's setup and teardown in different tasks, so closing the
    ASGIWebSocketTransport portal raises anyio's "cancel scope in a different
    task" error — harmless at end-of-session, so it is swallowed.
    """
    transport = ASGIWebSocketTransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://testserver")
    await client.__aenter__()
    yield client
    try:
        await client.__aexit__(None, None, None)
    except RuntimeError:
        pass


@pytest.fixture(autouse=True)
def _use_app_session_override(request: pytest.FixtureRequest):
    """Keep synchronous Playwright tests outside pytest-asyncio's event loop."""
    if request.node.path.name != "test_ui.py":
        request.getfixturevalue("_override_session")


@pytest_asyncio.fixture
async def _override_session(session: AsyncSession, client: AsyncClient):
    # The client is session-scoped (one cookie jar); reset it so auth/role-preview
    # cookies don't leak between tests.
    client.cookies.clear()

    async def override_get_session():
        yield session

    class SharedSessionContext:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *_args):
            return False

    def override_heartbeat_session_factory():
        return SharedSessionContext

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_heartbeat_session_factory] = (
        override_heartbeat_session_factory
    )
    yield
    client.cookies.clear()
    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_heartbeat_session_factory, None)


@pytest_asyncio.fixture(name="facilitator")
async def facilitator_fixture(session: AsyncSession) -> User:
    user = User(
        email="facilitator@example.com",
        display_name="Facilitator",
        hashed_password=hash_password("password1234"),
        role=UserRole.facilitator,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


@pytest_asyncio.fixture(name="participant")
async def participant_fixture(session: AsyncSession) -> User:
    user = User(
        email="participant@example.com",
        display_name="Participant",
        hashed_password=hash_password("password1234"),
        role=UserRole.participant,
        team="it_ops",
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


@pytest.fixture(name="facilitator_token")
def facilitator_token_fixture(facilitator: User) -> str:
    return create_access_token(subject=facilitator.email, role=facilitator.role.value)


@pytest.fixture(name="participant_token")
def participant_token_fixture(participant: User) -> str:
    return create_access_token(subject=participant.email, role=participant.role.value)


@pytest_asyncio.fixture(name="second_facilitator")
async def second_facilitator_fixture(session: AsyncSession) -> User:
    """A second, unrelated facilitator — used to assert per-exercise ownership
    scoping (#12): they must not reach the first facilitator's exercises."""
    user = User(
        email="facilitator2@example.com",
        display_name="Facilitator Two",
        hashed_password=hash_password("password1234"),
        role=UserRole.facilitator,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


@pytest.fixture(name="second_facilitator_token")
def second_facilitator_token_fixture(second_facilitator: User) -> str:
    return create_access_token(
        subject=second_facilitator.email, role=second_facilitator.role.value
    )


@pytest_asyncio.fixture(name="admin")
async def admin_fixture(session: AsyncSession) -> User:
    """A global admin (#12) — retains cross-facilitator access to every exercise."""
    user = User(
        email="admin@example.com",
        display_name="Admin",
        hashed_password=hash_password("password1234"),
        role=UserRole.facilitator,
        is_admin=True,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


@pytest.fixture(name="admin_token")
def admin_token_fixture(admin: User) -> str:
    return create_access_token(
        subject=admin.email, role=admin.role.value, is_admin=admin.is_admin
    )


@pytest.fixture(name="sample_definition")
def sample_definition_fixture() -> ScenarioDefinition:
    """Minimal valid two-inject scenario with one branch."""
    return ScenarioDefinition(
        title="Test Scenario",
        description="A test",
        tags=["test"],
        participant_teams=[
            {"id": "it_ops", "label": "IT Ops"},
            {"id": "legal", "label": "Legal"},
        ],
        injects=[
            InjectNode(
                id="inject_01",
                title="Initial Alert",
                content="Systems are compromised. What do you do?",
                target_teams=["it_ops"],
                options=[
                    InjectOption(id="opt_a", label="Isolate", next_inject_id="inject_02"),
                    InjectOption(id="opt_b", label="Monitor", next_inject_id=None),
                ],
            ),
            InjectNode(
                id="inject_02",
                title="Containment",
                content="Systems isolated. Notify the ICO?",
                target_teams=["legal"],
                options=[],
            ),
        ],
        start_inject_id="inject_01",
    )


@pytest_asyncio.fixture(name="sample_scenario")
async def sample_scenario_fixture(
    session: AsyncSession, facilitator: User, sample_definition: ScenarioDefinition
) -> Scenario:
    from app.services.scenario_service import create_scenario

    return await create_scenario(
        session, definition=sample_definition, created_by=facilitator.id
    )


@pytest_asyncio.fixture(name="draft_exercise")
async def draft_exercise_fixture(
    session: AsyncSession, facilitator: User, sample_scenario: Scenario
):
    from app.services.exercise_service import create_exercise

    return await create_exercise(
        session,
        scenario_id=sample_scenario.id,
        title="Test Exercise",
        created_by=facilitator.id,
    )


@pytest_asyncio.fixture(name="active_exercise")
async def active_exercise_fixture(
    session: AsyncSession, facilitator: User, participant: User, sample_scenario: Scenario
):
    from app.models.exercise import ExerciseState
    from app.services.exercise_service import create_exercise, enrol_member, transition_state

    ex = await create_exercise(
        session,
        scenario_id=sample_scenario.id,
        title="Active Exercise",
        created_by=facilitator.id,
    )
    await enrol_member(session, exercise=ex, user_id=participant.id)
    return await transition_state(session, ex, ExerciseState.active)
