# SPDX-FileCopyrightText: © 2024 Tiny Tapeout
# SPDX-License-Identifier: Apache-2.0

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles


# PIO encoding: [15:13] opcode, [12:8] delay, [7:0] args
OP_JMP = 0b000
OP_WAIT = 0b001
OP_SET = 0b111

DEST_PINS = 0b000
DEST_X = 0b001
DEST_Y = 0b010
DEST_PINDIRS = 0b100

JMP_ALWAYS = 0b000
JMP_NOT_X = 0b001
JMP_X_DEC = 0b010
JMP_NOT_Y = 0b011
JMP_Y_DEC = 0b100
JMP_X_NE_Y = 0b101

WAIT_GPIO = 0b00


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


def encode_set(dest, data, delay=0):
    return (
        (OP_SET << 13)
        | ((delay & 0x1F) << 8)
        | ((dest & 0x7) << 5)
        | (data & 0x1F)
    )


def halt(address):
    return encode_jmp(address, JMP_ALWAYS)


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


@cocotb.test()
async def test_set_pins(dut):
    """SET PINS writes the 5-bit immediate onto uo_out / uio_out."""
    await start_clock(dut)
    await load_program(
        dut,
        [
            encode_set(DEST_PINS, 0x15),
            halt(1),
        ],
    )

    assert dut.uo_out.value == 0x15
    assert dut.uio_out.value == 0x15


@cocotb.test()
async def test_set_pindirs(dut):
    """SET PINDIRS writes the 5-bit immediate onto uio_oe."""
    await start_clock(dut)
    await load_program(
        dut,
        [
            encode_set(DEST_PINDIRS, 0x1F),
            halt(1),
        ],
    )

    assert dut.uio_oe.value == 0x1F


@cocotb.test()
async def test_wait_gpio_high(dut):
    """WAIT 1 GPIO 0 stalls until uio_in[0] goes high, then SET runs."""
    await start_clock(dut)
    dut.uio_in.value = 0
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

    dut.uio_in.value = 1
    await ClockCycles(dut.clk, 3)
    assert dut.uo_out.value == 1


@cocotb.test()
async def test_wait_gpio_low(dut):
    """WAIT 0 GPIO 0 stalls until uio_in[0] goes low, then SET runs."""
    await start_clock(dut)
    dut.uio_in.value = 1
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

    dut.uio_in.value = 0
    await ClockCycles(dut.clk, 3)
    assert dut.uo_out.value == 7


@cocotb.test()
async def test_wait_already_true(dut):
    """If the WAIT condition is already true, it completes on the first cycle."""
    await start_clock(dut)
    dut.uio_in.value = 1
    await load_program(
        dut,
        [
            encode_wait(1, index=0),
            encode_set(DEST_PINS, 0x03),
            halt(2),
        ],
    )

    # WAIT already true, then SET
    await ClockCycles(dut.clk, 2)
    assert dut.uo_out.value == 3


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
