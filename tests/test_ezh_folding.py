"""Check fixed-point execution against the ordinary instruction interpreter."""

import subprocess

import pytest

from razer_emu import Firmware
from razer_emu.core import Keyboard

EZH = 0x4001D000
CODE = 0x20002400


def _self_branch(condition):
    # SUB PC, PC, #8: architectural PC reads as instruction address + 8.
    return 8 | (condition << 5) | (13 << 10) | (13 << 14) | (1 << 18) | (8 << 20)


def _start(keyboard, words):
    for index, word in enumerate(words):
        keyboard._bus.WriteDoubleWord(CODE + index * 4, word)
    keyboard._bus.WriteDoubleWord(EZH + 0x20, CODE)
    keyboard._bus.WriteDoubleWord(EZH + 0x24, 0xC0DE0001)


def _snapshot(ezh):
    # These internal counters distinguish identical PC values reached through
    # different heartbeat/loop/flag histories.
    from System.Reflection import BindingFlags

    flags = BindingFlags.Instance | BindingFlags.NonPublic
    internal = []
    for name in (
        "flagsMask",
        "heartbeatPeriod",
        "heartbeatLeft",
        "waitForBeat",
        "syncToBeat",
        "loopStart",
        "loopEnd",
        "loopRemaining",
        "carry",
    ):
        member = ezh.GetType().GetProperty(name, flags) or ezh.GetType().GetField(
            name, flags
        )
        value = member.GetValue(ezh)
        internal.append(
            bool(value)
            if name in ("waitForBeat", "syncToBeat", "carry")
            else int(value)
        )
    return (
        int(ezh.Cycles),
        int(ezh.Instructions),
        int(ezh.ReadDoubleWord(0x28)),
        tuple(int(ezh.GetRegister(index)) for index in range(16)),
        int(ezh.ReadDoubleWord(0x48)),
        tuple(internal),
    )


@pytest.mark.parametrize("period", [0, 7, 32, 33, 1200])
@pytest.mark.parametrize("synchronized", [False, True])
def test_self_branch_folding_preserves_heartbeat_and_register_history(
    cpu_image, period, synchronized
):
    with Keyboard(Firmware.load(cpu_image)) as keyboard:
        traces = []
        for folded in (False, True):
            keyboard.reset()
            ezh = keyboard._ezh
            ezh.LoopFolding = folded
            _start(
                keyboard,
                [
                    (period << 16) | 0x32,
                    (0x80000000 if synchronized else 0) | 0x52,
                    _self_branch(13),
                    (7 << 20) | (1 << 18),
                    _self_branch(0),
                ],
            )
            trace = []
            for index, count in enumerate(
                (1, 1, 1, 2, 31, 32, 33, 63, 127, 128, 1023, 4096, 17, 3001)
            ):
                if index == 10:
                    ezh.OnGPIO(0, True)
                if index == 12:
                    keyboard._bus.WriteDoubleWord(EZH + 0x24, 0xC0DE0003)
                ezh.AdvanceCycles(count)
                trace.append(_snapshot(ezh))
            traces.append(trace)
        assert traces[0] == traces[1]


def test_finite_tight_loop_is_not_mistaken_for_a_self_branch(cpu_image):
    with Keyboard(Firmware.load(cpu_image)) as keyboard:
        traces = []
        for folded in (False, True):
            keyboard.reset()
            ezh = keyboard._ezh
            ezh.LoopFolding = folded
            _start(
                keyboard,
                [
                    6 | (13 << 14) | (1 << 18) | (20 << 20),  # R0 = loop end
                    5 << 10,  # R5 = R0
                    (6 << 10) | (1 << 18) | (3 << 20),  # R6 = three repeats
                    0x1A | (5 << 14) | (6 << 20),  # TIGHT_LOOP R5, R6
                    0x12,  # one-time prologue
                    13 << 10,  # MOV PC, R0
                    0x12,
                    (1 << 10) | (1 << 18) | (55 << 20),  # MOV R1, #55
                ],
            )
            ezh.AdvanceCycles(10)
            assert ezh.ReadDoubleWord(0x28) == CODE + 32
            assert ezh.GetRegister(1) == 55
            traces.append(_snapshot(ezh))
        assert traces[0] == traces[1]


@pytest.mark.parametrize("signal", ["ex", "pending_trap"])
def test_self_branch_observes_another_cpu_signal_at_a_known_deadline(
    cpu_image, tmp_path, signal
):
    # 3 setup instructions + 2*2398 loop instructions + one store = 4800
    # Cortex-M instructions, placing the signal at 50 us with the 96-MIPS model.
    address, value = (
        (EZH + 0x24, 0xC0DE0003) if signal == "ex" else (EZH + 0x48, 0x00FF0001)
    )
    source = tmp_path / "signal.S"
    source.write_text(f""".syntax unified
.cpu cortex-m33
.thumb
.section .text
.word 0x04008000
.word reset+1
.space 0x140-(.-.text)
.global reset
reset:
 ldr r0, ={address:#x}
 ldr r1, ={value:#x}
 movw r2, #2398
1:
 subs r2, #1
 bne 1b
 str r1, [r0]
2:
 b 2b
.ltorg
""")
    obj, elf, image = (
        tmp_path / name for name in ("signal.o", "signal.elf", "signal.bin")
    )
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
    traces = []
    for folded in (False, True):
        with Keyboard(Firmware.load(image)) as keyboard:
            ezh = keyboard._ezh
            ezh.LoopFolding = folded
            words = []
            if signal == "pending_trap":
                words.append((11 << 10) | (1 << 18) | (255 << 20))
                keyboard._bus.WriteDoubleWord(EZH + 0x48, 0x00FF0000)
            words += [
                _self_branch(13 if signal == "ex" else 12),
                (7 << 20) | (1 << 18),
                _self_branch(0),
            ]
            _start(keyboard, words)
            trace = []
            for duration in (40, 10, 10):
                assert keyboard.run_for(duration).stopped_reason is None
                trace.append(_snapshot(ezh))
            assert trace[0][3][0] == 0
            assert trace[-1][3][0] == 7
            traces.append(trace)
    assert traces[0] == traces[1]
