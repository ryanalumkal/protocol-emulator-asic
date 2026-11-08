# SPDX-FileCopyrightText: © 2026 Ryan Neil Alumkal
# SPDX-License-Identifier: Apache-2.0
"""SPI host interface tests. Port-level only, so they run on RTL and gate level."""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles

from pio_model import OP_JMP, OP_SET, encode, encode_mov, encode_pull, encode_push
from spi_host import (
    CMD_CTRL,
    CMD_READ_RX,
    CMD_WRITE_IMEM,
    CMD_WRITE_TX,
    CTRL_FIFO_CLR,
    CTRL_RUN,
    CTRL_SM_RST,
    CTRL_STEP,
    ST_MARKER,
    ST_RUNNING,
    ST_RX_EMPTY,
    ST_RX_UDF,
    ST_TX_EMPTY,
    ST_TX_FULL,
    ST_TX_OVF,
    spi_ctrl,
    spi_idle,
    spi_read_rx,
    spi_status,
    spi_transfer,
    spi_write_imem,
    spi_write_tx,
)

STATUS_RESET = ST_TX_EMPTY | ST_RX_EMPTY | ST_MARKER  # 0x29
UIO_RX_NOT_EMPTY = 1 << 5
UIO_RUNNING = 1 << 6

SET_PINS, SET_X = 0, 1
MOV_ISR, MOV_SRC_X, MOV_SRC_OSR, MOV_INVERT = 6, 1, 7, 1


def jmp(addr):
    return encode(OP_JMP, 0, addr)


def set_pins(value):
    return encode(OP_SET, SET_PINS, value)


async def reset(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.ena.value = 1
    dut.ui_in.value = 0
    spi_idle(dut)
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 5)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 5)


async def load_and_run(dut, program):
    await spi_write_imem(dut, 0, program)
    await spi_ctrl(dut, CTRL_RUN)


@cocotb.test()
async def test_status_after_reset(dut):
    """Status byte after reset: halted, both FIFOs empty, marker bit set."""
    await reset(dut)
    assert await spi_status(dut) == STATUS_RESET
    assert int(dut.uio_out.value) & UIO_RUNNING == 0


@cocotb.test()
async def test_ctrl_run_halt(dut):
    """CTRL bit 0 sets and clears running, visible in status and on uio[6]."""
    await reset(dut)
    await spi_write_imem(dut, 0, [jmp(0)])
    await spi_ctrl(dut, CTRL_RUN)
    assert await spi_status(dut) & ST_RUNNING
    assert int(dut.uio_out.value) & UIO_RUNNING

    await spi_ctrl(dut, 0)
    assert await spi_status(dut) & ST_RUNNING == 0
    assert int(dut.uio_out.value) & UIO_RUNNING == 0


@cocotb.test()
async def test_imem_burst_load_and_run(dut):
    """Program loaded with one WRITE_IMEM burst runs after CTRL RUN."""
    await reset(dut)
    await load_and_run(dut, [set_pins(0x03), set_pins(0x15), jmp(2), jmp(3)])
    await ClockCycles(dut.clk, 5)
    assert dut.uo_out.value == 0x15


@cocotb.test()
async def test_imem_address_wraps(dut):
    """A burst starting at 15 wraps to 0: one transaction writes words 15 and 0."""
    await reset(dut)
    await spi_write_imem(dut, 15, [set_pins(0x0B), jmp(15)])
    await spi_ctrl(dut, CTRL_RUN)
    await ClockCycles(dut.clk, 5)
    assert dut.uo_out.value == 0x0B


@cocotb.test()
async def test_imem_write_while_running(dut):
    """IMEM can be rewritten while the SM runs (RP2040 PIO behaviour)."""
    await reset(dut)
    await load_and_run(dut, [set_pins(0x01), jmp(0)])
    await ClockCycles(dut.clk, 5)
    assert dut.uo_out.value == 0x01

    await spi_write_imem(dut, 0, [set_pins(0x02)])
    await ClockCycles(dut.clk, 5)
    assert dut.uo_out.value == 0x02


@cocotb.test()
async def test_tx_rx_stream(dut):
    """Host TX -> PULL -> MOV ISR, ~OSR -> PUSH -> host RX, through both FIFOs."""
    await reset(dut)
    await load_and_run(
        dut,
        [
            encode_pull(),
            encode_mov(MOV_ISR, MOV_SRC_OSR, MOV_INVERT),
            encode_push(),
            jmp(0),
        ],
    )
    data = [0x00, 0x5A, 0xC3, 0xFF]
    await spi_write_tx(dut, data)
    await ClockCycles(dut.clk, 20)
    assert int(dut.uio_out.value) & UIO_RX_NOT_EMPTY
    assert await spi_read_rx(dut, 4) == [~d & 0xFF for d in data]
    assert await spi_status(dut) & ST_RX_EMPTY
    assert int(dut.uio_out.value) & UIO_RX_NOT_EMPTY == 0


@cocotb.test()
async def test_read_rx_pops_only_bytes_read(dut):
    """Back-to-back single-byte reads each get the next byte; nothing is skipped."""
    await reset(dut)
    await load_and_run(
        dut,
        [
            encode(OP_SET, SET_X, 0x11),
            encode_mov(MOV_ISR, MOV_SRC_X),
            encode_push(),
            encode(OP_SET, SET_X, 0x12),
            encode_mov(MOV_ISR, MOV_SRC_X),
            encode_push(),
            jmp(6),
        ],
    )
    await ClockCycles(dut.clk, 10)
    assert await spi_read_rx(dut, 1) == [0x11]
    assert await spi_read_rx(dut, 1) == [0x12]
    status = await spi_status(dut)
    assert status & ST_RX_EMPTY
    assert status & ST_RX_UDF == 0


@cocotb.test()
async def test_tx_overflow_flag(dut):
    """Writing a fifth byte into the 4-deep TX FIFO drops it and sets tx_ovf until read."""
    await reset(dut)
    await spi_write_tx(dut, [1, 2, 3, 4, 5])
    status = await spi_status(dut)
    assert status & ST_TX_FULL
    assert status & ST_TX_OVF
    # Reading STATUS cleared the sticky flag
    assert await spi_status(dut) & ST_TX_OVF == 0


@cocotb.test()
async def test_rx_underflow_flag(dut):
    """Reading an empty RX FIFO returns 0x00 and sets rx_udf until read."""
    await reset(dut)
    assert await spi_read_rx(dut, 1) == [0x00]
    assert await spi_status(dut) & ST_RX_UDF
    assert await spi_status(dut) & ST_RX_UDF == 0


@cocotb.test()
async def test_cs_abort_mid_byte(dut):
    """cs_n rising mid-byte drops the partial byte; the next transaction is clean."""
    await reset(dut)
    await spi_transfer(dut, [CMD_WRITE_TX, 0xAB], abort_after_bits=12)
    assert await spi_status(dut) == STATUS_RESET

    await spi_write_tx(dut, [0xCD])
    assert await spi_status(dut) & ST_TX_EMPTY == 0


@cocotb.test()
async def test_ctrl_step(dut):
    """CTRL step executes exactly one instruction while halted."""
    await reset(dut)
    await spi_write_imem(dut, 0, [set_pins(1), set_pins(2), set_pins(3), jmp(3)])
    await spi_ctrl(dut, CTRL_STEP)
    await ClockCycles(dut.clk, 5)
    assert dut.uo_out.value == 1
    await spi_ctrl(dut, CTRL_STEP)
    await ClockCycles(dut.clk, 5)
    assert dut.uo_out.value == 2
    assert await spi_status(dut) & ST_RUNNING == 0


@cocotb.test()
async def test_ctrl_sm_reset_and_fifo_flush(dut):
    """CTRL SM reset clears SM state and halts; FIFO flush empties TX."""
    await reset(dut)
    await load_and_run(dut, [set_pins(0x1F), jmp(1)])
    await ClockCycles(dut.clk, 5)
    assert dut.uo_out.value == 0x1F

    await spi_ctrl(dut, CTRL_SM_RST)
    assert dut.uo_out.value == 0
    assert await spi_status(dut) & ST_RUNNING == 0

    await spi_write_tx(dut, [0x42])
    assert await spi_status(dut) & ST_TX_EMPTY == 0
    await spi_ctrl(dut, CTRL_FIFO_CLR)
    assert await spi_status(dut) == STATUS_RESET

    # SM reset put PC back at 0, so the program restarts from the top
    await spi_ctrl(dut, CTRL_RUN)
    await ClockCycles(dut.clk, 5)
    assert dut.uo_out.value == 0x1F


@cocotb.test()
async def test_max_sck(dut):
    """Load, run and stream at the SCK limit (half period = 4 clk, SCK = clk/8)."""
    await reset(dut)
    program = [encode_pull(), encode_mov(MOV_ISR, MOV_SRC_OSR), encode_push(), jmp(0)]
    payload = [CMD_WRITE_IMEM]
    for word in program:
        payload += [word >> 8, word & 0xFF]
    await spi_transfer(dut, payload, half=4)
    await spi_transfer(dut, [CMD_CTRL | CTRL_RUN], half=4)
    await spi_transfer(dut, [CMD_WRITE_TX, 0xA5, 0x3C], half=4)
    await ClockCycles(dut.clk, 20)
    assert (await spi_transfer(dut, [CMD_READ_RX, 0, 0], half=4))[1:] == [0xA5, 0x3C]
