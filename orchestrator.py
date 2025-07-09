import os
from cryptography.exceptions import InvalidTag

# --- Local Imports ---
import credential_loader 
from tools import cisco_arp_tool, cucm_vtc_tool, vtc_api_tool

# --- Configuration ---
CREDENTIALS_FILE = "./credentials.enc"
NETWORK_DEVICE_IP = '192.168.1.1' 
CUCM_IP = '192.168.1.10'
ARP_OUTPUT_FILE = 'output/arp.yml'
VTC_OUTPUT_FILE = 'output/vtc.yml'

# --- Helper Functions ---
def normalize_mac(mac_address: str) -> str:
    # Sterilizes mac addresses
    return mac_address.upper().replace('SEP', '').replace(':', '').replace('.', '').replace('-', '').lower()

def format_dict_to_yaml(data: dict, root_key: str) -> str:
    # Manually formats a dictionary of dictionaries into a YAML-like string.
    yaml_lines = [f"{root_key}:"]
    for primary_key, details_dict in data.items():
        yaml_lines.append(f"  {primary_key}:")
        for sub_key, value in details_dict.items():
            yaml_lines.append(f"    {sub_key}: {value}")
    return "\n".join(yaml_lines)

def format_list_to_yaml(data_list: list, root_key: str) -> str:
    # Manually formats a list of dictionaries into a YAML-like string.
    yaml_lines = [f"{root_key}:"]
    for item in data_list:
        first_key = list(item.keys())[0]
        yaml_lines.append(f"- {first_key}: {item[first_key]}")
        for key, value in list(item.items())[1:]:
            yaml_lines.append(f"  {key}: {value}")
    return "\n".join(yaml_lines)

# --- Main execution block ---
if __name__ == "__main__":
    print("--- Site Awareness Dashboard (SAD) ---")
    # 1. Load All Credentials Securely
    try:
        master_password = credential_loader.getpass.getpass("Enter master password for credentials file: ")
        creds = credential_loader.load_credentials(CREDENTIALS_FILE, master_password)
        # Extract credentials needed for this run
        net_user = creds.get('net_user')
        net_pass = creds.get('net_pass')
        cucm_user = creds.get('cucm_user')
        cucm_pass = creds.get('cucm_pass')
        vtc_user = creds.get('vtc_user')
        vtc_pass = creds.get('vtc_pass')

        if not all([net_user, net_pass, cucm_user, cucm_pass, vtc_user, vtc_pass]):
            raise ValueError("One or more required keys are missing from the credentials file.")
        print("Credentials successfully loaded and decrypted.")
    except (FileNotFoundError, InvalidTag, ValueError) as e:
        print(f"\nCritical Error: Could not load credentials. {e}")
        exit()
    # Create output directory if it doesn't exist
    os.makedirs('output', exist_ok=True)
    
    # --- TASK 1: Get and Save Full ARP Table ---
    print(f"\n--- Step 1. Fetching ARP table from {NETWORK_DEVICE_IP} ---")
    arp_device_info = {'device_type': 'cisco_ios', 'host': NETWORK_DEVICE_IP, 'username': net_user, 'password': net_pass,}
    full_arp_data = cisco.arp_tool.get_cisco_arp_dict(arp_device_info)
    if not full_arp_data:
        print("\nCritical Error: Could not retrieve ARP data. Exiting.")
        exit()
    try:
        arp_yaml_output = format_dict_to_yaml(full_arp_data, 'arp_table')
        with open(ARP_OUTPUT_FILE, 'w') as f:
            f.write(arp_yaml_output)
        print(f"Success! Full ARP table saved to '{ARP_OUTPUT_FILE}'")
    except IOError as e:
        print(f"Error writing ARP file: {e}")

    # --- TASK 2: Get CUCM Data, then Enrich with ARP and Live Status ---
    print(f"\n--- STEP 2: Fetching VTC data from {CUCM_IP} ---")
    vtc_devices = cucm_vtc_tool.get_vtc_devices(CUCM_IP, cucm_user, cucm_pass)

    if not vtc_devices:
        print("\nSkipping VTC enrichment: Could not retrieve VTC device data.")
        exit()

    print("\n--- STEP 3: Correlating Data and Querying Live Device Status ---")
    mac_to_ip_map = {normalize_mac(details['mac_address']): ip for ip, details in full_arp_data.items()}
    
    enriched_vtc_list = []
    total_devices = len(vtc_devices)
    
    for i, device in enumerate(vtc_devices, 1):
        vtc_mac_normalized = normalize_mac(device['device_name'])
        
        # Find IP address from ARP data
        ip_address = mac_to_ip_map.get(vtc_mac_normalized, "OFFLINE")
        device['ip_address'] = ip_address
        
        print(f"  -> Processing device {i}/{total_devices}: {device['device_name']}...", end='')
        
        # If the device has an IP, try to query its live status
        if ip_address != "OFFLINE":
            live_status = vtc_api_tool.get_device_status(ip_address, vtc_user, vtc_pass)
            
            if live_status:
                # Successfully got status, merge it into the device's dictionary
                device.update(live_status)
                print(" [STATUS OK]")
            else:
                # Could not connect (bad password, firewall, offline but still in ARP)
                device['live_status'] = "UNREACHABLE"
                print(" [STATUS FAILED]")
        else:
            print(" [OFFLINE]")

        enriched_vtc_list.append(device)
    
    # --- TASK 3: Write final enriched report ---
    print(f"\n--- Step 3. Writing enriched VTC report ---")
    vtc_yaml_output = format_list_to_yaml(enriched_vtc_list, 'vtc_devices')
    try:
        with open(VTC_OUTPUT_FILE, 'w') as f:
            f.write(vtc_yaml_output)
        print(f"\n --- All tasks complete. Enriched VTC data saved to '{VTC_OUTPUT_FILE}' ---")
    except IOError as e:
        print(f"--- Error writing VTC file: {e} ---")
