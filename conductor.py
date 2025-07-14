import yaml
import subprocess
import os
import argparse
import tempfile
import json
from cryptography.exceptions import InvalidTag
# --- Local Module Imports ---
import credential_loader
import shared_utils
from tools import cucm_vtc_tool

# --- Configuration ---
CONFIG_DIR = "./configs/"
OUTPUT_DIR = "./output/"
ORCHESTRATOR_SCRIPT = "orchestrator.py"
CREDENTIALS_FILE = "./credentials.enc"

def get_sites_to_process(target: str, groups_config_file: str) -> list:
    # Determines the list of individual sites to run based on the target
    # Handles deeply nested group definitions by searching for the target key and then flattening the result
    try:
        with open(groups_config_file, 'r') as f:
            site_groups = yaml.safe_load(f) or {}
    except FileNotFoundError:
        print(f"Info: Site groups file: '{groups_config_file}' not found. Assuming target is a single site.")
        return [target]
    except yaml.YAMLError as e:
        print(f"Error: Could not parse site groups file '{groups_config_file}'. Reason {e}")
        return []
    # Stage 1: Search the entire YAML structure for our target key using the shared helper.
    # The search starts from the root of the loaded YAML data.
    target_node = shared_utils._find_target_node_recursive(site_groups, target)
    if target_node is not None:
        # Target key was found. Now, flatten its contents to get the final list of site names.
        print(f"Target '{target}' found as a group. Recursively resolving all member sites...")
        resolved_sites = set()
        # Call the second helper to traverse the found node and collect all the site strings.
        shared_utils._flatten_sites_recursive(target_node, resolved_sites)
        # Return a sorted list for consistent, predictable execution order
        return sorted(list(resolved_sites))
    else:
        # If the target key was not found anywhere in the group file
        # then we treat the target itself as the name of the single, individual site
        print(f"Target: '{target}' not found as a group. Processing as a single site.")
        return [target]
    
# --- Main Execution Block ---
def main():
    parser = argparse.ArgumentParser(description="SAD Platform Conductor")
    parser.add_argument("--target", required=True, help="The site or group to process.")
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

        # --- 4. Phase 1: Run Discovery and ARP for all sites ---
        print("\n--- CONDUCTOR PHASE 1: DISCOVERY & ARP COLLECTION ---")
        group_arp_table = {}
        site_subnet_map = {}
        for site in sites_to_process:
            print(f"\n-> Running Discovery/ARP for site: {site}")
            command = ["python", ORCHESTRATOR_SCRIPT, "--site", site, "--phase", "discovery_and_arp"]
            result = subprocess.run(command)
            if result.returncode != 0:
                print(f"ERROR: Orchestrator worker failed during discovery for site: '{site}'. Halting group run.")
                raise Exception(f"Worker script failed for site {site}")
            try:
                with open(f"{OUTPUT_DIR}{site}/arp_table.yml", 'r') as f:
                    loaded_yaml = yaml.safe_load(f)
                    site_arp_data = loaded_yaml.get('arp_table', {})
                # --- Debugging Print Statements ---
                # print(f" -> Loaded ARP data for site '{site}'. Type: {type(site_arp_data)}. Items: {len(site_arp_data)}"")
                # print(f" -> Current group_arp_table type: {type(group_arp_table)}"")
                # --- End Debugging Print Statements ---
                # Ensure both are dictionaries before updating
                if isinstance(site_arp_data, dict) and isinstance(group_arp_table, dict):
                    group_arp_table.update(site_arp_data)
                else:
                    print(f"  -> ERROR: Type mismatch. Cannot merge ARP data for site '{site}'.")
                # Load the VLAN/Subnet Info
                with open(f"{OUTPUT_DIR}{site}/discovered_vlans.yml", 'r') as f:
                    site_subnet_map[site] = yaml.safe_load(f).get('vlan_info', {}).get('subnet_list', [])
            
            except FileNotFoundError:
                print(f"Warning: Could not load discovery output for site {site}. It may be missing from enrichment.")
            except Exception as e:
                print(f"Error: Failed to process output files for site '{site}'. Reason {e}")
        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix=".json", encoding='utf-8') as tf:
            json.dump(group_arp_table, tf)
            temp_arp_cache_file = tf.name
        os.environ['SAD_GROUP_ARP_CACHE'] = temp_arp_cache_file
        print(f"\nSuccess: Aggregated {len(group_arp_table)} ARP entries and created temporary cache.")
        # --- 5. Phase 2: Global VTC/Phone Filtering ---
        print("\n--- CONDUCTOR PHASE 2: GLOBAL VTC/PHONE FILTERING ---")
        primary_site_name = args.target if args.target in site_subnet_map else sites_to_process[0]
        primary_site_devices = [dev for dev in all_network_devices if dev.get('site') == primary_site_name]
        primary_site_seed = shared_utils.find_device_by_role(primary_site_devices, 'discovery_seed')
        vtc_pattern = shared_utils.generate_vtc_pattern(primary_site_seed['ip']) if primary_site_seed else None
        if vtc_pattern:
            global_phone_list = cucm_vtc_tool.get_vtc_devices(services_config['cucm_cluster']['publisher_ip'], creds['cucm_user'], creds['cucm_pass'], vtc_pattern)
            mac_to_ip_map = {shared_utils.normalize_mac(details['mac_address']): ip for ip, details in group_arp_table.items()}
            group_phones = [phone for phone in global_phone_list if shared_utils.normalize_mac(phone['device_name']) in mac_to_ip_map]
            print(f"Success: Filtered global list down to {len(group_phones)} phones belonging to this group.")

            # --- 6. Phase 3: Delegate Final Enrichment ---
            print("\n--- CONDUCTOR PHASE 3: LIVE STATUS ENRICHMENT ---")
            for site in sites_to_process:
                site_subnets = site_subnet_map.get(site, [])
                devices_for_this_site = [
                    phone for phone in group_phones
                    if shared_utils.is_ip_in_subnets(mac_to_ip_map.get(shared_utils.normalize_mac(phone['device_name'])), site_subnets)
                ]
                if devices_for_this_site:
                    print(f"Delegating {len(devices_for_this_site)} devices to '{site}' for enrichment.")
                    output_path = f"{OUTPUT_DIR}{site}/devices_to_enrich.yml"
                    shared_utils.save_data_to_yaml(output_path, devices_for_this_site, 'vtc_devices')
                    command = ["python", ORCHESTRATOR_SCRIPT, "--site", site, "--phase", "enrichment"]
                    subprocess.run(command)
                else:
                    print(f"No VTC/Phones from the group found in site '{site}'. Skipping enrichment.")
        else:
            print(f"Warning: Could not generate VTC pattern. Skipping all VTC/Phone tasks.")
    
    except (FileNotFoundError, InvalidTag, ValueError, yaml.YAMLError, Exception) as e:
        print(f"\nCRITICAL CONDUCTOR ERROR: {e}")
    finally:
        # Finally, ensure the temp files are deleted even if the script crashes
        if temp_creds_file and os.path.exists(temp_creds_file):
            print("\nCleaning up temporary credential file...")
            os.remove(temp_creds_file)
        if temp_arp_cache_file and os.path.exists(temp_arp_cache_file):
            print("\nCleaning up temporary ARP Cache file...")
            os.remove(temp_arp_cache_file)
    print("\n--- Conductor has finished all phases. ---")

if __name__ == "__main__":
    main()
