import pytest
from test_usb import BUFFER, _arm, _array
from test_usb import usb_board as usb_board


def pump_board(usb_board, data, *, flags=0x8C000000):
    k, b, d = usb_board
    from Antmicro.Renode.Peripherals.USB import RazerUsbInterruptPump
    from Antmicro.Renode.Time import TimeInterval

    b.WriteBytes(_array(data), BUFFER)
    _arm(b, 3, BUFFER, len(data), flags=flags)
    pump = RazerUsbInterruptPump(d)
    pump.BeginBatch(1000)

    def service(us):
        pump.ServiceHook(TimeInterval.FromMicroseconds(us))

    return pump, service, b, d


def results(pump):
    return [(int(x.Sequence), int(x.Status), bytes(x.Data), int(x.AtUs)) for x in pump.Drain()]


def test_fifo_requests_observe_endpoint_interval(usb_board):
    pump, service, b, d = pump_board(usb_board, bytes(range(16)))
    pump.Add(1, 1, 8, 0, 8, 125)
    pump.Add(2, 1, 8, 0, 8, 125)
    service(0)
    assert results(pump) == [(1, 0, bytes(range(8)), 1000)]
    service(124)
    assert results(pump) == []
    service(125)
    assert results(pump) == [(2, 0, bytes(range(8, 16)), 1125)]
    assert pump.PendingCount == 0


@pytest.mark.parametrize(
    "length,flags,status,expected",
    [(16, 0, 0, bytes(range(9))), (16, 1, -121, bytes(range(9))), (4, 0, -75, bytes(range(4)))],
)
def test_short_and_overflow_transfers(usb_board, length, flags, status, expected):
    pump, service, b, d = pump_board(usb_board, bytes(range(9)))
    pump.Add(1, 1, length, flags, 8, 125)
    service(0)
    service(125)
    assert results(pump) == [(1, status, expected, 1000 if length == 4 else 1125)]


def test_no_request_or_cancel_never_consumes_packet(usb_board):
    pump, service, b, d = pump_board(usb_board, b"12345678")
    pump.Add(1, 1, 8, 0, 8, 125)
    assert pump.Cancel(1)
    assert not pump.Cancel(1)
    service(0)
    assert results(pump) == []
    assert bytes(d.HostIn(1, 8).Data) == b"12345678"


def test_cancel_head_preserves_next_request(usb_board):
    pump, service, b, d = pump_board(usb_board, b"12345678")
    pump.Add(1, 1, 8, 0, 8, 125)
    pump.Add(2, 1, 8, 0, 8, 125)
    pump.Cancel(1)
    service(0)
    assert results(pump) == [(2, 0, b"12345678", 1000)]


def test_partial_transfer_stall_matches_usbip_behavior(usb_board):
    pump, service, b, d = pump_board(usb_board, bytes(range(16)))
    pump.Add(1, 1, 16, 0, 8, 125)
    service(0)
    assert results(pump) == []
    _arm(b, 3, BUFFER, 8, flags=0xA0000000)
    service(125)
    assert results(pump) == [(1, -32, b"", 1125)]
