"""Compare cached watchdog bus writes with Renode's instrumented bus path."""

import subprocess
from pathlib import Path

import pytest

from razer_emu import Firmware
from razer_emu.core import Keyboard

_helper_loaded = False


def _install_probe(keyboard, mode, base, hook):
    global _helper_loaded
    if not _helper_loaded:
        from pyrenode3.wrappers import Monitor

        source = Path(__file__).parent / "fixtures" / "watchdog-dispatch.cs"
        _, error = Monitor().execute("include @" + str(source))
        assert not error, error
        _helper_loaded = True
    from Antmicro.Renode.Utilities import RazerDispatchProbe

    RazerDispatchProbe.Install(
        mode, keyboard._machine.internal, keyboard._cpu, keyboard._watchdog, base, hook
    )
    return RazerDispatchProbe


def _feed_image(tmp_path, base):
    source = tmp_path / "feed.S"
    source.write_text(f""".syntax unified
.cpu cortex-m33
.thumb
.section .text
.word 0x04008000
.word reset+1
.space 0x140-(.-.text)
.global reset
reset:
 ldr r2, =0x4008E000
 movs r3, #0
 str r3, [r2]
 ldr r0, ={base:#x}
 movs r1, #1
 str r1, [r0]
 movs r1, #255
 str r1, [r0,#4]
 movs r1, #0xAA
 str r1, [r0,#8]
 movs r1, #0x55
 str r1, [r0,#8]
1:
 b 1b
.ltorg
""")
    obj, elf, image = (tmp_path / name for name in ("feed.o", "feed.elf", "feed.bin"))
    subprocess.run(
        [
            "arm-none-eabi-as",
            "-mcpu=cortex-m33",
            "-mthumb",
            str(source),
            "-o",
            str(obj),
        ],
        check=True,
    )
    subprocess.run(
        [
            "arm-none-eabi-ld",
            "-Ttext=0x20000000",
            "-e",
            "reset",
            str(obj),
            "-o",
            str(elf),
        ],
        check=True,
    )
    subprocess.run(
        ["arm-none-eabi-objcopy", "-O", "binary", str(elf), str(image)], check=True
    )
    # Eight 16-bit instructions precede the feed sequence. The first literal load,
    # GPIO setup and watchdog setup are deliberately ordinary bus operations.
    return Firmware.load(image), 0x20000150


@pytest.mark.parametrize("base", [0x4000C000, 0x5000C000])
@pytest.mark.parametrize(
    "mode",
    [
        "none",
        "hook",
        "watchpoint",
        "tag",
        "lock",
        "disabled",
        "context",
        "remap",
        "midrun-tag",
        "other-watchpoint",
        "other-wrapper",
        "wfi-hook",
    ],
)
def test_watchdog_dispatch_preserves_bus_instrumentation(
    cpu_image, tmp_path, base, mode
):
    firmware, hook = _feed_image(tmp_path, base)
    results = []
    for enabled in (False, True):
        with Keyboard(firmware) as keyboard:
            keyboard._cpu.WatchdogFastPathEnabled = enabled
            probe = _install_probe(keyboard, mode, base, hook)
            state = keyboard.run_for(100)
            results.append(
                (
                    state.to_dict(),
                    int(keyboard._watchdog.ReadDoubleWord(12)),
                    int(probe.BeforeWrites),
                    int(probe.WatchpointCalls),
                    None if probe.Recorder is None else int(probe.Recorder.FeedWrites),
                )
            )
            assert keyboard._cpu.FastWatchdogWrites == (
                2 if enabled and mode == "none" else 0
            )
    assert results[0] == results[1]


class _CustomService:
    def __init__(self, hook):
        self.ScheduledServiceHook = hook

    def BeginBatch(self, elapsed_us):
        pass


@pytest.mark.parametrize("base", [0x4000C000, 0x5000C000])
def test_multicast_service_cannot_reuse_trusted_pump_dispatch(
    cpu_image, tmp_path, base
):
    firmware, hook = _feed_image(tmp_path, base)
    results = []
    for enabled in (False, True):
        with Keyboard(firmware) as keyboard:
            keyboard._cpu.WatchdogFastPathEnabled = enabled
            probe = _install_probe(keyboard, "none", base, hook)
            service = _CustomService(
                probe.CreateMulticastService(
                    keyboard._usb_device, keyboard._machine.internal, base
                )
            )
            state = keyboard.run_for(100, _usb_pump=service)
            results.append(
                (state.to_dict(), int(keyboard._watchdog.ReadDoubleWord(12)))
            )
            assert probe.BeforeWrites == 1
            assert keyboard._cpu.FastWatchdogWrites == 0
    assert results[0] == results[1]
