"""Cross-replica config-cache invalidation (#213).

The failure this prevents: an admin saves the SMTP host on replica A and replica B keeps
mailing through the old one until someone restarts it.
"""

import pytest

from app.services import config_sync, pg_bus


class _CtxSession:
    """Hand the handler the test's transactional session instead of a new one.

    Same trick the scheduler tests use: a handler that opened its own
    ``AsyncSession(engine)`` could not see rows this test has not committed.
    """

    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def local_session(monkeypatch, session):
    monkeypatch.setattr(config_sync, "AsyncSession", lambda *a, **k: _CtxSession(session))
    return session


def test_every_cached_settings_scope_is_refreshable():
    """A scope with no refresher is a cache that silently goes stale on every other
    replica, so the map is asserted whole rather than sampled."""
    assert set(config_sync.SCOPES) == {"proxy", "siem", "email", "general", "llm", "oidc"}


def test_proxy_is_refreshed_before_oidc():
    """OIDC registration bakes the resolved proxy into each Authlib client (#97), so a
    reconnect that refreshed OIDC first would re-register against the old proxy."""
    order = list(config_sync.SCOPES)
    assert order.index("proxy") < order.index("oidc")


async def test_a_scope_notification_reloads_that_cache(local_session, monkeypatch):
    refreshed: list[str] = []
    monkeypatch.setitem(config_sync.SCOPES, "email", _spy_appending(refreshed, "email"))

    await config_sync.handle(pg_bus.envelope(scope="email"))

    assert refreshed == ["email"]


async def test_the_publishing_replica_refreshes_from_its_own_notification(
    local_session, monkeypatch
):
    """Deliberately not skipped by origin: re-reading a row this replica just wrote is
    idempotent, and one fewer branch than filtering it out."""
    refreshed: list[str] = []

    monkeypatch.setitem(config_sync.SCOPES, "llm", _spy_appending(refreshed, "llm"))

    await config_sync.handle(pg_bus.envelope(scope="llm"))

    assert refreshed == ["llm"]


async def test_an_unknown_scope_is_ignored(local_session):
    """A newer replica publishing a scope this build has never heard of, mid-rolling
    deploy. The peer that owns it will act on it; this one must not fall over."""
    await config_sync.handle(pg_bus.envelope(scope="quantum_settings"))


async def test_a_descriptor_without_a_scope_is_ignored(local_session, caplog):
    await config_sync.handle(pg_bus.envelope(nothing="useful"))

    assert "without a scope" in caplog.text


async def test_reconnect_refreshes_every_scope(local_session, monkeypatch):
    """At-most-once delivery means everything published during the outage is simply
    gone, so recovery has to assume all of it was."""
    refreshed: list[str] = []

    for scope in list(config_sync.SCOPES):
        monkeypatch.setitem(config_sync.SCOPES, scope, _spy_appending(refreshed, scope))
    monkeypatch.setattr(config_sync, "_first_connect", False)

    await config_sync.on_bus_connected()

    assert refreshed == list(config_sync.SCOPES)


async def test_the_first_connect_refreshes_nothing(local_session, monkeypatch):
    """Startup has just loaded every scope; refreshing OIDC here would drop the Authlib
    registration the lifespan performed moments earlier."""
    refreshed: list[str] = []
    for scope in list(config_sync.SCOPES):
        monkeypatch.setitem(config_sync.SCOPES, scope, _spy_appending(refreshed, scope))
    monkeypatch.setattr(config_sync, "_first_connect", True)

    await config_sync.on_bus_connected()

    assert refreshed == []


async def test_one_unreadable_scope_does_not_stop_the_others(local_session, monkeypatch):
    refreshed: list[str] = []

    async def explode(session):
        raise RuntimeError("settings table is on fire")

    for scope in list(config_sync.SCOPES):
        monkeypatch.setitem(config_sync.SCOPES, scope, _spy_appending(refreshed, scope))
    monkeypatch.setitem(config_sync.SCOPES, "siem", explode)
    monkeypatch.setattr(config_sync, "_first_connect", False)

    await config_sync.on_bus_connected()

    assert "siem" not in refreshed
    assert len(refreshed) == len(config_sync.SCOPES) - 1


async def test_proxy_refresh_also_drops_the_clients_that_captured_the_old_proxy(
    local_session, monkeypatch
):
    """The LLM provider and the Authlib OIDC clients bake the proxy in at construction,
    so reloading only the proxy cache would leave both routing through the old one."""
    from app.services import proxy_settings_service

    invalidated: list[str] = []
    monkeypatch.setattr(
        proxy_settings_service,
        "_invalidate_dependent_caches",
        lambda: invalidated.append("dependents"),
    )

    await config_sync.handle(pg_bus.envelope(scope="proxy"))

    assert invalidated == ["dependents"]


async def test_a_settings_save_publishes_before_it_commits(session, monkeypatch):
    """The ordering is the whole guarantee (#213): published inside the transaction,
    Postgres delivers the invalidation only if the save committed. Move the publish
    below the commit and a rolled-back save would tell every replica to reload.
    """
    from app.services import general_settings_service

    calls: list[str] = []
    original_commit = session.commit

    async def spy_publish(_session, scope):
        calls.append(f"publish:{scope}")

    async def spy_commit():
        calls.append("commit")
        await original_commit()

    monkeypatch.setattr(pg_bus, "publish_config_changed", spy_publish)
    monkeypatch.setattr(session, "commit", spy_commit)

    await general_settings_service.update_settings(session, {"registration_enabled": True})

    assert calls[-2:] == ["publish:general", "commit"]


def _spy_appending(sink: list[str], scope: str):
    async def spy(session):
        sink.append(scope)

    return spy
