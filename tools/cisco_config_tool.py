import hashlib
from netmiko import ConnectHandler

def get_running_config(device_info: dict, username: str, password: str) -> str | None:
    # Connects to a device and retrieves its running configuration
    conn_details = {
        'device_type': device_info.get('type', 'cisco_ios'),
        'host': device_info.get('ip'),
        'username': username,
        'password': password,
    }
    try:
        with ConnectHandler(**conn_details) as net_connect:
            output = net_connect.send_command("show running-config", read_timeout=120)
            return output
    except Exception as e:
        print(f"    -> Error getting config from {conn_details['host']}: {e}")
        return None

def calculate_md5(config_text: str) -> str:
    # Calculates the MD5 hash of a given string of text
    if not config_text:
        return ""
    return hashlib.md5(config_text.encode('utf-8')).hexdigest()

def get_config_and_hash(device_info: dict, username: str, password: str) -> tuple[str | None, str | None]:
    # A wrapper function that gets the running config and its MD5 hash
    # Returns a tuple containing (config_text, config_hash) or (None, None) on failure
    config_text = get_running_config(device_info, username, password)
    if config_text:
        config_hash = calculate_md5(config_text)
        return (config_text, config_hash)
    
    return (None, None)
