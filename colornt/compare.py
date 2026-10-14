"""Byte comparison without rehashing ROM inputs or trusting timestamps."""
from pathlib import Path
from collections import Counter, defaultdict


def equal_bytes(left: Path, right: Path) -> bool:
    if left.stat().st_size != right.stat().st_size:
        return False
    with left.open("rb") as a, right.open("rb") as b:
        while True:
            aa, bb = a.read(1024 * 1024), b.read(1024 * 1024)
            if aa != bb:
                return False
            if not aa:
                return True


def compare_trees(left: Path, right: Path) -> dict:
    """Compare exact relative paths; configs describe Android symlink semantics."""
    left = left.resolve(strict=True)
    right = right.resolve(strict=True)
    if not left.is_dir() or not right.is_dir():
        raise ValueError("Tree comparison needs two directories")

    def files(root):
        return {p.relative_to(root).as_posix(): p for p in root.rglob("*")
                if p.is_symlink() or p.is_file()}

    a, b = files(left), files(right)
    counts = {}
    changes = []
    for name in sorted(a.keys() | b.keys()):
        aa, bb = a.get(name), b.get(name)
        if aa is None:
            status = "added"
        elif bb is None:
            status = "removed"
        elif aa.is_symlink() or bb.is_symlink():
            status = "equal" if aa.is_symlink() and bb.is_symlink() and aa.readlink() == bb.readlink() else "changed"
        else:
            status = "equal" if equal_bytes(aa, bb) else "changed"
        counts[status] = counts.get(status, 0) + 1
        if status != "equal":
            changes.append({"path": name, "status": status,
                            "before_size": aa.lstat().st_size if aa else None,
                            "after_size": bb.lstat().st_size if bb else None})
    return {"schema": 1, "left": str(left), "right": str(right), "counts": counts,
            "changes": changes, "method": "byte comparison; no input checksum validation",
            "limitations": ["Android ownership/SELinux/symlink metadata must also be compared in config/.",
                            "Changed files cannot be attributed to base ROM until base is supplied."]}


def _equal_entry(left: Path, right: Path) -> bool:
    if left.is_symlink() or right.is_symlink():
        return left.is_symlink() and right.is_symlink() and left.readlink() == right.readlink()
    return equal_bytes(left, right)


def three_way_trees(base: Path, donor: Path, output: Path,
                    exclude: tuple[str, ...] = ("config", "dsp")) -> dict:
    """Attribute paths by exact bytes without calculating whole-ROM hashes.

    A file present only in one input but absent from output is not necessarily
    debloat: it can belong to the hardware partition that was replaced wholesale.
    """
    roots = [path.resolve(strict=True) for path in (base, donor, output)]
    if any(not path.is_dir() for path in roots):
        raise ValueError("Three-way comparison requires extracted directories")

    def files(root):
        return {p.relative_to(root).as_posix(): p for p in root.rglob("*")
                if p.relative_to(root).parts[0] not in exclude and (p.is_symlink() or p.is_file())}

    a, b, c = [files(path) for path in roots]
    counts = Counter()
    partition_counts = defaultdict(Counter)
    rows = []
    for name in sorted(a.keys() | b.keys() | c.keys()):
        aa, bb, cc = a.get(name), b.get(name), c.get(name)
        if cc is None:
            origin = "absent-in-output"
        else:
            same_a = aa is not None and _equal_entry(aa, cc)
            same_b = bb is not None and _equal_entry(bb, cc)
            origin = ("both" if same_a and same_b else "base" if same_a else
                      "donor" if same_b else "modified" if aa is not None or bb is not None else "added")
        row = {"path": name, "origin": origin,
               "base_size": aa.lstat().st_size if aa else None,
               "donor_size": bb.lstat().st_size if bb else None,
               "output_size": cc.lstat().st_size if cc else None}
        rows.append(row)
        counts[origin] += 1
        partition_counts[name.split("/")[0]][origin] += 1
    return {"schema": 1, "base": str(roots[0]), "donor": str(roots[1]),
            "output": str(roots[2]), "method": "exact byte comparison; no input hash validation",
            "exclude": list(exclude), "counts": dict(counts),
            "by_partition": {name: dict(values) for name, values in partition_counts.items()},
            "entries": rows,
            "limitations": ["Content matches do not prove which source was actually used when both match.",
                            "Filesystem metadata and APK/JAR behavior require separate validation.",
                            "Absent input files do not all represent debloat."]}


def three_way_images(base: Path, donor: Path, output: Path) -> dict:
    """Normalize packaging layouts by unique partition basename, compare bytes."""
    def images(root):
        result = {}
        for path in root.resolve(strict=True).rglob("*.img"):
            if path.name in result:
                raise ValueError(f"Duplicate image basename: {path.name}")
            result[path.name] = path
        return result

    a, b, c = [images(root) for root in (base, donor, output)]
    rows = []
    counts = Counter()
    for name in sorted(a.keys() | b.keys() | c.keys()):
        aa, bb, cc = a.get(name), b.get(name), c.get(name)
        if cc is None:
            origin = "absent-in-output"
        else:
            same_a = aa is not None and equal_bytes(aa, cc)
            same_b = bb is not None and equal_bytes(bb, cc)
            origin = "both" if same_a and same_b else "base" if same_a else "donor" if same_b else "modified-or-added"
        counts[origin] += 1
        rows.append({"partition": name, "origin": origin,
                     "base": str(aa) if aa else None, "donor": str(bb) if bb else None,
                     "output": str(cc) if cc else None})
    return {"schema": 1, "counts": dict(counts), "partitions": rows,
            "method": "unique basename and byte comparison; no input hash validation",
            "note": "Repacked filesystem images can differ while contained files match."}
