// SPDX-License-Identifier: MIT
using System;
using System.Threading;
using Antmicro.Renode.Peripherals.Timers;
using Antmicro.Renode.Peripherals.USB;
using Antmicro.Renode.Peripherals.Bus;
using Antmicro.Renode.Core;
using Antmicro.Renode.Peripherals.CPU;
using Antmicro.Renode.Peripherals.Miscellaneous;
using Antmicro.Renode.Peripherals.GPIOPort;
using Antmicro.Renode.Peripherals.LED;
using Antmicro.Renode.Time;

namespace Antmicro.Renode.Utilities
{
    // Keep firmware execution and fault inspection inside the CLR. Sync hooks
    // run while all processors are quiescent, so register/MMIO reads are safe.
    public class RazerExecutionRunner
    {
        public RazerExecutionRunner(Emulation emulation, IMachine machine, CortexM cpu,
            RazerLpcAnalog[] peripherals, RazerLpcSyscon syscon, RazerLpcGpio gpio)
        {
            this.emulation = emulation; this.machine = machine; this.cpu = cpu;
            this.peripherals = peripherals; this.syscon = syscon; this.gpio = gpio;
        }
        public ulong Run(ulong duration, ulong initialReads, ulong initialWrites,
            RazerLpcWatchdog watchdog, RazerLpcEzh ezh, RazerLpcUsbDevice usb,
            RazerChroma192[] lighting)
        {
            // Tiny USB polling runs already return to Python for state inspection.
            // Avoid creating a dispatcher thread for each individual polling tick.
            if(duration <= 10)
            {
                emulation.RunFor(TimeInterval.FromMicroseconds(duration));
                return duration;
            }
            var source = emulation.MasterTimeSource;
            var start = source.ElapsedVirtualTime;
            var previousQuantum = source.Quantum;
            var previousAdvance = source.AdvanceImmediately;
            var target = TimeInterval.FromMicroseconds(duration);
            Exception failure = null;
            using(var done = new ManualResetEventSlim())
            {
                Action<TimeInterval> check = now =>
                {
                    try
                    {
                        var stop = now - start >= target;
                        var reads = (ulong)((SystemBus)machine.SystemBus).UnexpectedReads + syscon.UnsupportedReads + gpio.UnsupportedReads;
                        var writes = (ulong)((SystemBus)machine.SystemBus).UnexpectedWrites + syscon.UnsupportedWrites + gpio.UnsupportedWrites;
                        foreach(var peripheral in peripherals)
                        { reads += peripheral.UnsupportedReads; writes += peripheral.UnsupportedWrites; }
                        var exception = cpu.CPSR.RawValue & 0x1FF;
                        if(watchdog.Expired || ezh.BusFault || ezh.UnsupportedInstruction || usb.TransferError != null ||
                            reads != initialReads || writes != initialWrites ||
                            (exception >= 3 && exception <= 6) || machine.SystemBus.ReadDoubleWord(0xE000ED28) != 0) stop = true;
                        foreach(var controller in lighting) if(controller.UnsupportedOperation != null) stop = true;
                        if(stop) { source.Stop(); done.Set(); }
                        else if(target - (now - start) < source.Quantum) source.Quantum = target - (now - start);
                    }
                    catch(Exception exception)
                    {
                        failure = exception;
                        source.Stop();
                        done.Set();
                    }
                };
                source.Quantum = TimeInterval.FromMicroseconds(Math.Min(10UL, duration));
                source.AdvanceImmediately = true;
                source.SyncHook += check;
                try
                {
                    emulation.ExternalsManager.Start();
                    foreach(var m in emulation.Machines) m.Start();
                    source.Start();
                    done.Wait();
                }
                finally
                {
                    emulation.PauseAll();
                    source.SyncHook -= check;
                    source.Quantum = previousQuantum;
                    source.AdvanceImmediately = previousAdvance;
                }
                if(failure != null) throw new InvalidOperationException("Execution monitoring failed", failure);
                return (source.ElapsedVirtualTime - start).Ticks / TimeInterval.FromMicroseconds(1).Ticks;
            }
        }
        private readonly Emulation emulation;
        private readonly IMachine machine;
        private readonly CortexM cpu;
        private readonly RazerLpcAnalog[] peripherals;
        private readonly RazerLpcSyscon syscon;
        private readonly RazerLpcGpio gpio;
    }
}
