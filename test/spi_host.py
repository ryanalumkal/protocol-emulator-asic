# SPDX-FileCopyrightText: © 2026 Ryan Neil Alumkal
# SPDX-License-Identifier: Apache-2.0
"""Bit-banged SPI master (mode 0, MSB first) on the uio Pmod pins.

Only touches top-level pins, so it works against RTL and the gate-level netlist.
All pin changes happen on the falling edge of clk to stay clear of the DUT's
sampling edge.
"""

from cocotb.triggers import ClockCycles, FallingEdge

# uio pin positions
PIN_CS_N = 0
PIN_MOSI = 1
PIN_MISO = 2
PIN_SCK = 3

# Command bytes are {op[3:0], arg[3:0]}
CMD_STATUS = 0x00
CMD_WRITE_IMEM = 0x10
CMD_WRITE_TX = 0x20
CMD_READ_RX = 0x30
CMD_CTRL = 0x40

CTRL_RUN = 1 << 0
CTRL_STEP = 1 << 1
CTRL_SM_RST = 1 << 2
CTRL_FIFO_CLR = 1 << 3

# Status byte: {running, tx_full, tx_empty, rx_full, rx_empty, tx_ovf, rx_udf, 1}
ST_RUNNING = 1 << 7
ST_TX_FULL = 1 << 6
ST_TX_EMPTY = 1 << 5
ST_RX_FULL = 1 << 4
ST_RX_EMPTY = 1 << 3
ST_TX_OVF = 1 << 2
ST_RX_UDF = 1 << 1
ST_MARKER = 1 << 0

# SCK half-period in clk cycles. The RTL oversamples SCK, so it needs >= 4 (SCK <= clk/8).
HALF = 5


def _drive(dut, cs_n, mosi=0, sck=0):
    dut.uio_in.value = (cs_n << PIN_CS_N) | (mosi << PIN_MOSI) | (sck << PIN_SCK)


def spi_idle(dut):
    """Deselect the device (call before releasing reset)."""
    _drive(dut, cs_n=1)


async def _wait(dut, cycles):
    await ClockCycles(dut.clk, cycles, rising=False)


async def spi_transfer(dut, tx_bytes, half=HALF, abort_after_bits=None):
    """One cs_n-low transaction. Returns the bytes read on MISO.

    abort_after_bits raises cs_n after that many SCK pulses (for abort tests).
    """
    mosi_bits = [(b >> i) & 1 for b in tx_bytes for i in range(7, -1, -1)]
    if abort_after_bits is not None:
        mosi_bits = mosi_bits[:abort_after_bits]

    await FallingEdge(dut.clk)
    _drive(dut, cs_n=0)
    await _wait(dut, half)

    miso_bits = []
    for mosi in mosi_bits:
        _drive(dut, cs_n=0, mosi=mosi)
        await _wait(dut, half)
        # Mode 0: master samples MISO on the rising edge
        miso_bits.append((int(dut.uio_out.value) >> PIN_MISO) & 1)
        _drive(dut, cs_n=0, mosi=mosi, sck=1)
        await _wait(dut, half)
        _drive(dut, cs_n=0, mosi=mosi)

    await _wait(dut, half)
    _drive(dut, cs_n=1)
    # Let cs_n high reach the synchronizer before the next transaction
    await _wait(dut, half)

    rx_bytes = []
    for n in range(len(miso_bits) // 8):
        byte = 0
        for bit in miso_bits[8 * n : 8 * n + 8]:
            byte = (byte << 1) | bit
        rx_bytes.append(byte)
    return rx_bytes


async def spi_status(dut):
    return (await spi_transfer(dut, [CMD_STATUS]))[0]


async def spi_ctrl(dut, bits):
    """Send CTRL; returns the status byte seen while sending it."""
    return (await spi_transfer(dut, [CMD_CTRL | (bits & 0xF)]))[0]


async def spi_write_imem(dut, addr, words):
    """Burst-write 16-bit words starting at addr (address wraps at 16)."""
    payload = [CMD_WRITE_IMEM | (addr & 0xF)]
    for word in words:
        payload += [(word >> 8) & 0xFF, word & 0xFF]
    return (await spi_transfer(dut, payload))[0]


async def spi_write_tx(dut, data):
    """Push bytes into the TX FIFO; returns the status byte."""
    return (await spi_transfer(dut, [CMD_WRITE_TX] + [d & 0xFF for d in data]))[0]


async def spi_read_rx(dut, n):
    """Pop n bytes from the RX FIFO (0x00 for each read while empty)."""
    return (await spi_transfer(dut, [CMD_READ_RX] + [0x00] * n))[1:]
