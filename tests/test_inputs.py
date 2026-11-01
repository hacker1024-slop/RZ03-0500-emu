import pytest

from razer_emu import Firmware, InputScript
from razer_emu.core import Keyboard


@pytest.mark.parametrize(
    "events",
    [
        {},
        [{"at_us": 0, "type": "keyboard", "pressed": True}],
        [{"at_us": 0, "type": "matrix", "row": 8, "column": 0, "pressed": True}],
        [{"at_us": 0, "type": "button", "index": 0, "pressed": 1}],
        [{"at_us": True, "type": "encoder", "phase_a": True, "phase_b": False}],
        [{"at_us": -1, "type": "button", "index": 0, "pressed": True}],
        [{"at_us": 0, "type": "button", "index": 0, "pressed": True, "presssed": True}],
        [
            {"at_us": 2, "type": "button", "index": 0, "pressed": True},
            {"at_us": 1, "type": "button", "index": 0, "pressed": False},
        ],
    ],
)
def test_invalid_input_scripts_are_rejected(events):
    with pytest.raises(ValueError):
        InputScript(events)


def test_input_events_split_virtual_quanta_and_reset_cancels_future_events(cpu_image):
    script = InputScript(
        [
            {"at_us": 5, "type": "matrix", "row": 3, "column": 1, "pressed": True},
            {"at_us": 7, "type": "encoder", "phase_a": False, "phase_b": True},
            {"at_us": 7, "type": "button", "index": 0, "pressed": True},
            {"at_us": 9, "type": "matrix", "row": 3, "column": 1, "pressed": False},
            {"at_us": 13, "type": "button", "index": 0, "pressed": False},
        ]
    )
    with Keyboard(Firmware.load(cpu_image)) as keyboard:
        playback = keyboard.prepare_input_script(script)
        keyboard.run_for(3)
        playback.start()
        keyboard.run_for(4)
        assert not keyboard.matrix.pressed
        keyboard.run_for(1)
        assert keyboard.matrix.pressed == frozenset({(3, 1)})
        keyboard.run_for(2)
        assert keyboard.encoder.phases == (False, True)
        assert keyboard.buttons.pressed == frozenset({0})
        keyboard.run_for(2)
        assert not keyboard.matrix.pressed and playback.applied == 4
        keyboard.reset()
        assert not playback.running
        keyboard.run_for(100)
        assert keyboard.buttons.pressed == frozenset({0})


def test_equal_time_events_keep_order_and_do_not_force_release(cpu_image):
    script = InputScript(
        [
            {"at_us": 0, "type": "button", "index": 0, "pressed": True},
            {"at_us": 0, "type": "button", "index": 0, "pressed": False},
            {"at_us": 0, "type": "button", "index": 1, "pressed": True},
        ]
    )
    with Keyboard(Firmware.load(cpu_image)) as keyboard:
        playback = keyboard.prepare_input_script(script)
        playback.start()
        assert playback.applied == 3 and not playback.running
        assert keyboard.buttons.pressed == frozenset({1})
