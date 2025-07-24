import os
import json
import yaml
import datetime

# --- Configuration ---
OUTPUT_DIR = "./output/"
DASHBOARD_DIR = "./dashbaord/"

def _gather_site_data(site_name):
    # Helper to read all YAML/TXT files for a single site and compile them
    site_output_dir = os.path.join(OUTPUT_DIR, site_name)
    site_data = {
        'site_name': site_name.replace('_', ' ').replace('-', ' ').title(),
        'topology': [],
        'arp_table': {},
        'vtcs': [],
        'configs': {}
    }

    # Safely load each file, providing empty defaults if a file is missing
    try:
        with open(f"{site_output_dir}discovered_topology.yml", 'r') as f:
            site_data['topology'] = yaml.safe_load(f).get('devices', [])
    except FileNotFoundError:
        print(f"  - Info: discovered_topology.yml not found for site '{site_name}'.")
    try:
        with open(f"{site_output_dir}arp_table.yml", 'r') as f:
            site_data['arp_table'] = yaml.safe_load(f).get('arp_table', {})
    except FileNotFoundError:
        print(f"  - Info: arp_table.yml not found for site '{site_name}'.")
    try:
        with open(f"{site_output_dir}vtc_devices_enriched.yml", 'r') as f:
            site_data['vtcs'] = yaml.safe_load(f).get('vtc_devices', {})
    except FileNotFoundError:
        # This is not an error if the site does not have any VTCs
        pass

    # Load device configurations
    config_dir = f"{site_output_dir}configs/"
    if os.path.exists(config_dir):
        for filename in os.listdir(config_dir):
            if filename.endswith(".txt"):
                device_name = filename.replace(".txt", "")
                with open(f"{config_dir}{filename}", 'r', encoding='utf-8') as cfg_f:
                    site_data['configs'][device_name] = cfg_f.read()
    return site_data

def _generate_css():
    # Returns the full CSS content for the dashboard's style.css file
    return """
    :root {
        --bg-color: #F1F1F1; --card-bg: #FFF; --text-color: #333; --header-bg: #F9F9F9;
        --border-color: #E1E1E1; --shadow-color: #DDD; --link-color: #007BFF; --link-hover: #0056b3;
        --table-header-bg: #F2F2F2; --modal-backdrop: rgba(0,0,0,0.6); --pre-bg: #F5F5F5;
    }
    body.dark-mode {
        --bg-color: #121212; --card-bg: #1E1E1E; --text-color: #E0E0E0; --header-bg: #2A2A2A;
        --border-color: #333; --shadow-color: #000; --link-color: #66B2FF; --link-hover: #8CC9FF;
        --table-header-bg: #2C2C2C; --modal-backdrop: rgba(0,0,0,0.8); --pre-bg: #252525;
    }
    body {
        background-color: var(--bg-color); color: var(--text-color); margin: 0;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
        padding-top: 80px; transition: background-color 0.3s, color 0.3s; line-height: 1.6;
    }
    .header {
        background-color: var(--header-bg); padding: 10px 20px; display: flex; justify-content: space-between;
        align-items: center; border-bottom: 1px solid var(--border-color); position: fixed; top: 0; left: 0; right: 0;
        z-index: 1000; box-shadow: 0 2px 5px rgba(0,0,0,0.1); transition: background-color 0.3s, border-color 0.3s;
        height: 60px; box-sizing: border-box;
    }
    .page-title { font-size: 1.5em; font-weight: 600; margin: 0; }
    .header-right { display: flex; align-items: center; gap: 20px; }
    .main-container { padding: 20px; max-width: 1600px; margin: 0 auto; }
    .site-card {
        background-color: var(--card-bg); border: 1px solid var(--border-color); border-radius: 8px;
        padding: 20px; box-shadow: 0 2px 4px var(--shadow-color); transition: transform 0.2s, box-shadow 0.2s;
    }
    .site-card:hover { transform: translateY(-5px); box-shadow: 0 4px 8px var(--shadow-color); }
    .site-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 20px; }
    .site-card h2 { margin-top: 0; color: var(--link-color); }
    .site-card ul { list-style: none; padding: 0; margin: 10px 0 15px 0; }
    .site-card a.details-link { text-decoration: none; color: var(--link-color); font-weight: bold; }
    .site-card a.details-link:hover { text-decoration: underline; color: var(--link-hover); }
    .section { background-color: var(--card-bg); border: 1px solid var(--border-color); border-radius: 8px; padding: 20px; margin-bottom: 30px; }
    .section-header {
        font-size: 1.3em; font-weight: 600; padding-bottom: 10px; margin-bottom: 15px; margin-top: 0;
        border-bottom: 2px solid var(--link-color); cursor: pointer; user-select: none; display: flex; justify-content: space-between; align-items: center;
    }
    .section-header::after { content: ''; transition: transform 0.3s; font-size: 0.8em; }
    .section-header.collapsed::after { transform: rotate(-90deg); }
    .section-content { max-height: 2000px; overflow-x: auto; transition: max-height 0.5s ease-in-out, padding 0.3s ease, margin 0.3s ease; overflow: hidden; }
    .section-content.collapsed { max-height: 0; padding-top: 0; padding-bottom: 0; margin-top: 0; overflow: hidden; border-top: none; }
    table { width: 100%; border-collapse: collapse; margin-top: 10px; }
    th, td { padding: 8px 12px; border: 1px solid var(--border-color); text-align: left; font-size: 0.9em; color: inherit; }
    th { background-color: var(--table-header-bg); font-weight: 600; }
    a { color: var(--link-color); text-decoration: none; }
    a.hover { text-decoration: underline; }
    h1, h2, h3, h4, h5, h6 { color: inherit; }
    .modal {
        display: none; position: fixed; z-index: 2000; left: 0; top: 0; width: 100%; height: 100;
        overflow: auto; background-color: var(--modal-backdrop); backdrop-filter: blur(5px);
    }
    .nodal-content {
        background-color: var(--card-bg); margin: 5% auto; padding: 20px; border: 1px solid var(--border-color);
        width: 80%; max-width: 1000px; border-radius: 8px; position: relative; box-shadow: 0 5px 15px rgba(0,0,0,0.5);
    }
    .modal-content pre { background-color: var(--pre-bg); color: var(--text-color); padding: 15px; border-radius: 5px; max-height: 70vh; overflow-y: auto; white-space: pre-wrap; word-wrap: break-word; }
    .close-button { color: #AAA; position: absolute; top: 10px; right: 25px; font-size: 35px; font-weight: bold; cursor: pointer; }
    .close-button:hover { color: var(--text-color); }
    #theme-toggle-button {font-size: 1.5em; background: none; border: none; cursor: pointer; color: var(--text-color); }
    .topology-map { position: relative; min-height: 400px; border: 1px solid var(--border-color); padding: 10px; border-radius: 8px; overflow: auto; }
    .topo-node.seed { border-color: #FFAD33; border-width: 3px; z-index: 3; }
    .topo-node { 
        position: absolute; background-color: var(--card-bg); border: 2px solid var(--link-color); padding: 5px 10px; border-radius: 5px; box-shadow: 0 2px 4px var(--shadow-color);
        font-size: 0.9em; white-space: nowrap; cursor: pointer; transform: translate(-50%, -50%); transition: transform 0.2s, box-shadow 0.2s; z-index: 2;
    }
    .topo-node:hover { transform: translate(-50%, -50%) scale(1.1); z-index: 10; }
    .topo-line { position: absolute; background-color: var(--text-color); height: 1px; z-index: 1; }
    """

def _generate_js():
    # Returns the full JavaScript content for the dashboard.js file
    return """
    document.addEventListener('DOMContentLoaded', () => {
        const path = window.location.pathname;
        const pageName = path.split("/").pop();
        setupThemeToggle(); // Setup theme first
        if (pageName === 'index.html' || pageName === '') {
            buildIndexPage();
        } else {
            const siteName = pageName.replace('.html', '');
            buildSitePage(siteName)
        }
    });

    function OrNA(value) {
        return value || 'N/A';
    }

    function buildTopologyMap(topology, siteName) {
        const container = document.getElementById('topology-map');
        if (!container) return;
        container.innerHTML = '';
        if (topology.length === 0) {
            container.textContent = 'No Topology data available.';
            return;
        }
        const seedNodeData = topology[0];
        if (topology.length === 1) {
            const centerX = container.offsetWidth / 2;
            const centerY = container.offsetHeight / 2;
            const seedElement = document.createElement('div');
            seedElement.className = 'topo-node seed';
            seedElement.textContent = seedNodeData.device_name;
            seedElement.style.left = `${centerX}px;`
            seedElement.style.top = `${centerY}px;`
            seedElement.onclick = () => showConfigModal(seedNodeData.device_name, SAD_DATA[siteName].configs[seedNodeData.device_name]);
            container.appendChild(seedElement);
            return;
        }
        const deviceMap = new Map(topology.map(d => [d.device_name, { ...d, element: null, x: 0, y: 0 }]));
        const seedDeviceName = topology[0].device_name;
        const seedNode = deviceMap.get(seedDeviceName);
        const childNodes = topology.slice(1).map(d => deviceMap.get(d.device_name));
        const centerX = container.offsetWidth / 2;
        const centerY = container.offsetHeight / 2;
        // Position the seed device in the center
        seedNode.x = centerX;
        seedNode.y = centerY;
        const seedElement = document.createElement('div');
        seedElement.className = 'topo-node seed';
        seedElement.textContent = seedNodeData.device_name;
        seedElement.style.left = `${seedNode.x}px`;
        seedElement.style.top = `${seedNode.y}px`;
        seedElement.onclick = () => showConfigModal(seedNode.device_name, SAD_DATA[siteName].configs[seedNode.device_name]);
        container.append(seedElement);
        // Use a short timeout to ensure elements are rendered before positioning
        setTimeout(() => {
            const finalCenterX = container.offsetWidth / 2;
            const finalCenterY = container.offsetHeight / 2;
            seedNode.x = finalCenterX; 
            seedNode.y = finalCenterY;
            seedElement.style.left = `${seedNode.x}px`;
            seedElement.style.top = `${seedNode.y}px`;
            // --- Dynamic Radius & Horizontal Stretch ---
            const nodeWidth = 150;
            const desiredSpacing = 50;
            const numChildren = childNodes.length;
            // 1.0 = perfect circle, 1.5 = 50% wider ellipse, 2.0 = twice as wide
            const horizontalStrechFactor = 1.5;
            const requiredCircumference = numChildren * (nodeWidth + desiredSpacing);
            let baseRadius = requiredCircumference / (2 * Math.PI);
            const minRadius = 150;
            const maxRadiusX = (finalCenterX - nodeWidth) / horizontalStrechFactor;
            const maxRadiusY = finalCenterY - 50;
            baseRadius = Math.max(minRadius, Math.min(baseRadius, maxRadiusX, maxRadiusY));
            const radiusY = baseRadius;
            const radiusX = baseRadius * horizontalStrechFactor;
            const angleStep = (2 * Math.PI) / numChildren;
            childNodes.forEach((node, index) => {
                const angle = angleStep * index;
                node.x = finalCenterX + radiusX * Math.cos(angle);
                node.y = finalCenterY + radiusY * Math.sin(angle);
                const nodeElement = document.createElement('div');
                nodeElement.className = 'topo-node';
                nodeElement.textContent = node.device_name;
                nodeElement.style.left = `${node.x}px`;
                nodeElement.style.top = `${node.y}px`;
                container.appendChild(nodeElement);
                const line = createLine(seedNode.x, seedNode.y, node.x, node.y);
                container.appendChild(line);
            });
        }, 50);
    }

    function createLine(x1, y1, x2, y2) {
        const length = Math.sqrt(Math.pow(x2 - x1, 2) + Math.pow(y2 - y1, 2));
        const angle = Math.atan2(y2 - y1, x2 - x1) * 180 / Math.PI;
        const line = document.createElement('div');
        line.className = 'topo-line';
        line.style.left = `${x1}px`;
        line.style.top = `${y1}px`;
        line.style.width = `${length}px`;
        line.style.transform = `rotate(${angle}deg)`;
        line.style.transformOrigin = '0 0';
        return line;
    }

    function buildIndexPage() {
        document.getElementById('page-title').textContent = 'SAD - Main Dashboard';
        const grid = document.getElementById('site-grid');
        if (!grid) return;
        grid.innerHTML = '';
        const siteNames = Object.keys(SAD_DATA).sort();
        siteNames.forEach(siteName => {
            const site = SAD_DATA[siteName];
            const card = document.createElement('div');
            card.className = 'site-card';
            card.innerHTML = `
                <h2>${site.site_name}</h2>
                <ul>
                    <li><strong>Network Devices:</strong> ${site.topology.length}</li>
                    <li><strong>VTCs Found:</strong> ${site.vtcs.length}</li>
                    <li><strong>Last Run:</strong> ${new Date(RUN_TIMESTAMP).toLocaleDateString()}</li>
                </ul>
                <a href="${siteName}.html" class="details-link">View Details</a>
            `;
            grid.appendChild(card);
        });
    }

    function buildSitePage(siteName) {
        const siteData = SAD_DATA[siteName];
        if (!siteData) {
            document.body.innerHTML = '<h1>Error: Site data not found.</h1><a href="index.html">Back to Dashboard</a>';
            return;
        }
        document.getElementById('page-title').textContent = `SAD - ${siteData.site_name}`;
        const backButton = document.getElementById('back-button-link');
        backButton.href = 'index.html';
        backButton.style.display = 'inline-block';
        // Build all tables and visuals for the site page
        buildDeviceTable(siteData.topology, siteData.configs);
        buildVtcTable(siteData.vtcs);
        buildArpTable(siteData.arp_table);
        buildTopologyMap(siteData.topology, siteName);
        setupCollapsibles();
    }

    function setupThemeToggle() {
        const toggle = document.getElementById('theme-toggle-button');
        const currentTheme = localStorage.getItem('theme');
        if (currentTheme === 'dark') {
            document.body.classList.add('dark-mode');
            toggle.textContent = 'SUN';
        } else {
            toggle.textContent = 'MOON';
        }
        toggle.addEventListener('click', () => {
            document.body.classList.toggle('dark-mode');
            let theme = 'light';
            if (document.body.classList.contains('dark-mode')) {
                theme = 'dark';
                toggle.textContent = 'SUN';
            } else {
                toggle.textContent = 'MOON';
            }
            localStorage.setItem('theme', theme);
        })
    }

    function setupCollapsibles() {
        document.querySelectorAll('.section-header').forEach(header => {
            const content = header.nextElementSibling;
            // Check if it should start collapsed
            if (header.classList.contains('collapsed')) {
                content.style.maxHeight = '0px';
            } else {
                content.style.maxHeight = content.scrollHeight + "px";
            }
            header.addEventListener('click', () => {
                header.classList.toggle('collapsed');
                content.classList.toggle('collapsed');
                if (content.style.maxHeight === '0px') {
                    // If it's already collapsed, expand it to its full natural height
                    content.style.maxHeight = content.scrollHeight + "px";
                } else {
                    // If it's expanded, collapse it back to zero
                    content.style.maxHeight = '0px';
                }
            });
        });
    }

    function buildDeviceTable(devices, configs) {
        const tbody = document.getElementById('device-table-body');
        if (!tbody) return;
        tbody.innerHTML = '';
        devices.forEach(dev => {
            const row = tbody.insertRow();
            row.innerHTML = `
                <td><a href="#" class="config-link" data-device-name="${dev.device_name}">${dev.device_name}</a></td>
                <td>${dev.ip}</td>
                <td>${OrNA(dev.platform)}</td>
            `;
        });
        document.querySelectorAll('.config-link').forEach(link => {
            link.addEventListener('click', e => {
                e.preventDefault();
                const deviceName = e.target.dataset.deviceName;
                const configText = configs[deviceName] || 'Configuration not found for this device.';
                showConfigModal(deviceName, configText);
            });
        });
    }

    function showConfigModal(deviceName, configText) {
        document.getElementById('modal-device-name').textContent = `Running Config: ${deviceName}`;
        document.getElementById('modal-config-text').textContent = configText;
        document.getElementById('config-modal').style.display = 'block';
    }

    function buildVtcTable(vtcs) {
        const tbody = document.getElementById('vtc-table-body')
        if (!tbody) return;
        tbody.innerHTML = '';
        vtcs.forEach(vtc => {
            const row = tbody.insertRow();
            row.innerHTML = `
                <td>${OrNA(vtc.device_name)}</td>
                <td>${OrNA(vtc.description)}</td>
                <td>${OrNA(vtc.phone_number)}</td>
                <td>${OrNA(vtc.ip_address)}</td>
            `;
        });
    }

    function buildArpTable(arpTable) {
        const tbody = document.getElementById('arp-table-body');
        if (!tbody) return;
        tbody.innerHTML = '';
        for (const [ip, details] of Object.entries(arpTable)) {
            const row = tbody.insertRow();
            row.innerHTML = `
                <td>${ip}</td>
                <td>${details.mac_address}</td>
                <td>${details.interface}</td>
            `;
        }
    }
    """

def _generate_html_files(all_sites_data):
    # Generates index.html and all site-specific detail pages
    base_html_structure = """
    <!DOCTYPE html>
    <html lang="en">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>SAD Dashboard</title>
            <link rel="stylesheet" href="style.css">
        </head>
        <body>
            <div class="header">
                <h1 id="page-title">SAD Dashboard</h1>
                <div class="header-right">
                    <a id="back-button-link" href="#" style="display:none; margin-right: 20px;"><- Back to Dashboard</a>
                    <button id="theme-toggle-button" title="Toggle Light/Dark Mode">MOON</button>
                </div>
            </div>
            <div class="main-container">
                {body_content}
            </div>
            <div id="config-modal" class="modal">
                <div class="modal-content">
                    <span class="close-button" onclick="document.getElementById('config-modal').style.display='none'">x</span>
                    <h2 id="modal-device-name"></h2>
                    <pre id="modal-config-text"></pre>
                </div>
            </div>
            <script src="data.js"></script>
            <script src="dashboard.js"></script>
        </body>
    </html>
    """

    # Create index.html
    index_content = base_html_structure.format(body_content='div id="site-grid" class="site-grid"></div>')
    with open(f"{DASHBOARD_DIR}index.html", 'w', encoding='utf-8') as f:
        f.write(index_content)

    # Create detail pages for each site
    site_page_body = """
    <div class="section">
        <h2 class="section-header expanded-by-default">Network Topology</h2>
        <div class="section-content">
            <div id="topology-map" class="topology-map"></div>
        </div>
    </div>
    <div class="section">
        <h2 class="section-header expanded-by-default">Network Devices</h2>
        <div class="section-content"><table><thead><tr><th>Hostname</th><th>IP Address</th><th>Platform</th></tr></thead><tbody id="device-table-body"></tbody></table></div>
    </div>
    <div class="section">
        <h2 class="section-header expanded-by-default">VTCs</h2>
        <div class="section-content"><table><thead><tr><th>Device Name</th><th>Description</th><th>Phone Number</th><th>IP Address</th></tr></thead><tbody id="vtc-table-body"></tbody></table></div>
    </div>
    <div class="section">
        <h2 class="section-header collapsed">ARP Table</h2>
        <div class="section-content collapsed"><table><thead><tr><th>IP Address</th><th>MAC Address</th><th>Interface</th></tr></thead><tbody id="arp-table-body"></tbody></table></div>
    </div>
    """
    for site_name in all_sites_data.keys():
        site_page_content = base_html_structure.format(body_content=site_page_body)
        with open(f"{DASHBOARD_DIR}{site_name}.html", 'w', encoding='utf-8') as f:
            f.write(site_page_content)

def generate_dashboard(sites_to_process: list):
    # Main function for this tool. Gathers all data and generates teh static dashboard
    print("\n--- DASHBOARD GENERATOR ---")
    os.makedirs(DASHBOARD_DIR, exist_ok=True)

    all_sites_data = {}
    for site in sites_to_process:
        print(f"  -> Compiling data for site: {site}")
        all_sites_data[site] = _gather_site_data(site)
    
    run_timestamp = datetime.datetime.now().isoformat()

    # Generate data.js
    data_js_content = f"const SAD_DATA = {json.dumps(all_sites_data, indent=2)};\n"
    data_js_content += f"const RUN_TIMESTAMP = '{run_timestamp}';"
    with open(f"{DASHBOARD_DIR}data.js", 'w', encoding='utf-8') as f:
        f.write(data_js_content)
    
    # Generate style.css
    with open(f"{DASHBOARD_DIR}style.css", 'w', encoding='utf-8') as f:
        f.write(_generate_css())

    # Generate dashboard.js
    with open(f"{DASHBOARD_DIR}dashboard.js", 'w', encoding='utf-8') as f:
        f.write(_generate_js())
    
    # Generate all HTML files
    _generate_html_files(all_sites_data)

    print("Success: Dashboard files generated in the 'dashboard' directory.")
