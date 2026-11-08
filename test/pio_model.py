# SPDX-License-Identifier: Apache-2.0
"""Cycle-accurate Python reference model of the PIO state machine in src/project.v.

One instruction executes per clock edge. The model mirrors the RTL's current
semantics (no delay/side-set; 8-bit ISR/OSR; 4-deep TX/RX FIFOs).
Deviations from the RP2040 PIO are listed in DEVIATIONS so they stay visible.
"""

from collections import Counter, deque
from dataclasses import dataclass, field, fields

OP_JMP, OP_WAIT, OP_IN, OP_OUT, OP_PUSH, OP_MOV, OP_IRQ, OP_SET = range(8)
OP_NAMES = ["JMP", "WAIT", "IN", "OUT", "PUSH", "MOV", "IRQ", "SET"]
UNIMPLEMENTED_OPS = (OP_IRQ,)

JMP_CONDS = ["", "!X", "X--", "!Y", "Y--", "X!=Y", "PIN", "!OSRE"]
IN_SRCS = ["PINS", "X", "Y", "NULL", "RSVD4", "RSVD5", "ISR", "OSR"]
OUT_DESTS = ["PINS", "X", "Y", "NULL", "PINDIRS", "PC", "ISR", "EXEC"]
SET_DESTS = ["PINS", "X", "Y", "RSVD3", "PINDIRS", "RSVD5", "RSVD6", "RSVD7"]
MOV_SRCS = ["PINS", "X", "Y", "NULL", "RSVD4", "STATUS", "ISR", "OSR"]
MOV_DESTS = ["PINS", "X", "Y", "RSVD3", "EXEC", "PC", "ISR", "OSR"]
MOV_OPS = ["", "~", "::", "RSVD"]

FIFO_DEPTH = 4

DEVIATIONS = [
    "JMP X--/Y-- does not decrement when the register is already 0 (RP2040 wraps to all-ones)",
    "JMP PIN and JMP !OSRE fall through unconditionally",
    "WAIT ignores the source field and always waits on ui_in[index[2:0]]",
    "Delay/side-set field is ignored",
    "ISR/OSR are 8-bit (RP2040 uses 32-bit)",
    "MOV STATUS source is hardwired to 0",
    "MOV EXEC destination is a no-op (still advances PC)",
    "IRQ does not advance PC (the state machine hangs on it)",
]


class ModelError(Exception):
    """The model reached a state the RTL can't represent (e.g. PC past IMEM)."""


def encode(op, sel=0, low=0, delay=0):
    """Generic encoder: [15:13] op, [12:8] delay, [7:5] sel, [4:0] low."""
    return ((op & 7) << 13) | ((delay & 0x1F) << 8) | ((sel & 7) << 5) | (low & 0x1F)


def encode_push(iff=0, blk=1, delay=0):
    return encode(OP_PUSH, ((iff & 1) << 1) | (blk & 1), 0, delay)


def encode_pull(ife=0, blk=1, delay=0):
    return (OP_PUSH << 13) | ((delay & 0x1F) << 8) | (1 << 7) | ((ife & 1) << 6) | ((blk & 1) << 5)


def encode_mov(dest, src, op=0, delay=0):
    """MOV: [7:5] dest, [4:3] op, [2:0] src."""
    return (
        (OP_MOV << 13)
        | ((delay & 0x1F) << 8)
        | ((dest & 7) << 5)
        | ((op & 3) << 3)
        | (src & 7)
    )


def shift_count(instr):
    n = instr & 0x1F
    return 8 if n == 0 or n > 8 else n


def bitrev8(value):
    value &= 0xFF
    return int(f"{value:08b}"[::-1], 2)


def disasm(instr):
    op = instr >> 13
    sel = (instr >> 5) & 7
    low = instr & 0x1F
    delay = (instr >> 8) & 0x1F
    suffix = f" [{delay}]" if delay else ""
    if op == OP_JMP:
        cond = JMP_CONDS[sel]
        return f"JMP {cond + ', ' if cond else ''}{low}{suffix}"
    if op == OP_WAIT:
        return f"WAIT {(instr >> 7) & 1} GPIO {low}{suffix}"
    if op == OP_IN:
        return f"IN {IN_SRCS[sel]}, {low}{suffix}"
    if op == OP_OUT:
        return f"OUT {OUT_DESTS[sel]}, {low}{suffix}"
    if op == OP_PUSH:
        dir_ = "PULL" if (instr >> 7) & 1 else "PUSH"
        iff = (instr >> 6) & 1
        blk = (instr >> 5) & 1
        flags = []
        if iff:
            flags.append("IfE" if dir_ == "PULL" else "IfF")
        if blk:
            flags.append("Blk")
        extra = f" {' '.join(flags)}" if flags else ""
        return f"{dir_}{extra}{suffix}"
    if op == OP_MOV:
        mov_op = (instr >> 3) & 3
        mov_src = instr & 7
        op_name = MOV_OPS[mov_op]
        src_name = f"{op_name}{MOV_SRCS[mov_src]}" if op_name and op_name != "RSVD" else MOV_SRCS[mov_src]
        if op_name == "RSVD":
            src_name = f"?{MOV_SRCS[mov_src]}"
        return f"MOV {MOV_DESTS[sel]}, {src_name}{suffix}"
    if op == OP_SET:
        return f"SET {SET_DESTS[sel]}, {low}{suffix}"
    return f"{OP_NAMES[op]} 0x{instr & 0xFF:02X}{suffix}"


@dataclass
class PioState:
    pc: int = 0
    x: int = 0
    y: int = 0
    isr: int = 0
    osr: int = 0
    pins_out: int = 0
    pins_oe: int = 0
    isr_count: int = 0
    osr_count: int = 0

    def diff(self, other):
        return [
            (f.name, getattr(self, f.name), getattr(other, f.name))
            for f in fields(self)
            if getattr(self, f.name) != getattr(other, f.name)
        ]

    def __str__(self):
        return " ".join(f"{f.name}={getattr(self, f.name):#04x}" for f in fields(self))


@dataclass
class PioModel:
    imem_depth: int = 16
    state: PioState = field(default_factory=PioState)
    imem: list = field(default_factory=list)
    coverage: Counter = field(default_factory=Counter)
    tx_fifo: deque = field(default_factory=lambda: deque(maxlen=FIFO_DEPTH))
    rx_fifo: deque = field(default_factory=lambda: deque(maxlen=FIFO_DEPTH))

    def __post_init__(self):
        if not self.imem:
            self.imem = [i & 0x1F for i in range(self.imem_depth)]  # JMP i (halt)
        # dataclass default_factory shares care — ensure fresh deques
        if self.tx_fifo.maxlen != FIFO_DEPTH:
            self.tx_fifo = deque(maxlen=FIFO_DEPTH)
        if self.rx_fifo.maxlen != FIFO_DEPTH:
            self.rx_fifo = deque(maxlen=FIFO_DEPTH)

    def reset(self):
        self.state = PioState()
        self.tx_fifo.clear()
        self.rx_fifo.clear()

    def load(self, program):
        for addr, word in enumerate(program):
            self.imem[addr] = word

    def tx_push(self, data):
        """Host write into TX FIFO (ignored when full)."""
        if len(self.tx_fifo) < FIFO_DEPTH:
            self.tx_fifo.append(data & 0xFF)

    def rx_pop(self):
        """Host read from RX FIFO; returns None if empty."""
        if not self.rx_fifo:
            return None
        return self.rx_fifo.popleft()

    @property
    def tx_empty(self):
        return len(self.tx_fifo) == 0

    @property
    def rx_full(self):
        return len(self.rx_fifo) >= FIFO_DEPTH

    def current_instr(self):
        if self.state.pc >= self.imem_depth:
            raise ModelError(f"PC {self.state.pc} is past the {self.imem_depth}-word IMEM")
        return self.imem[self.state.pc]

    def step(self, ui_in):
        """Execute one clock edge with `ui_in` sampled at that edge."""
        s = self.state
        instr = self.current_instr()
        op = instr >> 13
        sel = (instr >> 5) & 7
        low = instr & 0x1F
        count = shift_count(instr)
        mask = 0xFF >> (8 - count)
        pc_mask = self.imem_depth - 1  # RTL PC is just wide enough for IMEM, so it wraps
        next_pc = (s.pc + 1) & pc_mask

        if op == OP_JMP:
            if sel == 0:
                taken = True
            elif sel == 1:
                taken = s.x == 0
            elif sel == 2:
                taken = s.x != 0
                if taken:
                    s.x = (s.x - 1) & 0xFF
            elif sel == 3:
                taken = s.y == 0
            elif sel == 4:
                taken = s.y != 0
                if taken:
                    s.y = (s.y - 1) & 0xFF
            elif sel == 5:
                taken = s.x != s.y
            else:
                taken = False
            self.coverage[("JMP", JMP_CONDS[sel] or "ALWAYS", "taken" if taken else "not_taken")] += 1
            s.pc = (low & pc_mask) if taken else next_pc

        elif op == OP_WAIT:
            pol = (instr >> 7) & 1
            done = ((ui_in >> (low & 7)) & 1) == pol
            self.coverage[("WAIT", f"pol{pol}", "release" if done else "stall")] += 1
            if done:
                s.pc = next_pc

        elif op == OP_IN:
            src = {0: ui_in, 1: s.x, 2: s.y, 3: 0, 6: s.isr, 7: s.osr}.get(sel)
            if src is not None:
                s.isr = ((s.isr << count) | (src & mask)) & 0xFF
                s.isr_count = min(8, s.isr_count + count)
            self.coverage[("IN", IN_SRCS[sel])] += 1
            self.coverage[("IN_COUNT", count)] += 1
            s.pc = next_pc

        elif op == OP_OUT:
            data = s.osr & mask
            if sel == 0:
                s.pins_out = (s.pins_out & ~mask & 0xFF) | data
            elif sel == 1:
                s.x = data
            elif sel == 2:
                s.y = data
            elif sel == 4:
                s.pins_oe = (s.pins_oe & ~mask & 0xFF) | data
            elif sel == 6:
                s.isr = data
            s.pc = data & pc_mask if sel == 5 else next_pc
            s.osr = s.osr >> count
            s.osr_count = max(0, s.osr_count - count)
            self.coverage[("OUT", OUT_DESTS[sel])] += 1
            self.coverage[("OUT_COUNT", count)] += 1

        elif op == OP_PUSH:
            is_pull = (instr >> 7) & 1
            flag_6 = (instr >> 6) & 1
            blk = (instr >> 5) & 1
            if is_pull:
                if flag_6 and s.osr_count != 0:
                    s.pc = next_pc
                    self.coverage[("PULL", "IfE_skip")] += 1
                elif self.tx_empty:
                    if not blk:
                        s.pc = next_pc
                        self.coverage[("PULL", "empty_skip")] += 1
                    else:
                        self.coverage[("PULL", "empty_stall")] += 1
                else:
                    s.osr = self.tx_fifo.popleft()
                    s.osr_count = 8
                    s.pc = next_pc
                    self.coverage[("PULL", "ok")] += 1
            else:
                if flag_6 and s.isr_count != 8:
                    s.pc = next_pc
                    self.coverage[("PUSH", "IfF_skip")] += 1
                elif self.rx_full:
                    if not blk:
                        s.pc = next_pc
                        self.coverage[("PUSH", "full_skip")] += 1
                    else:
                        self.coverage[("PUSH", "full_stall")] += 1
                else:
                    self.rx_fifo.append(s.isr & 0xFF)
                    s.isr = 0
                    s.isr_count = 0
                    s.pc = next_pc
                    self.coverage[("PUSH", "ok")] += 1

        elif op == OP_MOV:
            mov_op = (instr >> 3) & 3
            mov_src = instr & 7
            src_val = {
                0: ui_in,
                1: s.x,
                2: s.y,
                3: 0,
                6: s.isr,
                7: s.osr,
            }.get(mov_src, 0) & 0xFF
            if mov_op == 1:
                result = (~src_val) & 0xFF
            elif mov_op == 2:
                result = bitrev8(src_val)
            else:
                result = src_val  # none or reserved op
            if sel == 0:
                s.pins_out = result
            elif sel == 1:
                s.x = result
            elif sel == 2:
                s.y = result
            elif sel == 6:
                s.isr = result
            elif sel == 7:
                s.osr = result
            s.pc = (result & pc_mask) if sel == 5 else next_pc
            self.coverage[("MOV", MOV_DESTS[sel])] += 1
            self.coverage[("MOV_SRC", MOV_SRCS[mov_src])] += 1
            self.coverage[("MOV_OP", MOV_OPS[mov_op] or "NONE")] += 1

        elif op == OP_SET:
            if sel == 0:
                s.pins_out = low
            elif sel == 1:
                s.x = low
            elif sel == 2:
                s.y = low
            elif sel == 4:
                s.pins_oe = low
            self.coverage[("SET", SET_DESTS[sel])] += 1
            s.pc = next_pc

        else:
            self.coverage[("UNIMPL", OP_NAMES[op])] += 1

        return instr


def coverage_goals(include_unimplemented=False, include_out_pc=True, include_mov_pc=True):
    goals = set()
    for cond in ["ALWAYS"] + JMP_CONDS[1:]:
        outcomes = ["taken"] if cond == "ALWAYS" else ["not_taken"] if cond in ("PIN", "!OSRE") else ["taken", "not_taken"]
        goals |= {("JMP", cond, o) for o in outcomes}
    goals |= {("WAIT", f"pol{p}", o) for p in (0, 1) for o in ("stall", "release")}
    goals |= {("IN", s) for s in IN_SRCS}
    goals |= {("OUT", d) for d in OUT_DESTS if include_out_pc or d != "PC"}
    goals |= {("SET", d) for d in SET_DESTS}
    goals |= {("IN_COUNT", n) for n in range(1, 9)}
    goals |= {("OUT_COUNT", n) for n in range(1, 9)}
    mov_dests = [d for d in MOV_DESTS if include_mov_pc or d != "PC"]
    goals |= {("MOV", d) for d in mov_dests}
    goals |= {("MOV_SRC", s) for s in MOV_SRCS}
    goals |= {("MOV_OP", o or "NONE") for o in MOV_OPS}
    if include_unimplemented:
        goals |= {("UNIMPL", OP_NAMES[op]) for op in UNIMPLEMENTED_OPS}
    return goals


def coverage_report(coverage, goals):
    missed = sorted(goals - set(coverage), key=str)
    hit = len(goals) - len(missed)
    lines = [f"Functional coverage: {hit}/{len(goals)} bins ({100 * hit / len(goals):.1f}%)"]
    for key in sorted(goals, key=str):
        lines.append(f"  {'/'.join(str(k) for k in key):<28} {coverage.get(key, 0)}")
    return "\n".join(lines), missed
