// SPDX-License-Identifier: MIT
using System;
using Antmicro.Renode.Core;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.Timers;
using Antmicro.Renode.Time;

namespace Antmicro.Renode.Peripherals.Miscellaneous
{
    // Functional EZH architecture-B interpreter. Register/MMIO encodings follow
    // NXP AN14650. Unimplemented instructions stop explicitly, including during
    // initialization; no instruction or firmware bytes are patched.
    public class RazerLpcEzh : RazerLpcAnalog
    {
        public RazerLpcEzh(IMachine machine)
        {
            this.machine = machine;
            clock = new LimitTimer(machine.ClockSource, 96000000, this, "ezh", limit: 96,
                eventEnabled: true, workMode: WorkMode.Periodic);
            clock.LimitReached += () => StepInstructions(96);
        }
        public override void Reset()
        {
            base.Reset(); values.Clear();
            for(long offset = 0x20; offset <= 0x48; offset += 4) values[offset] = 0;
            Array.Clear(registers, 0, registers.Length);
            Instructions = 0; Running = false; clock?.Reset();
            pc = 0; result = 0; carry = false;
        }
        public override uint ReadDoubleWord(long offset)
        {
            if(offset == 0x28) return pc;
            if(offset == 0x2C) return registers[12];
            return base.ReadDoubleWord(offset);
        }
        public override void WriteDoubleWord(long offset, uint value)
        {
            if(offset == 0x24)
            {
                if((value >> 16) != 0xC0DE) return;
                var wasRunning = Running;
                values[0x24] = value & 0x1F;
                Running = (value & 1) != 0;
                if(Running && !wasRunning) pc = values[0x20] & ~3U;
                clock.Enabled = Running;
                return;
            }
            base.WriteDoubleWord(offset, value);
        }
        public uint GetRegister(int index)
        {
            if(index < 0 || index > 15) throw new ArgumentOutOfRangeException(nameof(index));
            return index == 13 ? pc : registers[index];
        }
        public ulong Instructions { get; private set; }
        public bool Running { get; private set; }
        public void StepInstructions(int count)
        {
            if(count < 1 || count > 4096) throw new ArgumentOutOfRangeException(nameof(count));
            for(var i = 0; i < count && Running; i++)
            {
                var instructionAddress = pc;
                var word = machine.SystemBus.ReadDoubleWord(pc);
                pc += 4;
                var opcode = word & 31;
                var condition = (int)((word >> 5) & 15);
                if(!Implemented(opcode)) { Fail(instructionAddress, word); break; }
                if(opcode != 0x12 && !Condition(condition)) { Instructions++; continue; }
                var destination = (int)((word >> 10) & 15);
                var source = (int)((word >> 14) & 15);
                var second = (int)((word >> 20) & 15);
                var shift = (int)((word >> 24) & 31);
                var flags = (word & 512) != 0;
                uint value;
                switch(opcode)
                {
                case 0x12: // no-op; heartbeat/timing forms remain unsupported
                    if(word != 0x12) { Fail(instructionAddress, word); continue; }
                    break;
                case 0: // register move or signed immediate shifted left
                    value = (word & (1 << 18)) == 0 ? ReadRegister(source) :
                        unchecked((uint)SignExtend(word >> 20, 11)) <<
                        (source | (int)((word >> 15) & 16));
                    if((word & 0x80000000) != 0) value = ~value;
                    WriteRegister(destination, value, flags); break;
                case 4: // immediate peripheral read: 18 encoded word-offset bits
                    value = machine.SystemBus.ReadDoubleWord(0x40000000U | ((word >> 12) & 0xFFFFC));
                    WriteRegister(destination, value, false); break;
                case 5: // peripheral write has a split immediate address field
                    var peripheral = 0x40000000U | ((word >> 12) & 0xFF000) | ((word >> 8) & 0xFFC);
                    machine.SystemBus.WriteDoubleWord(peripheral, ReadRegister(second)); break;
                case 1: case 2:
                    var isWord = (word & (1 << 18)) != 0;
                    var offset = SignExtend(word >> 24, 8) * (isWord ? 4 : 1);
                    var address = unchecked(ReadRegister(source) + (uint)offset);
                    var writeback = (word & (opcode == 1 ? 1U << 20 : 1U << 10)) != 0;
                    var access = writeback && (word & (1 << 19)) != 0 ? ReadRegister(source) : address;
                    if(isWord) access &= ~3U;
                    if(opcode == 1)
                    {
                        value = isWord ? machine.SystemBus.ReadDoubleWord(access) : machine.SystemBus.ReadByte(access);
                        if(!isWord && (word & (1 << 21)) != 0) value = unchecked((uint)(sbyte)value);
                        WriteRegister(destination, value, false);
                    }
                    else if(isWord) machine.SystemBus.WriteDoubleWord(access, ReadRegister(second));
                    else machine.SystemBus.WriteByte(access, (byte)ReadRegister(second));
                    if(writeback && source != 13 && !(opcode == 1 && source == destination)) registers[source] = address;
                    break;
                case 0x10: // immediate shift; fused ALU forms need further implementation
                    if((word >> 29) != 0) { Fail(instructionAddress, word); continue; }
                    value = ReadRegister(second);
                    if(flags && shift != 0)
                    {
                        var kind = (word >> 18) & 3;
                        carry = kind == 0 ? ((value >> (32 - shift)) & 1) != 0 :
                            ((value >> (shift - 1)) & 1) != 0;
                    }
                    switch((word >> 18) & 3)
                    {
                    case 0: value <<= shift; break;
                    case 1: value = unchecked((uint)((int)value >> shift)); break;
                    case 2: value >>= shift; break;
                    case 3: if(shift != 0) value = (value >> shift) | (value << (32 - shift)); break;
                    }
                    WriteRegister(destination, value, flags); break;
                case 0x18:
                    var bit = 1U << ((word & (1 << 18)) != 0 ? (int)(ReadRegister(second) & 31) : shift);
                    value = ReadRegister(source);
                    switch(word >> 29)
                    {
                    case 1: value &= ~bit; break;
                    case 5: value &= bit; break;
                    case 6: value |= bit; break;
                    case 7: value ^= bit; break;
                    default: Fail(instructionAddress, word); continue;
                    }
                    WriteRegister(destination, value, flags); break;
                }
                Instructions++;
            }
        }
        private bool Implemented(uint opcode) => opcode == 0 || opcode == 1 || opcode == 2 ||
            opcode == 4 || opcode == 5 || opcode == 0x10 || opcode == 0x12 || opcode == 0x18;
        private uint ReadRegister(int register) => register == 13 ? pc + 4 : registers[register];
        private void WriteRegister(int register, uint value, bool flags)
        {
            if(register == 13)
            {
                // Scheduled branches must be modeled before accepting their S form.
                if(flags) { Fail(pc - 4, machine.SystemBus.ReadDoubleWord(pc - 4)); return; }
                pc = value & ~3U;
            }
            else if(register != 14) registers[register] = value;
            if(flags) result = value;
        }
        private bool Condition(int condition)
        {
            switch(condition)
            {
            case 0: return true;
            case 1: return result == 0;
            case 2: return result != 0;
            case 3: return (int)result >= 0;
            case 4: return (int)result < 0;
            case 5: return (int)result > 0;
            case 6: return (int)result <= 0;
            case 7: return carry;
            case 8: return !carry;
            case 9: return carry && result == 0;
            case 13: return (values[0x24] & 2) == 0;
            case 15: return (values[0x24] & 2) != 0;
            default: Fail(pc - 4, machine.SystemBus.ReadDoubleWord(pc - 4)); return false;
            }
        }
        private static int SignExtend(uint value, int width) => (int)(value << (32 - width)) >> (32 - width);
        private void Fail(uint address, uint word)
        {
            this.Log(LogLevel.Warning, "Unsupported EZH instruction 0x{0:X8} at 0x{1:X8}", word, address);
            base.WriteDoubleWord(0x1000, word);
            pc = address; Running = false; clock.Enabled = false;
        }
        private readonly IMachine machine;
        private readonly LimitTimer clock;
        private readonly uint[] registers = new uint[16];
        private uint pc, result;
        private bool carry;
    }
}
