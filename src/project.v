/*
 * Copyright (c) 2026 Ryan Neil Alumkal
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

  // IN/OUT shift amount: 0 or anything > 8 means a full 8 bits
  wire [3:0] in_count = (bit_count == 0 || bit_count > 8) ? 4'd8 : bit_count[3:0];
  wire [7:0] in_mask  = 8'hFF >> (4'd8 - in_count);

  reg [7:0] pins_out;
  reg [7:0] pins_oe;

  // ui_in  = state machine input pins (WAIT, IN)
  // uo_out = state machine output pins (SET, OUT); SET only reaches uo_out[4:0]
  //
  // uio = host interface, laid out as a standard SPI Pmod:
  //   uio[0] in  cs_n         uio[4] out tx_full
  //   uio[1] in  mosi         uio[5] out rx_not_empty
  //   uio[2] out miso         uio[6] out running
  //   uio[3] in  sck          uio[7] -   spare
  wire spi_cs_n = uio_in[0];
  wire spi_mosi = uio_in[1];
  wire spi_sck  = uio_in[3];

  // TODO: drive these from the SPI host interface, FIFOs and run control
  wire spi_miso     = 1'b0;
  wire tx_full      = 1'b0;
  wire rx_not_empty = 1'b0;
  wire running      = 1'b0;

  assign uo_out  = pins_out;
  assign uio_out = {1'b0, running, rx_not_empty, tx_full, 1'b0, spi_miso, 2'b00};
  assign uio_oe  = 8'b0111_0100;

  // WAIT GPIO: stall until ui_in[index] == polarity
  wire wait_gpio = ui_in[index[2:0]];
  wire wait_cond = (wait_gpio == wait_pol);

  // fifo #(.DATA_WIDTH(8), ADDR_WIDTH(2)) rx_fifo (.clk (clk), .rst_n(rst_n), .data_in(), data_out);
  // fifo #(.DATA_WIDTH(8), ADDR_WIDTH(2)) tx_fifo (.clk (clk), .rst_n(rst_n), .data_in(), data_out);

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

        3'b010: begin // IN
          case(src_high) 
            3'b000: begin //PINS(ui_in)
              isr <= (isr << in_count) | (ui_in & in_mask);
            end
            3'b001: begin //X
              isr <= (isr << in_count) | (x & in_mask);
            end
            3'b010: begin //Y
              isr <= (isr << in_count) | (y & in_mask);
            end
            3'b011: begin //NULL (zeros)
              isr <= (isr << in_count) | (0 & in_mask);
            end
            3'b100: begin //reserved
            end
            3'b101: begin //reserved
            end
            3'b110: begin //ISR
              isr <= (isr << in_count) | (isr & in_mask);
            end
            3'b111: begin //OSR
              isr <= (isr << in_count) | (osr & in_mask);
            end
          endcase
          pc <= pc + 1;
        end

        3'b011: begin // OUT
          case(dest) 
              3'b000: begin //PINS(pin_out)
                pins_out  <= (pins_out & ~in_mask) | (osr & in_mask);
              end
              3'b001: begin //X
                x         <= osr & in_mask;
              end
              3'b010: begin //Y
                y         <= osr & in_mask;
              end
              3'b011: begin //NULL(discard)
              end
              3'b100: begin //PINDIRS(pins_oe)
                pins_oe  <= (pins_oe & ~in_mask) | (osr & in_mask);
              end
              3'b101: begin //PC (computed jump)
                pc       <= osr[4:0] & in_mask[4:0]; 
              end
              3'b110: begin //ISR
                isr      <= osr & in_mask; 
              end
              3'b111: begin //EXEC (skip)
              end
            endcase
            if (dest != 3'b101) pc <= pc + 1;
            osr <= osr >> in_count;
        end

        3'b100: begin //PUSH

        end

        3'b101: begin //MOV

        end

        3'b110: begin //IRQ

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
  wire _unused = &{ena, spi_cs_n, spi_mosi, spi_sck, uio_in[7:4], uio_in[2], 1'b0};

endmodule


// module fifo #( 
//   parameter DATA_WIDTH = 8;
//   parameter ADDR_WIDTH = 2;
// ) (
//   input wire                  clk;
//   input wire                  rst_n;
//   input wire                  wr_en;
//   input wire                  rd_en;
//   input wire [DATA_WIDTH-1:0] data_in;
//   output reg [DATA_WIDTH-1:0] data_out;
//   output wire                 full;
//   output wire                 empty;
// );

//   localparam DEPTH = 1 << ADDR_WIDTH;

//   reg pointer;

//   reg [DATA_WIDTH-1:0] mem [0:DEPTH-1];

//   full = (pointer == DEPTH);
//   empty = (pointer == 0);

//   always @ (posedge clk or negedge rst_n) begin
//     if (!rst_n) begin
//       mem <= 0;
//       pointer <= 0;
//     end else begin
//       if (!full && wr_en) begin
  
//       end
//     end


//   end


// endmodule
