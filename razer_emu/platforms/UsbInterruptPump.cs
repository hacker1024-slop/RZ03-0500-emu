// SPDX-License-Identifier: MIT
using System;
using System.Collections.Generic;
using Antmicro.Renode.Time;
namespace Antmicro.Renode.Peripherals.USB
{
    public sealed class RazerUsbCompletion
    {
        public uint Sequence { get; internal set; }
        public int Status { get; internal set; }
        public byte[] Data { get; internal set; }
        public ulong AtUs { get; internal set; }
    }
    // Own-thread queue; ServiceHook is a CLR delegate, never a Python callback.
    // Polling never acknowledges an endpoint without a queued guest URB.
    public sealed class RazerUsbInterruptPump
    {
        public RazerUsbInterruptPump(RazerLpcUsbDevice device)
        {
            this.device = device;
            ServiceHook = Service;
            ScheduledServiceHook = ServiceAndNext;
        }
        public Action<TimeInterval> ServiceHook { get; }
        public Func<TimeInterval, TimeInterval> ScheduledServiceHook { get; }
        public int PendingCount => requests.Count;
        public void BeginBatch(ulong startUs) { batchStartUs = startUs; }
        public void Add(uint sequence, int endpoint, int length, uint flags, int packetSize, ulong intervalUs)
        {
            if(endpoint < 1 || endpoint > 15 || length < 1 || length > 65536 || packetSize < 1 || packetSize > 1024 || intervalUs < 125 || intervalUs % 125 != 0)
                throw new ArgumentOutOfRangeException();
            if(requests.Count >= 128 || requests.ContainsKey(sequence)) throw new ArgumentException("Invalid pending USB request");
            if(!endpoints.TryGetValue(endpoint, out var pipe))
            {
                pipe = new Pipe { Endpoint = endpoint, PacketSize = packetSize, IntervalUs = intervalUs };
                endpoints.Add(endpoint, pipe);
            }
            if(pipe.PacketSize != packetSize || pipe.IntervalUs != intervalUs) throw new ArgumentException("Endpoint descriptor changed");
            var request = new Request { Sequence = sequence, Length = length, Flags = flags };
            requests.Add(sequence, request); pipe.Requests.Enqueue(request);
        }
        public bool Cancel(uint sequence)
        {
            if(!requests.TryGetValue(sequence, out var request)) return false;
            request.Cancelled = true; requests.Remove(sequence); return true;
        }
        public RazerUsbCompletion[] Drain()
        {
            if(completed.Count == 0) return Array.Empty<RazerUsbCompletion>();
            var result = completed.ToArray(); completed.Clear(); return result;
        }
        private TimeInterval ServiceAndNext(TimeInterval relative)
        {
            Service(relative);
            var now = batchStartUs + relative.Ticks / TimeInterval.TicksPerMicrosecond;
            ulong delay = 125;
            var found = false;
            foreach(var pipe in endpoints.Values)
            {
                if(pipe.Requests.Count == 0) continue;
                var until = pipe.NextDueUs - now;
                if(!found || until < delay) { delay = until; found = true; }
            }
            return TimeInterval.FromMicroseconds(delay);
        }
        private void Service(TimeInterval relative)
        {
            var now = batchStartUs + relative.Ticks / TimeInterval.TicksPerMicrosecond;
            foreach(var pipe in endpoints.Values)
            {
                while(pipe.Requests.Count != 0 && pipe.Requests.Peek().Cancelled) pipe.Requests.Dequeue();
                if(pipe.Requests.Count == 0 || now < pipe.NextDueUs) continue;
                pipe.NextDueUs = now + pipe.IntervalUs;
                var request = pipe.Requests.Peek();
                var packet = device.HostIn(pipe.Endpoint, pipe.PacketSize);
                if(packet.Handshake == RazerUsbHandshake.Nak) continue;
                if(packet.Handshake == RazerUsbHandshake.Stall)
                { request.Data.Clear(); Complete(pipe, request, -32, now); continue; }
                var remaining = request.Length - request.Data.Count;
                var count = Math.Min(remaining, packet.Data.Length);
                for(var i = 0; i < count; i++) request.Data.Add(packet.Data[i]);
                var shortPacket = packet.Data.Length < pipe.PacketSize;
                if(packet.Data.Length > remaining || shortPacket || request.Data.Count == request.Length)
                {
                    var status = packet.Data.Length > remaining ? -75 : 0;
                    if(shortPacket && (request.Flags & 1) != 0 && request.Data.Count < request.Length) status = -121;
                    Complete(pipe, request, status, now);
                }
            }
        }
        private void Complete(Pipe pipe, Request request, int status, ulong now)
        {
            pipe.Requests.Dequeue(); requests.Remove(request.Sequence);
            completed.Enqueue(new RazerUsbCompletion { Sequence = request.Sequence, Status = status, Data = request.Data.ToArray(), AtUs = now });
        }
        private sealed class Pipe
        {
            public int Endpoint, PacketSize;
            public ulong IntervalUs, NextDueUs;
            public readonly Queue<Request> Requests = new Queue<Request>();
        }
        private sealed class Request
        {
            public uint Sequence, Flags;
            public int Length;
            public bool Cancelled;
            public readonly List<byte> Data = new List<byte>();
        }
        private ulong batchStartUs;
        private readonly RazerLpcUsbDevice device;
        private readonly Dictionary<int, Pipe> endpoints = new Dictionary<int, Pipe>();
        private readonly Dictionary<uint, Request> requests = new Dictionary<uint, Request>();
        private readonly Queue<RazerUsbCompletion> completed = new Queue<RazerUsbCompletion>();
    }
}
