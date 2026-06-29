"""Tests for central logging configuration (#17)."""

import logging

import pytest

import app.logging_config as logging_config
from app.logging_config import configure_logging


@pytest.fixture
def restore_logging():
    """Snapshot and fully restore global logging state mutated by configure_logging.

    configure_logging() rewrites the root handlers/level and the audit logger's
    handlers/level/propagate, and sets a module-level _configured flag. Without a
    full restore these leak into later tests (e.g. audit caplog assertions break
    once audit propagation is disabled).
    """
    root = logging.getLogger()
    audit = logging.getLogger("deep_thought.audit")
    snapshot = {
        "configured": logging_config._configured,
        "root_handlers": list(root.handlers),
        "root_level": root.level,
        "audit_handlers": list(audit.handlers),
        "audit_level": audit.level,
        "audit_propagate": audit.propagate,
    }
    logging_config._configured = False
    try:
        yield
    finally:
        root.handlers[:] = snapshot["root_handlers"]
        root.setLevel(snapshot["root_level"])
        audit.handlers[:] = snapshot["audit_handlers"]
        audit.setLevel(snapshot["audit_level"])
        audit.propagate = snapshot["audit_propagate"]
        logging_config._configured = snapshot["configured"]


def test_configure_logging_installs_root_handler(restore_logging):
    configure_logging()
    root = logging.getLogger()
    assert root.handlers, "root logger should have a handler after configuration"
    assert root.level == getattr(
        logging, logging_config.settings.log_level.upper(), logging.INFO
    )


def test_audit_logger_is_isolated_and_unwrapped(restore_logging):
    configure_logging()
    audit = logging.getLogger("deep_thought.audit")
    # Audit stream stays pure JSON: its own handler, not propagated to root.
    assert audit.propagate is False
    assert audit.handlers
    fmt = audit.handlers[0].formatter
    assert fmt is not None and fmt._fmt == "%(message)s"


def test_configure_logging_is_idempotent(restore_logging):
    configure_logging()
    root = logging.getLogger()
    count = len(root.handlers)
    configure_logging()  # second call is a no-op
    assert len(root.handlers) == count
