# conductor.py
import yaml
import subprocess
import os
import argparse
import tempfile
import json
import itertools
import pprint
from cryptography.exceptions import InvalidTag
# --- Local Module Imports
import credential_loader
import shared_utils
from tools import cucm_vtc_tool, dashboard_generator_tool

# --- Configuration ---
CONFIG_DIR = "./configs/"
OUTPUT_DIR = "./output/"
ORCHESTRATOR_SCRIPT = "orchestrator.py"
CREDENTIALS_FILE = "./credentials.enc"

def get_sites_to_process(target: str, groups_config_file: str) -> list:
    # Determines the list of individual sites to run based on the target
    try:
        with open(groups_config_file, 'r') as f:
            site_groups = yaml.safe_load(f) or {}
    except FileNotFoundError:
        print(f"Info: Site groups file: '{groups_config_file}' not found. Assuming target is a single site.")
        return [target]
    except yaml.YAMLError as e:
        print(f"Erro: Could not parse site groups file '{groups_config_file}'. Reason {e}")
        return []
    
    target_node = shared_utils._find_target_node_recursive(site_groups, target)
    if target_node is not None:
        print(f"Target '{target}' found as a group. Recursively resolving all member sites...")
        resolved_sites = set()
        shared_utils._flatten_sites_recursive(target_node, resolved_sites)
        return sorted(list(resolved_sites))
    else:
        print(f"Target: '{target}' not found as a group. Processing as a single site.")
        return [target]

# --- Main Execution Block ---
def main():
    parser = argparse.ArgumentParser(description="SAD Platform Conductor")
    parser.add_argument("--target", required=True, help="The site or group to process.")
    parser.add_argument("--run-mode", default="full",
                        choices=['full', 'discovery_only', 'backup_configs', 'generate_dashboard'],
                        help="Specify the operational workflow to run.")
    args = parser.parse_args()
    print("--- SAD Platform Conductor ---")
    
    temp_creds_file = None
    temp_arp_cache_file = None
    try:
        # --- 1. Load Credentials and Create Secure Temp File ---
        master_password = credential_loader.getpass.getpass("Enter master password to unlock credentials: ")
        creds = credential_loader.load_credentials(CREDENTIALS_FILE, master_password)

        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix=".json", encoding='utf-8') as tf:
            json.dump(creds, tf)
            temp_creds_file = tf.name
        print("Success: Credentials decrypted and loaded into a temporary cache.")
        os.environ['SAD_TEMP_CREDS_FILE'] = temp_creds_file
        
        # --- 2. Load Static Configurations ---
        with open(f"{CONFIG_DIR}network_devices.yml", 'r') as f:
            all_network_devices = yaml.safe_load(f)
        with open(f"{CONFIG_DIR}services.yml", 'r') as f:
            services_config = yaml.safe_load(f)

        # --- 3. Determine Sites and Group Info ---
        sites_to_process = get_sites_to_process(args.target, f"{CONFIG_DIR}site_groups.yml")
        if not sites_to_process:
            print(f"No valid sites found for target '{args.target}'. Exiting.")
            exit(1)
        print(f"\nFinal list of sites to be processed: {sites_to_process}")

        # --- 4. Phase 1: Run Discovery and ARP for all sites (Required for most modes) ---
        # This block is run for all modes that need discovery data
        run_discovery = args.run_mode != 'discovery_only'
        if args.run_mode == 'discovery_only':
            # discovery_only mode doesn't technically depend on other phases, so it has its own simple loop
            print("\nRun mode is 'discovery_only'. Running discovery phase...")
            for site in sites_to_process:
                print(f"\n-> Running Discovery/ARP for site: {site}")
                command = ["python", ORCHESTRATOR_SCRIPT, "--site", site, "--phase", "discovery_and_arp"]
                result = subprocess.run(command)
            run_discovery = False # Prevent running discovery again
        
        if run_discovery:
            print("\n--- CONDUCTOR PHASE 1: DISCOVERY & ARP COLLECTION ---")
            group_arp_table = {}
            site_subnet_map = {}
            for site in sites_to_process:
                print(f"\n-> Running Discovery/ARP for site: {site}")
                command = ["python", ORCHESTRATOR_SCRIPT, "--site", site, "--phase", "discovery_and_arp"]
                result = subprocess.run(command)
                if result.returncode != 0:
                    raise Exception(f"Worker script failed during discovery for site: {site}")
                try:
                    with open(f"{OUTPUT_DIR}/arp_table.yml", 'r') as f:
                        site_arp_data = yaml.safe_load(f).get('arp_table', {})
                    if isinstance(site_arp_data, dict):
                        group_arp_table.update(site_arp_data)
                    else:
                        print(f"  -> ERROR: Type mismatch. ARP data for site '{site}' is not a dictionary.")
                    with open(f"{OUTPUT_DIR}{site}/discovered_vlans.yml", 'r') as f:
                        site_subnet_map[site] = yaml.safe_load(f).get('vlan_info', {}).get('subnet_list', [])
                except FileNotFoundError:
                    print(f"Warning: Could not load discovery output for site {site}.")

            # --- DEBUG BLOCK #1: Inspect the Final ARP Table ---
            # print("\n" + "="*20 + " ARP AGGREGATION DEBUG " + "="*20)
            # print(f"Final Aggregated group_arp_table contains {len(group_arp_table)} entries.")
            # if group_arp_table:
            #     print("Sample of aggregated ARP entries:")
            #     for ip, details in itertools.islice(group_arp_table.items(), 3):
            #         print(f"  IP: {ip}, MAC: {details.get('mac_address')}")
            #     print("="*61 + "\n")
            # --- END DEBUG BLOCK #1 ---

            with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix=".json", encoding='utf-8') as tf:
                json.dump(group_arp_table, tf)
                temp_arp_cache_file = tf.name
            os.environ['SAD_GROUP_ARP_CACHE'] = temp_arp_cache_file
            print("Success: Aggregated ARP entries and created temporary cache.")

        # --- 5. Conditional Workflow based on --run-mode ---
        if args.run_mode == 'backup_configs':
            print("\n--- CONDUCTOR WORKFLOW: CONFIGURATION BACKUP ---")
            for site in sites_to_process:
                print(f"\n-> Delegating config backup for site: {site}")
                command = ["python", ORCHESTRATOR_SCRIPT, "--site", site, "--phase", "backup_configs"]
                subprocess.run(command)
        
        elif args.run_mode == 'full' or args.run_mode == 'generate_dashboard':
            print("\n--- CONDUCTOR WORKFLOW: VTC/PHONE ENRICHMENT ---")
            primary_site_name = args.target if args.target in site_subnet_map else sites_to_process[0]
            primary_site_devices = [dev for dev in all_network_devices if dev.get('site') == primary_site_name]
            primary_site_seed = shared_utils.find_device_by_role(primary_site_devices, 'discovery_seed')
            vtc_pattern = shared_utils.generate_vtc_pattern(primary_site_seed['ip']) if primary_site_seed else None

            if vtc_pattern:
                global_phone_list = cucm_vtc_tool.get_vtc_devices(services_config['cucm_cluster']['publisher_ip'], creds['cucm_user'], creds['cucm_pass'], vtc_pattern)
                if global_phone_list:
                    mac_to_ip_map = {shared_utils.normalize_mac(details['mac_address']): ip for ip, details in group_arp_table.items()}

                    # --- DEBUG BLOCK #2: Inspect the MAC addresses ---
                    # print("\n" + "="*20 + " VTC FILTERING DEBUG " + "="*20)
                    # print(f"CUCM returned {len(global_phone_list)} devices.")
                    # print(f"The mac_to_ip_map (from ARP table) contains {len(mac_to_ip_map)} entries.")
                    # sample_phone_from_cucm = global_phone_list[0]
                    # cucm_mac_raw = sample_phone_from_cucm.get('device_name')
                    # cucm_mac_normalized = shared_utils.normalize_mac(cucm_mac_raw)
                    # sample_arp_mac_raw = list(group_arp_table.values())[0].get('mac_addres') if group_arp_table else "N/A"
                    # arp_mac_normalized = shared_utils.normalize_mac(sample_arp_mac_raw)
                    # print("\n--- MAC Address Format Comparison ---")
                    # print(f"Sample CUCM MAC (Raw)        : {cucm_mac_raw}")
                    # print(f"Sample CUCM Mac (Normalized) : {cucm_mac_normalized}")
                    # print(f"Sample ARP Mac (Raw)         : {sample_arp_mac_raw}")
                    # print(f"Sample ARP Mac (Normalized)  : {arp_mac_normalized}")
                    # match_found = cucm_mac_normalized in mac_to_ip_map
                    # print(f"\nDoes the sample normlaized CUCM MAC exist in the ARP map? -> {match_found}")
                    # print("="*59 + "\n")
                    # --- END DEBUG BLOCK #2

                    group_phones = [phone for phone in global_phone_list if shared_utils.normalize_mac(phone['device_name']) in mac_to_ip_map]
                    print(f"Success: Filtered global list down to {len(group_phones)} phones belonging to this group.")
                    for site in sites_to_process:
                        site_subnets = site_subnet_map.get(site, [])
                        devices_for_this_site = [p for p in group_phones if shared_utils.is_ip_in_subnets(mac_to_ip_map.get(shared_utils.normalize_mac(p['device_name'])), site_subnets)]
                        if devices_for_this_site:
                            print(f"Delegating {len(devices_for_this_site)} devices to '{site}' for enrichment.")
                            shared_utils.save_data_to_yaml(f"{OUTPUT_DIR}{site}/devices_to_enrich.yml", devices_for_this_site, 'vtc_devices')
                            command = ["python", ORCHESTRATOR_SCRIPT, "--site", site, "--phase", "enrichment"]
                            subprocess.run(command)
                        else:
                            print(f"No VTC/Phones from the group found in this site '{site}'. Skipping enrichment.")
            else:
                print("Warning: Could not generate VTC pattern. Skipping all VTC/Phone tasks.")
            
            if args.run_mode == 'generate_dashboard':
                print("\n--- CONDUCTOR WORKFLOW: GENERATING DASHBOARD ---")
                print("Backing up configurations first...")
                for site in sites_to_process:
                    command = ["python", ORCHESTRATOR_SCRIPT, "--site", site, "--phase", "backup_configs"]
                    subprocess.run(command)
                dashboard_generator_tool.generate_dashboard(sites_to_process)
    
    except (FileNotFoundError, InvalidTag, ValueError, yaml.YAMLError, Exception) as e:
        print(f"\nCRITICAL CONDUCTOR ERROR: {e}")
    finally:
        if temp_creds_file and os.path.exists(temp_creds_file):
            print("\nCleaning up temporary credential file...")
            os.remove(temp_creds_file)
        if temp_arp_cache_file and os.path.exists(temp_arp_cache_file):
            print("\nCleaning up temporary ARP cache file...")
            os.remove(temp_arp_cache_file)
    print("\n--- Conductor has finished all phases. ---")

if __name__ == "__main__":
    main()
