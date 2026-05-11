import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.database import get_session
from app.main import app
from app.models.scenario import Scenario
from app.models.user import User, UserRole
from app.schemas.scenario_json import InjectNode, InjectOption, ScenarioDefinition
from app.services.auth_service import create_access_token, hash_password


@pytest.fixture(scope="session", autouse=True)
def seed_playwright_users():
    """Seed facilitator@deep.test and participant@deep.test into the live dev server.

    These are used by Playwright UI tests in test_ui.py. Silently no-ops if the
    server is not running on port 8765 (409 Conflict if users already exist is ignored).
    """
    try:
        import httpx
        base = "http://localhost:8765"
        for email, name, role, team in [
            ("facilitator@deep.test", "Test Facilitator", "facilitator", None),
            ("participant@deep.test", "Test Participant", "participant", "it_ops"),
        ]:
            httpx.post(
                f"{base}/api/auth/register",
                json={"email": email, "display_name": name, "password": "password123",
                      "role": role, **({"team": team} if team else {})},
                timeout=2.0,
            )
    except Exception:
        pass


@pytest.fixture(name="session")
def session_fixture():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    SQLModel.metadata.drop_all(engine)


@pytest.fixture(name="client")
def client_fixture(session: Session):
    def override_get_session():
        yield session

    app.dependency_overrides[get_session] = override_get_session
    with TestClient(app, raise_server_exceptions=True) as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture(name="facilitator")
def facilitator_fixture(session: Session) -> User:
    user = User(
        email="facilitator@example.com",
        display_name="Facilitator",
        hashed_password=hash_password("password123"),
        role=UserRole.facilitator,
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


@pytest.fixture(name="participant")
def participant_fixture(session: Session) -> User:
    user = User(
        email="participant@example.com",
        display_name="Participant",
        hashed_password=hash_password("password123"),
        role=UserRole.participant,
        team="it_ops",
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


@pytest.fixture(name="facilitator_token")
def facilitator_token_fixture(facilitator: User) -> str:
    return create_access_token(subject=facilitator.email, role=facilitator.role.value)


@pytest.fixture(name="participant_token")
def participant_token_fixture(participant: User) -> str:
    return create_access_token(subject=participant.email, role=participant.role.value)


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


@pytest.fixture(name="sample_scenario")
def sample_scenario_fixture(
    session: Session, facilitator: User, sample_definition: ScenarioDefinition
) -> Scenario:
    from app.services.scenario_service import create_scenario

    return create_scenario(session, definition=sample_definition, created_by=facilitator.id)


@pytest.fixture(name="draft_exercise")
def draft_exercise_fixture(session: Session, facilitator: User, sample_scenario: Scenario):
    from app.services.exercise_service import create_exercise

    return create_exercise(
        session,
        scenario_id=sample_scenario.id,
        title="Test Exercise",
        created_by=facilitator.id,
    )


@pytest.fixture(name="active_exercise")
def active_exercise_fixture(session: Session, facilitator: User, sample_scenario: Scenario):
    from app.models.exercise import ExerciseState
    from app.services.exercise_service import create_exercise, transition_state

    ex = create_exercise(
        session,
        scenario_id=sample_scenario.id,
        title="Active Exercise",
        created_by=facilitator.id,
    )
    return transition_state(session, ex, ExerciseState.active)
