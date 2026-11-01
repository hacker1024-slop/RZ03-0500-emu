// SPDX-License-Identifier: MIT
using System.Collections.Generic;
using Antmicro.Renode.Core;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.Miscellaneous;

namespace Antmicro.Renode.Peripherals.I2C
{
    // Functional LPC Flexcomm I2C master. Unconnected addresses NACK; they are
    // never treated as successfully responding hardware.
    public class RazerLpcI2c : RazerLpcAnalog
    {
        public RazerLpcI2c() { IRQ = new GPIO(); Reset(); }
        public override void Reset()
        {
            base.Reset(); values.Clear();
            foreach(var offset in new long[] {0x800,0x804,0x808,0x810,0x814,0x818,0x820,0x824,0x828})
                values[offset] = 0;
            values[0xFF8] = 0x70; // USART, SPI and I2C present, no selected mode
            state = 0; active = null; pending.Clear();
            IRQ?.Unset();
        }
        public void Attach(int address, II2CPeripheral device) { devices[address] = device; }
        public override uint ReadDoubleWord(long offset)
        {
            if(offset == 0x804) return Status;
            if(offset == 0x818) return Status & values[0x808];
            return base.ReadDoubleWord(offset);
        }
        public override void WriteDoubleWord(long offset, uint value)
        {
            switch(offset)
            {
            case 0xFF8: values[offset] = (value & 0xF) | 0x70; return;
            case 0x808: values[offset] |= value; UpdateIrq(); return;
            case 0x80C: values[0x808] &= ~value; UpdateIrq(); return;
            case 0x800: values[offset] = value; UpdateIrq(); return;
            case 0x820:
                values[offset] = value;
                if((value & 2) != 0)
                {
                    FlushWrite();
                    var data = values[0x828];
                    var address = (int)(data >> 1) & 0x7F;
                    reading = (data & 1) != 0;
                    if(!devices.TryGetValue(address, out active))
                    {
                        this.Log(LogLevel.Warning, "I2C address 0x{0:X2} NACK: no device attached", address);
                        state = 3;
                    }
                    else { state = reading ? 1 : 2; Receive(); }
                }
                else if((value & 4) != 0)
                { FlushWrite(); active?.FinishTransmission(); active = null; state = 0; }
                else if((value & 1) != 0 && active != null)
                {
                    if(reading) Receive();
                    else pending.Add((byte)values[0x828]);
                }
                if((value & 8) != 0) base.WriteDoubleWord(0x1820, value); // DMA not yet modeled
                UpdateIrq(); return;
            }
            base.WriteDoubleWord(offset, value);
        }
        private void FlushWrite()
        {
            if(active != null && pending.Count != 0) active.Write(pending.ToArray());
            pending.Clear();
        }
        private readonly List<byte> pending = new List<byte>();
        private void Receive()
        {
            if(reading && active != null) values[0x828] = active.Read(1)[0];
        }
        private uint Status => (values[0x800] & 1) == 0 ? 0 : (uint)(1 | state << 1);
        private void UpdateIrq() { IRQ.Set((Status & values[0x808] & 1) != 0); }
        public GPIO IRQ { get; }
        private int state;
        private bool reading;
        private II2CPeripheral active;
        private readonly Dictionary<int, II2CPeripheral> devices = new Dictionary<int, II2CPeripheral>();
    }
}
