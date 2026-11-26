// SPDX-FileCopyrightText: © 2026 GNSS AFE + megabytebeat Authors
// SPDX-License-Identifier: Apache-2.0
//
// Hardened macro wrapper around the original XLS-generated proppy/ttgf-megabytebeat
// (tt_um_proppy_megabytebeat + megabytebeat.v + pwm_audio.v) in a dedicated VDD_BB
// power domain (shared VSS).
//
// Enable / quiet-by-default behavior:
//   * The chip_top pad for `ena` (bb_ena_pad) has internal pull-down (PD=1 driven by
//     VDD_BB-domain `vdd_bb_hi_s`), so when unconnected `ena` defaults to 0.
//   * While `ena == 0` and `rst_n == 1`, the integrated clock gate shuts off `gclk`
//     completely (zero dynamic switching during GNSS operation) and all 5 PWM outputs
//     are clamped to 0.
//   * On a 1 -> 0 transition of `ena`, `ena_q` keeps `gclk` alive for 1 cycle so the
//     synchronous resets inside XLS `megabytebeat` and `pwm_audio` clear all FFs cleanly
//     before the clock tree stops.
//   * While `ena == 1` and `rst_n == 1`, `tt_um_proppy_megabytebeat` runs bit-identically
//     to the upstream XLS-generated design.
//   * Dedicated VDD_BB-domain constant outputs (vdd_bb_hi_*, vss_lo_*) drive all static
//     control pins (PD, PU, OE, IE, CS, SL, PDRV0, PDRV1) of the megabytebeat I/O pads,
//     guaranteeing ZERO GNSS-VDD cells on any megabytebeat net.

`default_nettype none

module megabytebeat_macro (
`ifdef USE_POWER_PINS
    inout  wire       VDD_BB,
    inout  wire       VSS,
`endif
    input  wire       clk,
    input  wire       rst_n,
    input  wire       ena,
    input  wire [7:0] ui_in,
    input  wire [7:0] uio_in,
    output wire [4:0] uo_pwm,
    output wire       vdd_bb_hi_s,
    output wire       vdd_bb_hi_e,
    output wire       vss_lo_s,
    output wire       vss_lo_w,
    output wire       vss_lo_e
);

    // 1-cycle flush register so synchronous reset clears all FFs when ena drops 1 -> 0
    reg ena_q;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            ena_q <= 1'b0;
        else
            ena_q <= ena;
    end

    wire clk_en = ena | (~rst_n) | ena_q;
    wire gclk;

`ifdef SYNTHESIS
    (* keep *) gf180mcu_fd_sc_mcu7t5v0__icgtp_4 u_icg (
        .CLK (clk),
        .E   (clk_en),
        .TE  (1'b0),
        .Q   (gclk)
    );

    wire tieh_s_raw, tieh_e_raw, tiel_s_raw, tiel_w_raw, tiel_e_raw;
    (* keep *) gf180mcu_fd_sc_mcu7t5v0__tieh  u_tieh_s (.Z(tieh_s_raw));
    (* keep *) gf180mcu_fd_sc_mcu7t5v0__buf_8 u_bufh_s (.I(tieh_s_raw), .Z(vdd_bb_hi_s));
    (* keep *) gf180mcu_fd_sc_mcu7t5v0__tieh  u_tieh_e (.Z(tieh_e_raw));
    (* keep *) gf180mcu_fd_sc_mcu7t5v0__buf_8 u_bufh_e (.I(tieh_e_raw), .Z(vdd_bb_hi_e));

    (* keep *) gf180mcu_fd_sc_mcu7t5v0__tiel  u_tiel_s (.ZN(tiel_s_raw));
    (* keep *) gf180mcu_fd_sc_mcu7t5v0__buf_8 u_bufl_s (.I(tiel_s_raw), .Z(vss_lo_s));
    (* keep *) gf180mcu_fd_sc_mcu7t5v0__tiel  u_tiel_w (.ZN(tiel_w_raw));
    (* keep *) gf180mcu_fd_sc_mcu7t5v0__buf_8 u_bufl_w (.I(tiel_w_raw), .Z(vss_lo_w));
    (* keep *) gf180mcu_fd_sc_mcu7t5v0__tiel  u_tiel_e (.ZN(tiel_e_raw));
    (* keep *) gf180mcu_fd_sc_mcu7t5v0__buf_8 u_bufl_e (.I(tiel_e_raw), .Z(vss_lo_e));
`else
    // Behavioral equivalent of positive-edge integrated clock gate (low-phase latch)
    /* verilator lint_off LATCH */
    reg clk_en_lat;
    always @(*) begin
        if (!clk)
            clk_en_lat = clk_en;
    end
    /* verilator lint_on LATCH */
    assign gclk = clk & clk_en_lat;

    assign vdd_bb_hi_s = 1'b1;
    assign vdd_bb_hi_e = 1'b1;
    assign vss_lo_s    = 1'b0;
    assign vss_lo_w    = 1'b0;
    assign vss_lo_e    = 1'b0;
`endif

    wire eff_rst_n = rst_n & ena;

    wire [7:0] uo_raw;
    wire [7:0] uio_out_unused;
    wire [7:0] uio_oe_unused;

    tt_um_proppy_megabytebeat u_tt (
        .ui_in   (ui_in),
        .uo_out  (uo_raw),
        .uio_in  (uio_in),
        .uio_out (uio_out_unused),
        .uio_oe  (uio_oe_unused),
        .ena     (ena),
        .clk     (gclk),
        .rst_n   (eff_rst_n)
    );

    // Map active tracks: track0=uo[0], track1=uo[1], track2=uo[2], track3=uo[3], track7=uo[7]
    // Clamped to 0 whenever ena == 0
    assign uo_pwm = ena ? {uo_raw[7], uo_raw[3:0]} : 5'b00000;

    wire _unused = &{uo_raw[6:4], uio_out_unused, uio_oe_unused, 1'b0};

endmodule

`default_nettype wire
