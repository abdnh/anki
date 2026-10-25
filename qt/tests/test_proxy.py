# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

from collections.abc import Iterator

import pytest

import aqt.proxy
from aqt.proxy import SystemProxySettings, defer_proxy_auto_detection
from aqt.qt import QNetworkProxy, QNetworkProxyFactory


@pytest.fixture(autouse=True)
def system_proxies_enabled() -> Iterator[None]:
    previous = QNetworkProxyFactory.usesSystemConfiguration()
    QNetworkProxyFactory.setUseSystemConfiguration(True)
    yield
    QNetworkProxyFactory.setUseSystemConfiguration(previous)


def use_settings(
    monkeypatch: pytest.MonkeyPatch,
    settings: SystemProxySettings,
    auto_detected: bool = False,
) -> None:
    monkeypatch.setattr(aqt.proxy, "read_system_proxy_settings", lambda: settings)
    monkeypatch.setattr(
        aqt.proxy, "system_has_auto_detected_proxy", lambda: auto_detected
    )


def run_defer() -> None:
    if thread := defer_proxy_auto_detection():
        thread.join(timeout=5)


@pytest.mark.parametrize(
    "settings",
    [
        SystemProxySettings(auto_detect=True, has_pac_url=True, has_proxy=False),
        SystemProxySettings(auto_detect=True, has_pac_url=False, has_proxy=True),
    ],
)
def test_defer_proxy_auto_detection_keeps_system_proxies_when_proxy_configured(
    monkeypatch: pytest.MonkeyPatch, settings: SystemProxySettings
) -> None:
    use_settings(monkeypatch, settings)

    run_defer()

    assert QNetworkProxyFactory.usesSystemConfiguration()


def test_defer_proxy_auto_detection_disables_system_proxies_when_nothing_detected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_settings(
        monkeypatch,
        SystemProxySettings(auto_detect=True, has_pac_url=False, has_proxy=False),
        auto_detected=False,
    )

    run_defer()

    assert not QNetworkProxyFactory.usesSystemConfiguration()


def test_defer_proxy_auto_detection_restores_system_proxies_when_pac_detected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_settings(
        monkeypatch,
        SystemProxySettings(auto_detect=True, has_pac_url=False, has_proxy=False),
        auto_detected=True,
    )

    run_defer()

    assert QNetworkProxyFactory.usesSystemConfiguration()


def test_defer_proxy_auto_detection_skips_detection_when_auto_detect_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_settings(
        monkeypatch,
        SystemProxySettings(auto_detect=False, has_pac_url=False, has_proxy=False),
        auto_detected=True,
    )

    thread = defer_proxy_auto_detection()

    assert thread is None
    assert not QNetworkProxyFactory.usesSystemConfiguration()


def test_defer_proxy_auto_detection_keeps_application_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_settings(
        monkeypatch,
        SystemProxySettings(auto_detect=True, has_pac_url=False, has_proxy=False),
        auto_detected=True,
    )
    proxy = QNetworkProxy(QNetworkProxy.ProxyType.HttpProxy, "proxy.example", 8080)
    QNetworkProxy.setApplicationProxy(proxy)

    run_defer()

    assert not QNetworkProxyFactory.usesSystemConfiguration()
    assert QNetworkProxy.applicationProxy().hostName() == "proxy.example"
