"""Qt Quick application and process bridge. All Renode access stays in worker.py."""

import argparse
import json
import sys
from importlib.resources import files
from pathlib import Path

from PySide6.QtCore import Property, QObject, QProcess, QStandardPaths, QUrl, Signal, Slot
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine

from .layout import load_layout
from .photo import keyboard_photo


class EmulatorBridge(QObject):
    changed = Signal()
    usbReport = Signal(int, str)

    def __init__(self, configuration, background=None):
        super().__init__()
        self.layout = load_layout()
        self.configuration = configuration
        photo = (
            Path(background)
            if background
            else keyboard_photo(
                QStandardPaths.writableLocation(QStandardPaths.StandardLocation.CacheLocation)
            )
        )
        if not photo.is_file():
            raise ValueError(f"Keyboard photo does not exist: {photo}")
        self._background = QUrl.fromLocalFile(str(photo.resolve()))
        self._colors = {}
        self._pressed = []
        self._running = False
        self._status = "Choose an application image to begin"
        self._virtual = "0.000 s"
        self._report = "No USB input reports yet"
        self._buffer = b""
        self._process = QProcess(self)
        self._process.readyReadStandardOutput.connect(self._read)
        self._process.readyReadStandardError.connect(self._stderr)
        self._process.errorOccurred.connect(self._process_error)
        self._process.finished.connect(self._finished)

    @Property("QVariantList", constant=True)
    def keys(self):
        return self.layout["keys"]

    @Property(QUrl, constant=True)
    def background(self):
        return self._background

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
    def report(self):
        return self._report

    def _send(self, command):
        if self._process.state() == QProcess.ProcessState.Running:
            self._process.write((json.dumps(command) + "\n").encode())

    @Slot(int, bool)
    def key(self, index, pressed):
        self._send({"type": "key", "index": index, "pressed": pressed})

    @Slot()
    def releaseAll(self):
        self._send({"type": "release_all"})

    @Slot()
    def toggleRunning(self):
        self._send({"type": "running", "value": not self._running})

    @Property(str, notify=changed)
    def captureSummary(self):
        count = len(self.configuration["flash_dumps"])
        mode = (
            "bootloader startup" if self.configuration.get("bootloader") else "application handoff"
        )
        return f"{count} flash capture(s) · {mode}"

    @Slot(QUrl)
    def addFlashDump(self, url):
        path = url.toLocalFile()
        if path not in self.configuration["flash_dumps"]:
            self.configuration["flash_dumps"].append(path)
        self.changed.emit()
        self.start()

    @Slot(QUrl)
    def setBootloader(self, url):
        self.configuration["bootloader"] = url.toLocalFile()
        self.changed.emit()
        self.start()

    @Slot(QUrl)
    def openFirmware(self, url):
        self.configuration["firmware"] = url.toLocalFile()
        self.start()

    @Slot()
    def start(self):
        if not self.configuration.get("firmware"):
            return
        self.close()
        self._buffer = b""
        self._colors = {}
        self._pressed = []
        self._status = "Starting original firmware…"
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
            elif message["type"] == "report":
                self.usbReport.emit(message["endpoint"], message["data"])
            elif message["type"] == "frame":
                self._colors = message["colors"]
                self._pressed = message["pressed"]
                self._running = message["running"]
                self._virtual = f"{message['virtual_us'] / 1_000_000:.3f} s"
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
        self.changed.emit()

    def _finished(self, code, status):
        self._running = False
        self._pressed = []
        self.changed.emit()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Qt Quick firmware keyboard emulator")
    parser.add_argument("firmware", nargs="?")
    parser.add_argument("--flash-dump", action="append", default=[])
    parser.add_argument("--bootloader-dump")
    parser.add_argument("--background", help="Optional aligned 750×420 keyboard image")
    args = parser.parse_args(argv)
    app = QGuiApplication([sys.argv[0]])
    app.setApplicationName("Razer Keyboard Emulator")
    configuration = {
        "firmware": args.firmware,
        "flash_dumps": args.flash_dump,
        "bootloader": args.bootloader_dump,
    }
    try:
        bridge = EmulatorBridge(configuration, args.background)
    except (OSError, ValueError) as exc:
        parser.error(f"Could not load keyboard photograph: {exc}")
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("emulator", bridge)
    engine.load(QUrl.fromLocalFile(str(files("razer_emu_gui").joinpath("qml/Main.qml"))))
    if not engine.rootObjects():
        return 1
    app.aboutToQuit.connect(bridge.close)
    bridge.start()
    return app.exec()
