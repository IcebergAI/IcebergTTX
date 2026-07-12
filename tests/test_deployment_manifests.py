"""Regression tests for deployment-manifest invariants."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _documents(path: str) -> list[dict]:
    with (ROOT / path).open(encoding="utf-8") as manifest:
        return [document for document in yaml.safe_load_all(manifest) if document]


def test_backup_script_is_posix_sh_compatible() -> None:
    cronjob = next(
        document
        for document in _documents("k8s/postgres/backup-cronjob.yaml")
        if document["kind"] == "CronJob"
    )
    container = cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]

    assert container["command"] == ["/bin/sh", "-c"]
    assert "set -eu\n" in container["args"][0]
    assert "set -euo pipefail" not in container["args"][0]


def test_network_policy_allows_backup_job_to_reach_postgres() -> None:
    policies = {
        document["metadata"]["name"]: document
        for document in _documents("k8s/networkpolicy.yaml")
    }
    policy = policies["allow-postgres-from-backup"]

    assert policy["spec"]["podSelector"]["matchLabels"] == {
        "app.kubernetes.io/name": "postgres"
    }
    assert policy["spec"]["ingress"] == [
        {
            "from": [
                {
                    "podSelector": {
                        "matchLabels": {"app.kubernetes.io/name": "postgres-backup"}
                    }
                }
            ],
            "ports": [{"protocol": "TCP", "port": 5432}],
        }
    ]


def test_postgres_creates_pgdata_below_the_fs_group_owned_volume() -> None:
    statefulset = _documents("k8s/postgres/statefulset.yaml")[0]
    container = statefulset["spec"]["template"]["spec"]["containers"][0]
    env = {item["name"]: item for item in container["env"]}
    mounts = {item["name"]: item for item in container["volumeMounts"]}

    assert env["PGDATA"]["value"] == "/var/lib/postgresql/data"
    assert mounts["postgres-data"]["mountPath"] == "/var/lib/postgresql"


def test_static_init_containers_do_not_preserve_root_owned_metadata() -> None:
    for path in ("k8s/app/deployment.yaml", "k8s/caddy/deployment.yaml"):
        deployment = _documents(path)[0]
        init = deployment["spec"]["template"]["spec"]["initContainers"][0]
        command = init["command"][-1]

        assert "cp -rL" in command
        assert "cp -a" not in command
