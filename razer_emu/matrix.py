"""Contact-level input for the inferred EZH matrix, without a guessed keymap."""

# Recovered from the original EZH program's output and direction initialization.
ROW_PINS = (7, 1, 17, 16, 23, 10, 15, 31)
COLUMN_PINS = (30, 29, 19, 6, 4, 3, 2, 25, 22, 21, 24, 28, 26, 18, 9, 27)


class Matrix:
    """Active-low, diode-isolated contacts; use on the Keyboard's owning thread.

    Rows and columns are electrical coordinates. They are not HID usages or
    physical key positions. IOCON selects CPU GPIO or EZH as the row driver.
    """

    def __init__(self, keyboard, backend):
        self._keyboard = keyboard
        self._backend = backend
        self._pressed = set()

    @property
    def rows(self):
        return len(ROW_PINS)

    @property
    def columns(self):
        return len(COLUMN_PINS)

    @property
    def pressed(self):
        self._keyboard.state()
        return frozenset(self._pressed)

    def set_key(self, row, column, pressed):
        self._keyboard.state()
        if (
            type(row) is not int
            or type(column) is not int
            or not 0 <= row < self.rows
            or not 0 <= column < self.columns
        ):
            raise ValueError("Matrix coordinate must be row 0–7, column 0–15")
        if type(pressed) is not bool:
            raise ValueError("Key state must be a boolean")
        self._backend.SetKey(row, column, pressed)
        if pressed:
            self._pressed.add((row, column))
        else:
            self._pressed.discard((row, column))

    def press(self, row, column):
        self.set_key(row, column, True)

    def release(self, row, column):
        self.set_key(row, column, False)
