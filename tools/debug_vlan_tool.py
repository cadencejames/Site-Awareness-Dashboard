# A minimal script to inspect the parsed output of 'show ip interface'
# from your specific device.

import getpass
import pprint  # The "pretty-print" library is perfect for viewing data structures
from netmiko import ConnectHandler

# --- IMPORTANT: Fill in your device details ---
DEVICE_INFO = {
    'device_type': 'cisco_ios',
    'host': '192.168.1.1', # <-- Your device IP
}

def main():
    # Connects to the device and prints the parsed output.
    username = input("Enter username: ")
    password = getpass.getpass("Enter password: ")

    conn_details = {
        **DEVICE_INFO,
        'username': username,
        'password': password,
    }
    try:
        print("\n--- Connecting to device... ---")
        with ConnectHandler(**conn_details) as net_connect:
            print("--- Connection successful. Fetching 'show ip interface'... ---")
            # This is the command we need to inspect
            parsed_data = net_connect.send_command("show ip interface", use_textfsm=True)
            print("\n" + "="*50)
            print("--- Raw Parsed Data from TextFSM ---")
            print("="*50)
            # Use pprint to display the data structure clearly
            pprint.pprint(parsed_data)
            print("\n" + "="*50)
            print("--- End of Data ---")

    except Exception as e:
        print(f"\nAn error occurred: {e}")

if __name__ == "__main__":
    main()
