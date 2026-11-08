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
The `uio` pins are an SPI host interface, laid out as a standard SPI Pmod. The host uses it to load the program,
write the TX FIFO, read the RX FIFO, and start or stop the state machine.

### SPI host interface

| Pin      | Direction | Signal |
|----------|-----------|--------|
| `uio[0]` | in  | `CS_N` |
| `uio[1]` | in  | `MOSI` |
| `uio[2]` | out | `MISO` |
| `uio[3]` | in  | `SCK` |
| `uio[4]` | out | `TX_FULL` |
| `uio[5]` | out | `RX_NOT_EMPTY` |
| `uio[6]` | out | `RUNNING` |

SPI mode 0 (CPOL=0, CPHA=0), MSB first, single device. SCK is sampled on the system clock, so it must be at most
`clk / 8` (about 6 MHz at 50 MHz; 5 MHz is a safe choice).

Each transaction is one `CS_N`-low period. The first byte is a command, `{op[3:0], arg[3:0]}`. While the command
shifts in, `MISO` returns the status byte.

| Command | Name | Following bytes | Effect |
|---------|------|-----------------|--------|
| `0x00` | STATUS | none | Clears the sticky `TX_OVF` and `RX_UDF` flags after reporting them |
| `0x1a` | WRITE_IMEM | `{hi, lo}` per word | Writes words at address `a`, `a+1`, ... (wraps at 16). Allowed while running |
| `0x20` | WRITE_TX | data bytes | Pushes each byte into the TX FIFO. A byte sent while the FIFO is full is dropped and sets `TX_OVF` |
| `0x30` | READ_RX | one dummy byte per byte read | Pops one byte from the RX FIFO per byte clocked out. Reads while empty return `0x00` and set `RX_UDF` |
| `0x4c` | CTRL | none | `c[0]`: running ← `c[0]`. `c[1]`: execute one instruction. `c[2]`: reset the state machine (PC, registers, pins; not the program). `c[3]`: flush both FIFOs |

Unknown commands are ignored until `CS_N` goes high. Every CTRL command writes the run bit, so `0x44` (reset)
also halts. Common CTRL bytes: `0x41` run, `0x40` halt, `0x42` single-step, `0x4C` halt + reset + flush.

Status byte, MSB first: `{RUNNING, TX_FULL, TX_EMPTY, RX_FULL, RX_EMPTY, TX_OVF, RX_UDF, 1}`. Bit 0 is always 1,
so reading `0x00` or `0xFF` means no device is responding. After reset the status is `0x29`.

If `CS_N` rises in the middle of a byte, that partial byte is discarded. In a READ_RX, a byte is popped from the
FIFO as soon as its first bit is clocked out, so aborting partway through a byte loses that byte.

## How to test

The design is checked in simulation with cocotb. `test/test_spi.py` and `test/test_gl.py` only use the top-level
pins, so they also run against the gate-level netlist. The other tests look at internal signals, and a Python
reference model (`test/pio_model.py`) is checked against the RTL with random programs.

```sh
cd test
make
```

On hardware:

1. Hold `rst_n` low with `CS_N` high, then release it. A STATUS command should return `0x29`.
2. Write the program with WRITE_IMEM, then send CTRL `0x41` to run it.
3. Watch `uo_out`, or feed data with WRITE_TX and read results with READ_RX. `TX_FULL`, `RX_NOT_EMPTY` and
   `RUNNING` on `uio[6:4]` report the status without an SPI transaction.

In the UART demo, `ui[3]` is the RX line and `uo[4]` is the TX line.

## External hardware

An SPI host, such as the RP2040 on the Tiny Tapeout demo board or any microcontroller connected to the `uio` Pmod
header.
