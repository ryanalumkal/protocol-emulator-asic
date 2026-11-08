<!---

This file is used to generate your project datasheet. Please fill in the information below and delete any unused
sections.

You can also include images in this folder and reference them in the markdown. Each image must be less than
512 kb in size, and the combined size of all images must be less than 1 MB.
-->

## How it works

This project is a small programmable I/O state machine inspired by the RP2040 PIO block. A host loads a short
program into it, and the program then drives the GPIO pins to emulate a serial protocol such as UART.

The state machine has:

- A 16-word × 16-bit instruction memory. The program survives `rst_n`.
- 8-bit `X` and `Y` scratch registers.
- An 8-bit input shift register (ISR) and output shift register (OSR).
- A 4-entry TX FIFO (host → state machine, read with `PULL`) and a 4-entry RX FIFO (state machine → host, written with `PUSH`).

Instructions are 16 bits wide: `[15:13]` opcode, `[12:8]` delay/side-set (reserved), `[7:0]` arguments.

| Opcode | Instruction | Summary |
|--------|-------------|---------|
| `000`  | `JMP`       | Jump always, or on `!X`, `X--`, `!Y`, `Y--`, `X != Y` |
| `001`  | `WAIT`      | Stall until `ui_in[index]` matches the polarity |
| `010`  | `IN`        | Shift 1–8 bits from pins / X / Y / NULL / ISR / OSR into the ISR |
| `011`  | `OUT`       | Shift 1–8 bits out of the OSR to pins / X / Y / NULL / PINDIRS / PC / ISR |
| `100`  | `PUSH`/`PULL` | Move the ISR into the RX FIFO, or the TX FIFO into the OSR (with IfFull/IfEmpty and Block flags) |
| `101`  | `MOV`       | Copy a source to a destination, optionally inverted or bit-reversed |
| `110`  | `IRQ`       | Reserved (no-op) |
| `111`  | `SET`       | Write a 5-bit immediate to pins / X / Y / PINDIRS |

`ui_in` is the state machine's input pins (`WAIT`, `IN`, `MOV`). `uo_out` is its output pins (`SET`, `OUT`, `MOV`).
The `uio` pins are reserved for an SPI host interface, laid out as a standard SPI Pmod. The host uses it to load
the program, write the TX FIFO, read the RX FIFO, and start or stop the state machine.

## How to test

The SPI host interface is still being implemented. For now the design is checked in simulation with cocotb. The
testbench loads programs and drives the FIFOs directly. A Python reference model (`test/pio_model.py`) is checked
against the RTL with random programs.

```sh
cd test
make
```

Once the SPI interface is in place, the steps will be:

1. Hold `rst_n` low, then release it.
2. Over SPI, write the program into instruction memory and set the run bit.
3. Watch `uo_out`, or feed data through the TX FIFO and read results from the RX FIFO. `TX_FULL`, `RX_NOT_EMPTY`
   and `RUNNING` on `uio[6:4]` report the status.

In the UART demo, `ui[3]` is the RX line and `uo[4]` is the TX line.

## External hardware

An SPI host, such as the RP2040 on the Tiny Tapeout demo board or any microcontroller connected to the `uio` Pmod
header.
