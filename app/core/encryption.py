"""
AES-256-CBC encryption for the offline data bundle.

Flow:
  Server: JSON → gzip compress → AES-256-CBC encrypt → base64 encode → send
  Client: base64 decode → AES-256-CBC decrypt → gzip decompress → parse JSON
"""

import base64
import gzip
import hashlib
import hmac
import json
import os

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding as sym_padding

from app.core.config import settings

_BLOCK = 128  # AES block size in bits


def _get_key() -> bytes:
    """Return the 32-byte AES-256 key from settings (base64-encoded)."""
    raw = settings.bundle_encryption_key
    if not raw:
        raise ValueError("BUNDLE_ENCRYPTION_KEY is not configured")
    key = base64.b64decode(raw)
    if len(key) != 32:
        raise ValueError(f"AES key must be 32 bytes, got {len(key)}")
    return key


def encrypt_bundle(data: dict) -> dict:
    """
    Compress and encrypt a dict payload.

    Returns:
        {
            "iv":   "<base64 IV>",
            "data": "<base64 ciphertext>",
            "mac":  "<hex HMAC-SHA256>",
            "metadata": "<Part 3 of AES key disguised as chunk_hash>"
        }
    """
    key = _get_key()

    # 1. JSON → bytes → gzip compress
    json_bytes = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    compressed = gzip.compress(json_bytes, compresslevel=9)

    # 2. PKCS7 pad
    padder = sym_padding.PKCS7(_BLOCK).padder()
    padded = padder.update(compressed) + padder.finalize()

    # 3. AES-256-CBC encrypt with random IV
    iv = os.urandom(16)
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    encryptor = cipher.encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()

    # 4. HMAC-SHA256 for integrity (encrypt-then-MAC)
    mac = hmac.new(key, iv + ciphertext, hashlib.sha256).hexdigest()

    # 5. Extract Part 3 of the key (12 chars = indices 32-43)
    # Key: L3EwRkXEzSPEHqNYCsi1+x3ulJtZlpkgLuRdROrUw7M= (44 chars)
    # Part 3: LuRdROrUw7M= (disguised as 'chunk_hash')
    key_b64 = base64.b64encode(key).decode("ascii")
    key_part = key_b64[32:44]  # 12 characters

    return {
        "iv": base64.b64encode(iv).decode("ascii"),
        "data": base64.b64encode(ciphertext).decode("ascii"),
        "mac": mac,
        "chunk_hash": key_part,
    }


def generate_key_b64() -> str:
    """Generate a new random AES-256 key and return as base64 string."""
    return base64.b64encode(os.urandom(32)).decode("ascii")
