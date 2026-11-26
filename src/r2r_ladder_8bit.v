// Copyright 2026 Google LLC.
// SPDX-License-Identifier: Apache-2.0
/// sta-blackbox

`default_nettype none
`ifndef SIM
(* blackbox, keep *)
`endif
module r2r_ladder_8bit (
    input  wire       term,
    input  wire [7:0] in,
    output wire       out
);
`ifdef SIM
    assign out = in[7];
    wire _unused = &{term, in[6:0], 1'b0};
`endif
endmodule
`default_nettype wire
