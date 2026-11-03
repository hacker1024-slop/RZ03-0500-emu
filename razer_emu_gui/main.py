"""Qt Quick application and process bridge. All Renode access stays in worker.py."""

import argparse
import json
import os
import signal
import socket
import sys
from collections import deque
from importlib.resources import files
from pathlib import Path

from PySide6.QtCore import Property, QObject, QProcess, QSocketNotifier, QUrl, Signal, Slot
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from shiboken6 import delete

from .layout import load_layout


class EmulatorBridge(QObject):
    changed = Signal()
    usbReport = Signal(int, str)

    def __init__(self, configuration):
        super().__init__()
        self.layout = load_layout()
        self.configuration = configuration
        self._colors = {}
        self._pressed = []
        self._running = False
        self._status = "Choose an application image to begin"
        self._virtual = "0.000 s"
        self._speed = "—"
        self._speed_samples = deque(maxlen=21)
        self._report = "No USB input reports yet"
        self._buffer = b""
        self._process = QProcess(self)
        if os.name == "posix":
            # The GUI owns terminal Ctrl+C and shuts the worker down over IPC.
            self._process.setUnixProcessParameters(QProcess.UnixProcessFlag.CreateNewSession)
        self._process.readyReadStandardOutput.connect(self._read)
        self._process.readyReadStandardError.connect(self._stderr)
        self._process.errorOccurred.connect(self._process_error)
        self._process.finished.connect(self._finished)

    @Property(int, constant=True)
    def boardWidth(self):
        return self.layout["width"]

    @Property(int, constant=True)
    def boardHeight(self):
        return self.layout["height"]

    @Property(bool, notify=changed)
    def usbipMode(self):
        return bool(self.configuration.get("usbip"))

    @Property(int, notify=changed)
    def usbipPort(self):
        return self.configuration.get("usbip") or 0

    @Slot(int)
    def configureUsbip(self, port):
        if not 0 <= port <= 65535 or port == self.usbipPort:
            return
        self.configuration["usbip"] = port or None
        if self.configuration.get("firmware") or self.configuration.get("full_image"):
            self.start()
        else:
            self.changed.emit()

    @Property("QVariantList", constant=True)
    def keys(self):
        return self.layout["keys"]

    @Property("QVariantMap", constant=True)
    def wheelRegion(self):
        return self.layout["wheel"]

    @Property("QVariantMap", notify=changed)
    def colors(self):
        return self._colors

    @Property("QVariantList", notify=changed)
    def pressed(self):
        return self._pressed

    @Property(bool, notify=changed)
    def running(self):
        return self._running

    @Property(str, notify=changed)
    def status(self):
        return self._status

    @Property(str, notify=changed)
    def virtualTime(self):
        return self._virtual

    @Property(str, notify=changed)
    def speed(self):
        return self._speed

    @Property(str, notify=changed)
    def report(self):
        return self._report

    def _send(self, command):
        if self._process.state() == QProcess.ProcessState.Running:
            self._process.write((json.dumps(command) + "\n").encode())

    @Slot(int, bool)
    def key(self, index, pressed):
        self._send({"type": "key", "index": index, "pressed": pressed})

    @Slot(int)
    def turnWheel(self, direction):
        self._send({"type": "wheel", "direction": direction})

    @Slot(int)
    def toggleKey(self, index):
        self._send({"type": "toggle_key", "index": index})

    @Slot()
    def releaseAll(self):
        self._send({"type": "release_all"})

    @Slot()
    def toggleRunning(self):
        self._send({"type": "running", "value": not self._running})

    @Property(str, notify=changed)
    def captureSummary(self):
        if self.configuration.get("full_image"):
            return "Complete flash image"
        if not self.configuration.get("firmware"):
            return ""
        mode = "Bootloader and application" if self.configuration.get("bootloader") else "Application only"
        count = len(self.configuration["flash_dumps"])
        return f"{mode} · {count} supplemental flash captures" if count else mode

    @Slot(QUrl, QUrl)
    def loadApplication(self, application, bootloader):
        self.configuration.update(
            firmware=application.toLocalFile(),
            bootloader=bootloader.toLocalFile() or None,
            flash_dumps=[],
            full_image=None,
        )
        self.start()

    @Slot(QUrl)
    def loadFullImage(self, image):
        self.configuration.update(
            firmware=None, bootloader=None, flash_dumps=[], full_image=image.toLocalFile()
        )
        self.start()

    def start(self):
        if not (self.configuration.get("firmware") or self.configuration.get("full_image")):
            return
        self.close()
        self._buffer = b""
        self._colors = {}
        self._pressed = []
        self._running = False
        self._virtual = "0.000 s"
        self._report = "No USB input reports yet"
        self._status = "Starting original firmware…"
        self._speed = "—"
        self._speed_samples.clear()
        self.changed.emit()
        self._process.start(sys.executable, ["-m", "razer_emu_gui.worker"])
        if self._process.waitForStarted(3000):
            self._send(self.configuration)

    @Slot()
    def reset(self):
        if self._process.state() == QProcess.ProcessState.Running:
            self._send({"type": "reset"})
        else:
            self.start()

    @Slot()
    def close(self):
        if self._process.state() != QProcess.ProcessState.NotRunning:
            self._send({"type": "quit"})
            if not self._process.waitForFinished(3000):
                self._process.kill()
                self._process.waitForFinished()

    def _read(self):
        self._buffer += bytes(self._process.readAllStandardOutput())
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if message["type"] == "error":
                self._status = message["message"]
                self._running = False
                self._speed = "—"
                self._speed_samples.clear()
            elif message["type"] == "report":
                self.usbReport.emit(message["endpoint"], message["data"])
            elif message["type"] == "frame":
                self._colors = message["colors"]
                self._pressed = message["pressed"]
                self._running = message["running"]
                self._virtual = f"{message['virtual_us'] / 1_000_000:.3f} s"
                wall = message.get("wall_seconds")
                virtual = message["virtual_us"]
                if not self._running or (
                    self._speed_samples and virtual < self._speed_samples[-1][1]
                ):
                    self._speed_samples.clear()
                    self._speed = "—"
                if self._running and wall is not None:
                    self._speed_samples.append((wall, virtual))
                    first_wall, first_virtual = self._speed_samples[0]
                    if wall > first_wall:
                        self._speed = (
                            f"{(virtual - first_virtual) / 1_000_000 / (wall - first_wall):.2f}×"
                        )
                self._report = message["report"] or "No USB input reports yet"
                self._status = "Running original firmware" if self._running else "Paused"
            self.changed.emit()

    def _stderr(self):
        text = bytes(self._process.readAllStandardError()).decode(errors="replace")
        if text:
            print(text, file=sys.stderr, end="")

    def _process_error(self, error):
        self._status = self._process.errorString()
        self._running = False
        self._speed = "—"
        self._speed_samples.clear()
        self.changed.emit()

    def _finished(self, code, status):
        self._running = False
        self._pressed = []
        self._speed = "—"
        self._speed_samples.clear()
        self.changed.emit()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Qt Quick firmware keyboard emulator")
    parser.add_argument("firmware", nargs="?")
    parser.add_argument("--flash-dump", action="append", default=[])
    parser.add_argument("--bootloader-dump", help="Raw bootloader image or verified capture folder")
    parser.add_argument("--full-image", help="Complete raw 512 KiB flash image")
    parser.add_argument(
        "--usbip",
        type=int,
        metavar="PORT",
        help="Export USB to a guest instead of consuming reports locally",
    )
    args = parser.parse_args(argv)
    if args.usbip is not None and not 1 <= args.usbip <= 65535:
        parser.error("USB/IP port must be between 1 and 65535")
    if args.full_image and (args.firmware or args.bootloader_dump or args.flash_dump):
        parser.error("--full-image cannot be combined with separate images or captures")
    if not args.firmware and (args.bootloader_dump or args.flash_dump):
        parser.error("Separate captures require an application image")
    app = QGuiApplication([sys.argv[0]])
    app.setApplicationName("Razer Keyboard Emulator")
    configuration = {
        "firmware": args.firmware,
        "full_image": args.full_image,
        "flash_dumps": args.flash_dump,
        "bootloader": args.bootloader_dump,
        "usbip": args.usbip,
    }
    try:
        bridge = EmulatorBridge(configuration)
    except (OSError, ValueError) as exc:
        parser.error(f"Could not load keyboard layout: {exc}")
    engine = QQmlApplicationEngine()
    try:
        engine.rootContext().setContextProperty("emulator", bridge)
        qml_path = Path(str(files("razer_emu_gui").joinpath("qml/Main.qml"))).resolve()
        engine.load(QUrl.fromLocalFile(str(qml_path)))
        if not engine.rootObjects():
            return 1
        app.aboutToQuit.connect(bridge.close)
        reader, writer = socket.socketpair()
        with reader, writer:
            reader.setblocking(False)
            writer.setblocking(False)
            notifier = QSocketNotifier(reader.fileno(), QSocketNotifier.Type.Read, app)
            def handle_signal():
                if signal.SIGINT in reader.recv(4096):
                    app.quit()

            notifier.activated.connect(handle_signal)
            previous_handler = signal.signal(signal.SIGINT, lambda *_: None)
            previous_fd = signal.set_wakeup_fd(writer.fileno())
            try:
                bridge.start()
                return app.exec()
            finally:
                notifier.setEnabled(False)
                signal.set_wakeup_fd(previous_fd)
                signal.signal(signal.SIGINT, previous_handler)
    finally:
        # Destroy QML bindings while their Python context object is still alive.
        delete(engine)
