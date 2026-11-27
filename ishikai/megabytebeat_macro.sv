// SPDX-FileCopyrightText: © 2026 GNSS AFE + megabytebeat Authors
// SPDX-License-Identifier: Apache-2.0
//
// Hardened mixed-signal macro wrapper around XLS `megabytebeat`
// (4x 4-bit ramp ADC on ua[3:0] + 5-track bytebeat + 2x selectable 8-bit R-2R DAC on ua[5:4])
// in a dedicated VDD_BB power domain (shared VSS), clocked natively at 32.768 kHz
// (1:1 ADC ramp tick -> 2.048 kHz CV refresh; /4 = 8.192 kHz DAC audio sample rate).
//
// Enable / quiet-by-default behavior:
//   * The chip_top pad for `ena` (bb_ena_pad) has internal pull-down (PD=1 driven by
//     VDD_BB-domain `vdd_bb_hi_s`), so when unconnected `ena` defaults to 0.
//   * While `ena == 0` and `rst_n == 1`, the integrated clock gate shuts off `gclk`
//     completely (zero dynamic switching during GNSS operation) and `analog_en == 0`
//     combinationally clamps all 4 ADC ladders and both DAC ladders to 0 V (< 2 nA/ch leakage).
//   * On a 1 -> 0 transition of `ena`, `ena_q` keeps `gclk` alive for 1 cycle so the
//     synchronous reset inside XLS `megabytebeat` clears all FFs cleanly before `gclk` stops.
//   * Dedicated VDD_BB-domain constant outputs (vdd_bb_hi_s, vss_lo_s, vss_lo_e) drive all
//     static control pins (PD, PU) of the megabytebeat I/O pads, guaranteeing ZERO GNSS-VDD
//     cells on any megabytebeat net.

`default_nettype none

module megabytebeat_macro (
`ifdef USE_POWER_PINS
    inout  wire       VDD_BB,
    inout  wire       VSS,
`endif
    input  wire       clk,
    input  wire       rst_n,
    input  wire       ena,
    input  wire [5:0] uio_in,
    inout  wire [5:0] ua,
    output wire       vdd_bb_hi_s,
    output wire       vss_lo_s,
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
    /* verilator lint_off PINMISSING */
    (* keep *) gf180mcu_fd_sc_mcu7t5v0__icgtp_4 u_icg (
        .CLK (clk),
        .E   (clk_en),
        .TE  (1'b0),
        .Q   (gclk)
    );

    wire tieh_s_raw, tiel_s_raw, tiel_e_raw;
    (* keep *) gf180mcu_fd_sc_mcu7t5v0__tieh  u_tieh_s (.Z(tieh_s_raw));
    (* keep *) gf180mcu_fd_sc_mcu7t5v0__buf_8 u_bufh_s (.I(tieh_s_raw), .Z(vdd_bb_hi_s));

    (* keep *) gf180mcu_fd_sc_mcu7t5v0__tiel  u_tiel_s (.ZN(tiel_s_raw));
    (* keep *) gf180mcu_fd_sc_mcu7t5v0__buf_8 u_bufl_s (.I(tiel_s_raw), .Z(vss_lo_s));
    (* keep *) gf180mcu_fd_sc_mcu7t5v0__tiel  u_tiel_e (.ZN(tiel_e_raw));
    (* keep *) gf180mcu_fd_sc_mcu7t5v0__buf_8 u_bufl_e (.I(tiel_e_raw), .Z(vss_lo_e));
    /* verilator lint_on PINMISSING */
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
    assign vss_lo_s    = 1'b0;
    assign vss_lo_e    = 1'b0;
`endif

    wire eff_rst_n = rst_n & ena;
    wire rst       = !eff_rst_n;
    wire analog_en = eff_rst_n;

    // Fused XLS core runs 1:1 on 32.768 kHz gclk:
    //   - 16-step ramp ADC sweep -> 32.768 kHz / 16 = 2.048 kHz CV update rate
    //   - 5-track bytebeat synth advances every 4 ticks -> 32.768 kHz / 4 = 8.192 kHz audio
    //   - 2x 8-bit poly R-2R DACs on ua[5:4] selected by uio_in[2:0] / uio_in[5:3]
    wire [15:0] adc_in_packed = {3'b0, ua[3], 3'b0, ua[2], 3'b0, ua[1], 3'b0, ua[0]};
    wire [39:0] pcm_bus;
    wire        pcm_vld;
    wire        dac_vld;
    wire        en_rdy;
    wire        adc_rdy;
    wire        dac_sel_rdy;

    megabytebeat u_core (
        .clk           (gclk),
        .reset         (rst),
        ._en_r         (analog_en),
        ._en_r_vld     (1'b1),
        ._en_r_rdy     (en_rdy),
        ._adc_in_r     (adc_in_packed),
        ._adc_in_r_vld (1'b1),
        ._adc_in_r_rdy (adc_rdy),
        ._dac_sel_r    (uio_in),
        ._dac_sel_r_vld(1'b1),
        ._dac_sel_r_rdy(dac_sel_rdy),
        ._pcm_s        (pcm_bus),
        ._pcm_s_vld    (pcm_vld),
        ._pcm_s_rdy    (1'b1),
        ._dac_s        (ua[5:4]),
        ._dac_s_vld    (dac_vld),
        ._dac_s_rdy    (1'b1)
    );

    wire _unused = &{pcm_bus, pcm_vld, dac_vld, en_rdy, adc_rdy, dac_sel_rdy, 1'b0};

endmodule

`default_nettype wire
