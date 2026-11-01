// SPDX-License-Identifier: MIT
using Antmicro.Renode.Core;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Peripherals.GPIOPort
{
    // Reuse LPC GPIO word registers; add LPC55 byte-per-pin and word-per-pin
    // aliases, and reject ports absent from the 64-pin GPIO block.
    public class RazerLpcGpio : LPC43xx_GPIO, IDoubleWordPeripheral, IBytePeripheral
    {
        public RazerLpcGpio(IMachine machine) : base(machine) { }
        public override void Reset() { base.Reset(); UnsupportedReads = UnsupportedWrites = 0; }
        public byte ReadByte(long offset)
        {
            if(offset >= 0 && offset < 64) return (byte)((base.ReadDoubleWord(0x2100 + (offset / 32) * 4) >> (int)(offset % 32)) & 1);
            UnsupportedReads++; this.Log(LogLevel.Warning, "Unsupported GPIO byte read at +0x{0:X}", offset); return 0;
        }
        public void WriteByte(long offset, byte value)
        {
            if(offset >= 0 && offset < 64)
            { base.WriteDoubleWord((value == 0 ? 0x2280 : 0x2200) + (offset / 32) * 4, 1U << (int)(offset % 32)); return; }
            UnsupportedWrites++; this.Log(LogLevel.Warning, "Unsupported GPIO byte write at +0x{0:X}", offset);
        }
        public new uint ReadDoubleWord(long offset)
        {
            if(offset >= 0x1000 && offset < 0x1100 && offset % 4 == 0) return ReadByte((offset - 0x1000) / 4);
            if(WordRegister(offset)) return base.ReadDoubleWord(offset);
            UnsupportedReads++; this.Log(LogLevel.Warning, "Unsupported GPIO word read at +0x{0:X}", offset); return 0;
        }
        public new void WriteDoubleWord(long offset, uint value)
        {
            if(offset >= 0x1000 && offset < 0x1100 && offset % 4 == 0) { WriteByte((offset - 0x1000) / 4, (byte)(value & 1)); return; }
            if(WordRegister(offset)) { base.WriteDoubleWord(offset, value); return; }
            UnsupportedWrites++; this.Log(LogLevel.Warning, "Unsupported GPIO word write at +0x{0:X}", offset);
        }
        private bool WordRegister(long offset) => offset >= 0x2000 && offset <= 0x2304 && (offset % 0x80 == 0 || offset % 0x80 == 4);
        public ulong UnsupportedReads { get; private set; }
        public ulong UnsupportedWrites { get; private set; }
    }
}
