// SPDX-License-Identifier: MIT
using System;
using System.Numerics;
using Antmicro.Renode.Core;
using Antmicro.Renode.Peripherals.Bus;
using Antmicro.Renode.Peripherals.I2C;
using Antmicro.Renode.Peripherals.Memory;
using Antmicro.Renode.Peripherals.Miscellaneous;
using Antmicro.Renode.Peripherals.Timers;
using Antmicro.Renode.Time;

namespace Antmicro.Renode.Peripherals.DMA
{
    // LPC DMA descriptors hold end addresses, while Renode's engine copies from
    // start addresses. Peripheral requests gate each beat, including SWTRIG.
    public class RazerLpcDma : RazerLpcAnalog, IGPIOReceiver
    {
        public RazerLpcDma(IMachine machine)
        {
            this.machine = machine;
            engine = new DmaEngine(machine.SystemBus);
            clock = new LimitTimer(machine.ClockSource, 1000000, this, "dma",
                limit: 1, workMode: WorkMode.Periodic, eventEnabled: true);
            clock.LimitReached += Service;
            Reset();
        }
        public override void Reset()
        {
            base.Reset(); values.Clear();
            foreach(var offset in new long[] {0,4,8,0x20,0x28,0x30,0x38,0x40,0x48,0x50,0x58,0x60,0x68,0x70,0x78})
                values[offset] = 0;
            for(long i = 0; i < 23; i++)
            {
                values[0x400 + i * 16] = 0;
                values[0x404 + i * 16] = 0;
                values[0x408 + i * 16] = 0;
            }
            Array.Clear(loaded, 0, loaded.Length);
            Array.Clear(requests, 0, requests.Length);
            clock?.Reset(); IRQ?.Unset();
        }
        public GPIO IRQ { get; } = new GPIO();
        public void OnGPIO(int number, bool value)
        {
            if(number < 0 || number >= 23) throw new ArgumentOutOfRangeException(nameof(number));
            requests[number] = value;
            Update();
        }
        public override void WriteDoubleWord(long offset, uint value)
        {
            value &= offset >= 0x20 && offset <= 0x78 ? 0x7FFFFFU : uint.MaxValue;
            switch(offset)
            {
            case 0:
                values[0] = value & 1;
                if(values[0] == 0)
                    for(var channel = 0; channel < 23; channel++) values[0x404 + channel * 16] &= ~4U;
                break;
            case 4: case 0x30: case 0x38: return;
            case 8: values[8] = value & 0xFFFFFE00; break;
            case 0x20: case 0x48: values[offset] |= value; break;
            case 0x28: values[0x20] &= ~value; break;
            case 0x50: values[0x48] &= ~value; break;
            case 0x40: case 0x58: case 0x60: values[offset] &= ~value; break;
            case 0x68: case 0x70: case 0x78:
                for(var channel = 0; channel < 23; channel++)
                {
                    if((value & (1U << channel)) == 0) continue;
                    var control = 0x404 + channel * 16;
                    if(offset == 0x68)
                    {
                        if((values[control + 4] & 1) != 0) values[control] |= 1;
                        values[control + 4] |= 1;
                    }
                    else if(offset == 0x70) values[control] |= 4;
                    else
                    {
                        values[control] = 0;
                        values[control + 4] &= ~1U;
                        loaded[channel] = false;
                    }
                }
                break;
            default:
                if(offset >= 0x400 && offset < 0x570 && offset % 16 == 4) return;
                if(offset >= 0x408 && offset < 0x570 && offset % 16 == 8)
                {
                    values[offset] = value & 0x03FFF33F;
                    var channel = (int)((offset - 0x408) / 16);
                    loaded[channel] = false;
                    if((value & 4) != 0) values[offset - 4] |= 4;
                }
                else base.WriteDoubleWord(offset, value);
                break;
            }
            Update();
        }
        private bool Ready(int channel)
        {
            var offset = 0x400 + channel * 16;
            return (values[offset + 8] & 1) != 0
                && (values[offset + 4] & 4) != 0
                && (requests[channel] || (values[offset] & 1) == 0);
        }
        private int NextEnabledChannel(int start)
        {
            if((values[0] & 1) == 0) return 32;
            return BitOperations.TrailingZeroCount(values[0x20] & (uint.MaxValue << start));
        }
        private void Update()
        {
            var enabledInterrupts = (values[0x58] | values[0x60]) & values[0x48];
            values[4] = (enabledInterrupts != 0 ? 2U : 0) | (values[0x40] != 0 ? 4U : 0);
            IRQ?.Set(values[4] != 0);
            uint active = 0;
            for(var channel = NextEnabledChannel(0); channel < 23; channel = NextEnabledChannel(channel + 1))
                if(Ready(channel)) active |= 1U << channel;
            values[0x30] = active;
            if(clock != null) clock.Enabled = active != 0;
        }
        private void Service()
        {
            // One beat per virtual microsecond; no bus arbitration/cycle timing.
            var statusChanged = false;
            for(var channel = NextEnabledChannel(0); channel < 23; channel = NextEnabledChannel(channel + 1))
            {
                if(!Ready(channel)) continue;
                var offset = 0x408 + channel * 16;
                var config = values[offset];
                var widthCode = (int)((config >> 8) & 3);
                if(widthCode == 3 || (values[offset - 8] & 0xC002) != 0)
                { RejectWrite(offset, config, "DMA width, hardware trigger or burst wrapping"); clock.Enabled = false; return; }
                var width = 1 << widthCode;
                var sourceStep = Step(config >> 12, width);
                var destinationStep = Step(config >> 14, width);
                if(!loaded[channel])
                {
                    if(!Load(channel, values[8] + (uint)channel * 16, config)) return;
                }
                try
                {
                    var bus = machine.SystemBus;
                    // A single byte from mapped RAM to the I2C data register
                    // uses the same bus operations as DmaEngine without allocating.
                    if(width == 1 && sourceStep == 1 && destinationStep == 0
                        && (destination[channel] & 0xFFF) == 0x828
                        && bus.WhatIsAt(source[channel])?.Peripheral is MappedMemory
                        && bus.WhatIsAt(destination[channel])?.Peripheral is RazerLpcI2c)
                    {
                        bus.ReadBytes(source[channel], 1, byteBeat, 0);
                        bus.WriteByte(destination[channel], byteBeat[0]);
                    }
                    else engine.IssueCopy(new Request(source[channel], destination[channel], width,
                        (TransferType)width, (TransferType)width, sourceStep, destinationStep,
                        sourceStep != 0, destinationStep != 0));
                }
                catch(BusAccessException)
                {
                    statusChanged = true;
                    values[0x40] |= 1U << channel;
                    values[offset] &= ~1U;
                    values[offset - 4] = 0;
                    loaded[channel] = false;
                    continue;
                }
                source[channel] += sourceStep; destination[channel] += destinationStep;
                if((config & 0x03FF0000) != 0) values[offset] -= 0x10000;
                else
                {
                    statusChanged = true;
                    var bit = 1U << channel;
                    if((config & 0x10) != 0) values[0x58] |= bit;
                    if((config & 0x20) != 0) values[0x60] |= bit;
                    // TRIG clears at the end of a complete transfer. CLRTRIG
                    // additionally clears it between reloaded descriptors.
                    if((config & 8) != 0 || (config & 2) == 0) values[offset - 4] &= ~4U;
                    values[offset] &= ~1U;
                    loaded[channel] = false;
                    var pending = (values[offset - 4] & 1) != 0;
                    values[offset - 4] &= ~1U;
                    if((config & 2) != 0 || pending)
                    {
                        var descriptor = (config & 2) != 0 ? next[channel] : values[8] + (uint)channel * 16;
                        if(descriptor == 0 || (descriptor & 15) != 0)
                        { RejectWrite(offset, config, "DMA invalid reload descriptor"); clock.Enabled = false; return; }
                        values[offset] = machine.SystemBus.ReadDoubleWord(descriptor) & 0x03FFF33F;
                        if(!Load(channel, descriptor, values[offset])) return;
                    }
                }
            }
            if(statusChanged) Update();
        }
        private bool Load(int channel, uint descriptor, uint config)
        {
            if((descriptor & 15) != 0 || !((descriptor >= 0x20000000 && descriptor <= 0x2002FFF0)
                || (descriptor >= 0x04000000 && descriptor <= 0x04007FF0)))
            {
                RejectWrite(8, descriptor, "DMA descriptor outside SRAM");
                clock.Enabled = false; return false;
            }
            var count = (config >> 16) & 0x3FF;
            var width = 1 << (int)((config >> 8) & 3);
            source[channel] = machine.SystemBus.ReadDoubleWord(descriptor + 4) - count * Step(config >> 12, width);
            destination[channel] = machine.SystemBus.ReadDoubleWord(descriptor + 8) - count * Step(config >> 14, width);
            next[channel] = machine.SystemBus.ReadDoubleWord(descriptor + 12);
            loaded[channel] = true;
            return true;
        }
        private static uint Step(uint code, int width) => (code & 3) == 0 ? 0 : (uint)(width << ((int)(code & 3) - 1));
        private readonly IMachine machine;
        private readonly DmaEngine engine;
        private readonly byte[] byteBeat = new byte[1];
        private readonly LimitTimer clock;
        private readonly bool[] requests = new bool[23], loaded = new bool[23];
        private readonly uint[] source = new uint[23], destination = new uint[23], next = new uint[23];
    }
}
