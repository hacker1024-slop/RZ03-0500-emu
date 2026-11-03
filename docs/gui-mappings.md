# BlackWidow V4 75% GUI mappings

The included map describes the ANSI layout of the BlackWidow V4 75%. The matrix and RGB records match both the 1.1.1 capture and the 1.2.1 updater application. The GUI uses the hardware map independently of firmware version.

## Inputs

The application's table at SRAM address `0x20014F30` in 1.1.1 and `0x20014E74` in 1.2.1 contains eight rows of sixteen scan IDs. The GUI joins physical key names to IDs using the captured default HID bindings and the ANSI legends, then locates each ID in that scan table. For example A is ID `0x1F`, electrical contact `(3,1)`. The GUI does not inject HID packets: input changes the modeled diode matrix and original firmware debounces it and generates USB reports. Fn is an actual matrix contact, not a synthesized modifier.

The independent buttons use GPIO1 pins 28 and 26, mapped by firmware to IDs `0xC8` and `0xF9`. They are displayed as mute and media play/pause using the manufacturer's control labels. Their actions remain firmware/profile dependent.

## RGB

At `0x20015772` in 1.1.1 and `0x200156B6` in 1.2.1, the application stores seven-byte records in seven groups of eighteen slots. Fields are key ID, two effect-position coordinates, controller index, and three PWM channel offsets. The routine near `0x20008664` uses the last three fields for RGB writes into each controller's 192-byte buffer. The effect-position coordinates are not interchangeable with scan contacts or host LED-frame columns.

The controller address bytes at `0x20014F00` begin `A0 BE A6`: 7-bit addresses `0x50`, `0x5F`, `0x53` on Flexcomm4. The GUI attaches these compatible lighting models; the third supports the board's underglow. Per-key RGB uses the first two. All 18 underglow records occupy row 6 and use the third controller. The GUI represents them as nine segments per side, with approximate placement on the image. The RGB records match both 1.1.1 and 1.2.1. A uses controller 0 and channels `(161,145,177)` in red/green/blue order. Existing firmware-driven static-green snapshots independently show its green channel enabled while its red/blue channels are zero.

Colours apply the channel enable mask, global current and shutdown state. They are normalized register levels, not calibrated emitted light.

## Physical view and image

The GUI uses an AI-generated illustration of the unlit ANSI keyboard.
Rectangular hit regions align with the pictured controls. The original
firmware supplies the overlay colors through the emulated lighting controllers.


## Validation

Tests check all 83 displayed key/control IDs, 81 unique matrix contacts, the two independent buttons, RGB current/enable/shutdown handling and every record against the private application tables when supplied. An offscreen Qt Quick test clicked A through the photo overlay and observed original-firmware USB A press and release after cold boot, without instruction patches or readiness overrides. The UI/core boundary uses local JSON messages; all Renode calls stay in the worker's owning thread/process. Electrical mappings are firmware-derived; physical wiring and optical output have not been measured on a keyboard for this GUI.
