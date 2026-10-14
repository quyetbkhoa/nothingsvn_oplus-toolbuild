"""Inspect raw Android image headers without changing or loading large images."""
import struct
import os
from pathlib import Path

SPARSE_MAGIC = 0xED26FF3A
EROFS_MAGIC = 0xE0F5E1E2


def inspect_image(path: Path) -> dict:
    size = path.stat().st_size
    with path.open("rb") as stream:
        data = stream.read(4096)
        footer = b""
        if size >= 64:
            stream.seek(-64, 2)
            footer = stream.read(64)
    result = {"format": "unknown", "size": size}
    if len(data) >= 28 and struct.unpack_from("<I", data)[0] == SPARSE_MAGIC:
        _, major, minor, file_header, chunk_header, block, blocks, chunks, checksum = struct.unpack_from("<I4H4I", data)
        result.update(format="android-sparse", major=major, minor=minor,
                      file_header_size=file_header, chunk_header_size=chunk_header,
                      block_size=block, expanded_size=block * blocks, chunks=chunks,
                      checksum=checksum)
    elif len(data) >= 1152 and struct.unpack_from("<I", data, 1024)[0] == EROFS_MAGIC:
        sb = data[1024:1152]
        result.update(format="erofs", block_size=1 << sb[12],
                      root_nid=struct.unpack_from("<H", sb, 14)[0],
                      inode_count=struct.unpack_from("<Q", sb, 16)[0],
                      build_time=struct.unpack_from("<Q", sb, 24)[0],
                      blocks=struct.unpack_from("<I", sb, 36)[0],
                      metadata_block=struct.unpack_from("<I", sb, 40)[0],
                      feature_compat=struct.unpack_from("<I", sb, 8)[0],
                      feature_incompat=struct.unpack_from("<I", sb, 80)[0],
                      uuid=sb[48:64].hex(),
                      volume_name=sb[64:80].split(b"\0", 1)[0].decode("utf-8", "replace"))
    elif len(data) >= 1082 and data[1080:1082] == b"\x53\xef":
        result.update(format="ext", block_size=1024 << struct.unpack_from("<I", data, 1048)[0])
    elif data.startswith(b"AVB0"):
        if len(data) < 256:
            raise ValueError(f"Truncated AVB header: {path}")
        result.update(format="avb-vbmeta", flags=struct.unpack_from(">I", data, 120)[0],
                      rollback_index=struct.unpack_from(">Q", data, 112)[0],
                      algorithm=struct.unpack_from(">I", data, 28)[0])
    elif data.startswith(b"ANDROID!"):
        result.update(format="android-boot", header_version=struct.unpack_from("<I", data, 40)[0])
    elif data.startswith(b"VNDRBOOT"):
        result.update(format="android-vendor-boot", header_version=struct.unpack_from("<I", data, 8)[0])
    elif data.startswith(b"CrAU"):
        result.update(format="android-payload", version=struct.unpack_from(">Q", data, 4)[0])
    if footer.startswith(b"AVBf") and result["format"] != "android-sparse":
        result["avb_footer"] = dict(zip(
            ("major", "minor", "original_image_size", "vbmeta_offset", "vbmeta_size"),
            struct.unpack_from(">IIQQQ", footer, 4)))
    return result


def expand_sparse(source: Path, destination: Path) -> None:
    """Expand sparse chunks with bounded memory. Refuse malformed/truncated data."""
    if source.resolve() == destination.resolve() or destination.exists():
        raise ValueError("Sparse output must be a new file distinct from input")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(destination.name + ".partial")
    if temp.exists():
        raise ValueError(f"Partial output already exists: {temp}")
    owned = False
    try:
        with source.open("rb") as src, temp.open("xb", buffering=0) as dst:
            owned = True
            header = src.read(28)
            if len(header) != 28:
                raise ValueError("Truncated sparse header")
            magic, major, minor, fh, ch, block, blocks, chunks, checksum = struct.unpack("<I4H4I", header)
            if magic != SPARSE_MAGIC or major != 1 or fh < 28 or ch < 12 or not block or block % 4:
                raise ValueError("Unsupported sparse header")
            if len(src.read(fh - 28)) != fh - 28:
                raise ValueError("Truncated sparse extended header")
            total = 0
            for _ in range(chunks):
                raw = src.read(ch)
                if len(raw) != ch:
                    raise ValueError("Truncated sparse chunk header")
                kind, _, count, length = struct.unpack_from("<2H2I", raw)
                payload_size = length - ch
                expanded = count * block
                if total + count > blocks:
                    raise ValueError('Sparse chunks exceed declared size')
                if kind == 0xCAC1:
                    if payload_size != expanded:
                        raise ValueError("Invalid raw chunk length")
                    remaining = expanded
                    while remaining:
                        data = src.read(min(remaining, 1024 * 1024))
                        if not data:
                            raise ValueError("Truncated raw chunk")
                        from .io import before_write
                        before_write(temp, len(data))
                        dst.write(data)
                        remaining -= len(data)
                elif kind == 0xCAC2:
                    fill = src.read(4)
                    if payload_size != 4 or len(fill) != 4:
                        raise ValueError("Invalid fill chunk")
                    data = fill * (256 * 1024)
                    remaining = expanded
                    while remaining:
                        from .io import before_write
                        before_write(temp, min(remaining, len(data)))
                        dst.write(data[:min(remaining, len(data))])
                        remaining -= min(remaining, len(data))
                elif kind == 0xCAC3:
                    if payload_size != 0:
                        raise ValueError("Invalid empty chunk")
                    from .io import before_write
                    before_write(temp, expanded)
                    dst.seek(expanded, 1)
                    # Make admitted logical holes visible to quota rescans now.
                    dst.truncate(dst.tell())
                elif kind == 0xCAC4:
                    if count != 0 or payload_size != 4 or len(src.read(4)) != 4:
                        raise ValueError("Invalid CRC chunk")
                else:
                    raise ValueError(f"Unknown sparse chunk: {kind:#x}")
                total += count
                if total > blocks:
                    raise ValueError("Sparse chunks exceed declared size")
            if total != blocks:
                raise ValueError("Sparse output block count mismatch")
            from .io import before_write
            before_write(temp, max(0, block * blocks - os.fstat(dst.fileno()).st_size))
            dst.truncate(block * blocks)
        temp.rename(destination)
    except Exception:
        if owned:
            temp.unlink(missing_ok=True)
        raise
