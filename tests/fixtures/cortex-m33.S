.syntax unified
.cpu cortex-m33
.thumb
.section .text
.word 0x04008000
.word reset+1
.space 0xd0-(.-.text)
.word timer_irq+1
.space 0x140-(.-.text)
.global reset
reset:
    ldr r0, =0x04006000
    msr msplim, r0
    mrs r1, msplim
    ldr r2, =0x20001000
    str r1, [r2]
    ldr r0, =0xe000ed88
    ldr r1, =0x00f00000
    str r1, [r0]
    dsb
    isb
    vmov.f32 s0, #1.0
    vadd.f32 s1, s0, s0
    vmov r1, s1
    str r1, [r2, #4]
    ldr r0, =0x40028000
    movs r1, #95
    str r1, [r0, #12]
    movs r1, #100
    str r1, [r0, #28]
    movs r1, #24
    str r1, [r0, #20]
    ldr r2, =0xe000e104
    movs r1, #16
    str r1, [r2]
    movs r1, #1
    str r1, [r0, #4]
loop:
    b loop

timer_irq:
    ldr r0, =0x40028000
    movs r1, #2
    str r1, [r0]
    ldr r0, =0x20001008
    ldr r1, [r0]
    adds r1, #1
    str r1, [r0]
    bx lr
