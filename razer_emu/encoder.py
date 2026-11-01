"""Electrical phase inputs for the firmware's media-wheel decoder."""

# Original 1.1.1 reader at 0x200130B8 uses port 1, pins 2 and 3.
ENCODER_PINS = (34, 35)


class Encoder:
    """Two digital contacts, without assigning physical rotation direction.

    Set phases on the Keyboard's owning thread and advance virtual time between
    edges so firmware can sample them. This object supplies input levels only;
    firmware performs quadrature decoding and chooses the resulting action.
    """

    def __init__(self, keyboard, gpio, phase_a, phase_b):
        self._keyboard = keyboard
        self._gpio = gpio
        self.set_phases(phase_a, phase_b)

    @property
    def phases(self):
        self._keyboard.state()
        return self._phases

    def set_phases(self, phase_a, phase_b):
        self._keyboard.state()
        if type(phase_a) is not bool or type(phase_b) is not bool:
            raise ValueError("Encoder phases must be booleans")
        self._gpio.OnGPIO(ENCODER_PINS[0], phase_a)
        self._gpio.OnGPIO(ENCODER_PINS[1], phase_b)
        self._phases = (phase_a, phase_b)
