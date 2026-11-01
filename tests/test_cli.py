"""CLI behavior and integration with the separate core package."""

import json
import os
import subprocess
import sys

import pytest


def test_cli_rejects_a_script_beyond_the_requested_duration(tmp_path, capsys):
    from razer_emu_cli.main import main

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
    command = [sys.executable, "-m", "razer_emu_cli", path]
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
