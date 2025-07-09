import re
from netmiko import ConnectHandler
from netmiko.exceptions import NetmikoTimeoutException, NetmikoAuthenticationException

def parse_cisco_arp(arp_output: str) -> dict:
    # Manually parses the raw string output of a 'show arp' command, capturing all details.
    arp_table_structured = {}
    header_pattern = re.compile(r"^\s*Protocol\s+Address")
    lines = arp_output.strip().split('\n')
    
    for line in lines:
        line = line.strip()
        if not line or header_pattern.match(line):
            continue
        parts = re.split(r'\s+', line)
        if len(parts) < 4:
            continue
        try:
            protocol = parts[0]
            ip_address = parts[1]
            age = parts[2]
            mac_address = parts[3]
            arp_type = parts[4] if len(parts) > 4 else 'N/A'
            interface = parts[5] if len(parts) > 5 else 'N/A'
            
            # The IP address is the key for the main dictionary
            arp_table_structured[ip_address] = {
                'mac_address': mac_address,
                'age': age,
                'interface': interface,
                'protocol': protocol,
                'type': arp_type
            }
        except IndexError:
            print(f"Warning: Skipping malformed ARP line: '{line}'")
            continue

    return arp_table_structured

def get_cisco_arp_dict(device_info: dict) -> dict | None:
    """
    Connects to a Cisco device and returns the parsed ARP table as a dictionary.
    Returns:
        A dictionary keyed by IP address with full details, or None on failure.
    """
    try:
        print(f"--- [ARP] Connecting to {device_info['host']}... ---")
        with ConnectHandler(**device_info) as net_connect:
            print("--- [ARP] Connection successful. Retrieving ARP table... ---")
            raw_arp_output = net_connect.send_command('show arp', use_textfsm=False)

            if not raw_arp_output:
                print("--- [ARP] Error: ARP table is empty or command failed. ---")
                return None

            print("--- [ARP] Parsing full ARP table details... ---")
            return parse_cisco_arp(raw_arp_output)

    except (NetmikoTimeoutException, NetmikoAuthenticationException) as e:
        print(f"--- [ARP] Error: Could not connect to network device. {e} ---")
        return None
    except Exception as e:
        print(f"--- [ARP] An unexpected error occurred: {e} ---")
        return None
