// SPDX-License-Identifier: MIT
using Antmicro.Renode.Core;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.Bus;
using Antmicro.Renode.Peripherals.Miscellaneous;
using Antmicro.Renode.Time;

namespace Antmicro.Renode.Peripherals.Timers
{
    // Reuses Renode's virtual-time ComparingTimer. Unlike LPC_CTimer, this
    // handles all four match channels; this firmware uses MR1, not MR0.
    public class RazerLpcTimer : RazerLpcAnalog
    {
        public RazerLpcTimer(IMachine machine, ulong frequency = 96000000)
        {
            timers = new ComparingTimer[4];
            IRQ = new GPIO();
            for(var i = 0; i < 4; i++)
            {
                var channel = i;
                timers[i] = new ComparingTimer(machine.ClockSource, frequency, this,
                    "match" + i, uint.MaxValue, compare: uint.MaxValue, eventEnabled: true, workMode: WorkMode.Periodic);
                timers[i].CompareReached += () => Match(channel);
            }
            Reset();
        }
        public override void Reset()
        {
            base.Reset();
            values.Clear();
            foreach(var offset in new long[] {0,4,8,12,16,20,24,28,32,36,40,60,112,116})
                values[offset] = 0;
            if(timers == null) return; // base constructor
            foreach(var timer in timers) timer.Reset();
            IRQ.Unset();
        }
        public override uint ReadDoubleWord(long offset)
        {
            if(offset == 8) return (uint)timers[0].Value;
            return base.ReadDoubleWord(offset);
        }
        public override void WriteDoubleWord(long offset, uint value)
        {
            switch(offset)
            {
            case 0:
                values[0] &= ~value;
                UpdateIrq(); return;
            case 4:
                values[4] = value & 3;
                foreach(var timer in timers)
                {
                    if((value & 2) != 0) timer.Value = 0;
                    timer.Enabled = (value & 3) == 1;
                }
                return;
            case 8:
                foreach(var timer in timers) timer.Value = value;
                return;
            case 12:
                values[12] = value;
                foreach(var timer in timers) timer.Divider = (ulong)value + 1;
                return;
            case 20:
                values[20] = value;
                UpdateIrq(); return;
            case 24: case 28: case 32: case 36:
                values[offset] = value;
                timers[(offset - 24) / 4].Compare = value;
                return;
            case 40: case 60: case 112: case 116:
                if(value != 0)
                {
                    // Capture, external match, counter and PWM modes need models.
                    base.WriteDoubleWord(0x1000 + offset, value); return;
                }
                break;
            }
            base.WriteDoubleWord(offset, value);
        }
        private void Match(int channel)
        {
            var control = values[20] >> (channel * 3);
            if((control & 1) != 0) values[0] |= (uint)(1 << channel);
            if((control & 2) != 0)
                foreach(var timer in timers) timer.Value = 0;
            if((control & 4) != 0)
            {
                values[4] &= ~1U;
                foreach(var timer in timers) timer.Enabled = false;
            }
            UpdateIrq();
        }
        private void UpdateIrq()
        {
            uint enabled = 0;
            for(var i = 0; i < 4; i++) if((values[20] & (1U << (3 * i))) != 0) enabled |= 1U << i;
            IRQ.Set((values[0] & enabled) != 0);
        }
        public GPIO IRQ { get; }
        private readonly ComparingTimer[] timers;
    }
}
