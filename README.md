# Site Awareness Dashboard (SAD)

The Site Awareness Dashboard is a Python-based orchestration platform that automatically collects, correlates, and reports on the real-time status of diverse IT systems across a network site.

## Overview

In any complex IT environment, understanding the current state of all devices—network gear, servers, and user endpoints—is a constant challenge. This platform solves that problem by providing a single, powerful script (`orchestrator.py`) that acts as a central brain. It runs a series of specialized "tool" scripts to gather live data from various sources, correlates that data to build a rich, holistic view, and saves the output into human-readable YAML files.

The end goal is to produce a "source of truth" data cache that can be used for reporting, troubleshooting, or as a backend for a web-based dashboard.

### Key Features

*   **Modular Tool-Based Architecture:** Each data source (Cisco IOS, CUCM, VTC endpoints) has its own specialized tool, making the platform easy to maintain and extend.
*   **Centralized Orchestration:** A single `orchestrator.py` script manages the entire data collection workflow.
*   **Secure Credential Management:** All sensitive credentials are encrypted at rest in a `credentials.enc` file using a master password. The platform never stores passwords in plain text or in the source code.
*   **Multi-Protocol Support:** Currently integrates with devices using SSH/CLI (Netmiko), AXL/SOAP (Requests), and device-specific XML/HTTPS APIs (xAPI).
*   **Data Correlation & Enrichment:** The platform intelligently combines data from multiple sources. For example, it uses MAC addresses from CUCM to find IP addresses from an ARP table and then queries the device directly for its live status.
*   **YAML-based Output:** All collected data is saved in a clean, structured, and human-readable YAML format, organized by site.

---

## Architecture Diagram

The project is organized into a clear, scalable structure:

```
sad_platform/
├── orchestrator.py             # The main script that manages the workflow.
├── credential_loader.py        # Module to securely load encrypted credentials.
├── credential_manager.py       # A CLI tool to manage (CRUD) credentials.
├── credentials.enc             # The encrypted file storing all secrets (NOT committed to git).
│
├── site_configs/               # Configuration files defining targets for each site.
│   └── new_york.yml
│
├── tools/                        # A package of specialist data-gathering modules.
│   ├── __init__.py
│   ├── cisco_arp_tool.py
│   ├── cucm_vtc_tool.py
│   └── vtc_api_tool.py
│
├── output/                       # Generated reports are saved here, organized by run.
│   └── arp.yml
│   └── vtc_devices.yml
│
└── requirements.txt            # Project dependencies.
```

---

## Getting Started

Follow these steps to set up the project on a new machine.

### Prerequisites

*   Python 3.8+
*   Access to the network devices, CUCM, and VTC endpoints you wish to query.

### Installation

1.  **Clone the repository:**
    ```bash
    git clone https://github.com/cadencejames/Site-Awareness-Dashboard.git
    cd sad_platform
    ```

2.  **Create and activate a virtual environment (recommended):**
    ```bash
    # For Linux/macOS
    python3 -m venv venv
    source venv/bin/activate

    # For Windows
    python -m venv venv
    .\venv\Scripts\activate
    ```

3.  **Install dependencies:**
    This project relies on several key libraries. Install them all using the `requirements.txt` file.
    ```bash
    pip install -r requirements.txt
    ```

    _If `requirements.txt` does not exist, create it with the following content:_
    ```
    netmiko
    requests
    lxml
    cryptography
    ```
---

## Configuration

Before running the orchestrator, you must configure your credentials and target devices.

### 1. Configure Credentials

This platform uses an encrypted file (`credentials.enc`) for all secrets. You must create this file using the provided `credential_manager.py` script.

1.  **Run the credential manager to add secrets:**
    ```bash
    python credential_manager.py
    ```

2.  Follow the prompts to create the file and add the necessary key-value pairs. The `orchestrator.py` script currently requires the following keys:
    *   `net_user`: Username for network devices (e.g., switches/routers).
    *   `net_pass`: Password for network devices.
    *   `cucm_user`: Username for CUCM AXL API.
    *   `cucm_pass`: Password for CUCM AXL API.
    *   `vtc_user`: Local admin username for VTC endpoints.
    *   `vtc_pass`: Local admin password for VTC endpoints.

    **Note:** The `credentials.enc` file is explicitly ignored by `.gitignore` and should **NEVER** be committed to version control.

### 2. Configure Site Targets

Device IP addresses are hardcoded in `orchestrator.py` for simplicity. For production use, it is recommended to move these to a `site_configs/` YAML file.

---

## Usage

To run the full data collection and enrichment process:

1.  Navigate to the root project directory (`sad_platform/`).
2.  Ensure your virtual environment is activated.
3.  Execute the orchestrator script:
    ```bash
    python orchestrator.py
    ```
4.  You will be prompted to enter the **master password** to unlock the `credentials.enc` file.
5.  The script will provide console output as it progresses through each stage of data collection.
6.  Upon completion, the final reports will be saved in the `output/` directory as `.yml` files.

---

## Roadmap

This platform is under active development. Future enhancements include:

*   [ ] **Add a CDP/LLDP Tool:** Create `cisco_cdp_tool.py` to map network topology.
*   [ ] **Add a Server Status Tool:** Create a tool to check the status of Windows/Linux servers via WinRM or SSH.
*   [ ] **Dynamic Site Configuration:** Enhance `orchestrator.py` to accept a command-line argument (`--site new_york`) to load different site configuration files.
*   [ ] **Web Front-End:** Develop a simple web application (using Flask or Django) that reads the YAML files from the `output/` directory and displays them in a user-friendly dashboard.
*   [ ] **Fully Automated Runs:** Adapt the credential system to allow for non-interactive execution in CI/CD pipelines or scheduled tasks.

---

## Contributing

Contributions are welcome! If you have an idea for a new tool or an improvement, please feel free to fork the repository and submit a pull request.

1.  Fork the Project
2.  Create your Feature Branch (`git checkout -b feature/AmazingFeature`)
3.  Commit your Changes (`git commit -m 'Add some AmazingFeature'`)
4.  Push to the Branch (`git push origin feature/AmazingFeature`)
5.  Open a Pull Request

---

## License

Distributed under the MIT License. See `LICENSE.txt` for more information.
