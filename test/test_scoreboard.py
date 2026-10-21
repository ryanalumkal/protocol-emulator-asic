# SPDX-License-Identifier: Apache-2.0
"""Randomized and corner-case tests checked cycle-by-cycle against pio_model.

Seed and length can be controlled from the environment:
  COCOTB_RANDOM_SEED=1234        reproduce a failing run
  PIO_RANDOM_PROGRAMS=1000       number of random programs
  PIO_STREAM_CYCLES=50000        length of the random instruction stream
"""

import os
import random
from collections import Counter, deque
from dataclasses import replace

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge, ReadOnly, RisingEdge, Timer

from pio_model import (
    IN_SRCS,
    MOV_DESTS,
    MOV_OPS,
    MOV_SRCS,
    OP_IN,
    OP_JMP,
    OP_MOV,
    OP_OUT,
    OP_SET,
    OP_WAIT,
    OUT_DESTS,
    PioModel,
    PioState,
    coverage_goals,
    coverage_report,
    disasm,
    encode,
    encode_mov,
)

UIO_OE_HOST = 0b0111_0100
STATE_REGS = ("x", "y", "isr", "osr")


def sm(dut):
    return dut.user_project


def imem_depth(dut):
    return len(sm(dut).instruction_memory)


def _int(handle):
    try:
        return int(handle.value)
    except ValueError:
        return None  # X/Z


def dut_state(dut):
    s = sm(dut)
    return PioState(
        pc=_int(s.pc),
        x=_int(s.x),
        y=_int(s.y),
        isr=_int(s.isr),
        osr=_int(s.osr),
        pins_out=_int(dut.uo_out),
        pins_oe=_int(s.pins_oe),
        isr_count=_int(s.isr_count),
        osr_count=_int(s.osr_count),
    )


def interesting_byte(rng):
    return rng.choice([0x00, 0xFF, 0x80, 0x01, rng.getrandbits(8), rng.getrandbits(8)])


class Scoreboard:
    """Compares DUT and model after every edge; dumps a trace on mismatch."""

    def __init__(self, dut, model, label, history=12):
        self.dut = dut
        self.model = model
        self.label = label
        self.history = deque(maxlen=history)
        self.cycle = 0

    def check(self, before, instr, ui_in):
        expected = self.model.state
        got = dut_state(self.dut)
        self.history.append((self.cycle, before.pc, instr, ui_in, replace(expected), got))
        self.cycle += 1

        problems = [f"{name}: model={exp:#04x} dut={act if act is None else hex(act)}"
                    for name, exp, act in expected.diff(got)]
        if int(self.dut.uio_oe.value) != UIO_OE_HOST:
            problems.append(f"uio_oe={int(self.dut.uio_oe.value):#04x}")
        if int(self.dut.uio_out.value) & ~UIO_OE_HOST & 0xFF:
            problems.append(f"uio_out drives input pins: {int(self.dut.uio_out.value):#04x}")
        if problems:
            raise AssertionError(self.report(problems))

    def report(self, problems):
        lines = [
            f"[{self.label}] mismatch after cycle {self.cycle - 1} "
            f"(seed {cocotb.RANDOM_SEED}): " + "; ".join(problems),
            "Recent history (state shown is AFTER the edge):",
        ]
        for cyc, pc, instr, ui, exp, got in self.history:
            mark = "  <-- mismatch" if exp.diff(got) else ""
            lines.append(f"  cyc {cyc:5d} pc={pc:2d} {instr:04x} {disasm(instr):<22} ui={ui:02x}{mark}")
            lines.append(f"      model: {exp}")
            if exp.diff(got):
                lines.append(f"      dut:   {got}")
        return "\n".join(lines)


async def start_clock(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = 0
    dut.imem_we.value = 0


async def reset_and_load(dut, program, depth):
    """Hold reset, deposit IMEM directly (unwritten words are JMP-to-self)."""
    await FallingEdge(dut.clk)
    dut.rst_n.value = 0
    for addr in range(depth):
        word = program[addr] if addr < len(program) else (addr & 0x1F)
        sm(dut).instruction_memory[addr].value = word
    await ClockCycles(dut.clk, 2)


def deposit(dut, model, name, value):
    getattr(sm(dut), name).value = value
    setattr(model.state, name, value)
    # Keep shift counts coherent with side-loaded register values
    if name == "osr":
        count = 8 if value else 0
        sm(dut).osr_count.value = count
        model.state.osr_count = count
    elif name == "isr":
        count = 8 if value else 0
        sm(dut).isr_count.value = count
        model.state.isr_count = count


async def release_reset(dut, model, rng, randomize_state):
    await FallingEdge(dut.clk)
    dut.rst_n.value = 1
    model.reset()
    if randomize_state:
        for name in STATE_REGS:
            deposit(dut, model, name, interesting_byte(rng))


async def clock_and_check(dut, model, sb, ui_in):
    """Drive ui_in (caller is in the falling-edge half), clock once, compare."""
    dut.ui_in.value = ui_in
    before = replace(model.state)
    await RisingEdge(dut.clk)
    await ReadOnly()
    instr = model.step(ui_in)
    sb.check(before, instr, ui_in)


# ---------------------------------------------------------------------------
# Random programs: realistic loops built from implemented instructions
# ---------------------------------------------------------------------------


def random_instr(rng, n):
    op = rng.choices(
        [OP_JMP, OP_WAIT, OP_IN, OP_OUT, OP_MOV, OP_SET],
        weights=[3, 2, 3, 3, 3, 3],
    )[0]
    delay = rng.getrandbits(5)  # ignored by the RTL; randomized to prove that
    count = rng.choice([0, rng.randint(1, 8), rng.randint(1, 8), rng.randint(9, 31)])
    if op == OP_JMP:
        return encode(OP_JMP, rng.randrange(8), rng.randrange(n), delay)
    if op == OP_WAIT:
        return encode(OP_WAIT, rng.getrandbits(3), rng.getrandbits(5), delay)
    if op == OP_IN:
        return encode(OP_IN, rng.randrange(8), count, delay)
    if op == OP_OUT:
        dest = rng.choice([d for d in range(8) if OUT_DESTS[d] != "PC"])
        return encode(OP_OUT, dest, count, delay)
    if op == OP_MOV:
        dest = rng.choice([d for d in range(8) if MOV_DESTS[d] != "PC"])
        return encode_mov(dest, rng.randrange(8), rng.randrange(4), delay)
    return encode(OP_SET, rng.randrange(8), rng.getrandbits(5), delay)


def random_program(rng, depth):
    n = rng.randint(2, depth)
    body = [random_instr(rng, n) for _ in range(n - 1)]
    return body + [encode(OP_JMP, 0, rng.randrange(n))]


@cocotb.test()
async def test_random_programs(dut):
    """Constrained-random programs with random ui_in and periodic OSR refills."""
    await start_clock(dut)
    rng = random.Random(cocotb.RANDOM_SEED)
    depth = imem_depth(dut)
    n_programs = int(os.environ.get("PIO_RANDOM_PROGRAMS", 150))
    coverage = Counter()

    for i in range(n_programs):
        program = random_program(rng, depth)
        model = PioModel(imem_depth=depth)
        model.load(program)
        await reset_and_load(dut, program, depth)
        await release_reset(dut, model, rng, randomize_state=True)
        sb = Scoreboard(dut, model, f"program {i}")
        for cyc in range(64):
            if cyc:
                await FallingEdge(dut.clk)
                if rng.random() < 0.15:
                    deposit(dut, model, "osr", interesting_byte(rng))  # stand-in for PULL
            await clock_and_check(dut, model, sb, rng.getrandbits(8))
        coverage += model.coverage

    report, missed = coverage_report(
        coverage, coverage_goals(include_out_pc=False, include_mov_pc=False)
    )
    dut._log.info(report)
    assert not missed, f"coverage holes: {missed}"


# ---------------------------------------------------------------------------
# Random instruction stream: any 16-bit word at the current PC every cycle
# ---------------------------------------------------------------------------


def keeps_pc_in_imem(model, word):
    for ui_in in (0x00, 0xFF):
        trial = PioModel(imem_depth=model.imem_depth, state=replace(model.state), imem=list(model.imem))
        trial.imem[model.state.pc] = word
        trial.step(ui_in)
        if trial.state.pc >= model.imem_depth:
            return False
    return True


async def run_stream(dut, rng, words, cycles, label, refill_p=0.2):
    """Each cycle, write the next word at the current PC and execute it.

    Words that would take PC past IMEM are retried after a JMP elsewhere
    (up to 4 times) so directed sweeps don't silently lose entries.
    """
    depth = imem_depth(dut)
    model = PioModel(imem_depth=depth)
    await reset_and_load(dut, [], depth)
    await release_reset(dut, model, rng, randomize_state=True)
    sb = Scoreboard(dut, model, label)
    pending, retries = None, 0
    for cyc in range(cycles):
        if cyc:
            await FallingEdge(dut.clk)
            for name in STATE_REGS:
                if rng.random() < refill_p:
                    deposit(dut, model, name, interesting_byte(rng))
        if pending is None:
            pending = next(words, None)
            if pending is None:
                break
            retries = 0
        if keeps_pc_in_imem(model, pending):
            word, pending = pending, None
        else:
            word = encode(OP_JMP, 0, rng.randrange(depth // 2))
            retries += 1
            if retries > 4:
                pending = None
        sm(dut).instruction_memory[model.state.pc].value = word
        model.imem[model.state.pc] = word
        await clock_and_check(dut, model, sb, interesting_byte(rng))
    return model


def random_words(rng):
    while True:
        yield rng.getrandbits(16)


@cocotb.test()
async def test_random_instruction_stream(dut):
    """Fully random instruction words (all opcodes and fields) vs. the model."""
    await start_clock(dut)
    rng = random.Random(cocotb.RANDOM_SEED + 1)
    cycles = int(os.environ.get("PIO_STREAM_CYCLES", 20000))
    model = await run_stream(dut, rng, random_words(rng), cycles, "stream")

    report, missed = coverage_report(model.coverage, coverage_goals(include_unimplemented=True))
    dut._log.info(report)
    assert not missed, f"coverage holes: {missed}"


# ---------------------------------------------------------------------------
# Corner cases
# ---------------------------------------------------------------------------


@cocotb.test()
async def test_in_out_every_count(dut):
    """Every IN source and OUT destination with every bit count 0..31."""
    await start_clock(dut)
    rng = random.Random(cocotb.RANDOM_SEED + 2)
    sweep = [encode(OP_IN, src, n) for src in range(len(IN_SRCS)) for n in range(32)]
    sweep += [encode(OP_OUT, dest, n) for dest in range(len(OUT_DESTS)) if OUT_DESTS[dest] != "PC" for n in range(32)]
    model = await run_stream(dut, rng, iter(sweep), 4 * len(sweep), "in/out sweep", refill_p=1.0)

    executed = sum(model.coverage[("IN", s)] for s in IN_SRCS) + sum(model.coverage[("OUT", d)] for d in OUT_DESTS)
    assert executed == len(sweep), f"only {executed}/{len(sweep)} sweep instructions executed"


@cocotb.test()
async def test_mov_every_combo(dut):
    """Every MOV dest × src × op (excluding dest PC to keep PC in IMEM)."""
    await start_clock(dut)
    rng = random.Random(cocotb.RANDOM_SEED + 4)
    sweep = [
        encode_mov(dest, src, op)
        for dest in range(len(MOV_DESTS))
        if MOV_DESTS[dest] != "PC"
        for src in range(len(MOV_SRCS))
        for op in range(len(MOV_OPS))
    ]
    model = await run_stream(dut, rng, iter(sweep), 4 * len(sweep), "mov sweep", refill_p=1.0)

    executed = sum(model.coverage[("MOV", d)] for d in MOV_DESTS if d != "PC")
    assert executed == len(sweep), f"only {executed}/{len(sweep)} MOV sweep instructions executed"
    assert all(model.coverage[("MOV_SRC", s)] for s in MOV_SRCS)
    assert all(model.coverage[("MOV_OP", o or "NONE")] for o in MOV_OPS)


@cocotb.test()
async def test_async_reset_mid_program(dut):
    """rst_n asserted between clock edges clears state immediately; program restarts at 0."""
    await start_clock(dut)
    rng = random.Random(cocotb.RANDOM_SEED + 3)
    depth = imem_depth(dut)

    for i in range(20):
        program = random_program(rng, depth)
        model = PioModel(imem_depth=depth)
        model.load(program)
        await reset_and_load(dut, program, depth)
        await release_reset(dut, model, rng, randomize_state=True)
        sb = Scoreboard(dut, model, f"reset program {i}")
        for cyc in range(rng.randint(3, 30)):
            if cyc:
                await FallingEdge(dut.clk)
            await clock_and_check(dut, model, sb, rng.getrandbits(8))

        await Timer(rng.randint(1, 4), unit="ns")  # between edges, not on one
        dut.rst_n.value = 0
        await Timer(1, unit="ns")
        await ReadOnly()
        got = dut_state(dut)
        assert not got.diff(PioState()), f"async reset did not clear state: {got}"

        await release_reset(dut, model, rng, randomize_state=False)
        sb.history.clear()
        for cyc in range(30):
            if cyc:
                await FallingEdge(dut.clk)
            await clock_and_check(dut, model, sb, rng.getrandbits(8))
