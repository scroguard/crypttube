"""Fetch an image from a user-supplied URL, safely.

This is the only place the application makes an outbound request, and only when a user
explicitly asks for it (setting an image "from URL"). Letting users make the server fetch
URLs is a classic way to reach things only the server can see (SSRF), so:

- only http(s), no credentials in the URL;
- the host must resolve exclusively to public internet addresses: no loopback, private
  (LAN), link-local (cloud metadata), CGNAT, multicast or reserved ranges;
- the connection is made to the address that was validated (the name is not resolved a
  second time), with the original name used for TLS verification and the Host header;
- redirects are followed by hand, each hop validated the same way;
- size and time are capped, no cookies or referrer are sent, proxies from the environment
  are ignored.
"""

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from urllib.parse import urljoin

import httpx

MAX_REDIRECTS = 4
USER_AGENT = "goon-streamer image fetch"


class FetchError(Exception):
    """A user-presentable reason the URL could not be fetched."""


def is_public_address(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if addr.version == 6 and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return addr.is_global and not addr.is_multicast


async def resolve_host(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise FetchError(f"could not resolve {host}") from e
    return list(dict.fromkeys(info[4][0] for info in infos))


Resolver = Callable[[str, int], Awaitable[list[str]]]


async def _pinned_target(url: httpx.URL, resolve: Resolver) -> tuple[httpx.URL, dict, dict]:
    """Validate one hop. Returns the URL rewritten to the vetted IP, plus the headers
    and extensions that keep virtual hosting and TLS verification on the real name."""
    if url.scheme not in ("http", "https"):
        raise FetchError("only http and https URLs are supported")
    if url.userinfo:
        raise FetchError("URLs with embedded credentials are not supported")
    host = url.host
    if not host:
        raise FetchError("the URL has no host")
    port = url.port or (443 if url.scheme == "https" else 80)

    try:
        ipaddress.ip_address(host)
        addresses = [host]
    except ValueError:
        addresses = await resolve(host, port)
    # If a name maps to both public and internal addresses, treat it as internal.
    if not addresses or not all(is_public_address(a) for a in addresses):
        raise FetchError("that address is not on the public internet")

    ip = addresses[0]
    pinned = url.copy_with(host=f"[{ip}]" if ":" in ip else ip)
    default_port = url.port in (None, 80 if url.scheme == "http" else 443)
    headers = {"Host": host if default_port else f"{host}:{url.port}"}
    extensions = {"sni_hostname": host} if url.scheme == "https" else {}
    return pinned, headers, extensions


async def fetch_image_bytes(
    url: str,
    *,
    max_bytes: int,
    timeout: float = 15.0,
    deadline: float = 60.0,
    resolve: Resolver = resolve_host,
    transport: httpx.AsyncBaseTransport | None = None,
) -> bytes:
    """Download `url` (following validated redirects) and return its body. `timeout`
    applies to each step; `deadline` to the whole download, lookups included."""
    try:
        current = httpx.URL(url.strip())
    except (httpx.InvalidURL, ValueError) as e:
        raise FetchError("that is not a valid URL") from e
    try:
        async with asyncio.timeout(deadline):
            return await _fetch(current, max_bytes, timeout, resolve, transport)
    except TimeoutError as e:
        raise FetchError("the server took too long to respond") from e


async def _fetch(current: httpx.URL, max_bytes: int, timeout: float, resolve: Resolver,
                 transport: httpx.AsyncBaseTransport | None) -> bytes:
    async with httpx.AsyncClient(
        follow_redirects=False, trust_env=False, timeout=httpx.Timeout(timeout, connect=8.0),
        headers={"User-Agent": USER_AGENT, "Accept": "image/*"}, transport=transport,
    ) as client:
        for _ in range(MAX_REDIRECTS + 1):
            pinned, headers, extensions = await _pinned_target(current, resolve)
            try:
                async with client.stream("GET", pinned, headers=headers, extensions=extensions) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get("location")
                        if not location:
                            raise FetchError("the server sent a redirect without a destination")
                        current = httpx.URL(urljoin(str(current), location))
                        continue
                    if response.status_code != 200:
                        raise FetchError(f"the server answered with HTTP {response.status_code}")
                    declared = response.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > max_bytes:
                        raise FetchError("the file is too large")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body += chunk
                        if len(body) > max_bytes:
                            raise FetchError("the file is too large")
                    if not body:
                        raise FetchError("the server sent an empty response")
                    return bytes(body)
            except httpx.TimeoutException as e:
                raise FetchError("the server took too long to respond") from e
            except httpx.HTTPError as e:
                raise FetchError(f"could not download the image ({type(e).__name__})") from e
        raise FetchError("too many redirects")
