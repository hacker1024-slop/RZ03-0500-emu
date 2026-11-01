// SPDX-License-Identifier: MIT
using System;

namespace Antmicro.Renode.Peripherals.Miscellaneous
{
    // Eight-slice detector described by NXP AN14650, figures 7/8 and table 10.
    // Inputs are trigger signals, not the separate 32-bit EZH GPIO data port.
    public sealed class RazerEzhBoolean
    {
        public void Reset()
        {
            inputs = previousInputs = sticky = selection = mode = Results = 0;
            sampleValid = false;
        }
        public void SetInput(int number, bool value)
        {
            if(number < 0 || number >= 8) throw new ArgumentOutOfRangeException(nameof(number));
            var bit = 1U << number;
            var next = value ? inputs | bit : inputs & ~bit;
            if(next != inputs) sampleValid = false;
            inputs = next;
        }
        public uint Selection
        {
            get
            {
                uint status = 0;
                for(var i = 0; i < 8; i++)
                    status |= ((inputs >> (int)((selection >> (8 + 3 * i)) & 7)) & 1) << i;
                return selection | status;
            }
            set { selection = value & 0xFFFFFF00; Results = 0; sampleValid = false; }
        }
        public uint Mode
        {
            get => mode;
            set { mode = value; sticky = 0; Results = 0; sampleValid = false; }
        }
        public uint Results { get; private set; }
        public bool Dirty => !sampleValid;
        public void Invalidate() { sampleValid = false; }
        public uint Sample(uint pendingTrap)
        {
            if(sampleValid && sampledInputs == inputs && sampledTrap == pendingTrap) return pendingTrap;
            // Polarity chooses which physical edge latches into pending status;
            // it does not invert a software-written slice override (4.2.9).
            var transitions = inputs ^ previousInputs;
            pendingTrap |= transitions & (inputs ^ (pendingTrap >> 8)) & 0xFF;
            uint outputs = 0;
            var chain = true;
            for(var slice = 0; slice < 8; slice++)
            {
                var bit = 1U << slice;
                var source = (int)((selection >> (8 + 3 * slice)) & 7);
                var current = (inputs & (1U << source)) != 0;
                var previous = (previousInputs & (1U << source)) != 0;
                var kind = (mode >> (8 + 3 * slice)) & 7;
                bool detected;
                switch(kind)
                {
                case 0: detected = true; break;
                case 1: detected = current && !previous; break;
                case 2: detected = !current && previous; break;
                case 3: case 7: detected = current != previous; break;
                case 4: detected = current; break;
                case 5: detected = !current; break;
                default: detected = false; break;
                }
                if(kind >= 1 && kind <= 3)
                {
                    if(detected) sticky |= bit;
                    detected = (sticky & bit) != 0;
                }
                if((pendingTrap & (bit << 16)) != 0)
                    detected = (pendingTrap & bit) != 0;
                chain &= detected;
                // The final slice always terminates the remaining AND term.
                // LPC5528 captures with CFM=0 select vector 7; a zero OR mask
                // does not disable all detection when the slice muxes are 1.
                if((mode & bit) != 0 || slice == 7)
                {
                    if(chain) outputs |= bit;
                    chain = true;
                }
            }
            var inputChanged = previousInputs != inputs;
            previousInputs = inputs;
            Results = outputs;
            sampledInputs = inputs; sampledTrap = pendingTrap; sampleValid = !inputChanged;
            return pendingTrap;
        }
        private uint sampledInputs, sampledTrap;
        private bool sampleValid;
        private uint inputs, previousInputs, sticky, selection, mode;
    }
}
