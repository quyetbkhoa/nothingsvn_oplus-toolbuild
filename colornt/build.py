"""Build planning and conservative assembly of fully pinned image recipes.

Filesystem/framework modifications are separate evidence-derived recipes. This
assembler refuses incomplete profiles rather than silently producing a stock ROM.
"""
import os
from pathlib import Path
import shutil
import urllib.request
from urllib.parse import urlsplit
import zipfile

from .io import confined, read_json, sha256, write_json


def boolean(value: str) -> bool:
    if value.lower() not in ("true", "false"):
        raise ValueError(f"Expected true/false: {value}")
    return value.lower() == "true"


def plan(base: str, donor: str | None, profile: Path, mode: str = "both",
         mods: bool = True, toolbox: bool = True, asset_root: Path | None = None) -> dict:
    if mode not in ("fastboot", "ota", "both"):
        raise ValueError(f"Unsupported build mode: {mode}")
    config = read_json(profile)
    problems = list(config.get("blockers", []))
    if config.get("status") not in ("verified", "sample-verified"):
        problems.append("Profile is not verified against the input/output sample")
    if config.get("variant") != {"install_mods": mods, "install_toolbox": toolbox}:
        problems.append("Profile has no verified recipe for this mods/toolbox flag combination")
    if mode != "fastboot":
        problems.append("OTA generator/signing/NT Manager compatibility is not implemented yet")
    for source, label in ((base, "base"), (donor, "donor")):
        if source is None:
            continue
        scheme = urlsplit(source).scheme
        if scheme not in ("http", "https") and not Path(source).exists():
            problems.append(f"Missing {label} ROM: {source}")
    expected_donor = config.get("requires_donor", True)
    if expected_donor and not donor:
        problems.append("This profile requires a donor ROM")
    if not config.get("partitions"):
        problems.append("No verified partition assembly recipe")
    if mods and not config.get("mods_complete", False):
        problems.append("Mod implementation/assets have not been fully verified")
    if toolbox and not config.get("toolbox_complete", False):
        problems.append("Toolbox implementation/assets have not been fully verified")
    if config.get("reconstruction"):
        from .tools import ROOT
        recovered = config["reconstruction"]
        recipe_path = ROOT / recovered["recipe"]
        assets = asset_root or ROOT / recovered["assets"]
        if not recipe_path.is_file():
            problems.append("Missing recovered sample recipe")
        if not assets.is_dir():
            problems.append("Recovered mod assets must be imported locally from the supplied output")
        elif recipe_path.is_file():
            from .reconstruct import verify_assets
            check = verify_assets(recipe_path, assets, content=False)
            if check["status"] != "pass":
                problems.append(f"Recovered asset store is incomplete: {len(check['issues'])} presence/size/link issues; run verify-reconstruction-assets")
    return {"schema": 1, "base": base, "donor": donor, "direction": "B to A",
            "profile": config.get("id"), "build_mode": mode, "install_mods": mods,
            "install_toolbox": toolbox, "ready": not problems, "blockers": problems,
            "stages": ["acquire", "extract", "compare compatibility", "select hardware partitions",
                       "apply verified mods", "repack", "verify", "package"],
            "features": config.get("features", [])}


def acquire(source: str, destination: Path, expected_sha256: str) -> Path:
    """Download only into a new workspace and verify a mandatory checksum."""
    if urlsplit(source).scheme not in ("http", "https"):
        path = Path(source).resolve(strict=True)
        if path.is_file() and sha256(path) != expected_sha256:
            raise ValueError("Local input checksum mismatch")
        return path
    if not expected_sha256:
        raise ValueError("URL download requires a pinned input SHA-256")
    if destination.exists():
        raise ValueError(f"Download target already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(destination.name + ".partial")
    try:
        request = urllib.request.Request(source, headers={"User-Agent": "ColorNT-Toolbuild/0.1"})
        with urllib.request.urlopen(request, timeout=60) as src, temp.open("xb") as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)
        if sha256(temp) != expected_sha256:
            raise ValueError("Downloaded ROM checksum mismatch")
        temp.rename(destination)
    finally:
        temp.unlink(missing_ok=True)
    return destination


def build_images(base: Path, donor: Path | None, profile: Path, asset_root: Path,
                 destination: Path, mods: bool, toolbox: bool) -> dict:
    """Assemble raw images from pinned base, donor and mod assets.

    This is an implemented backend for exact image recipes. A device profile
    must cover every partition with hashes and use source groups explicitly.
    """
    config = read_json(profile)
    if config.get("reconstruction"):
        raise ValueError("Recovered filesystem profiles must use the reconstruction pipeline")
    check = plan(str(base), str(donor) if donor else None, profile, "fastboot", mods, toolbox)
    if not check["ready"]:
        raise ValueError("Build blocked: " + "; ".join(check["blockers"]))
    roots = {"base": base.resolve(strict=True), "assets": asset_root.resolve(strict=True)}
    if donor:
        roots["donor"] = donor.resolve(strict=True)
    if any(not value.is_dir() for value in roots.values()):
        raise ValueError("Image assembly needs extracted image directories")
    destination = destination.resolve()
    if destination.exists():
        raise ValueError("Build destination must be new")
    if any(destination == value or destination.is_relative_to(value) or value.is_relative_to(destination)
           for value in roots.values()):
        raise ValueError("Build destination overlaps source directories")
    prepared = []
    outputs = set()
    partition_names = set()
    for entry in config["partitions"]:
        if entry["source"] not in roots:
            raise ValueError(f"Missing source group {entry['source']}")
        source = confined(roots[entry["source"]], entry["path"])
        target = confined(destination, entry["output"])
        if target in outputs or target.suffix != ".img" or target.name in partition_names:
            raise ValueError("Duplicate/invalid partition output")
        if entry["output"].split("/")[0] not in ("system", "firmware-update", "images"):
            raise ValueError("Unknown output partition group")
        outputs.add(target)
        partition_names.add(target.name)
        if not entry.get("sha256") or not source.is_file() or sha256(source) != entry["sha256"]:
            raise ValueError(f"Partition checksum mismatch: {source}")
        prepared.append((entry, source, target))
    if not prepared:
        raise ValueError("Empty partition recipe")
    install = config.get("install_metadata")
    if not install or not install.get("Devices", {}).get("Model"):
        raise ValueError("Missing target .install metadata")
    model = install["Devices"]["Model"]
    confined(destination, model + ".install")
    destination.mkdir(parents=True)
    report = {"schema": 1, "status": "building", "profile": config["id"], "partitions": []}
    try:
        for entry, source, target in prepared:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            digest = sha256(target)
            if digest != entry["sha256"]:
                raise ValueError(f"Copied image checksum mismatch: {target}")
            report["partitions"].append({"path": entry["output"], "source": entry["source"], "sha256": digest})
        write_json(destination / (model + ".install"), install)
        report["status"] = "complete"
        write_json(destination.with_name(destination.name + ".build.json"), report)
        return report
    except Exception as exc:
        report.update(status="failed", error=str(exc))
        write_json(destination.with_name(destination.name + ".build.json"), report)
        raise


def package(source: Path, destination: Path) -> dict:
    """ZIP an assembled image folder. Packaging is not device validation."""
    source = source.resolve(strict=True)
    destination = destination.resolve()
    if not source.is_dir() or not list(source.glob("*.install")):
        raise ValueError("Expected ROM folder with .install metadata")
    if destination.exists() or destination.is_relative_to(source):
        raise ValueError("Package destination must be new and outside ROM folder")
    files = sorted(source.rglob("*"))
    if any(path.is_symlink() for path in files):
        raise ValueError("Image package cannot contain symlinks")
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".partial")
    if partial.exists():
        raise ValueError("Partial package already exists")
    try:
        with zipfile.ZipFile(partial, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True) as archive:
            for path in files:
                if path.is_file():
                    archive.write(path, path.relative_to(source).as_posix())
        with zipfile.ZipFile(partial) as archive:
            bad = archive.testzip()
            if bad:
                raise ValueError(f"ZIP verification failed: {bad}")
        partial.rename(destination)
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    return {"path": str(destination), "size": destination.stat().st_size,
            "sha256": sha256(destination), "device_validated": False}
