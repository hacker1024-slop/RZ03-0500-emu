// SPDX-License-Identifier: MIT
using System;
using System.Collections.Generic;
using Antmicro.Renode.Peripherals.Miscellaneous;
using Antmicro.Renode.Peripherals.GPIOPort;

namespace Antmicro.Renode.Peripherals.Input
{
    // Ideal active-low contacts with per-key diodes and pulled-up columns.
    // Firmware tables map the EZH's 32-bit port to GPIO port zero. IOCON
    // selects which controller drives each row; indices are not package pins.
    public class RazerDiodeMatrix
    {
        public RazerDiodeMatrix(RazerLpcEzh ezh, RazerLpcGpio gpio, int[] rowPins, int[] columnPins)
        {
            this.gpio = gpio;
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
            matrixMask = columnMask;
            foreach(var pin in rows) matrixMask |= 1U << pin;
            ezh.InputPins = (output, direction) =>
                (output & direction & ~matrixMask) | ReadSharedPins(output, direction);
            gpio.InputPins = (output, direction) =>
                (output & ~matrixMask) | ReadSharedPins(ezh.GetRegister(8), ezh.GetRegister(9));
        }
        private uint ReadSharedPins(uint ezhOutput, uint ezhDirection)
        {
            var output = (ezhOutput & gpio.EzhFunctionMask) | (gpio.Port0Output & gpio.GpioFunctionMask);
            var direction = (ezhDirection & gpio.EzhFunctionMask) | (gpio.Port0Direction & gpio.GpioFunctionMask);
            return ReadPins(output, direction) & matrixMask;
        }
        public void SetKey(int row, int column, bool value)
        {
            if(row < 0 || row >= rows.Length || column < 0 || column >= columns.Length)
                throw new ArgumentOutOfRangeException("Matrix coordinate outside configured geometry");
            var bit = 1U << columns[column];
            if(value) pressed[row] |= bit;
            else pressed[row] &= ~bit;
            var rowBit = 1U << row;
            activeRows = pressed[row] == 0 ? activeRows & ~rowBit : activeRows | rowBit;
        }
        public uint ReadPins(uint output, uint direction)
        {
            // Non-matrix input pins default low. Output pins read back their
            // driven value; only input columns receive the pull-up/contact state.
            var input = columnMask;
            if(activeRows == 0) return (input & ~direction) | (output & direction);
            for(var row = 0; row < rows.Length; row++)
            {
                var rowBit = 1U << rows[row];
                if((direction & rowBit) == 0 || (output & rowBit) != 0) continue;
                input &= ~pressed[row];
            }
            return (input & ~direction) | (output & direction);
        }
        private readonly int[] rows, columns;
        private readonly RazerLpcGpio gpio;
        private readonly uint[] pressed;
        private volatile uint activeRows;
        private readonly uint columnMask, matrixMask;
    }
}
