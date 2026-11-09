"""
orion_client.py - read-only client for the SolarWinds Orion Information
Service (SWIS) REST API. Standard library only.

It only ever SENDS queries (SWQL SELECTs) and hands back raw rows or rows run
through orion_parser. It never writes to Orion and never touches SAD's
database. The caller supplies the login; nothing here stores it anywhere but
in memory for the life of the client object, and it is never put into an
error message, a repr or a log line.

Paging: SWQL result sets are fetched in chunks with "keyset" paging
(TOP n ... WHERE id > @last ORDER BY id), which only needs plain SWQL features
and cannot skip or repeat rows if events arrive while a pull is running.

All times are the Orion server's own clock (see orion_parser).
"""
import base64
import http.client
import json
import re
import socket
import ssl
import urllib.error
import urllib.request

import orion_parser as op

DEFAULT_PORT = 17774
QUERY_PATH = "/SolarWinds/InformationService/v3/Json/Query"
USER_AGENT = "SAD-Orion/1.0"
MAX_PAGES = 2000  # safety stop for a runaway pager (2000 pages x page_size rows)

# Node properties we try to read. The required ones must exist; the optional
# ones are probed once and quietly left out on an Orion version that lacks them.
NODE_REQUIRED = ("NodeID", "Caption", "Status")
NODE_IP_CANDIDATES = ("IPAddress", "IP_Address")
EVENT_SAMPLE_FIELDS = ("EventID", "EventTime", "EventType", "NetworkNode", "NetObjectID", "NetObjectType")
NODE_OPTIONAL = ("DNS", "SysName", "StatusDescription", "Vendor", "MachineType",
                 "PollInterval", "Unmanaged", "UnManageFrom", "UnManageUntil")


class OrionError(Exception):
    """Base class for everything this module raises."""


class OrionAuthError(OrionError):
    """Orion refused the login."""


class OrionConnectionError(OrionError):
    """Could not reach Orion, TLS failed, or the request timed out."""


class OrionQueryError(OrionError):
    """Orion answered, but with an error for this query."""

    def __init__(self, status, detail):
        OrionError.__init__(self, f"Orion returned HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


class OrionClient:
    def __init__(self, host, username, password, port=DEFAULT_PORT, verify_tls=False,
                 timeout=120, scheme="https", page_size=2000, use_system_proxy=False):
        if not host:
            raise OrionError("No Orion host given.")
        self.host = host
        self.port = int(port)
        self.username = username
        self.verify_tls = bool(verify_tls)
        self.timeout = timeout
        self.scheme = scheme
        self.page_size = int(page_size)
        raw = f"{username}:{password}".encode("utf-8")
        self._auth = "Basic " + base64.b64encode(raw).decode("ascii")
        self._url = f"{scheme}://{host}:{self.port}{QUERY_PATH}"
        handlers = []
        if not use_system_proxy:
            # Orion is an internal host; a stray system proxy setting must not intercept it.
            handlers.append(urllib.request.ProxyHandler({}))
        if scheme == "https":
            ctx = ssl.create_default_context()
            if not self.verify_tls:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            handlers.append(urllib.request.HTTPSHandler(context=ctx))
        self._opener = urllib.request.build_opener(*handlers)
        self._node_fields = None
        self.last_event_queries = []
        self.last_event_duplicates = 0

    def __repr__(self):
        return f"<OrionClient {self.scheme}://{self.host}:{self.port} as {self.username}>"

    # ------------------------------------------------------------------
    # One query
    # ------------------------------------------------------------------
    def query(self, swql, parameters=None):
        """Run one SWQL query and return the list of row dicts."""
        body = {"query": swql}
        if parameters:
            body["parameters"] = parameters
        request = urllib.request.Request(
            self._url,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": self._auth,
                "User-Agent": USER_AGENT,
            },
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = self._error_detail(exc)
            if exc.code in (401, 403):
                raise OrionAuthError(
                    f"Orion rejected the login (HTTP {exc.code}). Check the username and password, and "
                    "that this account may use the Orion API.")
            raise OrionQueryError(exc.code, detail)
        except urllib.error.URLError as exc:
            raise self._connection_error(exc.reason)
        except (socket.timeout, TimeoutError):
            raise self._connection_error(socket.timeout())
        except (OSError, http.client.HTTPException) as exc:
            raise self._connection_error(exc)

        try:
            payload = json.loads(raw.decode("utf-8"))
            rows = payload["results"]
        except (ValueError, KeyError, TypeError, UnicodeDecodeError):
            raise OrionError("Orion sent a response that is not the expected JSON (no 'results' list).")
        if not isinstance(rows, list):
            raise OrionError("Orion sent a response whose 'results' is not a list.")
        return rows

    @staticmethod
    def _error_detail(exc):
        try:
            text = exc.read(4000).decode("utf-8", "replace")
        except Exception:
            text = ""
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict) and parsed.get("Message"):
                text = str(parsed["Message"])
        except ValueError:
            pass
        text = " ".join(text.split())
        return text[:300] if text else (exc.reason or "no detail given")

    def _connection_error(self, reason):
        where = f"{self.host}:{self.port}"
        text = str(reason)
        if isinstance(reason, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in text:
            return OrionConnectionError(
                f"Orion's TLS certificate was not accepted ({where}). If it is self-signed, set "
                "verify_tls to false for this host.")
        if isinstance(reason, (socket.timeout, TimeoutError)) or "timed out" in text:
            return OrionConnectionError(f"Timed out after {self.timeout} s talking to {where}.")
        if isinstance(reason, ConnectionRefusedError):
            return OrionConnectionError(f"Connection refused by {where} (wrong host or port, or SWIS is not running).")
        if isinstance(reason, socket.gaierror):
            return OrionConnectionError(f"Could not resolve the host name {self.host}.")
        return OrionConnectionError(f"Could not reach {where}: {text or reason.__class__.__name__}")

    # ------------------------------------------------------------------
    # Paging
    # ------------------------------------------------------------------
    def _keyset(self, swql, key):
        """Run a 'TOP n ... WHERE <key> > @last ORDER BY <key>' query page by
        page and return every row. Stops if a page fails to move forward
        rather than looping forever."""
        last, rows_out = 0, []
        for _ in range(MAX_PAGES):
            rows = self.query(swql, {"last": last})
            rows_out.extend(rows)
            if len(rows) < self.page_size:
                return rows_out
            new_last = op._to_int(rows[-1].get(key))
            if new_last is None or new_last <= last:
                raise OrionError(f"Paging stopped moving forward at {key}={rows[-1].get(key)!r}; refusing to loop.")
            last = new_last
        raise OrionError(f"More than {MAX_PAGES} pages; refusing to continue.")

    # ------------------------------------------------------------------
    # Specific questions
    # ------------------------------------------------------------------
    def server_now(self):
        """The Orion server's clock right now (naive datetime, server-local)."""
        rows = self.query("SELECT TOP 1 GETDATE() AS Now FROM Orion.Nodes")
        if not rows or "Now" not in rows[0]:
            raise OrionError("Orion did not return its current time.")
        return op.parse_time(rows[0]["Now"])

    def node_fields(self):
        """(available, missing) lists of the node properties we use. Probed once
        per client, one tiny query per optional property."""
        if self._node_fields is None:
            available, missing = [], []
            ip_field = None
            for name in NODE_IP_CANDIDATES:
                if self._field_exists(name):
                    ip_field = name
                    break
            for name in NODE_REQUIRED:
                if not self._field_exists(name):
                    raise OrionError(f"This Orion version has no Orion.Nodes.{name} property.")
                available.append(name)
            if ip_field is None:
                raise OrionError(f"This Orion version has no node IP address property (tried {', '.join(NODE_IP_CANDIDATES)}).")
            available.append(ip_field)
            missing.extend(n for n in NODE_IP_CANDIDATES if n != ip_field)
            for name in NODE_OPTIONAL:
                (available if self._field_exists(name) else missing).append(name)
            self._node_fields = (available, missing)
        return list(self._node_fields[0]), list(self._node_fields[1])

    def _field_exists(self, name):
        try:
            self.query(f"SELECT TOP 1 n.{name} FROM Orion.Nodes n")
            return True
        except OrionQueryError as exc:
            if exc.status == 400:
                return False
            raise

    def fetch_nodes(self):
        """Every node, normalised. Returns (nodes, parse_errors)."""
        available, _ = self.node_fields()
        select = ", ".join(f"n.{f}" for f in available)
        rows = self._keyset(
            f"SELECT TOP {self.page_size} {select} FROM Orion.Nodes n WHERE n.NodeID > @last ORDER BY n.NodeID",
            "NodeID")
        return op.normalize_rows(rows, op.normalize_node)

    def fetch_events(self, days, event_types=(op.EVENT_DOWN, op.EVENT_UP), node_ids=None):
        """Node events of the given types from the last `days` days (by the Orion
        server's clock), normalised and sorted by time then EventID. `node_ids`,
        if given, filters client-side. Returns (events, parse_errors).

        Each type is requested with its own query. A single "EventType IN (1, 5)"
        query was seen returning only the first type on a real server, so the
        types are fetched separately and merged here."""
        days = int(days)
        if days < 1 or days > 3650:
            raise OrionError("days must be between 1 and 3650.")
        events, errors, seen_ids = [], [], set()
        self.last_event_queries = []     # what was really sent, for diagnosing odd results
        duplicates = 0
        for etype in dict.fromkeys(int(t) for t in event_types):
            swql = (
                f"SELECT TOP {self.page_size} e.EventID, e.EventTime, e.EventType, e.NetworkNode FROM Orion.Events e "
                f"WHERE e.NetObjectType = 'N' AND e.EventType = {etype} "
                f"AND e.EventTime >= ADDDAY(-{days}, GETDATE()) AND e.EventID > @last ORDER BY e.EventID")
            rows = self._keyset(swql, "EventID")
            received = sorted({op._to_int(r.get("EventType")) for r in rows if op._to_int(r.get("EventType")) is not None})
            self.last_event_queries.append({"clause": re.search(r"e\.EventType = \d+", swql).group(0),
                                            "rows": len(rows), "types_received": received})
            wrong = [t for t in received if t != etype]
            if wrong:
                raise OrionError(f"Asked Orion for event type {etype} but it returned type(s) {wrong}; refusing to continue.")
            items, errs = op.normalize_rows(rows, op.normalize_event)
            errors.extend(errs)
            for item in items:
                if item["event_id"] is not None and item["event_id"] in seen_ids:
                    duplicates += 1
                    continue
                seen_ids.add(item["event_id"])
                events.append(item)
        self.last_event_duplicates = duplicates
        events.sort(key=op.event_sort_key)
        if node_ids is not None:
            wanted = set(node_ids)
            events = [e for e in events if e["node_id"] in wanted]
        return events, errors

    def sample_events(self, event_type, limit=3):
        """The newest few raw node events of one type, exactly as Orion sent them
        (no normalising). For diagnosing odd results."""
        last_error = None
        for names in (EVENT_SAMPLE_FIELDS, EVENT_SAMPLE_FIELDS[:4]):
            fields = ", ".join(f"e.{name}" for name in names)
            try:
                return self.query(
                    f"SELECT TOP {int(limit)} {fields} FROM Orion.Events e "
                    f"WHERE e.NetObjectType = 'N' AND e.EventType = {int(event_type)} ORDER BY e.EventID DESC")
            except OrionQueryError as exc:
                last_error = exc
        raise last_error

    def node_status_counts(self):
        """{status: number of nodes}."""
        rows = self.query("SELECT n.Status, COUNT(*) AS Cnt FROM Orion.Nodes n GROUP BY n.Status")
        return self._counts(rows, "Status")

    def event_type_counts(self, days=None):
        """{event type: number of node events}, over the last `days` days or, with
        no argument, over everything Orion still holds."""
        window = ""
        if days is not None:
            window = f" AND e.EventTime >= ADDDAY(-{int(days)}, GETDATE())"
        rows = self.query(
            "SELECT e.EventType, COUNT(*) AS Cnt FROM Orion.Events e "
            f"WHERE e.NetObjectType = 'N'{window} GROUP BY e.EventType")
        return self._counts(rows, "EventType")

    @staticmethod
    def _counts(rows, key):
        out = {}
        for row in rows:
            k, c = op._to_int(row.get(key)), op._to_int(row.get("Cnt"))
            if k is not None and c is not None:
                out[k] = c
        return out

    def event_span(self):
        """{'earliest', 'latest', 'total'} for all node events Orion still holds
        (earliest/latest are None when there are none). This is the retention check."""
        rows = self.query(
            "SELECT MIN(e.EventTime) AS Earliest, MAX(e.EventTime) AS Latest, COUNT(*) AS Total "
            "FROM Orion.Events e WHERE e.NetObjectType = 'N'")
        row = rows[0] if rows else {}
        total = op._to_int(row.get("Total")) or 0

        def when(key):
            value = row.get(key)
            return op.parse_time(value) if value not in (None, "") else None
        return {"earliest": when("Earliest"), "latest": when("Latest"), "total": total}

    def event_type_names(self):
        """{event type id: name} from Orion.EventTypes."""
        rows = self.query("SELECT et.EventType, et.Name FROM Orion.EventTypes et")
        return {op._to_int(r.get("EventType")): op._to_str(r.get("Name"))
                for r in rows if op._to_int(r.get("EventType")) is not None}

    def status_names(self):
        """{status id: name} from Orion.StatusInfo."""
        rows = self.query("SELECT s.StatusId, s.StatusName FROM Orion.StatusInfo s")
        return {op._to_int(r.get("StatusId")): op._to_str(r.get("StatusName"))
                for r in rows if op._to_int(r.get("StatusId")) is not None}