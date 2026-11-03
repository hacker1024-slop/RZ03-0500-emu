# BlackWidow V4 75% GUI mappings

The included map describes the ANSI layout of the BlackWidow V4 75%. The matrix and RGB records match both the 1.1.1 capture and the 1.2.1 updater application. The GUI uses the hardware map independently of firmware version.

## Inputs

The application's table at SRAM address `0x20014F30` in 1.1.1 and `0x20014E74` in 1.2.1 contains eight rows of sixteen scan IDs. The GUI joins physical key names to IDs using the captured default HID bindings and the ANSI legends, then locates each ID in that scan table. For example A is ID `0x1F`, electrical contact `(3,1)`. The GUI does not inject HID packets: input changes the modeled diode matrix and original firmware debounces it and generates USB reports. Fn is an actual matrix contact, not a synthesized modifier.

The independent buttons use GPIO1 pins 28 and 26, mapped by firmware to IDs `0xC8` and `0xF9`. They are displayed as mute and media play/pause using the manufacturer's control labels. Their actions remain firmware/profile dependent.

## RGB

At `0x20015772` in 1.1.1 and `0x200156B6` in 1.2.1, the application stores seven-byte records in seven groups of eighteen slots. Fields are key ID, two effect-position coordinates, controller index, and three PWM channel offsets. The routine near `0x20008664` uses the last three fields for RGB writes into each controller's 192-byte buffer. The effect-position coordinates are not interchangeable with scan contacts or host LED-frame columns.

The controller address bytes at `0x20014F00` begin `A0 BE A6`: 7-bit addresses `0x50`, `0x5F`, `0x53` on Flexcomm4. The GUI attaches these compatible lighting models; the third supports the board's underglow. Per-key RGB uses the first two. All 18 underglow records occupy row 6 and use the third controller. The GUI represents them as nine segments per side, with approximate placement on the image. The RGB records match both 1.1.1 and 1.2.1. A uses controller 0 and channels `(161,145,177)` in red/green/blue order. Existing firmware-driven static-green snapshots independently show its green channel enabled while its red/blue channels are zero.

Colours apply the channel enable mask, global current and shutdown state. They are normalized register levels, not calibrated emitted light. The background photograph's baked-in lighting is decorative and does not represent running firmware state.

## Physical view and image

Razer's public [ANSI selection geometry](https://apps.razer.com/synapse/products/677/ui/static/media/1.9d8f32eb.svg) identifies key legends and visual regions. Those identifiers are joined to the default binding IDs; its `data-row`/`data-col` attributes are **not** used as electrical contacts or blindly indexed into the firmware table. Rectangular hit regions are calibrated to the [actual top-view product image](https://apps.razer.com/synapse/products/677/ui/677_0/PluginImages/677_0_1_dashboard3x.png). They are approximate inset regions, not recovered switch boundaries. Manufacturer SVG paths, logos, application bundles and photographs are not redistributed in the source package.

The photograph is fetched separately and cached with SHA-256 `dac11c5e9df3c26530f6a98bac2c25ac51c94d3b4daee198f5498a3fd50906e8`. It remains manufacturer artwork, outside the project's MIT code license. A local image can be selected with `--background`; its framing must match the supplied map.

## Validation

Tests check all 83 displayed key/control IDs, 81 unique matrix contacts, the two independent buttons, RGB current/enable/shutdown handling and every record against the private application tables when supplied. An offscreen Qt Quick test clicked A through the photo overlay and observed original-firmware USB A press and release after cold boot, without instruction patches or readiness overrides. The UI/core boundary uses local JSON messages; all Renode calls stay in the worker's owning thread/process. Electrical mappings are firmware-derived; physical wiring and optical output have not been measured on a keyboard for this GUI.
