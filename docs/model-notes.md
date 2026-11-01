# Hardware model evidence and assumptions

Working target: BlackWidow V4 75%, USB 1532:02A5, application 1.1.1; MCU LPC5528JBD100 identified at model level by a published teardown. Pin assignments and the external LED controller remain to be established.

Primary register-layout reference: [NXP LPC5528 header](https://github.com/nxp-mcuxpresso/mcux-sdk/blob/main/devices/LPC5528/LPC5528.h). NXP's [power driver](https://github.com/nxp-mcuxpresso/mcux-sdk/blob/main/devices/LPC5528/drivers/fsl_power.c) identifies the NMPA factory power-profile addresses and fallback constants. Code addresses below refer to the captured 1.1.1 application, loaded at `0x20000000`.

| Component | Current model and evidence | Remaining limits |
| --- | --- | --- |
| Cortex-M33 | Renode 1.17.0. Original reset executes MSR MSPLIM. Synthetic tests check MRS readback, FPU and interrupt entry/return. | Not a silicon timing model. TrustZone disabled for the LPC5528 target. |
| SRAM | 192 KiB at 0x20000000, 32 KiB at 0x04000000. Vectors select stack 0x04008000. | Other aliases and retention not implemented. |
| Flash | 512 KiB memory backing, application at 0x8000. Flash controller supports read-mode setup, 16-byte reads, page erase, blank check, page-buffer writes and 512-byte page programming. Unprogrammed words have invalid ECC. | No bootloader execution or persistent backing yet. CPU reads of erased words bus-fault; controller reads return zeros with ECC_ERR. Programming an already programmed page is rejected. Erase/program completion is immediate, not cycle accurate. |
| NMPA | 0x9FC00–0xA0000 backed by zeros, representing unavailable trims. Firmware checks validity bits and uses its own fallback path. | Virtual defaults, not measured factory values. Writes are not protected yet. |
| SYSCON +0xFE0 | Firmware at 0x200098BE reads low 9 bits and selects flash-related geometry from a table. Token 2 selects 0x80000. | Undocumented in the current public header. Token 2 is a virtual 512 KiB configuration, not a live silicon-ID measurement. |
| SYSCON +0x2EC | Firmware at 0x20002352 consumes bit 0 in a timing calculation. Model returns 0. | Reserved in the current public header; bit-clear is an explicit bring-up assumption requiring confirmation. |
| SYSCON/PMC/ANACTRL | Observed control-register read/write, set/clear aliases, and oscillator-output configuration. DCDC fallback constants follow NXP SDK. | Clock/reset outputs and analog settling not modeled. Unsupported offsets are reported. |
| IOCON/GPIO | IOCON stores pin configuration. GPIO wraps Renode LPC43xx shared word-register layout with LPC55 per-pin byte and word aliases. IOCON pull-up/down settings drive input defaults. | Pin mux and matrix wiring incomplete. Only implemented two-port registers are accepted; direction set/clear aliases forthcoming. |
| CTIMER2/3 | Each uses four Renode ComparingTimer instances, 96 MHz source, prescaler, match interrupts/reset/stop and W1C interrupt status. Firmware uses MR1. CTIMER3 connects to NVIC IRQ13; CTIMER2 to IRQ36. | Capture, external match, counter and PWM operations stop as unsupported when enabled. Dynamic clock switching not implemented. |
| USB port mode | Stores USBHSH PORTMODE at 0x400A3050. | Register storage only; no USB controller or device transactions yet. |
| DMA0 | Channel and common configuration registers. | Transfer triggers stop as unsupported. Descriptor execution, request lines and IRQs forthcoming. |
| Flexcomm2/4 | LPC I²C master registers and pending-state transitions; absent target addresses NACK. | DMA request handshake and external LED controller forthcoming. |

The emulator changes peripheral state, not application instructions. Do not replace a missing model with a patch that skips initialization or fakes USB replies. Bootloader emulation requires a valid bootloader image and a separate boot path; do not interpret unreadable dump bytes as code.

Flash behavior references: [NXP explanation of erased memory and ECC](https://community.nxp.com/t5/LPC-Microcontrollers/Default-Flash-Memory-state/m-p/1052473), [LPC55 HAL flash driver](https://github.com/lpc55/lpc55-hal/blob/master/src/drivers/flash.rs). Raw inspection reads bypass ECC faults; firmware reads do not.

| Additional component | Status |
| --- | --- |
| WWDT | Renode LimitTimer at FRO 1 MHz / 4, AA/55 feed sequence, reload, feed-window check and expiration. Expiration stops the core as `watchdog_expired`; boot-ROM reset and warning IRQ remain unimplemented. |
| EZH/SmartDMA | Control and mailbox registers at 0x4001D000, documented by [NXP AN14650](https://www.nxp.com/docs/en/application-note/AN14650.pdf). A keyed START write starts the virtual-time instruction engine; unimplemented instructions stop explicitly. Original firmware requests entry 0x20001001 with mailbox 0x20026872. This is a separate custom-ISA coprocessor, not ARM or the newer RISC-V EZH-V. |

Flash captures are external inputs. A fully valid 16-byte capture word is treated as programmed; a byte-validity mask is not a measurement of physical ECC cells. For captures spanning the application, the explicitly supplied application image takes precedence. CPU reset preserves simulated nonvolatile data; it reloads the selected application handoff. Both image bytes and simulated erase/program effects remain private unless a caller explicitly exports them.

## EZH backend assessment

[ErichStyger's QEMU fork](https://github.com/ErichStyger/qemu), inspected at revision `6a14bf40bd46fea99dbf240340afb4acd69b924c`, contains an architecture-B target, disassembler and QMP register access. Its decoder currently lists eight instruction patterns. It lacks `E_PER_READ`, shift operations and heartbeat timing used in this firmware's startup, and its standalone MCXN947 board's peripheral reads/writes are placeholders. It is a useful reference and potential future backend, but does not yet replace a complete LPC5528 platform. The target files are LGPL-2.1-or-later; its machine implementation is GPL-2.0-or-later. No fork source has been copied into this repository.

The Renode peripheral interprets an initial instruction subset using its existing bus and virtual timers. It executes the original EZH mailbox setup, stack-pointer load, input-buffer pointer load and GPIO register initialization. Heartbeat/wait synchronization, basic arithmetic, immediate/register branches, scheduled branches with two delay slots, and absolute subroutine calls are implemented. Synthetic tests cover synchronization, delayed PC writes and call/return behavior. Instruction timing and pipeline interactions are functional approximations.

With the private settings captures, the current original-image checkpoint is 42 completed EZH instructions, stopping at `0x20001614` at `0x200010A8`, decoded as an interrupt-trigger instruction. This lies in an apparent inline pointer table; its intended control flow remains unresolved. No table entries or application instructions are patched. At this checkpoint (18.56 ms), the Cortex-M33 has executed 1,781,760 instructions with no reported CPU fault. Interrupt routing, Boolean detection, the remaining instruction set and physical EZH I/O wiring remain incomplete. This checkpoint is not a working matrix scanner.

The decoded initialization sets eight EZH output bits (`7, 1, 17, 16, 23, 10, 15, 31`) and clears sixteen direction bits (`30, 29, 19, 6, 4, 3, 2, 25, 22, 21, 24, 28, 26, 18, 9, 27`). This is consistent with an 8×16 scan matrix; that is an inference, not a verified key-to-pin mapping. MCU pin-mux assignments, physical key positions and the scanning loop still need validation.
