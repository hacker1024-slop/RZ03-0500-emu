using System;
using Antmicro.Renode.Core;
using Antmicro.Renode.Peripherals;
using Antmicro.Renode.Peripherals.CPU;
using Antmicro.Renode.Peripherals.Bus;
using Antmicro.Renode.Peripherals.Miscellaneous;
using Antmicro.Renode.Peripherals.USB;
using Antmicro.Renode.Time;
namespace Antmicro.Renode.Utilities
{
    public static class RazerDispatchProbe
    {
        public static ulong BeforeWrites, WatchpointCalls;
        public static RazerDispatchRecorder Recorder;
        public static Func<TimeInterval, TimeInterval> CreateMulticastService(RazerLpcUsbDevice device, IMachine machine, ulong baseAddress)
        {
            var pump = new RazerUsbInterruptPump(device);
            Func<TimeInterval, TimeInterval> instrument = now =>
            {
                BeforeWrites++;
                ((SystemBus)machine.SystemBus).Tag(new Antmicro.Renode.Core.Range(baseAddress+8,4), "test-service",0,false,true,true);
                return TimeInterval.FromMicroseconds(125);
            };
            // A multicast delegate reports the final callback's trusted target.
            return instrument + pump.ScheduledServiceHook;
        }
        public static void Install(string mode, IMachine machine, RazerCortexM cpu, IBusPeripheral watchdog, ulong baseAddress, ulong codeHookAddress)
        {
            var bus = (SystemBus)machine.SystemBus;
            BeforeWrites = WatchpointCalls = 0; Recorder = null;
            switch(mode)
            {
            case "hook":
                bus.SetHookBeforePeripheralWrite<uint>(watchdog, (value, offset) => { if(offset==8) BeforeWrites++; return offset==8 && value==0xAA ? 0U : value; });
                break;
            case "watchpoint":
                bus.AddWatchpointHook(baseAddress+8, SysbusAccessWidth.DoubleWord, Access.Write,
                    (c,a,w,v) => WatchpointCalls++);
                break;
            case "tag":
                bus.Tag(new Antmicro.Renode.Core.Range(baseAddress+8,4),"review-feed",0,false,true,true);
                break;
            case "lock":
                bus.SetAddressRangeLocked(new Antmicro.Renode.Core.Range(baseAddress+8,4),true,cpu);
                break;
            case "disabled": bus.SetPeripheralEnabled(watchdog,false); break;
            case "context": cpu.UpdateContextOnLoadAndStore = true; break;
            case "remap":
                bus.Unregister(watchdog);
                Recorder = new RazerDispatchRecorder();
                bus.Register(Recorder,new BusRangeRegistration(new Antmicro.Renode.Core.Range(baseAddress,0x1000)));
                break;
            case "other-watchpoint":
                bus.AddWatchpointHook(0x4008E000, SysbusAccessWidth.DoubleWord, Access.Write,
                    (c,a,w,v) => { WatchpointCalls++; bus.Tag(new Antmicro.Renode.Core.Range(baseAddress+8,4),"review-other-watch",0,false,true,true); });
                break;
            case "other-wrapper":
                bus.SetHookBeforePeripheralWrite<uint>((IBusPeripheral)bus.WhatPeripheralIsAt(0x4008E000),
                    (value,offset) => { bus.Tag(new Antmicro.Renode.Core.Range(baseAddress+8,4),"review-other-wrapper",0,false,true,true); return value; });
                break;
            case "midrun-tag":
                cpu.AddHook(codeHookAddress,(c,a) => bus.Tag(new Antmicro.Renode.Core.Range(baseAddress+8,4),"review-midrun",0,false,true,true));
                break;
            case "wfi-hook": cpu.AddHookAtWfiStateChange(state => BeforeWrites++); break;
            case "none": break;
            default: throw new ArgumentException(mode);
            }
        }
    }
    public class RazerDispatchRecorder : IDoubleWordPeripheral, IKnownSize
    {
        public ulong FeedWrites;
        public void Reset() { FeedWrites = 0; }
        public uint ReadDoubleWord(long offset) => 0;
        public void WriteDoubleWord(long offset,uint value) { if(offset==8) FeedWrites++; }
        public long Size => 0x1000;
    }
}
