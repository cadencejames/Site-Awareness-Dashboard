import os
import yaml
import ipaddress

# --- Data Handling Helpers ---
def normalize_mac(mac_address: str) -> str:
    # Converts MAC formats to a standard format (lowercase, no separators).
    if not mac_address:
        return ""
    return mac_address.upper().replace('SEP', '').replace(':', '').replace('.', '').replace('-', '').lower()

def save_data_to_yaml(filepath: str, data: dict | list, root_key: str):
    # Saves Python data to a YAML file, creating the directory if needed.
    try:
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, 'w') as f:
            yaml.dump({root_key: data}, f, default_flow_style=False, sort_keys=False)
        print(f"  -> Success! Data saved to '{filepath}'")
    except IOError as e:
        print(f"  -> Error: Could not write to file '{filepath}'. Reason: {e}")

def _find_target_node_recursive(current_node, target_key):
    # Recursively searches a nested dictionary/list structure for a specific key.
    # Returns the value associated with that key if found.
    if isinstance(current_node, dict):
        if target_key in current_node:
            return current_node[target_key]
        for value in current_node.values():
            found_node = _find_target_node_recursive(value, target_key)
            if found_node:
                return found_node
    elif isinstance(current_node, target_key):
        for item in current_node:
            found_node = _find_target_node_recursive(item, target_key)
            if found_node is not None:
                return found_node
    return None

def _flatten_sites_recursive(node, sites_set):
    # Recursively traverse the group structure
    # It populates the 'sites_set' with all found site names (strings)
    if isinstance(node, str):
        sites_set.add(node)
    elif isinstance(node, list):
        for item in node:
            _flatten_sites_recursive(item, sites_set)
    elif isinstance(node, dict):
        for value in node.values():
            _flatten_sites_recursive(value, sites_set)

# --- Network Logic Helpers ---
def is_ip_in_subnets(ip: str, subnets: list) -> bool:
    # Checks if a given IP address belongs to any of the provided subnets.
    try:
        ip_addr = ipaddress.ip_address(ip)
        for subnet_str in subnets:
            if ip_addr in ipaddress.ip_network(subnet_str):
                return True
    except (ValueError, TypeError):
        return False
    return False

def generate_vtc_pattern(seed_ip: str) -> str | None:
    # Generates the site-specific VTC phone number pattern based on the seed IP.
    try:
        site_code_str = seed_ip.split('.')[1]
        base_prefix, vtc_suffix = 5, '1'
        if len(site_code_str) == 3:
            new_prefix = base_prefix + int(site_code_str[0])
            final_pattern = f"{new_prefix}{site_code_str[1:]}{vtc_suffix}%"
        elif len(site_code_str) == 2:
            final_pattern = f"{base_prefix}{site_code_str}{vtc_suffix}%"
        elif len(site_code_str) == 1:
            final_pattern = f"{base_prefix}0{site_code_str}{vtc_suffix}%"
        else:
            return None
        return final_pattern
    except (IndexError, ValueError):
        return None

# --- Configuration & Discovery Helpers ---
def is_excluded(device_name: str, patterns: list) -> bool:
    # Checks if a device name matches any of the exclusion patterns.
    if not device_name:
        return False # Cannot exclude an empty name
    device_name_lower = device_name.lower()

    for pattern in patterns:
        p_lower = pattern.lower()
        
        # Case 1: Wildcard at both ends
        if p_lower.startswith('*') and p_lower.endswith('*'):
            # Strip the asterisks and check if the pattern is contained within the name
            if p_lower.strip('*') in device_name_lower:
                return True
        # Case 2: Wildcard at the end only
        elif p_lower.endswith('*'):
            if device_name_lower.startswith(p_lower.strip('*')):
                return True
        # Case 3: Wildcard at the beginning only
        elif p_lower.startswith('*'):
            if device_name_lower.endswith(p_lower.strip('*')):
                return True
        # Case 4: No wildcard, exact match
        else:
            if device_name_lower == p_lower:
                return True
    # If no pattern matched after checking all of them
    return False

def find_device_by_role(device_list, target_role):
    # Finds the first device dictionary in a list that has a specific role.
    for device in device_list:
        if target_role in device.get('roles', []):
            return device
    return None
