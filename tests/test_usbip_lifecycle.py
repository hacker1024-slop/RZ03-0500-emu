import asyncio
import os
import struct

import pytest
from test_usbip import _connect, _submit

from razer_emu import Firmware, FlashRegion
from razer_emu.core import Keyboard
from razer_emu.usbip import UsbIpServer


def test_native_usbip_server_cancellation_closes_import_and_listener(cpu_image, monkeypatch):
    firmware = os.environ.get("RAZER_TEST_FIRMWARE")
    captures = os.environ.get("RAZER_TEST_FLASH_DUMPS")
    if not firmware or not captures:
        pytest.skip("Set private firmware and flash capture paths")
    regions = [FlashRegion.load_dump(path) for path in captures.split(os.pathsep)]
    with Keyboard(Firmware.load(firmware), flash_regions=regions) as keyboard:
        enumeration = keyboard.usb.enumerate_device()
        bridge = UsbIpServer(keyboard, enumeration)

        async def scenario():
            ready = asyncio.get_running_loop().create_future()
            real_start = asyncio.start_server

            async def start_server(*args, **kwargs):
                server = await real_start(*args, **kwargs)
                ready.set_result(server.sockets[0].getsockname()[1])
                return server

            monkeypatch.setattr(asyncio, "start_server", start_server)
            serving = asyncio.create_task(bridge.serve(port=0))
            writer = None
            try:
                port = await asyncio.wait_for(ready, 5)
                reader, writer = await _connect(port)
                assert struct.unpack("!HHI", await reader.readexactly(8)) == (0x111, 3, 0)
                await reader.readexactly(312)
                writer.write(_submit(99, endpoint=1, length=8))
                await writer.drain()
                for _ in range(5):
                    await asyncio.sleep(0)
                assert bridge._imported and bridge._clients
                serving.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(serving, 5)
                assert not bridge._imported and not bridge._clients
                await asyncio.wait_for(reader.read(), 2)
                assert reader.at_eof()
                with pytest.raises(ConnectionRefusedError):
                    await asyncio.open_connection("127.0.0.1", port)
            finally:
                serving.cancel()
                await asyncio.gather(serving, return_exceptions=True)
                if writer is not None:
                    writer.close()
                    await writer.wait_closed()

        asyncio.run(scenario())
