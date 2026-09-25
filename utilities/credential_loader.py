"""
credential_loader.py - Runtime credential access for SAD.

Called once at the start of a conductor run. Prompts for the master
password, decrypts the current user's credentials file via
credential_store, and returns the plain dict for the rest of the run
to use in memory.

Since V1 runs orchestrator as a direct function call in the same
process as conductor (not a separate subprocess), there's no need to
re-distribute credentials via a temp file cache the way the previous
version did - the decrypted dict just stays in memory and gets passed
directly to whatever needs it (tools/, etc.).

Which file gets used is resolved per-OS-user (see
credential_store.resolve_credentials_path()) - several people sharing
one machine or network drive each get their own separate,
independently-password-protected store rather than one shared file.
"""

import getpass
from cryptography.exceptions import InvalidTag
import credential_store

DEFAULT_CREDENTIALS_FILE = credential_store.DEFAULT_CREDENTIALS_FILE  # kept for anything importing this name directly


def load_credentials(filepath: str = None, master_password: str = None) -> dict:
    """Decrypt and return the credentials dict.

    If filepath is not supplied, it's resolved per-OS-user (see
    credential_store.resolve_credentials_path()) rather than always
    using one fixed name.

    If master_password is not supplied, prompts for it interactively
    (getpass, so it isn't echoed to the terminal).

    Raises FileNotFoundError if the store doesn't exist yet, and
    cryptography.exceptions.InvalidTag if the password is wrong.
    """
    if filepath is None:
        filepath = credential_store.resolve_credentials_path()

    if master_password is None:
        master_password = getpass.getpass("Enter master password to unlock credential store: ")

    try:
        return credential_store.load_and_decrypt(filepath, master_password)
    except FileNotFoundError:
        print(f"Error: credential store not found at '{filepath}'. "
              f"Run credential_manager.py first to create one.")
        raise
    except InvalidTag:
        print("Error: invalid master password or corrupted credentials file.")
        raise


def prompt_and_load(filepath: str = None, max_attempts: int = 3) -> dict:
    """Interactive helper: prompt for the password, retrying on a wrong
    entry. This is what orchestrator.py calls at the top of a CLI run
    (via _load_creds_or_exit()).
    """
    if filepath is None:
        filepath = credential_store.resolve_credentials_path()
        print(f"Using credential store: {filepath}")

    for attempt in range(1, max_attempts + 1):
        master_password = getpass.getpass("Enter master password to unlock credential store: ")
        try:
            return credential_store.load_and_decrypt(filepath, master_password)
        except FileNotFoundError:
            print(f"Error: credential store not found at '{filepath}'. "
                  f"Run credential_manager.py first to create one.")
            raise
        except InvalidTag:
            remaining = max_attempts - attempt
            if remaining > 0:
                print(f"Invalid password. {remaining} attempt(s) remaining.")
            else:
                print("Invalid password. No attempts remaining.")
    raise InvalidTag("Master password rejected after maximum attempts.")
