# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import sys
import threading
from dataclasses import dataclass

from aqt.qt import QNetworkProxyFactory

ERROR_WINHTTP_AUTODETECTION_FAILED = 12180
WINHTTP_AUTO_DETECT_TYPE_DHCP = 0x1
WINHTTP_AUTO_DETECT_TYPE_DNS_A = 0x2


@dataclass
class SystemProxySettings:
    auto_detect: bool
    has_pac_url: bool
    has_proxy: bool


def defer_proxy_auto_detection() -> threading.Thread | None:
    """When QtWebEngine uses the Windows proxy settings, the first webview
    requests wait about 400 ms at startup. If auto-detection is the only system
    setting (the Windows default), start without system proxies, and turn them
    back on if auto-detection finds a PAC file.

    Returns the auto-detection thread, if one was started."""
    if not QNetworkProxyFactory.usesSystemConfiguration():
        # an application proxy was set
        return None
    settings = read_system_proxy_settings()
    if settings is None or settings.has_pac_url or settings.has_proxy:
        return None
    QNetworkProxyFactory.setUseSystemConfiguration(False)
    if not settings.auto_detect:
        return None
    thread = threading.Thread(
        target=_restore_system_proxies_if_needed, name="proxy-auto-detect", daemon=True
    )
    thread.start()
    return thread


def _restore_system_proxies_if_needed() -> None:
    if system_has_auto_detected_proxy():
        print("proxy auto-detection found a configuration; using system proxies")
        QNetworkProxyFactory.setUseSystemConfiguration(True)


def read_system_proxy_settings() -> SystemProxySettings | None:
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    class IEProxyConfig(ctypes.Structure):
        _fields_ = [
            ("fAutoDetect", wintypes.BOOL),
            ("lpszAutoConfigUrl", ctypes.c_void_p),
            ("lpszProxy", ctypes.c_void_p),
            ("lpszProxyBypass", ctypes.c_void_p),
        ]

    config = IEProxyConfig()
    winhttp = ctypes.WinDLL("winhttp")
    if not winhttp.WinHttpGetIEProxyConfigForCurrentUser(ctypes.byref(config)):
        return None
    kernel32 = ctypes.WinDLL("kernel32")
    for ptr in (config.lpszAutoConfigUrl, config.lpszProxy, config.lpszProxyBypass):
        if ptr:
            kernel32.GlobalFree(ctypes.c_void_p(ptr))
    return SystemProxySettings(
        auto_detect=bool(config.fAutoDetect),
        has_pac_url=bool(config.lpszAutoConfigUrl),
        has_proxy=bool(config.lpszProxy),
    )


def system_has_auto_detected_proxy() -> bool:
    """Runs WPAD discovery (DHCP, then DNS), as Chromium does. This can block
    for seconds. Unexpected errors count as a found proxy, so that the system
    settings are used as before."""
    if sys.platform != "win32":
        return False
    import ctypes

    winhttp = ctypes.WinDLL("winhttp", use_last_error=True)
    url = ctypes.c_void_p()
    if winhttp.WinHttpDetectAutoProxyConfigUrl(
        WINHTTP_AUTO_DETECT_TYPE_DHCP | WINHTTP_AUTO_DETECT_TYPE_DNS_A,
        ctypes.byref(url),
    ):
        if url:
            ctypes.WinDLL("kernel32").GlobalFree(url)
        return True
    return ctypes.get_last_error() != ERROR_WINHTTP_AUTODETECTION_FAILED
