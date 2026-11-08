# SPDX-FileCopyrightText: © 2024 Tiny Tapeout
# SPDX-License-Identifier: Apache-2.0

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge, RisingEdge


# PIO encoding: [15:13] opcode, [12:8] delay, [7:0] args
OP_JMP = 0b000
OP_WAIT = 0b001
OP_IN = 0b010
OP_OUT = 0b011
OP_PUSH = 0b100  # also PULL when bit7=1
OP_MOV = 0b101
OP_SET = 0b111

# SET / OUT destinations
DEST_PINS = 0b000
DEST_X = 0b001
DEST_Y = 0b010
DEST_NULL = 0b011
DEST_PINDIRS = 0b100
DEST_PC = 0b101
DEST_ISR = 0b110
DEST_EXEC = 0b111

# MOV destinations (EXEC is 100; OSR is 111 — differs from OUT)
MOV_DEST_PINS = 0b000
MOV_DEST_X = 0b001
MOV_DEST_Y = 0b010
MOV_DEST_EXEC = 0b100
MOV_DEST_PC = 0b101
MOV_DEST_ISR = 0b110
MOV_DEST_OSR = 0b111

# IN / MOV sources
SRC_PINS = 0b000
SRC_X = 0b001
SRC_Y = 0b010
SRC_NULL = 0b011
SRC_STATUS = 0b101
SRC_ISR = 0b110
SRC_OSR = 0b111

MOV_OP_NONE = 0b00
MOV_OP_INVERT = 0b01
MOV_OP_BITREV = 0b10

JMP_ALWAYS = 0b000
JMP_NOT_X = 0b001
JMP_X_DEC = 0b010
JMP_NOT_Y = 0b011
JMP_Y_DEC = 0b100
JMP_X_NE_Y = 0b101

WAIT_GPIO = 0b00

# uio is the SPI host interface: miso, tx_full, rx_not_empty, running are outputs
UIO_OE_HOST = 0b0111_0100

IMEM_DEPTH = 16


def encode_jmp(address, condition=JMP_ALWAYS, delay=0):
    return (
        (OP_JMP << 13)
        | ((delay & 0x1F) << 8)
        | ((condition & 0x7) << 5)
        | (address & 0x1F)
    )


def encode_wait(polarity, index=0, src=WAIT_GPIO, delay=0):
    return (
        (OP_WAIT << 13)
        | ((delay & 0x1F) << 8)
        | ((polarity & 1) << 7)
        | ((src & 0x3) << 5)
        | (index & 0x1F)
    )


def encode_in(src, count, delay=0):
    return (
        (OP_IN << 13)
        | ((delay & 0x1F) << 8)
        | ((src & 0x7) << 5)
        | (count & 0x1F)
    )


def encode_out(dest, count, delay=0):
    return (
        (OP_OUT << 13)
        | ((delay & 0x1F) << 8)
        | ((dest & 0x7) << 5)
        | (count & 0x1F)
    )


def encode_set(dest, data, delay=0):
    return (
        (OP_SET << 13)
        | ((delay & 0x1F) << 8)
        | ((dest & 0x7) << 5)
        | (data & 0x1F)
    )


def encode_push(iff=0, blk=1, delay=0):
    """PUSH: bit7=0, bit6=IfF, bit5=Blk."""
    return (
        (OP_PUSH << 13)
        | ((delay & 0x1F) << 8)
        | ((iff & 1) << 6)
        | ((blk & 1) << 5)
    )


def encode_pull(ife=0, blk=1, delay=0):
    """PULL: bit7=1, bit6=IfE, bit5=Blk."""
    return (
        (OP_PUSH << 13)
        | ((delay & 0x1F) << 8)
        | (1 << 7)
        | ((ife & 1) << 6)
        | ((blk & 1) << 5)
    )


def encode_mov(dest, src, op=MOV_OP_NONE, delay=0):
    """MOV: [7:5] dest, [4:3] op, [2:0] src."""
    return (
        (OP_MOV << 13)
        | ((delay & 0x1F) << 8)
        | ((dest & 0x7) << 5)
        | ((op & 0x3) << 3)
        | (src & 0x7)
    )


def halt(address):
    return encode_jmp(address, JMP_ALWAYS)


def sm(dut):
    return dut.user_project


async def start_clock(dut):
    clock = Clock(dut.clk, 10, unit="ns")
    cocotb.start_soon(clock.start())
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = 0


async def load_program(dut, program):
    """Reset, write all 16 IMEM words through the host port while halted, then run."""
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 2)
    dut.rst_n.value = 1

    words = [halt(i) for i in range(IMEM_DEPTH)]
    for i, word in enumerate(program):
        words[i] = word

    for addr, word in enumerate(words):
        await host_imem_write(dut, addr, word)

    sm(dut).running.value = 1
    # First edge after running goes high executes instruction 0
    await ClockCycles(dut.clk, 2)


async def run_with_osr(dut, osr, program, cycles=10):
    """Run `program` with OSR preloaded.

    Park on WAIT 1 GPIO 7 at address 0 while OSR is written directly, then
    release. The program passed in starts at address 1.
    """
    dut.ui_in.value = 0
    await load_program(dut, [encode_wait(1, index=7)] + program)
    sm(dut).osr.value = osr
    sm(dut).osr_count.value = 8 if osr else 0
    await ClockCycles(dut.clk, 1)
    dut.ui_in.value = 0x80
    await ClockCycles(dut.clk, cycles)


async def host_imem_write(dut, addr, word):
    """Pulse a one-cycle host write into instruction memory."""
    await FallingEdge(dut.clk)
    sm(dut).host_imem_addr.value = addr
    sm(dut).host_imem_wdata.value = word
    sm(dut).host_imem_we.value = 1
    await RisingEdge(dut.clk)


async def host_tx_write(dut, data):
    """Pulse a one-cycle host write into the TX FIFO."""
    await FallingEdge(dut.clk)
    sm(dut).host_tx_wdata.value = data & 0xFF
    sm(dut).host_tx_wr_en.value = 1
    await RisingEdge(dut.clk)


async def host_rx_read(dut):
    """Pulse a one-cycle host read from the RX FIFO; return the data word."""
    assert int(sm(dut).rx_empty.value) == 0, "RX FIFO empty"
    data = int(sm(dut).rx_rdata.value)
    await FallingEdge(dut.clk)
    sm(dut).host_rx_rd_en.value = 1
    await RisingEdge(dut.clk)
    return data


# ---------------------------------------------------------------------------
# Reset / pinout
# ---------------------------------------------------------------------------


@cocotb.test()
async def test_reset_state(dut):
    """Reset clears state and the uio host pins have a fixed direction."""
    await start_clock(dut)
    await load_program(dut, [halt(0)])

    assert dut.uo_out.value == 0
    assert dut.uio_oe.value == UIO_OE_HOST
    assert sm(dut).pc.value == 0
    assert sm(dut).x.value == 0
    assert sm(dut).y.value == 0
    assert sm(dut).isr.value == 0
    assert sm(dut).osr.value == 0
    assert sm(dut).pins_oe.value == 0


# ---------------------------------------------------------------------------
# SET
# ---------------------------------------------------------------------------


@cocotb.test()
async def test_set_pins(dut):
    """SET PINS writes the 5-bit immediate onto uo_out."""
    await start_clock(dut)
    await load_program(dut, [encode_set(DEST_PINS, 0x15), halt(1)])

    assert dut.uo_out.value == 0x15


@cocotb.test()
async def test_set_pins_clears_upper_bits(dut):
    """SET PINS zero-extends, so uo_out[7:5] set by OUT are cleared."""
    await start_clock(dut)
    await run_with_osr(
        dut,
        0xFF,
        [
            encode_out(DEST_PINS, 8),
            encode_set(DEST_PINS, 0x01),
            halt(3),
        ],
    )

    assert dut.uo_out.value == 0x01


@cocotb.test()
async def test_set_x_y(dut):
    """SET X and SET Y load the 5-bit immediate into the scratch registers."""
    await start_clock(dut)
    await load_program(
        dut,
        [
            encode_set(DEST_X, 0x1F),
            encode_set(DEST_Y, 0x0A),
            halt(2),
        ],
    )
    await ClockCycles(dut.clk, 2)

    assert sm(dut).x.value == 0x1F
    assert sm(dut).y.value == 0x0A


@cocotb.test()
async def test_set_pindirs(dut):
    """SET PINDIRS writes pins_oe; the uio host pin directions are unaffected."""
    await start_clock(dut)
    await load_program(dut, [encode_set(DEST_PINDIRS, 0x1F), halt(1)])
    await ClockCycles(dut.clk, 1)

    assert sm(dut).pins_oe.value == 0x1F
    assert dut.uio_oe.value == UIO_OE_HOST


# ---------------------------------------------------------------------------
# JMP
# ---------------------------------------------------------------------------


@cocotb.test()
async def test_jmp_always(dut):
    """Unconditional JMP skips the instructions in between."""
    await start_clock(dut)
    await load_program(
        dut,
        [
            encode_jmp(3),
            encode_set(DEST_PINS, 0x02),
            encode_set(DEST_PINS, 0x03),
            encode_set(DEST_PINS, 0x04),
            halt(4),
        ],
    )
    await ClockCycles(dut.clk, 4)

    assert dut.uo_out.value == 4


@cocotb.test()
async def test_jmp_not_x_taken(dut):
    """JMP !X skips the middle SET when X is 0."""
    await start_clock(dut)
    await load_program(
        dut,
        [
            encode_set(DEST_X, 0),
            encode_jmp(3, JMP_NOT_X),
            encode_set(DEST_PINS, 0x02),
            encode_set(DEST_PINS, 0x04),
            halt(4),
        ],
    )
    await ClockCycles(dut.clk, 4)

    assert dut.uo_out.value == 4


@cocotb.test()
async def test_jmp_not_x_not_taken(dut):
    """JMP !X falls through when X is nonzero."""
    await start_clock(dut)
    await load_program(
        dut,
        [
            encode_set(DEST_X, 1),
            encode_jmp(3, JMP_NOT_X),
            encode_set(DEST_PINS, 0x02),
            halt(3),
        ],
    )
    await ClockCycles(dut.clk, 4)

    assert dut.uo_out.value == 2


@cocotb.test()
async def test_jmp_x_dec(dut):
    """JMP X-- loops until X hits 0, then the final SET runs."""
    await start_clock(dut)
    await load_program(
        dut,
        [
            encode_set(DEST_X, 3),
            encode_set(DEST_PINS, 0x01),
            encode_jmp(1, JMP_X_DEC),
            encode_set(DEST_PINS, 0x1F),
            halt(4),
        ],
    )
    await ClockCycles(dut.clk, 20)

    assert dut.uo_out.value == 0x1F


@cocotb.test()
async def test_jmp_x_dec_iteration_count(dut):
    """A JMP X-- loop body runs X+1 times (one 1-bit IN per iteration)."""
    await start_clock(dut)
    dut.ui_in.value = 0x01
    await load_program(
        dut,
        [
            encode_set(DEST_X, 3),
            encode_in(SRC_PINS, 1),
            encode_jmp(1, JMP_X_DEC),
            halt(3),
        ],
    )
    await ClockCycles(dut.clk, 20)

    assert sm(dut).isr.value == 0b1111


@cocotb.test()
async def test_jmp_not_y_taken(dut):
    """JMP !Y jumps when Y is 0."""
    await start_clock(dut)
    await load_program(
        dut,
        [
            encode_set(DEST_Y, 0),
            encode_jmp(3, JMP_NOT_Y),
            encode_set(DEST_PINS, 0x02),
            encode_set(DEST_PINS, 0x04),
            halt(4),
        ],
    )
    await ClockCycles(dut.clk, 4)

    assert dut.uo_out.value == 4


@cocotb.test()
async def test_jmp_not_y_not_taken(dut):
    """JMP !Y falls through when Y is nonzero."""
    await start_clock(dut)
    await load_program(
        dut,
        [
            encode_set(DEST_Y, 5),
            encode_jmp(3, JMP_NOT_Y),
            encode_set(DEST_PINS, 0x02),
            halt(3),
        ],
    )
    await ClockCycles(dut.clk, 4)

    assert dut.uo_out.value == 2


@cocotb.test()
async def test_jmp_y_dec_iteration_count(dut):
    """A JMP Y-- loop body runs Y+1 times."""
    await start_clock(dut)
    dut.ui_in.value = 0x01
    await load_program(
        dut,
        [
            encode_set(DEST_Y, 2),
            encode_in(SRC_PINS, 1),
            encode_jmp(1, JMP_Y_DEC),
            halt(3),
        ],
    )
    await ClockCycles(dut.clk, 20)

    assert sm(dut).isr.value == 0b111


@cocotb.test()
async def test_jmp_x_ne_y_taken(dut):
    """JMP X!=Y jumps when X and Y differ."""
    await start_clock(dut)
    await load_program(
        dut,
        [
            encode_set(DEST_X, 3),
            encode_set(DEST_Y, 4),
            encode_jmp(4, JMP_X_NE_Y),
            encode_set(DEST_PINS, 0x02),
            encode_set(DEST_PINS, 0x08),
            halt(5),
        ],
    )
    await ClockCycles(dut.clk, 6)

    assert dut.uo_out.value == 0x08


@cocotb.test()
async def test_jmp_x_ne_y_not_taken(dut):
    """JMP X!=Y falls through when X equals Y."""
    await start_clock(dut)
    await load_program(
        dut,
        [
            encode_set(DEST_X, 7),
            encode_set(DEST_Y, 7),
            encode_jmp(5, JMP_X_NE_Y),
            encode_set(DEST_PINS, 0x02),
            halt(4),
        ],
    )
    await ClockCycles(dut.clk, 6)

    assert dut.uo_out.value == 0x02


# ---------------------------------------------------------------------------
# WAIT
# ---------------------------------------------------------------------------


@cocotb.test()
async def test_wait_gpio_high(dut):
    """WAIT 1 GPIO 0 stalls until ui_in[0] goes high, then SET runs."""
    await start_clock(dut)
    dut.ui_in.value = 0
    await load_program(
        dut,
        [
            encode_wait(1, index=0),
            encode_set(DEST_PINS, 0x01),
            halt(2),
        ],
    )

    # Still waiting: pin is low, SET must not have run
    await ClockCycles(dut.clk, 5)
    assert dut.uo_out.value == 0
    assert sm(dut).pc.value == 0

    dut.ui_in.value = 1
    await ClockCycles(dut.clk, 3)
    assert dut.uo_out.value == 1


@cocotb.test()
async def test_wait_gpio_low(dut):
    """WAIT 0 GPIO 0 stalls until ui_in[0] goes low, then SET runs."""
    await start_clock(dut)
    dut.ui_in.value = 1
    await load_program(
        dut,
        [
            encode_wait(0, index=0),
            encode_set(DEST_PINS, 0x07),
            halt(2),
        ],
    )

    await ClockCycles(dut.clk, 5)
    assert dut.uo_out.value == 0

    dut.ui_in.value = 0
    await ClockCycles(dut.clk, 3)
    assert dut.uo_out.value == 7


@cocotb.test()
async def test_wait_already_true(dut):
    """If the WAIT condition is already true, it completes on the first cycle."""
    await start_clock(dut)
    dut.ui_in.value = 1
    await load_program(
        dut,
        [
            encode_wait(1, index=0),
            encode_set(DEST_PINS, 0x03),
            halt(2),
        ],
    )
    await ClockCycles(dut.clk, 2)

    assert dut.uo_out.value == 3


@cocotb.test()
async def test_wait_gpio_index(dut):
    """WAIT watches only the indexed pin; other pins toggling don't release it."""
    await start_clock(dut)
    dut.ui_in.value = 0
    await load_program(
        dut,
        [
            encode_wait(1, index=5),
            encode_set(DEST_PINS, 0x05),
            halt(2),
        ],
    )

    dut.ui_in.value = 0xDF  # every pin high except ui_in[5]
    await ClockCycles(dut.clk, 5)
    assert dut.uo_out.value == 0

    dut.ui_in.value = 0x20
    await ClockCycles(dut.clk, 3)
    assert dut.uo_out.value == 0x05


# ---------------------------------------------------------------------------
# IN (shift left into ISR)
# ---------------------------------------------------------------------------


@cocotb.test()
async def test_in_pins_full(dut):
    """IN PINS with bit count 0 shifts in all 8 bits of ui_in."""
    await start_clock(dut)
    dut.ui_in.value = 0xA5
    await load_program(dut, [encode_in(SRC_PINS, 0), halt(1)])
    await ClockCycles(dut.clk, 2)

    assert sm(dut).isr.value == 0xA5


@cocotb.test()
async def test_in_pins_partial(dut):
    """Two IN PINS 4 shift the low nibble of ui_in in twice."""
    await start_clock(dut)
    dut.ui_in.value = 0xA5
    await load_program(
        dut,
        [
            encode_in(SRC_PINS, 4),
            encode_in(SRC_PINS, 4),
            halt(2),
        ],
    )
    await ClockCycles(dut.clk, 3)

    assert sm(dut).isr.value == 0x55


@cocotb.test()
async def test_in_count_over_8_is_full(dut):
    """Bit counts above 8 are treated as a full 8-bit shift."""
    await start_clock(dut)
    dut.ui_in.value = 0x3C
    await load_program(
        dut,
        [
            encode_in(SRC_PINS, 4),
            encode_in(SRC_PINS, 31),
            halt(2),
        ],
    )
    await ClockCycles(dut.clk, 3)

    assert sm(dut).isr.value == 0x3C


@cocotb.test()
async def test_in_x_y(dut):
    """IN X / IN Y shift in the low bits of the scratch registers."""
    await start_clock(dut)
    await load_program(
        dut,
        [
            encode_set(DEST_X, 0x1F),
            encode_set(DEST_Y, 0x02),
            encode_in(SRC_X, 3),
            encode_in(SRC_Y, 2),
            halt(4),
        ],
    )
    await ClockCycles(dut.clk, 5)

    assert sm(dut).isr.value == 0b11110


@cocotb.test()
async def test_in_null(dut):
    """IN NULL shifts zeros in."""
    await start_clock(dut)
    dut.ui_in.value = 0xA5
    await load_program(
        dut,
        [
            encode_in(SRC_PINS, 8),
            encode_in(SRC_NULL, 4),
            halt(2),
        ],
    )
    await ClockCycles(dut.clk, 3)

    assert sm(dut).isr.value == 0x50


@cocotb.test()
async def test_in_isr(dut):
    """IN ISR shifts in the low bits of the old ISR value."""
    await start_clock(dut)
    dut.ui_in.value = 0xA5
    await load_program(
        dut,
        [
            encode_in(SRC_PINS, 8),
            encode_in(SRC_ISR, 4),
            halt(2),
        ],
    )
    await ClockCycles(dut.clk, 3)

    assert sm(dut).isr.value == 0x55


@cocotb.test()
async def test_in_osr(dut):
    """IN OSR copies OSR bits into ISR without consuming OSR."""
    await start_clock(dut)
    await run_with_osr(dut, 0xC3, [encode_in(SRC_OSR, 8), halt(2)])

    assert sm(dut).isr.value == 0xC3
    assert sm(dut).osr.value == 0xC3


# ---------------------------------------------------------------------------
# OUT (shift right out of OSR)
# ---------------------------------------------------------------------------


@cocotb.test()
async def test_out_pins_partial(dut):
    """OUT PINS 4 writes the low nibble and leaves the other pins alone."""
    await start_clock(dut)
    await run_with_osr(
        dut,
        0xA5,
        [
            encode_set(DEST_PINS, 0x10),
            encode_out(DEST_PINS, 4),
            halt(3),
        ],
    )

    assert dut.uo_out.value == 0x15
    assert sm(dut).osr.value == 0x0A


@cocotb.test()
async def test_out_pins_sequence(dut):
    """Successive OUT PINS consume OSR from the LSB end."""
    await start_clock(dut)
    await run_with_osr(
        dut,
        0xA5,
        [
            encode_out(DEST_PINS, 4),
            encode_out(DEST_PINS, 8),
            halt(3),
        ],
    )

    assert dut.uo_out.value == 0x0A
    assert sm(dut).osr.value == 0


@cocotb.test()
async def test_out_x_y(dut):
    """OUT X / OUT Y load masked OSR bits into the scratch registers."""
    await start_clock(dut)
    await run_with_osr(
        dut,
        0xC3,
        [
            encode_out(DEST_X, 4),
            encode_out(DEST_Y, 4),
            halt(3),
        ],
    )

    assert sm(dut).x.value == 0x3
    assert sm(dut).y.value == 0xC
    assert sm(dut).osr.value == 0


@cocotb.test()
async def test_out_null(dut):
    """OUT NULL discards bits but still shifts OSR."""
    await start_clock(dut)
    await run_with_osr(
        dut,
        0xA5,
        [
            encode_out(DEST_NULL, 4),
            encode_out(DEST_PINS, 8),
            halt(3),
        ],
    )

    assert dut.uo_out.value == 0x0A


@cocotb.test()
async def test_out_pindirs(dut):
    """OUT PINDIRS writes pins_oe."""
    await start_clock(dut)
    await run_with_osr(dut, 0xA5, [encode_out(DEST_PINDIRS, 8), halt(2)])

    assert sm(dut).pins_oe.value == 0xA5
    assert dut.uio_oe.value == UIO_OE_HOST


@cocotb.test()
async def test_out_pc(dut):
    """OUT PC performs a computed jump to the OSR value."""
    await start_clock(dut)
    await run_with_osr(
        dut,
        0x06,
        [
            encode_out(DEST_PC, 5),        # 1
            encode_set(DEST_PINS, 0x01),   # 2
            encode_set(DEST_PINS, 0x02),   # 3
            halt(4),                       # 4
            halt(5),                       # 5
            encode_set(DEST_PINS, 0x1E),   # 6
            halt(7),                       # 7
        ],
    )

    assert dut.uo_out.value == 0x1E
    assert sm(dut).pc.value == 7


@cocotb.test()
async def test_out_isr(dut):
    """OUT ISR overwrites ISR with the shifted-out bits."""
    await start_clock(dut)
    dut.ui_in.value = 0
    await load_program(
        dut,
        [
            encode_wait(1, index=7),
            encode_in(SRC_PINS, 8),   # ISR = 0x80 (just ui_in[7])
            encode_out(DEST_ISR, 4),
            halt(3),
        ],
    )
    sm(dut).osr.value = 0x5A
    await ClockCycles(dut.clk, 1)
    dut.ui_in.value = 0x80
    await ClockCycles(dut.clk, 6)

    assert sm(dut).isr.value == 0x0A
    assert sm(dut).osr.value == 0x05


@cocotb.test()
async def test_out_exec_shifts_and_advances(dut):
    """OUT EXEC (not implemented) still consumes OSR and advances PC."""
    await start_clock(dut)
    await run_with_osr(
        dut,
        0xF0,
        [
            encode_out(DEST_EXEC, 4),
            encode_set(DEST_PINS, 0x09),
            halt(3),
        ],
    )

    assert sm(dut).osr.value == 0x0F
    assert dut.uo_out.value == 0x09


# ---------------------------------------------------------------------------
# MOV
# ---------------------------------------------------------------------------


@cocotb.test()
async def test_mov_x_to_y(dut):
    """MOV Y, X copies X into Y."""
    await start_clock(dut)
    await load_program(
        dut,
        [
            encode_set(DEST_X, 0x1A),
            encode_mov(MOV_DEST_Y, SRC_X),
            halt(2),
        ],
    )
    await ClockCycles(dut.clk, 4)

    assert sm(dut).x.value == 0x1A
    assert sm(dut).y.value == 0x1A
    assert sm(dut).pc.value == 2


@cocotb.test()
async def test_mov_invert(dut):
    """MOV X, ~Y writes the bitwise inverse of Y into X."""
    await start_clock(dut)
    await load_program(
        dut,
        [
            encode_set(DEST_Y, 0x0C),
            encode_mov(MOV_DEST_X, SRC_Y, MOV_OP_INVERT),
            halt(2),
        ],
    )
    await ClockCycles(dut.clk, 4)

    assert sm(dut).y.value == 0x0C
    assert sm(dut).x.value == 0xF3


@cocotb.test()
async def test_mov_bitrev(dut):
    """MOV PINS, ::OSR bit-reverses OSR onto the output pins."""
    await start_clock(dut)
    # 0xA3 = 1010_0011 → bitrev 1100_0101 = 0xC5
    await run_with_osr(
        dut,
        0xA3,
        [
            encode_mov(MOV_DEST_PINS, SRC_OSR, MOV_OP_BITREV),
            halt(2),
        ],
    )

    assert dut.uo_out.value == 0xC5
    assert sm(dut).osr.value == 0xA3


@cocotb.test()
async def test_mov_pins_src(dut):
    """MOV X, PINS samples ui_in into X."""
    await start_clock(dut)
    dut.ui_in.value = 0x5A
    await load_program(
        dut,
        [
            encode_mov(MOV_DEST_X, SRC_PINS),
            halt(1),
        ],
    )
    await ClockCycles(dut.clk, 3)

    assert sm(dut).x.value == 0x5A


@cocotb.test()
async def test_mov_null_clears(dut):
    """MOV X, NULL writes zeros."""
    await start_clock(dut)
    await load_program(
        dut,
        [
            encode_set(DEST_X, 0x1F),
            encode_mov(MOV_DEST_X, SRC_NULL),
            halt(2),
        ],
    )
    await ClockCycles(dut.clk, 4)

    assert sm(dut).x.value == 0


@cocotb.test()
async def test_mov_status_is_zero(dut):
    """MOV STATUS source is stubbed to 0."""
    await start_clock(dut)
    await load_program(
        dut,
        [
            encode_set(DEST_X, 0x15),
            encode_mov(MOV_DEST_X, SRC_STATUS),
            halt(2),
        ],
    )
    await ClockCycles(dut.clk, 4)

    assert sm(dut).x.value == 0


@cocotb.test()
async def test_mov_isr_osr_swap(dut):
    """MOV can copy ISR↔OSR without shifting."""
    await start_clock(dut)
    dut.ui_in.value = 0
    await load_program(
        dut,
        [
            encode_wait(1, index=7),
            encode_mov(MOV_DEST_ISR, SRC_OSR),
            encode_mov(MOV_DEST_OSR, SRC_NULL),
            encode_mov(MOV_DEST_OSR, SRC_ISR),
            halt(4),
        ],
    )
    sm(dut).osr.value = 0x3C
    await ClockCycles(dut.clk, 1)
    dut.ui_in.value = 0x80
    await ClockCycles(dut.clk, 6)

    assert sm(dut).isr.value == 0x3C
    assert sm(dut).osr.value == 0x3C


@cocotb.test()
async def test_mov_to_pc(dut):
    """MOV PC, X jumps to X[4:0]."""
    await start_clock(dut)
    await load_program(
        dut,
        [
            encode_set(DEST_X, 0x04),
            encode_mov(MOV_DEST_PC, SRC_X),
            encode_set(DEST_PINS, 0x01),  # 2: skipped
            encode_set(DEST_PINS, 0x02),  # 3: skipped
            encode_set(DEST_PINS, 0x1E),  # 4: landed here
            halt(5),
        ],
    )
    await ClockCycles(dut.clk, 6)

    assert sm(dut).pc.value == 5
    assert dut.uo_out.value == 0x1E


@cocotb.test()
async def test_mov_exec_advances(dut):
    """MOV EXEC is a no-op but still advances PC."""
    await start_clock(dut)
    await load_program(
        dut,
        [
            encode_set(DEST_X, 0x0A),
            encode_mov(MOV_DEST_EXEC, SRC_X),
            encode_set(DEST_PINS, 0x07),
            halt(3),
        ],
    )
    await ClockCycles(dut.clk, 5)

    assert sm(dut).pc.value == 3
    assert sm(dut).x.value == 0x0A
    assert dut.uo_out.value == 0x07


# ---------------------------------------------------------------------------
# PUSH / PULL
# ---------------------------------------------------------------------------


@cocotb.test()
async def test_pull_loads_osr_from_tx(dut):
    """PULL copies TX FIFO data into OSR and advances PC."""
    await start_clock(dut)
    await load_program(
        dut,
        [
            encode_pull(ife=0, blk=1),
            encode_out(DEST_PINS, 8),
            halt(2),
        ],
    )

    # Stall on empty TX until host writes
    await ClockCycles(dut.clk, 3)
    assert sm(dut).pc.value == 0
    assert int(sm(dut).tx_empty.value) == 1

    await host_tx_write(dut, 0xA5)
    await ClockCycles(dut.clk, 3)

    assert sm(dut).pc.value == 2
    assert sm(dut).osr.value == 0
    assert dut.uo_out.value == 0xA5
    assert sm(dut).osr_count.value == 0


@cocotb.test()
async def test_pull_ife_skips_when_osr_full(dut):
    """PULL IfE is a no-op when OSR still has bits."""
    await start_clock(dut)
    await load_program(
        dut,
        [
            encode_pull(ife=1, blk=1),
            encode_out(DEST_PINS, 8),
            halt(2),
        ],
    )
    await host_tx_write(dut, 0x3C)
    # Pre-fill OSR so IfE skips the pull
    sm(dut).osr.value = 0x12
    sm(dut).osr_count.value = 8
    await ClockCycles(dut.clk, 4)

    assert dut.uo_out.value == 0x12
    assert int(sm(dut).tx_empty.value) == 0  # TX word never consumed


@cocotb.test()
async def test_pull_noblock_skips_when_empty(dut):
    """PULL !Blk advances without loading when TX is empty."""
    await start_clock(dut)
    await load_program(
        dut,
        [
            encode_pull(ife=0, blk=0),
            encode_set(DEST_PINS, 0x07),
            halt(2),
        ],
    )
    await ClockCycles(dut.clk, 3)

    assert sm(dut).pc.value == 2
    assert sm(dut).osr.value == 0
    assert dut.uo_out.value == 0x07


@cocotb.test()
async def test_push_sends_isr_to_rx(dut):
    """PUSH writes ISR into RX FIFO, clears ISR, and advances PC."""
    await start_clock(dut)
    dut.ui_in.value = 0x5A
    await load_program(
        dut,
        [
            encode_in(SRC_PINS, 8),
            encode_push(iff=0, blk=1),
            halt(2),
        ],
    )
    await ClockCycles(dut.clk, 4)

    assert sm(dut).pc.value == 2
    assert sm(dut).isr.value == 0
    assert sm(dut).isr_count.value == 0
    assert int(sm(dut).rx_empty.value) == 0
    assert int(sm(dut).rx_rdata.value) == 0x5A

    data = await host_rx_read(dut)
    assert data == 0x5A
    await ClockCycles(dut.clk, 1)
    assert int(sm(dut).rx_empty.value) == 1


@cocotb.test()
async def test_push_iff_skips_when_isr_not_full(dut):
    """PUSH IfF is a no-op unless ISR has a full 8 bits."""
    await start_clock(dut)
    dut.ui_in.value = 0x0F
    await load_program(
        dut,
        [
            encode_in(SRC_PINS, 4),       # only 4 bits → not full
            encode_push(iff=1, blk=1),
            encode_set(DEST_PINS, 0x01),
            halt(3),
        ],
    )
    await ClockCycles(dut.clk, 5)

    assert sm(dut).pc.value == 3
    assert sm(dut).isr_count.value == 4
    assert int(sm(dut).rx_empty.value) == 1  # nothing pushed
    assert dut.uo_out.value == 0x01


@cocotb.test()
async def test_push_pull_roundtrip(dut):
    """Host TX -> PULL -> OUT pins, and IN -> PUSH -> host RX."""
    await start_clock(dut)
    dut.ui_in.value = 0xC3
    await load_program(
        dut,
        [
            encode_pull(ife=0, blk=1),    # 0: OSR ← TX
            encode_out(DEST_PINS, 8),     # 1: pins ← OSR
            encode_in(SRC_PINS, 8),       # 2: ISR ← ui_in
            encode_push(iff=0, blk=1),    # 3: RX ← ISR
            halt(4),
        ],
    )

    await host_tx_write(dut, 0x91)
    await ClockCycles(dut.clk, 6)

    assert dut.uo_out.value == 0x91
    assert sm(dut).pc.value == 4
    assert int(sm(dut).rx_rdata.value) == 0xC3
    data = await host_rx_read(dut)
    assert data == 0xC3
