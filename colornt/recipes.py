"""Apply version-specific file recipes to disposable trees with hash guards."""
from pathlib import Path
import shutil

from .io import confined, sha256


def apply_recipe(root: Path, asset_root: Path, recipe: dict) -> list[dict]:
    root = root.resolve(strict=True)
    asset_root = asset_root.resolve(strict=True)
    operations = recipe.get("operations", [])
    prepared = []
    seen = set()
    # Validate all preconditions before the first change.
    for op in operations:
        path = confined(root, op["path"])
        if path in seen:
            raise ValueError(f"Duplicate recipe target: {op['path']}")
        seen.add(path)
        if path.is_symlink():
            raise ValueError(f"Recipe cannot write through a symlink: {op['path']}")
        before = op.get("before_sha256")
        if before is None:
            if path.exists():
                raise ValueError(f"Expected absent target: {op['path']}")
        elif not path.is_file() or sha256(path) != before:
            raise ValueError(f"Input hash mismatch: {op['path']}")
        kind = op["kind"]
        content = None
        if kind == "replace":
            asset = confined(asset_root, op["asset"])
            if not op.get("after_sha256") or sha256(asset) != op["after_sha256"]:
                raise ValueError(f"Asset hash mismatch: {op['asset']}")
            content = asset
        elif kind == "text-replace":
            if before is None or not op.get("after_sha256"):
                raise ValueError("Text replacement requires before and after hashes")
            raw = path.read_bytes()
            old = op["old"].encode("utf-8")
            new = op["new"].encode("utf-8")
            if not old or raw.count(old) != op.get("count", 1):
                raise ValueError(f"Text match count mismatch: {op['path']}")
            content = raw.replace(old, new)
            import hashlib
            if hashlib.sha256(content).hexdigest() != op["after_sha256"]:
                raise ValueError(f"Text result hash mismatch: {op['path']}")
        elif kind == "delete":
            if before is None:
                raise ValueError("Delete requires an existing file and before hash")
        else:
            raise ValueError(f"Unsupported operation: {kind}")
        prepared.append((op, path, content))
    report = []
    for op, path, content in prepared:
        if op["kind"] == "delete":
            path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, Path):
                shutil.copyfile(content, path)
            else:
                path.write_bytes(content)
        report.append({"path": op["path"], "kind": op["kind"],
                       "before_sha256": op.get("before_sha256"),
                       "after_sha256": None if op["kind"] == "delete" else sha256(path)})
    return report
