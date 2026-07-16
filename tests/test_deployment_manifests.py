"""Regression tests for deployment-manifest invariants."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _documents(path: str) -> list[dict]:
    with (ROOT / path).open(encoding="utf-8") as manifest:
        return [document for document in yaml.safe_load_all(manifest) if document]


def _kustomization_resources(path: str) -> list[str]:
    return _documents(path)[0].get("resources", [])


def _kinds(path: str) -> set[str]:
    return {document.get("kind") for document in _documents(path)}


def test_backup_script_is_posix_sh_compatible() -> None:
    cronjob = next(
        document
        for document in _documents("k8s/base/postgres/backup-cronjob.yaml")
        if document["kind"] == "CronJob"
    )
    container = cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]

    assert container["command"] == ["/bin/sh", "-c"]
    assert "set -eu\n" in container["args"][0]
    assert "set -euo pipefail" not in container["args"][0]


def test_network_policy_allows_backup_job_to_reach_postgres() -> None:
    policies = {
        document["metadata"]["name"]: document
        for document in _documents("k8s/base/networkpolicy.yaml")
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
    statefulset = _documents("k8s/base/postgres/statefulset.yaml")[0]
    container = statefulset["spec"]["template"]["spec"]["containers"][0]
    env = {item["name"]: item for item in container["env"]}
    mounts = {item["name"]: item for item in container["volumeMounts"]}

    assert env["PGDATA"]["value"] == "/var/lib/postgresql/pgdata"
    assert mounts["postgres-data"]["mountPath"] == "/var/lib/postgresql"


def test_static_init_containers_do_not_preserve_root_owned_metadata() -> None:
    for path in ("k8s/base/app/deployment.yaml", "k8s/base/caddy/deployment.yaml"):
        deployment = _documents(path)[0]
        init = deployment["spec"]["template"]["spec"]["initContainers"][0]
        command = init["command"][-1]

        assert "cp -rL" in command
        assert "cp -a" not in command


# --- Kustomize base + overlays structure (#254) --------------------------------
# These guard the layout without shelling out to the kustomize binary (which CI
# does not have): the base stays cloud-agnostic and the only AWS CRD lives in the
# eks overlay, so applying the base or the nginx overlay never depends on the AWS
# Load Balancer Controller.

_KUSTOMIZATIONS = (
    "k8s/base/kustomization.yaml",
    "k8s/overlays/nginx/kustomization.yaml",
    "k8s/overlays/eks/kustomization.yaml",
)


def test_every_kustomization_resource_exists() -> None:
    for kustomization in _KUSTOMIZATIONS:
        base_dir = (ROOT / kustomization).parent
        for resource in _kustomization_resources(kustomization):
            assert (base_dir / resource).exists(), (
                f"{kustomization} references missing resource {resource}"
            )


def test_base_is_cloud_agnostic_no_ingress_or_crd() -> None:
    # The base must not carry an ingress edge or any cloud CRD — those are overlay
    # concerns — so `kubectl apply -k k8s/base` works on any conformant cluster.
    base_dir = ROOT / "k8s/base"
    kinds: set[str] = set()
    for resource in _kustomization_resources("k8s/base/kustomization.yaml"):
        kinds |= _kinds(str((base_dir / resource).relative_to(ROOT)))

    assert "Ingress" not in kinds
    assert "TargetGroupBinding" not in kinds


def test_nginx_overlay_adds_only_a_standard_ingress() -> None:
    resources = _kustomization_resources("k8s/overlays/nginx/kustomization.yaml")
    assert "../../base" in resources
    assert "Ingress" in _kinds("k8s/overlays/nginx/ingress.yaml")


def test_eks_overlay_confines_targetgroupbinding_and_uses_ip_targets() -> None:
    resources = _kustomization_resources("k8s/overlays/eks/kustomization.yaml")
    assert "../../base" in resources
    assert "targetgroupbinding.yaml" in resources

    tgb = _documents("k8s/overlays/eks/targetgroupbinding.yaml")[0]
    assert tgb["kind"] == "TargetGroupBinding"
    # `ip` is what lets the base's ClusterIP caddy Service work unchanged; `instance`
    # mode would need a NodePort Service.
    assert tgb["spec"]["targetType"] == "ip"
    # It must bind to the caddy Service the base ships.
    assert tgb["spec"]["serviceRef"]["name"] == "caddy"
