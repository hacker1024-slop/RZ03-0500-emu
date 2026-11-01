// SPDX-License-Identifier: MIT
using System;
using Antmicro.Renode.Core;
using Antmicro.Renode.Peripherals.Miscellaneous;
using ELFSharp.ELF;
namespace Antmicro.Renode.Peripherals.CPU
{
    public class RazerEzhCpu : BaseCPU, IHasOwnLife, IRazerReusableCpu
    {
        public RazerEzhCpu(IMachine machine, RazerLpcEzh ezh) : base(1, "ezh-b", machine, Endianess.LittleEndian)
        {
            this.ezh = ezh;
            ezh.ExternalClock = true;
            PerformanceInMips = 96;
        }
        public override ExecutionResult ExecuteInstructions(ulong count, out ulong executed)
        {
            executed = count;
            while(count != 0)
            {
                var chunk = (int)Math.Min(count, 4096UL);
                if(!timerDriven) ezh.AdvanceCycles(chunk);
                count -= (ulong)chunk;
            }
            cycles += executed;
            return ExecutionResult.Ok;
        }
        public void EnterTimerDrivenMode()
        {
            timerDriven = true;
            ezh.ExternalClock = false;
        }
        public override void Reset() { HardPauseForControl(); base.Reset(); cycles = 0; }
        public override string Architecture => "ezh-b";
        public override ulong ExecutedInstructions => cycles;
        public override RegisterValue PC { get => ezh.ReadDoubleWord(0x28); set => throw new NotSupportedException(); }
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

        private readonly RazerLpcEzh ezh;
        private ulong cycles;
        private bool timerDriven;
    }
}
