"""End-to-end replay of the recovered sample; original output is not an input."""
from pathlib import Path
import urllib.request
from urllib.parse import urlsplit
import shutil
import uuid

from .extract import extract
from .io import read_json, write_json
from .reconstruct import stage, rebuild, verify, _separate
from .tools import ROOT


def _download(source: str, destination: Path) -> Path:
    if urlsplit(source).scheme not in ("http", "https"):
        return Path(source).resolve(strict=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(".partial")
    request = urllib.request.Request(source, headers={"User-Agent": "ColorNT-Toolbuild/0.1"})
    try:
        with urllib.request.urlopen(request, timeout=60) as src, partial.open("xb") as dst:
            expected = src.headers.get("Content-Length")
            shutil.copyfileobj(src, dst, 1024 * 1024)
        if expected and partial.stat().st_size != int(expected):
            raise ValueError("Truncated ROM download")
        partial.rename(destination)
        return destination
    finally:
        partial.unlink(missing_ok=True)


def _extraction(source: Path, role: str, workspace: Path, explicit: Path | None) -> Path:
    cache = explicit or ROOT / "work" / role
    if (cache / "extraction.json").is_file():
        report = read_json(cache / "extraction.json")
        if report["status"] == "complete" and Path(report["source"]).resolve() == source:
            return cache.resolve()
        if explicit:
            raise ValueError(f"{role} extraction cache does not belong to this input")
    elif explicit:
        raise ValueError(f"Missing {role} extraction cache")
    out = workspace / role
    extract(source, out, hashes=False)
    return out


def execute_sample(base: str, donor: str, profile_path: Path, assets: Path,
                   destination: Path, base_workspace: Path | None = None,
                   donor_workspace: Path | None = None) -> dict:
    profile = read_json(profile_path)
    if profile.get("status") != "sample-verified":
        raise ValueError("Reconstruction profile must pass the recorded sample round-trip first")
    recipe_path = ROOT / profile["reconstruction"]["recipe"]
    if not recipe_path.is_file():
        raise ValueError("Missing recovered recipe")
    roots = [assets.resolve(strict=True)]
    for source in (base, donor):
        if urlsplit(source).scheme not in ("http", "https"):
            roots.append(Path(source).resolve(strict=True))
    out = _separate(destination, roots)
    workspace = ROOT / "work" / (out.name + "-" + uuid.uuid4().hex[:8])
    workspace.mkdir(parents=True)
    sources = {role: _download(source, workspace / "downloads" / (role + ".zip"))
               for role, source in (("base", base), ("donor", donor))}
    caches = {role: _extraction(sources[role], role, workspace, explicit)
              for role, explicit in (("base", base_workspace), ("donor", donor_workspace))}
    recipe = read_json(recipe_path)
    for role in ("base", "donor"):
        if sources[role].is_dir():
            actual = read_json(sources[role] / "firmware_info.json")
            for field in ("product_name", "version_name", "android_version", "security_patch", "arb_index"):
                if actual.get(field) != recipe["input_identity"][role].get(field):
                    raise ValueError(f"{role} input identity mismatch: {field}")
        # Folder flasher metadata is descriptive rather than cryptographic.
        # For OTA sources, validate the extracted build properties instead.
        if sources[role].is_file():
            expected = recipe["input_identity"][role]["version_name"]
            version = expected.split("_", 1)[1].split("(", 1)[0]
            model = expected.split("_", 1)[0]
            props = "\n".join(path.read_text(encoding="utf-8", errors="replace")
                               for path in (caches[role] / "fs").rglob("build.prop"))
            if model not in props or version not in props:
                raise ValueError(f"{role} OTA version does not match recovered sample")
    staged = workspace / "stage"
    stage(caches["base"] / "fs", caches["donor"] / "fs", assets, recipe_path, staged)
    # Raw payload images have a flat layout. Rebuild maps input partition names
    # by basename when the original folder layout isn't available.
    raw_inputs = {}
    for role in ("base", "donor"):
        if sources[role].is_dir():
            raw_inputs[role] = sources[role]
        else:
            raw_inputs[role] = caches[role] / "raw"
            write_json(raw_inputs[role] / "firmware_info.json", recipe["input_identity"][role])
    build_report = rebuild(raw_inputs["base"], raw_inputs["donor"], assets, recipe_path, staged, out)
    unpacked = workspace / "candidate"
    extract(out, unpacked, hashes=False)
    checked = verify(staged, unpacked / "fs", recipe_path)
    write_json(workspace / "verification.json", checked)
    if checked["status"] != "pass":
        raise ValueError(f"Candidate filesystem verification failed; inspect {workspace / 'verification.json'}")
    result = {"schema": 1, "status": "complete", "output": str(out), "workspace": str(workspace),
              "profile": profile["id"], "partitions": len(build_report["partitions"]),
              "validation": checked, "device_validated": False, "reference_required": False}
    write_json(out.parent / (out.name + ".pipeline.json"), result)
    return result
