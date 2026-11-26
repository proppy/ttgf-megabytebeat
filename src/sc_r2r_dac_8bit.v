// Copyright 2026 Google LLC.
// SPDX-License-Identifier: Apache-2.0
//
// Structural 8-bit R-2R DAC wrapper: 9x (* keep *) clkbuf_20 drivers (term + d[7:0])
// feeding the pure-passive `r2r_ladder_8bit` poly-resistor macro.

`default_nettype none

module sc_r2r_dac_8bit (
    input  wire [7:0] d,
    output wire       out
);
`ifdef SIM
    r2r_ladder_8bit u_r2r (
        .term(1'b0),
        .in(d),
        .out(out)
    );
`else
    wire       term_tie;
    wire       term_drv;
    wire [7:0] pre_drv;
    wire [7:0] in_drv;

    /* verilator lint_off PINMISSING */
    (* keep *)
    gf180mcu_fd_sc_mcu7t5v0__tiel u_tiel (
        .ZN(term_tie)
    );

    (* keep *)
    gf180mcu_fd_sc_mcu7t5v0__clkbuf_20 u_term (
        .I(term_tie),
        .Z(term_drv)
    );

    genvar b;
    generate
        for (b = 0; b < 8; b = b + 1) begin : gen_drv
            // Non-dont_touch pre-buffer lets OpenROAD Resizer freely buffer upstream digital nets.
            (* keep *)
            gf180mcu_fd_sc_mcu7t5v0__clkbuf_4 u_pre (
                .I(d[b]),
                .Z(pre_drv[b])
            );

            (* keep *)
            gf180mcu_fd_sc_mcu7t5v0__clkbuf_20 u_buf (
                .I(pre_drv[b]),
                .Z(in_drv[b])
            );
        end
    endgenerate
    /* verilator lint_on PINMISSING */

    r2r_ladder_8bit u_r2r (
        .term(term_drv),
        .in(in_drv),
        .out(out)
    );
`endif
endmodule
`default_nettype wire
