"""Opt-in photo-backed QML click through the original firmware worker."""

import os
import time
from importlib.resources import files

import pytest


def test_qml_mouse_click_produces_firmware_press_and_release(request, monkeypatch):
    if not request.config.getoption("--run-engine"):
        pytest.skip("Pass --run-engine for the GUI/firmware integration test")
    firmware = os.environ.get("RAZER_TEST_FIRMWARE")
    dumps = os.environ.get("RAZER_TEST_FLASH_DUMPS")
    full_image = os.environ.get("RAZER_TEST_FULL_IMAGE")
    if not firmware and not full_image:
        pytest.skip("Supply private firmware")
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("QT_QUICK_BACKEND", "software")
    from PySide6.QtCore import QPointF, Qt, QUrl
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuick import QQuickWindow  # noqa: F401 - register QQuickWindow wrappers
    from PySide6.QtTest import QTest

    from razer_emu_gui.main import EmulatorBridge

    app = QGuiApplication.instance() or QGuiApplication([])
    bridge = EmulatorBridge(
        {
            "firmware": firmware,
            "full_image": full_image,
            "flash_dumps": dumps.split(os.pathsep) if dumps else [],
            "bootloader": os.environ.get("RAZER_TEST_BOOTLOADER_DUMP"),
        },
    )
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("emulator", bridge)
    engine.load(QUrl.fromLocalFile(str(files("razer_emu_gui").joinpath("qml/Main.qml"))))
    assert engine.rootObjects()
    window = engine.rootObjects()[0]
    reports = []
    bridge.usbReport.connect(lambda endpoint, data: reports.append((endpoint, data)))

    def find_key(item):
        if item.objectName() == "key_selA":
            return item
        for child in item.childItems():
            result = find_key(child)
            if result is not None:
                return result
        return None

    def wait_until(condition):
        deadline = time.monotonic() + 90
        while not condition() and time.monotonic() < deadline:
            QTest.qWait(20)
        assert condition(), bridge.status

    try:
        bridge.start()
        wait_until(lambda: bridge.running)
        key = find_key(window.contentItem())
        assert key is not None
        position = key.mapToScene(QPointF(key.width() / 2, key.height() / 2)).toPoint()
        QTest.mouseClick(
            window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, position
        )
        press = (0x81, "0000040000000000")
        release = (0x81, "0000000000000000")
        wait_until(lambda: press in reports and release in reports[reports.index(press) + 1 :])
        assert len(bridge.colors) == 83
        bridge.toggleRunning()
        wait_until(lambda: not bridge.running)
        assert not bridge.pressed
    finally:
        bridge.close()
        window.close()
        engine.deleteLater()
        app.processEvents()
