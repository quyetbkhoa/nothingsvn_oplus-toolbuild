"""Bounded ELF dependency inspection; does not emulate Android's linker."""
import struct
import mmap


def inspect_elf(path):
    with path.open('rb') as stream:
        if stream.read(4) != b'\x7fELF':
            return None
        with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
            return _inspect_data(data)


def _inspect_data(data):
    if data[:4] != b'\x7fELF':
        return None
    if len(data) < 52 or data[4] not in (1, 2) or data[5] != 1:
        raise ValueError('Unsupported or truncated ELF')
    bits = 64 if data[4] == 2 else 32
    def unpack(fmt, offset):
        if offset < 0 or offset + struct.calcsize(fmt) > len(data):
            raise ValueError('ELF structure outside file')
        return struct.unpack_from(fmt, data, offset)
    if bits == 64:
        phoff = unpack('<Q', 32)[0]
        entsize, count = unpack('<HH', 54)
        fmt, expected = '<IIQQQQQQ', 56
    else:
        phoff = unpack('<I', 28)[0]
        entsize, count = unpack('<HH', 42)
        fmt, expected = '<IIIIIIII', 32
    if entsize < expected or count > 4096:
        raise ValueError('Invalid ELF program headers')
    loads, dynamic, interpreter = [], None, None
    for index in range(count):
        h = unpack(fmt, phoff + index * entsize)
        typ, offset, va, size, align = (h[0], h[2], h[3], h[5], h[7]) if bits == 64 else (h[0], h[1], h[2], h[4], h[7])
        if offset + size > len(data):
            raise ValueError('ELF segment outside file')
        if typ == 1:
            loads.append((va, offset, size, align))
        if typ == 2:
            dynamic = (offset, size)
        if typ == 3:
            interpreter = data[offset:offset + size].rstrip(b'\0').decode('utf-8')
    tags, needed, soname = {}, [], None
    if dynamic:
        width, dfmt = (16, '<qQ') if bits == 64 else (8, '<iI')
        if dynamic[1] % width:
            raise ValueError('Misaligned ELF dynamic table')
        for offset in range(dynamic[0], sum(dynamic), width):
            key, value = unpack(dfmt, offset)
            if key == 0:
                break
            if key == 1:
                needed.append(value)
            else:
                tags[key] = value
        if needed or 14 in tags:
            if 5 not in tags or 10 not in tags:
                raise ValueError('Missing ELF dynamic string table')
            matches = [(o + tags[5] - va, size - (tags[5] - va)) for va, o, size, _ in loads if va <= tags[5] < va + size]
            if len(matches) != 1 or tags[10] > matches[0][1]:
                raise ValueError('Invalid ELF string table address')
            table = data[matches[0][0]:matches[0][0] + tags[10]]
            def string(index):
                end = table.find(b'\0', index)
                if index < 0 or index >= len(table) or end < 0:
                    raise ValueError('Invalid ELF string index')
                return table[index:end].decode('utf-8')
            needed = [string(i) for i in needed]
            soname = string(tags[14]) if 14 in tags else None
    return {'bits': bits, 'machine': unpack('<H', 18)[0], 'needed': needed,
            'soname': soname, 'interpreter': interpreter,
            'load_alignments': [x[3] for x in loads]}
