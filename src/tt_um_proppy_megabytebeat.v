// Copyright 2025-2026 Google LLC.
// SPDX-License-Identifier: Apache-2.0

`default_nettype none

module tt_um_proppy_megabytebeat (
    input  wire [7:0] ui_in,    // Dedicated inputs
    output wire [7:0] uo_out,   // Dedicated outputs
    input  wire [7:0] uio_in,   // IOs: Input path
    output wire [7:0] uio_out,  // IOs: Output path
    output wire [7:0] uio_oe,   // IOs: Enable path (active high: 0=input, 1=output)
    /* verilator lint_off UNUSEDSIGNAL */
    /* verilator lint_off UNDRIVEN */
    inout  wire [7:0] ua,       // Analog outputs: ua[4:0] = 5x 8-bit R-2R DAC tracks
    /* verilator lint_on UNDRIVEN */
    /* verilator lint_on UNUSEDSIGNAL */
    input  wire       ena,      // always 1 when the design is powered; 0 clamps DACs to 0 V
    input  wire       clk,      // clock
    input  wire       rst_n     // reset_n - low to reset
);
   wire        rst = !rst_n;
   wire        analog_en = rst_n && ena;
   wire [15:0] packed_params = {uio_in, ui_in};  // {d, c, b, a}
   wire [39:0] pcm_bus;
   wire        pcm_vld;
   wire        dac_vld;
   wire        en_rdy;
   wire        params_rdy;
   wire [7:0]  pcm[0:7];

   reg [7:0]   clk_8;
   wire        sample_rdy = (clk_8 == 8'hff);
   always @(posedge clk) begin
      if (!rst_n) clk_8 <= 0;
      else begin
         clk_8 <= clk_8 + 1;
      end
   end

   // 5x sc_r2r_dac_8bit are instantiated directly inside `megabytebeat` via XLS #[extern_verilog]
   // (--flop_outputs=false routes passive R-2R `out` wires straight to `ua[4:0]` with zero gates).
   megabytebeat u_megabytebeat (
      .clk(clk),
      .reset(rst),
      ._en_r(analog_en),
      ._en_r_vld(1'b1),
      ._en_r_rdy(en_rdy),
      ._params_r(packed_params),
      ._params_r_vld(1'b1),
      ._params_r_rdy(params_rdy),
      ._pcm_s(pcm_bus),
      ._pcm_s_vld(pcm_vld),
      ._pcm_s_rdy(sample_rdy),
      ._dac_s(ua[4:0]),
      ._dac_s_vld(dac_vld),
      ._dac_s_rdy(sample_rdy)
   );

   assign pcm[0] = pcm_bus[7:0];
   assign pcm[1] = pcm_bus[15:8];
   assign pcm[2] = pcm_bus[23:16];
   assign pcm[3] = pcm_bus[31:24];
   assign pcm[4] = 8'b0;
   assign pcm[5] = 8'b0;
   assign pcm[6] = 8'b0;
   assign pcm[7] = pcm_bus[39:32];

   generate
      for (genvar i = 0; i < 8; i++) begin : pwm_audio
         pwm_audio pwm0 (
            .clk(clk),
            .rst_n(rst_n),
            .sample(pcm[i]),
            .pwm(uo_out[i])
         );
      end
   endgenerate

   assign uio_oe  = 8'b00000000;  // set uio as inputs.
   assign uio_out = 8'b00000000;  // unused to keep yosys happy.

   wire _unused = &{en_rdy, params_rdy, pcm_vld, dac_vld, 1'b0};

endmodule
`default_nettype wire
