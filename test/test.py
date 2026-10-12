# SPDX-FileCopyrightText: © 2024 Tiny Tapeout
# SPDX-License-Identifier: Apache-2.0

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles


# PIO encoding: [15:13] opcode, [12:8] delay, [7:0] args
OP_JMP = 0b000
OP_WAIT = 0b001
OP_IN = 0b010
OP_OUT = 0b011
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

# IN sources
SRC_PINS = 0b000
SRC_X = 0b001
SRC_Y = 0b010
SRC_NULL = 0b011
SRC_ISR = 0b110
SRC_OSR = 0b111

JMP_ALWAYS = 0b000
JMP_NOT_X = 0b001
JMP_X_DEC = 0b010
JMP_NOT_Y = 0b011
JMP_Y_DEC = 0b100
JMP_X_NE_Y = 0b101

WAIT_GPIO = 0b00

# uio is the SPI host interface: miso, tx_full, rx_not_empty, running are outputs
UIO_OE_HOST = 0b0111_0100


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
    dut.imem_we.value = 0


async def load_program(dut, program):
    """Write 32 instruction words while held in reset, then release."""
    dut.rst_n.value = 0
    dut.imem_we.value = 0
    await ClockCycles(dut.clk, 2)

    words = [halt(i) for i in range(32)]
    for i, word in enumerate(program):
        words[i] = word

    for addr, word in enumerate(words):
        dut.imem_addr.value = addr
        dut.imem_wdata.value = word
        dut.imem_we.value = 1
        await ClockCycles(dut.clk, 1)

    dut.imem_we.value = 0
    await ClockCycles(dut.clk, 1)
    dut.rst_n.value = 1
    # First edge after reset release executes instruction 0
    await ClockCycles(dut.clk, 2)


async def run_with_osr(dut, osr, program, cycles=10):
    """Run `program` with OSR preloaded.

    There is no PULL yet, so the program is parked on WAIT 1 GPIO 7 at
    address 0 while OSR is written directly, then released. The program
    passed in starts at address 1.
    """
    dut.ui_in.value = 0
    await load_program(dut, [encode_wait(1, index=7)] + program)
    sm(dut).osr.value = osr
    await ClockCycles(dut.clk, 1)
    dut.ui_in.value = 0x80
    await ClockCycles(dut.clk, cycles)


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
