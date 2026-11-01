# Razer keyboard emulator

A firmware-driven emulator for the Razer BlackWidow V4 75% (LPC5528 / Cortex-M33), built on [Renode](https://renode.io/) and [pyrenode3](https://github.com/antmicro/pyrenode3).

**Work in progress. Keyboard input, lighting and Razer Synapse support remain unfinished.** The original application executes without instruction patches, enumerates on an in-process virtual USB host, returns its HID descriptors, and answers a Razer firmware-version query. Unsupported peripheral accesses stop execution with diagnostics. Enumeration or a completed time slice alone does not establish a working keyboard.

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

The CLI prints CPU state, executed-instruction count, virtual duration and unsupported-access counters as JSON. EZH instruction stops and bus faults have distinct stop reasons; `ezh_fault_address` identifies a coprocessor bus fault separately from Cortex-M fault registers. `ezh_holding` reports whether it is waiting at HOLD for a Boolean trigger. Exit status `2` means missing hardware behavior, a fault or a failed USB transaction; diagnostics go to stderr. Exit status `1` means loading or configuration failed. Exit status `0` only means the requested operation and time slice completed.

The verified application capture from firmware-dump version 1.1.1 is the initial development target. The application expects initialized settings outside its own image. Load private, verified region captures from the firmware-dump tool:

```sh
razer-emu application.bin --flash-dump ./settings-capture --microseconds 20000
```

Repeat `--flash-dump` for additional regions. The importer verifies capture hashes and masks; incomplete 16-byte words remain unprogrammed and retain invalid ECC. An application-only boot can fault when it accesses absent profile data. Captures may contain private settings and macros; do not publish them.

To attach the virtual USB host, read descriptors, assign an address and select the first configuration:

```sh
razer-emu application.bin --flash-dump ./settings-capture --usb-enumerate --microseconds 1000
```

The JSON includes `usb` metadata and raw descriptors produced by firmware. The requested time slice runs after enumeration. USB failures include `usb_error`. This host is entirely in-process; it does not connect to a physical keyboard or expose a network USB device.

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

`reset()`, `step(count)`, `run_for(microseconds)`, `state()`, `read_memory()` and `logs()` are frontend-independent. Use the core on an owning worker thread in a GUI. Renode has process-global state: use **one emulator per process**, and do not mix another Renode simulation into that process. Matrix contacts and lighting snapshots are available through the models below; physical key mapping and encoder events remain incomplete.

USB control requests are available through `keyboard.usb`:

```python
from razer_emu import SetupPacket

# Inside the Keyboard context, with the necessary flash captures loaded:
usb_info = keyboard.usb.enumerate_device()
descriptor = keyboard.usb.control(SetupPacket(0x80, 6, value=0x100, length=18))
```

`connect()`, `reset_bus()` and `control()` can also be called separately. NAK polling advances virtual time within a bounded budget. `UsbTimeout`, `UsbStall` and `UsbError` distinguish deadline expiry, firmware STALL and emulator/device errors. The current host uses 64-byte endpoint-zero packets for this high-speed keyboard. Control replies come from the firmware's USB stack, including class requests; the host contains no Razer response implementation.

`keyboard.usb.read_interrupt(0x81, 8, interval_us=125, timeout_us=100_000)` receives one firmware-produced IN packet. Supply the endpoint address, maximum packet size and polling interval appropriate to the firmware's descriptor. Short and zero-length packets are returned unchanged. Polling advances virtual time and preserves each endpoint's cadence across calls; disconnect, bus reset and MCU reset clear that schedule. Completion interrupts are serviced when virtual time next advances. This API does not synthesize HID reports or resolve the scanner startup limitation below.

### Matrix contacts

A contact model can be attached before execution with `matrix = keyboard.attach_matrix()`. It uses the eight row bits and sixteen column bits recovered from the EZH program. `matrix.press(row, column)`, `matrix.release(row, column)` and `matrix.set_key(row, column, pressed)` update ideal active-low, diode-isolated contacts. Coordinates range from row 0–7 and column 0–15. `matrix.pressed` returns an immutable set of held coordinates; contacts stay held across MCU reset. After attachment, the view is also available as `keyboard.matrix`.

These coordinates are electrical scan positions. Physical key/HID mapping, CPU GPIO pin mux and media controls remain incomplete. Normal firmware startup still reaches the unresolved EZH HOLD checkpoint, so attaching contacts does not yet produce USB key events. Matrix tests verify the EZH input path and row selection using synthetic scanner instructions. A separate private component probe can execute the original row routine, but selecting that routine directly is not a completed firmware boot.

### Lighting output

The photographed chip is branded RAZER CHROMA 192. Its observed firmware transactions match an IS31FL3733-compatible register interface. Populated addresses are still unconfirmed, so configure a target explicitly before running firmware:

```python
# Inside the Keyboard context, before stepping, running or USB enumeration:
lighting = keyboard.attach_lighting(4, 0x50)
keyboard.usb.enumerate_device()
keyboard.run_for(400_000)
channels = lighting.state()
print(channels.pwm)  # 192 PWM bytes in driver channel order
print(channels.levels)  # normalized PWM × global current, with on/off masking
```

The equivalent CLI option is `--lighting 4:0x50`, repeatable for distinct addresses. Its JSON adds `lighting` entries with PWM and packed enable-mask hex strings, global current, software shutdown and any unsupported operation. No lighting targets are attached by default. Bus numbers 2 and 4 select the modeled Flexcomm I2C controllers; compatible addresses range from 0x50 to 0x5F.

These are logical channel levels, not calibrated light output. The physical channel-to-key/RGB mapping, external shutdown pin, automatic breathing and electrical fault detection remain incomplete. Requests to execute breathing or open/short detection stop as `unsupported_lighting`; snapshots retain the diagnostic, and `levels` raises for that unsupported operation. Reset clears lighting registers while retaining configured attachments. Snapshot access after closing the Keyboard is rejected.

## Implementation and limits

- Renode supplies Cortex-M33 instruction execution, floating point, NVIC, exception handling, SRAM/USB RAM backing, the memory bus, GPIO register behavior, the DMA copy engine and the virtual clock infrastructure.
- The application is loaded at runtime address `0x20000000` and flash-storage address `0x8000`. Its reset vector and stack pointer come from the original image. The incomplete bootloader capture is not executed.
- Small C# models implement the observed clock/power control registers, flash-controller read/erase/program operations, four-channel match timers, DMA transfers, Flexcomm I²C master operations, watchdog feeding/countdown, and EZH control registers. EZH supports initial mailbox/stack/GPIO operations, heartbeat synchronization, basic arithmetic, fused shift/ALU operations, branches/calls, tight loops, Boolean detection and HOLD/vector dispatch. Its interrupt outputs and mailbox handshake are modeled, but the board's trigger/interrupt routing, emergency-vector execution and much of its instruction set remain incomplete.
- Unknown bus accesses and unsupported operations in these models are checked every 10 virtual microseconds. Several accesses can occur within that interval before execution stops.
- DMA0 executes SRAM descriptors with peripheral request gating, linked transfers, completion/error interrupts and aborts. Flexcomm I2C generates DMA requests for attached targets; absent targets NACK. Hardware trigger routing and bus arbitration timing remain incomplete. An explicitly configured CHROMA 192-compatible model implements page unlocking, 192 PWM channels, 24 enable bytes, global current, software shutdown and read-triggered reset.
- The USB PHY implements control aliases and functional power/PLL readiness. The IP3511 HS controller implements bus reset, SETUP/IN/OUT packet DMA, completion interrupts, endpoint aborts, byte-count tracking and double buffering, backed by 16 KiB of USB RAM. A virtual host starts periodic SOF interrupts after bus reset, with a wrapping 11-bit frame counter. Isochronous traffic, suspend/resume, LPM and external USB transport remain incomplete.
- Timing is functional, not cycle accurate. Virtual CPU rate and currently configured CTIMER2/3 sources are 96 MHz. Clock gating, analog behavior, reset propagation and pin electrical behavior are incomplete.
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

The synthetic Cortex-M33 fixture checks stack-limit register readback, floating-point execution, periodic MR1 interrupts, acknowledgement, exception return, reset and unsupported-access reporting. USB tests cover packet DMA, short transfers, completion/abort behavior and virtual deadlines. Private-image tests target application 1.1.1 and validate descriptor reads, configuration, STALL recovery and a 90-byte Razer feature-report exchange; they do not assert working keyboard input. Set `RAZER_TEST_FLASH_DUMPS` to verified capture directories separated by your operating system's path separator (`:` on Linux, `;` on Windows).

## Remaining milestones

1. Complete MCU/board peripheral behavior and establish reproducible application boot.
2. Recover matrix, encoder and LED wiring; expose input and output events through the core.
3. Complete USB timing and interrupt/HID traffic, including input generated by the original scanner.
4. **Last:** export that controller through Renode USB/IP and validate real Razer software in a QEMU Windows guest. Host-side USB forwarding has not been implemented or tested yet.

MIT licensed. Third-party components retain their respective licenses.
