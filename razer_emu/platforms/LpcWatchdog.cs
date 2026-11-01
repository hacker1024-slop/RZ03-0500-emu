// SPDX-License-Identifier: MIT
using Antmicro.Renode.Core;
using Antmicro.Renode.Peripherals.Miscellaneous;
using Antmicro.Renode.Time;

namespace Antmicro.Renode.Peripherals.Timers
{
    public class RazerLpcWatchdog : RazerLpcAnalog
    {
        public RazerLpcWatchdog(IMachine machine)
        {
            timer = new LimitTimer(machine.ClockSource, 1000000, this, "watchdog",
                limit: 0xFFFFFF, divider: 4, workMode: WorkMode.OneShot, eventEnabled: true);
            timer.LimitReached += () => { Expired = true; values[0] |= 12; };
            Reset();
        }
        public override void Reset()
        {
            base.Reset(); values.Clear();
            values[0] = values[4] = values[0x14] = 0;
            values[0x18] = 0xFFFFFF;
            timer?.Reset(); Expired = false; feed = 0;
        }
        public override uint ReadDoubleWord(long offset)
        {
            if(offset == 0xC) return (uint)timer.Value;
            if(offset == 8) return 0; // feed register is write-only
            return base.ReadDoubleWord(offset);
        }
        public override void WriteDoubleWord(long offset, uint value)
        {
            if(offset == 0) { values[0] = (value & 0x13) | (values[0] & 1); return; }
            if(offset == 4) { values[4] = value & 0xFFFFFF; return; }
            if(offset == 8)
            {
                if(feed == 0xAA && value == 0x55)
                {
                    if(timer.Enabled && timer.Value > values[0x18]) { Expired = true; return; }
                    timer.Limit = System.Math.Max(0xFFU, values[4]);
                    timer.ResetValue();
                    timer.Enabled = (values[0] & 1) != 0;
                    feed = 0;
                }
                else feed = value == 0xAA ? value : 0;
                return;
            }
            // Warning IRQ support is required before nonzero warning windows.
            if(offset == 0x14 && value != 0) { base.WriteDoubleWord(0x1014, value); return; }
            base.WriteDoubleWord(offset, value);
        }
        public bool Expired { get; private set; }
        private readonly LimitTimer timer;
        private uint feed;
    }
}
