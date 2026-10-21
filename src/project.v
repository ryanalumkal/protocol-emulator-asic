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
  reg [3:0] osr_count;
  wire      osr_empty = (osr_count == 0);
  reg [3:0] isr_count;
  wire      isr_full  = (isr_count == 4'd8);

  reg [15:0] instruction_memory [0:15];  // 16-bit, 16-word instruction memory

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

  // TX FIFO: host (SPI / TB) writes, SM (PULL) reads into OSR
  // RX FIFO: SM (PUSH) writes from ISR, host (SPI / TB) reads
  // Host strobes are regs the TB (later SPI) pulses for one cycle.
  reg  [7:0] host_tx_wdata;
  reg        host_tx_wr_en;
  reg        host_rx_rd_en;

  wire [7:0] tx_rdata;
  wire       tx_full;
  wire       tx_empty;
  wire [7:0] rx_rdata;
  wire       rx_full;
  wire       rx_empty;
  wire       rx_not_empty = !rx_empty;

  // SM FIFO side-effects are combinational so the FIFO updates on the same edge
  wire pull_do = (opcode == 3'b100) && push_pull_dir
                 && !(flag_6 && !osr_empty) && !tx_empty;
  wire push_do = (opcode == 3'b100) && !push_pull_dir
                 && !(flag_6 && !isr_full) && !rx_full;

  wire       tx_wr_en = host_tx_wr_en;
  wire [7:0] tx_wdata = host_tx_wdata;
  wire       tx_rd_en = pull_do;
  wire       rx_wr_en = push_do;
  wire [7:0] rx_wdata = isr;
  wire       rx_rd_en = host_rx_rd_en;

  fifo #(.DATA_WIDTH(8), .ADDR_WIDTH(2)) tx_fifo (
    .clk     (clk),
    .rst_n   (rst_n),
    .wr_en   (tx_wr_en),
    .rd_en   (tx_rd_en),
    .data_in (tx_wdata),
    .data_out(tx_rdata),
    .full    (tx_full),
    .empty   (tx_empty)
  );

  fifo #(.DATA_WIDTH(8), .ADDR_WIDTH(2)) rx_fifo (
    .clk     (clk),
    .rst_n   (rst_n),
    .wr_en   (rx_wr_en),
    .rd_en   (rx_rd_en),
    .data_in (rx_wdata),
    .data_out(rx_rdata),
    .full    (rx_full),
    .empty   (rx_empty)
  );

  assign uo_out  = pins_out;
  assign uio_out = {1'b0, running, rx_not_empty, tx_full, 1'b0, spi_miso, 2'b00};
  assign uio_oe  = 8'b0111_0100;

  // WAIT GPIO: stall until ui_in[index] == polarity
  wire wait_gpio = ui_in[index[2:0]];
  wire wait_cond = (wait_gpio == wait_pol);

  //MOV INSTRUCTION
  wire [7:0] mov_src_val =
    (mov_src == 3'b000) ? ui_in :
    (mov_src == 3'b001) ? x     :
    (mov_src == 3'b010) ? y     :
    (mov_src == 3'b011) ? 8'h00 :
    (mov_src == 3'b110) ? isr   :
    (mov_src == 3'b111) ? osr   : 8'h00;  // reserved + STATUS → 0

  wire [7:0] mov_bitrev = {mov_src_val[0], mov_src_val[1], mov_src_val[2], mov_src_val[3],
                          mov_src_val[4], mov_src_val[5], mov_src_val[6], mov_src_val[7]};

  wire [7:0] mov_result =
      (mov_op == 2'b01) ? ~mov_src_val :
      (mov_op == 2'b10) ? mov_bitrev   :
                          mov_src_val;

  reg running = 1'b0;

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
  assign uio_out[2] = spi_miso;
  wire spi_sck  = uio_in[3];
  assign uio_out[4] = tx_full;
  assign uio_out[5] = rx_not_empty;
  assign uio_out[6] = running;

  // SPI Interface
  
  always @(posedge clk) begin

  end

  // MAIN FSM
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      pc        <= 0;
      osr       <= 0;
      isr       <= 0;
      x         <= 0;
      y         <= 0;
      pins_out  <= 0;
      pins_oe   <= 0;
      osr_count     <= 0;
      isr_count     <= 0;
      host_tx_wr_en <= 0;
      host_tx_wdata <= 0;
      host_rx_rd_en <= 0;
      running <= 0;
    end else begin
      // Host strobes are one-shot; TB/SPI must re-assert each cycle
      host_tx_wr_en <= 1'b0;
      host_rx_rd_en <= 1'b0;
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
          // reserved sources do not change the shift count
          // widen the sum so 8+8 does not wrap in 4 bits
          if (src_high != 3'b100 && src_high != 3'b101) begin
            if ({1'b0, isr_count} + in_count >= 5'd8)
              isr_count <= 4'd8;
            else
              isr_count <= isr_count + in_count;
          end
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
            // track OSR fill so PULL IfE works
            osr_count <= (osr_count > in_count) ? (osr_count - in_count) : 4'd0;
        end

        3'b100: begin // PUSH/PULL
          if (push_pull_dir) begin // PULL
            if (flag_6 && !osr_empty) begin
              pc <= pc + 1;
            end else if (tx_empty) begin
              if (!flag_5) pc <= pc + 1;
            end else begin
              osr       <= tx_rdata;
              osr_count <= 4'd8;
              pc        <= pc + 1;
            end
          end else begin // PUSH
            if (flag_6 && !isr_full) begin
              pc <= pc + 1;
            end else if (rx_full) begin
              if (!flag_5) pc <= pc + 1;
            end else begin
              isr       <= 8'h00;
              isr_count <= 4'd0;
              pc        <= pc + 1;
            end
          end
        end

        3'b101: begin //MOV
          case(dest) 
            3'b000: begin //PINS(pin_out)
              pins_out <= mov_result;
            end
            3'b001: begin //X
              x <= mov_result;
            end
            3'b010: begin //Y
              y <= mov_result;
            end
            3'b011: begin //NULL(discard)
            end
            3'b100: begin //exec
            end
            3'b101: begin //PC (computed jump)
              pc <= mov_result[4:0];
            end
            3'b110: begin //ISR
              isr <= mov_result;
            end
            3'b111: begin //osr
              osr <= mov_result;
            end
            default ;
          endcase
          if (dest != 3'b101) pc <= pc + 1;
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

`ifdef FORMAL
  `include "pio_props.vh"
`endif

endmodule


module fifo #( 
  parameter DATA_WIDTH = 8,
  parameter ADDR_WIDTH = 2
) (
  input wire                  clk,
  input wire                  rst_n,
  input wire                  wr_en,
  input wire                  rd_en,
  input wire [DATA_WIDTH-1:0] data_in,
  output wire [DATA_WIDTH-1:0] data_out,
  output wire                 full,
  output wire                 empty
);

  localparam DEPTH = 1 << ADDR_WIDTH;

  reg [ADDR_WIDTH:0] wr_ptr;
  reg [ADDR_WIDTH:0] rd_ptr;

  reg [DATA_WIDTH-1:0] mem [0:DEPTH-1];

  assign full = (wr_ptr[ADDR_WIDTH] != rd_ptr[ADDR_WIDTH] 
                && wr_ptr[ADDR_WIDTH-1:0] == rd_ptr[ADDR_WIDTH-1:0]);
  assign empty = (wr_ptr == rd_ptr);

  assign data_out = mem[rd_ptr[ADDR_WIDTH-1:0]];

  always @ (posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      wr_ptr <= 0;
      rd_ptr <= 0;
    end else begin
      if (!full && wr_en) begin
        mem[wr_ptr[ADDR_WIDTH-1:0]] <= data_in;
        wr_ptr <= wr_ptr + 1;
      end
      if (!empty && rd_en) begin
        rd_ptr <= rd_ptr + 1;
      end
    end
  end

endmodule
