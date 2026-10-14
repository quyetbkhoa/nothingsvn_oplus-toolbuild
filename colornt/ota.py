"""Bounded OTA payload manifest inspection without reading or hashing ROM data.

Field numbers follow AOSP update_engine/update_metadata.proto (blob 6d16da40).
https://android.googlesource.com/platform/system/update_engine/+/refs/heads/main/update_metadata.proto
"""
from collections import defaultdict
from pathlib import Path
import struct
import zipfile

from .io import is_zip_container


def _varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    for shift in range(0, 70, 7):
        if offset >= len(data):
            raise ValueError("Truncated protobuf varint")
        byte = data[offset]
        offset += 1
        value |= (byte & 127) << shift
        if not byte & 128:
            return value, offset
    raise ValueError("Oversized protobuf varint")


def _fields(data: bytes) -> dict:
    result = defaultdict(list)
    offset = 0
    while offset < len(data):
        tag, offset = _varint(data, offset)
        field, wire = tag >> 3, tag & 7
        if not field:
            raise ValueError("Invalid protobuf field")
        if wire == 0:
            value, offset = _varint(data, offset)
        elif wire in (1, 5):
            length = 8 if wire == 1 else 4
            value = data[offset:offset + length]
            offset += length
        elif wire == 2:
            length, offset = _varint(data, offset)
            value = data[offset:offset + length]
            offset += length
        else:
            raise ValueError("Unsupported protobuf wire type")
        if offset > len(data):
            raise ValueError("Truncated protobuf field")
        result[field].append(value)
    return dict(result)


def _read_manifest(stream) -> dict:
    header = stream.read(20)
    if len(header) != 20 or header[:4] != b"CrAU":
        raise ValueError("Not an OTA payload")
    major, size = struct.unpack(">QQ", header[4:])
    if major not in (1, 2) or size > 64 * 1024 * 1024:
        raise ValueError("Unsupported or oversized OTA manifest")
    signature_size = 0
    if major == 2:
        data = stream.read(4)
        if len(data) != 4:
            raise ValueError("Truncated payload header")
        signature_size = struct.unpack(">I", data)[0]
    data = stream.read(size)
    if len(data) != size:
        raise ValueError("Truncated payload manifest")
    manifest = _fields(data)
    groups = []
    for dynamic in manifest.get(15, []):
        metadata = _fields(dynamic)
        for raw in metadata.get(1, []):
            group = _fields(raw)
            groups.append({"name": group[1][0].decode("utf-8"), "maximum_size": group.get(2, [0])[0],
                           "partitions": [x.decode("utf-8") for x in group.get(3, [])]})
    partitions = []
    for raw in manifest.get(13, []):
        partition = _fields(raw)
        info = _fields(partition[7][0]) if partition.get(7) else {}
        partitions.append({"name": partition[1][0].decode("utf-8"), "new_size": info.get(1, [None])[0]})
    return {"schema": 1, "major_version": major, "minor_version": manifest.get(12, [0])[0],
            "manifest_size": size, "metadata_signature_size": signature_size,
            "block_size": manifest.get(3, [4096])[0], "groups": groups, "partitions": partitions,
            "method": "payload header and bounded manifest only; no ROM checksum validation"}


def payload_info(source: Path) -> dict:
    source = source.resolve(strict=True)
    if is_zip_container(source):
        with zipfile.ZipFile(source) as archive:
            members = [x for x in archive.namelist() if x.rsplit("/", 1)[-1] == "payload.bin"]
            if len(members) != 1:
                raise ValueError("Expected one payload.bin")
            with archive.open(members[0]) as stream:
                return _read_manifest(stream)
    with source.open("rb") as stream:
        return _read_manifest(stream)


def check_groups(groups: list[dict], image_sizes: dict[str, int],
                 extra_assignments: dict[str, str] | None = None) -> list[dict]:
    """Account for omitted partitions and explicit extra-partition assignments."""
    extra_assignments = extra_assignments or {}
    names = {x["name"] for x in groups}
    if any(name not in names for name in extra_assignments.values()):
        raise ValueError("Unknown dynamic group assignment")
    reports = []
    assigned = set()
    for group in groups:
        members = set(group["partitions"]) | {p for p, name in extra_assignments.items() if name == group["name"]}
        if assigned & members:
            raise ValueError("Partition belongs to multiple dynamic groups")
        assigned.update(members)
        present = members & image_sizes.keys()
        if members - image_sizes.keys():
            raise ValueError("Missing dynamic partition sizes: " + ", ".join(sorted(members - image_sizes.keys())))
        used = sum((image_sizes[name] + 4095) // 4096 * 4096 for name in present)
        maximum = group["maximum_size"]
        if not maximum or used > maximum:
            raise ValueError(f"Dynamic group {group['name']} exceeds capacity: {used}/{maximum}")
        reports.append({"name": group["name"], "used_bytes": used, "maximum_bytes": maximum,
                        "remaining_bytes": maximum - used, "partitions": sorted(present)})
    if image_sizes.keys() - assigned:
        raise ValueError("Unassigned logical partitions: " + ", ".join(sorted(image_sizes.keys() - assigned)))
    return reports
