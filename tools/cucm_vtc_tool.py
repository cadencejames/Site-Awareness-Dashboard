import requests
import base64
from lxml import etree

# Disable warnings for self-signed certificates on CUCM
requests.packages.urllib3.disable_warnings(
    requests.packages.urllib3.exceptions.InsecureRequestWarning
)

AXL_VERSION = "14.0"
SOAP_TEMPLATE = """
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:ns="http://www.cisco.com/AXL/API/{version}">
   <soapenv:Header/>
   <soapenv:Body>
      <ns:executeSQLQuery>
         <sql>{sql_query}</sql>
      </ns:executeSQLQuery>
   </soapenv:Body>
</soapenv:Envelope>
"""
SQL_QUERY = """
    SELECT d.name AS device_name,
           d.description AS device_description,
           tp.name AS model_phone,
           n.dnorpattern AS phone_number
    FROM device d
    JOIN devicenumplanmap dmap ON d.pkid = dmap.fkdevice
    JOIN numplan n ON dmap.fknumplan = n.pkid
    JOIN typeproduct tp ON d.tkmodel = tp.tkmodel
    WHERE n.dnorpattern LIKE '5551%'
    ORDER BY n.dnorpattern
"""

def get_vtc_devices(cucm_host: str, username: str, password: str) -> list | None:
    cucm_url = f"https://{cucm_host}:8443/axl/"
    auth_string = f"{username}:{password}"
    auth_token = base64.b64encode(auth_string.encode('utf-8')).decode('ascii')
    headers = {'Authorization': f'Basic {auth_token}', 'Content-Type': 'text/xml', 'SOAPAction': f'CUCM:DB ver={AXL_VERSION} executeSQLQuery'}
    payload = SOAP_TEMPLATE.format(version=AXL_VERSION, sql_query=SQL_QUERY)
    
    print(f"--- [CUCM] Querying {cucm_host} for VTC devices... ---")
    try:
        response = requests.post(cucm_url, headers=headers, data=payload.encode('utf-8'), verify=False, timeout=20)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"--- [CUCM] Error: AXL request failed: {e} ---")
        return None

    try:
        root = etree.fromstring(response.content)
        fault_string = root.findtext('.//faultstring')
        if fault_string:
            print(f"--- [CUCM] Error: AXL API returned a fault: {fault_string} ---")
            return None
        ns = {'axl': f'http://www.cisco.com/AXL/API/{AXL_VERSION}'}
        rows = root.xpath("//row", namespaces=ns)
        devices = [{'device_name': r.findtext('device_name', 'N/A'), 'description': r.findtext('device_description', 'N/A'), 'model': r.findtext('model_phone', 'N/A'), 'phone_number': r.findtext('phone_number', 'N/A')} for r in rows]
        print(f"--- [CUCM] Found {len(devices)} VTC devices. ---")
        return devices
    except etree.XMLSyntaxError:
        print("--- [CUCM] Error: Failed to parse AXL XML response. ---")
        return None
