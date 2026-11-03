// SPDX-License-Identifier: MIT
using System;
using System.Collections;
using System.Linq.Expressions;
using System.Reflection;
using Antmicro.Renode.Core;
using Antmicro.Renode.Peripherals.IRQControllers;
using Antmicro.Renode.Time;
using Antmicro.Renode.Peripherals.Bus;
using Antmicro.Renode.Peripherals.Bus.Wrappers;
using Antmicro.Renode.Peripherals;
using Antmicro.Renode.Peripherals.Miscellaneous;
using Antmicro.Renode.Utilities.Binding;
namespace Antmicro.Renode.Peripherals.CPU
{
    public interface IRazerReusableCpu { void BeginBoundedRun(); void HardPauseForControl(); }
    public class RazerCortexM : CortexM, IHasOwnLife, IRazerReusableCpu
    {
        public RazerCortexM(string cpuType, IMachine machine, NVIC nvic) : base(cpuType, machine, nvic) { boardMachine = machine; boardNvic = nvic; }
        public void ConfigureWatchdogFastPath(IDoubleWordPeripheral watchdog) { fastWatchdog = watchdog; }
        public ulong FastWatchdogWrites { get; private set; }
        public bool WatchdogFastPathEnabled { get; set; } = true;
        public void PrepareFastPeripheralAccess(bool allowCachedDispatch = true)
        {
            fastNonsecure = fastSecure = null;
            fastDwt = null;
            if(!allowCachedDispatch || UpdateContextOnLoadAndStore || TrustZoneEnabled || ExternalMmuWindowsCount != 0 || HasInstrumentation()) return;
            if(!(boardMachine is Machine machine) || machine.GdbStubs.Count != 0 || machine.Profiler != null || !(machine.SystemBus is SystemBus bus)) return;
            if(HasBusInstrumentation(bus)) return;
            if(WatchdogFastPathEnabled && fastWatchdog != null && bus.IsPeripheralEnabled((IPeripheral)fastWatchdog))
            {
                fastNonsecure = ResolveFastAccess(bus, 0x4000C008);
                fastSecure = ResolveFastAccess(bus, 0x5000C008);
            }

            const ulong counterAddress = 0xE0001004;
            foreach(var tag in bus.Tags) if(tag.Key.Contains(counterAddress)) return;
            if(CheckExternalPermissions(counterAddress) != 0 &&
                !bus.IsAddressRangeLocked(new Antmicro.Renode.Core.Range(counterAddress, 4), this) &&
                !bus.TryGetWatchpointsAt(counterAddress, Access.Read, out _) &&
                bus.WhatPeripheralIsAt(counterAddress, this) is RazerDwt dwt &&
                bus.IsPeripheralEnabled(dwt) &&
                findAccess(bus, counterAddress, this, out var counter, out var start, out var offset, null) &&
                counterAddress - start + offset == 4 && counter.SetAbsoluteAddress == null &&
                ReferenceEquals(counter.ReadDoubleWord.Target, dwt))
            {
                fastDwt = dwt;
            }
        }
        private bool HasInstrumentation()
        {
            foreach(var field in hookFields)
            {
                if(field == null) return true;
                var value = field.GetValue(this);
                if(value == null) continue;
                if(field.Name != "wfiStateChangeHook") return true;
                // The NVIC's own WFI observer is part of the board, not user instrumentation.
                foreach(var callback in ((Delegate)value).GetInvocationList())
                    if(!ReferenceEquals(callback.Target, boardNvic) || callback.Method != nvicWfiMethod) return true;
            }
            var descriptor = hookDescriptor?.GetValue(this);
            return descriptor == null || !(hookDictionary?.GetValue(descriptor) is ICollection hooks) || hooks.Count != 0;
        }
        private bool HasBusInstrumentation(SystemBus bus)
        {
            foreach(var field in busHookFields)
                if(field == null || !(field.GetValue(bus) is ICollection hooks) || hooks.Count != 0) return true;
            foreach(var entry in boardMachine.GetRegisteredPeripherals())
            {
                if(!(entry.RegistrationPoint is BusRangeRegistration registration)) continue;
                if(registration.Condition != null || registration.StateMask.HasValue) return true;
                if(findAccess(bus, registration.Range.StartAddress, this, out var access, out var _, out var _, null) &&
                    (access.ReadByte?.Target is HookWrapper || access.ReadWord?.Target is HookWrapper ||
                     access.ReadDoubleWord?.Target is HookWrapper || access.ReadQuadWord?.Target is HookWrapper ||
                     access.WriteByte?.Target is HookWrapper || access.WriteWord?.Target is HookWrapper ||
                     access.WriteDoubleWord?.Target is HookWrapper || access.WriteQuadWord?.Target is HookWrapper)) return true;
            }
            return false;
        }
        private FastAccess ResolveFastAccess(SystemBus bus, ulong address)
        {
            if(CheckExternalPermissions(address) == 0 || bus.IsAddressRangeLocked(new Antmicro.Renode.Core.Range(address, 4), this) || bus.TryGetWatchpointsAt(address, Access.Write, out var _)) return null;
            foreach(var tag in bus.Tags) if(tag.Key.Contains(address)) return null;
            if(!ReferenceEquals(bus.WhatPeripheralIsAt(address, this), fastWatchdog)) return null;
            if(!findAccess(bus, address, this, out var access, out var start, out var offset, null)) return null;
            if((address - start) + offset != 8 || access.SetAbsoluteAddress != null || !ReferenceEquals(access.WriteDoubleWord.Target, fastWatchdog)) return null;
            return new FastAccess(access);
        }
        [Export]
        protected new void WriteDoubleWordToBus(ulong address, ulong value, ulong cpuState)
        {
            var access = address == 0x4000C008 ? fastNonsecure : address == 0x5000C008 ? fastSecure : null;
            if(access != null && ExecutionMode == ExecutionMode.Continuous && !UpdateContextOnLoadAndStore && ReferenceEquals(access.Methods.WriteDoubleWord, access.Write))
            {
                try
                {
                    lock(access.Methods.Lock) access.Write(8, (uint)value);
                    FastWatchdogWrites++;
                }
                catch(BusAccessException exception)
                {
                    HandleBusAccessError(address, SysbusAccessWidth.DoubleWord, BusAccess.Operation.Write, exception.Error);
                }
                return;
            }
            base.WriteDoubleWordToBus(address, value, cpuState);
        }
        [Export]
        protected new ulong ReadDoubleWordFromBus(ulong address, ulong cpuState)
        {
            if(address == 0xE0001004 && fastDwt != null && IsExecutingContinuously &&
                !UpdateContextOnLoadAndStore)
            {
                var value = fastDwt.ReadCycleCounter(this);
                if(fastDwt.CanAdvanceWith(this) && ConfigureNativeDwt != null)
                {
                    // Renode reports time in whole dispatch quanta. Use the
                    // same rounding in the native MMIO callback.
                    var cycles = ExecutedInstructions - dispatchNativeTotal + dispatchResiduum;
                    var rounded = cycles - cycles % dispatchCycleQuantum;
                    ConfigureNativeDwt(unchecked(dispatchNativeTotal - dispatchResiduum),
                        dispatchCycleQuantum, unchecked(value - (uint)rounded));
                    nativeDwtActive = true;
                }
                return value;
            }
            return base.ReadDoubleWordFromBus(address, cpuState);
        }

        internal void InvalidateNativeDwt()
        {
            if(!nativeDwtActive) return;
            ConfigureNativeDwt(0, 0, 0);
            nativeDwtActive = false;
        }

        [Import(Name = "razer_configure_dwt", Optional = true, UseExceptionWrapper = false)]
        private Action<ulong, ulong, uint> ConfigureNativeDwt;

        private bool nativeDwtActive;
        private RazerDwt fastDwt;
        private delegate bool FindAccess(SystemBus bus, ulong address, IPeripheral context, out PeripheralAccessMethods access, out ulong start, out ulong offset, ulong? state);
        private static readonly FindAccess findAccess = (FindAccess)typeof(SystemBus).GetMethod("TryFindPeripheralAccessMethods", BindingFlags.Instance | BindingFlags.NonPublic).CreateDelegate(typeof(FindAccess));
        private readonly IMachine boardMachine;
        private readonly NVIC boardNvic;
        private static readonly MethodInfo nvicWfiMethod = typeof(NVIC).GetMethod("HandleWfiStateChange", BindingFlags.Instance | BindingFlags.NonPublic);
        private IDoubleWordPeripheral fastWatchdog;
        private FastAccess fastNonsecure, fastSecure;
        private sealed class FastAccess
        {
            public FastAccess(PeripheralAccessMethods methods) { Methods = methods; Write = methods.WriteDoubleWord; }
            public readonly PeripheralAccessMethods Methods;
            public readonly BusAccess.DoubleWordWriteMethod Write;
        }
        private static readonly FieldInfo[] hookFields = Array.ConvertAll(new[] {
            "blockBeginInternalHook", "blockBeginUserHook", "blockFinishedHook", "memoryAccessHook",
            "interruptBeginHook", "interruptEndHook", "wfiStateChangeHook", "mmuFaultHook"
        }, name => FindField(typeof(TranslationCPU), name));
        private static readonly FieldInfo[] busHookFields = Array.ConvertAll(new[] { "hooksOnRead", "hooksOnWrite" },
            name => FindField(typeof(SystemBus), name));
        private static readonly FieldInfo hookDescriptor = FindField(typeof(TranslationCPU), "hooks");
        private static readonly FieldInfo hookDictionary = FindField(hookDescriptor?.FieldType, "hooks");
        private static FieldInfo FindField(Type type, string name)
        {
            for(; type != null; type = type.BaseType)
            {
                var field = type.GetField(name, BindingFlags.Instance | BindingFlags.NonPublic | BindingFlags.DeclaredOnly);
                if(field != null) return field;
            }
            return null;
        }
        public TimeInterval CurrentExecutionTime
        {
            get
            {
                if(inExecute)
                    return dispatchTime + TimeInterval.FromTicks((ExecutedInstructions - dispatchNativeTotal + dispatchResiduum) / dispatchCycleQuantum * dispatchTicksPerQuantum);
                var pending = ExecutedInstructions - reportedNativeTotal;
                return TimeHandle.TotalElapsedTime + TimeInterval.FromCPUCycles(pending + getResiduum(this), PerformanceInMips, out var _);
            }
        }
        public override void SyncTime()
        {
            if(OnPossessedThread) reportedNativeTotal = ExecutedInstructions;
            base.SyncTime();
        }
        public bool IsExecutingContinuously => inExecute && ExecutionMode == ExecutionMode.Continuous;
        public ulong DispatchGeneration { get; private set; }
        public override ExecutionResult ExecuteInstructions(ulong count, out ulong executed)
        {
            DispatchGeneration++;
            dispatchTime = TimeHandle.TotalElapsedTime;
            dispatchResiduum = getResiduum(this);
            dispatchNativeTotal = ExecutedInstructions;
            var mips = PerformanceInMips;
            var gcd = Antmicro.Renode.Utilities.Misc.GCD(mips, TimeInterval.TicksPerMicrosecond);
            dispatchCycleQuantum = mips / gcd;
            dispatchTicksPerQuantum = TimeInterval.TicksPerMicrosecond / gcd;
            inExecute = true;
            try { return base.ExecuteInstructions(count, out executed); }
            finally
            {
                InvalidateNativeDwt();
                inExecute = false;
                reportedNativeTotal = ExecutedInstructions;
            }
        }
        public override void Reset()
        {
            HardPauseForControl();
            base.Reset(); inExecute = false; reportedNativeTotal = 0;
        }
        public void BeginBoundedRun() { reuseRequested = true; }
        public void HardPauseForControl()
        {
            lock(pauseLock)
            {
                reuseRequested = false;
                if(!parked) return;
                parked = false;
                coldHandle = true;
                // The native thread is blocked on its disabled time handle.
                // Use the ordinary guarded pause path to interrupt and join it.
                isPausedRequested = false;
                base.OnPause();
            }
        }
        protected override void RequestPause()
        {
            if(reuseRequested && !isAborted && !isPaused && !DebuggerConnected &&
                !OnPossessedThread && ExecutionMode == ExecutionMode.Continuous &&
                TimeHandle != null && TimeHandle.IsReadyForNewTimeGrant)
            {
                TimeHandle.Enabled = false;
                parked = true;
                ParkCount++;
                return;
            }
            base.RequestPause();
        }
        protected override void OnPause()
        {
            if(parked)
            {
                isPausedRequested = true;
                InvokeHalted(new HaltArguments(HaltReason.Pause, this));
                return;
            }
            base.OnPause();
        }
        public override void Start()
        {
            if(parked) ResumeParked();
            else { base.Start(); EnableColdHandle(); }
        }
        void IHasOwnLife.Resume()
        {
            if(parked) ResumeParked();
            else { base.Resume(); EnableColdHandle(); }
        }
        private void EnableColdHandle()
        {
            if(coldHandle && ExecutionMode == ExecutionMode.Continuous)
            {
                coldHandle = false;
                TimeHandle.Enabled = true;
            }
        }
        private void ResumeParked()
        {
            lock(pauseLock)
            {
                isPausedRequested = false;
                parked = false;
                TimeHandle.Enabled = true;
            }
        }
        public override void Dispose()
        {
            HardPauseForControl();
            base.Dispose();
        }
        public int ParkCount { get; private set; }
        private bool reuseRequested, parked, coldHandle;

        private static Func<BaseCPU, ulong> CreateResiduumGetter()
        {
            var cpu = Expression.Parameter(typeof(BaseCPU));
            return Expression.Lambda<Func<BaseCPU, ulong>>(Expression.Field(cpu, typeof(BaseCPU).GetField("executedResiduum", BindingFlags.Instance | BindingFlags.NonPublic)), cpu).Compile();
        }
        private static readonly Func<BaseCPU, ulong> getResiduum = CreateResiduumGetter();
        private ulong reportedNativeTotal;
        private TimeInterval dispatchTime;
        private ulong dispatchNativeTotal, dispatchResiduum, dispatchCycleQuantum, dispatchTicksPerQuantum;
        private bool inExecute;
    }
}

namespace Antmicro.Renode.Peripherals.Miscellaneous
{
    using Antmicro.Renode.Peripherals.CPU;

    // Keep Renode's DWT register model, but avoid synchronizing the machine
    // clock on every CYCCNT read in a firmware polling loop.
    public class RazerDwt : DWT
    {
        public RazerDwt(IMachine machine, uint frequency) : base(machine, frequency)
        {
            this.machine = machine;
            this.frequency = frequency;
        }

        internal bool CanAdvanceWith(RazerCortexM cpu) => enabled && HasCpuFrequency(cpu);

        private bool HasCpuFrequency(RazerCortexM cpu) =>
            cpu.PerformanceInMips * 1000000UL == frequency;

        internal uint ReadCycleCounter(RazerCortexM cpu)
        {
            if(!HasCpuFrequency(cpu))
                return base.ReadDoubleWord(CycleCounter);
            if(anchorCpu != cpu || anchorDispatch != cpu.DispatchGeneration)
            {
                anchorValue = base.ReadDoubleWord(CycleCounter);
                anchorTime = cpu.CurrentExecutionTime.Ticks;
                anchorCpu = cpu;
                anchorDispatch = cpu.DispatchGeneration;
            }
            if(!enabled) return anchorValue;
            var ticks = cpu.CurrentExecutionTime.Ticks - anchorTime;
            return unchecked(anchorValue + (uint)(ticks * frequency / TimeInterval.TicksPerSecond));
        }

        public override void WriteDoubleWord(long offset, uint value)
        {
            if(offset == Control || offset == CycleCounter)
            {
                if(machine.SystemBus.TryGetCurrentCPU(out var cpu)) cpu.SyncTime();
                anchorCpu?.InvalidateNativeDwt();
                anchorCpu = null;
                if(offset == Control) enabled = (value & 1) != 0;
            }
            base.WriteDoubleWord(offset, value);
        }

        public override void Reset()
        {
            anchorCpu?.InvalidateNativeDwt();
            anchorCpu = null;
            enabled = false;
            base.Reset();
        }

        private const long Control = 0, CycleCounter = 4;
        private readonly IMachine machine;
        private readonly uint frequency;
        private RazerCortexM anchorCpu;
        private ulong anchorDispatch, anchorTime;
        private uint anchorValue;
        private bool enabled;
    }
}
