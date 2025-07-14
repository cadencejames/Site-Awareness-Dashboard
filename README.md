# Site Awareness Dashboard (SAD)

The Site Awareness Dashboard is a Python-based orchestration platform that automatically collects, correlates, and reports on the real-time status of diverse IT systems across one or more network sites. It is designed to handle complex, multi-site environments with shared services and provides a unified, up-to-date snapshot of a site's infrastructure.

## Overview

This platform uses a powerful **Conductor/Worker** architecture to dynamically discover and inventory an entire network site from a single "seed" device. It intelligently gathers data from multiple sources, combines it into a group-wide data cache, and produces a series of detailed YAML reports that serve as a "source of truth" for the environment's state.

### Key Features

*   **Hierarchical Group Processing:** Define simple or deeply nested groups of sites in a YAML file. The platform can run against a single site or an entire group (e.g., `all`, `east_coast`).
*   **Dynamic Two-Phase Discovery:**
    1.  **VLAN/Subnet Discovery:** Automatically discovers a site's local IP subnets from a single seed device.
    2.  **Recursive Topology Discovery:** Uses the discovered subnets to perform a multi-hop discovery of all in-scope network devices via CDP.
*   **Intelligent Filtering:** Discovery is precise, with configurable rules to ignore irrelevant devices (like phones), out-of-scope networks (WAN/transit links), and to handle devices with separate management IPs.
*   **Secure, Centralized Credential Management:** A single master password entered once at runtime decrypts all necessary credentials. These are securely passed to worker processes via a temporary file cache that is automatically cleaned up.
*   **Group-Wide Data Aggregation:** For group runs, the conductor aggregates data (like ARP tables) from all member sites to create a unified data cache for accurate, context-aware filtering.
*   **Modular Architecture:** The system is cleanly separated into a smart `conductor`, a "dumb" `orchestrator` worker, a library of `tools`, and a `shared_utils` module for maximum maintainability and extensibility.

---

## Architecture

The platform is designed around a clear separation of concerns.

*   **`conductor.py` (The Brain):** The main script you run. It handles user input (target site/group), parses group definitions, manages the multi-phase workflow, aggregates data from multiple sites, and calls the orchestrator worker.
*   **`orchestrator.py` (The Worker):** A "dumb" worker script that performs specific tasks for a single site when called by the conductor. It executes distinct phases like "discovery & ARP" or "live enrichment".
*   **`shared_utils.py`:** A library of common helper functions (e.g., data formatters, recursive parsers) used by both the conductor and orchestrator to reduce code duplication.
*   **`tools/`:** A package of specialist modules, each responsible for communicating with a specific type of system (e.g., Cisco IOS, CUCM AXL, VTC xAPI).
*   **`configs/`:** A directory of YAML files that define the entire environment, including seed devices, site groups, and service endpoints.

```
sad_platform/
├── conductor.py                    # The main script you run.
├── orchestrator.py                 # The worker script called by the conductor.
├── shared_utils.py                 # Common helper functions.
├── credential_loader.py            # Securely loads encrypted credentials.
├── credential_manager.py           # CLI tool to manage credentials.
├── credentials.enc                 # The encrypted secrets file (NEVER commit).
│
├── configs/
│   ├── network_devices.yml         # Defines seed devices for each site.
│   ├── site_groups.yml             # Defines simple or nested site groups.
│   ├── services.yml                # Defines centralized enterprise services (CUCM, DNS, etc.).
│   └── management_overrides.yml    # Maps device names to reachable management IPs.
│
└── tools/
    └── ...
```

---

## Getting Started

### Prerequisites

*   Python 3.8+
*   Git

### Installation

1.  **Clone the repository:**
    ```bash
    git clone <your-repo-url>
    cd sad_platform
    ```

2.  **Create and activate a virtual environment:**
    ```bash
    python -m venv venv
    source venv/bin/activate
    ```

3.  **Install dependencies:**
    ```bash
    pip install -r requirements.txt
    ```
    _Your `requirements.txt` should contain:_
    ```
    netmiko
    requests
    lxml
    cryptography
    pyyaml
    ntc-templates
    ```

---

## Configuration

### 1. Configure Credentials
Use the `credential_manager.py` to create your `credentials.enc` file.
```bash
python credential_manager.py
```
Ensure you create keys for all required services (e.g., `net_user`, `net_pass`, `cucm_user`, `cucm_pass`, etc.).

### 2. Configure Your Environment
Populate the YAML files in the `configs/` directory:

1.  **`services.yml`:** Define your centralized enterprise applications (CUCM, DNS, etc.).
2.  **`network_devices.yml`:** Define the **seed device** for each individual site. Each entry must have a unique `site` name and the `discovery_seed` role.
3.  **`site_groups.yml`:** Define your operational groups. You can create simple flat groups or complex, nested hierarchies. The key is the name you will use with the `--target` flag.
4.  **`management_overrides.yml`:** (Optional) Add entries for any devices that must be accessed via a specific management IP that differs from their CDP-advertised IP.

---

## Usage

You will always interact with the platform via the `conductor.py` script. It accepts a single required argument: `--target`.

The target can be the name of an individual site or a group defined in `site_groups.yml`.

1.  **Run against a single site:**
    ```bash
    python conductor.py --target newark
    ```

2.  **Run against a simple group:**
    (Assuming `emea: [london, paris]` exists in `site_groups.yml`)
    ```bash
    python conductor.py --target emea
    ```
    This will process the `london` and `paris` sites sequentially.

3.  **Run against a nested group:**
    (Assuming `all: [united_states: [...], europe: [...]]` exists)
    ```bash
    python conductor.py --target all
    ```
    The conductor will recursively find every individual site defined under the `all` key and process them.

Upon execution, you will be prompted for your master password once. The conductor will then orchestrate the multi-phase run, and all output files will be saved into site-specific directories within `output/`.

---

## Roadmap

*   [ ] **Add Interface Status Tool:** Create a tool to collect `show interface status` from all discovered devices.
*   [ ] **Add Server Status Tool:** Create a tool to check the status of Windows/Linux servers via WinRM or SSH.
*   [ ] **Web Front-End:** Develop a simple web application (using Flask or Django) that reads the YAML files from the `output/` directory and displays them in a user-friendly dashboard.
*   [ ] **Parallel Execution:** Enhance the conductor to run independent site collections in parallel to speed up large group runs.
