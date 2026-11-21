"""The vault drop target a capture carries, and the one call this service makes to a vault.

The worker seals the result to the phone's public key and deposits it with the write-only drop token;
this service never deposits, never reads, and never sees a result. It only checks the target's shape
(https, public address, usable key) and asks the vault whether the drop token is alive.
"""

import asyncio
import base64
import binascii
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
from nacl.exceptions import CryptoError
from nacl.public import PublicKey, SealedBox

from app.config import Settings


class InvalidDropTarget(ValueError):
    pass


class VaultUnreachable(Exception):
    """The vault could not say whether a token is valid: the caller should retry, not refuse."""


@dataclass(frozen=True)
class DropTarget:
    url: str
    token: str
    inbox_public_key: bytes
    record_id: str


def parse_public_key(value: str) -> bytes:
    try:
        raw = base64.b64decode(value, validate=True)
        # A key of the right length can still be unusable (all zeros, low-order points): sealing fails
        # only later, in the background. Try it now so the request is refused instead.
        SealedBox(PublicKey(raw)).encrypt(b"x")
    except (binascii.Error, ValueError, TypeError, CryptoError) as exc:
        raise InvalidDropTarget("inbox_public_key must be a base64 X25519 public key") from exc
    return raw


async def check_vault_url(url: str, settings: Settings) -> str:
    """Reject URLs that would make this service call into private networks (SSRF)."""
    parts = urlsplit(url)
    if parts.scheme != "https" and not settings.vault_allow_insecure:
        raise InvalidDropTarget("vault_url must be https")
    if not parts.hostname or parts.username or parts.password:
        raise InvalidDropTarget("vault_url must be a plain host URL")
    if not settings.vault_allow_insecure:
        try:
            infos = await asyncio.to_thread(socket.getaddrinfo, parts.hostname, parts.port or 443)
        except socket.gaierror as exc:
            raise InvalidDropTarget("vault_url host does not resolve") from exc
        if not all(ipaddress.ip_address(info[4][0]).is_global for info in infos):
            raise InvalidDropTarget("vault_url must point to a public address")
    return f"{parts.scheme}://{parts.netloc}"


class VaultVerifier:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self._settings = settings
        self._transport = transport

    async def verify_drop_token(self, target: DropTarget) -> bool:
        """Whether the vault knows this token as a live drop token (`GET /v1/me` answers `role: dropper`).

        That call returns the account id and role, never data, and works for a write-only token, so the
        token's power does not change. Any other role is refused: only a token that cannot read may be
        used to authenticate here. Raises `VaultUnreachable` when the vault cannot answer.
        """
        headers = {"Authorization": f"Bearer {target.token}"}
        async with httpx.AsyncClient(
            transport=self._transport, timeout=self._settings.vault_timeout_s, follow_redirects=False
        ) as client:
            try:
                resp = await client.get(f"{target.url}/v1/me", headers=headers)
            except httpx.HTTPError as exc:
                raise VaultUnreachable(type(exc).__name__) from exc
        if resp.status_code >= 500:
            raise VaultUnreachable(str(resp.status_code))
        if resp.status_code != 200:
            return False
        try:
            return resp.json().get("role") == "dropper"
        except (ValueError, AttributeError):
            return False
