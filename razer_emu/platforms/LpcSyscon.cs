// SPDX-License-Identifier: MIT
using System.Collections.Generic;
using System.Linq;
using Antmicro.Renode.Core;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Peripherals.Miscellaneous
{
    // Functional control-register model. Clock frequencies and reset outputs are
    // deliberately not guessed: only startup's observed operations are supported.
    public class RazerLpcSyscon : IDoubleWordPeripheral, IKnownSize
    {
        public RazerLpcSyscon(IMachine machine) { this.machine = machine; Reset(); }
        public void Reset()
        {
            values.Clear();
            UnsupportedReads = UnsupportedWrites = 0;
            for(long group = 0x100; group <= 0x200; group += 0x100)
                for(long bank = 0; bank < 3; bank++) values[group + bank * 4] = 0;
            values[0x280] = 0; // MAINCLKSELA
            values[0x284] = 0; // MAINCLKSELB
            values[0x400] = 0; // FMCCR
            values[0x380] = 0; // AHBCLKDIV
            values[0x2B8] = 0; // Flexcomm2 clock selector
            values[0x2C0] = 0; // Flexcomm4 clock selector
            values[0x274] = 0; // CTIMER2 clock selector
            // Firmware's flash geometry selector: token 2 selects 512 KiB.
            // This register is absent from the public header; see model notes.
            values[0xFE0] = 2;
            values[0x2EC] = 0; // Reserved timing bit read by startup (virtual default)
            values[0x308] = 0; // TRACECLKDIV
        }
        public virtual uint ReadDoubleWord(long offset)
        {
            if(values.TryGetValue(offset, out var value)) return value;
            UnsupportedReads++;
            this.Log(LogLevel.Warning, "Unsupported SYSCON read at +0x{0:X}, PC {1}", offset, machine.SystemBus.GetCPUs().First().PC);
            return 0;
        }
        public void WriteDoubleWord(long offset, uint value)
        {
            for(long group = 0x100; group <= 0x200; group += 0x100)
            {
                if(offset >= group + 0x20 && offset <= group + 0x28 && offset % 4 == 0)
                { values[offset - 0x20] |= value; return; }
                if(offset >= group + 0x40 && offset <= group + 0x48 && offset % 4 == 0)
                { values[offset - 0x40] &= ~value; return; }
            }
            if(values.ContainsKey(offset)) { values[offset] = value; return; }
            UnsupportedWrites++;
            this.Log(LogLevel.Warning, "Unsupported SYSCON write at +0x{0:X}: 0x{1:X}", offset, value);
        }
        public long Size => 0x1000;
        public ulong UnsupportedReads { get; private set; }
        public ulong UnsupportedWrites { get; private set; }
        private readonly IMachine machine;
        private readonly Dictionary<long, uint> values = new Dictionary<long, uint>();
    }
}

namespace Antmicro.Renode.Peripherals.Miscellaneous
{
    public class RazerLpcAnalog : IDoubleWordPeripheral, IKnownSize
    {
        public RazerLpcAnalog() { Reset(); }
        public virtual void Reset()
        {
            values.Clear();
            values[0x10] = 0x4000; // FRO192M_CTRL: 12 MHz output enabled
            UnsupportedReads = UnsupportedWrites = 0;
        }
        public virtual uint ReadDoubleWord(long offset)
        {
            if(values.TryGetValue(offset, out var value)) return value;
            UnsupportedReads++;
            this.Log(LogLevel.Warning, "Unsupported analog read at +0x{0:X}", offset);
            return 0;
        }
        public virtual void WriteDoubleWord(long offset, uint value)
        {
            if(values.ContainsKey(offset)) { values[offset] = value; return; }
            UnsupportedWrites++;
            this.Log(LogLevel.Warning, "Unsupported analog write at +0x{0:X}: 0x{1:X}", offset, value);
        }
        public long Size => 0x1000;
        public ulong UnsupportedReads { get; private set; }
        public ulong UnsupportedWrites { get; private set; }
        protected readonly Dictionary<long, uint> values = new Dictionary<long, uint>();
    }

    public class RazerLpcPmc : RazerLpcAnalog
    {
        public override void Reset()
        {
            base.Reset();
            values.Clear();
            values[0x08] = 0; // RESETCTRL
            values[0x30] = 0; // BODVBAT
            values[0x10] = 0x010C4E68; // SDK fallback DCDC0
            values[0x14] = 0x01803A98; // SDK fallback DCDC1
            values[0x1C] = 0; // LDOPMU
            values[0xB8] = 0; // PDRUNCFG0
        }
        public override void WriteDoubleWord(long offset, uint value)
        {
            if(offset == 0xC0) { values[0xB8] |= value; return; }
            if(offset == 0xC8) { values[0xB8] &= ~value; return; }
            base.WriteDoubleWord(offset, value);
        }
    }
}

namespace Antmicro.Renode.Peripherals.Miscellaneous
{
    // INIT command is used by CLOCK_SetFLASHAccessCycles. Other commands are
    // intentionally unsupported until flash read/program semantics are modeled.
    public class RazerLpcFlashController : RazerLpcAnalog
    {
        public RazerLpcFlashController(IMachine machine) { this.machine = machine; }
        private readonly IMachine machine;
        public override void Reset()
        {
            base.Reset();
            values.Clear();
            values[0x00] = 0;
            values[0x10] = values[0x14] = 0;
            for(long i = 0; i < 4; i++) values[0x80 + i * 4] = 0;
            values[0xFE0] = 0;
        }
        public override void WriteDoubleWord(long offset, uint value)
        {
            if(offset == 0xFE8) { values[0xFE0] &= ~value; return; }
            if(offset == 0 && value == 2) { values[0] = value; values[0xFE0] |= 4; return; }
            if(offset == 0 && value == 3)
            {
                var address = (ulong)values[0x10] * 16;
                if(address + 16 > 0x80000) { values[0xFE0] |= 5; return; }
                for(long i = 0; i < 4; i++)
                    values[0x80 + i * 4] = machine.SystemBus.ReadDoubleWord(address + (ulong)i * 4);
                values[0xFE0] |= 4; return;
            }
            if(offset == 0) { base.WriteDoubleWord(0x1000, value); return; }
            base.WriteDoubleWord(offset, value);
        }
    }
}

namespace Antmicro.Renode.Peripherals.Miscellaneous
{
    public class RazerLpcIocon : RazerLpcAnalog
    {
        public RazerLpcIocon(Antmicro.Renode.Peripherals.GPIOPort.RazerLpcGpio gpio) { this.gpio = gpio; }
        private readonly Antmicro.Renode.Peripherals.GPIOPort.RazerLpcGpio gpio;
        public override void WriteDoubleWord(long offset, uint value)
        {
            base.WriteDoubleWord(offset, value);
            if(offset >= 0 && offset < 256 && offset % 4 == 0)
            {
                var mode = (value >> 4) & 3;
                if(mode == 1 || mode == 2) gpio.OnGPIO((int)(offset / 4), mode == 2);
            }
        }
        public override void Reset()
        {
            base.Reset(); values.Clear();
            for(long i = 0; i < 64; i++) values[i * 4] = 0;
        }
    }
}
