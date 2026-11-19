"""
cucm_enrich.py - Enriches SAD's phone/VTC clients with CUCM/RIS data.

Usage (run from the project root):
    python3 utilities/cucm_enrich.py

Reads every client SAD has tagged 'cisco_phone' or 'vtc' (see
orchestrator.py's _classify_sep_device_type()) directly from the
clients table - no CSV involved anywhere - queries CUCM's RIS API and
each device's own local HTTP/xAPI, and writes the results into the
phone_enrichment table (see db.py's schema comment for the full
reasoning behind that table's shape).

Run this whenever you want fresher CUCM-side data, not on any fixed
schedule - once a device has an existing model+serial (or has already
been attempted and genuinely couldn't be reached), later runs skip the
slow per-device HTTP/xAPI work for it automatically. RIS's own IP/
registration status ARE refreshed for every tracked device on every
run regardless, since status can genuinely change over time.

Requires: requests
    pip install requests --break-system-packages

Reads two credential types from your SAD credential store (see
credential_manager.py) before doing anything else: 'cucm' (RIS access)
and 'vtc' (local admin login used for each VTC's own xAPI). Add both
via credential_manager.py's (A)dd option if you haven't already -
each just needs a username and a password field.

The 'cucm' account also needs the "Standard AXL API Access" role: one
bulk AXL query per run (see _fetch_axl_descriptions()) reads CUCM's
Description field for every phone/VTC and stores it alongside the RIS
data. RIS itself has no description field. If AXL fails (no role, wrong
AXL_VERSION, CUCM unreachable) the run says so and carries on without
touching any description already stored.

Configuration: set CUCM_HOST (and, if needed, AXL_VERSION) below before running.
"""

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import xml.etree.ElementTree as ET

import requests
from requests.auth import HTTPBasicAuth
import urllib3

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db  # noqa: E402
import mac_parser  # noqa: E402
import credential_loader  # noqa: E402

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------
CUCM_HOST = "cucm-publisher.example.com"  # <-- set your CUCM Publisher hostname or IP here

AXL_VERSION = "12.5"                      # AXL schema version to ask for; set it to your CUCM's major.minor (e.g. "14.0")
AXL_PAGE_SIZE = 500                       # phones per listPhone call (AXL refuses a single answer of more than 1000 rows)
AXL_PACE_DELAY = 1.0                      # seconds between AXL pages, to stay well under CUCM's AXL throttle
AXL_THROTTLE_WAIT = 30                    # seconds to wait before retrying a throttled (HTTP 503) page
AXL_THROTTLE_TRIES = 3

RIS_BATCH_SIZE = 500                      # devices per RIS call (Cisco-documented safe max)
RIS_NAMESPACE = {"ns1": "http://schemas.cisco.com/ast/soap"}
RIS_SOAP_NS = "http://schemas.cisco.com/ast/soap"
RIS_PACE_DELAY = 4.5                      # seconds between RIS calls (~13/min, under the 15/min default limit)

DEVICE_HTTP_TIMEOUT = 4                   # seconds, per-device web/xAPI query
DEVICE_HTTP_WORKERS = 10                  # concurrent threads for the per-device pull phase

VERIFY_TLS = False                        # CUCM/device self-signed certs are common; set True if you have valid certs


# ---------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------
def _get_credential(creds: dict, cred_type: str) -> tuple:
    """Pulls username/password out of the credential store's given
    type (e.g. "cucm" or "vtc") - same shape and same clear-error
    philosophy as orchestrator.py's _get_tacacs_credentials(), just
    parameterized since this script needs two different credential
    types rather than the one reserved "tacacs" type.
    """
    entry = creds.get(cred_type)
    if not entry:
        raise ValueError(
            f"No '{cred_type}' credential found in your credential store. "
            f"Run credential_manager.py and add a '{cred_type}' credential (username + password) first."
        )
    username = entry.get("username", {}).get("value")
    password = entry.get("password", {}).get("value")
    if not username or not password:
        raise ValueError(
            f"Your '{cred_type}' credential is missing a username or password. "
            f"Run credential_manager.py and use (U)pdate to fix it."
        )
    return username, password


# ---------------------------------------------------------------------
# SEP device name construction
# ---------------------------------------------------------------------
def _build_sep_name(mac: str) -> str:
    """CUCM device names for SEP-registered devices are "SEP" followed
    by the 12-hex-digit MAC, uppercase, no separators. Built from
    normalize_mac() rather than trusting whatever format/case a
    network device happened to report - CUCM's own device-name lookup
    is not confirmed to be format/case-forgiving the way SAD's own
    internal MAC matching is everywhere else, so this always
    constructs the canonical form itself.
    """
    return "SEP" + mac_parser.normalize_mac(mac).upper()


def _chunked(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


# ---------------------------------------------------------------------
# RIS (ported from the proven, real cucm_serial_pull.py - this logic
# already works against real CUCM, so its internals are kept as-is
# rather than rewritten)
# ---------------------------------------------------------------------
def _build_ris_request(device_names):
    items_xml = "".join(
        f"<soap:item><soap:Item>{name}</soap:Item></soap:item>"
        for name in device_names
    )
    return f"""<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:soap="{RIS_SOAP_NS}">
  <soapenv:Header/>
  <soapenv:Body>
    <soap:selectCmDeviceExt>
      <soap:StateInfo></soap:StateInfo>
      <soap:CmSelectionCriteria>
        <soap:MaxReturnedDevices>1000</soap:MaxReturnedDevices>
        <soap:DeviceClass>Any</soap:DeviceClass>
        <soap:Model>255</soap:Model>
        <soap:Status>Any</soap:Status>
        <soap:NodeName></soap:NodeName>
        <soap:SelectBy>Name</soap:SelectBy>
        <soap:SelectItems>{items_xml}</soap:SelectItems>
        <soap:Protocol>Any</soap:Protocol>
        <soap:DownloadStatus>Any</soap:DownloadStatus>
      </soap:CmSelectionCriteria>
    </soap:selectCmDeviceExt>
  </soapenv:Body>
</soapenv:Envelope>"""


def _parse_phone_numbers(device_el):
    """RIS's structured per-line data lives in "LinesStatus" (plural,
    confirmed against his real RIS output - an earlier version of this
    read "LineStatus", singular, which was simply a typo and never
    matched anything at any batch size). One item per line, each with
    its own clean DirectoryNumber - present even for a device with
    just one line. Multiple numbers per phone is common (his direct
    confirmation), so this returns every one found, joined for
    display, not just the first. Returns None if the device has no
    lines at all (e.g. a VTC).
    """
    numbers = []
    lines_status = device_el.find("ns1:LinesStatus", RIS_NAMESPACE)
    if lines_status is not None:
        items = lines_status.findall("ns1:item", RIS_NAMESPACE)
        if not items:
            items = lines_status.findall("ns1:Item", RIS_NAMESPACE)
        for item in items:
            dn_el = item.find("ns1:DirectoryNumber", RIS_NAMESPACE)
            if dn_el is not None and dn_el.text:
                numbers.append(dn_el.text.strip())
    return ", ".join(numbers) if numbers else None


def _parse_ris_response(xml_text):
    """Returns dict: device_name -> {ip, status, phone_number}"""
    results = {}
    root = ET.fromstring(xml_text)

    devices = root.findall(".//ns1:CmDevices/ns1:item", RIS_NAMESPACE)
    if not devices:
        devices = root.findall(".//ns1:CmDevices/ns1:Item", RIS_NAMESPACE)

    for device in devices:
        name_el = device.find("ns1:Name", RIS_NAMESPACE)
        status_el = device.find("ns1:Status", RIS_NAMESPACE)

        name = name_el.text if name_el is not None else None
        if not name:
            continue

        ip_el = device.find(".//ns1:IPAddress/ns1:item/ns1:IP", RIS_NAMESPACE)
        if ip_el is None:
            ip_el = device.find(".//ns1:IPAddress/ns1:Item/ns1:IP", RIS_NAMESPACE)

        results[name] = {
            "ip": ip_el.text if ip_el is not None else None,
            "status": status_el.text if status_el is not None else None,
            "phone_number": _parse_phone_numbers(device),
        }

    return results


def _query_ris_batch(session, cucm_host, auth, device_names):
    url = f"https://{cucm_host}:8443/realtimeservice2/services/RISService70"
    headers = {"Content-Type": "text/xml; charset=utf-8", "SOAPAction": ""}
    body = _build_ris_request(device_names)

    resp = session.post(url, data=body, headers=headers, auth=auth, verify=VERIFY_TLS, timeout=30)
    resp.raise_for_status()

    if "Fault" in resp.text or "faultstring" in resp.text:
        print(f"  WARNING: RIS fault in batch response (first 300 chars): {resp.text[:300]}")
        return {}

    return _parse_ris_response(resp.text)


# ---------------------------------------------------------------------
# AXL (CUCM's configuration API) - the Description field. RIS cannot
# return it. One bulk, paged listPhone for every SEP device, matched to
# SAD's phones/VTCs by device name afterwards.
# ---------------------------------------------------------------------
class AxlError(Exception):
    """AXL could not be read; the message is meant to be shown as is."""


def _local(tag):
    """Element name without its {namespace} part."""
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _build_axl_request(first, skip):
    return f"""<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:ns="http://www.cisco.com/AXL/API/{AXL_VERSION}">
  <soapenv:Header/>
  <soapenv:Body>
    <ns:listPhone>
      <searchCriteria><name>SEP%</name></searchCriteria>
      <returnedTags><name/><description/></returnedTags>
      <skip>{skip}</skip>
      <first>{first}</first>
    </ns:listPhone>
  </soapenv:Body>
</soapenv:Envelope>"""


def _parse_axl_page(xml_text):
    """Returns (descriptions, row_count): device name (uppercase) ->
    description or None. Raises AxlError on a SOAP fault."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise AxlError(f"AXL answered with something that is not XML ({e}).") from None
    for el in root.iter():
        if _local(el.tag) == "faultstring":
            raise AxlError(f"AXL returned a fault: {(el.text or '').strip()[:300]}")
    out, rows = {}, 0
    for phone in root.iter():
        if _local(phone.tag) != "phone":
            continue
        rows += 1
        name = description = None
        for child in phone:
            if _local(child.tag) == "name":
                name = (child.text or "").strip()
            elif _local(child.tag) == "description":
                description = (child.text or "").strip() or None
        if name:
            out[name.upper()] = description
    return out, rows


def _post_axl_page(session, cucm_host, auth, first, skip):
    url = f"https://{cucm_host}:8443/axl/"
    headers = {"Content-Type": "text/xml; charset=utf-8", "SOAPAction": f'"CUCM:DB ver={AXL_VERSION}"'}
    body = _build_axl_request(first, skip)
    for attempt in range(1, AXL_THROTTLE_TRIES + 1):
        resp = session.post(url, data=body, headers=headers, auth=auth, verify=VERIFY_TLS, timeout=60)
        if resp.status_code == 503 and attempt < AXL_THROTTLE_TRIES:
            print(f"    AXL is throttling; waiting {AXL_THROTTLE_WAIT}s (try {attempt}/{AXL_THROTTLE_TRIES})...")
            time.sleep(AXL_THROTTLE_WAIT)
            continue
        break
    if resp.status_code in (401, 403):
        raise AxlError(f"AXL refused the 'cucm' login (HTTP {resp.status_code}). The account needs the "
                       f"'Standard AXL API Access' role, and the username/password must be right.")
    if resp.status_code == 503:
        raise AxlError("AXL is still throttling after several tries; run again in a few minutes.")
    if resp.status_code >= 400 and "faultstring" not in resp.text:
        raise AxlError(f"AXL returned HTTP {resp.status_code}.")
    return _parse_axl_page(resp.text)


def _fetch_axl_descriptions(session, cucm_host, auth):
    """Every SEP device's Description, as {NAME: description or None}.
    Raises AxlError (clear message) when the whole list cannot be read;
    a partial list is never returned, so the caller can trust that a
    device missing from the result is really not in CUCM."""
    result, skip = {}, 0
    while True:
        try:
            page, rows = _post_axl_page(session, cucm_host, auth, AXL_PAGE_SIZE, skip)
        except requests.exceptions.RequestException as e:
            raise AxlError(f"Could not reach AXL on {cucm_host}: {e}") from None
        result.update(page)
        print(f"  AXL page: {rows} phone(s) (running total {len(result)})")
        if rows < AXL_PAGE_SIZE:
            return result
        skip += AXL_PAGE_SIZE
        time.sleep(AXL_PACE_DELAY)


# ---------------------------------------------------------------------
# Per-device HTTP/xAPI (ported from the proven, real script)
# ---------------------------------------------------------------------
def _get_serial_from_phone(ip, timeout=DEVICE_HTTP_TIMEOUT):
    """Query a Cisco IP phone's DeviceInformationX page. Returns
    (serial, model) or (None, None).
    """
    if not ip:
        return None, None
    try:
        resp = requests.get(f"https://{ip}/DeviceInformationX", timeout=timeout, verify=False)
        if resp.status_code != 200:
            return None, None
        root = ET.fromstring(resp.text)
        serial_el = root.find(".//serialNumber")
        model_el = root.find(".//modelNumber")
        serial = serial_el.text.strip() if serial_el is not None and serial_el.text else None
        model = model_el.text.strip() if model_el is not None and model_el.text else None
        return serial, model
    except requests.exceptions.SSLError:
        # Legacy (79xx-era) phones may only support TLS 1.0, which
        # modern OpenSSL blocks - these need a manual lookup.
        return None, None
    except Exception:
        return None, None


def _get_serial_from_vtc(ip, vtc_username, vtc_password, timeout=DEVICE_HTTP_TIMEOUT):
    """Query a RoomOS/CE xAPI device for its serial/model. Returns
    (serial, model) or (None, None).
    """
    if not ip:
        return None, None
    try:
        resp = requests.get(
            f"https://{ip}/getxml?location=/Status/SystemUnit",
            auth=HTTPBasicAuth(vtc_username, vtc_password),
            timeout=timeout, verify=False,
        )
        if resp.status_code != 200:
            return None, None
        root = ET.fromstring(resp.text)
        serial_el = root.find(".//SerialNumber")
        model_el = root.find(".//ProductId")
        serial = serial_el.text.strip() if serial_el is not None and serial_el.text else None
        model = model_el.text.strip() if model_el is not None and model_el.text else None
        return serial, model
    except Exception:
        return None, None


def _pull_serial_and_model(client, ip, vtc_username, vtc_password):
    """Goes straight to the API matching this client's already-known
    device_type (from CDP-based classification - see
    orchestrator.py's _classify_sep_device_type()), rather than trying
    phone-then-VTC blindly the way the original standalone script did
    (which had no prior knowledge of device type). Falls back to the
    other API only if the expected one comes back empty, in case a
    device was ever misclassified.
    """
    if client["device_type"] == "cisco_phone":
        serial, model = _get_serial_from_phone(ip)
        if not serial:
            serial, model = _get_serial_from_vtc(ip, vtc_username, vtc_password)
    else:  # "vtc"
        serial, model = _get_serial_from_vtc(ip, vtc_username, vtc_password)
        if not serial:
            serial, model = _get_serial_from_phone(ip)
    return serial, model


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------
def run(creds):
    """The whole enrichment pass, given an already-unlocked credential
    dict (the GUI's Discovery tab passes the one it unlocked at login;
    main() below gets one from credential_loader for command-line use).
    Raises ValueError if the 'cucm' or 'vtc' credential is missing."""
    print("=== SAD CUCM/VTC Enrichment ===\n")

    cucm_username, cucm_password = _get_credential(creds, "cucm")
    vtc_username, vtc_password = _get_credential(creds, "vtc")
    cucm_auth = HTTPBasicAuth(cucm_username, cucm_password)

    with db.get_conn() as conn:
        all_clients = db.get_all_clients(conn)
        targets = [c for c in all_clients if c["device_type"] in ("cisco_phone", "vtc")]
        print(f"Found {len(targets)} phone/VTC client(s) tracked across all sites.")
        if not targets:
            print("Nothing to enrich.")
            return

        by_device_name = {_build_sep_name(c["mac"]): c for c in targets}
        device_names = list(by_device_name.keys())

        # --- Step 1: RIS bulk query - every device, every run, no skipping ---
        print(f"\nQuerying RIS for {len(device_names)} device(s) (batched)...")
        session = requests.Session()
        ris_results = {}
        batches = list(_chunked(device_names, RIS_BATCH_SIZE))
        for i, batch in enumerate(batches, start=1):
            print(f"  Batch {i}/{len(batches)} ({len(batch)} devices)...")
            try:
                ris_results.update(_query_ris_batch(session, CUCM_HOST, cucm_auth, batch))
            except requests.exceptions.RequestException as e:
                print(f"  ERROR on batch {i}: {e}")
            if i < len(batches):
                time.sleep(RIS_PACE_DELAY)
        print(f"RIS returned data for {len(ris_results)} / {len(device_names)} device(s).")

        for name, client in by_device_name.items():
            info = ris_results.get(name, {})
            db.upsert_phone_enrichment_status(
                conn, client["mac"], name, info.get("ip"), info.get("status"),
                phone_number=info.get("phone_number"),
            )
        # Commit after each phase so this run never holds sad.db's write
        # lock through the slow network steps below (the GUI and its
        # write queue share the same database file).
        conn.commit()

        # --- Step 1b: AXL bulk query for CUCM's Description - every run, no skipping ---
        print("\nQuerying AXL for device descriptions...")
        try:
            axl_results = _fetch_axl_descriptions(session, CUCM_HOST, cucm_auth)
        except AxlError as e:
            axl_results = None
            print(f"  WARNING: descriptions were not updated this run. {e}")
        if axl_results is not None:
            wanted = {client["mac"]: axl_results.get(name) for name, client in by_device_name.items()}
            changed = db.set_phone_descriptions(conn, wanted)
            found = sum(1 for name in by_device_name if axl_results.get(name))
            print(f"AXL had a description for {found} / {len(by_device_name)} device(s); {changed} changed.")
        conn.commit()

        # --- Step 2: per-device serial/model pull - skip anything already attempted ---
        existing_by_mac = {row["mac"]: row for row in db.get_all_phone_enrichment(conn)}
        to_pull = []
        for name, client in by_device_name.items():
            existing = existing_by_mac.get(mac_parser.normalize_mac(client["mac"]))
            if existing is None or existing["last_attempted"] is None:
                to_pull.append((name, client))
        skipped = len(by_device_name) - len(to_pull)
        print(f"\n{len(to_pull)} device(s) need a serial/model pull ({skipped} already attempted previously, skipped).")

        def process(name, client):
            ip = ris_results.get(name, {}).get("ip")
            serial, model = _pull_serial_and_model(client, ip, vtc_username, vtc_password)
            return client["mac"], serial, model

        results = []
        if to_pull:
            print("Querying devices directly for serial/model...")
            with ThreadPoolExecutor(max_workers=DEVICE_HTTP_WORKERS) as executor:
                futures = [executor.submit(process, name, client) for name, client in to_pull]
                done = 0
                for future in as_completed(futures):
                    results.append(future.result())
                    done += 1
                    if done % 100 == 0 or done == len(to_pull):
                        print(f"  Processed {done}/{len(to_pull)}")

        for mac, serial, model in results:
            db.record_phone_serial_pull(conn, mac, model=model, serial_number=serial)

    got_serial = sum(1 for _, serial, _ in results if serial)
    print("\nDone.")
    print(f"  New serial numbers found this run:  {got_serial}")
    print(f"  Still missing (unreachable/legacy): {len(results) - got_serial}")
    print(f"  Skipped (already attempted before): {skipped}")


def main():
    run(credential_loader.prompt_and_load())


if __name__ == "__main__":
    main()