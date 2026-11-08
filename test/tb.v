`default_nettype none
`timescale 1ns / 1ps

/* This testbench just instantiates the module and makes some convenient wires
   that can be driven / tested by the cocotb test.py.
*/
module tb ();

  // Dump the signals to a FST file. You can view it with gtkwave or surfer.
  initial begin
    $dumpfile("tb.fst");
    $dumpvars(0, tb);
    #1;
  end

  // Wire up the inputs and outputs:
  reg clk;
  reg rst_n;
  reg ena;
  reg [7:0] ui_in;
  reg [7:0] uio_in;
  wire [7:0] uo_out;
  wire [7:0] uio_out;
  wire [7:0] uio_oe;

  tt_um_ryanalumkal user_project (
      .ui_in  (ui_in),
      .uo_out (uo_out),
      .uio_in (uio_in),
      .uio_out(uio_out),
      .uio_oe (uio_oe),
      .ena    (ena),
      .clk    (clk),
      .rst_n  (rst_n)
  );

`ifndef GL_TEST
  // FIFO observability for cocotb. The host side is driven over SPI on uio_in
  // (see spi_host.py). Internal nets don't exist in the gate-level netlist, so RTL only.
  wire [7:0] tb_rx_data  = user_project.rx_rdata;
  wire       tb_rx_empty = user_project.rx_empty;
  wire       tb_tx_full  = user_project.tx_full;
  wire       tb_tx_empty = user_project.tx_empty;
`endif

endmodule
