"""User-Agent → AdCOM device type, for ``device.devicetype`` in the ``/policy`` patch.

MediaTailor sends the viewer's User-Agent on the ad request, and Prebid Server copies it to ``device.ua``; the device
type is derived here rather than in a MediaTailor Function. The rules are deliberately conservative: a TV-attached
device is declared Connected TV only on an explicit TV token, so a desktop or mobile browser (the reference player
included) is never declared CTV to demand. A User-Agent no rule recognises gives no device type.
"""

import re
from typing import Final

from fast_channels.core.pds.models import DeviceType

_RULES: Final[tuple[tuple[re.Pattern[str], DeviceType], ...]] = (
    # Smart TVs and TV-attached streaming players (Samsung, LG, Sony, Hisense, Vizio, HbbTV sets, Roku, Fire TV,
    # Apple TV, Chromecast, Android/Google TV).
    (
        re.compile(
            r"smart[-_ ]?tv|hbbtv|\bweb[o0]s\b|netcast|bravia|vidaa|vizio|\broku\b|appletv|apple tv|\btvos\b"
            r"|\bcrkey\b|android ?tv|google ?tv|fire ?tv",
            re.IGNORECASE,
        ),
        DeviceType.CONNECTED_TV,
    ),
    (re.compile(r"\bAFT[A-Z0-9]{1,8}\b"), DeviceType.CONNECTED_TV),  # Fire TV model codes (AFTMM, AFTKA, ...)
    (re.compile(r"\bstb\b", re.IGNORECASE), DeviceType.SET_TOP_BOX),
    (re.compile(r"playstation|xbox|nintendo", re.IGNORECASE), DeviceType.CONNECTED_DEVICE),  # game consoles
    (re.compile(r"\bipad\b|\btablet\b|\bkindle\b|\bsilk/", re.IGNORECASE), DeviceType.TABLET),
    (re.compile(r"\biphone\b|\bipod\b|windows phone|android.*\bmobile\b", re.IGNORECASE), DeviceType.PHONE),
    (re.compile(r"\bandroid\b", re.IGNORECASE), DeviceType.TABLET),  # Android without "Mobile" is a tablet
    (re.compile(r"windows nt|macintosh|\bx11\b|\bcros\b", re.IGNORECASE), DeviceType.PERSONAL_COMPUTER),
)


def device_type(user_agent: str) -> DeviceType | None:
    """The AdCOM device type of ``user_agent``, or ``None`` when no rule recognises it.

    Rules apply in order, most specific first: TV and set-top tokens, consoles, tablets, phones, then desktops
    (an Android TV or a console UA also names Android, Linux or Windows).
    """
    for pattern, kind in _RULES:
        if pattern.search(user_agent):
            return kind
    return None
