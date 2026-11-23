// Copyright 2026 Google LLC.
// SPDX-License-Identifier: Apache-2.0

`default_nettype none

module megabytebeat_chip_core #(
    parameter integer TRACKS = 5
) (
    input  wire              clk,
    input  wire              rst_n,
    input  wire              bpm_in,
    input  wire [3:0]        adc_in,
    output wire [TRACKS-1:0] dac_out
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

    adc_subsystem u_adc_sub (
        .clk(clk),
        .reset(reset),
        .adc_in(adc_in),
        .params_out(adc_params),
        .params_vld(adc_params_vld),
        .params_rdy(adc_params_rdy)
    );

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

    genvar k;
    generate
        for (k = 0; k < TRACKS; k = k + 1) begin : gen_dac
            reg [7:0] dac_reg;
            always @(posedge clk) begin
                if (reset) dac_reg <= 8'd128;
                else if (pcm_vld) dac_reg <= pcm_bus[k*8 +: 8];
            end
            sc_r2r_dac_8bit u_dac (
                .d(dac_reg),
                .out(dac_out[k])
            );
        end
    endgenerate
endmodule

`default_nettype wire
