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
    wire [15:0] adc_params;
    wire        adc_params_vld;
    wire        adc_params_rdy;
    wire [15:0] beat_params;
    wire        beat_params_vld;
    wire        beat_params_rdy;
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

    adc_ramp u_ramp (
        .clk(clk),
        .reset(reset),
        ._cmp_r({cmp_3, cmp_2, cmp_1, cmp_0}),
        ._cmp_r_vld(1'b1),
        ._cmp_r_rdy(),
        ._step_s(adc_step),
        ._step_s_vld(),
        ._step_s_rdy(1'b1),
        ._params_s(adc_params),
        ._params_s_vld(adc_params_vld),
        ._params_s_rdy(adc_params_rdy)
    );

    reg [15:0] held_params = 16'h0a2a;
    always @(posedge clk) begin
        if (reset) held_params <= 16'h0a2a;
        else if (adc_params_vld) held_params <= adc_params;
    end
    assign dbg_a = held_params[3:0];
    assign dbg_b = held_params[7:4];
    assign dbg_c = held_params[11:8];
    assign dbg_d = held_params[15:12];

    bpm_sync u_bpm (
        .clk(clk),
        .reset(reset),
        .bpm_sync__bpm_r(bpm_ff1),
        .bpm_sync__bpm_r_vld(1'b1),
        .bpm_sync__bpm_r_rdy(),
        .bpm_sync__adc_params_r(adc_params),
        .bpm_sync__adc_params_r_vld(adc_params_vld),
        .bpm_sync__adc_params_r_rdy(adc_params_rdy),
        .bpm_sync__beat_params_s(beat_params),
        .bpm_sync__beat_params_s_vld(beat_params_vld),
        .bpm_sync__beat_params_s_rdy(beat_params_rdy)
    );

    megabytebeat u_synth (
        .clk(clk),
        .reset(reset),
        ._params_r(beat_params),
        ._params_r_vld(beat_params_vld),
        ._params_r_rdy(beat_params_rdy),
        ._pcm_s(pcm_bus),
        ._pcm_s_vld(pcm_vld),
        ._pcm_s_rdy(1'b1)
    );
    assign dbg_sample_tick = pcm_vld;

    reg [7:0] r_dac_0 = 8'd128;
    reg [7:0] r_dac_1 = 8'd128;
    reg [7:0] r_dac_2 = 8'd128;
    reg [7:0] r_dac_3 = 8'd128;
    reg [7:0] r_dac_4 = 8'd128;
    always @(posedge clk) begin
        if (reset) begin
            r_dac_0 <= 8'd128;
            r_dac_1 <= 8'd128;
            r_dac_2 <= 8'd128;
            r_dac_3 <= 8'd128;
            r_dac_4 <= 8'd128;
        end else if (pcm_vld) begin
            r_dac_0 <= pcm_bus[7:0];
            r_dac_1 <= pcm_bus[15:8];
            r_dac_2 <= pcm_bus[23:16];
            r_dac_3 <= pcm_bus[31:24];
            r_dac_4 <= pcm_bus[39:32];
        end
    end
    assign dac_d_0 = r_dac_0;
    assign dac_d_1 = r_dac_1;
    assign dac_d_2 = r_dac_2;
    assign dac_d_3 = r_dac_3;
    assign dac_d_4 = r_dac_4;
endmodule
