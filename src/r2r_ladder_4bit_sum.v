// Copyright 2026 Google LLC.
// SPDX-License-Identifier: Apache-2.0
/// sta-blackbox

`default_nettype none
`ifndef SIM
(* blackbox, keep *)
`endif
module r2r_ladder_4bit_sum (
    input  wire       term,
    input  wire [3:0] in,
    output wire       out,
    input  wire       sum_in
);
`ifdef SIM
    // In GL cocotb sim, testbench drives comparator output on adc_in -> sum_in -> out -> aoi21_1 -> inv_1
    assign out = sum_in;
    wire _unused = &{term, in, 1'b0};
`endif
endmodule
`default_nettype wire
