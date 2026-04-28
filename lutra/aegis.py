"""Auto-refresh _aegis_cas JWT from Chrome browser cookies.

Chrome stores cookies in an encrypted SQLite database.
On Linux, the encryption key lives in GNOME Keyring (SecretService D-Bus API).

Flow:
    1. Read encrypted cookie from Chrome Cookies DB
    2. Fetch decryption key from GNOME Keyring via secretstorage
    3. Decrypt with AES-128-GCM (v11 prefix) or AES-128-CBC (v10 prefix)
    4. Validate JWT expiry

Usage:
    token = get_aegis_cas("jira-phone.mioffice.cn")
    if token:
        headers["Cookie"] = f"_aegis_cas={token}"
"""

import base64
import json
import logging
import re
import sqlite3
import shutil
import tempfile
import time
from pathlib import Path

log = logging.getLogger("lutra.aegis")

# Cache to avoid hitting the DB on every request
_cache: dict[str, tuple[str, float]] = {}  # domain → (token, expiry_ts)


def get_aegis_cas(domain: str = "jira-phone.mioffice.cn") -> str:
    """Get a valid _aegis_cas token for the given domain.

    Returns the token string, or "" if unavailable/expired.
    Caches the result until 5 minutes before JWT expiry.
    """
    now = time.time()

    # Check cache first
    cached = _cache.get(domain)
    if cached:
        token, expiry = cached
        if now < expiry - 300:  # 5 min buffer
            return token

    # Try to read from Chrome
    try:
        token = _read_from_chrome(domain)
    except Exception as e:
        log.warning("Failed to read aegis_cas from Chrome: %s", e)
        return ""

    if not token:
        return ""

    # Parse JWT expiry and cache
    expiry = _jwt_expiry(token)
    if expiry and now >= expiry:
        log.warning("aegis_cas from Chrome is already expired")
        return ""

    _cache[domain] = (token, expiry or now + 3600)
    log.info("Refreshed aegis_cas from Chrome (expires in %.1f hours)",
             (expiry - now) / 3600 if expiry else 0)
    return token


def _read_from_chrome(domain: str) -> str:
    """Read and decrypt _aegis_cas cookie from Chrome's cookie database."""
    db_path = Path.home() / ".config/google-chrome/Default/Cookies"
    if not db_path.exists():
        log.debug("Chrome cookie DB not found: %s", db_path)
        return ""

    # Chrome locks the DB, so copy it to a temp file
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite")
    try:
        shutil.copy2(str(db_path), tmp.name)
        conn = sqlite3.connect(tmp.name)
        c = conn.cursor()
        c.execute(
            "SELECT encrypted_value FROM cookies "
            "WHERE host_key = ? AND name = '_aegis_cas'",
            (domain,),
        )
        row = c.fetchone()
        conn.close()
    finally:
        Path(tmp.name).unlink(missing_ok=True)

    if not row:
        log.debug("No _aegis_cas cookie found for %s", domain)
        return ""

    encrypted = row[0]
    if not encrypted:
        return ""

    return _decrypt_chrome_cookie(encrypted)


def _decrypt_chrome_cookie(encrypted: bytes) -> str:
    """Decrypt a Chrome cookie value (v10 or v11 encryption)."""
    prefix = encrypted[:3]

    if prefix == b"v11":
        return _decrypt_v11(encrypted)
    elif prefix == b"v10":
        return _decrypt_v10(encrypted)
    else:
        # Might be unencrypted
        try:
            return encrypted.decode("utf-8")
        except UnicodeDecodeError:
            log.warning("Unknown Chrome cookie encryption prefix: %s", prefix)
            return ""


def _decrypt_v11(encrypted: bytes) -> str:
    """v11: AES-128-CBC with key from GNOME Keyring (same cipher as v10).

    Chrome prepends a 32-byte HMAC for domain integrity check.
    """
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes

    key_password = _get_chrome_keyring_password()
    if not key_password:
        return ""

    # Derive AES-128 key from keyring password
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA1(),
        length=16,
        salt=b"saltysalt",
        iterations=1,
    )
    aes_key = kdf.derive(key_password)

    # v11 layout: "v11" (3 bytes) + ciphertext (AES-128-CBC, IV = 16 spaces)
    ciphertext = encrypted[3:]
    iv = b" " * 16

    try:
        cipher = Cipher(algorithms.AES128(aes_key), modes.CBC(iv))
        decryptor = cipher.decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()

        # PKCS7 unpadding
        pad_len = padded[-1]
        if isinstance(pad_len, int) and 0 < pad_len <= 16:
            padded = padded[:-pad_len]

        # Chrome prepends a 32-byte HMAC for domain integrity — skip it
        try:
            return padded.decode("utf-8")
        except UnicodeDecodeError:
            return padded[32:].decode("utf-8")
    except Exception as e:
        log.warning("v11 decryption failed: %s", e)
        return ""


def _decrypt_v10(encrypted: bytes) -> str:
    """v10: AES-128-CBC with key derived from 'peanuts'."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes

    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA1(),
        length=16,
        salt=b"saltysalt",
        iterations=1,
    )
    aes_key = kdf.derive(b"peanuts")

    # v10 layout: "v10" (3 bytes) + ciphertext (AES-128-CBC, IV = 16 spaces)
    ciphertext = encrypted[3:]
    iv = b" " * 16

    try:
        cipher = Cipher(algorithms.AES128(aes_key), modes.CBC(iv))
        decryptor = cipher.decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()

        # PKCS7 unpadding
        pad_len = padded[-1]
        if isinstance(pad_len, int) and 0 < pad_len <= 16:
            padded = padded[:-pad_len]
        return padded.decode("utf-8")
    except Exception as e:
        log.warning("v10 decryption failed: %s", e)
        return ""


def _get_chrome_keyring_password() -> bytes | None:
    """Get Chrome Safe Storage password from GNOME Keyring."""
    try:
        import secretstorage

        bus = secretstorage.dbus_init()
        collection = secretstorage.get_default_collection(bus)

        if collection.is_locked():
            collection.unlock()

        for item in collection.get_all_items():
            attrs = item.get_attributes()
            if (
                attrs.get("application") == "chrome"
                and "chrome_libsecret" in attrs.get("xdg:schema", "")
            ):
                return item.get_secret()

        log.warning("Chrome Safe Storage key not found in keyring")
        return None
    except ImportError:
        log.warning("secretstorage not installed — cannot decrypt Chrome cookies")
        return None
    except Exception as e:
        log.warning("Failed to access GNOME Keyring: %s", e)
        return None


def _jwt_expiry(token: str) -> float | None:
    """Extract expiry timestamp from a JWT without verifying signature."""
    parts = token.split(".")
    if len(parts) < 2:
        return None

    try:
        payload_b64 = parts[1] + "=" * (4 - len(parts[1]) % 4)
        raw = base64.urlsafe_b64decode(payload_b64)
        # Payload may contain binary data, try lossy decode
        text = raw.decode("utf-8", errors="replace")
        m = re.search(r'"exp"\s*:\s*(\d+)', text)
        if m:
            return float(m.group(1))
    except Exception:
        pass
    return None
