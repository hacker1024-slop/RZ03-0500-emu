import json
import os
import subprocess
import sys

import pytest

from razer_emu import Firmware, InputScript
from razer_emu.core import Keyboard


@pytest.mark.parametrize(
    "events",
    [
        {},
        [{"at_us": 0, "type": "keyboard", "pressed": True}],
        [{"at_us": 0, "type": "matrix", "row": 8, "column": 0, "pressed": True}],
        [{"at_us": 0, "type": "button", "index": 0, "pressed": 1}],
        [{"at_us": True, "type": "encoder", "phase_a": True, "phase_b": False}],
        [{"at_us": -1, "type": "button", "index": 0, "pressed": True}],
        [{"at_us": 0, "type": "button", "index": 0, "pressed": True, "presssed": True}],
        [
            {"at_us": 2, "type": "button", "index": 0, "pressed": True},
            {"at_us": 1, "type": "button", "index": 0, "pressed": False},
        ],
    ],
)
def test_invalid_input_scripts_are_rejected(events):
    with pytest.raises(ValueError):
        InputScript(events)


def test_input_events_split_virtual_quanta_and_reset_cancels_future_events(cpu_image):
    script = InputScript(
        [
            {"at_us": 5, "type": "matrix", "row": 3, "column": 1, "pressed": True},
            {"at_us": 7, "type": "encoder", "phase_a": False, "phase_b": True},
            {"at_us": 7, "type": "button", "index": 0, "pressed": True},
            {"at_us": 9, "type": "matrix", "row": 3, "column": 1, "pressed": False},
            {"at_us": 13, "type": "button", "index": 0, "pressed": False},
        ]
    )
    with Keyboard(Firmware.load(cpu_image)) as keyboard:
        playback = keyboard.prepare_input_script(script)
        keyboard.run_for(3)
        playback.start()
        keyboard.run_for(4)
        assert not keyboard.matrix.pressed
        keyboard.run_for(1)
        assert keyboard.matrix.pressed == frozenset({(3, 1)})
        keyboard.run_for(2)
        assert keyboard.encoder.phases == (False, True)
        assert keyboard.buttons.pressed == frozenset({0})
        keyboard.run_for(2)
        assert not keyboard.matrix.pressed and playback.applied == 4
        keyboard.reset()
        assert not playback.running
        keyboard.run_for(100)
        assert keyboard.buttons.pressed == frozenset({0})


def test_equal_time_events_keep_order_and_do_not_force_release(cpu_image):
    script = InputScript(
        [
            {"at_us": 0, "type": "button", "index": 0, "pressed": True},
            {"at_us": 0, "type": "button", "index": 0, "pressed": False},
            {"at_us": 0, "type": "button", "index": 1, "pressed": True},
        ]
    )
    with Keyboard(Firmware.load(cpu_image)) as keyboard:
        playback = keyboard.prepare_input_script(script)
        playback.start()
        assert playback.applied == 3 and not playback.running
        assert keyboard.buttons.pressed == frozenset({1})


def test_cli_rejects_a_script_beyond_the_requested_duration(tmp_path, capsys):
    from razer_emu.cli import main

    path = tmp_path / "inputs.json"
    path.write_text(
        json.dumps(
            [
                {"at_us": 6, "type": "button", "index": 0, "pressed": True},
            ]
        )
    )
    assert main(["missing.bin", "--input-script", str(path), "--microseconds", "5"]) == 1
    assert "extends beyond" in capsys.readouterr().err


def test_private_firmware_cli_script_produces_key_reports(cpu_image, tmp_path):
    path = os.environ.get("RAZER_TEST_FIRMWARE")
    dumps = os.environ.get("RAZER_TEST_FLASH_DUMPS")
    if not path or not dumps:
        pytest.skip("Set private firmware and flash captures for CLI input integration")
    script = tmp_path / "inputs.json"
    script.write_text(
        json.dumps(
            [
                {"at_us": 10_000, "type": "matrix", "row": 3, "column": 1, "pressed": True},
                {"at_us": 90_000, "type": "matrix", "row": 3, "column": 1, "pressed": False},
            ]
        )
    )
    command = [sys.executable, "-m", "razer_emu", path]
    for directory in dumps.split(os.pathsep):
        command.extend(("--flash-dump", directory))
    command.extend(("--usb-enumerate", "--input-script", str(script), "--microseconds", "160000"))
    result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["input_events_applied"] == 2 and output["stopped_reason"] is None
    reports = [r["data"] for r in output["usb_reports"] if r["endpoint"] == "0x81"]
    press = reports.index("0000040000000000")
    assert "0000000000000000" in reports[press + 1 :]
