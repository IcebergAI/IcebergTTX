"""Report and optionally remove stale inject attachment files (#139)."""

import argparse
import asyncio
import json
from pathlib import Path

from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.database import engine
from app.models.inject import Inject
from app.services import audit_service

ATTACHMENT_ROOT = Path("uploads/inject_attachments")


def _under_root(path: Path, root: Path) -> bool:
    return root == path or root in path.parents


async def reconcile(*, apply: bool = False, root: Path = ATTACHMENT_ROOT) -> dict:
    """Find missing DB-referenced files and root-confined orphan files.

    The default is read-only. Apply mode removes only files under ``root`` that
    have no live Inject reference; repeated runs are safe.
    """
    resolved_root = root.resolve()
    async with AsyncSession(engine, expire_on_commit=False) as session:
        injects = (
            await session.exec(
                select(Inject).where(col(Inject.attachment_path).is_not(None))
            )
        ).all()

    referenced = {
        Path(inject.attachment_path).resolve()
        for inject in injects
        if inject.attachment_path
        and _under_root(Path(inject.attachment_path).resolve(), resolved_root)
    }
    existing = {path.resolve() for path in resolved_root.rglob("*") if path.is_file()}
    missing = sorted(str(path) for path in referenced - existing)
    orphans = sorted(existing - referenced)
    removed: list[str] = []
    if apply:
        for path in orphans:
            if _under_root(path, resolved_root):
                path.unlink(missing_ok=True)
                removed.append(str(path))
        audit_service.emit(
            "attachments.reconcile",
            target_type="attachment_storage",
            reason=f"removed={len(removed)} missing={len(missing)}",
            severity="warning",
        )
    return {
        "dry_run": not apply,
        "missing_files": missing,
        "orphan_files": [str(path) for path in orphans],
        "removed_files": removed,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Reconcile inject attachment storage")
    parser.add_argument("--apply", action="store_true", help="remove reported orphan files")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(reconcile(apply=args.apply)), indent=2))


if __name__ == "__main__":
    main()
