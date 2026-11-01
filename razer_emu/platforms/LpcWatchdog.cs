// SPDX-License-Identifier: MIT
using Antmicro.Renode.Core;
using Antmicro.Renode.Peripherals.Miscellaneous;
using Antmicro.Renode.Time;
using Antmicro.Renode.Peripherals.CPU;
namespace Antmicro.Renode.Peripherals.Timers
{
    public class RazerLpcWatchdog : RazerLpcAnalog
    {
        public RazerLpcWatchdog(IMachine machine)
        {
            this.machine = machine;
            timer = new LimitTimer(machine.ClockSource, 1000000000, this, "watchdog",
                limit: 0xFFFFFFUL * 4000, workMode: WorkMode.OneShot, eventEnabled: true);
            timer.LimitReached += CheckDeadline;
            Reset();
        }
        public override void Reset()
        {
            base.Reset(); values.Clear();
            mode = timeout = 0;
            window = 0xFFFFFF;
            timer?.Reset(); Expired = false; feed = 0;
            active = false; deadline = scheduledDeadline = origin = 0; stoppedValue = 0xFFFFFF;
        }
        private ulong Now
        {
            get
            {
                if(arm == null)
                    foreach(var cpu in machine.SystemBus.GetCPUs())
                        if(cpu is RazerCortexM candidate) { arm = candidate; break; }
                if(arm != null && arm.OnPossessedThread) return arm.CurrentExecutionTime.Ticks;
                return machine.ClockSource.CurrentValue.Ticks;
            }
        }
        private uint CurrentValue => active ? ValueAt(Now) : stoppedValue;
        private uint ValueAt(ulong now)
        {
            var expires = deadline;
            return active ? (expires > now ? (uint)((expires - now + 3999) / 4000) : 0) : stoppedValue;
        }
        public override uint ReadDoubleWord(long offset)
        {
            if(offset == 0) return mode;
            if(offset == 4) return timeout;
            if(offset == 0x18) return window;
            if(offset == 0x14) return 0;
            if(offset == 0xC) return CurrentValue;
            if(offset == 8) return 0;
            return base.ReadDoubleWord(offset);
        }
        public override void WriteDoubleWord(long offset, uint value)
        {
            if(offset == 0) { mode = (value & 0x13) | (mode & 1); return; }
            if(offset == 4) { timeout = value & 0xFFFFFF; return; }
            if(offset == 0x18) { window = value; return; }
            if(offset == 8)
            {
                if(feed == 0xAA && value == 0x55)
                {
                    var now = Now;
                    if(active && window < 0xFFFFFF && ValueAt(now) > window) { Expired = true; return; }
                    stoppedValue = System.Math.Max(0xFFU, timeout);
                    if(!active) origin = now;
                    deadline = now + (ulong)stoppedValue * 4000 - (now - origin) % 4000;
                    var enable = (mode & 1) != 0;
                    if(enable && (!active || deadline < scheduledDeadline)) Schedule();
                    else if(!enable && active) timer.Enabled = false;
                    active = enable;
                    feed = 0;
                }
                else feed = value == 0xAA ? value : 0;
                return;
            }
            if(offset == 0x14)
            {
                if(value != 0) base.WriteDoubleWord(0x1014, value);
                return;
            }
            base.WriteDoubleWord(offset, value);
        }
        private void Schedule()
        {
            scheduledDeadline = deadline;
            var globalNow = machine.ClockSource.CurrentValue.Ticks;
            timer.Limit = deadline > globalNow ? deadline - globalNow : 1;
            timer.ResetValue(); timer.Enabled = true;
        }
        private void CheckDeadline()
        {
            if(!active) return;
            if(machine.ClockSource.CurrentValue.Ticks < deadline) { Schedule(); return; }
            active = false; stoppedValue = 0;
            Expired = true; mode |= 12;
        }
        public bool Expired { get; private set; }
        private RazerCortexM arm;
        private readonly IMachine machine;
        private readonly LimitTimer timer;
        private ulong deadline, scheduledDeadline, origin;
        private uint feed, stoppedValue, mode, timeout, window;
        private bool active;
    }
}
