"""
credential_manager.py - Interactive CLI for creating/managing SAD's
encrypted credential store.

Run this directly (from the project root) whenever you need to add,
update, view, reveal, or delete a stored credential:

    python3 utilities/credential_manager.py

Credentials are organized by TYPE, not as flat individual keys. Each
type is a named bag of fields (e.g. "tacacs" has a username field and
a password field; an API-key type might have just one field). This
matches how people actually think about a credential ("my TACACS
login") instead of two unrelated-looking entries that just happen to
share a naming convention.

"tacacs" is the one reserved type name - orchestrator.py always reads
tacacs.username/tacacs.password for its own device connections, since
type names are otherwise completely freeform and the code needs one
fixed, known place to look. Anything else you create (cucm, vtc, an
API key, whatever) is yours to organize however makes sense - SAD's
own code never reads those automatically.

Which file this manages is resolved per-OS-user (see
credential_store.resolve_credentials_path()) - several people sharing
one machine or network drive each get their own separate,
independently-password-protected store. The very first time someone
new runs this tool, it creates their own file automatically - nothing
to configure.

This is a standalone maintenance tool - it is not imported by
orchestrator.py or gui.py. Those use credential_loader.py instead.
"""

import os
import getpass
from cryptography.exceptions import InvalidTag
import credential_store

CREDENTIALS_FILE = credential_store.resolve_credentials_path()

RESERVED_TYPE = "tacacs"  # every SAD user needs one of these - see module docstring


def _prompt_field_value(field_name: str, sensitive: bool) -> str:
    """Sensitive fields are masked while typing (getpass) so they're
    never echoed to the screen or left in terminal scrollback; non-
    sensitive fields are typed visibly so you can see you got them
    right (a username has no real secrecy need, and being able to see
    it while typing catches typos immediately instead of leaving them
    to eventually surface as a failed device login).
    """
    if sensitive:
        return getpass.getpass(f"  Value for '{field_name}' (hidden): ")
    return input(f"  Value for '{field_name}': ").strip()


def _prompt_username_password_fields() -> dict:
    """The quick path for the extremely common case of a plain
    username+password credential - used for RESERVED_TYPE and offered
    as a shortcut for any other type too.
    """
    username = input("  Username: ").strip()
    password = getpass.getpass("  Password (hidden): ")
    return {
        "username": {"value": username, "sensitive": False},
        "password": {"value": password, "sensitive": True},
    }


def _prompt_custom_fields(type_name: str) -> dict:
    """The general path for a credential type that isn't a plain
    username+password - any number of arbitrarily-named fields, each
    independently marked sensitive or not (e.g. a single API key field,
    or something with more than two parts).
    """
    fields = {}
    print(f"Adding fields for '{type_name}'. Leave the field name blank when you're done.")
    while True:
        field_name = input("  Field name: ").strip()
        if not field_name:
            break
        if field_name in fields:
            print(f"  '{field_name}' already entered this round - use (U)pdate later to change it.")
            continue
        sensitive = input(f"  Is '{field_name}' sensitive (masked when typed/viewed)? (Y/n): ").strip().lower() != "n"
        value = _prompt_field_value(field_name, sensitive)
        fields[field_name] = {"value": value, "sensitive": sensitive}
    return fields


def initialize_store() -> tuple[str, dict]:
    """Create a new, empty credential store, then offer to set up the
    one credential everyone needs (RESERVED_TYPE) right away instead
    of making a first-time user hunt for it in the menu themselves.
    Returns (master_password, credentials).
    """
    print("No credential store found. Let's create one.")
    while True:
        mp = getpass.getpass("Enter a new master password: ")
        mp_verify = getpass.getpass("Verify master password: ")
        if mp == mp_verify:
            break
        print("Passwords do not match. Please try again.")

    credentials = {}
    credential_store.save_and_encrypt(CREDENTIALS_FILE, credentials, mp)
    print(f"Successfully created empty credential store: '{CREDENTIALS_FILE}'")

    setup_now = input(
        f"\nSet up your '{RESERVED_TYPE}' credential now? This is what SAD uses to log into "
        f"network devices. (Y/n): "
    ).strip().lower()
    if setup_now != "n":
        credentials[RESERVED_TYPE] = _prompt_username_password_fields()
        credential_store.save_and_encrypt(CREDENTIALS_FILE, credentials, mp)
        print(f"Credential '{RESERVED_TYPE}' saved.")

    return mp, credentials


def unlock_store() -> tuple[str, dict]:
    """Prompt for the master password until the existing store decrypts.
    Returns (password, credentials).

    After a couple of failed attempts, offers an escape hatch to create
    a brand new personal store instead. This matters specifically
    because CREDENTIALS_FILE might be a shared legacy file that
    actually belongs to someone else entirely on a shared drive (see
    credential_store.resolve_credentials_path()'s docstring - there's
    no way to tell "this is the original single user" apart from "this
    is someone else's very first run" purely from file existence), not
    necessarily a genuinely wrong password for your own store.
    """
    global CREDENTIALS_FILE
    attempts = 0
    while True:
        mp = getpass.getpass(f"Enter master password to unlock '{CREDENTIALS_FILE}': ")
        try:
            creds = credential_store.load_and_decrypt(CREDENTIALS_FILE, mp)
            print("Credential store unlocked successfully.")
            return mp, creds
        except InvalidTag:
            attempts += 1
            print("Invalid password or corrupted file.")
            if attempts >= 2:
                choice = input(
                    f"\nIf '{CREDENTIALS_FILE}' isn't actually your store (e.g. it belongs to "
                    f"someone else on a shared drive), you can create your own instead.\n"
                    f"(R)etry the password, or create a (N)ew personal store? "
                ).strip().lower()
                if choice == "n":
                    try:
                        CREDENTIALS_FILE = f"credentials_{getpass.getuser()}.enc"
                    except Exception:
                        CREDENTIALS_FILE = credential_store.DEFAULT_CREDENTIALS_FILE
                    return initialize_store()


def add_credential_type(credentials: dict, master_password: str) -> None:
    type_name = input("Enter a name for this credential type (e.g. tacacs, cucm, vtc): ").strip()
    if not type_name:
        print("Type name cannot be empty.")
        return
    if type_name in credentials:
        print(f"'{type_name}' already exists - use (U)pdate to change or add a field on it instead.")
        return

    standard = input("Is this a standard username + password credential? (Y/n): ").strip().lower()
    if standard != "n":
        fields = _prompt_username_password_fields()
    else:
        fields = _prompt_custom_fields(type_name)

    if not fields:
        print("No fields entered - nothing saved.")
        return

    credentials[type_name] = fields
    credential_store.save_and_encrypt(CREDENTIALS_FILE, credentials, master_password)
    print(f"Credential type '{type_name}' saved with {len(fields)} field(s).")


def update_credential_field(credentials: dict, master_password: str) -> None:
    """Change an existing field's value, or add a new field to an
    already-existing type - the same action either way from the
    person's point of view ("set this field to this value").
    """
    if not credentials:
        print("Store is empty - use (A)dd first.")
        return
    print("Existing credential types:", ", ".join(sorted(credentials.keys())))
    type_name = input("Enter the credential type to update: ").strip()
    if type_name not in credentials:
        print(f"'{type_name}' not found.")
        return

    existing_fields = credentials[type_name]
    print(f"'{type_name}' currently has fields: {', '.join(sorted(existing_fields.keys())) or '(none)'}")
    field_name = input("Enter the field name to update (or a new name to add one): ").strip()
    if not field_name:
        print("Field name cannot be empty.")
        return

    is_new_field = field_name not in existing_fields
    if is_new_field:
        sensitive = input(f"Is '{field_name}' sensitive (masked when typed/viewed)? (Y/n): ").strip().lower() != "n"
    else:
        sensitive = existing_fields[field_name]["sensitive"]

    value = _prompt_field_value(field_name, sensitive)
    existing_fields[field_name] = {"value": value, "sensitive": sensitive}

    credential_store.save_and_encrypt(CREDENTIALS_FILE, credentials, master_password)
    action = "added to" if is_new_field else "updated in"
    print(f"Field '{field_name}' {action} '{type_name}'.")


def view_credentials(credentials: dict) -> None:
    """Shows every stored type and field. Non-sensitive values (like a
    username) print directly; sensitive ones show a placeholder rather
    than the real value - see reveal_field() for deliberately viewing
    one for real.
    """
    if not credentials:
        print("Store is empty.")
        return
    print("\n--- Stored Credentials ---")
    for type_name in sorted(credentials.keys()):
        print(f"{type_name}:")
        fields = credentials[type_name]
        if not fields:
            print("    (no fields)")
            continue
        for field_name in sorted(fields.keys()):
            field = fields[field_name]
            shown = "********" if field["sensitive"] else field["value"]
            print(f"    {field_name}: {shown}")
    print("--------------------------")


def reveal_field(credentials: dict) -> None:
    """Deliberately show one field's real value, including sensitive
    ones - a distinct, explicit action (not a side effect of viewing
    the store), so a real secret only ever prints when someone
    specifically asks to see that exact one.
    """
    if not credentials:
        print("Store is empty.")
        return
    print("Existing credential types:", ", ".join(sorted(credentials.keys())))
    type_name = input("Enter the credential type: ").strip()
    if type_name not in credentials:
        print(f"'{type_name}' not found.")
        return

    fields = credentials[type_name]
    print(f"'{type_name}' fields: {', '.join(sorted(fields.keys())) or '(none)'}")
    field_name = input("Enter the field name to reveal: ").strip()
    if field_name not in fields:
        print(f"'{field_name}' not found on '{type_name}'.")
        return

    confirm = input(
        f"Reveal the real value of '{type_name}.{field_name}'? This will print it visibly. (y/N): "
    ).strip().lower()
    if confirm != "y":
        print("Cancelled.")
        return
    print(f"{type_name}.{field_name} = {fields[field_name]['value']}")


def delete_credential_type(credentials: dict, master_password: str) -> None:
    """Deletes an entire credential type (all its fields together) -
    not single fields, to avoid silently leaving a type in a broken
    partial state (e.g. a tacacs entry with a username but no
    password). Re-add the type fresh if you need to remove just one
    field from it.
    """
    if not credentials:
        print("Store is empty.")
        return
    print("Existing credential types:", ", ".join(sorted(credentials.keys())))
    type_name = input("Enter the credential type to delete: ").strip()
    if type_name not in credentials:
        print(f"'{type_name}' not found.")
        return

    confirm = input(
        f"Are you sure you want to delete the ENTIRE '{type_name}' credential (all its fields)? (y/n): "
    ).strip().lower()
    if confirm == "y":
        del credentials[type_name]
        credential_store.save_and_encrypt(CREDENTIALS_FILE, credentials, master_password)
        print(f"Credential type '{type_name}' deleted.")
    else:
        print("Deletion cancelled.")


def main_menu(credentials: dict, master_password: str) -> None:
    """Interactive loop for managing the store's credential types."""
    while True:
        print("\n--- Credential Manager ---")
        print("(A)dd a new credential type")
        print("(U)pdate a field (or add a new field to an existing type)")
        print("(V)iew stored credential types")
        print("(R)eveal a field's real value")
        print("(D)elete an entire credential type")
        print("(Q)uit and save")
        choice = input("Enter your choice: ").strip().lower()

        if choice == "a":
            add_credential_type(credentials, master_password)
        elif choice == "u":
            update_credential_field(credentials, master_password)
        elif choice == "v":
            view_credentials(credentials)
        elif choice == "r":
            reveal_field(credentials)
        elif choice == "d":
            delete_credential_type(credentials, master_password)
        elif choice == "q":
            print("Exiting.")
            break
        else:
            print("Invalid choice, please try again.")


if __name__ == "__main__":
    try:
        print(f"Using credential store: {CREDENTIALS_FILE}")
        if not os.path.exists(CREDENTIALS_FILE):
            master_password, credentials = initialize_store()
        else:
            master_password, credentials = unlock_store()

        main_menu(credentials, master_password)

    except KeyboardInterrupt:
        print("\nOperation cancelled by user. Exiting.")
    except Exception as e:
        print(f"\nA fatal error occurred: {e}")
