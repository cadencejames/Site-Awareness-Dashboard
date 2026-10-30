"""
credential_store.py - Shared cryptographic core for SAD's credential store.

This is the ONLY place key derivation and encrypt/decrypt logic should
live. credential_manager.py (the interactive CLI for creating/editing
the store), credential_loader.py (the runtime reader orchestrator.py
and the other CLI tools use), and gui.py's CredentialsTab all import
from here rather than keeping their own copies - a single shared
implementation means there's nothing to drift out of sync between the
places that write the store and the places that read it.

Store files live in the "credentials" folder (CREDENTIALS_DIR) under the
project root, one per OS user (credentials_<username>.enc). Files left in
the project root by earlier versions are still found and used in place.

File format written to disk (e.g. credentials.enc):
    [ SALT_SIZE bytes salt ][ NONCE_SIZE bytes nonce ][ AES-GCM ciphertext ]
"""

import os
import getpass
import json
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.backends import default_backend
from cryptography.exceptions import InvalidTag  # re-exported for callers

BACKEND = default_backend()

DEFAULT_CREDENTIALS_FILE = "credentials.enc"
CREDENTIALS_DIR = "credentials"


def personal_credentials_path(base_dir: str = ".") -> str:
    """Where a NEW store for the current OS user goes: inside
    CREDENTIALS_DIR, as credentials_<username>.enc (or the original fixed
    name if the username can't be determined). Doesn't check whether the
    file exists."""
    folder = os.path.join(base_dir, CREDENTIALS_DIR)
    try:
        return os.path.join(folder, f"credentials_{getpass.getuser()}.enc")
    except Exception:
        return os.path.join(folder, DEFAULT_CREDENTIALS_FILE)


def resolve_credentials_path(base_dir: str = ".") -> str:
    """Picks which credentials file to use for the current OS user, so
    several people sharing one machine or one network drive each get
    their own separate, independently-password-protected file rather
    than one shared file (and one shared master password) for everyone.

    Resolution order (the first file that exists wins):
      1. credentials/credentials_<username>.enc - this person's store.
      2. credentials_<username>.enc in base_dir itself - a store left
         in the project root by an earlier version, used in place.
      3. credentials/credentials.enc, then credentials.enc in base_dir -
         the original shared file, for whoever was using this tool
         before per-user files existed. A per-user file always beats it.
      4. Otherwise credentials/credentials_<username>.enc (even though
         it doesn't exist yet) - the natural place for a brand new
         store; credential_manager.py and the GUI offer to create it.

    If the OS username can't be determined for any reason, only the
    original fixed filename is considered (in credentials/ first).
    """
    folder = os.path.join(base_dir, CREDENTIALS_DIR)
    legacy_candidates = [os.path.join(folder, DEFAULT_CREDENTIALS_FILE),
                         os.path.join(base_dir, DEFAULT_CREDENTIALS_FILE)]
    try:
        username = getpass.getuser()
    except Exception:
        username = None

    candidates = []
    if username:
        candidates += [os.path.join(folder, f"credentials_{username}.enc"),
                       os.path.join(base_dir, f"credentials_{username}.enc")]
    candidates += legacy_candidates

    for path in candidates:
        if os.path.exists(path):
            return path
    return personal_credentials_path(base_dir)

SALT_SIZE = 16
NONCE_SIZE = 12
ITERATIONS = 100_000  # PBKDF2 rounds - raise over time as hardware improves
KEY_SIZE = 32  # AES-256


def derive_key(password: bytes, salt: bytes) -> bytes:
    """Derive a 32-byte AES key from a master password and salt."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=KEY_SIZE,
        salt=salt,
        iterations=ITERATIONS,
        backend=BACKEND,
    )
    return kdf.derive(password)


def load_and_decrypt(filepath: str, master_password: str) -> dict:
    """Read salt/nonce/ciphertext from filepath and return the decrypted dict.

    Raises FileNotFoundError if filepath doesn't exist, and
    cryptography.exceptions.InvalidTag if the password is wrong or the
    file has been tampered with/corrupted.
    """
    with open(filepath, "rb") as f:
        salt = f.read(SALT_SIZE)
        nonce = f.read(NONCE_SIZE)
        encrypted_data = f.read()

    key = derive_key(master_password.encode("utf-8"), salt)
    aesgcm = AESGCM(key)
    decrypted_bytes = aesgcm.decrypt(nonce, encrypted_data, None)
    return json.loads(decrypted_bytes.decode("utf-8"))


def save_and_encrypt(filepath: str, credentials: dict, master_password: str) -> None:
    """Encrypt credentials and overwrite filepath. Generates a fresh salt/nonce."""
    salt = os.urandom(SALT_SIZE)
    key = derive_key(master_password.encode("utf-8"), salt)
    aesgcm = AESGCM(key)
    nonce = os.urandom(NONCE_SIZE)

    folder = os.path.dirname(filepath)
    if folder:
        os.makedirs(folder, exist_ok=True)

    credentials_bytes = json.dumps(credentials).encode("utf-8")
    encrypted_data = aesgcm.encrypt(nonce, credentials_bytes, None)

    with open(filepath, "wb") as f:
        f.write(salt)
        f.write(nonce)
        f.write(encrypted_data)