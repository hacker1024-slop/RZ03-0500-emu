// SPDX-License-Identifier: MIT
using Antmicro.Renode.Core;
using Antmicro.Renode.Peripherals.Miscellaneous;

namespace Antmicro.Renode.Peripherals.USB
{
    // IP3511 HS register/interrupt control plane. Firmware owns the endpoint
    // command/status list in USB RAM. Host packet transactions are forthcoming.
    public class RazerLpcUsbDevice : RazerLpcAnalog
    {
        public RazerLpcUsbDevice(IMachine machine, RazerLpcUsbPhy phy)
        {
            this.machine = machine; this.phy = phy;
        }
        public override void Reset()
        {
            base.Reset(); values.Clear();
            foreach(var offset in new long[] {0,4,8,12,16,20,24,28,32,36,40,52}) values[offset] = 0;
            values[0] = 0x800 | (vbusPresent ? 0x10000000U : 0);
            values[4] = 0x02000000;
            values[12] = 0x41000000;
            values[16] = vbusPresent ? 0x100000U : 0;
            IRQ?.Unset();
        }
        public GPIO IRQ { get; } = new GPIO();
        public bool Enabled => phy.Ready && (values[0] & 0x80) != 0;
        public byte Address => (byte)(values[0] & 0x7F);
        public bool VbusPresent
        {
            get => vbusPresent;
            set
            {
                var changed = vbusPresent != value;
                // IP3511 HS signals VBUS edges through DEV_INT/OTG_C and
                // LPM.ADPPROBE (NXP driver ISR attachment detection).
                vbusPresent = value;
                values[0] = value ? values[0] | 0x10000000 : values[0] & ~0x10000000U;
                values[16] = value ? values[16] | 0x100000 : values[16] & ~0x100000U;
                if(changed && Enabled) RaiseVbusChange();
            }
        }
        public override uint ReadDoubleWord(long offset)
        {
            if(offset == 0x28) return values[0x20];
            return base.ReadDoubleWord(offset);
        }
        public override void WriteDoubleWord(long offset, uint value)
        {
            switch(offset)
            {
            case 0:
                // Normal controls, W1C change/SETUP flags and physical RO fields
                // share this register. Preserve status on ordinary RMW writes.
                var wasEnabled = (values[0] & 0x80) != 0;
                values[0] = (values[0] & ~ControlMask) | (value & ControlMask);
                values[0] &= ~(value & ChangeMask);
                if(!wasEnabled && Enabled && vbusPresent) RaiseVbusChange();
                break;
            case 4: case 0x34: return; // INFO / endpoint toggle are read-only
            case 8: values[offset] = value & 0xFFFFFF00; break;
            case 12: values[offset] = value; break;
            case 16: values[offset] = (value & ~0x100000U) | (values[offset] & 0x100000); break;
            case 20:
                if((value & 0xFFF) != 0)
                {
                    var list = values[8];
                    if(list < 0x40100000 || list + 96UL > 0x40104000)
                    { RejectWrite(offset, value, "USB endpoint list outside USB RAM"); return; }
                    for(var index = 0; index < 12; index++)
                    {
                        if((value & (1U << index)) == 0) continue;
                        for(var bank = 0; bank < 2; bank++)
                        {
                            var address = list + (uint)(index * 8 + bank * 4);
                            var command = machine.SystemBus.ReadDoubleWord(address);
                            machine.SystemBus.WriteDoubleWord(address, command & ~0x80000000U);
                        }
                    }
                }
                // Abort is synchronous in the functional model; SKIP self-clears.
                values[offset] = 0;
                break;
            case 24: case 28: values[offset] = value & 0xFFC; break;
            case 32: values[offset] &= ~(value & InterruptMask); break;
            case 36: values[offset] = value & InterruptMask; break;
            case 40: values[32] |= value & InterruptMask; break;
            default: base.WriteDoubleWord(offset, value); return;
            }
            IRQ.Set((values[32] & values[36]) != 0);
        }
        private void RaiseVbusChange()
        {
            values[0] |= 0x08000000;
            values[32] |= 0x80000000;
            IRQ.Set((values[32] & values[36]) != 0);
        }
        private readonly IMachine machine;
        private readonly RazerLpcUsbPhy phy;
        private bool vbusPresent;
        private const uint InterruptMask = 0xC0000FFF;
        private const uint ControlMask = 0xE00BFAFF;
        private const uint ChangeMask = 0x0F000100;
    }
}
