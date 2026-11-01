"""Frontend-independent execution and inspection of a firmware-backed keyboard."""

from dataclasses import asdict, dataclass
from importlib.resources import files
from uuid import uuid4

from .firmware import FLASH_APPLICATION, RUNTIME_BASE, Firmware


@dataclass(frozen=True)
class State:
    pc: int
    sp: int
    instructions: int
    ezh_pc: int
    ezh_instructions: int
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
        "dma0",
        "flexcomm4",
        "flexcomm2",
        "gpio",
        "wwdt",
        "ezh",
    )

    def __init__(self, firmware: Firmware, *, flash_regions=()):
        if Keyboard._active:
            raise RuntimeError("Use one Keyboard per process")
        # Renode is deliberately lazy-loaded, allowing file inspection without it.
        from pyrenode3.wrappers import Emulation, Monitor

        # isort: split
        # pyrenode3 must load the CLR before importing its namespaces.
        from Antmicro.Renode.Logging import Logger, LogLevel
        from Antmicro.Renode.Logging.Backends import MemoryBackend

        self.firmware = firmware
        self._emulation = Emulation()
        self._engine = self._emulation.internal
        self._name = "keyboard-" + uuid4().hex
        self._log = MemoryBackend()
        Logger.AddBackend(self._log, self._name)
        self._log.SetLogLevel(LogLevel.Warning)
        self._machine = self._emulation.add_mach(self._name)
        Keyboard._active = True
        self._closed = False
        try:
            if not Keyboard._models_loaded:
                for name in (
                    "LpcGpio.cs",
                    "LpcSyscon.cs",
                    "LpcTimer.cs",
                    "LpcDma.cs",
                    "LpcI2c.cs",
                    "LpcFlash.cs",
                    "LpcWatchdog.cs",
                    "LpcEzh.cs",
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
            from System import Array, Byte

            flash = self._machine.sysbus.flash.internal
            for region in flash_regions:
                flash.LoadRegion(
                    region.address, Array[Byte](region.data), Array[Byte](region.valid_mask)
                )
            self._watchdog = self._machine.sysbus.wwdt.internal
            self._ezh = self._machine.sysbus.ezh.internal
            self.reset()
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _register(value):
        from Antmicro.Renode.Peripherals.CPU import RegisterValue

        return RegisterValue.Create(value, 32)

    @staticmethod
    def _value(register):
        return int(str(register), 16)

    def reset(self):
        """Recreate the application handoff; this does not run the incomplete bootloader."""
        from System import Array, Byte

        if self._closed:
            raise RuntimeError("Keyboard is closed")
        self._engine.PauseAll()
        self._machine.Reset()
        self._bus.WriteBytes(Array[Byte](bytes(0x30000)), RUNTIME_BASE)
        self._bus.WriteBytes(Array[Byte](bytes(0x8000)), 0x04000000)
        self._bus.WriteBytes(Array[Byte](self.firmware.image), RUNTIME_BASE)
        self._machine.sysbus.flash.LoadImage(FLASH_APPLICATION, Array[Byte](self.firmware.image))
        self._cpu.VectorTableOffset = RUNTIME_BASE
        self._cpu.SP = self._register(self.firmware.initial_sp)
        self._cpu.PC = self._register(self.firmware.reset_vector)
        self._cpu.PerformanceInMips = 96
        self._elapsed_us = 0
        self._reason = None
        self._initial_reads = self._bus.UnexpectedReads
        self._initial_writes = self._bus.UnexpectedWrites

    def step(self, count=1):
        if not 1 <= count <= 1000:
            raise ValueError("Step count must be between 1 and 1000")
        self._ensure_running()
        self._cpu.Step(count)
        self._engine.PauseAll()
        return self.state()

    def run_for(self, microseconds):
        """Advance bounded virtual time, checking faults and missing models every 10 us."""
        from Antmicro.Renode.Peripherals.CPU import ExecutionMode
        from Antmicro.Renode.Time import TimeInterval

        if not isinstance(microseconds, int) or not 1 <= microseconds <= 10_000_000:
            raise ValueError("Duration must be an integer from 1 to 10,000,000 microseconds")
        self._ensure_running()
        self._cpu.ExecutionMode = ExecutionMode.Continuous
        remaining = microseconds
        while remaining and not self._reason:
            quantum = min(10, remaining)
            self._engine.RunFor(TimeInterval.FromMicroseconds(quantum))
            self._elapsed_us += quantum
            remaining -= quantum
            self.state()
        return self.state()

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
        return [entry.Message for entry in self._log.GetMemoryLogEntries(1000)]

    def read_memory(self, address, length):
        if self._closed:
            raise RuntimeError("Keyboard is closed")
        ranges = (
            (0, 0x80000),
            (0x04000000, 0x04008000),
            (0x20000000, 0x20030000),
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
