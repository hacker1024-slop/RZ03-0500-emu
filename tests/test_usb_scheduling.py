from test_usb import BUFFER, _arm, _array
from test_usb import usb_board as usb_board

from razer_emu import InputScript


def make_pump(usb_board):
    keyboard, bus, device = usb_board
    from Antmicro.Renode.Peripherals.USB import RazerUsbInterruptPump

    return RazerUsbInterruptPump(device)


def drain(pump):
    return [(int(x.Sequence), int(x.Status), bytes(x.Data), int(x.AtUs)) for x in pump.Drain()]


def test_input_event_does_not_shift_interrupt_poll_deadline(usb_board):
    keyboard, bus, device = usb_board
    script = InputScript([{"at_us": 7, "type": "button", "index": 0, "pressed": True}])
    keyboard.prepare_input_script(script).start()
    bus.WriteBytes(_array(bytes(range(16))), BUFFER)
    _arm(bus, 3, BUFFER, 16, flags=0x8C000000)
    pump = make_pump(usb_board)
    pump.Add(1, 1, 8, 0, 8, 125)
    pump.Add(2, 1, 8, 0, 8, 125)
    keyboard.run_for(250, _usb_pump=pump)
    assert drain(pump) == [(1, 0, bytes(range(8)), 0), (2, 0, bytes(range(8, 16)), 125)]


def test_endpoint_polling_phases_survive_separate_bounded_runs(usb_board):
    keyboard, bus, device = usb_board
    bus.WriteBytes(_array(bytes(range(16))), BUFFER)
    bus.WriteBytes(_array(bytes(range(16, 32))), BUFFER + 64)
    _arm(bus, 3, BUFFER, 16, flags=0x8C000000)
    _arm(bus, 5, BUFFER + 64, 16, flags=0x8C000000)
    pump = make_pump(usb_board)
    pump.Add(1, 1, 8, 0, 8, 125)
    pump.Add(2, 1, 8, 0, 8, 125)
    keyboard.run_for(7, _usb_pump=pump)
    pump.Add(3, 2, 8, 0, 8, 125)
    pump.Add(4, 2, 8, 0, 8, 125)
    keyboard.run_for(250, _usb_pump=pump)
    assert drain(pump) == [
        (1, 0, bytes(range(8)), 0),
        (3, 0, bytes(range(16, 24)), 7),
        (2, 0, bytes(range(8, 16)), 125),
        (4, 0, bytes(range(24, 32)), 132),
    ]


def test_cancel_partial_request_keeps_acked_packet_consumed(usb_board):
    from Antmicro.Renode.Time import TimeInterval

    keyboard, bus, device = usb_board
    bus.WriteBytes(_array(bytes(range(16))), BUFFER)
    _arm(bus, 3, BUFFER, 16, flags=0x8C000000)
    pump = make_pump(usb_board)
    pump.BeginBatch(0)
    pump.Add(1, 1, 16, 0, 8, 125)
    pump.Add(2, 1, 8, 0, 8, 125)
    pump.ServiceHook(TimeInterval.FromMicroseconds(0))
    assert drain(pump) == []
    assert pump.Cancel(1)
    pump.ServiceHook(TimeInterval.FromMicroseconds(125))
    assert drain(pump) == [(2, 0, bytes(range(8, 16)), 125)]
    assert pump.PendingCount == 0


def test_long_interval_endpoint_resumes_after_multiple_empty_batches(usb_board):
    keyboard, bus, device = usb_board
    bus.WriteBytes(_array(bytes(range(16))), BUFFER)
    _arm(bus, 3, BUFFER, 16, flags=0x8C000000)
    pump = make_pump(usb_board)
    pump.Add(1, 1, 8, 0, 8, 1000)
    pump.Add(2, 1, 8, 0, 8, 1000)
    for duration in [7, 27, 222, 499, 300]:
        keyboard.run_for(duration, _usb_pump=pump)
    assert drain(pump) == [(1, 0, bytes(range(8)), 0), (2, 0, bytes(range(8, 16)), 1000)]


def test_native_usbip_closes_on_virtual_device_disconnect(usb_board):
    import asyncio

    import pytest
    from test_usbip import _enumeration, _submit

    from razer_emu.usb import UsbError
    from razer_emu.usbip import UsbIpServer

    keyboard, bus, device = usb_board
    bridge = UsbIpServer(keyboard, _enumeration())

    class Writer:
        def write(self, data):
            pass

        async def drain(self):
            await asyncio.sleep(0)

    async def scenario():
        reader = asyncio.StreamReader()
        reader.feed_data(_submit(1, endpoint=1, length=8))
        task = asyncio.create_task(bridge._transfers(reader, Writer()))
        try:
            for _ in range(5):
                await asyncio.sleep(0)
            device.VbusPresent = False
            with pytest.raises(UsbError, match="disconnected"):
                await asyncio.wait_for(task, 0.15)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
