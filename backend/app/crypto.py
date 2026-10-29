"""Encryption at rest for users' own uploads (and what is generated from them).

Turned on by giving the stack a key file (see README, "Encrypting user uploads"). With
no key, nothing is encrypted and everything below is a pass-through.

File format ("GSENC1"): a 128-byte header, then the content in 1 MiB chunks, each sealed
with AES-256-GCM. Every file has its own random data key, stored in the header wrapped
(AES-GCM) with the master key. Chunk i uses the nonce prefix + i and authenticates its
index and whether it is the last chunk, so chunks can't be reordered, swapped between
files or cut off without detection. Any chunk can be decrypted on its own, which keeps
HTTP Range requests and video seeking cheap.

The master key never leaves memory. Code reads media through `open_media()` (a seekable
plaintext view of a file, encrypted or not) and hands encrypted files to ffmpeg through
`ffmpeg_input()`: a URL on a loopback-only HTTP server inside this process, so the
plaintext never touches the disk.
"""

import base64
import binascii
import contextlib
import contextvars
import hashlib
import io
import logging
import os
import secrets
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

log = logging.getLogger(__name__)

MAGIC = b"GSENC1\0\0"
HEADER_SIZE = 128
CHUNK = 1024 * 1024
TAG = 16
# magic | chunk size | plain size | key id | wrap nonce | wrapped data key | nonce prefix
HEADER = struct.Struct(">8sIQ8s12s48s4s")


class CryptoError(OSError):
    """An encrypted file can't be read: wrong or missing key, or damaged/tampered data."""


# ---------------------------------------------------------------------------
# The master key
# ---------------------------------------------------------------------------

_key: bytes | None = None
_key_loaded = False


def parse_key(raw: bytes) -> bytes | None:
    """32 bytes, given as base64 (`openssl rand -base64 32`), hex, or raw. Empty = no key."""
    text = raw.strip()
    if not text:
        return None
    try:
        key = base64.b64decode(text, validate=True)
        if len(key) == 32:
            return key
    except (binascii.Error, ValueError):
        pass
    try:
        key = bytes.fromhex(text.decode("ascii"))
        if len(key) == 32:
            return key
    except (ValueError, UnicodeDecodeError):
        pass
    if len(raw) == 32:
        return raw
    raise ValueError("the uploads key file must hold a 32-byte key, e.g. made with `openssl rand -base64 32`")


def load_key(path: str | os.PathLike | None) -> bytes | None:
    """Read the key file once. A missing or empty file means encryption is off."""
    global _key, _key_loaded
    _key_loaded = True
    _key = None
    if path:
        try:
            raw = Path(path).read_bytes()
        except FileNotFoundError:
            raw = b""
        except IsADirectoryError as e:
            raise ValueError(f"the uploads key file {path} is a directory") from e
        except PermissionError as e:
            raise ValueError(
                f"the uploads key file can't be read by the app (uid {os.getuid()}): give the key file on the "
                f"host to the same user as PUID (e.g. `chown {os.getuid()} keyfile; chmod 400 keyfile`)") from e
        _key = parse_key(raw)
    if _key:
        log.info("encryption of user uploads is on (key %s)", key_id().hex())
    return _key


def set_key(key: bytes | None) -> None:
    """For tests."""
    global _key, _key_loaded
    _key, _key_loaded = key, True


def master_key() -> bytes | None:
    if not _key_loaded:
        from app.config import get_settings
        load_key(get_settings().uploads_key_file)
    return _key


def enabled() -> bool:
    return master_key() is not None


def key_id(key: bytes | None = None) -> bytes:
    # The label keeps the project's old name: it is part of every encrypted file's header.
    return hashlib.sha256(b"goonstreamer uploads key id" + (key or master_key() or b"")).digest()[:8]


# ---------------------------------------------------------------------------
# Format
# ---------------------------------------------------------------------------

def _chunk_nonce(prefix: bytes, index: int) -> bytes:
    return prefix + struct.pack(">Q", index)


def _aad(index: int, last: bool) -> bytes:
    return struct.pack(">Q?", index, last)


def is_encrypted(path: str | os.PathLike) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(len(MAGIC)) == MAGIC
    except OSError:
        return False


class _Header:
    def __init__(self, raw: bytes, path: str):
        if len(raw) < HEADER.size:
            raise CryptoError(f"{path}: truncated encrypted file")
        magic, self.chunk, self.size, kid, wrap_nonce, wrapped, self.prefix = HEADER.unpack(raw[:HEADER.size])
        if magic != MAGIC:
            raise CryptoError(f"{path}: not an encrypted file")
        key = master_key()
        if key is None:
            raise CryptoError(f"{path} is encrypted, but no uploads key is configured")
        if kid != key_id(key):
            raise CryptoError(f"{path} was encrypted with a different uploads key")
        try:
            self.data_key = AESGCM(key).decrypt(wrap_nonce, wrapped, MAGIC + kid)
        except InvalidTag as e:
            raise CryptoError(f"{path}: the file's key can't be unlocked (damaged header?)") from e
        self.chunks = max(1, -(-self.size // self.chunk))


def _new_header(size: int, data_key: bytes, prefix: bytes) -> bytes:
    key = master_key()
    assert key is not None
    kid = key_id(key)
    wrap_nonce = secrets.token_bytes(12)
    wrapped = AESGCM(key).encrypt(wrap_nonce, data_key, MAGIC + kid)
    return HEADER.pack(MAGIC, CHUNK, size, kid, wrap_nonce, wrapped, prefix).ljust(HEADER_SIZE, b"\0")


class EncryptedReader(io.RawIOBase):
    """Seekable plaintext view of an encrypted file."""

    def __init__(self, path: str | os.PathLike):
        self.path = str(path)
        self._f = open(path, "rb")
        try:
            self._h = _Header(self._f.read(HEADER_SIZE), self.path)
        except BaseException:
            self._f.close()
            raise
        self._aead = AESGCM(self._h.data_key)
        self._pos = 0
        self._cached: tuple[int, bytes] | None = None

    @property
    def size(self) -> int:
        return self._h.size

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self._pos, io.SEEK_END: self._h.size}[whence]
        self._pos = max(0, base + offset)
        return self._pos

    def _chunk(self, index: int) -> bytes:
        if self._cached and self._cached[0] == index:
            return self._cached[1]
        h = self._h
        self._f.seek(HEADER_SIZE + index * (h.chunk + TAG))
        sealed = self._f.read(h.chunk + TAG)
        try:
            plain = self._aead.decrypt(_chunk_nonce(h.prefix, index), sealed, _aad(index, index == h.chunks - 1))
        except InvalidTag as e:
            raise CryptoError(f"{self.path}: chunk {index} failed authentication (damaged or tampered)") from e
        self._cached = (index, plain)
        return plain

    def readinto(self, buffer) -> int:
        if self._pos >= self._h.size:
            return 0
        index, offset = divmod(self._pos, self._h.chunk)
        data = self._chunk(index)[offset:offset + len(buffer)]
        buffer[:len(data)] = data
        self._pos += len(data)
        return len(data)

    def read_range(self, start: int, length: int) -> bytes:
        self.seek(start)
        out = bytearray()
        while len(out) < length:
            part = self.read(length - len(out))
            if not part:
                break
            out += part
        return bytes(out)

    def close(self) -> None:
        if not self.closed:
            self._f.close()
        super().close()


class EncryptingWriter(io.RawIOBase):
    """Write plaintext, store it encrypted. `close()` seals the last chunk and the header
    (the file must be seekable: the plaintext size is written into the header last)."""

    def __init__(self, f):
        self._f = f
        self._data_key = AESGCM.generate_key(256)
        self._aead = AESGCM(self._data_key)
        self._prefix = secrets.token_bytes(4)
        self._buf = bytearray()
        self._index = 0
        self._size = 0
        f.write(bytes(HEADER_SIZE))           # placeholder, rewritten at the end

    def writable(self) -> bool:
        return True

    def _seal(self, data: bytes, last: bool) -> None:
        self._f.write(self._aead.encrypt(_chunk_nonce(self._prefix, self._index), data, _aad(self._index, last)))
        self._index += 1

    def write(self, data) -> int:
        self._buf += data
        self._size += len(data)
        # always keep at least one byte back, so the final chunk is the one marked "last"
        while len(self._buf) > CHUNK:
            self._seal(bytes(self._buf[:CHUNK]), False)
            del self._buf[:CHUNK]
        return len(data)

    def close(self) -> None:
        if self.closed:
            return
        self._seal(bytes(self._buf), True)
        self._buf.clear()
        self._f.seek(0)
        self._f.write(_new_header(self._size, self._data_key, self._prefix))
        self._f.flush()
        super().close()


def encrypt_bytes(data: bytes) -> bytes:
    out = io.BytesIO()
    w = EncryptingWriter(out)
    w.write(data)
    w.close()
    return out.getvalue()


# ---------------------------------------------------------------------------
# Reading media, encrypted or not
# ---------------------------------------------------------------------------

def open_media(path: str | os.PathLike):
    """A seekable binary file object with the plaintext of `path`."""
    if is_encrypted(path):
        return io.BufferedReader(EncryptedReader(path), buffer_size=CHUNK)
    return open(path, "rb")


def plain_size(path: str | os.PathLike, stat_size: int | None = None) -> int:
    """The size of the content (an encrypted file is a little larger on disk)."""
    if is_encrypted(path):
        with EncryptedReader(path) as r:
            return r.size
    return os.path.getsize(path) if stat_size is None else stat_size


def read_all(path: str | os.PathLike) -> bytes:
    with open_media(path) as f:
        return f.read()


def is_private(path: str | os.PathLike) -> bool:
    """Is this a user's own upload (or under the uploads volume)?"""
    from app.config import get_settings
    try:
        return Path(path).resolve().is_relative_to(get_settings().uploads_dir.resolve())
    except OSError:
        return False


# Generated files (thumbnails, previews) made from a private source are encrypted too.
# Code that generates from such a source runs inside `private_outputs(source_path)`;
# the context is inherited by asyncio.to_thread, so the writers deep down see it.
_private_outputs: contextvars.ContextVar[bool] = contextvars.ContextVar("private_outputs", default=False)


@contextlib.contextmanager
def private_outputs(source: str | os.PathLike | None):
    token = _private_outputs.set(bool(source) and enabled() and is_private(source))
    try:
        yield
    finally:
        _private_outputs.reset(token)


@contextlib.asynccontextmanager
async def async_private_outputs(source: str | os.PathLike | None):
    with private_outputs(source):
        yield


def outputs_private() -> bool:
    return _private_outputs.get()


# ---------------------------------------------------------------------------
# ffmpeg input: a loopback HTTP server streaming plaintext (Range-capable)
# ---------------------------------------------------------------------------

_server: ThreadingHTTPServer | None = None
_server_lock = threading.Lock()
_served: dict[str, str] = {}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:       # quiet; paths are private
        pass

    def _serve(self, body: bool) -> None:
        path = _served.get(self.path.lstrip("/"))
        if path is None:
            self.send_error(404)
            return
        try:
            reader = EncryptedReader(path)
        except OSError:
            self.send_error(500)
            return
        with reader:
            size = reader.size
            start, end = 0, size - 1
            rng = self.headers.get("Range", "")
            status = 200
            if rng.startswith("bytes="):
                first, _, last = rng[6:].partition("-")
                try:
                    if first:
                        start, end = int(first), min(int(last), size - 1) if last else size - 1
                    elif last:
                        start = max(0, size - int(last))
                    status = 206
                except ValueError:
                    pass
            if start >= size and size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(status)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(max(0, end - start + 1)))
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if not body:
                return
            reader.seek(start)
            left = end - start + 1
            try:
                while left > 0:
                    part = reader.read(min(CHUNK, left))
                    if not part:
                        break
                    self.wfile.write(part)
                    left -= len(part)
            except (BrokenPipeError, ConnectionResetError):
                pass                              # ffmpeg seeks by dropping the connection

    def do_GET(self) -> None:
        self._serve(True)

    def do_HEAD(self) -> None:
        self._serve(False)


def _ensure_server() -> ThreadingHTTPServer:
    global _server
    with _server_lock:
        if _server is None:
            _server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
            _server.daemon_threads = True
            threading.Thread(target=_server.serve_forever, name="decrypt-http", daemon=True).start()
        return _server


@contextlib.contextmanager
def ffmpeg_input(path: str):
    """What to give ffmpeg/ffprobe as input for `path`: the path itself, or for an
    encrypted file a private loopback URL valid while the block runs."""
    if not is_encrypted(path):
        yield path
        return
    server = _ensure_server()
    token = secrets.token_urlsafe(24)
    _served[token] = path
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/{token}"
    finally:
        _served.pop(token, None)


# ---------------------------------------------------------------------------
# Encrypting files in place
# ---------------------------------------------------------------------------

def encrypt_file_in_place(path: str | os.PathLike) -> bool:
    """Encrypt a plain file where it is: written beside it, then swapped in atomically,
    keeping its permissions and modification time (so a rescan sees nothing new).
    Returns False if it was already encrypted."""
    path = Path(path)
    if is_encrypted(path):
        return False
    st = path.stat()
    tmp = path.with_name(f".enc-{secrets.token_hex(6)}-{path.name}"[:255])
    try:
        with open(path, "rb") as src, open(tmp, "wb") as raw:
            writer = EncryptingWriter(raw)
            while block := src.read(CHUNK * 4):
                writer.write(block)
            writer.close()
            raw.flush()
            os.fsync(raw.fileno())
        os.chmod(tmp, st.st_mode & 0o7777)
        os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return True
