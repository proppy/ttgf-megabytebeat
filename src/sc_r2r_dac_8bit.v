// Copyright 2026 Google LLC.
// SPDX-License-Identifier: Apache-2.0

`default_nettype none
`ifdef SIM
module sc_r2r_dac_8bit (
`ifdef USE_POWER_PINS
    inout  wire       VDD,
    inout  wire       VSS,
`endif
    input  wire [7:0] d,
    output wire       out
);
    assign out = d[7];
endmodule
`else
(* blackbox, keep *)
module sc_r2r_dac_8bit (
`ifdef USE_POWER_PINS
    inout  wire       VDD,
    inout  wire       VSS,
`endif
    input  wire [7:0] d,
    output wire       out
);
endmodule
`endif
`default_nettype wire
