/*
 * Copyright (c) 2024 Ryan Neil Alumkal
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

module tt_um_ryanalumkal (
    input  wire [7:0] ui_in,    // Dedicated inputs
    output wire [7:0] uo_out,   // Dedicated outputs
    input  wire [7:0] uio_in,   // IOs: Input path
    output wire [7:0] uio_out,  // IOs: Output path
    output wire [7:0] uio_oe,   // IOs: Enable path (active high: 0=input, 1=output)
    input  wire       ena,      // always 1 when the design is powered, so you can ignore it
    input  wire       clk,      // clock
    input  wire       rst_n     // reset_n - low to reset
);

  reg [4:0] pc;
  reg [7:0] osr;
  reg [7:0] isr;
  reg [7:0] x;
  reg [7:0] y;

  reg [15:0] instruction_memory [0:31];  // 16-bit, 32-word instruction memory

  // Current instruction
  wire [15:0] instr_reg = instruction_memory[pc];

  // Common fields
  wire [2:0] opcode         = instr_reg[15:13];
  wire [4:0] delay_side_set = instr_reg[12:8];

  // Overlapping lower-byte fields (bits 7:0)
  wire [2:0] condition = instr_reg[7:5];   // JMP
  wire [2:0] src_high  = instr_reg[7:5];   // IN
  wire [2:0] dest      = instr_reg[7:5];   // OUT, MOV, SET

  wire [4:0] address   = instr_reg[4:0];   // JMP
  wire [4:0] index     = instr_reg[4:0];   // WAIT, IRQ
  wire [4:0] bit_count = instr_reg[4:0];   // IN, OUT
  wire [4:0] set_data  = instr_reg[4:0];   // SET

  // Instruction-specific flags
  wire       wait_pol = instr_reg[7];
  wire [1:0] wait_src = instr_reg[6:5];

  wire       push_pull_dir = instr_reg[7];  // 0 = PUSH, 1 = PULL
  wire       flag_6        = instr_reg[6];  // IfF (PUSH), IfE (PULL), Clr (IRQ)
  wire       flag_5        = instr_reg[5];  // Blk (PUSH/PULL), Wait (IRQ)

  wire [1:0] mov_op  = instr_reg[4:3];
  wire [2:0] mov_src = instr_reg[2:0];

  reg [7:0] pins_out;
  reg [7:0] pins_oe;

  assign uio_out = pins_out;
  assign uio_oe  = pins_oe;
  assign uo_out  = pins_out;  // optional mirror, or leave 0 for now

  // WAIT GPIO: stall until uio_in[index] == polarity
  wire wait_gpio = uio_in[index[2:0]];
  wire wait_cond = (wait_gpio == wait_pol);

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      pc       <= 0;
      osr      <= 0;
      isr      <= 0;
      x        <= 0;
      y        <= 0;
      pins_out <= 0;
      pins_oe  <= 0;
    end else begin
      // EXECUTE
      case (opcode)
        3'b000: begin  // JMP
          case (condition)
            3'b000: begin
              pc <= address;
            end
            3'b001: begin
              if (!x) pc <= address;
              else    pc <= pc + 1;
            end
            3'b010: begin
              if (x != 0) begin
                pc <= address;
                x  <= x - 1;
              end else begin
                pc <= pc + 1;
              end
            end
            3'b011: begin
              if (!y) pc <= address;
              else    pc <= pc + 1;
            end
            3'b100: begin
              if (y != 0) begin
                pc <= address;
                y  <= y - 1;
              end else begin
                pc <= pc + 1;
              end
            end
            3'b101: begin
              if (x != y) pc <= address;
              else        pc <= pc + 1;
            end
            default: pc <= pc + 1;
          endcase
        end

        3'b001: begin  // WAIT
          if (wait_cond)
            pc <= pc + 1;
        end

        3'b111: begin  // SET
          case (dest)
            3'b000: pins_out <= {3'b000, set_data};
            3'b001: x        <= {3'b000, set_data};
            3'b010: y        <= {3'b000, set_data};
            3'b100: pins_oe  <= {3'b000, set_data};
            default: ;
          endcase
          pc <= pc + 1;
        end

        default: pc <= pc + 1;
      endcase
    end
  end

  // List all unused inputs to prevent warnings
  wire _unused = &{ena, 1'b0};

endmodule
