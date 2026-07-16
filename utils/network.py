"""HTTP helpers that validate every redirect target before connecting."""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from typing import Any
from urllib.parse import urljoin, urlparse

from exceptions import NetworkError, PathTraversalError

_REDIRECT_STATUSES = {301, 302, 303, 307, 308}


def validate_public_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise PathTraversalError("Only absolute HTTP/HTTPS URLs are allowed")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise PathTraversalError("URL port is invalid") from exc
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, port)}
    except socket.gaierror as exc:
        raise NetworkError(f"Cannot resolve {parsed.hostname}") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise NetworkError(f"Private or non-global address is blocked: {ip}")


def request_with_safe_redirects(
    client: Any,
    method: str,
    url: str,
    *,
    validator: Callable[[str], None] = validate_public_url,
    max_redirects: int = 5,
    **kwargs: Any,
) -> Any:
    """Follow bounded redirects only after validating each resolved destination."""
    current_url = url
    current_method = method.upper()
    kwargs.pop("allow_redirects", None)
    for redirect_count in range(max_redirects + 1):
        validator(current_url)
        request = getattr(client, current_method.lower())
        response = request(current_url, allow_redirects=False, **kwargs)
        if response.status_code not in _REDIRECT_STATUSES:
            return response
        location = response.headers.get("Location")
        if not location:
            return response
        if redirect_count == max_redirects:
            response.close()
            raise NetworkError(f"Too many redirects while requesting {url}")
        status_code = response.status_code
        next_url = urljoin(response.url or current_url, location)
        response.close()
        if status_code == 303 and current_method != "HEAD":
            current_method = "GET"
            kwargs.pop("data", None)
            kwargs.pop("json", None)
            kwargs.pop("files", None)
        current_url = next_url
    raise RuntimeError("unreachable")
