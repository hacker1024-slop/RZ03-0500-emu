// SPDX-License-Identifier: MIT
using System;
using Antmicro.Renode.Core;
using Antmicro.Renode.Exceptions;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.Bus;
using Antmicro.Renode.Peripherals.CPU;
using Antmicro.Renode.Peripherals.Miscellaneous;

namespace Antmicro.Renode.Peripherals.MTD
{
    // LPC55 erased words have no valid ECC. Raw controller access is separate
    // from CPU reads, which must produce a bus fault on an unprogrammed word.
    // IExecutableIO routes code fetch through these same checked read methods.
    public class RazerLpcFlash : IBytePeripheral, IWordPeripheral, IDoubleWordPeripheral, IExecutableIO, IKnownSize
    {
        public RazerLpcFlash(IMachine machine) { this.machine = machine; }
        public void Reset() { } // reset does not erase nonvolatile memory
        public long Size => bytes.Length;
        public event Action EccError;
        public bool SimulateOverprogramEccFault { get; set; } = true;
        public byte ReadByte(long offset)
        {
            if(!IsEccReadable((int)offset))
            {
                EccError?.Invoke();
                throw new BusAccessException(BusAccessError.AddressError);
            }
            return bytes[offset];
        }
        public ushort ReadWord(long offset) => (ushort)(ReadByte(offset) | ReadByte(offset + 1) << 8);
        public uint ReadDoubleWord(long offset) => (uint)(ReadWord(offset) | (uint)ReadWord(offset + 2) << 16);
        public ulong ReadQuadWord(long offset) => ReadDoubleWord(offset) | ((ulong)ReadDoubleWord(offset + 4) << 32);
        public byte[] ReadBytes(long offset, int count, IPeripheral context = null)
        {
            var result = new byte[count];
            for(var i = 0; i < count; i++) result[i] = ReadByte(offset + i);
            return result;
        }
        public void WriteBytes(long offset, byte[] data, int startingIndex, int count, IPeripheral context = null)
        {
            for(var i = 0; i < count; i++) WriteByte(offset + i, data[startingIndex + i]);
        }
        public void WriteQuadWord(long offset, ulong value)
        { WriteDoubleWord(offset, (uint)value); WriteDoubleWord(offset + 4, (uint)(value >> 32)); }
        public void WriteByte(long offset, byte value)
        {
            if(machine.SystemBus.TryGetCurrentCPU(out var cpu))
                throw new BusAccessException(BusAccessError.AddressError);
            bytes[offset] = value;
            programmed[offset / 16] = true; // host image loading only
            corrupt[offset / 16] = false;
            Invalidate((int)offset, 1);
        }
        public void WriteWord(long offset, ushort value)
        { WriteByte(offset, (byte)value); WriteByte(offset + 1, (byte)(value >> 8)); }
        public void WriteDoubleWord(long offset, uint value)
        { WriteWord(offset, (ushort)value); WriteWord(offset + 2, (ushort)(value >> 16)); }
        public void LoadImage(int address, byte[] image)
        {
            if(address < 0 || address % 16 != 0 || address + image.Length > bytes.Length)
                throw new ArgumentException("Image is outside the flash bank");
            Array.Copy(image, 0, bytes, address, image.Length);
            for(var i = 0; i < image.Length; i += 16) programmed[(address + i) / 16] = true;
            Array.Clear(corrupt, address / 16, (image.Length + 15) / 16);
            Invalidate(address, image.Length);
        }
        public void LoadRegion(int address, byte[] image, byte[] mask)
        {
            if(address < 0 || address % 16 != 0 || image.Length % 16 != 0 ||
               address + image.Length > bytes.Length || mask.Length != image.Length)
                throw new ArgumentException("Invalid captured flash region");
            for(var word = 0; word < image.Length; word += 16)
            {
                var valid = true;
                for(var i = 0; i < 16; i++) valid &= mask[word + i] == 1;
                if(!valid) continue; // never turn a placeholder into programmed data
                Array.Copy(image, word, bytes, address + word, 16);
                programmed[(address + word) / 16] = true;
                corrupt[(address + word) / 16] = false;
            }
            Invalidate(address, image.Length);
        }
        public bool IsProgrammed(int address) => programmed[address / 16];
        public bool IsEccReadable(int address) => IsProgrammed(address) && !corrupt[address / 16];
        public byte[] ReadRaw(int address, int count)
        {
            var result = new byte[count];
            Array.Copy(bytes, address, result, 0, count);
            return result;
        }
        public void Erase(int address, int count)
        {
            Array.Clear(bytes, address, count);
            Array.Clear(programmed, address / 16, count / 16);
            Array.Clear(corrupt, address / 16, count / 16);
            Invalidate(address, count);
        }
        public bool Program(int address, byte[] data)
        {
            for(var i = 0; i < data.Length; i += 16)
                if(IsProgrammed(address + i))
                {
                    if(SimulateOverprogramEccFault)
                    {
                        // Worst-case fault injection. The original raw data is retained.
                        Array.Fill(corrupt, true, address / 16, data.Length / 16);
                        Invalidate(address, data.Length);
                    }
                    return false;
                }
            Array.Copy(data, 0, bytes, address, data.Length);
            for(var i = 0; i < data.Length; i += 16) programmed[(address + i) / 16] = true;
            Invalidate(address, data.Length);
            return true;
        }
        private void Invalidate(int address, int count)
        {
            foreach(var cpu in machine.SystemBus.GetCPUs())
                if(cpu is TranslationCPU translated)
                    foreach(var registration in machine.SystemBus.GetRegistrationPoints(this))
                    {
                        var start = (long)(registration.Range.StartAddress + registration.Offset) + address;
                        translated.OrderTranslationBlocksInvalidation(new IntPtr(start), new IntPtr(start + count));
                    }
        }
        private readonly byte[] bytes = new byte[0x80000];
        private readonly bool[] corrupt = new bool[0x80000 / 16];
        private readonly bool[] programmed = new bool[0x80000 / 16];
        private readonly IMachine machine;
    }

    public class RazerLpcFlashController : RazerLpcAnalog
    {
        public RazerLpcFlashController(RazerLpcFlash memory)
        {
            this.memory = memory;
            memory.EccError += () => values[0xFE0] |= 8;
        }
        public override void Reset()
        {
            base.Reset(); values.Clear();
            foreach(var offset in new long[] {0,4,0x10,0x14,0x80,0x84,0x88,0x8C,0xFE0,0xFE4})
                values[offset] = 0;
            Array.Clear(page, 0, page.Length);
        }
        public override void WriteDoubleWord(long offset, uint value)
        {
            if(offset == 0xFE8) { values[0xFE0] &= ~value; return; }
            if(offset == 0xFEC) { values[0xFE0] |= value & 15; return; }
            if(offset == 0xFD8) { values[0xFE4] &= ~value; return; }
            if(offset == 0xFDC) { values[0xFE4] |= value; return; }
            if(offset == 4 && (value & 1) != 0)
            { Array.Clear(page, 0, page.Length); values[0xFE0] = 4; return; }
            if(offset != 0) { base.WriteDoubleWord(offset, value); return; }
            values[0] = value;
            var address = (ulong)values[0x10] * 16;
            var last = (ulong)values[0x14] * 16;
            switch(value)
            {
            case 0: case 2: values[0xFE0] |= 4; return;
            case 3: // read one 16-byte physical flash word
                if(address + 16 > (ulong)memory.Size) { values[0xFE0] |= 6; return; }
                var data = memory.ReadRaw((int)address, 16);
                for(var i = 0; i < 4; i++) values[0x80 + i * 4] = BitConverter.ToUInt32(data, i * 4);
                values[0xFE0] |= memory.IsEccReadable((int)address) ? 4U : 12U;
                return;
            case 4: // erase inclusive range of 512-byte pages
                address &= ~511UL;
                last = (last & ~511UL) + 512;
                if(address >= last || last > (ulong)memory.Size) { values[0xFE0] |= 6; return; }
                memory.Erase((int)address, (int)(last - address));
                values[0xFE0] |= 4; return;
            case 5: // blank check over an inclusive physical-word range
                if(address > last || last + 16 > (ulong)memory.Size) { values[0xFE0] |= 6; return; }
                for(var current = address; current <= last; current += 16)
                    if(memory.IsProgrammed((int)current)) { values[0xFE0] |= 5; return; }
                values[0xFE0] |= 4; return;
            case 8: // load the 32-word page register
                var index = (int)(values[0x10] & 31) * 16;
                for(var i = 0; i < 4; i++)
                    Array.Copy(BitConverter.GetBytes(values[0x80 + i * 4]), 0, page, index + i * 4, 4);
                values[0xFE0] |= 4; return;
            case 12: // program page, including its ECC validity
                address &= ~511UL;
                if(address + 512 > (ulong)memory.Size) { values[0xFE0] |= 6; return; }
                if(memory.Program((int)address, page))
                    values[0xFE0] |= 4;
                else
                {
                    this.Log(LogLevel.Warning, "Flash program rejected on non-erased page 0x{0:X}", address);
                    values[0xFE0] |= 5;
                }
                return;
            }
            // Unknown commands must not silently acknowledge success.
            base.WriteDoubleWord(0x1000, value);
        }
        private readonly RazerLpcFlash memory;
        private readonly byte[] page = new byte[512];
    }
}
