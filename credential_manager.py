import os
import json
import getpass
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.backends import default_backend
from cryptography.exceptions import InvalidTag

# --- Configuration ---
BACKEND = default_backend()
CREDENTIALS_FILE = "credentials.enc"
SALT_SIZE = 16
NONCE_SIZE = 12
ITERATIONS = 100_000 # Increase for more security
KEY_SIZE = 32 # AES-256

# --- Core Cryptographic Functions ---
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

def load_and_decrypt(filepath: str, master_password: str) -> dict:
    # Loads and decrypts the credential store. Handles errors.
    with open(filepath, "rb") as f:
        salt = f.read(SALT_SIZE)
        nonce = f.read(NONCE_SIZE)
        encrypted_data = f.read()
    
    key = derive_key(master_password.encode('utf-8'), salt)
    aesgcm = AESGCM(key)
    
    decrypted_bytes = aesgcm.decrypt(nonce, encrypted_data, None)
    return json.loads(decrypted_bytes.decode('utf-8'))

def save_and_encrypt(filepath: str, credentials: dict, master_password: str):
    # Encrypts and saves the credential store. Overwrites the existing file.
    # A new salt is generated for every save for maximum security.
    salt = os.urandom(SALT_SIZE)
    key = derive_key(master_password.encode('utf-8'), salt)
    
    aesgcm = AESGCM(key)
    nonce = os.urandom(NONCE_SIZE)
    
    credentials_bytes = json.dumps(credentials).encode('utf-8')
    encrypted_data = aesgcm.encrypt(nonce, credentials_bytes, None)
    
    with open(filepath, "wb") as f:
        f.write(salt)
        f.write(nonce)
        f.write(encrypted_data)

# --- User Interface Functions ---
def initialize_store():
    # Creates a new, empty credential store.
    print("No credential store found. Let's create one.")
    while True:
        mp = getpass.getpass("Enter a new master password: ")
        mp_verify = getpass.getpass("Verify master password: ")
        if mp == mp_verify:
            break
        else:
            print("Passwords do not match. Please try again.")
    
    save_and_encrypt(CREDENTIALS_FILE, {}, mp)
    print(f"Successfully created empty credential store: '{CREDENTIALS_FILE}'")
    return mp

def main_menu(credentials: dict, master_password: str):
    # The main interactive loop for managing credentials.
    while True:
        print("\n--- Credential Manager ---")
        print("(L)ist credentials")
        print("(A)dd or update a credential")
        print("(D)elete a credential")
        print("(Q)uit and save")
        
        choice = input("Enter your choice: ").lower()

        if choice == 'l':
            if not credentials:
                print("Store is empty.")
            else:
                print("\n--- Stored Credential Keys ---")
                for key in sorted(credentials.keys()):
                    print(f"  - {key}")
                print("------------------------------")
        
        elif choice == 'a':
            key = input("Enter credential key to add/update: ")
            if not key:
                print("Key cannot be empty.")
                continue
            value = getpass.getpass(f"Enter value for '{key}': ")
            credentials[key] = value
            print(f"Credential '{key}' has been set.")
            # Auto-save after every modification for safety
            save_and_encrypt(CREDENTIALS_FILE, credentials, master_password)
            print("Store saved successfully.")

        elif choice == 'd':
            key_to_delete = input("Enter credential key to delete: ")
            if key_to_delete in credentials:
                confirm = input(f"Are you sure you want to delete '{key_to_delete}'? (y/n): ").lower()
                if confirm == 'y':
                    del credentials[key_to_delete]
                    print(f"Credential '{key_to_delete}' deleted.")
                    # Auto-save after every modification
                    save_and_encrypt(CREDENTIALS_FILE, credentials, master_password)
                    print("Store saved successfully.")
                else:
                    print("Deletion cancelled.")
            else:
                print(f"Error: Key '{key_to_delete}' not found in store.")

        elif choice == 'q':
            print("Exiting.")
            break
        
        else:
            print("Invalid choice, please try again.")

# --- Main Execution ---
if __name__ == "__main__":
    master_password = None
    credentials = {}
    try:
        if not os.path.exists(CREDENTIALS_FILE):
            master_password = initialize_store()
        else:
            # Unlock the existing store
            while True:
                master_password = getpass.getpass("Enter master password to unlock store: ")
                try:
                    credentials = load_and_decrypt(CREDENTIALS_FILE, master_password)
                    print("Credential store unlocked successfully.")
                    break
                except InvalidTag:
                    print("Invalid password or corrupted file. Please try again.")
                except Exception as e:
                    print(f"An unexpected error occurred: {e}")
                    exit(1)
        
        # If we have a password and loaded credentials, show the menu
        if master_password is not None:
            main_menu(credentials, master_password)

    except KeyboardInterrupt:
        print("\nOperation cancelled by user. Exiting.")
    except Exception as e:
        print(f"\nA fatal error occurred: {e}")
