"""Recover and replay a version-specific port recipe from A, B and an output.

The reference is needed once to derive/import changed assets. Replay uses only
the two inputs and the recovered assets. This does not infer unseen mod variants
or claim that prepatched APK/DEX assets work on other Android versions.
"""
from collections import Counter
import os
from pathlib import Path
import shutil
import struct
import re
import uuid as uuid_module

from .android_links import copy_entry, entry_digest, link_target, entry_path
from .images import inspect_image
from .io import confined, read_json, sha256, write_json
from .repack import repack_erofs
from .workspace_fs import android_directory


def _separate(destination: Path, roots: list[Path], resume: bool = False) -> Path:
    destination = destination.resolve()
    if destination.exists() and (not resume or not destination.is_dir()):
        raise ValueError(f"Destination must be new: {destination}")
    if any(destination == root or destination.is_relative_to(root) or root.is_relative_to(destination)
           for root in roots):
        raise ValueError("Destination overlaps a source directory")
    return destination


def _manifest_paths(entries: list[dict], key: str) -> None:
    seen = set()
    dummy = Path.cwd() / "work" / "path-validation"
    for entry in entries:
        path = confined(dummy, entry[key])
        name = path.as_posix()
        if name in seen:
            raise ValueError(f"Duplicate recipe path: {entry[key]}")
        seen.add(name)


def derive(base: Path, donor: Path, reference: Path, file_report: Path,
           image_report: Path, extraction_report: Path, asset_root: Path,
           recipe_path: Path, case_id: str, capacity_report: Path | None = None,
           extra_assignments: dict[str, str] | None = None) -> dict:
    """Import output-only changes and record exact source selection for each file."""
    roots = [x.resolve(strict=True) for x in (base, donor, reference)]
    a, b, c = roots
    if any(not x.is_dir() for x in roots):
        raise ValueError("Derivation requires extracted filesystem directories")
    assets = _separate(asset_root, roots)
    if recipe_path.exists():
        raise ValueError("Recipe destination must be new")
    report = read_json(file_report)
    if [str(x) for x in roots] != [report.get(x) for x in ("base", "donor", "output")]:
        raise ValueError("Comparison report belongs to different filesystem roots")
    images = read_json(image_report)
    extracted = read_json(extraction_report)
    if extracted["status"] != "complete":
        raise ValueError("Reference extraction is incomplete")
    by_name = {x["name"]: x for x in extracted["partitions"]}
    raw_root = Path(extracted["source"]).resolve(strict=True)
    image_roots = {}
    for role, fs in (("base", a), ("donor", b)):
        source = Path(read_json(fs.parent / "extraction.json")["source"]).resolve(strict=True)
        if not source.is_dir():
            raise ValueError("This recipe derivation expects input image folders")
        image_roots[role] = source
    recipe = {"schema": 1, "id": case_id, "kind": "sample-reconstruction",
              "variant": {"install_mods": True, "install_toolbox": True},
              "scope": "Exact supplied input versions and observed fastboot/mods/toolbox variant",
              "device_validated": False, "files": [], "directories": [], "metadata": [],
              "images": [], "evidence": {"files": str(file_report), "images": str(image_report)},
              "input_identity": {role: read_json(root / "firmware_info.json")
                                 for role, root in image_roots.items()},
              "limitations": ["Recovered APK/JAR assets are pinned to this sample; generic patch semantics remain to be implemented.",
                              "Opaque splash and EXT4 my_preload are recovered assets.",
                              "EROFS physical layout and compression bytes can differ.",
                              "No device boot validation, OTA signing or other flag combinations are implied."]}
    if capacity_report:
        recipe["dynamic_budget"] = {"groups": read_json(capacity_report)["groups"],
                                    "evidence": str(capacity_report), "extra_assignments": extra_assignments or {}}
    repacked = set()
    for row in images["partitions"]:
        if not row["output"]:
            continue
        source = Path(row["output"]).resolve(strict=True)
        name = source.stem
        info = by_name[name].get("expanded_image", by_name[name]["image"])
        entry = {"partition": name, "output": source.relative_to(raw_root).as_posix(),
                 "reference_size": source.stat().st_size, "reference_sha256": sha256(source)}
        if row["origin"] in ("base", "donor", "both"):
            role = "base" if row["origin"] != "donor" else "donor"
            selected = Path(row[role]).resolve(strict=True)
            entry.update(kind="copy", source=role,
                         path=selected.relative_to(image_roots[role]).as_posix())
        elif name.startswith("vbmeta"):
            selected = Path(row["base"]).resolve(strict=True)
            data = bytearray(selected.read_bytes())
            if data[:4] != b"AVB0" or len(data) < 256:
                raise ValueError("Invalid base vbmeta")
            data[120:124] = struct.pack(">I", 3)
            if bytes(data) != source.read_bytes():
                raise ValueError("Reference vbmeta is not the observed flags-only patch")
            entry.update(kind="vbmeta-flags", source="base", flags=3,
                         path=selected.relative_to(image_roots["base"]).as_posix())
        elif info["format"] == "erofs":
            repacked.add(name)
            options = (c / "config" / f"{name}_fs_options").read_text(encoding="utf-8")
            cluster = re.search(r"(?:^|\s)-C\s+(\d+)", options)
            entry.update(kind="repack-erofs", timestamp=info["build_time"],
                         uuid=str(uuid_module.UUID(info["uuid"])),
                         sparse=by_name[name]["image"]["format"] == "android-sparse",
                         capacity=info["size"], compression_cluster=int(cluster.group(1)) if cluster else 4096)
        else:
            asset = "images/" + source.name + ".dat"
            entry.update(kind="asset", asset=asset,
                         reason="Observed sample artifact; generic generator not yet recovered")
        recipe["images"].append(entry)
    _manifest_paths(recipe["images"], "output")
    # Validate all reference paths before creating the imported asset directory.
    chosen = [row for row in report["entries"]
              if row["origin"] != "absent-in-output" and row["path"].split("/")[0] in repacked]
    _manifest_paths(chosen, "path")
    android_directory(assets)
    for index, row in enumerate(chosen):
        path = row["path"]
        actual = entry_path(c, path)
        digest = entry_digest(actual)
        entry = {"path": path, "sha256": digest, "size": actual.lstat().st_size,
                 "mtime_ns": actual.lstat().st_mtime_ns, "origin": row["origin"]}
        target = link_target(actual)
        if target is not None:
            entry["link_target"] = target
        origin = row["origin"]
        if origin == "both":
            role = "base" if path.split("/")[0] in ("vendor", "odm") else "donor"
        else:
            role = origin
        if role in ("base", "donor"):
            entry.update(source=role, source_path=path)
        else:
            asset = "files/" + path
            # Keep imported link representations as regular payload bytes. Replay
            # restores link type instead of letting the host follow their target.
            imported = confined(assets, asset)
            imported.parent.mkdir(parents=True, exist_ok=True)
            if actual.is_symlink():
                from .android_links import MAGIC
                imported.write_bytes(MAGIC + (target + "\0").encode("utf-16-le"))
            else:
                shutil.copyfile(actual, imported)
            entry.update(source="assets", source_path=asset)
        recipe["files"].append(entry)
        if index % 2000 == 0:
            print(f"Deriving files: {index}/{len(chosen)}", flush=True)
    for name in sorted(repacked):
        for path in sorted((c / name).rglob("*")):
            if path.is_dir() and not path.is_symlink():
                recipe["directories"].append({"path": path.relative_to(c).as_posix(),
                                               "mtime_ns": path.stat().st_mtime_ns})
        recipe["directories"].append({"path": name, "mtime_ns": (c / name).stat().st_mtime_ns})
        for suffix in ("fs_config", "file_contexts"):
            path = f"config/{name}_{suffix}"
            actual = confined(c, path)
            asset = "metadata/" + path
            dest = confined(assets, asset)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(actual, dest)
            recipe["metadata"].append({"path": path, "asset": asset, "sha256": sha256(actual)})
    for entry in recipe["images"]:
        if entry["kind"] == "asset":
            dest = confined(assets, entry["asset"])
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(raw_root / entry["output"], dest)
    installs = list(raw_root.glob("*.install"))
    if len(installs) != 1:
        raise ValueError("Expected one reference .install file")
    recipe["install"] = {"name": installs[0].name, "metadata": read_json(installs[0])}
    recipe["counts"] = dict(Counter(x["source"] for x in recipe["files"]))
    recipe["asset_bytes"] = sum(x.stat().st_size for x in assets.rglob("*") if x.is_file())
    write_json(recipe_path, recipe)
    return {"status": "derived", "recipe": str(recipe_path.resolve()),
            "assets": str(assets), "files": len(recipe["files"]), "counts": recipe["counts"],
            "images": len(recipe["images"]), "asset_bytes": recipe["asset_bytes"],
            "device_validated": False}


def _validate_recipe(recipe: dict) -> None:
    if recipe.get("schema") != 1 or recipe.get("kind") != "sample-reconstruction":
        raise ValueError("Unsupported reconstruction recipe")
    for key, field in (("files", "path"), ("directories", "path"),
                       ("metadata", "path"), ("images", "output")):
        _manifest_paths(recipe[key], field)
    names = [x["partition"] for x in recipe["images"]]
    if len(names) != len(set(names)):
        raise ValueError("Duplicate partition basename")
    for entry in recipe["files"]:
        if entry["source"] not in ("base", "donor", "assets"):
            raise ValueError("Unknown file source")
    for entry in recipe["images"]:
        if entry["kind"] not in ("copy", "vbmeta-flags", "asset", "repack-erofs"):
            raise ValueError("Unknown image recipe")
        if entry["output"].split("/")[0] not in ("system", "firmware-update", "images"):
            raise ValueError("Unknown output group")
        if Path(entry["output"]).name != entry["partition"] + ".img":
            raise ValueError("Partition path/name mismatch")


def verify_assets(recipe_path: Path, assets: Path, content: bool = True) -> dict:
    """Check the complete ignored asset store, independently of ROM inputs."""
    recipe = read_json(recipe_path)
    _validate_recipe(recipe)
    root = assets.resolve(strict=True)
    expected = {}
    for entry in recipe["files"]:
        if entry["source"] == "assets":
            expected[entry["source_path"]] = (entry["sha256"], entry.get("link_target"), entry.get("size"))
    for entry in recipe["metadata"]:
        expected[entry["asset"]] = (entry["sha256"], None, None)
    for entry in recipe["images"]:
        if entry["kind"] == "asset":
            expected[entry["asset"]] = (entry["reference_sha256"], None, entry["reference_size"])
    issues = []
    for name, (digest, target, size) in expected.items():
        path = entry_path(root, name)
        if not path.is_file() and not path.is_symlink():
            issues.append({"path": name, "reason": "missing asset"})
        elif link_target(path) != target or (content and entry_digest(path) != digest):
            issues.append({"path": name, "reason": "content/link mismatch"})
        elif target is None and size is not None and path.stat().st_size != size:
            issues.append({"path": name, "reason": "size mismatch"})
    actual = {p.relative_to(root).as_posix(): p for p in root.rglob("*") if p.is_file() or p.is_symlink()}
    for name in sorted(actual.keys() - expected.keys()):
        issues.append({"path": name, "reason": "unexpected asset"})
    total = sum(p.lstat().st_size for p in actual.values())
    if recipe.get("asset_bytes") is not None and total != recipe["asset_bytes"]:
        issues.append({"reason": "asset byte total mismatch", "actual": total, "expected": recipe["asset_bytes"]})
    return {"schema": 1, "status": "fail" if issues else "pass", "recipe": recipe["id"],
            "assets": str(root), "expected_files": len(expected), "actual_files": len(actual),
            "bytes": total, "issues": issues, "content_verified": content and not issues,
            "scope": "asset integrity only; no ROM/device validation" if content else "asset presence/size/link check; content digests not checked"}


def import_assets(recipe_path: Path, reference: Path, reference_images: Path, destination: Path) -> dict:
    """Restore ignored assets in a fresh checkout from the supplied output."""
    recipe = read_json(recipe_path)
    _validate_recipe(recipe)
    fs, images = [x.resolve(strict=True) for x in (reference, reference_images)]
    if not fs.is_dir() or not images.is_dir():
        raise ValueError("Asset import requires reference filesystem and image folders")
    out = _separate(destination, [fs, images])
    prepared = []
    for entry in recipe["files"]:
        if entry["source"] != "assets":
            continue
        source = entry_path(fs, entry["path"])
        if entry_digest(source) != entry["sha256"] or link_target(source) != entry.get("link_target"):
            raise ValueError(f"Reference mod asset differs from recipe: {entry['path']}")
        prepared.append((entry["source_path"], source, True))
    for entry in recipe["metadata"]:
        source = confined(fs, entry["path"])
        if sha256(source) != entry["sha256"]:
            raise ValueError("Reference Android metadata differs from recipe")
        prepared.append((entry["asset"], source, False))
    for entry in recipe["images"]:
        if entry["kind"] != "asset":
            continue
        source = confined(images, entry["output"])
        if sha256(source) != entry["reference_sha256"]:
            raise ValueError("Reference image asset differs from recipe")
        prepared.append((entry["asset"], source, False))
    _manifest_paths([{"path": name} for name, _, _ in prepared], "path")
    android_directory(out)
    for name, source, link_allowed in prepared:
        target = confined(out, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        if link_allowed and source.is_symlink():
            from .android_links import MAGIC
            target.write_bytes(MAGIC + (link_target(source) + "\0").encode("utf-16-le"))
        else:
            shutil.copyfile(source, target)
    return {"status": "imported", "assets": str(out), "files": len(prepared),
            "recipe": recipe["id"], "bytes": sum(x.stat().st_size for x in out.rglob("*") if x.is_file())}


def stage(base: Path, donor: Path, assets: Path, recipe_path: Path, destination: Path,
          source_overrides: dict | None = None, progress=None) -> dict:
    """Compose new trees using the recovered source map; no reference needed."""
    recipe = read_json(recipe_path)
    _validate_recipe(recipe)
    roots = {role: root.resolve(strict=True) for role, root in
             (("base", base), ("donor", donor), ("assets", assets))}
    overrides = {}
    known_parts = {(r['source'], r['source_path'].split('/')[0]) for r in recipe['files']}
    for role, partitions in (source_overrides or {}).items():
        for part, path in partitions.items():
            if role not in ('base', 'donor') or (role, part) not in known_parts:
                raise ValueError('Unknown reconstruction source override')
            root = Path(path).resolve(strict=True)
            if not root.is_dir() or not (root / part).is_dir():
                raise ValueError('Reconstruction source override must be an extracted filesystem root')
            overrides[(role, part)] = root
    out = _separate(destination, list(roots.values()) + list(overrides.values()))
    prepared = []
    for entry in recipe["files"]:
        source_root = overrides.get((entry['source'], entry['source_path'].split('/')[0]), roots[entry['source']])
        source = entry_path(source_root, entry["source_path"])
        if not source.exists() and not source.is_symlink():
            raise ValueError(f"Missing reconstruction source: {source}")
        target_link = link_target(source)
        if target_link != entry.get("link_target"):
            raise ValueError(f"Link type/target mismatch: {source}")
        if target_link is None and source.stat().st_size != entry["size"]:
            raise ValueError(f"Source size mismatch: {source}")
        # Pin imported assets before creating the candidate workspace.
        if entry["source"] == "assets" and entry_digest(source) != entry["sha256"]:
            raise ValueError(f"Recovered asset checksum mismatch: {source}")
        prepared.append((entry, source))
    for entry in recipe["metadata"]:
        source = confined(roots["assets"], entry["asset"])
        if sha256(source) != entry["sha256"]:
            raise ValueError("Recovered metadata checksum mismatch")
    android_directory(out)
    report = {"schema": 1, "status": "staging", "recipe": recipe["id"],
              "files": 0, "reference_required": False, "device_validated": False}
    try:
        for entry in recipe["directories"]:
            confined(out, entry["path"]).mkdir(parents=True, exist_ok=True)
        for index, (entry, source) in enumerate(prepared):
            dest = confined(out, entry["path"])
            copy_entry(source, dest, entry["mtime_ns"])
            if entry_digest(dest) != entry["sha256"]:
                raise ValueError(f"Reconstructed file differs from sample: {entry['path']}")
            report["files"] += 1
            if index % 2000 == 0:
                message = f"Staging files: {index}/{len(prepared)}"
                if progress:
                    progress(message)
                else:
                    print(message, flush=True)
        for entry in recipe["metadata"]:
            dest = confined(out, entry["path"])
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(confined(roots["assets"], entry["asset"]), dest)
        # Apply directory timestamps after children have been created.
        for entry in sorted(recipe["directories"], key=lambda x: len(x["path"]), reverse=True):
            stamp = entry["mtime_ns"]
            os.utime(confined(out, entry["path"]), ns=(stamp, stamp))
        report["status"] = "complete"
        write_json(out.parent / (out.name + ".stage.json"), report)
        return report
    except Exception as exc:
        report.update(status="failed", error=str(exc))
        write_json(out.parent / (out.name + ".stage.json"), report)
        raise


def _sparse(raw: Path, destination: Path) -> None:
    """Encode small EROFS with room for sparse framing in the logical size.

    AOSP fastbootd compares downloaded bytes against the block-device size
    before expanding sparse data. RAW-only framing is larger than its payload,
    so reserve one trailing block with a DONT_CARE chunk. It is outside the
    filesystem's declared blocks; budget checks use the new expanded size.
    """
    size = raw.stat().st_size
    if size % 4096 or size > 0xFFFFFFFF - 12:
        raise ValueError("Sparse encoder requires block-aligned image below 4 GiB")
    with raw.open("rb") as src, destination.open("xb") as dst:
        dst.write(struct.pack("<I4H4I", 0xED26FF3A, 1, 0, 28, 12, 4096, size // 4096 + 1, 2, 0))
        dst.write(struct.pack("<HHII", 0xCAC1, 0, size // 4096, size + 12))
        shutil.copyfileobj(src, dst, 1024 * 1024)
        dst.write(struct.pack("<HHII", 0xCAC3, 0, 1, 12))


def rebuild(base_images: Path, donor_images: Path, assets: Path, recipe_path: Path,
            staged: Path, destination: Path, partitions: list[str] | None = None,
            resume: bool = False) -> dict:
    """Repack recovered trees and assemble an unflashed candidate image folder."""
    recipe = read_json(recipe_path)
    _validate_recipe(recipe)
    roots = {role: root.resolve(strict=True) for role, root in
             (("base", base_images), ("donor", donor_images), ("assets", assets), ("stage", staged))}
    out = _separate(destination, list(roots.values()), resume=resume)
    cached = {}
    report_path = out.parent / (out.name + ".build.json")
    if out.exists():
        if not report_path.is_file():
            raise ValueError("Cannot resume an output without its build report")
        previous = read_json(report_path)
        if previous.get("recipe") != recipe["id"]:
            raise ValueError("Resume output belongs to a different recipe")
        cached = {x["name"]: x for x in previous["partitions"]}
    stage_report = roots["stage"].parent / (roots["stage"].name + ".stage.json")
    if not stage_report.is_file():
        raise ValueError("Staging has not completed")
    status = read_json(stage_report)
    if status.get("status") != "complete" or status.get("recipe") != recipe["id"]:
        raise ValueError("Staging failed or belongs to another recipe")
    for role in ("base", "donor"):
        actual = read_json(roots[role] / "firmware_info.json")
        expected = recipe["input_identity"][role]
        for field in ("product_name", "version_name", "android_version", "security_patch", "arb_index"):
            if actual.get(field) != expected.get(field):
                raise ValueError(f"{role} input identity mismatch: {field}")
    entries = [x for x in recipe["images"] if not partitions or x["partition"] in partitions]
    if partitions and set(partitions) != {x["partition"] for x in entries}:
        raise ValueError("Unknown requested partition")
    resolved = {}
    for entry in entries:
        if entry["kind"] in ("copy", "vbmeta-flags"):
            source = confined(roots[entry["source"]], entry["path"])
            if not source.is_file():
                matches = list(roots[entry["source"]].rglob(entry["partition"] + ".img"))
                if len(matches) != 1:
                    raise ValueError("Missing/ambiguous input partition basename")
                source = matches[0]
            if not source.is_file() or source.stat().st_size != entry["reference_size"]:
                raise ValueError(f"Image size/source mismatch: {entry['partition']}")
            resolved[entry["partition"]] = source
        elif entry["kind"] == "asset":
            if sha256(confined(roots["assets"], entry["asset"])) != entry["reference_sha256"]:
                raise ValueError("Recovered image asset checksum mismatch")
        else:
            tree = confined(roots["stage"], entry["partition"])
            if not tree.is_dir():
                raise ValueError("Missing staged partition")
            for suffix in ("fs_config", "file_contexts"):
                if not (roots["stage"] / "config" / f"{entry['partition']}_{suffix}").is_file():
                    raise ValueError("Missing staged Android metadata")
    out.mkdir(parents=True, exist_ok=resume)
    report = {"schema": 1, "status": "building", "recipe": recipe["id"], "partitions": [],
              "device_validated": False, "reference_required": False,
              "filesystem_validation_required": True, "subset": bool(partitions)}
    try:
        for entry in entries:
            print(f"Rebuilding {entry['partition']} ({entry['kind']})", flush=True)
            dest = confined(out, entry["output"])
            dest.parent.mkdir(parents=True, exist_ok=True)
            kind = entry["kind"]
            if resume and dest.is_file():
                digest = sha256(dest)
                info = inspect_image(dest)
                prior = cached.get(entry["partition"])
                if prior:
                    if digest != prior["sha256"] or prior["kind"] != kind:
                        raise ValueError("Resume image differs from its recorded build")
                    if kind != "repack-erofs" and digest != entry["reference_sha256"]:
                        raise ValueError("Resume image differs from current recipe")
                    report["partitions"].append(prior)
                    continue
                # A capacity failure occurs after mkfs completes but before the
                # image is added to the report. Reuse it only as an unverified
                # candidate; mandatory final round-trip validates every file.
                if kind == "repack-erofs" and not entry["sparse"] and info["format"] == "erofs":
                    if info["uuid"] != entry["uuid"].replace("-", "") or info["build_time"] != entry["timestamp"]:
                        raise ValueError("Unrecorded resume image has different filesystem identity")
                    report["partitions"].append({"name": entry["partition"], "kind": kind, "image": info,
                                                "sha256": digest, "byte_identical_to_reference": digest == entry["reference_sha256"],
                                                "resumed_unverified": True})
                    continue
                raise ValueError("Cannot reuse this unrecorded resume image")
            if kind in ("copy", "vbmeta-flags"):
                shutil.copyfile(resolved[entry["partition"]], dest)
                if kind == "vbmeta-flags":
                    with dest.open("r+b") as stream:
                        if stream.read(4) != b"AVB0":
                            raise ValueError("Invalid vbmeta header")
                        stream.seek(120)
                        stream.write(struct.pack(">I", entry["flags"]))
            elif kind == "asset":
                shutil.copyfile(confined(roots["assets"], entry["asset"]), dest)
            else:
                raw = out.parent / (out.name + ".repack") / (entry["partition"] + ".img") if entry["sparse"] else dest
                result = repack_erofs(roots["stage"] / entry["partition"], roots["stage"] / "config",
                                      raw, entry["timestamp"], entry["uuid"], preserve_mtime=True,
                                      cluster_size=entry.get("compression_cluster", 4096),
                                      log_path=out.parent / (out.name + ".repack") / (entry["partition"] + ".log"))
                if not recipe.get("dynamic_budget") and result["image"]["size"] > entry["capacity"]:
                    raise ValueError(f"Repacked image exceeds sample capacity: {entry['partition']}")
                if entry["sparse"]:
                    _sparse(raw, dest)
            digest = sha256(dest)
            if kind != "repack-erofs" and digest != entry["reference_sha256"]:
                raise ValueError(f"Reconstructed image differs: {entry['partition']}")
            report["partitions"].append({"name": entry["partition"], "kind": kind,
                                          "image": inspect_image(dest), "sha256": digest,
                                          "byte_identical_to_reference": digest == entry["reference_sha256"]})
            write_json(out.parent / (out.name + ".build.json"), report)
        if not partitions:
            if recipe.get("dynamic_budget"):
                from .ota import check_groups
                sizes = {x["name"]: x["image"].get("expanded_size", x["image"]["size"])
                         for x in report["partitions"] if (out / "system" / (x["name"] + ".img")).is_file()}
                budget = recipe["dynamic_budget"]
                report["dynamic_budget"] = check_groups(budget["groups"], sizes, budget.get("extra_assignments"))
            write_json(confined(out, recipe["install"]["name"]), recipe["install"]["metadata"])
        report["status"] = "complete"
        write_json(out.parent / (out.name + ".build.json"), report)
        return report
    except Exception as exc:
        report.update(status="failed", error=str(exc))
        write_json(out.parent / (out.name + ".build.json"), report)
        raise


def verify(staged: Path, unpacked: Path, recipe_path: Path, partitions: list[str] | None = None) -> dict:
    """Check contents, link targets, empty directories and Android metadata."""
    recipe = read_json(recipe_path)
    _validate_recipe(recipe)
    roots = [staged.resolve(strict=True), unpacked.resolve(strict=True)]
    names = {x["partition"] for x in recipe["images"] if x["kind"] == "repack-erofs"}
    if partitions:
        if not set(partitions) <= names:
            raise ValueError("Verification only covers recipe EROFS partitions")
        names = set(partitions)
    issues = []
    checked = 0
    for entry in recipe["files"]:
        if entry["path"].split("/")[0] not in names:
            continue
        for role, root in zip(("stage", "re-extracted"), roots):
            path = entry_path(root, entry["path"])
            if not path.exists() and not path.is_symlink():
                issues.append({"path": entry["path"], "root": role, "reason": "missing"})
            elif entry_digest(path) != entry["sha256"] or link_target(path) != entry.get("link_target"):
                issues.append({"path": entry["path"], "root": role, "reason": "content/link mismatch"})
        checked += 1
    expected_files = {x["path"] for x in recipe["files"] if x["path"].split("/")[0] in names}
    expected_dirs = {x["path"] for x in recipe["directories"] if x["path"].split("/")[0] in names}
    for root in roots:
        actual_files, actual_dirs = set(), set()
        for name in names:
            for path in (root / name).rglob("*"):
                relative = path.relative_to(root).as_posix()
                (actual_dirs if path.is_dir() and not path.is_symlink() else actual_files).add(relative)
            if (root / name).is_dir():
                actual_dirs.add(name)
        for path in sorted(actual_files - expected_files):
            issues.append({"path": path, "root": str(root), "reason": "unexpected file"})
        for path in sorted(expected_dirs ^ actual_dirs):
            issues.append({"path": path, "root": str(root), "reason": "directory set differs"})
    for entry in recipe["metadata"]:
        if not any(entry["path"] == f"config/{name}_{suffix}"
                   for name in names for suffix in ("fs_config", "file_contexts")):
            continue
        aa, bb = [confined(root, entry["path"]) for root in roots]
        if not aa.is_file() or not bb.is_file():
            issues.append({"path": entry["path"], "reason": "missing Android metadata"})
        else:
            if sha256(aa) != entry["sha256"]:
                issues.append({"path": entry["path"], "reason": "staged metadata differs from recovered sample"})
            left = set(aa.read_text(encoding="utf-8").splitlines())
            right = set(bb.read_text(encoding="utf-8").splitlines())
            if left != right:
                issues.append({"path": entry["path"], "reason": "Android metadata differs",
                               "missing_lines": sorted(left - right)[:20], "added_lines": sorted(right - left)[:20]})
    return {"schema": 1, "status": "pass" if not issues else "fail", "files_checked": checked,
            "partitions": sorted(names), "issues": issues, "device_validated": False,
            "checks": ["file bytes", "Android link targets", "empty directories", "uid/gid/mode/capabilities", "SELinux contexts"],
            "limitations": ["This comparison does not establish device boot or generic mod behavior.",
                            "Image layout, file hardlink identity and sub-100ns timestamps are not compared."]}
