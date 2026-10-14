"""Reproducible file/image manifests; source files are read-only."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import stat
import zipfile

from .images import inspect_image
from .io import sha256, read_json, is_zip_container


def inventory(root: Path, hashes: bool = True, workers: int = 2) -> dict:
    root = root.resolve(strict=True)
    if root.is_file():
        if is_zip_container(root):
            with zipfile.ZipFile(root) as archive:
                entries = [{"path": f.filename, "size": f.file_size,
                            "compressed_size": f.compress_size, "crc32": f"{f.CRC:08x}"}
                           for f in archive.infolist() if not f.is_dir()]
            result = {"schema": 1, "kind": "zip", "source": str(root),
                      "entries": sorted(entries, key=lambda f: f["path"]),
                      "size": root.stat().st_size}
            if hashes:
                result["sha256"] = sha256(root)
            return result
        result = {"schema": 1, "kind": "image", "source": str(root),
                  "entries": [{"path": root.name, **inspect_image(root)}]}
        if hashes:
            result["entries"][0]["sha256"] = sha256(root)
        return result
    paths = sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix())

    def record(path):
        info = path.lstat()
        entry = {"path": path.relative_to(root).as_posix()}
        if path.is_symlink():
            return {**entry, "kind": "symlink", "target": str(path.readlink())}
        if not stat.S_ISREG(info.st_mode):
            return None
        entry.update(kind="file", size=info.st_size)
        if path.suffix == ".img":
            entry.update(inspect_image(path))
        if hashes:
            entry["sha256"] = sha256(path)
        return entry

    with ThreadPoolExecutor(max_workers=workers) as executor:
        entries = [x for x in executor.map(record, paths) if x is not None]
    metadata = {}
    for path in sorted(root.glob("*.install")):
        metadata[path.name] = read_json(path)
    return {"schema": 1, "kind": "directory", "source": str(root),
            "hashes": hashes, "entries": entries, "metadata": metadata,
            "total_size": sum(x.get("size", 0) for x in entries)}


def three_way(base: dict, donor: dict, output: dict) -> dict:
    """Classify output by content, never by filename alone."""
    for manifest in (base, donor, output):
        if manifest.get("kind") == "zip":
            raise ValueError("Extract ZIP/image files before content comparison")
        if any(x.get("kind") != "symlink" and not x.get("sha256") for x in manifest["entries"]):
            raise ValueError("Comparison requires SHA-256 inventories")

    def signature(entry):
        return ("link", entry["target"]) if entry.get("kind") == "symlink" else ("file", entry["sha256"])

    a = {x["path"]: x for x in base["entries"]}
    b = {x["path"]: x for x in donor["entries"]}
    c = {x["path"]: x for x in output["entries"]}
    rows = []
    counts = {}
    for name in sorted(a.keys() | b.keys() | c.keys()):
        aa, bb, cc = a.get(name), b.get(name), c.get(name)
        if cc is None:
            origin = "absent-in-output"
        else:
            same_a = aa is not None and signature(aa) == signature(cc)
            same_b = bb is not None and signature(bb) == signature(cc)
            origin = "both" if same_a and same_b else "base" if same_a else "donor" if same_b else "modified-or-added"
        counts[origin] = counts.get(origin, 0) + 1
        rows.append({"path": name, "origin": origin, "base": aa, "donor": bb, "output": cc})
    return {"schema": 1, "base": base["source"], "donor": donor["source"],
            "output": output["source"], "counts": counts, "entries": rows,
            "note": "Image-level differences require filesystem extraction; origin is not semantic equivalence."}
