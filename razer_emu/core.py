"""Frontend-independent execution and inspection of a firmware-backed keyboard."""

from dataclasses import asdict, dataclass
from importlib.resources import files
from uuid import uuid4

from .firmware import FLASH_APPLICATION, RUNTIME_BASE, Bootloader, Firmware
from .lighting import LightingController
from .matrix import COLUMN_PINS, ROW_PINS, Matrix
from .usb import UsbHost


@dataclass(frozen=True)
class State:
    pc: int
    sp: int
    instructions: int
    ezh_pc: int
    ezh_instructions: int
    ezh_holding: bool
    ezh_fault_address: int | None
    elapsed_us: int
    exception: int
    fault_status: int
    fault_address: int | None
    unknown_reads: int
    unknown_writes: int
    stopped_reason: str | None

    def to_dict(self):
        return asdict(self)


class Keyboard:
    """A synchronous core for CLI/GUI workers. Call from one owning thread.

    Fault and unsupported-access checks default to every 10 virtual microseconds.
    check_interval_us can increase this to reduce synchronization overhead; the
    GUI uses 125 us. Execution still ends at the requested virtual-time boundary.
    Runs only virtual hardware. Unknown MMIO stops a bounded run; it never implies
    a successful boot. Renode's process-global emulation advances all its machines,
    so only one Keyboard instance is currently supported in each process.
    """

    _active = False
    _models_loaded = False
    _models = (
        "syscon",
        "pmc",
        "anactrl",
        "flashController",
        "iocon",
        "ctimer2",
        "ctimer3",
        "usbPort",
        "usbPhy",
        "usbDevice",
        "dma0",
        "flexcomm4",
        "flexcomm2",
        "gpio",
        "wwdt",
        "ezh",
    )

    def __init__(
        self,
        firmware: Firmware,
        *,
        flash_regions=(),
        bootloader: Bootloader | None = None,
        check_interval_us: int = 10,
    ):
        if type(check_interval_us) is not int or not 1 <= check_interval_us <= 1000:
            raise ValueError("check_interval_us must be an integer from 1 to 1000")
        if Keyboard._active:
            raise RuntimeError("Use one Keyboard per process")
        # Renode is deliberately lazy-loaded, allowing file inspection without it.
        from pyrenode3.wrappers import Emulation, Monitor

        # isort: split
        # pyrenode3 must load the CLR before importing its namespaces.
        from Antmicro.Renode.Logging import Logger, LogLevel
        from Antmicro.Renode.Logging.Backends import MemoryBackend

        self.firmware = firmware
        self.bootloader = bootloader
        self._emulation = Emulation()
        self._engine = self._emulation.internal
        self._name = "keyboard-" + uuid4().hex
        self._log = MemoryBackend()
        Logger.AddBackend(self._log, self._name)
        self._log.SetLogLevel(LogLevel.Warning)
        self._machine = self._emulation.add_mach(self._name)
        Keyboard._active = True
        self._closed = False
        self._lighting = {}
        self._matrix = None
        self._encoder = None
        self._buttons = None
        self._input_playback = None
        try:
            if not Keyboard._models_loaded:
                for name in (
                    "CortexMTime.cs",
                    "LpcGpio.cs",
                    "LpcSyscon.cs",
                    "LpcUsbPhy.cs",
                    "LpcUsbDevice.cs",
                    "LpcTimer.cs",
                    "LpcDma.cs",
                    "LpcI2c.cs",
                    "Chroma192.cs",
                    "LpcFlash.cs",
                    "LpcWatchdog.cs",
                    "LpcEzhBoolean.cs",
                    "LpcEzh.cs",
                    "DiodeMatrix.cs",
                    "UsbInterruptPump.cs",
                    "ExecutionRunner.cs",
                    "EzhCpu.cs",
                ):
                    model = files("razer_emu").joinpath("platforms/" + name)
                    _, error = Monitor().execute(f"include @{model}")
                    if error:
                        raise RuntimeError(error)
                Keyboard._models_loaded = True
            self._machine.load_repl(str(files("razer_emu").joinpath("platforms/lpc5528.repl")))
            self._bus = self._machine.sysbus.internal
            self._cpu = self._machine.sysbus.cpu.internal
            self._modeled_peripherals = tuple(
                getattr(self._machine.sysbus, name).internal for name in self._models
            )
            from Antmicro.Renode.Peripherals.Miscellaneous import RazerLpcAnalog
            from Antmicro.Renode.Utilities import RazerExecutionRunner
            from System import Array, Byte

            self._runner = RazerExecutionRunner(
                self._engine,
                self._machine.internal,
                self._cpu,
                Array[RazerLpcAnalog](
                    [p for p in self._modeled_peripherals if isinstance(p, RazerLpcAnalog)]
                ),
                self._machine.sysbus.syscon.internal,
                self._machine.sysbus.gpio.internal,
            )
            self._runner.CheckIntervalUs = check_interval_us

            flash = self._machine.sysbus.flash.internal
            for region in flash_regions:
                flash.LoadRegion(
                    region.address,
                    Array[Byte](region.data),
                    Array[Byte](region.valid_mask),
                )
            if bootloader is not None:
                flash.LoadImage(0, Array[Byte](bootloader.image))
                flash.LoadImage(FLASH_APPLICATION, Array[Byte](firmware.image))
            self._watchdog = self._machine.sysbus.wwdt.internal
            self._cpu.ConfigureWatchdogFastPath(self._watchdog)
            self._ezh = self._machine.sysbus.ezh.internal
            self._ezh_cpu = self._machine.sysbus.ezhCpu.internal
            self._usb_device = self._machine.sysbus.usbDevice.internal
            self.usb = UsbHost(self, self._usb_device)
            self.reset()
        except BaseException:
            self.close()
            raise

    @property
    def matrix(self):
        self.state()
        return self._matrix

    @property
    def buttons(self):
        self.state()
        return self._buttons

    def attach_buttons(self):
        """Attach the two independent active-low contacts before execution."""
        from Antmicro.Renode.Peripherals.Miscellaneous import Button

        from .buttons import BUTTON_PINS, Buttons

        self._ensure_running()
        if self._elapsed_us or self._cpu.ExecutedInstructions:
            raise RuntimeError("Attach buttons before firmware execution")
        if self._buttons is not None:
            raise ValueError("Buttons already configured")
        backends = []
        for pin in BUTTON_PINS:
            button = Button(True)
            button.ReleaseOnReset = False
            button.IRQ.Connect(self._machine.sysbus.gpio.internal, pin)
            backends.append(button)
        self._buttons = Buttons(self, backends)
        return self._buttons

    @property
    def encoder(self):
        self.state()
        return self._encoder

    def attach_encoder(self, *, phase_a=True, phase_b=True):
        """Attach GPIO1 pins 2/3 before execution, initially both high by default."""
        from .encoder import Encoder

        self._ensure_running()
        if self._elapsed_us or self._cpu.ExecutedInstructions:
            raise RuntimeError("Attach the encoder before firmware execution")
        if self._encoder is not None:
            raise ValueError("Encoder already configured")
        self._encoder = Encoder(self, self._machine.sysbus.gpio.internal, phase_a, phase_b)
        return self._encoder

    def attach_matrix(self):
        """Attach the inferred 8×16 GPIO/EZH contacts before firmware execution."""
        from Antmicro.Renode.Peripherals.Input import RazerDiodeMatrix
        from System import Array, Int32

        self._ensure_running()
        if self._elapsed_us or self._cpu.ExecutedInstructions:
            raise RuntimeError("Attach the matrix before firmware execution")
        if self._matrix is not None:
            raise ValueError("Matrix already configured")
        backend = RazerDiodeMatrix(
            self._ezh,
            self._machine.sysbus.gpio.internal,
            Array[Int32](ROW_PINS),
            Array[Int32](COLUMN_PINS),
        )
        self._matrix = Matrix(self, backend)
        return self._matrix

    def attach_lighting(self, bus, address):
        """Configure an inferred CHROMA 192-compatible target before execution.

        Addresses are explicit because the populated addresses are not confirmed.
        Bus 2 or 4 selects the modeled Flexcomm I2C controller.
        """
        from Antmicro.Renode.Peripherals.LED import RazerChroma192

        self._ensure_running()
        if self._elapsed_us or self._cpu.ExecutedInstructions:
            raise RuntimeError("Attach lighting before firmware execution")
        if type(bus) is not int or bus not in (2, 4):
            raise ValueError("Lighting bus must be 2 or 4")
        if type(address) is not int or not 0x50 <= address <= 0x5F:
            raise ValueError("Lighting address must be 0x50 to 0x5F")
        if (bus, address) in self._lighting:
            raise ValueError("Lighting address already configured")
        backend = RazerChroma192()
        getattr(self._machine.sysbus, f"flexcomm{bus}").internal.Attach(address, backend)
        controller = LightingController(self, backend, bus, address)
        self._lighting[bus, address] = controller
        return controller

    @staticmethod
    def _register(value):
        from Antmicro.Renode.Peripherals.CPU import RegisterValue

        return RegisterValue.Create(value, 32)

    @staticmethod
    def _value(register):
        return int(register.RawValue)

    def _hard_pause_processors(self):
        for name in ("_cpu", "_ezh_cpu"):
            processor = getattr(self, name, None)
            if processor is not None:
                processor.HardPauseForControl()

    def reset(self):
        """Reset into the supplied bootloader, or recreate the application handoff."""
        from System import Array, Byte

        if self._closed:
            raise RuntimeError("Keyboard is closed")
        self._input_playback = None
        self._hard_pause_processors()
        self._engine.PauseAll()
        self._machine.Reset()
        self.usb._reset_timing()
        for controller in self._lighting.values():
            controller._backend.Reset()
        self._bus.WriteBytes(Array[Byte](bytes(0x30000)), RUNTIME_BASE)
        self._bus.WriteBytes(Array[Byte](bytes(0x8000)), 0x04000000)
        self._bus.WriteBytes(Array[Byte](bytes(0x4000)), 0x40100000)
        if self.bootloader is None:
            self._bus.WriteBytes(Array[Byte](self.firmware.image), RUNTIME_BASE)
            self._machine.sysbus.flash.LoadImage(
                FLASH_APPLICATION, Array[Byte](self.firmware.image)
            )
            self._cpu.VectorTableOffset = RUNTIME_BASE
            image = self.firmware
        else:
            self._cpu.VectorTableOffset = 0
            image = self.bootloader
        self._cpu.SP = self._register(image.initial_sp)
        self._cpu.PC = self._register(image.reset_vector)
        self._cpu.PerformanceInMips = 96
        self._elapsed_us = 0
        self._reason = None
        self._initial_reads = self._bus.UnexpectedReads
        self._initial_writes = self._bus.UnexpectedWrites

    def prepare_input_script(self, script):
        """Attach required inputs before execution, returning a startable playback."""
        from .inputs import InputPlayback, InputScript

        self._ensure_running()
        if self._elapsed_us or self._cpu.ExecutedInstructions:
            raise RuntimeError("Prepare input scripts before firmware execution")
        if not isinstance(script, InputScript):
            raise TypeError("Expected an InputScript")
        return InputPlayback(self, script)

    def step(self, count=1):
        if not 1 <= count <= 1000:
            raise ValueError("Step count must be between 1 and 1000")
        self._ensure_running()
        from Antmicro.Renode.Time import TimeInterval

        self._hard_pause_processors()
        self._ezh_cpu.EnterTimerDrivenMode()
        source = self._engine.MasterTimeSource
        local = self._machine.internal.LocalTimeSource
        quantum, local_quantum = source.Quantum, local.Quantum
        # Stepping leaves an unfinished grant in Renode's time handles. Bound
        # both grant layers by the smallest supported timed run, so resuming a
        # partial step cannot overrun a later run_for(1) request.
        source.Quantum = local.Quantum = TimeInterval.FromMicroseconds(1)
        try:
            self._cpu.Step(count)
        finally:
            self._engine.PauseAll()
            source.Quantum, local.Quantum = quantum, local_quantum
        return self.state()

    def run_for(self, microseconds, *, _usb_pump=None):
        """Advance bounded virtual time, checking faults at the configured interval."""
        from Antmicro.Renode.Peripherals.CPU import ExecutionMode

        if not isinstance(microseconds, int) or not 1 <= microseconds <= 10_000_000:
            raise ValueError("Duration must be an integer from 1 to 10,000,000 microseconds")
        self._ensure_running()
        self._cpu.ExecutionMode = ExecutionMode.Continuous
        remaining = microseconds
        while remaining and not self._reason:
            quantum = remaining
            if self._input_playback is not None:
                self._input_playback._apply_due(self._elapsed_us)
                if self._input_playback is not None:
                    quantum = min(quantum, self._input_playback.next_due_us - self._elapsed_us)
            from Antmicro.Renode.Peripherals.LED import RazerChroma192
            from System import Array

            lights = Array[RazerChroma192]([c._backend for c in self._lighting.values()])
            args = (
                quantum,
                self._initial_reads,
                self._initial_writes,
                self._watchdog,
                self._ezh,
                self._usb_device,
                lights,
            )
            try:
                if _usb_pump is None:
                    self._runner.Run(*args)
                else:
                    _usb_pump.BeginBatch(self._elapsed_us)
                    self._runner.RunScheduled(*args, _usb_pump.ScheduledServiceHook)
            finally:
                advanced = int(self._runner.LastElapsedUs)
                self._elapsed_us += advanced
                remaining -= advanced
            state = self.state()
        if not self._reason and self._input_playback is not None:
            self._input_playback._apply_due(self._elapsed_us)
            return self.state()
        return state

    def state(self):
        if self._closed:
            raise RuntimeError("Keyboard is closed")
        exception = self._value(self._cpu.CPSR) & 0x1FF
        faults = self._bus.ReadDoubleWord(0xE000ED28)
        reads = self._bus.UnexpectedReads - self._initial_reads
        reads += sum(int(peripheral.UnsupportedReads) for peripheral in self._modeled_peripherals)
        writes = self._bus.UnexpectedWrites - self._initial_writes
        writes += sum(int(peripheral.UnsupportedWrites) for peripheral in self._modeled_peripherals)
        if self._watchdog.Expired:
            self._reason = "watchdog_expired"
        elif self._ezh.BusFault:
            self._reason = "ezh_bus_fault"
        elif self._ezh.UnsupportedInstruction:
            self._reason = "unsupported_ezh_instruction"
        elif self._usb_device.TransferError:
            self._reason = "unsupported_usb_transfer"
        elif any(c._backend.UnsupportedOperation for c in self._lighting.values()):
            self._reason = "unsupported_lighting"
        elif reads or writes:
            self._reason = "unsupported_mmio"
        elif exception in (3, 4, 5, 6) or faults:
            self._reason = "cpu_fault"
        return State(
            self._value(self._cpu.PC),
            self._value(self._cpu.SP),
            self._cpu.ExecutedInstructions,
            self._ezh.ReadDoubleWord(0x28),
            self._ezh.Instructions,
            self._ezh.Holding,
            self._ezh.FaultAddress if self._ezh.BusFault else None,
            self._elapsed_us,
            exception,
            faults,
            self._bus.ReadDoubleWord(0xE000ED38) if faults & 0x8000 else None,
            reads,
            writes,
            self._reason,
        )

    def logs(self):
        from Antmicro.Renode.Logging import Logger

        Logger.Flush()
        entries = [entry.Message for entry in self._log.GetMemoryLogEntries(1000)]
        entries.extend(
            f"lighting {chip.bus}:{chip.address:#04x}: {chip._backend.UnsupportedOperation}"
            for chip in self._lighting.values()
            if chip._backend.UnsupportedOperation
        )
        return entries

    def read_memory(self, address, length):
        if self._closed:
            raise RuntimeError("Keyboard is closed")
        ranges = (
            (0, 0x80000),
            (0x04000000, 0x04008000),
            (0x20000000, 0x20030000),
            (0x40100000, 0x40104000),
            (0x9FC00, 0xA0000),
        )
        if (
            not isinstance(length, int)
            or length < 0
            or not any(start <= address and address + length <= end for start, end in ranges)
        ):
            raise ValueError("Inspection must remain within a modeled memory region")
        if address + length <= 0x80000:
            return bytes(self._machine.sysbus.flash.ReadRaw(address, length))
        return bytes(self._bus.ReadBytes(address, length))

    def _ensure_running(self):
        if self._closed:
            raise RuntimeError("Keyboard is closed")
        if self._reason:
            raise RuntimeError(f"Execution stopped: {self._reason}; reset before continuing")

    def close(self):
        if self._closed:
            return
        from Antmicro.Renode.Logging import Logger

        self._hard_pause_processors()
        self._engine.PauseAll()
        self._emulation.rem_mach(self._name)
        Logger.RemoveBackend(self._log)
        self._emulation.clear()
        self._closed = True
        Keyboard._active = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
