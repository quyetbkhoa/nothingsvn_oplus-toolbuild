"""Read APK binary manifests and compare APK/JAR members with standard Python."""
from pathlib import Path
import struct
import zipfile


def _strings(data: bytes, offset: int) -> list[str]:
    _, header, size = struct.unpack_from("<HHI", data, offset)
    count, _, flags, start, _ = struct.unpack_from("<5I", data, offset + 8)
    if size < header or offset + size > len(data) or count > size // 4:
        raise ValueError("Invalid binary XML string pool")
    utf8 = bool(flags & 0x100)

    def length(pos):
        if utf8:
            value = data[pos]
            if value & 0x80:
                return ((value & 0x7F) << 8) | data[pos + 1], pos + 2
            return value, pos + 1
        value = struct.unpack_from("<H", data, pos)[0]
        if value & 0x8000:
            return ((value & 0x7FFF) << 16) | struct.unpack_from("<H", data, pos + 2)[0], pos + 4
        return value, pos + 2

    strings = []
    for index in range(count):
        pos = offset + start + struct.unpack_from("<I", data, offset + header + index * 4)[0]
        _, pos = length(pos)
        if utf8:
            byte_count, pos = length(pos)
            strings.append(data[pos:pos + byte_count].decode("utf-8", "replace"))
        else:
            count16 = length(offset + start + struct.unpack_from("<I", data, offset + header + index * 4)[0])[0]
            strings.append(data[pos:pos + count16 * 2].decode("utf-16-le", "replace"))
    return strings


def binary_manifest(data: bytes) -> dict:
    if len(data) < 8 or struct.unpack_from("<H", data)[0] != 0x0003:
        raise ValueError("Expected Android binary XML")
    _, offset, total = struct.unpack_from("<HHI", data)
    if total != len(data):
        raise ValueError("Invalid binary XML length")
    strings = []
    tags = []
    while offset < total:
        kind, header, size = struct.unpack_from("<HHI", data, offset)
        if size < header or size < 8 or offset + size > total:
            raise ValueError("Invalid binary XML chunk")
        if kind == 0x0001:
            strings = _strings(data, offset)
        elif kind == 0x0102:
            ext = offset + header
            _, name, attr_start, attr_size, attr_count = struct.unpack_from("<IIHHH", data, ext)
            if attr_size < 20:
                raise ValueError("Invalid XML attribute size")
            attrs = {}
            for index in range(attr_count):
                pos = ext + attr_start + index * attr_size
                if pos + 20 > offset + size:
                    raise ValueError("XML attribute outside chunk")
                ns, attr_name, raw = struct.unpack_from("<III", data, pos)
                value_type = data[pos + 15]
                value = struct.unpack_from("<I", data, pos + 16)[0]
                if raw != 0xFFFFFFFF:
                    content = strings[raw]
                elif value_type == 3:
                    content = strings[value]
                elif value_type == 18:
                    content = bool(value)
                elif value_type in (16, 17):
                    content = value
                else:
                    content = f"@{value:08x}"
                attrs[strings[attr_name]] = content
            tags.append({"tag": strings[name], "attributes": attrs})
        offset += size
    return {"tags": tags}


def apk_info(path: Path) -> dict:
    with zipfile.ZipFile(path) as archive:
        xml = binary_manifest(archive.read("AndroidManifest.xml"))
        dex = sorted(x.filename for x in archive.infolist() if x.filename.endswith(".dex"))
    manifest = next((x["attributes"] for x in xml["tags"] if x["tag"] == "manifest"), {})
    sdk = next((x["attributes"] for x in xml["tags"] if x["tag"] == "uses-sdk"), {})
    app = next((x["attributes"] for x in xml["tags"] if x["tag"] == "application"), {})
    permissions = [x["attributes"].get("name") for x in xml["tags"] if x["tag"] == "uses-permission"]
    return {"path": str(path.resolve()), "package": manifest.get("package"),
            "version_name": manifest.get("versionName"), "version_code": manifest.get("versionCode"),
            "sdk": sdk, "application": app, "permissions": permissions, "dex": dex}


def archive_diff(left: Path, right: Path) -> dict:
    with zipfile.ZipFile(left) as a, zipfile.ZipFile(right) as b:
        aa = {x.filename: x for x in a.infolist() if not x.is_dir()}
        bb = {x.filename: x for x in b.infolist() if not x.is_dir()}
        changes = []
        equal = 0
        for name in sorted(aa.keys() | bb.keys()):
            if name not in aa:
                status = "added"
            elif name not in bb:
                status = "removed"
            elif aa[name].file_size != bb[name].file_size:
                status = "changed"
            else:
                # Compare actual content; CRC is insufficient for evidence.
                with a.open(name) as src_a, b.open(name) as src_b:
                    status = "equal"
                    while True:
                        data_a, data_b = src_a.read(1024 * 1024), src_b.read(1024 * 1024)
                        if data_a != data_b:
                            status = "changed"
                            break
                        if not data_a:
                            break
            if status == "equal":
                equal += 1
            else:
                changes.append({"path": name, "status": status})
    return {"left": str(left.resolve()), "right": str(right.resolve()),
            "equal": equal, "changes": changes}
