"""Evidence-scoped property transformations for disposable port trees.

Copy hardware values from A; rewrite named identity fields in B while retaining
its OS/version/region. This module is not a compatibility or boot certificate.
"""
import hashlib
import os
from pathlib import Path
import re

from .android_links import link_target
from .io import confined


KEY = re.compile(r"[A-Za-z0-9_.-]+\Z")
IDENTIFIER = re.compile(r"[A-Za-z0-9]+\Z")


class Properties:
    def __init__(self, raw: bytes):
        self.bom = raw.startswith(b"\xef\xbb\xbf")
        self.lines = raw.decode("utf-8-sig").splitlines(keepends=True)
        self.entries = {}
        for index, line in enumerate(self.lines):
            body = line.rstrip("\r\n")
            if not body.strip() or body.lstrip().startswith("#"):
                continue
            # Android build.prop may import other files, including paths with
            # ${property} expansion. Preserve directives verbatim; never resolve
            # them against the host or claim the runtime property set is known.
            if re.fullmatch(r"import[ \t]+\S+(?:[ \t]+\S+)?", body.strip()):
                continue
            if "=" not in body:
                raise ValueError("Unsupported property syntax (expected key=value)")
            key, value = body.split("=", 1)
            key = key.strip()
            if not KEY.fullmatch(key):
                raise ValueError(f"Invalid property key: {key}")
            self.entries.setdefault(key, []).append((index, value.strip()))

    def values(self, key: str) -> list[str]:
        if not isinstance(key, str) or not KEY.fullmatch(key):
            raise ValueError("Invalid property key")
        return [value for _, value in self.entries.get(key, [])]

    def one(self, key: str) -> str:
        values = self.values(key)
        if len(values) != 1:
            raise ValueError(f"Source property must occur exactly once: {key}")
        return values[0]

    def render(self, changes: dict[str, str]) -> bytes:
        lines = list(self.lines)
        newline = "\r\n" if any(x.endswith("\r\n") for x in lines) else "\n"
        for key, value in changes.items():
            if any(c in value for c in "\r\n\0"):
                raise ValueError(f"Invalid property value: {key}")
            entries = self.entries.get(key, [])
            if not entries:
                if lines and not lines[-1].endswith("\n"):
                    lines[-1] += newline
                lines.append(f"{key}={value}{newline}")
            for index, _ in entries:
                original = lines[index]
                ending = "\r\n" if original.endswith("\r\n") else "\n" if original.endswith("\n") else ""
                body = original[:-len(ending)] if ending else original
                prefix, old = body.split("=", 1)
                leading = old[:len(old) - len(old.lstrip())]
                trailing = old[len(old.rstrip()):] if old.strip() else ""
                lines[index] = prefix + "=" + leading + value + trailing + ending
        return (b"\xef\xbb\xbf" if self.bom else b"") + "".join(lines).encode("utf-8")


def identity_value(value: str, form: str, base: dict, donor: dict, suffixes: list[str]) -> str:
    """Transform explicit identity syntax, never substring-replace arbitrary text."""
    def product(name):
        for suffix in ["", *suffixes]:
            if name == donor["model"] + suffix:
                return base["model"] + suffix
        raise ValueError(f"Unrecognized donor product/region: {name}")

    if form in ("model", "device"):
        if value != donor[form]:
            raise ValueError(f"Donor {form} identity mismatch")
        return base[form]
    if form == "product-name":
        return product(value)
    if form == "model-prefix":
        if not value.startswith(donor["model"]):
            raise ValueError("Donor model prefix mismatch")
        tail = value[len(donor["model"]):]
        if not tail.startswith(("_", "export_", "domestic_")):
            raise ValueError("Unsupported model prefix delimiter")
        return base["model"] + tail
    if form == "fingerprint":
        match = re.fullmatch(r"([^/]+)/([^/]+)/([^:]+):([^/]+)/([^/]+)/([^:]+):([^/]+)/([^/]+)", value)
        if not match or match[3] != donor["device"]:
            raise ValueError("Donor fingerprint syntax/device mismatch")
        brand, name, _, android, build, incremental, variant, tags = match.groups()
        return f"{brand}/{product(name)}/{base['device']}:{android}/{build}/{incremental}:{variant}/{tags}"
    raise ValueError(f"Unknown identity form: {form}")


def port_properties(stage: Path, base: Path, donor: Path, policy: dict, dry_run: bool = False,
                    allow_applied: bool = False) -> dict:
    if policy.get("schema") != 1 or policy.get("kind") != "port-properties":
        raise ValueError("Unsupported property policy")
    if set(policy) - {"schema", "kind", "id", "scope", "evidence", "conditions", "identity", "files"}:
        raise ValueError("Unknown property policy fields")
    roots = {name: path.resolve(strict=True) for name, path in (("stage", stage), ("base", base), ("donor", donor))}
    if any(not path.is_dir() for path in roots.values()):
        raise ValueError("Property transformation requires extracted filesystem directories")
    for role in ("base", "donor"):
        a, b = roots["stage"], roots[role]
        if a == b or a.is_relative_to(b) or b.is_relative_to(a):
            raise ValueError("Staged tree overlaps protected input")
    cache = {}

    def document(role, name):
        if role not in roots:
            raise ValueError(f"Unknown property source role: {role}")
        path = confined(roots[role], name)
        if path not in cache:
            if link_target(path) is not None:
                raise ValueError("Cannot transform/read property through an Android link")
            raw = path.read_bytes()
            cache[path] = (Properties(raw), raw)
        return path, cache[path][0]

    def reference(ref):
        if not isinstance(ref, dict) or set(ref) != {"role", "path", "key"} or ref["role"] not in ("base", "donor"):
            raise ValueError("Source reference requires role=base/donor, path and key")
        return document(ref["role"], ref["path"])[1].one(ref["key"])

    conditions = policy.get("conditions", [])
    if not conditions:
        raise ValueError("Property policy requires explicit input conditions")
    for condition in conditions:
        if set(condition) != {"source", "equals"} or reference(condition["source"]) != condition["equals"]:
            raise ValueError(f"Property input condition failed: {condition}")
    identity = policy.get("identity", {})
    names = {}
    suffixes = identity.get("product_suffixes", [])
    if not isinstance(suffixes, list) or len(suffixes) != len(set(suffixes)) or any(not isinstance(s, str) or not IDENTIFIER.fullmatch(s) for s in suffixes):
        raise ValueError("Invalid explicit product suffixes")
    if identity:
        if set(identity) != {"base", "donor", "product_suffixes"}:
            raise ValueError("Unknown identity fields")
        for role in ("base", "donor"):
            if set(identity[role]) != {"model", "device"}:
                raise ValueError("Identity requires model/device references")
            names[role] = {key: reference(ref) for key, ref in identity[role].items()}
            if any(not IDENTIFIER.fullmatch(value) for value in names[role].values()):
                raise ValueError("Invalid model/device identity")
            if any(ref["role"] != role for ref in identity[role].values()):
                raise ValueError("Identity reference role mismatch")
    prepared, seen = [], set()
    # Resolve and validate every source, target, occurrence and result first.
    for item in policy.get("files", []):
        if set(item) != {"path", "baseline", "operations"} or item["baseline"] not in ("base", "donor"):
            raise ValueError("Invalid property file/baseline")
        path, staged = document("stage", item["path"])
        if path.stat().st_nlink != 1:
            raise ValueError("Staged property target must not be a hardlink")
        if path in seen:
            raise ValueError("Duplicate property file target")
        seen.add(path)
        _, original = document(item["baseline"], item["path"])
        changes, operations = {}, []
        for op in item["operations"]:
            required = {"key", "kind", "occurrences", "source" if op.get("kind") == "copy" else "form"}
            if set(op) != required or op["kind"] not in ("copy", "identity"):
                raise ValueError("Unknown property operation/fields")
            key = op["key"]
            if key in changes:
                raise ValueError(f"Duplicate property operation: {key}")
            count = op["occurrences"]
            values = original.values(key)
            if type(count) is not int or count < 0 or len(values) != count:
                raise ValueError(f"Property baseline/occurrence mismatch: {item['path']}:{key}")
            if op["kind"] == "copy":
                value = reference(op["source"])
            else:
                if not names or not values:
                    raise ValueError("Identity transformation requires existing key and identity references")
                results = [identity_value(v, op["form"], names["base"], names["donor"], suffixes) for v in values]
                if len(set(results)) != 1:
                    raise ValueError("Duplicate identity results disagree")
                value = results[0]
            current = staged.values(key)
            desired = [value] * max(1, count)
            # A byte-qualified reconstruction may already contain this exact
            # operation. Other staged values are still rejected, batch-wide.
            if current != values and not (allow_applied and current == desired):
                raise ValueError(f"Property baseline/occurrence mismatch: {item['path']}:{key}")
            changes[key] = value
            operations.append({"key": key, "kind": op["kind"], "before": current, "after": value})
        if not operations:
            raise ValueError("Empty property operation list")
        raw = staged.render(changes)
        # Parse the result as well, including newly appended properties.
        result = Properties(raw)
        for key, value in changes.items():
            if result.values(key) != [value] * max(1, len(staged.values(key))):
                raise ValueError("Rendered property result mismatch")
        prepared.append((path, raw, operations))
    if not prepared:
        raise ValueError("Empty property policy")
    reports = []
    for path, raw, operations in prepared:
        before = cache[path][1]
        if not dry_run:
            # Preserve the existing inode/mode and extraction timestamp. Android
            # uid/gid/SELinux/capabilities remain in the staging metadata files.
            stat = path.stat()
            path.write_bytes(raw)
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        reports.append({"path": path.relative_to(roots["stage"]).as_posix(), "operations": operations,
                        "before_sha256": hashlib.sha256(before).hexdigest(),
                        "after_sha256": hashlib.sha256(raw).hexdigest()})
    return {"schema": 1, "status": "planned" if dry_run else "applied", "policy": policy.get("id"),
            "direction": "B to A", "files": reports, "device_validated": False,
            "scope": "file-local property transformations only; imports, compatibility, signing and runtime remain unverified"}
