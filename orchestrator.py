import os
import yaml
import argparse
import json
# --- Local Module Imports ---
import shared_utils
from tools import cisco_arp_tool, cisco_cdp_tool, cisco_config_tool, cisco_vlan_tool, vtc_api_tool

# --- Configuration ---
CONFIG_DIR = "./configs/"
OUTPUT_DIR = "./output/"
DISCOVERY_EXCLUSION_PATTERNS = ['SEP*', "*spine*", "*leaf*"]

# --- Main Phase Functions ---
def do_discovery_and_arp_phase(site_name, site_seed_device, creds, mgmt_override):
    # Phase 1: Discovery topology and collect all ARP data for a single site
    print(f"--- Starting Discovery & ARP Phase for site: {site_name} ---")
    output_dir = f"{OUTPUT_DIR}{site_name}/"
    os.makedirs(output_dir, exist_ok=True)
    subnet_info = cisco_vlan_tool.get_vlan_and_subnet_info(site_seed_device, creds['net_user'], creds['net_pass'])
    site_subnets = subnet_info.get('subnet_list', [])
    if not site_subnets:
        print("Critical Error: No subnets discovered. Aborting.")
        return False
    shared_utils.save_data_to_yaml(f"{output_dir}discoverd_vlans.yml", subnet_info, 'vlan_info')

    standardized_seed = {'device_name': site_seed_device.get('device_name', site_seed_device['ip']), 'ip': site_seed_device['ip'], 'type': site_seed_device.get('type', 'cisco_ios')}
    devices_to_scan = [standardized_seed]
    discovered_topology, discovered_by_name, scanned_ips = {}, {}, set()
    while devices_to_scan:
        current_device = devices_to_scan.pop(0)
        current_ip = current_device['ip']
        if current_ip in scanned_ips:
            continue
        neighbors = cisco_cdp_tool.get_discovered_devices(current_device, creds['net_user'], creds['net_pass'])
        scanned_ips.add(current_ip)
        if current_ip not in discovered_topology:
            discovered_topology[current_ip] = current_device
            if 'device_name' in current_device:
                discovered_by_name[current_device['device_name']] = current_ip
        if neighbors is None:
            continue

        for neighbor in neighbors:
            neighbor_name = neighbor.get('device_name', '')
            override_info = mgmt_override.get(neighbor_name)
            neighbor_ip = override_info.get('management_ip') if override_info else neighbor.get('ip_address')
            if shared_utils.is_excluded(neighbor_name, DISCOVERY_EXCLUSION_PATTERNS):
                continue
            if not neighbor_ip or not shared_utils.is_ip_in_subnets(neighbor_ip, site_subnets) or neighbor_name in discovered_by_name:
                continue
            standardized_neighbor = {'device_name': neighbor_name, 'ip': neighbor_ip, 'type': 'cisco_ios', 'platform': neighbor.get('platform', 'N/A')}
            devices_to_scan.append(standardized_neighbor)
            discovered_topology[neighbor_ip] = standardized_neighbor
            discovered_by_name[neighbor_name] = neighbor_ip
    shared_utils.save_data_to_yaml(f"{output_dir}discovered_topology.yml", list(discovered_topology.values()), "devices")

    full_arp_table = {}
    for device in discovered_topology.values():
        arp_data = cisco_arp_tool.get_cisco_arp_dict(device, creds['net_user'], creds['net_pass'])
        if arp_data:
            full_arp_table.update(arp_data)
    shared_utils.save_data_to_yaml(f"{output_dir}arp_table.yml", full_arp_table, "arp_table")
    return True

def do_enrichment_phase(site_name, creds):
    # Phase 2: Perform live enrichment on a pre-filtered list of devices
    print(f"--- Starting Live Enrichment Phase for site: {site_name} ---")
    output_dir = f"{OUTPUT_DIR}{site_name}/"
    group_arp_cache_path = os.getenv('SAD_GROUP_ARP_CACHE')
    if not group_arp_cache_path:
        print("Worker Error: SAD_GROUP_ARP_CACHE environment variable not set. Cannot load ARP data.")
        return False
    try:
        with open(f"{output_dir}devices_to_enrich.yml", 'r') as f:
            devices_to_enrich = yaml.safe_load(f).get('vtc_devices', [])
        with open(group_arp_cache_path, 'r') as f:
            group_arp_table = json.load(f)
    except FileNotFoundError as e:
        print(f"Error: Required input file not found for enrichment phase: {e}")
        return False

    mac_to_ip_map = {shared_utils.normalize_mac(details['mac_address']): ip for ip, details in group_arp_table.items()}
    enriched_list = []

    for device in devices_to_enrich:
        vtc_mac_normalized = shared_utils.normalize_mac(device['device_name'])
        ip_address = mac_to_ip_map.get(vtc_mac_normalized)
        device['ip_address'] = ip_address
        if ip_address:
            live_status = vtc_api_tool.get_device_status(ip_address, creds['vtc_user'], creds['vtc_pass'])
            if live_status:
                device.update(live_status)
            else:
                device['live_status'] = "UNREACHABLE"
        else:
            device['ip_address'] = "NOT_FOUND_IN_GROUP_ARP"
        enriched_list.append(device)
    shared_utils.save_data_to_yaml(F"{output_dir}vtc_devices_enriched.yml", enriched_list, 'vtc_devices')
    return True

def do_config_backup_phase(site_name, creds):
    # Phase 3: Backs up the running config for all discovered devices at a site
    print(f"--- Starting Configuration Backup Phase for site: {site_name} ---")
    # Define Paths
    site_output_dir = f"{OUTPUT_DIR}{site_name}/"
    config_backup_dir = f"{site_output_dir}configs/"
    archive_dir = f"{config_backup_dir}archive/"
    os.makedirs(archive_dir, exist_ok=True)

    # This phase depends on the discovery phase having run first
    try:
        with open(f"{site_output_dir}discovered_topology.yml", 'r') as f:
            discovered_devices = yaml.safe_load(f).get('devices', [])
    except FileNotFoundError:
        print(f"Error: Cannot run backup. 'discovered_topology.yml' not found for site '{site_name}'.")
        return False
    for device in discovered_devices:
        device_name = device.get('device_name')
        if not device_name:
            continue
        print(f"  -> Processing config for: {device_name}")

        # Get the new config and its hash
        new_config, new_hash = cisco_config_tool.get_config_and_hash(device, creds['net_user'], creds['net_pass'])
        if not new_config:
            print(f"    - Skipping {device_name} (could not fetch config).")
            continue
        current_config_path = f"{config_backup_dir}{device_name}.txt"
        old_hash = ""

        # Try to read the old config file to get its hash
        if os.path.exists(current_config_path):
            with open(current_config_path, 'r') as f:
                old_config = f.read()
                old_hash = cisco_config_tool.calculate_md5(old_config)

        # Compare hashes
        if new_hash == old_hash:
            print(f"    - No changes detected for {device_name}.")
        else:
            print(f"    - CHANGE DETECTED for {device_name}. Backup up new config.")
            # If an old file exists, move it to the archive
            if os.path.exists(current_config_path):
                import datetime
                timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                archive_path = f"{archive_dir}{device_name}_{timestamp}.txt"
                os.rename(current_config_path, archive_path)
                print(f"    - Archived old config to: {archive_path}")
            # Write the new config file
            with open(current_config_path, 'w') as f:
                f.write(new_config)
    return True

# --- Main Execution Block for the Worker ---
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SAD Worker Orchestrator")
    parser.add_argument("--site", required=True, help="The individual site to process.")
    parser.add_argument("--phase", required=True, choices=['discovery_and_arp', 'enrichment', 'backup_configs'], help="The execution phase.")
    args = parser.parse_args()

    # --- Retrieve credentials from temp credential file
    temp_creds_path = os.getenv('SAD_TEMP_CREDS_FILE')
    if not temp_creds_path:
        print("Worker Error: SAD_TEMP_CREDS_FILE environment variable not set. Cannot load credentials.")
        exit(1)
    try:
        with open(temp_creds_path, 'r') as f:
            creds = json.load(f)
        with open(f"{CONFIG_DIR}network_devices.yml", 'r') as f:
            all_network_devices = yaml.safe_load(f)
        with open(f"{CONFIG_DIR}management_overrides.yml", 'r') as f:
            mgmt_overrides = yaml.safe_load(f) or {}
    except (FileNotFoundError, json.JSONDecodeError, yaml.YAMLError) as e:
        print(f"Worker Error: Could not load initial configurations. Reason: {e}")
        exit(1)
    
    site_device_config = [dev for dev in all_network_devices if dev.get('site') == args.site]
    if not site_device_config:
        print(f"Worker Error: No devices found for site '{args.site}' in network_devices.yml")

    success = False
    if args.phase == 'discovery_and_arp':
        seed_device = shared_utils.find_device_by_role(site_device_config, 'discovery_seed')
        if not seed_device:
            print(f"Worker Error: No 'discovery_seed' device found for site '{args.site}'.")
            exit(1)
        success = do_discovery_and_arp_phase(args.site, seed_device, creds, mgmt_overrides)
    elif args.phase == 'enrichment':
        success = do_enrichment_phase(args.site, creds)
    elif args.phase == 'backup_configs':
        success = do_config_backup_phase(args.site, creds)
    
    if not success:
        print(f"Worker for site '{args.site}' phase '{args.phase}' failed.")
        exit(1)
    else:
        print(f"Worker for site '{args.site}' phase '{args.phase}' completed succesfully.")
        exit(0)
