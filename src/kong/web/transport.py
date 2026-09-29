"""Public-address HTTP with validated, pinned DNS and bounded identity bodies."""
import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

import httpx


class WebError(ValueError):
    pass


def public_url(value):
    if len(value) > 4000 or any(ord(c) < 32 for c in value) or "\\" in value:
        raise WebError("Invalid public URL")
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None:
            raise WebError("Only credential-free HTTP(S) URLs are supported")
        if parsed.port not in {None, 80, 443}:
            raise WebError("Only standard HTTP(S) ports are supported")
        host = parsed.hostname.encode("idna").decode("ascii").lower().rstrip(".")
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            raise WebError("Private hosts are not supported")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise WebError("Private or special addresses are not supported")
        return str(httpx.URL(value).copy_with(host=host, fragment=None))
    except (ValueError, UnicodeError, httpx.InvalidURL) as exc:
        raise WebError("Invalid or nonpublic HTTP(S) URL") from exc


async def request_public(method, url, *, timeout, max_bytes, headers=None, json_body=None, proxy_url=None):
    original = httpx.URL(public_url(url))
    try:
        async with asyncio.timeout(timeout):
            addresses = await asyncio.get_running_loop().getaddrinfo(
                original.host, original.port or (443 if original.scheme == "https" else 80),
                type=socket.SOCK_STREAM)
            ips = list(dict.fromkeys(entry[4][0] for entry in addresses))
            # Explicitly configured trusted proxies may use RFC2544 fake-IP DNS.
            # Other private resolutions remain blocked. The proxy owns final DNS
            # and must enforce destination policy; direct mode always pins an IP.
            def allowed(ip):
                address = ipaddress.ip_address(ip)
                return address.is_global or (proxy_url is not None and address in ipaddress.ip_network("198.18.0.0/15"))
            if not ips or any(not allowed(ip) for ip in ips):
                raise WebError("Host resolves to private or special addresses")
            # Connect to the validated address, never resolve the hostname again.
            # TLS still verifies the original hostname via httpcore's SNI extension.
            pinned = original if proxy_url else original.copy_with(host=ips[0])
            host_header = original.netloc.decode("ascii")
            request_headers = {"User-Agent": "Kong-Agent/0.3 public-research", "Accept-Encoding": "identity",
                               **(headers or {}), "Host": host_header}
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=timeout, proxy=proxy_url) as client:
                async with client.stream(method, pinned, headers=request_headers, json=json_body,
                                         extensions={"sni_hostname": original.host}) as response:
                    if response.headers.get("content-encoding", "identity").lower() not in {"identity", ""}:
                        raise WebError("Server returned compressed content despite identity request")
                    body = bytearray()
                    async for chunk in response.aiter_raw():
                        if len(body) + len(chunk) > max_bytes:
                            raise WebError("HTTP response exceeds configured byte limit")
                        body.extend(chunk)
                    return response.status_code, dict(response.headers), bytes(body), response.encoding or "utf-8"
    except WebError:
        raise
    except (httpx.HTTPError, OSError, TimeoutError) as exc:
        # Do not serialize request objects, auth headers or provider response bodies.
        raise WebError(f"Web request failed ({type(exc).__name__}); no automatic retry or provider switch") from exc
