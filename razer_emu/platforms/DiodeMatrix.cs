// SPDX-License-Identifier: MIT
using System;
using System.Collections.Generic;
using Antmicro.Renode.Peripherals.Miscellaneous;

namespace Antmicro.Renode.Peripherals.Input
{
    // Ideal active-low contacts with per-key diodes and pulled-up columns.
    // Pin indices refer to the EZH's 32-bit digital port, not package numbers.
    public class RazerDiodeMatrix
    {
        public RazerDiodeMatrix(RazerLpcEzh ezh, int[] rowPins, int[] columnPins)
        {
            var used = new HashSet<int>();
            foreach(var pins in new[] {rowPins, columnPins})
            {
                if(pins == null || pins.Length == 0 || pins.Length > 32)
                    throw new ArgumentException("Matrix pin lists must contain 1 to 32 pins");
                foreach(var pin in pins)
                    if(pin < 0 || pin >= 32 || !used.Add(pin))
                        throw new ArgumentException("Matrix pins must be distinct indices from 0 to 31");
            }
            rows = (int[])rowPins.Clone(); columns = (int[])columnPins.Clone();
            pressed = new uint[rows.Length];
            foreach(var pin in columns) columnMask |= 1U << pin;
            ezh.InputPins = ReadPins;
        }
        public void SetKey(int row, int column, bool value)
        {
            if(row < 0 || row >= rows.Length || column < 0 || column >= columns.Length)
                throw new ArgumentOutOfRangeException("Matrix coordinate outside configured geometry");
            var bit = 1U << columns[column];
            if(value) pressed[row] |= bit;
            else pressed[row] &= ~bit;
        }
        public uint ReadPins(uint output, uint direction)
        {
            // Non-matrix input pins default low. Output pins read back their
            // driven value; only input columns receive the pull-up/contact state.
            var input = columnMask;
            for(var row = 0; row < rows.Length; row++)
            {
                var rowBit = 1U << rows[row];
                if((direction & rowBit) == 0 || (output & rowBit) != 0) continue;
                input &= ~pressed[row];
            }
            return (input & ~direction) | (output & direction);
        }
        private readonly int[] rows, columns;
        private readonly uint[] pressed;
        private readonly uint columnMask;
    }
}
