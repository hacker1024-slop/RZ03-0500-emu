// SPDX-License-Identifier: MIT
using System;
using System.Collections.Generic;
using Antmicro.Renode.Core;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.Timers;
using Antmicro.Renode.Peripherals.Bus;
using Antmicro.Renode.Time;

namespace Antmicro.Renode.Peripherals.Miscellaneous
{
    // Functional EZH architecture-B interpreter. Register/MMIO encodings follow
    // NXP AN14650. Unimplemented instructions stop explicitly, including during
    // initialization; no instruction or firmware bytes are patched.
    public class RazerLpcEzh : RazerLpcAnalog, INumberedGPIOOutput
    {
        public RazerLpcEzh(IMachine machine)
        {
            this.machine = machine;
            var channels = new Dictionary<int, IGPIO>();
            for(var i = 0; i < 24; i++) channels[i] = new GPIO();
            Connections = channels;
            clock = new LimitTimer(machine.ClockSource, 96000000, this, "ezh", limit: 96,
                eventEnabled: true, workMode: WorkMode.Periodic);
            clock.LimitReached += () => AdvanceCycles(96);
        }
        public override void Reset()
        {
            base.Reset(); values.Clear();
            for(long offset = 0x20; offset <= 0x48; offset += 4) values[offset] = 0;
            Array.Clear(registers, 0, registers.Length);
            Instructions = 0; Running = false; clock?.Reset();
            pc = 0; flagsMask = 0; carry = false; Cycles = 0;
            heartbeatPeriod = heartbeatLeft = 0; waitForBeat = syncToBeat = false;
            branchDelay = 0; branchTarget = 0;
            BusFault = false; FaultAddress = 0; UnsupportedInstruction = false;
            loopStart = loopEnd = loopRemaining = 0;
            IRQ?.Unset();
            if(Connections != null) foreach(var channel in Connections.Values) channel.Unset();
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
            if(offset == 0x44 && (values[0x40] & 3) == 2) IRQ.Blink();
        }
        public uint GetRegister(int index)
        {
            if(index < 0 || index > 15) throw new ArgumentOutOfRangeException(nameof(index));
            return index == 13 ? pc : registers[index];
        }
        public ulong Instructions { get; private set; }
        public bool Running { get; private set; }
        public ulong Cycles { get; private set; }
        public bool BusFault { get; private set; }
        public bool UnsupportedInstruction { get; private set; }
        public uint FaultAddress { get; private set; }
        // Platform-specific SYSCON hijack routing must connect these outputs.
        // The common output also pulses for a mailbox write in handshake mode.
        public GPIO IRQ { get; } = new GPIO();
        public IReadOnlyDictionary<int, IGPIO> Connections { get; }
        public void AdvanceCycles(int count)
        {
            if(count < 1 || count > 4096) throw new ArgumentOutOfRangeException(nameof(count));
            for(var i = 0; i < count && Running; i++)
            {
                Cycles++;
                var beat = heartbeatPeriod == 0 || --heartbeatLeft == 0;
                if(beat) heartbeatLeft = heartbeatPeriod;
                if((waitForBeat || syncToBeat) && !beat) continue;
                waitForBeat = false;
                var instructionAddress = pc;
                try
                {
                    var word = ReadWord(pc);
                    pc += 4;
                    // GOSUB uses the entire word as its address. Decode its two-bit
                    // tag before interpreting the overlapping opcode/condition fields.
                    if((word & 3) == 3)
                    {
                        registers[15] = pc;
                        WriteRegister(13, word & ~3U, false);
                        CompleteInstruction();
                        continue;
                    }
                    var opcode = word & 31;
                    var condition = (int)((word >> 5) & 15);
                    // INT_TRIGGER is unconditional: its channel mask overlaps bit 8
                    // of the usual condition field (AN14650 sections 4.2.14/5.21).
                    if(opcode == 0x14)
                    {
                        IRQ.Blink();
                        for(var channel = 0; channel < 24; channel++)
                            if((word & (1U << (channel + 8))) != 0) Connections[channel].Blink();
                        CompleteInstruction();
                        continue;
                    }
                    if((condition == 10 || condition == 11) && opcode != 0x15)
                    { Fail(instructionAddress, word); break; }
                    if(opcode != 0x12 && !Condition(condition))
                    { if(Running) CompleteInstruction(); continue; }
                    if(!Implemented(opcode)) { Fail(instructionAddress, word); break; }
                    var destination = (int)((word >> 10) & 15);
                    var source = (int)((word >> 14) & 15);
                    var second = (int)((word >> 20) & 15);
                    var shift = (int)((word >> 24) & 31);
                    var flags = (word & 512) != 0;
                    uint value;
                    switch(opcode)
                    {
                    case 0x1A:
                        // The next opcode is a one-time prologue. The loop body
                        // starts two words after TIGHT_LOOP; Rend is exclusive.
                        loopStart = instructionAddress + 8;
                        loopEnd = ReadRegister(source) & ~3U;
                        loopRemaining = ReadRegister(second);
                        break;
                    case 0x12:
                        switch(condition)
                        {
                        case 0: break; // NOP
                        case 1:
                            heartbeatPeriod = (word & 512) == 0 ? word >> 16 : ReadRegister(source) & 0xFFFF;
                            heartbeatLeft = heartbeatPeriod;
                            break;
                        case 2: syncToBeat = (word & 0x80000000) != 0; break;
                        case 3: waitForBeat = true; break;
                        default: Fail(instructionAddress, word); continue;
                        }
                        break;
                    case 6: case 8: case 12: case 13: case 14:
                        var immediate = (word & (1 << 18)) != 0;
                        var left = ReadRegister(source);
                        var right = immediate ? unchecked((uint)SignExtend(word >> 20, 12)) : ReadRegister(second);
                        var nextCarry = carry;
                        switch(opcode)
                        {
                        case 6:
                            var sum = (ulong)left + right;
                            value = (uint)sum; nextCarry = sum > uint.MaxValue; break;
                        case 8: value = unchecked(left - right); nextCarry = left >= right; break;
                        case 12: value = left | right; break;
                        case 13: value = left & right; break;
                        default: value = left ^ right; break;
                        }
                        if((word & (1 << 19)) != 0) value = ~value;
                        if(!immediate)
                        {
                            if((word & 0x80000000) != 0)
                                value = (value >> 24) | ((value >> 8) & 0xFF00) | ((value << 8) & 0xFF0000) | (value << 24);
                            value = Shift(value, (int)((word >> 29) & 3), shift, ref nextCarry);
                        }
                        if(flags && destination != 13) carry = nextCarry;
                        WriteRegister(destination, value, flags); break;
                    case 0x15:
                        var target = (word & 512) != 0 ? (pc & 0xFF800000) | ((word >> 9) & 0x7FFFFC) : ReadRegister(source);
                        var scheduled = condition == 10 || condition == 11;
                        if((word & 1024) != 0) registers[15] = instructionAddress + (scheduled ? 12U : 4U);
                        WriteRegister(13, target, scheduled);
                        break;
                    case 0: // register move or signed immediate shifted left
                        value = (word & (1 << 18)) == 0 ? ReadRegister(source) :
                            unchecked((uint)SignExtend(word >> 20, 11)) <<
                            (source | (int)((word >> 15) & 16));
                        if((word & 0x80000000) != 0) value = ~value;
                        WriteRegister(destination, value, flags); break;
                    case 4: // immediate peripheral read: 18 encoded word-offset bits
                        value = ReadWord(0x40000000U | ((word >> 12) & 0xFFFFC));
                        WriteRegister(destination, value, false); break;
                    case 5: // peripheral write has a split immediate address field
                        var peripheral = 0x40000000U | ((word >> 12) & 0xFF000) | ((word >> 8) & 0xFFC);
                        WriteWord(peripheral, ReadRegister(second)); break;
                    case 1: case 2:
                        var isWord = (word & (1 << 18)) != 0;
                        var offset = SignExtend(word >> 24, 8) * (isWord ? 4 : 1);
                        var address = unchecked(ReadRegister(source) + (uint)offset);
                        var writeback = (word & (opcode == 1 ? 1U << 20 : 1U << 10)) != 0;
                        var access = writeback && (word & (1 << 19)) != 0 ? ReadRegister(source) : address;
                        if(isWord) access &= ~3U;
                        if(opcode == 1)
                        {
                            value = isWord ? ReadWord(access) : ReadByte(access);
                            if(!isWord && (word & (1 << 21)) != 0) value = unchecked((uint)(sbyte)value);
                            WriteRegister(destination, value, false);
                        }
                        else if(isWord) WriteWord(access, ReadRegister(second));
                        else WriteByte(access, (byte)ReadRegister(second));
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
                    if(Running) CompleteInstruction();
                }
                catch(BusAccessException)
                {
                    // Emergency-vector execution is not modeled yet. Stop the
                    // coprocessor explicitly; this is not a Cortex-M bus fault.
                    BusFault = true; FaultAddress = accessAddress;
                    pc = instructionAddress; Running = false; clock.Enabled = false;
                    this.Log(LogLevel.Warning, "EZH bus fault at 0x{0:X8}, instruction 0x{1:X8}; emergency handling not implemented", FaultAddress, pc);
                }
            }
        }
        private uint ReadWord(uint address)
        {
            accessAddress = address;
            return machine.SystemBus.ReadDoubleWord(address);
        }
        private byte ReadByte(uint address)
        {
            accessAddress = address;
            return machine.SystemBus.ReadByte(address);
        }
        private void WriteWord(uint address, uint value)
        {
            accessAddress = address;
            machine.SystemBus.WriteDoubleWord(address, value);
        }
        private void WriteByte(uint address, byte value)
        {
            accessAddress = address;
            machine.SystemBus.WriteByte(address, value);
        }
        private void CompleteInstruction()
        {
            Instructions++;
            if(branchDelay > 0 && --branchDelay == 0) pc = branchTarget;
            if(loopRemaining != 0 && pc == loopEnd)
            {
                loopRemaining--;
                pc = loopStart;
            }
        }
        private bool Implemented(uint opcode) => opcode == 0 || opcode == 1 || opcode == 2 ||
            opcode == 4 || opcode == 5 || opcode == 6 || opcode == 8 || opcode == 12 || opcode == 13 ||
            opcode == 14 || opcode == 0x10 || opcode == 0x12 || opcode == 0x15 || opcode == 0x18 || opcode == 0x1A;
        private uint ReadRegister(int register) => register == 13 ? pc + 4 : registers[register];
        private void WriteRegister(int register, uint value, bool flags)
        {
            if(register == 13)
            {
                if(flags) { branchTarget = value & ~3U; branchDelay = 3; }
                else { pc = value & ~3U; branchDelay = 0; }
            }
            else
            {
                if(register != 14) registers[register] = value;
                if(flags)
                {
                    flagsMask = (ushort)((value == 0 ? 1 << 1 : 1 << 2) |
                        ((int)value >= 0 ? 1 << 3 : 1 << 4) |
                        ((int)value > 0 ? 1 << 5 : 1 << 6) |
                        (carry ? 1 << 7 : 1 << 8) | (carry && value == 0 ? 1 << 9 : 0));
                }
            }
        }
        private bool Condition(int condition)
        {
            if(condition >= 1 && condition <= 9) return (flagsMask & (1 << condition)) != 0;
            switch(condition)
            {
            case 0: case 10: return true;
            case 11: return (flagsMask & (1 << 2)) != 0;
            case 13: return (values[0x24] & 2) == 0;
            case 15: return (values[0x24] & 2) != 0;
            default: Fail(pc - 4, ReadWord(pc - 4)); return false;
            }
        }
        private static uint Shift(uint value, int kind, int amount, ref bool carryOut)
        {
            if(amount == 0) return value;
            carryOut = kind == 0 ? ((value >> (32 - amount)) & 1) != 0 : ((value >> (amount - 1)) & 1) != 0;
            switch(kind)
            {
            case 0: return value << amount;
            case 1: return unchecked((uint)((int)value >> amount));
            case 2: return value >> amount;
            default: return (value >> amount) | (value << (32 - amount));
            }
        }
        private static int SignExtend(uint value, int width) => (int)(value << (32 - width)) >> (32 - width);
        private void Fail(uint address, uint word)
        {
            this.Log(LogLevel.Warning, "Unsupported EZH instruction 0x{0:X8} at 0x{1:X8}", word, address);
            UnsupportedInstruction = true;
            pc = address; Running = false; clock.Enabled = false;
        }
        private readonly IMachine machine;
        private readonly LimitTimer clock;
        private readonly uint[] registers = new uint[16];
        private uint pc, branchTarget, heartbeatPeriod, heartbeatLeft, accessAddress;
        private uint loopStart, loopEnd, loopRemaining;
        private int branchDelay;
        private ushort flagsMask;
        private bool waitForBeat, syncToBeat;
        private bool carry;
    }
}
