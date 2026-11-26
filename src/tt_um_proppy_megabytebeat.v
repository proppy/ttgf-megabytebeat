// Copyright 2025-2026 Google LLC.
// SPDX-License-Identifier: Apache-2.0

`default_nettype none

// Tiny Tapeout TTGF26c mixed-signal top (Digital-on-Top, 2x2 analog slot, 6 ua[5:0] pins).
//
// Single-mode lean mixed-signal bytebeat synthesizer (2.097152 MHz clock):
//   - /64 tick divider drives the fused XLS `megabytebeat` core at 32.768 kHz:
//       * 4x 4-bit resistive-summing ramp ADCs (`ua[3:0]`, instantiated inside XLS via #[extern_verilog])
//       * 32.768 kHz Bresenham BPM / tap-tempo sync (`ui_in[0]`, 2-FF synced + edge-stretched)
//       * 5-track 8-bit bytebeat synthesizer (default 120 BPM = 8.192 kHz sample rate)
//       * 2x 8-bit poly R-2R DACs (`ua[4]` / `ua[5]`, instantiated inside XLS via #[extern_verilog],
//         selected by `ui_in[3:1]` and `ui_in[6:4]`)
//   - 5x 8-bit PWM audio outputs on `uo_out[{7,3,2,1,0}]` (`2.097152 MHz / 256 = 8.192 kHz`)
//   - Combinational zero-standby power-gating (`rst || !ena`) clamps all 4 ADC and 2 DAC ladders to 0 V.
//
// Note on characterization / test modes:
//   Legacy test-mode muxes are intentionally omitted to keep 2x2 routing density low.
module tt_um_proppy_megabytebeat (
    input  wire [7:0] ui_in,    // [0]=bpm_in, [3:1]=dac0_sel, [6:4]=dac1_sel, [7]=unused
    output wire [7:0] uo_out,   // [0..3,7]=5x PWM audio, [6:4]=0
    input  wire [7:0] uio_in,   // unused
    output wire [7:0] uio_out,  // 0
    output wire [7:0] uio_oe,   // 0 (all inputs)
    /* verilator lint_off UNUSEDSIGNAL */
    /* verilator lint_off UNDRIVEN */
    inout  wire [7:0] ua,       // ua[3:0]=adc_in[3:0], ua[4]=dac0, ua[5]=dac1
    /* verilator lint_on UNDRIVEN */
    /* verilator lint_on UNUSEDSIGNAL */
    input  wire       ena,      // high when project is selected; 0 clamps analog ladders to 0 V
    input  wire       clk,      // 2.097152 MHz (= 64 * 32.768 kHz)
    input  wire       rst_n     // active-low synchronous reset
);
    wire rst       = !rst_n;
    wire analog_en = rst_n && ena;

    // /64 tick divider: 2.097152 MHz -> 32.768 kHz core tick (aligned with /256 PWM frames)
    reg [5:0] div64;
    always @(posedge clk) begin
        if (rst) div64 <= 6'h00;
        else div64 <= div64 + 6'h01;
    end
    wire tick_en = (div64 == 6'h3f);

    // 2-FF synchronizer + pulse stretcher for external bpm_in (ui_in[0]) across /64 ticks
    reg bpm_ff0, bpm_ff1, bpm_prev, bpm_edge_pend;
    always @(posedge clk) begin
        if (rst) begin
            bpm_ff0       <= 1'b0;
            bpm_ff1       <= 1'b0;
            bpm_prev      <= 1'b0;
            bpm_edge_pend <= 1'b0;
        end else begin
            bpm_ff0  <= ui_in[0];
            bpm_ff1  <= bpm_ff0;
            bpm_prev <= bpm_ff1;
            if (bpm_ff1 && !bpm_prev) bpm_edge_pend <= 1'b1;
            else if (tick_en)         bpm_edge_pend <= 1'b0;
        end
    end
    wire bpm_in_sync = bpm_ff1 || bpm_edge_pend;

    // 1. Fused XLS core: 4x sc_adc_ramp_1ch_4b -> bpm_sync -> 5-track synth -> 2x sc_r2r_dac_8bit
    wire [15:0] adc_in_packed = {3'b0, ua[3], 3'b0, ua[2], 3'b0, ua[1], 3'b0, ua[0]};
    wire [39:0] pcm_bus;

    megabytebeat u_core (
        .clk(clk),
        .reset(rst),
        ._en_r(analog_en),
        ._en_r_vld(tick_en),
        ._en_r_rdy(),
        ._bpm_r(bpm_in_sync),
        ._bpm_r_vld(tick_en),
        ._bpm_r_rdy(),
        ._adc_in_r(adc_in_packed),
        ._adc_in_r_vld(tick_en),
        ._adc_in_r_rdy(),
        ._dac_sel_r(ui_in[6:1]),
        ._dac_sel_r_vld(tick_en),
        ._dac_sel_r_rdy(),
        ._pcm_s(pcm_bus),
        ._pcm_s_vld(),
        ._pcm_s_rdy(1'b1),
        ._dac_s(ua[5:4]),
        ._dac_s_vld(),
        ._dac_s_rdy(1'b1)
    );

    // 2. 5x 8-bit PWM audio outputs on uo_out[{7,3,2,1,0}] directly from registered State.pcm
    pwm_audio u_pwm0 (.clk(clk), .rst_n(rst_n), .sample(pcm_bus[7:0]),   .pwm(uo_out[0]));
    pwm_audio u_pwm1 (.clk(clk), .rst_n(rst_n), .sample(pcm_bus[15:8]),  .pwm(uo_out[1]));
    pwm_audio u_pwm2 (.clk(clk), .rst_n(rst_n), .sample(pcm_bus[23:16]), .pwm(uo_out[2]));
    pwm_audio u_pwm3 (.clk(clk), .rst_n(rst_n), .sample(pcm_bus[31:24]), .pwm(uo_out[3]));
    assign uo_out[6:4] = 3'b000;
    pwm_audio u_pwm7 (.clk(clk), .rst_n(rst_n), .sample(pcm_bus[39:32]), .pwm(uo_out[7]));

    assign uio_out = 8'h00;
    assign uio_oe  = 8'h00;

    wire _unused = &{ui_in[7], uio_in, 1'b0};
endmodule

`default_nettype wire
