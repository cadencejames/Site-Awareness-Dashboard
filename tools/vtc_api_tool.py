import requests
from lxml import etree

# Disable warnings for self-signed certificates
requests.packages.urllib3.disable_warnings(requests.packages.urllib3.exceptions.InsecureRequestWarning)

def find_value(element, default='N/A'):
    """
    A robust helper to find a value within an XML element.
    Some older APIs use <TagName><Value>123</Value></TagName>
    Newer ones might use <TagName>123</TagName>
    This function handles both cases gracefully.
    """
    if element is None:
        return default
    value_child = element.find('Value')
    if value_child is not None and value_child.text is not None:
        return value_child.text
    elif element.text is not None:
        return element.text
    else:
        return default

def get_device_status(device_ip: str, username: str, password: str) -> dict | None:
    # Connects to a VTC device via its HTTP GET API and retrieves status info
    url = f"https://{device_ip}/status.xml"
    auth = (username, password)
    try:
        # Make one single GET request to fetch the entire status document
        response = requests.get(url, auth=auth,  verify=False, timeout=15)
        response.raise_for_status()
        # Parse the entire XML response at once
        root = etree.fromstring(response.content)
        # Use precise XPath to find each element we care about.
        status_data = {
            'uptime_seconds': find_value(root.find('./SystemUnit/Uptime')),
            'software_version': find_value(root.find('./SystemUnit/Software/Version')),
            'software_release_date': find_value(root.find('./SystemUnit/Software/ReleaseDate')),
            # Note: Call status might be in a different top-level tag
            'active_calls': find_value(root.find('./Call/NumberOfActiveCalls')),
            'in_progress_calls': find_value(root.find('./Call/NumberOfInProgressCalls')),
            'system_name': find_value(root.find('./SystemUnit/Name')),
        }        
        return status_data

    except requests.exceptions.RequestException:
        # This catches connection errors, timeouts, auth failures, etc. Indicates we could not talk to the device
        return None
    except etree.XMLSyntaxError:
        # The device sent back something that wasn't valid XML.
        return {'error': 'Invalid XML response from device'}
