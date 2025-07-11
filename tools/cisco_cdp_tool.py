import re
from netmiko import ConnectHandler

def is_cdp_enabled(net_connect) -> bool:
    """
    Checks if CDP is running globally on the device.
    This is a quick sanity check before attempting discovery.
    Args:
        net_connect: An active Netmiko connection object.
    Returns:
        True if CDP is enabled, False otherwise.
    """
    output = net_connect.send_command("show cdp")

    # A device with CDP disabled will typically include this string.
    if "cdp is not enabled" in output.lower():
        return False    
    return True

def parse_cdp_neighbors_detail(cdp_output: str) -> list:
    # Parses the output of 'show cdp neighbors detail' to extract key information.
    # This is more complex than a simple table, so we process it block by block.
    discovered_devices = []
    # Split the output into blocks, one for each neighbor
    neighbor_blocks = cdp_output.strip().split('-------------------------')
    
    for block in neighbor_blocks:
        if not block.strip():
            continue
        device_info = {}
        device_id_match = re.search(r"Device ID: (.+)", block)
        ip_address_match = re.search(r"IP address: (.+)", block)
        platform_match = re.search(r"Platform: (.+?),", block)
        interface_match = re.search(r"Interface: (.+?),", block)
        
        if device_id_match and ip_address_match:
            device_info['device_name'] = device_id_match.group(1).strip()
            device_info['ip_address'] = ip_address_match.group(1).strip()
            if platform_match:
                device_info['platform'] = platform_match.group(1).strip()
            if interface_match:
                device_info['local_interface'] = interface_match.group(1).strip()
            discovered_devices.append(device_info)
    return discovered_devices

def get_discovered_devices(device_info: dict, username: str, password: str) -> list | None:
    # Connects to a seed device and discovers its CDP neighbors.
    # Includes a check to ensure CDP is globally enabled on the device first.
    conn_details = {
        'device_type': device_info.get('type', 'cisco_ios'),
        'host': device_info['ip'],
        'username': username,
        'password': password,
    }
    try:
        print(f"--- [CDP] Connecting to device {conn_details['host']} for discovery... ---")
        with ConnectHandler(**conn_details) as net_connect:
            # --- Start Sanity Check ---
            # Before we do anything else, check if CDP is even running.
            if not is_cdp_enabled(net_connect):
                print(f"  -> Warning: CDP is not enabled on {conn_details['host']}. Skipping discovery on this device.")
                return [] # Return an empty list, as there are no neighbors to find.
            # --- End Sanity Check ---
            print(f"  -> CDP is enabled. Running 'show cdp neighbors detail'...")
            output = net_connect.send_command("show cdp neighbors detail", read_timeout=90)
            
            if not output:
                # This now specifically means CDP is on, but no neighbors were seen.
                print("  -> No active CDP neighbors found.")
                return []
                
            return parse_cdp_neighbors_detail(output)
            
    except Exception as e:
        print(f"--- [CDP] Error during discovery on {conn_details['host']}: {e}")
        # Return None to indicate a connection/authentication failure which is different from finding zero neighbors.
        return None
