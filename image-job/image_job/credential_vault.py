"""AES-GCM protection for persisted upstream Authorization credentials."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from typing import Any


_KEY_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_AAD_VERSION = b"lumen-image-job:authorization:v1"
_NONCE_BYTES = 12


class CredentialVaultError(RuntimeError):
    """Raised when credential encryption or decryption cannot be completed."""


class CredentialVaultConfigError(CredentialVaultError):
    """Raised when the active credential key is not safely configured."""


@dataclass(frozen=True)
class EncryptedCredential:
    ciphertext: bytes
    nonce: bytes
    key_id: str


def validate_key_id(key_id: str) -> str:
    normalized = (key_id or "").strip()
    if _KEY_ID_RE.fullmatch(normalized) is None:
        raise CredentialVaultConfigError(
            "IMAGE_JOB_CREDENTIAL_ACTIVE_KEY_ID must be a non-empty "
            "ASCII key id up to 64 characters"
        )
    return normalized


def validate_master_secret(master_secret: str) -> bytes:
    value = master_secret or ""
    encoded = value.encode("utf-8")
    if len(encoded) < 32:
        raise CredentialVaultConfigError(
            "IMAGE_JOB_CREDENTIAL_MASTER_SECRET must contain at least 32 bytes"
        )
    if any(char.isspace() for char in value):
        raise CredentialVaultConfigError(
            "IMAGE_JOB_CREDENTIAL_MASTER_SECRET must not contain whitespace"
        )
    return encoded


def _aesgcm(key: bytes) -> Any:
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ModuleNotFoundError as exc:
        raise CredentialVaultConfigError(
            "cryptography package is required for image-job credential encryption"
        ) from exc
    return AESGCM(key)


def _aad_field(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return len(encoded).to_bytes(4, "big") + encoded


def credential_aad(job_id: str, owner_hash: str) -> bytes:
    if not job_id or not owner_hash:
        raise CredentialVaultError(
            "job_id and owner_hash are required for credential encryption"
        )
    return b"".join(
        (
            _AAD_VERSION,
            _aad_field(job_id),
            _aad_field(owner_hash),
        )
    )


class CredentialVault:
    """Derive versioned AES-256-GCM keys from one protected master secret."""

    def __init__(self, *, active_key_id: str, master_secret: str) -> None:
        self._active_key_id = validate_key_id(active_key_id)
        self._master_secret = validate_master_secret(master_secret)

    @property
    def active_key_id(self) -> str:
        return self._active_key_id

    def validate_runtime(self) -> None:
        _aesgcm(self._derive_key(self._active_key_id))

    def _derive_key(self, key_id: str) -> bytes:
        normalized = validate_key_id(key_id)
        return hmac.new(
            self._master_secret,
            (
                "lumen-image-job:credential-vault:"
                f"{normalized}:aes-gcm:v1"
            ).encode("ascii"),
            hashlib.sha256,
        ).digest()

    def encrypt(
        self,
        authorization: str,
        *,
        job_id: str,
        owner_hash: str,
    ) -> EncryptedCredential:
        if not authorization:
            raise CredentialVaultError("Authorization credential is empty")
        nonce = secrets.token_bytes(_NONCE_BYTES)
        key_id = self._active_key_id
        ciphertext = _aesgcm(self._derive_key(key_id)).encrypt(
            nonce,
            authorization.encode("utf-8"),
            credential_aad(job_id, owner_hash),
        )
        return EncryptedCredential(
            ciphertext=ciphertext,
            nonce=nonce,
            key_id=key_id,
        )

    def decrypt(
        self,
        *,
        ciphertext: bytes | bytearray | memoryview,
        nonce: bytes | bytearray | memoryview,
        key_id: str,
        job_id: str,
        owner_hash: str,
    ) -> str:
        raw_ciphertext = bytes(ciphertext)
        raw_nonce = bytes(nonce)
        if not raw_ciphertext or len(raw_nonce) != _NONCE_BYTES:
            raise CredentialVaultError("stored credential envelope is incomplete")
        try:
            plaintext = _aesgcm(self._derive_key(key_id)).decrypt(
                raw_nonce,
                raw_ciphertext,
                credential_aad(job_id, owner_hash),
            )
            authorization = plaintext.decode("utf-8")
        except CredentialVaultError:
            raise
        except Exception as exc:
            raise CredentialVaultError(
                "stored Authorization credential failed authentication"
            ) from exc
        if not authorization:
            raise CredentialVaultError("stored Authorization credential is empty")
        return authorization

    def decrypt_job_row(self, row: Any) -> str:
        try:
            return self.decrypt(
                ciphertext=row["auth_ciphertext"],
                nonce=row["auth_nonce"],
                key_id=str(row["auth_key_id"] or ""),
                job_id=str(row["job_id"] or ""),
                owner_hash=str(row["auth_hash"] or ""),
            )
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            raise CredentialVaultError(
                "stored credential envelope is incomplete"
            ) from exc
