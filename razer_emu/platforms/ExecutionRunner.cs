// SPDX-License-Identifier: MIT
using System;
using System.Reflection;
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
    // Drives Renode on the owning thread. Device service and register inspection
    // happen inside safe synchronization hooks; CPU threads stay alive for the
    // entire bounded run, including interrupt endpoint polling slots.
    public class RazerExecutionRunner : IGPIOReceiver
    {
        public RazerExecutionRunner(Emulation emulation, IMachine machine, CortexM cpu,
            RazerLpcAnalog[] peripherals, RazerLpcSyscon syscon, RazerLpcGpio gpio)
        {
            this.emulation = emulation; this.machine = machine; this.cpu = cpu;
            this.peripherals = peripherals; this.syscon = syscon; this.gpio = gpio;
        }
        public bool ResetRequested { get; private set; }
        public void OnGPIO(int number, bool value) { if(value) ResetRequested = true; }
        public void Reset() { ResetRequested = false; }
        public ulong CheckIntervalUs
        {
            get => checkIntervalUs;
            set
            {
                if(value < 1 || value > 1000) throw new ArgumentOutOfRangeException(nameof(value));
                checkIntervalUs = value;
            }
        }
        public ulong LastElapsedUs { get; private set; }
        public ulong Run(ulong duration, ulong initialReads, ulong initialWrites,
            RazerLpcWatchdog watchdog, RazerLpcEzh ezh, RazerLpcUsbDevice usb,
            RazerChroma192[] lighting)
        {
            return RunServiced(duration, initialReads, initialWrites, watchdog, ezh, usb, lighting, null, 0);
        }
        public ulong RunServiced(ulong duration, ulong initialReads, ulong initialWrites,
            RazerLpcWatchdog watchdog, RazerLpcEzh ezh, RazerLpcUsbDevice usb,
            RazerChroma192[] lighting, Action<TimeInterval> service, ulong serviceIntervalUs)
        {
            LastElapsedUs = 0;
            if(service != null && serviceIntervalUs == 0) throw new ArgumentOutOfRangeException(nameof(serviceIntervalUs));
            return RunScheduled(duration, initialReads, initialWrites, watchdog, ezh, usb, lighting,
                service == null ? (Func<TimeInterval, TimeInterval>)null : relative =>
                {
                    service(relative);
                    return TimeInterval.FromMicroseconds(serviceIntervalUs);
                });
        }
        public ulong RunScheduled(ulong duration, ulong initialReads, ulong initialWrites,
            RazerLpcWatchdog watchdog, RazerLpcEzh ezh, RazerLpcUsbDevice usb,
            RazerChroma192[] lighting, Func<TimeInterval, TimeInterval> service)
        {
            LastElapsedUs = 0;
            (cpu as RazerCortexM)?.PrepareFastPeripheralAccess(service == null ||
                (service.Target is RazerUsbInterruptPump pump && service == pump.ScheduledServiceHook));
            if(duration == 0) throw new ArgumentOutOfRangeException(nameof(duration));
            var source = emulation.MasterTimeSource;
            var start = source.ElapsedVirtualTime;
            var previousQuantum = source.Quantum;
            var previousAdvance = source.AdvanceImmediately;
            var target = TimeInterval.FromMicroseconds(duration);
            var nextService = TimeInterval.Empty;
            var stop = false;
            Exception failure = null;
            Action<TimeInterval> check = now =>
            {
                try
                {
                    var relative = now - start;
                    var reads = (ulong)((SystemBus)machine.SystemBus).UnexpectedReads + syscon.UnsupportedReads + gpio.UnsupportedReads;
                    var writes = (ulong)((SystemBus)machine.SystemBus).UnexpectedWrites + syscon.UnsupportedWrites + gpio.UnsupportedWrites;
                    foreach(var peripheral in peripherals)
                    { reads += peripheral.UnsupportedReads; writes += peripheral.UnsupportedWrites; }
                    var exception = cpu.CPSR.RawValue & 0x1FF;
                    stop = ResetRequested || watchdog.Expired || ezh.BusFault || ezh.UnsupportedInstruction || usb.TransferError != null ||
                        reads != initialReads || writes != initialWrites ||
                        (exception >= 3 && exception <= 6) || machine.SystemBus.ReadDoubleWord(0xE000ED28) != 0;
                    foreach(var controller in lighting) if(controller.UnsupportedOperation != null) stop = true;
                    if(!stop && service != null && relative >= nextService)
                    {
                        var wait = service(relative);
                        if(wait.Ticks == 0) throw new ArgumentOutOfRangeException("Service callback delay must be positive");
                        nextService = relative + wait;
                    }
                    if(relative < target) source.Quantum = NextQuantum(target - relative, service == null ? (TimeInterval?)null : nextService - relative);
                }
                catch(Exception exception)
                {
                    failure = exception; stop = true;
                }
            };
            source.Quantum = NextQuantum(target, null);
            source.AdvanceImmediately = true;
            source.SyncHook += check;
            try
            {
                if(service != null)
                {
                    nextService = service(TimeInterval.Empty);
                    if(nextService.Ticks == 0) throw new ArgumentOutOfRangeException("Service callback delay must be positive");
                    source.Quantum = NextQuantum(target, nextService);
                }
                foreach(var processor in machine.SystemBus.GetCPUs())
                    if(processor is IRazerReusableCpu reusable) reusable.BeginBoundedRun();
                emulation.ExternalsManager.Start();
                foreach(var m in emulation.Machines) m.Start();
                // These are the same lifetime scopes used by MasterTimeSource.RunFor.
                // Its public loop cannot stop early on a model fault. Open delegates
                // expose the protected scheduling primitives without mutating state.
                using(obtainStarted(source))
                using(obtainActive(source))
                using(TimeDomainsManager.Instance.RegisterCurrentThread(() => new TimeStamp(source.ElapsedVirtualTime, source.Domain)))
                {
                    var remaining = target;
                    while(remaining.Ticks != 0 && !stop)
                    {
                        waitIfBlocked(source);
                        executeChunk(source, out var elapsed, remaining);
                        remaining -= elapsed;
                    }
                }
            }
            finally
            {
                emulation.PauseAll();
                source.SyncHook -= check;
                source.Quantum = previousQuantum;
                source.AdvanceImmediately = previousAdvance;
                LastElapsedUs = (source.ElapsedVirtualTime - start).Ticks / TimeInterval.FromMicroseconds(1).Ticks;
            }
            if(failure != null) throw new InvalidOperationException("Emulation service failed", failure);
            return LastElapsedUs;
        }
        private TimeInterval NextQuantum(TimeInterval remaining, TimeInterval? untilService)
        {
            var result = TimeInterval.FromMicroseconds(checkIntervalUs);
            if(remaining < result) result = remaining;
            if(untilService.HasValue && untilService.Value < result) result = untilService.Value;
            return result;
        }
        private delegate bool ExecuteChunk(TimeSourceBase source, out TimeInterval elapsed, TimeInterval? limit);
        private static readonly BindingFlags protectedMethods = BindingFlags.Instance | BindingFlags.NonPublic;
        private static readonly Func<TimeSourceBase, IDisposable> obtainStarted = (Func<TimeSourceBase, IDisposable>)typeof(TimeSourceBase).GetMethod("ObtainStartedState", protectedMethods).CreateDelegate(typeof(Func<TimeSourceBase, IDisposable>));
        private static readonly Func<TimeSourceBase, IDisposable> obtainActive = (Func<TimeSourceBase, IDisposable>)typeof(TimeSourceBase).GetMethod("ObtainSourceActiveState", protectedMethods).CreateDelegate(typeof(Func<TimeSourceBase, IDisposable>));
        private static readonly Action<TimeSourceBase> waitIfBlocked = (Action<TimeSourceBase>)typeof(TimeSourceBase).GetMethod("WaitIfBlocked", protectedMethods).CreateDelegate(typeof(Action<TimeSourceBase>));
        private static readonly ExecuteChunk executeChunk = (ExecuteChunk)typeof(TimeSourceBase).GetMethod("InnerExecute", protectedMethods).CreateDelegate(typeof(ExecuteChunk));
        private ulong checkIntervalUs = 10;
        private readonly Emulation emulation;
        private readonly IMachine machine;
        private readonly CortexM cpu;
        private readonly RazerLpcAnalog[] peripherals;
        private readonly RazerLpcSyscon syscon;
        private readonly RazerLpcGpio gpio;
    }
}
