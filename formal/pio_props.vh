// Formal properties for tt_um_ryanalumkal.
// Included inside the module body by src/project.v under `ifdef FORMAL.
// IMEM has no write port in the RTL yet, so the solver gets one here: any
// program can be loaded while rst_n is low (as the testbench does). Every
// property must therefore hold for ANY program.
// Each property is labelled; formal/run_formal.sh proves them one at a time.

  (* anyseq *) reg        f_imem_we;
  (* anyseq *) reg [4:0]  f_imem_addr;
  (* anyseq *) reg [15:0] f_imem_wdata;

  always @(posedge clk)
    if (!rst_n && f_imem_we)
      instruction_memory[f_imem_addr] <= f_imem_wdata;

  localparam [2:0] F_JMP = 3'd0, F_WAIT = 3'd1, F_IN = 3'd2, F_OUT = 3'd3, F_SET = 3'd7;
  localparam [2:0] F_DEST_PINS = 3'd0, F_DEST_X = 3'd1, F_DEST_Y = 3'd2,
                   F_DEST_PINDIRS = 3'd4, F_DEST_PC = 3'd5, F_DEST_ISR = 3'd6;

  reg        f_past_valid = 1'b0;
  reg        f_rst_n_q;
  reg [15:0] f_instr;
  reg [4:0]  f_pc;
  reg [7:0]  f_x, f_y, f_isr, f_osr, f_pins_out, f_pins_oe;
  reg        f_wait_cond;
  reg [3:0]  f_count;

  always @(posedge clk) begin
    f_past_valid <= 1'b1;
    f_rst_n_q    <= rst_n;
    f_instr      <= instr_reg;
    f_pc         <= pc;
    f_x          <= x;
    f_y          <= y;
    f_isr        <= isr;
    f_osr        <= osr;
    f_pins_out   <= pins_out;
    f_pins_oe    <= pins_oe;
    f_wait_cond  <= wait_cond;
    f_count      <= in_count;
  end

  wire [2:0] f_op  = f_instr[15:13];
  wire [2:0] f_sel = f_instr[7:5];
  // The last clock edge executed f_instr (no reset at or since that edge)
  wire       f_step = f_past_valid && f_rst_n_q && rst_n;

  always @(*) begin
    // ---- Pins / host interface -------------------------------------------
    p_uio_oe_fixed: assert (uio_oe == 8'b0111_0100);
    p_uio_out_only_on_outputs: assert ((uio_out & ~uio_oe) == 8'h00);

    // ---- Reset -----------------------------------------------------------
    if (!rst_n)
      p_reset_clears_state: assert (pc == 0 && x == 0 && y == 0 && isr == 0 && osr == 0 &&
                                    pins_out == 0 && pins_oe == 0);

    // ---- PC must always address a real IMEM word ---------------------------
    p_pc_in_imem: assert (pc < $size(instruction_memory));

    if (f_step) begin
      // ---- Progress: everything except JMP/WAIT/OUT PC advances by one ------
      if (f_op != F_JMP && f_op != F_WAIT && !(f_op == F_OUT && f_sel == F_DEST_PC))
        p_pc_advances: assert (pc == f_pc + 5'd1);

      // ---- WAIT stalls without side effects ---------------------------------
      if (f_op == F_WAIT && !f_wait_cond)
        p_wait_stalls: assert (pc == f_pc && x == f_x && y == f_y && isr == f_isr &&
                               osr == f_osr && pins_out == f_pins_out && pins_oe == f_pins_oe);

      // ---- Shift registers --------------------------------------------------
      if (f_op == F_OUT)
        p_out_shifts_osr: assert (osr == (f_osr >> f_count));
      if (f_op == F_IN && f_sel != 3'd4 && f_sel != 3'd5)
        p_in_shifts_isr: assert ((isr >> f_count) == (f_isr & (8'hFF >> f_count)));

      // ---- JMP X--/Y-- never underflow (documents a deviation from RP2040) --
      if (f_op == F_JMP && f_sel == 3'd2)
        p_x_dec_saturates: assert (x == ((f_x == 0) ? 8'd0 : f_x - 8'd1));
      if (f_op == F_JMP && f_sel == 3'd4)
        p_y_dec_saturates: assert (y == ((f_y == 0) ? 8'd0 : f_y - 8'd1));

      // ---- SET PINS only reaches uo_out[4:0] --------------------------------
      if (f_op == F_SET && f_sel == F_DEST_PINS)
        p_set_pins_5bit: assert (uo_out[7:5] == 3'b000);

      // ---- Frame conditions: who may write each register --------------------
      if (x != f_x)
        p_x_writers: assert ((f_op == F_SET && f_sel == F_DEST_X) || (f_op == F_OUT && f_sel == F_DEST_X) ||
                             (f_op == F_JMP && f_sel == 3'd2));
      if (y != f_y)
        p_y_writers: assert ((f_op == F_SET && f_sel == F_DEST_Y) || (f_op == F_OUT && f_sel == F_DEST_Y) ||
                             (f_op == F_JMP && f_sel == 3'd4));
      if (pins_out != f_pins_out)
        p_pins_out_writers: assert ((f_op == F_SET || f_op == F_OUT) && f_sel == F_DEST_PINS);
      if (pins_oe != f_pins_oe)
        p_pins_oe_writers: assert ((f_op == F_SET || f_op == F_OUT) && f_sel == F_DEST_PINDIRS);
      if (isr != f_isr)
        p_isr_writers: assert (f_op == F_IN || (f_op == F_OUT && f_sel == F_DEST_ISR));
      if (osr != f_osr)
        p_osr_writers: assert (f_op == F_OUT);
    end
  end
