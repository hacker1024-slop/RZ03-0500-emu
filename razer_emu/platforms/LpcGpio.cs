// SPDX-License-Identifier: MIT
using System;
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
        public override void Reset()
        {
            base.Reset(); UnsupportedReads = UnsupportedWrites = 0;
            GpioFunctionMask = uint.MaxValue; EzhFunctionMask = 0;
            pullUps = 0;
            for(var pin = 0; pin < 64; pin++)
                if((externalMask & (1UL << pin)) != 0)
                    base.OnGPIO(pin, (externalValues & (1UL << pin)) != 0);
        }
        public override void OnGPIO(int number, bool value)
        {
            CheckInput(number);
            var bit = 1UL << number;
            externalMask |= bit;
            externalValues = value ? externalValues | bit : externalValues & ~bit;
            base.OnGPIO(number, value);
        }
        public void ReleaseInput(int number)
        {
            CheckInput(number);
            externalMask &= ~(1UL << number);
            base.OnGPIO(number, (pullUps & (1UL << number)) != 0);
        }
        public void SetInputPull(int number, bool high)
        {
            CheckInput(number);
            var bit = 1UL << number;
            pullUps = high ? pullUps | bit : pullUps & ~bit;
            if((externalMask & bit) == 0) base.OnGPIO(number, high);
        }
        private static void CheckInput(int number)
        {
            if(number < 0 || number >= 64) throw new ArgumentOutOfRangeException(nameof(number));
        }
        public Func<uint, uint, uint> InputPins { get; set; }
        public uint GpioFunctionMask { get; private set; }
        public uint EzhFunctionMask { get; private set; }
        public uint Port0Output => base.ReadDoubleWord(0x2100);
        public uint Port0Direction => base.ReadDoubleWord(0x2000);
        public uint ReadSharedPort0(uint ezhOutput, uint ezhDirection)
        {
            var gpioDrive = Port0Direction & GpioFunctionMask;
            var ezhDrive = ezhDirection & EzhFunctionMask;
            var inputs = (uint)((pullUps & ~externalMask) | (externalValues & externalMask));
            return (inputs & ~(gpioDrive | ezhDrive)) |
                (Port0Output & gpioDrive) | (ezhOutput & ezhDrive);
        }
        public void SetPinFunction(int pin, uint function)
        {
            if(pin < 0 || pin >= 32) return; // EZH/matrix uses port zero only.
            var bit = 1U << pin;
            GpioFunctionMask = function == 0 ? GpioFunctionMask | bit : GpioFunctionMask & ~bit;
            EzhFunctionMask = function == 15 ? EzhFunctionMask | bit : EzhFunctionMask & ~bit;
        }
        private uint ReadPins(long port)
        {
            var value = base.ReadDoubleWord(0x2100 + port * 4);
            return port == 0 && InputPins != null ? InputPins(value, Port0Direction) : value;
        }
        public byte ReadByte(long offset)
        {
            if(offset >= 0 && offset < 64) return (byte)((ReadPins(offset / 32) >> (int)(offset % 32)) & 1);
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
            if(offset == 0x2100 || offset == 0x2104 || offset == 0x2200 || offset == 0x2204)
                return ReadPins((offset & 4) / 4);
            if(offset == 0x2180 || offset == 0x2184)
                return ReadPins((offset & 4) / 4) & ~base.ReadDoubleWord(0x2080 + (offset & 4));
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
        private ulong externalMask, externalValues, pullUps;
    }
}
