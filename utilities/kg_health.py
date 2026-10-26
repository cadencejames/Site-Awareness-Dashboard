"""kg_health.py - derives a KG (TACLANE) device's health from its stored
fields and the thresholds in settings.json. Pure functions: nothing here
touches the database, so the rules can be tested and changed in isolation.

Health values, worst first: "critical", "warning", "unknown", "ok" (and the
overall result is the worst of the individual checks - see evaluate()).

  state            looked up in settings gemone.state_health (case ignored);
                   a blank or "unknown" state is "unknown"; any other state
                   that is not listed is "critical"
  KEYMAT expiry    critical when days left <= keymat_critical_days (0 = the
                   expiry day itself or later), warning when days left <=
                   keymat_warn_days
  battery age      warning at battery_warn_years since the last change,
                   critical at battery_critical_years if one is set
  interfaces       a plaintext or ciphertext side reporting "down" gets
                   interface_down_health
  associations     unreachable pairs only affect health when
                   unreachable_pairs_warn is true
"""
import datetime

_RANK = {"ok": 0, "unknown": 1, "warning": 2, "critical": 3}
_VALID = set(_RANK)


def parse_date(text):
    """A YYYY-MM-DD date (extra text after it is ignored) or None."""
    if not text:
        return None
    try:
        return datetime.date.fromisoformat(str(text).strip()[:10])
    except ValueError:
        return None


def _add_years(d: datetime.date, years: float) -> datetime.date:
    """d plus a number of calendar years (a fraction counts as days), so
    "2 years after 2024-10-23" is exactly 2026-10-23."""
    whole = int(years)
    try:
        base = d.replace(year=d.year + whole)
    except ValueError:               # Feb 29 in a year with no Feb 29
        base = d.replace(year=d.year + whole, day=28)
    return base + datetime.timedelta(days=round((years - whole) * 365.25))


def _worse(a: str, b: str) -> str:
    return a if _RANK[a] >= _RANK[b] else b


def state_health(state, cfg: dict) -> str:
    text = (state or "").strip()
    if not text or text.lower() == "unknown":
        return "unknown"
    for name, health in (cfg.get("state_health") or {}).items():
        if str(name).strip().lower() == text.lower():
            return health if health in _VALID else "critical"
    return "critical"


def evaluate(device: dict, cfg: dict, today: datetime.date = None, unreachable: int = 0) -> dict:
    """device needs: state, ffvs_exp_date, battery_change_date, pt_status,
    ct_status (any may be None). Returns a dict with the overall "health", each
    part's health, the numbers behind them, and a short list of "reasons" for
    anything that is not ok."""
    today = today or datetime.date.today()
    reasons = []

    st = state_health(device.get("state"), cfg)
    if st in ("warning", "critical"):
        reasons.append(f"state is {device.get('state')}")

    exp = parse_date(device.get("ffvs_exp_date"))
    days = (exp - today).days if exp else None
    km = "unknown"
    if days is not None:
        crit = cfg.get("keymat_critical_days", 0)
        warn = cfg.get("keymat_warn_days", 30)
        if crit is not None and days <= crit:
            km = "critical"
            reasons.append("KEYMAT expired" if days < 0 else "KEYMAT expires today")
        elif warn is not None and days <= warn:
            km = "warning"
            reasons.append(f"KEYMAT expires in {days} days")
        else:
            km = "ok"

    changed = parse_date(device.get("battery_change_date"))
    years = round((today - changed).days / 365.25, 1) if changed else None   # for display
    bt = "unknown"
    if changed is not None:
        crit = cfg.get("battery_critical_years")
        warn = cfg.get("battery_warn_years")
        if crit is not None and today >= _add_years(changed, crit):
            bt = "critical"
            reasons.append(f"battery changed {years} years ago")
        elif warn is not None and today >= _add_years(changed, warn):
            bt = "warning"
            reasons.append(f"battery changed {years} years ago")
        else:
            bt = "ok"

    iface = "ok"
    down = cfg.get("interface_down_health", "critical")
    if down not in _VALID:
        down = "critical"
    for label, value in (("plaintext", device.get("pt_status")), ("ciphertext", device.get("ct_status"))):
        if value and str(value).strip().lower() == "down":
            iface = _worse(iface, down)
            reasons.append(f"{label} interface down")

    assoc = "ok"
    if unreachable and cfg.get("unreachable_pairs_warn"):
        assoc = "warning"
        reasons.append(f"{unreachable} unreachable pair(s)")

    overall = "ok"
    for part in (st, km, bt, iface, assoc):
        if part in ("warning", "critical"):
            overall = _worse(overall, part)
    if overall == "ok" and st == "unknown":
        overall = "unknown"

    return {
        "health": overall, "state_health": st, "keymat_health": km, "battery_health": bt,
        "interface_health": iface, "keymat_days": days, "battery_years": years, "reasons": reasons,
    }


def worst(healths) -> str:
    """The worst of several health values ("ok" for none)."""
    result = "ok"
    for h in healths:
        if h in _RANK:
            result = _worse(result, h)
    return result