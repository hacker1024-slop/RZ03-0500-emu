# Hardware model evidence and assumptions

Working target: BlackWidow V4 75%, USB 1532:02A5, application 1.1.1; MCU LPC5528JBD100 identified at model level by a published teardown. Pin assignments and the external LED controller remain to be established.

Primary register-layout reference: [NXP LPC5528 header](https://github.com/nxp-mcuxpresso/mcux-sdk/blob/main/devices/LPC5528/LPC5528.h). NXP's [power driver](https://github.com/nxp-mcuxpresso/mcux-sdk/blob/main/devices/LPC5528/drivers/fsl_power.c) identifies the NMPA factory power-profile addresses and fallback constants. Code addresses below refer to the captured 1.1.1 application, loaded at `0x20000000`.

| Component | Current model and evidence | Remaining limits |
| --- | --- | --- |
| Cortex-M33 | Renode 1.17.0. Original reset executes MSR MSPLIM. Synthetic tests check MRS readback, FPU and interrupt entry/return. | Not a silicon timing model. TrustZone disabled for the LPC5528 target. |
| SRAM | 192 KiB at 0x20000000, 32 KiB at 0x04000000. Vectors select stack 0x04008000. | Other aliases and retention not implemented. |
| Flash | 512 KiB memory backing, application at 0x8000. Flash controller command 2 initializes access timing; command 3 reads a 16-byte word and raises DONE. | No bootloader execution, erase/program command implementation or persistent backing yet. Unloaded flash starts at 0xFF, not specimen data. |
| NMPA | 0x9FC00–0xA0000 backed by zeros, representing unavailable trims. Firmware checks validity bits and uses its own fallback path. | Virtual defaults, not measured factory values. Writes are not protected yet. |
| SYSCON +0xFE0 | Firmware at 0x200098BE reads low 9 bits and selects flash-related geometry from a table. Token 2 selects 0x80000. | Undocumented in the current public header. Token 2 is a virtual 512 KiB configuration, not a live silicon-ID measurement. |
| SYSCON +0x2EC | Firmware at 0x20002352 consumes bit 0 in a timing calculation. Model returns 0. | Reserved in the current public header; bit-clear is an explicit bring-up assumption requiring confirmation. |
| SYSCON/PMC/ANACTRL | Observed control-register read/write, set/clear aliases, and oscillator-output configuration. DCDC fallback constants follow NXP SDK. | Clock/reset outputs and analog settling not modeled. Unsupported offsets are reported. |
| IOCON/GPIO | IOCON stores pin configuration. GPIO wraps Renode LPC43xx shared word-register layout with LPC55 per-pin byte and word aliases. IOCON pull-up/down settings drive input defaults. | Pin mux and matrix wiring incomplete. Only implemented two-port registers are accepted; direction set/clear aliases forthcoming. |
| CTIMER2 | Uses four Renode ComparingTimer instances, 96 MHz source, prescaler, match interrupts/reset/stop and W1C interrupt status. Firmware uses MR1. | Capture, external match, counter and PWM operations stop as unsupported when enabled. Dynamic clock switching not implemented. |
| DMA0 | Channel and common configuration registers. | Transfer triggers stop as unsupported. Descriptor execution, request lines and IRQs forthcoming. |
| Flexcomm2/4 | LPC I²C master registers and pending-state transitions; absent target addresses NACK. | DMA request handshake and external LED controller forthcoming. |

The emulator changes peripheral state, not application instructions. Do not replace a missing model with a patch that skips initialization or fakes USB replies. Bootloader emulation requires a valid bootloader image and a separate boot path; do not interpret unreadable dump bytes as code.
