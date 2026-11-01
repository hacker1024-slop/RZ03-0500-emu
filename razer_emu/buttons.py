"""The two independent active-low contacts read by the original firmware."""

# GPIO1 pins 28 and 26; firmware maps them to internal keys C8 and F9.
BUTTON_PINS = (60, 58)


class Buttons:
    """Indexed contacts backed by Renode buttons, with firmware-selected actions.

    Indices describe the firmware input table, not physical positions or HID
    usages. Use on the Keyboard's owning thread and advance time for debounce.
    """

    def __init__(self, keyboard, backends):
        self._keyboard = keyboard
        self._backends = tuple(backends)

    @property
    def pressed(self):
        self._keyboard.state()
        return frozenset(i for i, button in enumerate(self._backends) if button.Pressed)

    def set_pressed(self, index, pressed):
        self._keyboard.state()
        if type(index) is not int or not 0 <= index < len(self._backends):
            raise ValueError("Button index must be 0 or 1")
        if type(pressed) is not bool:
            raise ValueError("Button state must be a boolean")
        button = self._backends[index]
        button.Press() if pressed else button.Release()

    def press(self, index):
        self.set_pressed(index, True)

    def release(self, index):
        self.set_pressed(index, False)
