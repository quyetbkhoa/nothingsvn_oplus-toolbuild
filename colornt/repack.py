"""EROFS repacking using explicit extracted Android ownership and SELinux data."""
from pathlib import Path
import re

from .images import inspect_image
from .io import sha256
from .tools import executable, native_path, run_tool


def repack_erofs(tree: Path, config_dir: Path, output: Path, timestamp: int, uuid: str,
                 preserve_mtime: bool = False, cluster_size: int | None = None,
                 log_path: Path | None = None) -> dict:
    tree = tree.resolve(strict=True)
    config_dir = config_dir.resolve(strict=True)
    output = output.resolve()
    if not tree.is_dir() or output.exists() or output.is_relative_to(tree):
        raise ValueError("EROFS input must be a directory and output a new external file")
    if timestamp < 0 or not re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", uuid):
        raise ValueError("Explicit nonnegative timestamp and UUID required")
    name = tree.name
    fs_config = config_dir / f"{name}_fs_config"
    contexts = config_dir / f"{name}_file_contexts"
    if not fs_config.is_file() or not contexts.is_file():
        raise ValueError("Both fs_config and file_contexts are required")
    tool = executable("mkfs.erofs")
    output.parent.mkdir(parents=True, exist_ok=True)
    args = ["--quiet", "-zlz4hc,9", "-T", str(timestamp), "-U", uuid,
            "--mount-point", "/" + name,
            "--fs-config-file=" + native_path(fs_config, tool),
            "--file-contexts=" + native_path(contexts, tool), output, tree]
    if preserve_mtime:
        args[1:1] = ["--mkfs-time", "--preserve-mtime"]
    if cluster_size is not None:
        if not 4096 <= cluster_size <= 1048576 or cluster_size % 4096:
            raise ValueError("Invalid EROFS compression cluster size")
        args[1:1] = ["-C", str(cluster_size)]
    run_tool("mkfs.erofs", args, log_path or output.with_name(output.name + ".log"))
    if not output.is_file() or inspect_image(output)["format"] != "erofs":
        raise ValueError("Repacker did not produce an EROFS image")
    return {"path": str(output), "sha256": sha256(output), "image": inspect_image(output),
            "avb_signed": False, "device_validated": False,
            "note": "Caller must verify metadata, partition budgets and boot/AVB compatibility."}
