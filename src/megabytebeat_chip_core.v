// Copyright 2026 Google LLC.
// SPDX-License-Identifier: Apache-2.0

`default_nettype none

module megabytebeat_chip_core #(
    parameter integer TRACKS = 5
) (
    input  wire              clk,
    input  wire              rst_n,
    input  wire              bpm_in,
    /* verilator lint_off UNUSEDSIGNAL */
    /* verilator lint_off UNDRIVEN */
    inout  wire [3:0]        adc_in,
    inout  wire [TRACKS-1:0] dac_out
    /* verilator lint_on UNDRIVEN */
    /* verilator lint_on UNUSEDSIGNAL */
);
    wire       reset = ~rst_n;
    wire [4:0] dac_bus;

    reg bpm_ff0, bpm_ff1;
    always @(posedge clk) begin
        if (reset) begin
            bpm_ff0 <= 1'b0;
            bpm_ff1 <= 1'b0;
        end else begin
            bpm_ff0 <= bpm_in;
            bpm_ff1 <= bpm_ff0;
        end
    end

    // Fused XLS core: instantiates 4x sc_adc_ramp_1ch_4b and 5x sc_r2r_dac_8bit via
    // #[extern_verilog], power-gating all 9 passive ladders during reset (_en_r=0).
    wire [15:0] adc_in_packed = {3'b0, adc_in[3], 3'b0, adc_in[2], 3'b0, adc_in[1], 3'b0, adc_in[0]};

    megabytebeat_core u_core (
        .clk(clk),
        .reset(reset),
        ._en_r(rst_n),
        ._en_r_vld(1'b1),
        ._en_r_rdy(),
        ._bpm_r(bpm_ff1),
        ._bpm_r_vld(1'b1),
        ._bpm_r_rdy(),
        ._adc_in_r(adc_in_packed),
        ._adc_in_r_vld(1'b1),
        ._adc_in_r_rdy(),
        ._adc_step_s(),
        ._adc_step_s_vld(),
        ._adc_step_s_rdy(1'b1),
        ._adc_params_s(),
        ._adc_params_s_vld(),
        ._adc_params_s_rdy(1'b1),
        ._pcm_s(),
        ._pcm_s_vld(),
        ._pcm_s_rdy(1'b1),
        ._dac_s(dac_bus),
        ._dac_s_vld(),
        ._dac_s_rdy(1'b1)
    );

    assign dac_out = dac_bus[TRACKS-1:0];
endmodule

`default_nettype wire
