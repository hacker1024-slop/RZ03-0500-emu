# Razer keyboard emulator

A firmware-driven emulator for the Razer BlackWidow V4 75% (LPC5528 / Cortex-M33), built on [Renode](https://renode.io/) and [pyrenode3](https://github.com/antmicro/pyrenode3).

**Work in progress. A firmware-derived ANSI key/RGB map is available in the GUI; physical wiring validation and broad Razer software compatibility remain unfinished. Native Windows Synapse recognition, gaming-mode control, static lighting and brightness changes are verified for the tested firmware.** The original application executes without instruction patches, enumerates on an in-process virtual USB host, returns its HID descriptors, and answers a Razer firmware-version query. Normal matrix, encoder and independent-button inputs produce firmware-generated key, volume and mute reports. Matrix verification currently covers A-key press/release with the captured default binding. Unsupported peripheral accesses stop execution with diagnostics. Enumeration or a completed time slice alone does not establish a working keyboard.

This project is AI-generated, experimental, unofficial, and unaffiliated with Razer. Peripheral behavior is incomplete and cannot establish that firmware modifications will work on physical hardware. The emulator itself does not open or modify a physical keyboard.

The warmed execution benchmark reaches **1.08× real time** on the development host (Ryzen 9 5900XT, Linux, Renode 1.17.0): five virtual seconds in 4.63 wall seconds, including key press/release, USB interrupt polling and all three lighting controllers. This uses 125 µs fault checks and 5 ms batches. Performance depends on the host and workload; startup compilation is excluded. The GUI displays its measured virtual-time/wall-time ratio. The configured 96 MHz instruction budget and firmware are unchanged.

The photo-backed Wayland GUI exporting USB to Windows 11 in QEMU also sustained **1.05×** over 15 wall seconds, with all 83 RGB indicators, a GUI A-key click arriving in Windows, and the original Razer DLL returning firmware version 1.1.1.

## Requirements and installation

- Python 3.11 or newer and Renode 1.17.0 (.NET build). The tested environment is Linux, Python 3.13, .NET 10, Renode 1.17.0.
- A compatible .NET runtime for your Renode installation.
- Your own raw application firmware image, including its vector table. Proprietary firmware is not included.

From this checkout, in an activated virtual environment:

```sh
python -m pip install -e '.[dev]'
```

The Python bridge is pinned to an upstream Git revision because its current Renode runtime-loader support is needed. Git must be available during installation. If Renode is not discovered automatically, set `PYRENODE_PATH` to its installation directory; consult pyrenode3's installation instructions. `DOTNET_ROOT` must select the runtime required by that installation.

On NixOS, enter `nix-shell` from this checkout. It provides Renode, pyrenode3,
PySide6 and the required environment variables. Run `python -m razer_emu_gui`
or `python -m razer_emu_cli` directly from the checkout. `shell.nix` uses your
configured nixpkgs.

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

## Scripted inputs

Save an ordered JSON array as `inputs.json`:

```json
[
  {"at_us": 10000, "type": "matrix", "row": 3, "column": 1, "pressed": true},
  {"at_us": 90000, "type": "matrix", "row": 3, "column": 1, "pressed": false}
]
```

```sh
razer-emu application.bin --flash-dump ./settings-capture \
  --usb-enumerate --input-script inputs.json --microseconds 160000
```

Times are integer virtual microseconds relative to playback start, after USB enumeration when requested. Equal-time events retain file order. Coordinates are electrical scan positions; (3, 1) is A in the tested capture. Leave time after the last edge for firmware sampling and debounce. In bounded runs, every event must fit within `--microseconds`. Playback does not release held inputs implicitly.

| Event type | Additional fields |
|---|---|
| `matrix` | `row` (0–7), `column` (0–15), `pressed` (boolean) |
| `encoder` | `phase_a`, `phase_b` (booleans) |
| `button` | `index` (0 or 1), `pressed` (boolean) |

The JSON result includes `input_events_applied`. With `--usb-enumerate`, it also contains `usb_reports`: endpoint addresses, raw firmware-produced packet hex, and polling times relative to playback start. This is a virtual host reading interrupt endpoints; the script only changes electrical inputs.

Scripts also work with `--usbip`. Playback starts before serving; virtual time advances during guest import and USB traffic, so allow enough lead-in before the first input. Reports go to the importing guest. A cold-boot CLI script has delivered A-key, volume and mute press/release events to Linux in QEMU through this path. For precise control over when playback begins, prepare a script through the library before execution and start it when your frontend is ready:

```python
from razer_emu import InputScript

# Inside the Keyboard context, before executing firmware:
playback = keyboard.prepare_input_script(InputScript.load("inputs.json"))
keyboard.usb.enumerate_device()
playback.start()
keyboard.run_for(160000)
```

The core splits its execution quanta at event deadlines, including time advanced by USB transactions. `playback.cancel()` cancels future events while retaining current contact levels. MCU reset cancels playback too; calling `start()` again replays the script from its first event. Instruction stepping does not advance the script's virtual-time counter. Like the other input APIs, playback belongs to the core's owning thread.

## Qt Quick application

Install the optional frontend dependency:

```sh
python -m pip install -e '.[gui]'
razer-emu-gui application.bin --flash-dump ./settings-capture
```

Open **USB/IP…** to enable or disable export and choose the loopback port
(default 3240). You can also launch with `--usbip 3240`. Applying a change
restarts loaded firmware and disconnects any attached guest. The GUI shows
the configured endpoint and bus ID. Firmware continues running while waiting
for a guest. An attached guest owns USB endpoint polling. The GUI disables
pause while export is enabled. Reset disconnects any attached guest, reboots
the keyboard, and resumes listening on the same port.

`python -m razer_emu_gui` is equivalent. The key and RGB map describes the BlackWidow V4 75% ANSI hardware. Both
firmware 1.1.1 and 1.2.1 use the same map. The GUI accepts structurally valid
application images for this board.

The **Load firmware** menu offers application only, bootloader and application,
or a complete raw 512 KiB flash image. For the two-file option, select the raw
32 KiB bootloader first and then the application. A complete image contains the
bootloader at offset 0 and the application at offset `0x8000`.

From the command line, pass an application as the positional argument, add
`--bootloader-dump` for a raw bootloader or verified capture folder, or pass
`--full-image flash.bin` by itself. In the first two modes, the GUI generates
blank, ECC-valid settings in emulated flash. Firmware 1.1.1 and 1.2.1 can start with their
defaults from this state. Optional `--flash-dump` captures override those bytes.
Complete images supply their own settings.

The GUI bundles an AI-generated image of the unlit keyboard. The overlaid colour indicators and translucent key highlights show the emulated controller output.

Click the top half of the pictured volume wheel for volume up or the bottom half for volume down. The firmware decodes the electrical phases and applies its active profile. Left-click a key for a momentary press. Right-click to hold it down, then right-click again to release it. Toggled keys stay held when the window loses focus and release on pause or reset. Short clicks last at least 40 ms of virtual time for firmware debounce; visible feedback may lag wall time when emulation is slow. Releasing outside a key or losing focus releases momentary presses. The **Limit to 1×** checkbox is enabled by default and paces emulation against wall time in the GUI and over USB/IP. The UI displays virtual time and firmware-produced USB input reports, with pause/resume and reset controls. Renode executes in a separate owning process, keeping GUI rendering independent of firmware execution.

The mapping includes 81 ANSI matrix keys and the two independent media buttons. Key IDs, matrix contacts and RGB channels come from firmware tables; the background and hit-region alignment come from Razer's published ANSI product assets. [Mapping provenance and validation](docs/gui-mappings.md) describes the sources and limits. No physical keyboard access is required.

## Core library

The distribution contains three Python packages:

- `razer_emu`: the reusable core, hardware models, firmware loading, inputs and USB transport.
- `razer_emu_cli`: argument parsing, command execution and terminal output; it depends on the core.
- `razer_emu_gui`: optional PySide6 / Qt Quick application with a separate emulator worker process.

Run the frontend with `razer-emu` or `python -m razer_emu_cli`. Core imports do not load the CLI. Execution and inspection live in the reusable core:

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

`Keyboard(..., check_interval_us=125)` reduces synchronization overhead by checking faults and unsupported accesses every 125 virtual microseconds. The library and ordinary headless CLI retain a 10 µs default; the GUI and USB/IP CLI use 125 µs. The accepted range is 1–1000 µs. Use `--check-interval-us 10` to select the stricter interval in the CLI. A run still ends at its requested virtual-time boundary, and scripted input deadlines and queued USB poll deadlines can shorten a synchronization interval.

`reset()`, `step(count)`, `run_for(microseconds)`, `state()`, `read_memory()` and `logs()` are frontend-independent. Execution is paused between advancement calls; a GUI can pause its worker by withholding further calls. Use the core on an owning worker thread in a GUI. Renode has process-global state: use **one emulator per process**, and do not mix another Renode simulation into that process. Matrix contacts and lighting snapshots are available through the models below; the GUI supplies a firmware-specific ANSI physical map. Normal firmware-generated matrix press/release is verified for electrical contact (3, 1), bound to A in the private capture.

Bounded runs retain quiescent CPU threads between calls; reset, stepping and close join them through Renode's normal pause path. Stepping switches EZH to its reference timer-driven schedule for the lifetime of that `Keyboard`; create a new instance to restore the faster schedule after debugging. Configure Renode instrumentation between execution calls. Custom callbacks that change bus mappings during execution are outside the supported core API; live external GDB attachment has not been validated.

USB control requests are available through `keyboard.usb`:

```python
from razer_emu import SetupPacket

# Inside the Keyboard context, with the necessary flash captures loaded:
usb_info = keyboard.usb.enumerate_device()
descriptor = keyboard.usb.control(SetupPacket(0x80, 6, value=0x100, length=18))
```

`connect()`, `reset_bus()` and `control()` can also be called separately. NAK polling advances virtual time within a bounded budget. `UsbTimeout`, `UsbStall` and `UsbError` distinguish deadline expiry, firmware STALL and emulator/device errors. The current host uses 64-byte endpoint-zero packets for this high-speed keyboard. Control replies come from the firmware's USB stack, including class requests; the host contains no Razer response implementation.

`keyboard.usb.read_interrupt(0x81, 8, interval_us=125, timeout_us=100_000)` receives one firmware-produced IN packet. Supply the endpoint address, maximum packet size and polling interval appropriate to the firmware's descriptor. Short and zero-length packets are returned unchanged. Polling advances virtual time and preserves each endpoint's cadence across calls; disconnect, bus reset and MCU reset clear that schedule. Completion interrupts are serviced when virtual time next advances. This API reads the firmware’s endpoint data without synthesizing HID reports.

For event loops, `keyboard.usb.poll_interrupt(0x81, 8, interval_us=125)` attempts one transaction without advancing virtual time. It returns `None` when the endpoint NAKs or its next poll is not due, and bytes on ACK (including `b""`). Advance time with `keyboard.run_for(...)` between attempts. Blocking and nonblocking reads share the endpoint polling schedule.

### USB/IP and QEMU

Start the firmware-backed USB/IP server with your own image and verified settings captures:

```sh
razer-emu /path/to/application.bin \
  --flash-dump /path/to/settings-capture \
  --flash-dump /path/to/remaining-flash-capture \
  --usbip 3240
```

The CLI binds to `127.0.0.1` and exports bus ID `1-1`. It does not open or forward a physical keyboard. USB/IP has no authentication; the library server also defaults to loopback. Stop with Ctrl+C. While serving, `--microseconds` and `--usb-enumerate` do not apply.

A QEMU guest using user-mode networking can reach the host at `10.0.2.2`. For a Linux guest with the USB/IP client installed:

```sh
sudo modprobe vhci-hcd
usbip list -r 10.0.2.2
sudo usbip attach -r 10.0.2.2 -b 1-1
# Later: inspect the imported port and detach it.
usbip port
sudo usbip detach -p 0
```

Linux in QEMU has successfully enumerated this server with the private 1.1.1 image and bound all five HID interfaces, creating keyboard, mouse and hidraw devices. A version query through the guest HID driver also returned a valid Razer 00/87 reply reporting 1.1.1. A cold-boot library run driving encoder phases and the independent mute contact also delivered Linux `KEY_VOLUMEDOWN`, `KEY_VOLUMEUP` and `KEY_MUTE` press/release events through USB/IP. The same cold-boot path also delivers Linux `KEY_A` press/release from matrix contact (3, 1), alongside the media events. The complete application image remains unchanged.

For a Windows QEMU guest, install a USB/IP client such as [usbip-win2](https://github.com/vadimgrn/usbip-win2) inside the guest, following its driver installation instructions. Its commands are `usbip.exe list -r 10.0.2.2` and `usbip.exe attach -r 10.0.2.2 -b 1-1`. Then inspect Device Manager and run Razer software in the guest. Windows 11 Pro (build 26100), USB/IP client 0.9.8.1, all five HID interfaces, and the original Razer updater DLL’s version query are verified. Synapse 4 (4.0.86.2610281118) recognizes the device and opens its profiles and keyboard settings. Gaming-mode control, static green lighting, brightness changes and lighting disable were exercised through its normal UI; firmware-driven LED-register changes were verified. Initial product-module setup required internet access; the settings/lighting changes then passed with the internet adapter disconnected. The model also handles the firmware’s periodic LED-driver scan-recovery sequence; Windows USB/IP and the original updater query remain functional beyond that timer boundary. This is representative coverage for one captured firmware, not a claim of full Synapse compatibility. The bridge supports USB/IP 1.1.1 control transfers and interrupt IN, including STALL, short packets and queued-request cancellation; other transfer types are unsupported.

The reusable entry point is `razer_emu.usbip.UsbIpServer(keyboard, enumeration)`, followed by `await server.serve()`. Create and run it on the same thread that owns `Keyboard`. The asyncio transport advances virtual time while requests are pending or the imported device is idle; it does not synchronize virtual time to wall time. A contact held for a short virtual duration can therefore remain held long enough in guest wall time to trigger the OS auto-repeat timer. Control transfers use the bounded synchronous core API, so a control request can delay other socket work until its virtual-time deadline.

Interrupt transfers are serviced inside Renode at the descriptor's virtual polling interval, only while a guest request is queued. Socket replies are flushed after each 5 ms virtual batch. This adds up to one batch of reply latency; it does not establish an 8 kHz end-to-end network report rate.

Protocol and network references: [Linux USB/IP specification](https://docs.kernel.org/usb/usbip_protocol.html), [Linux VHCI address handling](https://github.com/torvalds/linux/blob/master/drivers/usb/usbip/vhci_hcd.c), [QEMU user networking](https://www.qemu.org/docs/master/system/devices/net.html).

### Querying through the original Razer updater DLL

The optional [native query example](examples/razer_updater_query.c) calls `OpenDevice`, `GetFWVersion` and `CloseDevice` from an original `FWUpdaterDLL.dll`. Obtain your own DLL from the BlackWidow V4 75% updater; it is not included. The example queries the version through Razer's HID implementation and contains no erase/program calls.

Build it as a **32-bit Windows executable** to match that DLL. For example, with MinGW-w64 from this checkout:

```sh
i686-w64-mingw32-gcc -O2 examples/razer_updater_query.c -o razer_updater_query.exe
```

Attach the emulated device inside the guest using the USB/IP steps above, put the executable and DLL in the same working directory, and run:

```text
razer_updater_query.exe
```

An optional argument selects a different DLL path. A successful query prints `Firmware version: 1.1.1` for the tested capture; other firmware versions are printed as returned. The native example has passed with the original Razer DLL under Wine 11.19 in a QEMU Linux guest, using the captured bootloader/application and USB/IP. The same example also passes on native Windows 11 Pro in QEMU. This verifies the updater query path. The separate Synapse UI checks above validate representative settings and lighting operations.

### Starting from the bootloader

The default start recreates the bootloader's application handoff in SRAM. To execute the captured bootloader itself, provide a fully verified 32 KiB capture at flash address zero:

```sh
razer-emu application.bin \
  --bootloader-dump ./bootloader-capture \
  --flash-dump ./application-b-capture \
  --flash-dump ./settings-capture \
  --flash-dump ./remaining-flash-capture \
  --usb-enumerate --microseconds 100000
```

The bootloader importer rejects holes, wrong region boundaries, invalid vectors and capture hash mismatches. No bootloader image is included. The library equivalent is `Keyboard(firmware, bootloader=Bootloader.load_dump(directory), flash_regions=regions)`; import `Bootloader` from `razer_emu`. This starts at the flash reset vector with zeroed SRAM and lets the original bootloader load and enter the application. In this mode, `reset()` preserves emulated flash programming and re-enters the bootloader. It does not recreate the MCU's internal ROM boot process or analog power-on timing.

The complete private bootloader and 1.1.1 application have passed cold boot and USB enumeration through this path. Normal A-key press/release is verified after both application handoff and this complete bootloader path. `--bootloader-dump` can also be combined with `--usbip`.

### Matrix contacts

A contact model can be attached before execution with `matrix = keyboard.attach_matrix()`. It uses the eight row bits and sixteen column bits recovered from the EZH program. `matrix.press(row, column)`, `matrix.release(row, column)` and `matrix.set_key(row, column, pressed)` update ideal active-low, diode-isolated contacts. Coordinates range from row 0–7 and column 0–15. `matrix.pressed` returns an immutable set of held coordinates; contacts stay held across MCU reset. After attachment, the view is also available as `keyboard.matrix`. Without a contact network, EZH still reads shared GPIO input levels and IOCON pull-ups; unconnected inputs are not forced low.

These coordinates are electrical scan positions. IOCON selects CPU GPIO (function 0) or EZH (function 15) as the driver of each matrix row; both input paths observe the shared contacts. Other alternate functions do not drive the modeled matrix. Physical matrix key/HID mapping remains incomplete. With the captured default binding, contact (3, 1) produces A-key press and release reports on endpoint `0x81` after normal startup. The regression covers application handoff and complete bootloader execution, verifies the application bytes remain unchanged, and uses no scanner-entry override or ready-flag writes. Matrix tests also cover input paths, pin selection and electrical row scanning; the full physical layout has not been verified.

### Media-wheel phases

Attach the encoder before execution with `encoder = keyboard.attach_encoder()`. Its two inputs are GPIO1 pins 2 and 3, as read by the original firmware. `encoder.set_phases(phase_a, phase_b)` accepts booleans, and `encoder.phases` returns their immutable pair. Both inputs start high unless you pass `phase_a=` and `phase_b=` to `attach_encoder()`. They survive MCU reset. The same view is available as `keyboard.encoder`.

A phase sequence `(True, True) → (True, False) → (False, False) → (False, True) → (True, True)` represents one electrical cycle; reverse it for the other direction. Advance virtual time between edges so firmware can sample them. Physical contact timing and clockwise mapping remain unverified, so the API does not supply a timed turn gesture. With the captured default media bindings, normal firmware execution generates volume-down reports for the sequence above and volume-up reports for the reverse, followed by release reports on endpoint `0x82`. The regression test allows 5 ms of virtual sampling per edge. One full electrical cycle generates two actions; its relationship to physical detents is unknown.

### Independent buttons

`buttons = keyboard.attach_buttons()` attaches the two active-low inputs from the firmware's independent-button table before execution. `buttons.press(index)`, `buttons.release(index)` and `buttons.set_pressed(index, pressed)` control indices 0 and 1. `buttons.pressed` returns an immutable set, and held contacts survive MCU reset. The view is also available as `keyboard.buttons`.

Index 0 drives GPIO1 pin 28 (internal key C8); index 1 drives GPIO1 pin 26 (internal key F9). These are electrical/table identities, not an established physical connector pinout. With the captured default profile, index 0 produces mute and release reports through the original firmware. Actions remain firmware/profile controlled. Allow virtual time for firmware debounce; the test holds and releases for 40 ms each. Index 1 has a special firmware binding and is not assigned a guessed HID usage by this API. Native Renode buttons provide the contact signals.

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
- The application is loaded at runtime address `0x20000000` and flash-storage address `0x8000`. Its reset vector and stack pointer come from the original image. With a complete `--bootloader-dump`, execution instead starts in checked flash and the original bootloader performs the application handoff.
- Small C# models implement the observed clock/power control registers, flash-controller read/erase/program operations, four-channel match timers, DMA transfers, Flexcomm I²C master operations, watchdog feeding/countdown, and EZH control registers. EZH supports initial mailbox/stack/GPIO operations, heartbeat synchronization, basic arithmetic, fused shift/ALU operations, branches/calls, tight loops, Boolean detection and HOLD/vector dispatch. Its interrupt outputs and mailbox handshake are modeled, but the board's trigger/interrupt routing, emergency-vector execution and much of its instruction set remain incomplete.
- Unknown bus accesses and unsupported operations are checked at the selected interval: normally 10 virtual microseconds in the library, 125 µs in the GUI and USB/IP CLI. Several accesses can occur within that interval before execution stops.
- C# handles bounded execution, queued USB polling and the EZH interpreter. EZH folds eligible self-branches while preserving instruction/cycle counts and heartbeat boundaries. Watchdog feeds update a lazy deadline and use a cached registered bus writer on an uninstrumented board; known debugger, watchpoint, hook and mapping conditions select Renode's regular dispatch path. No native extension or separate C compiler is required for these optimizations.
- DMA0 executes SRAM descriptors with peripheral request gating, linked transfers, completion/error interrupts and aborts. Flexcomm I2C generates DMA requests for attached targets; absent targets NACK. Hardware trigger routing and bus arbitration timing remain incomplete. An explicitly configured CHROMA 192-compatible model implements page unlocking, 192 PWM channels, 24 enable bytes, global current, software shutdown and read-triggered reset.
- The USB PHY implements control aliases and functional power/PLL readiness. The IP3511 HS controller implements bus reset, SETUP/IN/OUT packet DMA, completion interrupts, endpoint aborts, byte-count tracking and double buffering, backed by 16 KiB of USB RAM. A virtual host starts periodic SOF interrupts after bus reset, with a wrapping 11-bit frame counter. USB/IP exposes control and interrupt IN transfers. Isochronous traffic, suspend/resume and LPM remain incomplete.
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

The synthetic Cortex-M33 fixture checks stack-limit register readback, floating-point execution, periodic MR1 interrupts, acknowledgement, exception return, reset and unsupported-access reporting. USB tests cover packet DMA, short transfers, completion/abort behavior and virtual deadlines. Private-image tests target application 1.1.1 and validate descriptor reads, configuration, STALL recovery and a 90-byte Razer feature-report exchange; normal-boot input tests validate matrix key, volume and mute reports from encoder phases and button contacts with the captured default media bindings. Normal matrix A-key press/release is tested through both application handoff and complete bootloader startup. A 6.5-second virtual run checks the lighting driver’s periodic test-register recovery; focused tests also check protection and rejection of unsupported test/trim configurations. Set `RAZER_TEST_BOOTLOADER_DUMP` to a complete bootloader capture to include native cold-boot/reset validation. Set `RAZER_TEST_FLASH_DUMPS` to verified capture directories separated by your operating system's path separator (`:` on Linux, `;` on Windows).

## Remaining milestones

1. Extend normal matrix-input validation to more bindings, chords, rollover and reset.
2. Validate the firmware-derived ANSI key/RGB map against physical hardware, add other layouts, and establish encoder detent/direction mapping.
3. Complete remaining peripheral and USB power-state behavior.
4. Broaden Windows/Razer software coverage beyond the verified Synapse recognition, gaming mode, static lighting, brightness and lighting-disable operations. USB suspend/resume, firmware updating and other products remain outside the verified set.

MIT licensed. Third-party components retain their respective licenses.
