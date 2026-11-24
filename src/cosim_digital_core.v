// Copyright 2026 Google LLC.
// SPDX-License-Identifier: Apache-2.0
//
// Scalar-input digital wrapper for ngspice-42 XSPICE d_cosim + Verilator vlnggen
// (avoids Ubuntu ngspice-42 verilator_shim.cpp bit-mask bug on multi-bit VL_IN8 buses).

module cosim_digital_core (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       bpm_in,
    input  wire       cmp_0,
    input  wire       cmp_1,
    input  wire       cmp_2,
    input  wire       cmp_3,
    output wire [3:0] adc_step,
    output wire [3:0] dbg_a,
    output wire [3:0] dbg_b,
    output wire [3:0] dbg_c,
    output wire [3:0] dbg_d,
    output wire       dbg_sample_tick,
    output wire [7:0] dac_d_0,
    output wire [7:0] dac_d_1,
    output wire [7:0] dac_d_2,
    output wire [7:0] dac_d_3,
    output wire [7:0] dac_d_4
);
    wire        reset = ~rst_n;
    wire [3:0]  step_eff;
    wire [15:0] adc_params;
    wire        adc_params_vld;
    wire [39:0] pcm_bus;
    wire        pcm_vld;

    reg bpm_ff0 = 1'b0;
    reg bpm_ff1 = 1'b0;
    always @(posedge clk) begin
        if (reset) begin
            bpm_ff0 <= 1'b0;
            bpm_ff1 <= 1'b0;
        end else begin
            bpm_ff0 <= bpm_in;
            bpm_ff1 <= bpm_ff0;
        end
    end

    // Compiled with `define SIM so embedded sc_adc_ramp_1ch_4b passes cmp_0..3 from SPICE straight through.
    wire [15:0] cmp_packed = {3'b0, cmp_3, 3'b0, cmp_2, 3'b0, cmp_1, 3'b0, cmp_0};

    megabytebeat_core u_core (
        .clk(clk),
        .reset(reset),
        ._en_r(rst_n),
        ._en_r_vld(1'b1),
        ._en_r_rdy(),
        ._bpm_r(bpm_ff1),
        ._bpm_r_vld(1'b1),
        ._bpm_r_rdy(),
        ._adc_in_r(cmp_packed),
        ._adc_in_r_vld(1'b1),
        ._adc_in_r_rdy(),
        ._adc_step_s(step_eff),
        ._adc_step_s_vld(),
        ._adc_step_s_rdy(1'b1),
        ._adc_params_s(adc_params),
        ._adc_params_s_vld(adc_params_vld),
        ._adc_params_s_rdy(1'b1),
        ._pcm_s(pcm_bus),
        ._pcm_s_vld(pcm_vld),
        ._pcm_s_rdy(1'b1)
    );
    assign adc_step = step_eff;

    reg [15:0] held_params = 16'h0a2a;
    reg        tick_r = 1'b0;
    reg [7:0]  r_dac_0 = 8'h80;
    reg [7:0]  r_dac_1 = 8'h80;
    reg [7:0]  r_dac_2 = 8'h80;
    reg [7:0]  r_dac_3 = 8'h80;
    reg [7:0]  r_dac_4 = 8'h80;
    always @(posedge clk) begin
        if (reset) begin
            held_params <= 16'h0a2a;
            tick_r      <= 1'b0;
            r_dac_0     <= 8'h80;
            r_dac_1     <= 8'h80;
            r_dac_2     <= 8'h80;
            r_dac_3     <= 8'h80;
            r_dac_4     <= 8'h80;
        end else begin
            if (adc_params_vld) held_params <= adc_params;
            tick_r <= pcm_vld;
            if (pcm_vld) begin
                r_dac_0 <= pcm_bus[7:0];
                r_dac_1 <= pcm_bus[15:8];
                r_dac_2 <= pcm_bus[23:16];
                r_dac_3 <= pcm_bus[31:24];
                r_dac_4 <= pcm_bus[39:32];
            end
        end
    end

    assign dbg_a           = held_params[3:0];
    assign dbg_b           = held_params[7:4];
    assign dbg_c           = held_params[11:8];
    assign dbg_d           = held_params[15:12];
    assign dbg_sample_tick = tick_r;
    assign dac_d_0         = r_dac_0;
    assign dac_d_1         = r_dac_1;
    assign dac_d_2         = r_dac_2;
    assign dac_d_3         = r_dac_3;
    assign dac_d_4         = r_dac_4;
endmodule
