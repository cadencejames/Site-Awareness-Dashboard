# filename: tools/cucm_vtc_tool.py
import requests
import base64
from lxml import etree

requests.packages.urllib3.disable_warnings(requests.packages.urllib3.exceptions.InsecureRequestWarning)

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
SQL_TEMPLATE = """
    SELECT d.name AS device_name,
           d.description AS device_description,
           tp.name AS model_phone,
           n.dnorpattern AS phone_number
    FROM device d
    JOIN devicenumplanmap dmap ON d.pkid = dmap.fkdevice
    JOIN numplan n ON dmap.fknumplan = n.pkid
    JOIN typeproduct tp ON d.tkmodel = tp.tkmodel
    WHERE n.dnorpattern LIKE '{vtc_pattern}'
    ORDER BY n.dnorpattern
"""

def get_vtc_devices(cucm_host: str, username: str, password: str, vtc_phone_pattern: str) -> list | None:
    # Queries CUCM for devices matching a specific phone number pattern.
    cucm_url = f"https://{cucm_host}:8443/axl/"
    auth_string = f"{username}:{password}"
    auth_token = base64.b64encode(auth_string.encode('utf-8')).decode('ascii')
    headers = {'Authorization': f'Basic {auth_token}', 'Content-Type': 'text/xml', 'SOAPAction': f'CUCM:DB ver={AXL_VERSION} executeSQLQuery'}
    
    final_sql_query = SQL_TEMPLATE.format(vtc_pattern=vtc_phone_pattern)
    payload = SOAP_TEMPLATE.format(version=AXL_VERSION, sql_query=final_sql_query)
    
    print(f"--- [CUCM] Querying {cucm_host} for devices with pattern '{vtc_phone_pattern}'... ---")
    try:
        response = requests.post(cucm_url, headers=headers, data=payload.encode('utf-8'), verify=False, timeout=30)
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
        print(f"--- [CUCM] Found {len(devices)} matching devices. ---")
        return devices
    except etree.XMLSyntaxError:
        print("--- [CUCM] Error: Failed to parse AXL XML response. ---")
        return None
