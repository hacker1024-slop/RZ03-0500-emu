import shutil
import subprocess
from pathlib import Path

import pytest


def pytest_addoption(parser):
    parser.addoption("--run-engine", action="store_true", help="Run Renode integration tests")


@pytest.fixture
def cpu_image(request, tmp_path):
    if not request.config.getoption("--run-engine"):
        pytest.skip("Pass --run-engine with Renode and GNU Arm binutils installed")
    for tool in ("arm-none-eabi-as", "arm-none-eabi-ld", "arm-none-eabi-objcopy"):
        if not shutil.which(tool):
            pytest.fail(f"Missing integration dependency: {tool}")
    source = Path(__file__).parent / "fixtures" / "cortex-m33.S"
    subprocess.run(
        [
            "arm-none-eabi-as",
            "-mcpu=cortex-m33",
            "-mfpu=fpv5-sp-d16",
            "-mthumb",
            str(source),
            "-o",
            str(tmp_path / "cpu.o"),
        ],
        check=True,
    )
    subprocess.run(
        [
            "arm-none-eabi-ld",
            "-Ttext=0x20000000",
            "-e",
            "reset",
            str(tmp_path / "cpu.o"),
            "-o",
            str(tmp_path / "cpu.elf"),
        ],
        check=True,
    )
    image = tmp_path / "cpu.bin"
    subprocess.run(
        ["arm-none-eabi-objcopy", "-O", "binary", str(tmp_path / "cpu.elf"), str(image)], check=True
    )
    return image
