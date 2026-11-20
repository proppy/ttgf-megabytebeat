// Copyright 2026 Google LLC.
// SPDX-License-Identifier: Apache-2.0

`default_nettype none

module adc_subsystem (
`ifdef USE_POWER_PINS
    inout  wire        VDD,
    inout  wire        VSS,
`endif
    input  wire        clk,
    input  wire        reset,
    input  wire [3:0]  adc_in,
    output wire [15:0] params_out,
    output wire        params_vld,
    input  wire        params_rdy
);
    wire [3:0] step_bus;
    wire [3:0] cmp_bus;

    adc_ramp u_ramp (
        .clk(clk),
        .reset(reset),
        ._cmp_r(cmp_bus),
        ._cmp_r_vld(1'b1),
        ._cmp_r_rdy(),
        ._step_s(step_bus),
        ._step_s_vld(),
        ._step_s_rdy(1'b1),
        ._params_s(params_out),
        ._params_s_vld(params_vld),
        ._params_s_rdy(params_rdy)
    );

    genvar i;
    generate
        for (i = 0; i < 4; i = i + 1) begin : gen_adc_ch
            sc_adc_ramp_1ch_4b u_adc_ch (
`ifdef USE_POWER_PINS
                .VDD(VDD),
                .VSS(VSS),
`endif
                .step(step_bus),
                .adc_in(adc_in[i]),
                .cmp(cmp_bus[i])
            );
        end
    endgenerate
endmodule

`default_nettype wire
