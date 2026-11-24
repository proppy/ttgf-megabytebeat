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

    // Fused XLS core: instantiates 4x sc_adc_ramp_1ch_4b via #[extern_verilog],
    // clamps step to 4'hF during reset (_en_r=0), and streams 5x 8-bit PCM.
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
        ._pcm_s(pcm_bus),
        ._pcm_s_vld(pcm_vld),
        ._pcm_s_rdy(1'b1)
    );

    genvar k;
    generate
        for (k = 0; k < TRACKS; k = k + 1) begin : gen_dac
            reg [7:0] dac_reg;
            always @(posedge clk) begin
                if (reset) dac_reg <= 8'h00;
                else if (pcm_vld) dac_reg <= pcm_bus[k*8 +: 8];
            end
            // Code-gate passive R-2R DAC to 0 V (d=8'h00) during reset for <1 nA/ch standby.
            sc_r2r_dac_8bit u_dac (
                .d(reset ? 8'h00 : dac_reg),
                .out(dac_out[k])
            );
        end
    endgenerate
endmodule

`default_nettype wire
