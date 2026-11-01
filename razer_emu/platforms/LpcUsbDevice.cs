// SPDX-License-Identifier: MIT
using System;
using Antmicro.Renode.Core;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.Miscellaneous;
using Antmicro.Renode.Peripherals.Timers;
using Antmicro.Renode.Time;

namespace Antmicro.Renode.Peripherals.USB
{
    public enum RazerUsbHandshake { Ack, Nak, Stall }
    public sealed class RazerUsbPacket
    {
        public RazerUsbPacket(RazerUsbHandshake handshake, byte[] data)
        { Handshake = handshake; Data = data; }
        public RazerUsbHandshake Handshake { get; }
        public byte[] Data { get; }
    }
    // Functional IP3511 HS packet DMA. Firmware owns every endpoint command,
    // descriptor payload and class response; no USB replies are synthesized.
    public class RazerLpcUsbDevice : RazerLpcAnalog
    {
        public RazerLpcUsbDevice(IMachine machine, RazerLpcUsbPhy phy)
        {
            this.machine = machine; this.phy = phy;
            frameClock = new LimitTimer(machine.ClockSource, 1000000, this, "usb-sof",
                limit: 125, workMode: WorkMode.Periodic, eventEnabled: true);
            frameClock.LimitReached += StartOfFrame;
            Reset();
        }
        public override void Reset()
        {
            base.Reset(); values.Clear();
            foreach(var offset in new long[] {0,4,8,12,16,20,24,28,32,36,40,52}) values[offset] = 0;
            values[0] = 0x800 | (vbusPresent ? 0x10000000U : 0);
            values[4] = 0x02000000;
            values[12] = 0x41000000;
            values[16] = vbusPresent ? 0x100000U : 0;
            Array.Clear(lastCommands, 0, lastCommands.Length);
            Array.Clear(positions, 0, positions.Length);
            TransferError = null;
            frameClock?.Reset(); hostFramesActive = false;
            microframe = 0; microframesPerFrame = 8;
            IRQ?.Unset();
        }
        public GPIO IRQ { get; } = new GPIO();
        public bool Enabled => phy.Ready && (values[0] & 0x80) != 0;
        public bool Connected => Enabled && vbusPresent && (values[0] & 0x10000) != 0;
        public byte Address => (byte)(values[0] & 0x7F);
        public string TransferError { get; private set; }
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
                if(!value) hostFramesActive = false;
                UpdateFrameClock();
            }
        }
        public override uint ReadDoubleWord(long offset)
        {
            if(offset == 0x28) return values[0x20];
            return base.ReadDoubleWord(offset);
        }
        public bool HostReset(bool highSpeed = true)
        {
            if(!CanTransact) return false;
            // Bus reset resets protocol state, not CPU state or firmware RAM.
            values[0] &= ~(0x7FU | 0x100U | 0xA0000U | 0xC00000U);
            values[0] |= 0x04000000U | (highSpeed ? 0x800000U : 0x400000U);
            values[24] = values[52] = 0;
            values[32] = 0x80000000;
            Array.Clear(lastCommands, 0, lastCommands.Length);
            Array.Clear(positions, 0, positions.Length);
            values[4] &= ~0x7FFU;
            microframe = 0; microframesPerFrame = highSpeed ? 8 : 1;
            hostFramesActive = true;
            frameClock.Limit = highSpeed ? 125UL : 1000UL;
            frameClock.ResetValue();
            UpdateFrameClock();
            UpdateInterrupt();
            return true;
        }
        public RazerUsbHandshake HostSetup(byte[] packet)
        {
            if(packet == null || packet.Length != 8) throw new ArgumentException("SETUP must contain eight bytes");
            if(!CanTransact || !ValidList()) return RazerUsbHandshake.Nak;
            var list = values[8];
            var setupCommand = machine.SystemBus.ReadDoubleWord(list + 4);
            var address = BufferAddress(setupCommand);
            if(!ValidBuffer(address, 8)) return RazerUsbHandshake.Nak;
            // A new SETUP aborts the previous control transfer and clears STALL.
            foreach(var offset in new uint[] {0, 8, 12})
            {
                var command = machine.SystemBus.ReadDoubleWord(list + offset);
                machine.SystemBus.WriteDoubleWord(list + offset, command & ~0xA0000000U);
            }
            for(var index = 0; index < 2; index++)
                for(var bank = 0; bank < 2; bank++) positions[index, bank] = 0;
            machine.SystemBus.WriteBytes(packet, address);
            values[24] &= ~3U;
            values[52] |= 3;
            values[0] |= 0x100;
            values[32] = (values[32] & ~3U) | 1;
            UpdateInterrupt();
            return RazerUsbHandshake.Ack;
        }
        public RazerUsbPacket HostIn(int endpoint, int maximumPacketSize = 64)
        {
            ValidatePacket(endpoint, maximumPacketSize);
            var index = endpoint * 2 + 1;
            var handshake = Prepare(index, out var bank, out var command, out var address);
            if(handshake != RazerUsbHandshake.Ack) return new RazerUsbPacket(handshake, new byte[0]);
            var count = Math.Min((int)((command >> 11) & 0x7FFF), maximumPacketSize);
            if(!ValidBuffer(address, count)) return new RazerUsbPacket(RazerUsbHandshake.Nak, new byte[0]);
            var data = machine.SystemBus.ReadBytes(address, count);
            FinishPacket(index, bank, command, count, false);
            return new RazerUsbPacket(RazerUsbHandshake.Ack, data);
        }
        public RazerUsbHandshake HostOut(int endpoint, byte[] packet, int maximumPacketSize = 64)
        {
            ValidatePacket(endpoint, maximumPacketSize);
            if(packet == null || packet.Length > maximumPacketSize) throw new ArgumentException("Invalid OUT packet length");
            var index = endpoint * 2;
            if(endpoint == 0 && (values[0] & 0x100) != 0) return RazerUsbHandshake.Nak;
            var handshake = Prepare(index, out var bank, out var command, out var address);
            if(handshake != RazerUsbHandshake.Ack) return handshake;
            var remaining = (int)((command >> 11) & 0x7FFF);
            if(packet.Length > remaining)
            {
                FailTransfer("USB OUT buffer overrun", command);
                return RazerUsbHandshake.Nak;
            }
            // NXP's driver documents a four-byte zero write for an OUT ZLP.
            if(!ValidBuffer(address, packet.Length == 0 ? 4 : packet.Length)) return RazerUsbHandshake.Nak;
            machine.SystemBus.WriteBytes(packet.Length == 0 ? new byte[4] : packet, address);
            FinishPacket(index, bank, command, packet.Length, packet.Length < maximumPacketSize);
            return RazerUsbHandshake.Ack;
        }
        private RazerUsbHandshake Prepare(int index, out int bank, out uint command, out uint address)
        {
            bank = 0; command = address = 0;
            if(!CanTransact || !ValidList()) return RazerUsbHandshake.Nak;
            if(index >= 2 && (values[28] & (1U << index)) != 0)
                bank = (values[24] & (1U << index)) != 0 ? 1 : 0;
            command = machine.SystemBus.ReadDoubleWord(values[8] + (uint)(index * 8 + bank * 4));
            if((command & 0x40000000) != 0) return RazerUsbHandshake.Nak;
            if((command & 0x20000000) != 0) return RazerUsbHandshake.Stall;
            if((command & 0x80000000) == 0) return RazerUsbHandshake.Nak;
            // IP3511 HS uses TYPE+RFTV together for its interrupt workaround.
            if((command & 0x0C000000) == 0x04000000)
            { FailTransfer("USB isochronous transfer not implemented", command); return RazerUsbHandshake.Nak; }
            if(lastCommands[index, bank] != command) positions[index, bank] = 0;
            address = BufferAddress(command) + (uint)positions[index, bank];
            return RazerUsbHandshake.Ack;
        }
        private void FinishPacket(int index, int bank, uint command, int count, bool shortPacket)
        {
            var remaining = (int)((command >> 11) & 0x7FFF) - count;
            var complete = remaining == 0 || shortPacket;
            var updated = (command & ~0x03FFF800U) | ((uint)remaining << 11);
            if(complete) updated &= ~0x80000000U;
            machine.SystemBus.WriteDoubleWord(values[8] + (uint)(index * 8 + bank * 4), updated);
            lastCommands[index, bank] = updated;
            positions[index, bank] = complete ? 0 : positions[index, bank] + count;
            values[52] ^= 1U << index;
            if(complete)
            {
                values[32] |= 1U << index;
                if(index >= 2 && (values[28] & (1U << index)) != 0) values[24] ^= 1U << index;
            }
            UpdateInterrupt();
            UpdateFrameClock();
        }
        private void StartOfFrame()
        {
            if(!CanTransact || (values[0] & 0xA0000) != 0)
            { UpdateFrameClock(); return; }
            if(++microframe == microframesPerFrame)
            {
                microframe = 0;
                values[4] = (values[4] & ~0x7FFU) | ((values[4] + 1) & 0x7FF);
            }
            values[32] |= 0x40000000;
            UpdateInterrupt();
        }
        private void UpdateFrameClock()
        {
            if(frameClock != null)
                frameClock.Enabled = hostFramesActive && CanTransact && (values[0] & 0xA0000) == 0;
        }
        private bool ValidList()
        {
            if(values[8] >= 0x40100000 && values[8] + 96UL <= 0x40104000) return true;
            FailTransfer("USB endpoint list outside USB RAM", values[8]);
            return false;
        }
        private bool ValidBuffer(uint address, int length)
        {
            if(address >= 0x40100000 && address + (ulong)length <= 0x40104000) return true;
            FailTransfer("USB DMA outside USB RAM", address);
            return false;
        }
        private static uint BufferAddress(uint command) => 0x40100000U + ((command & 0x7FF) << 6);
        private void FailTransfer(string operation, uint value)
        {
            TransferError = string.Format("{0}: 0x{1:X8}", operation, value);
            this.Log(LogLevel.Warning, TransferError);
        }
        private static void ValidatePacket(int endpoint, int maximumPacketSize)
        {
            if(endpoint < 0 || endpoint >= 6) throw new ArgumentOutOfRangeException(nameof(endpoint));
            if(maximumPacketSize < 1 || maximumPacketSize > 1024) throw new ArgumentOutOfRangeException(nameof(maximumPacketSize));
        }
        private bool CanTransact => Connected && (values[0] & 0xE0000000) == 0;
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
                            lastCommands[index, bank] = command & ~0x80000000U;
                            positions[index, bank] = 0;
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
            UpdateInterrupt();
            UpdateFrameClock();
        }
        private void RaiseVbusChange()
        {
            values[0] |= 0x08000000;
            values[32] |= 0x80000000;
            UpdateInterrupt();
        }
        private void UpdateInterrupt() => IRQ.Set((values[32] & values[36]) != 0);
        private readonly IMachine machine;
        private readonly RazerLpcUsbPhy phy;
        private bool vbusPresent;
        private readonly LimitTimer frameClock;
        private bool hostFramesActive;
        private int microframe, microframesPerFrame;
        private readonly uint[,] lastCommands = new uint[12, 2];
        private readonly int[,] positions = new int[12, 2];
        private const uint InterruptMask = 0xC0000FFF;
        private const uint ControlMask = 0xE00BFAFF;
        private const uint ChangeMask = 0x0F000100;
    }
}
