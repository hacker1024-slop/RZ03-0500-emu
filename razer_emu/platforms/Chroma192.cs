// SPDX-License-Identifier: MIT
using System;
using Antmicro.Renode.Peripherals.I2C;

namespace Antmicro.Renode.Peripherals.LED
{
    // Register-compatible lighting model, inferred from firmware transactions and
    // the photographed CHROMA 192 marking. This is not a silicon identification.
    public class RazerChroma192 : II2CPeripheral
    {
        public RazerChroma192() { Reset(); }
        public void Reset()
        {
            UnsupportedOperation = null;
            ResetRegisters();
        }
        public void Write(byte[] data)
        {
            if(data.Length == 0) return;
            pointer = data[0];
            for(var i = 1; i < data.Length; i++)
            {
                WriteRegister(pointer, data[i]);
                pointer = (byte)(pointer + 1);
            }
        }
        public byte[] Read(int count = 1)
        {
            if(count < 0) throw new ArgumentOutOfRangeException(nameof(count));
            var result = new byte[count];
            for(var i = 0; i < count; i++)
            {
                if(pointer == 0xFE) result[i] = unlock;
                else if(pointer == 0xF1) result[i] = 0; // no injected electrical faults
                else if(page == 0 && pointer >= 0x18 && pointer <= 0x47) result[i] = 0;
                else if(page == 3 && pointer == 0x11)
                { ResetRegisters(); continue; } // reading reset returns zero and resets registers
                else Fail("read from an unsupported or write-only register", pointer);
                pointer = (byte)(pointer + 1);
            }
            return result;
        }
        public void FinishTransmission() { }
        public byte[] Pwm { get { var result = new byte[192]; Array.Copy(registers, 256, result, 0, 192); return result; } }
        public byte[] Enabled { get { var result = new byte[24]; Array.Copy(registers, result, 24); return result; } }
        public byte GlobalCurrent => registers[3 * 256 + 1];
        public bool Shutdown => (registers[3 * 256] & 1) == 0;
        public string UnsupportedOperation { get; private set; }
        private void ResetRegisters()
        {
            Array.Clear(registers, 0, registers.Length);
            page = pointer = unlock = interruptMask = 0;
            testUnlocked = false;
        }
        private void WriteRegister(byte address, byte value)
        {
            if(address == 0xFE) { unlock = value == 0xC5 ? value : (byte)0; return; }
            if(address == 0xFD)
            {
                if(unlock != 0xC5) return;
                unlock = 0;
                if(value > 3) { Fail("reserved page selection", value); return; }
                page = value; return;
            }
            if(address == 0xF0)
            {
                interruptMask = (byte)(value & 15);
                return;
            }
            if(page == 0 && address < 24 || page == 1 && address < 192)
            { registers[page * 256 + address] = value; return; }
            if(page == 2 && address < 192)
            { registers[page * 256 + address] = (byte)(value & 3); return; }
            if(page == 3 && address <= 0x10)
            {
                registers[3 * 256 + address] = value;
                if(address == 0 && (value & 6) != 0) Fail("automatic breathing or open/short detection", value);
                return;
            }
            // IS31FL3733-compatible PG3 test protection (datasheet tables 21-24).
            // Firmware periodically restores normal scanning/default trims.
            if(page == 3 && address == 0xE0)
            { testUnlocked = (value & 1) != 0; return; }
            if(page == 3 && address >= 0xE1 && address <= 0xE3)
            {
                if(!testUnlocked) return;
                if(value != 0) Fail("nondefault lighting test configuration", address);
                return;
            }
            Fail("write to a reserved or read-only register", address);
        }
        private void Fail(string operation, byte value)
        {
            if(UnsupportedOperation == null)
                UnsupportedOperation = string.Format("CHROMA 192 {0}: 0x{1:X2}", operation, value);
        }
        private readonly byte[] registers = new byte[4 * 256];
        private byte page, pointer, unlock, interruptMask;
        private bool testUnlocked;
    }
}
