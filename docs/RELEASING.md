# Releasing IcebergTTX

IcebergTTX follows [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html).

- **`MAJOR.MINOR.PATCH`** — while on `0.x`, the project is **pre-stable**: minor bumps
  may include breaking changes. `1.0.0` marks the first stable API contract.
- **Pre-releases** use a suffix: `X.Y.Z-beta.N` (also `-alpha.N`, `-rc.N`). The first
  public release is **`v0.1.0-beta.1`**.
- **Version source of truth** is `pyproject.toml` `version` (PEP 440, e.g. `0.1.0b1`).
  `app/services/audit_service.APP_VERSION` derives from it via `importlib.metadata`, so
  audit events always match the built artefact. The git tag uses the SemVer display form
  (`v0.1.0-beta.1`); the release workflow reconciles the two and **fails** on a mismatch.

## Image tags (GHCR)

Images publish to `ghcr.io/icebergai/iceberg-ttx`. On a tag push the workflow pushes:

- the full version, e.g. `0.1.0-beta.1`, and a `sha-<commit>` tag — always;
- `X.Y` and `latest` — **only for stable** releases (never for `-beta`/`-rc`).

Every pushed image carries an SBOM, a signed SLSA build-provenance attestation, and a
cosign (keyless) signature.

## Cutting a release

1. **Bump the version** in `pyproject.toml` (PEP 440 form) and run `uv lock` to update
   the project version in `uv.lock`.
2. **Update `CHANGELOG.md`**: move items from `[Unreleased]` into a new
   `[X.Y.Z(-beta.N)]` section; refresh the compare/link footnotes.
3. **Open a PR**, let CI pass (`uv lock --check`, tests, zizmor/actionlint, CodeQL), merge.
4. **Tag and push** the merge commit on `main`:
   ```
   git checkout main && git pull
   git tag v0.1.0-beta.1
   git push origin v0.1.0-beta.1
   ```
   The `Release` workflow builds and pushes the image, attaches the SBOM + provenance,
   signs it, and creates a GitHub Release (marked **pre-release** when the tag has a `-`).

Before the very first real tag, validate the pipeline with a throwaway pre-release tag
(e.g. `v0.0.0-test`) or a `workflow_dispatch` run with **dry_run** unchecked, then delete
the test tag, its Release, and the GHCR version.

## Verifying a released image

```
docker pull ghcr.io/icebergai/iceberg-ttx:0.1.0-beta.1

# cosign signature (keyless — identity is the release workflow)
cosign verify ghcr.io/icebergai/iceberg-ttx:0.1.0-beta.1 \
  --certificate-identity-regexp 'https://github.com/IcebergAI/IcebergTTX/.github/workflows/release.yml@.*' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com

# SLSA build provenance
gh attestation verify oci://ghcr.io/icebergai/iceberg-ttx:0.1.0-beta.1 --repo IcebergAI/IcebergTTX
```

## Deploying a release

- **Docker Compose**: swap the `build:` block for `image: ghcr.io/icebergai/iceberg-ttx:<version>`.
- **Kubernetes**: the image is already referenced in `k8s/app/deployment.yaml` (app + the
  `copy-static` init container) and `k8s/caddy/deployment.yaml`. Update the tag, and **pin
  by digest** (`ghcr.io/icebergai/iceberg-ttx@sha256:…`) for reproducible rollouts. The app
  runs single-replica (`strategy: Recreate`) and self-applies Alembic migrations on startup.
