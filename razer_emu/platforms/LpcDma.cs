// SPDX-License-Identifier: MIT
using Antmicro.Renode.Peripherals.Miscellaneous;

namespace Antmicro.Renode.Peripherals.DMA
{
    // Configuration stage. Triggering a transfer is explicitly rejected until
    // descriptor chaining and peripheral request handshakes are implemented.
    public class RazerLpcDma : RazerLpcAnalog
    {
        public override void Reset()
        {
            base.Reset(); values.Clear();
            foreach(var offset in new long[] {0,4,8,0x20,0x30,0x38,0x40,0x48,0x58,0x60})
                values[offset] = 0;
            for(long i = 0; i < 23; i++)
            {
                values[0x400 + i * 16] = 0;
                values[0x404 + i * 16] = 0;
                values[0x408 + i * 16] = 0;
            }
        }
        public override void WriteDoubleWord(long offset, uint value)
        {
            switch(offset)
            {
            case 0x20: case 0x48: values[offset] |= value; return;
            case 0x28: values[0x20] &= ~value; return;
            case 0x50: values[0x48] &= ~value; return;
            case 0x40: case 0x58: case 0x60: values[offset] &= ~value; return;
            }
            if(offset >= 0x408 && offset < 0x570 && offset % 16 == 8 && (value & 5) == 5)
            { base.WriteDoubleWord(0x1000 + offset, value); return; }
            base.WriteDoubleWord(offset, value);
        }
    }
}
