import json
import getpass
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.backends import default_backend
from cryptography.exceptions import InvalidTag

BACKEND = default_backend()
SALT_SIZE = 16
NONCE_SIZE = 12
ITERATIONS = 100_000
KEY_SIZE = 32

def derive_key(password: bytes, salt: bytes) -> bytes:
    # Derive a secure encryption key from a password and salt.
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=KEY_SIZE,
        salt=salt,
        iterations=ITERATIONS,
        backend=BACKEND
    )
    return kdf.derive(password)

def load_credentials(filepath: str, master_password: str) -> dict:
    # Loads and decrypts credentials from the specified file
    # Promppts for the master password to decrypt the data
    try:
        with open(filepath, 'rb') as f:
            # Read the salt, nonce, and ciphertext from the file
            salt = f.read(SALT_SIZE)
            nonce = f.read(NONCE)
            encrypted_data = f.read()
        # Derive the key from the provided password and stored salt
        key = derive_key(master_password.encode('utf-8'), salt)
        # Decrypt
        aesgcm = AESGCM(key)
        decrypted_bytes = aesgcm.decrypt(nonce, encrypted_data, None)
        # Convert from JSON bytes back to a Python Dictionary
        credentials = json.loads(decrypted_bytes.decode('utf-8'))
        return credentials
    except FileNotFoundError:
        print(f"Error: Credentials file not found at '{CREDENTIALS_FILE}'")
        raise
    except InvalidTag:
        # This exception is raised if the key is wrong or if the ciphertext has been tampered with
        print("Error: Invalid master password or corrupted credentials file.")
        raise
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
        raise
