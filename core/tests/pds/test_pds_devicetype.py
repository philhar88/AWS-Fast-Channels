import pytest

from fast_channels.core.pds.devicetype import device_type
from fast_channels.core.pds.models import DeviceType

CTV = DeviceType.CONNECTED_TV


@pytest.mark.parametrize(
    ("user_agent", "expected"),
    [
        ("Mozilla/5.0 (SMART-TV; Linux; Tizen 6.0) AppleWebKit/537.36 (KHTML, like Gecko) TV Safari/537.36", CTV),
        ("Mozilla/5.0 (SMART-TV; Linux) fastv3-exp-p0h/m-full-2", CTV),
        ("Mozilla/5.0 (Web0S; Linux/SmartTV) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/79.0 Safari/537.36", CTV),
        ("Mozilla/5.0 (Linux; Android 9; BRAVIA 4K GB) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0", CTV),
        ("Roku/DVP-9.10 (519.10E04111A)", CTV),
        ("Mozilla/5.0 (Linux; Android 9; AFTMM Build/PS7233) AppleWebKit/537.36 (KHTML, like Gecko) Silk/96.2", CTV),
        ("AppleCoreMedia/1.0.0.19K547 (Apple TV; U; CPU OS 15_2 like Mac OS X; en_us)", CTV),
        (
            "Mozilla/5.0 (X11; Linux armv7l) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0 CrKey/1.56",
            CTV,
        ),
        ("Mozilla/5.0 (Linux; Android 11; SHIELD Android TV) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/96.0", CTV),
        ("HbbTV/1.5.1 (+DRM; Vestel; MB130; 3.1.1.0; ; _TV_G31_2020;)", CTV),
        ("Mozilla/5.0 (Linux; U; Android 4.2.2; en-us; Amino STB A140) AppleWebKit/534.30", DeviceType.SET_TOP_BOX),
        (
            "Mozilla/5.0 (PlayStation; PlayStation 5/2.26) AppleWebKit/605.1.15 (KHTML, like Gecko)",
            DeviceType.CONNECTED_DEVICE,
        ),
        (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; Xbox; Xbox One) AppleWebKit/537.36 Edge/44.18363.8131",
            DeviceType.CONNECTED_DEVICE,
        ),
        (
            "Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148",
            DeviceType.TABLET,
        ),
        (
            "Mozilla/5.0 (Linux; Android 13; SM-X700) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari",
            DeviceType.TABLET,
        ),
        (
            "Mozilla/5.0 (Linux; Android 11; KFTRWI) AppleWebKit/537.36 (KHTML, like Gecko) Silk/120.4 like Chrome",
            DeviceType.TABLET,
        ),
        (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile",
            DeviceType.PHONE,
        ),
        (
            "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Mobile Safari",
            DeviceType.PHONE,
        ),
        (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari",
            DeviceType.PERSONAL_COMPUTER,
        ),
        (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0",
            DeviceType.PERSONAL_COMPUTER,
        ),
        ("Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0", DeviceType.PERSONAL_COMPUTER),
        (
            "Mozilla/5.0 (X11; CrOS x86_64 14541.0.0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0",
            DeviceType.PERSONAL_COMPUTER,
        ),
        ("", None),
        ("curl/8.5.0", None),
        ("Midas", None),
    ],
)
def test_device_type(user_agent: str, expected: DeviceType | None) -> None:
    assert device_type(user_agent) is expected


def test_no_desktop_or_mobile_browser_is_declared_ctv() -> None:
    browsers = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/120.0",
        "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Edg/120.0 after",
    ]
    assert all(device_type(ua) is not CTV for ua in browsers)
