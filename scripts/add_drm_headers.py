"""Add DRM init boxes (pssh) to an MP4 whose moov comes after mdat, as some downloaded
files have: the content itself is not encrypted, only the headers are left over."""
import struct, sys
WIDEVINE = bytes.fromhex("edef8ba979d64acea3c827dcd51d21ed")
PLAYREADY = bytes.fromhex("9a04f07998404286ab92e65be0885f95")

def pssh(system_id: bytes, data: bytes) -> bytes:
    body = b"\0\0\0\0" + system_id + struct.pack(">I", len(data)) + data
    return struct.pack(">I", 8 + len(body)) + b"pssh" + body

src, dest = sys.argv[1], sys.argv[2]
b = bytearray(open(src, "rb").read())
off, moov = 0, None
while off < len(b):
    size, typ = struct.unpack(">I4s", b[off:off + 8])
    if size == 1:                      # 64-bit size follows (an mdat over 4 GiB)
        size = struct.unpack(">Q", b[off + 8:off + 16])[0]
    elif size == 0:                    # runs to the end of the file
        size = len(b) - off
    if size < 8:
        sys.exit(f"bad box size {size} at offset {off}")
    if typ == b"moov":
        moov = (off, size)
    off += size
if moov is None:
    sys.exit("no moov box found")
mo, ms = moov
assert mo + ms == len(b), "moov must be the last box"
extra = pssh(WIDEVINE, b"\x08\x01\x12\x10" + bytes(16)) + pssh(PLAYREADY, bytes(32))
if struct.unpack(">I", b[mo:mo + 4])[0] == 1:     # 64-bit size field
    b[mo + 8:mo + 16] = struct.pack(">Q", ms + len(extra))
else:
    b[mo:mo + 4] = struct.pack(">I", ms + len(extra))
open(dest, "wb").write(bytes(b) + extra)
