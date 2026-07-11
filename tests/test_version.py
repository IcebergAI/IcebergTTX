"""Guard the single-source version contract (#73).

APP_VERSION must derive from the installed package metadata (pyproject `version`),
not a second hardcoded literal that can drift, and must not silently fall back to
the "not installed" sentinel in a normal (installed) environment.
"""

from importlib.metadata import version

from app.services import audit_service


def test_app_version_derives_from_package_metadata():
    assert audit_service.APP_VERSION == version("iceberg-ttx")
    assert audit_service.APP_VERSION != "0.0.0+unknown"
