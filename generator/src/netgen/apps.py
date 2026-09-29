"""Application catalogue and per-device-type demand.

`app` is what an upstream classifier would label the flow as. Most of these ride on
443, which is the point: the port says nothing, the classification does.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class App:
    name: str
    category: str
    criticality: str  # critical | business | bulk
    ip_proto: int  # 6 tcp, 17 udp
    l7: str
    dst_port: int
    hosted: str  # dc | internet
    bytes_c2s: float  # median bytes per session
    bytes_s2c: float
    duration_s: float  # median session duration when nothing is throttling it
    server_ms: float  # median server think time before the first response byte


# fmt: off
_CATALOGUE: tuple[App, ...] = (
    #    name                  category         crit        proto l7              port   hosted      up     down   dur  think
    App("core_banking",       "banking",       "critical", 6,  "tls1.2",         443,   "dc",       3e3,   25e3,  8,   45),
    App("card_authorisation", "payments",      "critical", 6,  "tls1.2",         7443,  "dc",       900,   700,   1.5, 120),
    App("atm_management",     "payments",      "business", 6,  "tls1.3",         443,   "dc",       2e3,   15e3,  20,  60),
    App("swift",              "payments",      "critical", 6,  "tls1.2",         48002, "dc",       8e3,   6e3,   30,  200),
    App("file_share",         "business",      "business", 6,  "smb3-encrypted", 445,   "dc",       2e5,   2e6,   30,  20),
    App("cctv_upload",        "security",      "bulk",     6,  "tls1.3",         443,   "dc",       1.5e8, 5e4,   600, 10),
    App("email",              "productivity",  "business", 6,  "tls1.3",         443,   "internet", 1.5e4, 1.2e5, 20,  90),
    App("teams_media",        "collaboration", "business", 17, "srtp",           3478,  "internet", 6e7,   6e7,   600, 0),
    App("software_updates",   "bulk",          "bulk",     6,  "tls1.3",         443,   "internet", 2e4,   8e7,   300, 30),
    App("guest_internet",     "guest",         "bulk",     17, "quic",           443,   "internet", 5e4,   1.5e7, 120, 60),
)
# fmt: on
APPS: dict[str, App] = {a.name: a for a in _CATALOGUE}

# sessions per device per hour at full business activity
DEMAND: dict[str, dict[str, float]] = {
    "teller": {
        "core_banking": 90,
        "email": 6,
        "teams_media": 0.5,
        "file_share": 4,
        "software_updates": 0.2,
    },
    "backoffice": {
        "core_banking": 30,
        "swift": 2,
        "email": 12,
        "teams_media": 1.5,
        "file_share": 8,
        "software_updates": 0.2,
    },
    "atm": {"card_authorisation": 25, "atm_management": 1, "software_updates": 0.05},
    "card_terminal": {"card_authorisation": 30},
    "cctv": {"cctv_upload": 5},
    "guest": {"guest_internet": 40},
}


def _bump(hour: float, centre: float, width: float) -> float:
    return math.exp(-((hour - centre) ** 2) / (2 * width**2))


def activity(device_type: str, local_hour: float, weekday: int) -> float:
    """0..1 activity multiplier. weekday: Monday=0 .. Sunday=6."""
    if device_type == "cctv":
        return 1.0
    if device_type == "atm":
        base = 0.15 + 0.55 * _bump(local_hour, 12.5, 2.0) + 0.65 * _bump(local_hour, 18.0, 1.8)
        return min(1.0, base * (0.8 if weekday >= 5 else 1.0))

    # staffed branch hours: weekdays 08-17, Saturday 09-13, closed Sunday
    if weekday < 5:
        open_h, close_h = 8.0, 17.0
    elif weekday == 5:
        open_h, close_h = 9.0, 13.0
    else:
        return 0.02
    if local_hour < open_h - 0.5 or local_hour > close_h + 0.5:
        return 0.02
    ramp = min(1.0, (local_hour - (open_h - 0.5)) / 1.0, ((close_h + 0.5) - local_hour) / 1.0)
    lunch = 1.0 - 0.2 * _bump(local_hour, 13.0, 0.6)
    level = max(0.02, ramp) * lunch
    return level * (0.8 if device_type == "guest" else 1.0)
