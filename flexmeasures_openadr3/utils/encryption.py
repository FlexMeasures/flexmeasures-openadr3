from flask import current_app

import base64
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


class SecretsEncryptionError(Exception):
    """Raised when secrets encryption/decryption fails."""


class InvalidSecretsEncryptionKey(SecretsEncryptionError):
    """Raised when a usable master key cannot be loaded."""


class SecretsDecryptionError(SecretsEncryptionError):
    """Raised when a value cannot be decrypted (wrong key or corrupted token)."""


_KDF_OUTPUT_BYTES = 32
_KDF_INFO = b"flexmeasures-openadr3:secrets"


@dataclass(frozen=True, slots=True)
class SecretsEncryptor:
    """Encrypt/decrypt secrets using a master key plus per-value secret key."""

    _encryption_key: str

    @staticmethod
    def _get_secrets_encryption_key() -> str:
        """Retrieve the master key used to protect OpenADR secrets."""
        key = current_app.config.get("OPENADR_SECRETS_ENCRYPTION_KEY")
        if isinstance(key, str) and key.strip():
            return key
        return current_app.config["SECRET_KEY"] or ""

    @classmethod
    def from_current_app(cls) -> "SecretsEncryptor":
        encryption_key = cls._get_secrets_encryption_key()
        if not isinstance(encryption_key, str) or not encryption_key.strip():
            raise InvalidSecretsEncryptionKey(
                "Missing OPENADR_SECRETS_ENCRYPTION_KEY or SECRET_KEY."
            )
        return cls(_encryption_key=encryption_key)

    def _fernet_for(self, secret_key: str) -> Fernet:
        if not isinstance(secret_key, str) or not secret_key:
            raise InvalidSecretsEncryptionKey("secret_key must be a non-empty string.")

        hkdf = HKDF(
            algorithm=hashes.SHA256(),
            length=_KDF_OUTPUT_BYTES,
            salt=secret_key.encode("utf-8"),
            info=_KDF_INFO,
        )
        derived = hkdf.derive(self._encryption_key.encode("utf-8"))
        return Fernet(base64.urlsafe_b64encode(derived))

    def encrypt(self, value: str) -> str:
        """Encrypt a string and return a URL-safe token."""
        if not isinstance(value, str):
            raise SecretsEncryptionError("value must be a string.")

        token = self._fernet_for(self._encryption_key).encrypt(value.encode("utf-8"))
        return token.decode("utf-8")

    def decrypt(self, token: str) -> str:
        """Decrypt a token and return the original string."""
        if not isinstance(token, str):
            raise SecretsDecryptionError("token must be a string.")

        try:
            raw = self._fernet_for(self._encryption_key).decrypt(token.encode("utf-8"))
        except InvalidToken as exc:
            raise SecretsDecryptionError(
                "Invalid token for the given encryption_key."
            ) from exc
        return raw.decode("utf-8")
