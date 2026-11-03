"""Opt-in photo-backed QML click through the original firmware worker."""

import os
import select
import socket
import struct
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

    def find_item(item, name):
        if item.objectName() == name:
            return item
        for child in item.childItems():
            result = find_item(child, name)
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
        key = find_item(window.contentItem(), "key_selA")
        assert key is not None
        position = key.mapToScene(QPointF(key.width() / 2, key.height() / 2)).toPoint()
        QTest.mouseClick(
            window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, position
        )
        press = (0x81, "0000040000000000")
        release = (0x81, "0000000000000000")
        wait_until(lambda: press in reports and release in reports[reports.index(press) + 1 :])
        reports.clear()
        QTest.mouseClick(
            window, Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier, position
        )
        wait_until(lambda: press in reports and key.property("held"))
        QTest.mouseClick(
            window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, position
        )
        QTest.qWait(200)
        assert release not in reports
        QTest.mouseClick(
            window, Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier, position
        )
        wait_until(lambda: release in reports and not key.property("held"))
        wheel = find_item(window.contentItem(), "volumeWheel")
        assert wheel is not None
        for fraction, usage in [(0.25, "e9"), (0.75, "ea")]:
            reports.clear()
            position = wheel.mapToScene(
                QPointF(wheel.width() / 2, wheel.height() * fraction)
            ).toPoint()
            QTest.mouseClick(
                window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, position
            )
            volume = (0x82, f"02{usage}" + "00" * 20)
            wait_until(lambda: volume in reports)
        assert len(bridge.colors) == 83
        assert bridge.limitSpeed
        bridge.setLimitSpeed(False)
        assert not bridge.limitSpeed
        bridge.setLimitSpeed(True)
        assert bridge.limitSpeed
        bridge.toggleRunning()
        wait_until(lambda: not bridge.running)
        assert not bridge.pressed
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            port = reserved.getsockname()[1]
        bridge.configureUsbip(port)
        assert bridge.usbipMode and bridge.usbipPort == port

        def exported(pid=None):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2) as client:
                    client.sendall(struct.pack("!HHI", 0x0111, 0x8005, 0))
                    with client.makefile("rb") as response:
                        if response.read(12) != struct.pack("!HHII", 0x0111, 5, 0, 1):
                            return False
                        if pid is None:
                            return True
                        record = response.read(312)
                        return len(record) == 312 and struct.unpack_from("!HH", record, 300) == (
                            0x1532, pid,
                        )
            except OSError:
                return False

        wait_until(exported)
        wait_until(lambda: bridge.running)
        idle_time = bridge.virtualTime
        wait_until(lambda: bridge.virtualTime != idle_time)

        def virtual_seconds():
            return float(bridge.virtualTime.split()[0])

        wait_until(lambda: virtual_seconds() > 0.5)
        bridge.reset()
        wait_until(lambda: virtual_seconds() < 0.5)
        wait_until(exported)
        with socket.create_connection(("127.0.0.1", port), timeout=5) as guest:
            guest.sendall(struct.pack("!HHI32s", 0x0111, 0x8003, 0, b"1-1"))
            with guest.makefile("rb") as response:
                assert response.read(8) == struct.pack("!HHI", 0x0111, 3, 0)
                assert len(response.read(struct.calcsize("!256s32sIIIHHHBBBBBB"))) == 312
                QTest.qWait(150)
                started = time.monotonic()
                before = virtual_seconds()
                wait_until(lambda: virtual_seconds() >= before + 0.2)
                assert virtual_seconds() - before <= time.monotonic() - started + 0.06
                if bridge.canResetIntoBootloader:
                    menu_button = find_item(window.contentItem(), "resetMenuButton")
                    assert menu_button is not None
                    menu_position = menu_button.mapToScene(
                        QPointF(menu_button.width() / 2, menu_button.height() / 2)
                    ).toPoint()
                    QTest.mouseClick(
                        window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                        menu_position,
                    )
                    wait_until(lambda: find_item(window.contentItem(), "resetIntoBootloader"))
                    reset_button = find_item(window.contentItem(), "resetIntoBootloader")
                    assert reset_button is not None and reset_button.isEnabled()
                    reset_position = reset_button.mapToScene(
                        QPointF(reset_button.width() / 2, reset_button.height() / 2)
                    ).toPoint()
                    QTest.mouseClick(
                        window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                        reset_position,
                    )
                    wait_until(lambda: bridge.status == "Running Razer bootloader")
                else:
                    bridge.reset()
                wait_until(lambda: bool(select.select([guest], [], [], 0)[0]))
                assert response.read(1) == b""
        wait_until(exported)
        if bridge.canResetIntoBootloader:
            bridge.reset()
            wait_until(lambda: bridge.status == "Running original firmware")
            wait_until(exported)
        if bridge.canResetIntoBootloader:
            # The official updater sends HID feature command 00/04, mode 1.
            with socket.create_connection(("127.0.0.1", port), timeout=5) as guest:
                guest.sendall(struct.pack("!HHI32s", 0x0111, 0x8003, 0, b"1-1"))
                with guest.makefile("rb") as response:
                    assert response.read(8) == struct.pack("!HHI", 0x0111, 3, 0)
                    assert len(response.read(312)) == 312
                    report = bytearray(90)
                    report[1], report[5], report[7], report[8], report[88] = 1, 2, 4, 1, 7
                    for sequence, setup, data in (
                        (1, struct.pack("<BBHHH", 0, 9, 1, 0, 0), b""),
                        (2, struct.pack("<BBHHH", 0x21, 9, 0x300, 3, 90), report),
                    ):
                        guest.sendall(struct.pack(
                            "!IIIIIIiiii8s", 1, sequence, 0x10001, 0, 0, 0,
                            len(data), 0, -1, 0, setup,
                        ) + data)
                        reply = response.read(48)
                        assert struct.unpack_from("!II12xi", reply) == (3, sequence, 0)
                    wait_until(lambda: bool(select.select([guest], [], [], 0)[0]))
                    assert response.read(1) == b""
            wait_until(lambda: bridge.status == "Running Razer bootloader")
            wait_until(lambda: exported(0x110E))
        bridge.configureUsbip(0)
        assert not bridge.usbipMode
        wait_until(lambda: bridge.running)
        with pytest.raises(ConnectionRefusedError):
            socket.create_connection(("127.0.0.1", port), timeout=0.2)
    finally:
        bridge.close()
        window.close()
        engine.deleteLater()
        app.processEvents()
