# Razer keyboard emulator

A firmware-driven emulator for the Razer BlackWidow V4 75% (LPC5528 / Cortex-M33), built on [Renode](https://renode.io/) and [pyrenode3](https://github.com/antmicro/pyrenode3).

**Work in progress. It does not yet emulate a working USB keyboard or support Razer Synapse.** The original application executes without instruction patches, decompresses its runtime data, and reaches board initialization. Unsupported peripheral accesses stop execution with diagnostics. A completed time slice alone is not evidence that boot completed.

This project is AI-generated, experimental, unofficial, and unaffiliated with Razer. Peripheral behavior is incomplete and cannot establish that firmware modifications will work on physical hardware. The emulator itself does not open or modify a physical keyboard.

## Requirements and installation

- Python 3.11 or newer and Renode 1.17.0 (.NET build). The tested environment is Linux, Python 3.13, .NET 10, Renode 1.17.0.
- A compatible .NET runtime for your Renode installation.
- Your own raw application firmware image, including its vector table. Proprietary firmware is not included.

From this checkout, in an activated virtual environment:

```sh
python -m pip install -e '.[dev]'
```

The Python bridge is pinned to an upstream Git revision because its current Renode runtime-loader support is needed. Git must be available during installation. If Renode is not discovered automatically, set `PYRENODE_PATH` to its installation directory; consult pyrenode3's installation instructions. `DOTNET_ROOT` must select the runtime required by that installation.

On NixOS, `nix-shell` provides the system dependencies and environment variables. Create a virtual environment and install this package inside that shell. `shell.nix` uses your configured nixpkgs; it is not a lock file.

## Headless execution

```sh
razer-emu /path/to/application.bin --microseconds 20000
```

The CLI prints CPU state, executed-instruction count, virtual duration and unsupported-access counters as JSON. Exit status `2` means the firmware reached missing hardware behavior or a CPU fault; diagnostics go to stderr. Exit status `1` means loading or configuration failed. Exit status `0` only means the requested time slice completed.

The verified application capture from firmware-dump version 1.1.1 is the initial development target. The application expects initialized settings outside its own image. Load private, verified region captures from the firmware-dump tool:

```sh
razer-emu application.bin --flash-dump ./settings-capture --microseconds 20000
```

Repeat `--flash-dump` for additional regions. The importer verifies capture hashes and masks; incomplete 16-byte words remain unprogrammed and retain invalid ECC. An application-only boot can fault when it accesses absent profile data. Captures may contain private settings and macros; do not publish them.

Updater payloads and complete flash dumps must be converted to raw application images before loading. Other models are not supported yet.

## Core library

The CLI is a thin frontend; execution and inspection live in the reusable core:

```python
from razer_emu import Firmware
from razer_emu.core import Keyboard

with Keyboard(Firmware.load("application.bin")) as keyboard:
    state = keyboard.run_for(1000)
    print(state.to_dict())
    print(keyboard.read_memory(0x20000000, 16).hex())
    if state.stopped_reason:
        print("\n".join(keyboard.logs()))
```

`Keyboard(..., flash_regions=[FlashRegion.load_dump(directory)])` provides the same capture loading in the library; import `FlashRegion` from `razer_emu`.

`reset()`, `step(count)`, `run_for(microseconds)`, `state()`, `read_memory()` and `logs()` are frontend-independent. Use the core on an owning worker thread in a GUI. Renode has process-global state: use **one emulator per process**, and do not mix another Renode simulation into that process. Keyboard-matrix input, encoder events, LED observation and USB requests are forthcoming as their peripheral models are completed.

## Implementation and limits

- Renode supplies Cortex-M33 instruction execution, floating point, NVIC, exception handling, SRAM backing, the memory bus, GPIO register behavior and the virtual clock infrastructure.
- The application is loaded at runtime address `0x20000000` and flash-storage address `0x8000`. Its reset vector and stack pointer come from the original image. The incomplete bootloader capture is not executed.
- Small C# models implement the observed clock/power control registers, flash-controller read/erase/program operations, four-channel match timers, DMA configuration, Flexcomm I²C master bring-up, watchdog feeding/countdown, and EZH control registers. EZH instruction execution remains unsupported.
- Unknown bus accesses and unsupported operations in these models are checked every 10 virtual microseconds. Several accesses can occur within that interval before execution stops.
- Timing is functional, not cycle accurate. Virtual CPU rate and currently configured CTIMER2 source are 96 MHz. Clock gating, analog behavior, resets, DMA data movement and pin electrical behavior are incomplete.
- Unloaded flash has invalid ECC; controller reads report ECC errors and CPU reads fault. No specimen settings or bootloader are fabricated. Factory trim defaults and undocumented register assumptions are described in [model notes](docs/model-notes.md). These are not a recovered copy of a particular keyboard's factory data or persistent settings.

## Tests

Unit tests require no firmware or emulator:

```sh
python -m pytest
```

For CPU and peripheral integration tests, install GNU Arm binutils and Renode:

```sh
python -m pytest --run-engine
```

To include execution of your private application image:

```sh
RAZER_TEST_FIRMWARE=/path/to/application.bin python -m pytest --run-engine
```

The synthetic Cortex-M33 fixture checks stack-limit register readback, floating-point execution, periodic MR1 interrupts, acknowledgement, exception return, reset and unsupported-access reporting. Private-image tests distinguish missing persistent settings from missing peripheral models; they do not assert a working keyboard. To validate with initialized settings, set `RAZER_TEST_FLASH_DUMPS` to capture directories separated by your operating system's path separator (`:` on Linux, `;` on Windows).

## Remaining milestones

1. Complete MCU/board peripheral behavior and establish reproducible application boot.
2. Recover matrix, encoder and LED wiring; expose input and output events through the core.
3. Implement the firmware-facing USB controller and validate enumeration and HID transactions in-process.
4. **Last:** export that controller through Renode USB/IP and validate real Razer software in a QEMU Windows guest. Host-side USB forwarding has not been implemented or tested yet.

MIT licensed. Third-party components retain their respective licenses.
