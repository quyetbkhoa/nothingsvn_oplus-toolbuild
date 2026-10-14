"""Extract into an empty workspace, preserving source ROMs and metadata."""
import shutil
import os
from pathlib import Path
import zipfile

from .images import inspect_image, expand_sparse
from .io import confined, sha256, write_json, is_zip_container, copy_stream
from .tools import executable, run_tool


def extract(source: Path, destination: Path, partitions: list[str] | None = None,
            hashes: bool = True) -> dict:
    source = source.resolve(strict=True)
    destination = destination.resolve()
    if destination.exists():
        raise ValueError(f"Extraction destination must be new: {destination}")
    if destination == source or destination.is_relative_to(source) or source.is_relative_to(destination):
        raise ValueError("Source and destination must be separate")
    # Check dependencies before creating a workspace.
    ota_zip = source.is_file() and is_zip_container(source)
    if ota_zip:
        with zipfile.ZipFile(source) as archive:
            payloads = [x for x in archive.namelist() if x.rsplit("/", 1)[-1] == "payload.bin"]
        if len(payloads) != 1:
            raise ValueError("Expected a ZIP containing exactly one payload.bin")
        executable("payload-dumper-go")
    destination.mkdir(parents=True)
    raw = destination / "raw"
    raw.mkdir()
    fs = destination / "fs"
    fs.mkdir()
    if os.name == 'nt':
        from .workspace_fs import enable_case_sensitive
        enable_case_sensitive(fs)
    report = {"schema": 1, "source": str(source), "partitions": [], "status": "extracting"}
    try:
        if ota_zip:
            payload = destination / "payload.bin"
            with zipfile.ZipFile(source) as archive, archive.open(payloads[0]) as src, payload.open("xb") as dst:
                copy_stream(src, dst)
            args = ["-o", raw]
            if partitions:
                args += ["-p", ",".join(partitions)]
            run_tool("payload-dumper-go", args + [payload], destination / "payload.log")
            images = sorted(raw.glob("*.img"))
        elif source.is_dir():
            images = sorted(source.rglob("*.img"))
        else:
            images = [source]
        names = set()
        for image in images:
            name = image.stem
            if partitions and name not in partitions:
                continue
            if name in names:
                raise ValueError(f"Duplicate partition name: {name}")
            names.add(name)
            info = inspect_image(image)
            record = {"name": name, "source": str(image), "image": info}
            if hashes:
                record["sha256"] = sha256(image)
            actual = image
            if info["format"] == "android-sparse":
                actual = raw / f"{name}.expanded.img"
                expand_sparse(image, actual)
                info = inspect_image(actual)
                record["expanded_image"] = info
            if info["format"] == "erofs":
                run_tool("extract.erofs", ["-s", "-x", "-T4", "-i", actual, "-o", fs], destination / f"{name}.log")
                expected = fs / name
                if not expected.is_dir():
                    raise ValueError(f"Extractor produced no {expected}")
                record["filesystem"] = str(expected)
                record["status"] = "extracted"
            elif info["format"] == "ext":
                try:
                    executable("7z")
                except ValueError:
                    record["status"] = "raw-only"
                    record["reason"] = "7z unavailable for EXT file content extraction"
                else:
                    expected = fs / name
                    expected.mkdir()
                    run_tool("7z", ["l", "-slt", actual], destination / f"{name}.metadata.txt")
                    run_tool("7z", ["x", "-y", "-bb0", "-o" + str(expected), actual], destination / f"{name}.log")
                    record["filesystem"] = str(expected)
                    record["status"] = "extracted-content-only"
                    record["limitations"] = ["7-Zip content extraction and inode listing do not preserve Android SELinux xattrs/capabilities for repack."]
            else:
                record["status"] = "raw-only"
                record["reason"] = "No configured filesystem extractor for this format"
            report["partitions"].append(record)
            write_json(destination / "extraction.json", report)
        if partitions and set(partitions) != names:
            raise ValueError(f"Missing partitions: {sorted(set(partitions) - names)}")
        report["status"] = "complete"
        write_json(destination / "extraction.json", report)
        return report
    except Exception as exc:
        report.update(status="failed", error=str(exc))
        write_json(destination / "extraction.json", report)
        raise
