# SPDX-FileCopyrightText: © 2024 Tiny Tapeout
# SPDX-License-Identifier: Apache-2.0

# Port-level tests that only touch the top-level pins, so they run against
# both RTL and the gate-level netlist (GATES=yes).

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles

# uio is the SPI host interface: miso, tx_full, rx_not_empty, running are outputs
UIO_OE_HOST = 0b0111_0100


@cocotb.test()
async def test_reset_pins(dut):
    """After reset the SM is halted: outputs low, host pins in their fixed directions."""
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = 0x01  # SPI cs_n high (deselected)
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 5)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 5)

    assert dut.uo_out.value == 0
    assert dut.uio_oe.value == UIO_OE_HOST
    # running, rx_not_empty, tx_full all low; miso tied low
    assert dut.uio_out.value == 0

    # Toggling the state machine inputs must not change anything while halted
    dut.ui_in.value = 0xFF
    await ClockCycles(dut.clk, 5)
    assert dut.uo_out.value == 0
    assert dut.uio_out.value == 0
