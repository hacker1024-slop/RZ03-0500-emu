// SPDX-License-Identifier: MIT
using System.Collections.Generic;

namespace Antmicro.Renode.Peripherals.Miscellaneous
{
    // LPC5528 USB PHY control plane. Reset values/masks are from NXP's
    // LPC5528_DFP 26.06.00 SVD; this does not simulate analog signaling.
    public class RazerLpcUsbPhy : RazerLpcAnalog
    {
        public override void Reset()
        {
            base.Reset(); values.Clear();
            values[0x00] = 0x001E1C00;
            values[0x10] = 0x0A000402;
            values[0x20] = 0;
            values[0x30] = 0xC0000000;
            values[0xA0] = 0x00D12000;
            values[0xC0] = 0x00700004;
            values[0x100] = 0x0A000402;
        }
        public bool ReferenceClockPresent { get; set; } = true;
        public bool PllLocked
        {
            get
            {
                var pll = values[0xA0];
                var biasPowered = (pll & (1 << 19)) == 0 || (pll & (1 << 20)) == 0;
                return ReferenceClockPresent && (pll & 0x203000) == 0x203000 && biasPowered;
            }
        }
        public bool Ready => PllLocked && values[0] == 0 &&
            (values[0x30] & 0xC0000000) == 0 && (values[0xA0] & 0x40) != 0;
        public override uint ReadDoubleWord(long offset)
        {
            if(offset == 0x40) return 0; // disconnected: no host or resume signal
            var register = offset & ~0xFL;
            if((offset & 3) != 0 || !WritableMasks.ContainsKey(register))
                return base.ReadDoubleWord(offset);
            var value = values[register];
            return register == 0xA0 && PllLocked ? value | 0x80000000 : value;
        }
        public override void WriteDoubleWord(long offset, uint value)
        {
            if(offset == 0x40) return; // STATUS is read-only
            var register = offset & ~0xFL;
            if((offset & 3) != 0 || !WritableMasks.TryGetValue(register, out var mask))
            { base.WriteDoubleWord(offset, value); return; }
            var bits = value & mask;
            switch(offset & 0xF)
            {
            case 0: values[register] = (values[register] & ~mask) | bits; break;
            case 4: values[register] |= bits; break;
            case 8: values[register] &= ~bits; break;
            case 12: values[register] ^= bits; break;
            }
            if(register == 0x30 && (values[register] & 0x80000000) != 0)
                values[register] |= 0x40000000;
        }
        private static readonly Dictionary<long, uint> WritableMasks = new Dictionary<long, uint>
        {
            {0x00, 0x001E1C00}, {0x10, 0x002F2F0F}, {0x20, 0x00400077},
            {0x30, 0xF6BFD73E},
            // SDK CLOCK_EnableUsbhs0PhyPllClock explicitly clears reserved bit 16.
            {0xA0, 0x41F93040}, {0xC0, 0x047C7FFF}, {0x100, 0x0000040E},
        };
    }
}
