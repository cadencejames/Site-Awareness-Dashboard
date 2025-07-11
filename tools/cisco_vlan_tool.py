from netmiko import ConnectHandler
import ipaddress

def get_vlan_and_subnet_info(device_info: dict, username: str, password: str) -> dict:
    """
    Connects to a device and discovers its VLANs and associated IP subnets.
    Returns:
        A dictionary with two keys: 'vlan_list' and 'subnet_list'.
    """
    conn_details = {
        'device_type': device_info.get('type', 'cisco_ios'),
        'host': device_info['ip'],
        'username': username,
        'password': password,
    }

    discovered_data = {
        "vlan_list": [],
        "subnet_list": []
    }
    try:
        print(f"--- [VLAN] Connecting to {conn_details['host']} for VLAN/Subnet discovery... ---")
        with ConnectHandler(**conn_details) as net_connect:
            vlans = net_connect.send_command("show vlan brief", use_textfsm=True)
            if vlans:
                discovered_data["vlan_list"] = vlans
            interfaces = net_connect.send_command("show ip interface brief", use_textfsm=True)
            if not interfaces:
                return discovered_data # Return what we have if the command fails
            subnets = set() # Use a set to avoid duplicate subnets
            for interface in interfaces:
                # Get the list of IPs and prefix lengths
                ips = interface.get('ip_address', [])
                prefixes = interface.get('prefix_length', [])
                if ips and prefixes and len(ips) == len(prefixes):
                    for i in range(len(ips)):
                        ip = ips[i]
                        prefix = prefixes[i]
                        try:
                            # Create an IPv4Interface object to easily get the network address
                            iface_obj = ipaddress.IPv4Interface(f"{ip}/{prefix}")
                            subnets.add(str(iface_obj.network))
                        except ValueError:
                            # Ignore invalid IP/mask combinations
                            continue
            discovered_data['subnet_list'] = sorted(list(subnets))
    except Exception as e:
        print(f"--- [VLAN] Error during VLAN discovery on {conn_details['host']}: {e}")
    return discovered_data
