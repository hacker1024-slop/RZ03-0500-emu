// SPDX-License-Identifier: MIT
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.Miscellaneous;

namespace Antmicro.Renode.Peripherals.Miscellaneous
{
    // NXP AN14650 control/mailbox registers. Starting the coprocessor remains
    // unsupported until the instruction engine is implemented; never fake completion.
    public class RazerLpcEzh : RazerLpcAnalog
    {
        public override void Reset()
        {
            base.Reset(); values.Clear();
            for(long offset = 0x20; offset <= 0x44; offset += 4) values[offset] = 0;
        }
        public override void WriteDoubleWord(long offset, uint value)
        {
            if(offset == 0x24)
            {
                if((value >> 16) != 0xC0DE) return;
                values[0x24] = value & 0x1F;
                if((value & 1) != 0)
                {
                    this.Log(LogLevel.Warning, "EZH execution requested at 0x{0:X8}, mailbox 0x{1:X8}",
                        values[0x20], values[0x40]);
                    base.WriteDoubleWord(0x1024, value);
                }
                return;
            }
            base.WriteDoubleWord(offset, value);
        }
    }
}
