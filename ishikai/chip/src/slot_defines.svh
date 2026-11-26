`ifdef SLOT_1X1

// Power/ground pads for core and I/O
`define NUM_DVDD_PADS 6
`define NUM_DVSS_PADS 8

`define NUM_VDD_PADS 2
`define NUM_VSS_PADS 2

// Signal pads
`define NUM_INPUT_PADS 12
`define NUM_BIDIR_PADS 40
`define NUM_ANALOG_PADS 2
`define NUM_NC_PADS 0

`endif

`ifdef SLOT_0P5X1

// GNSS AFE + proppy/ttgf-megabytebeat dual-VDD 0p5x1 chip
//   * GNSS analog:  VDDA / VDDV / VSSA (north afe_rf_pad, isolated via fill10_noPWR)
//   * GNSS digital: VDD / DVDD (4 pads) + shared VSS / DVSS (7 pads)
//   * megabytebeat: VDD_BB / DVDD_BB (3 pads) + shared VSS / DVSS, separated from
//                   GNSS digital VDD via fill10_noVDD (east & west) and fed into
//                   megabytebeat_macro via 6x fill10_supplyVDD
//   * Mixed-signal 5x 8-bit R-2R DAC stage (#29): 5x asig_5p0 DAC outputs (bb_ua_PAD[4:0]),
//     3x South PWM outputs (bb_uo_PAD[2:0]), and 16x parameter inputs (bb_ui_PAD, bb_uio_PAD).
`define HAS_MEGABYTEBEAT

// Power/ground pads for I/O + core (gf180mcu_fd_io shorts DVDD=VDD, DVSS=VSS)
`define NUM_DVDD_PADS 4
`define NUM_DVDD_BB_PADS 3
`define NUM_DVSS_PADS 4
`define NUM_DVSS_BB_PADS 3

`define NUM_VDD_PADS 0
`define NUM_VSS_PADS 0

// GNSS digital + analog signal pads
`define NUM_INPUT_PADS 2
`define NUM_BIDIR_PADS 6
`define NUM_ANALOG_PADS 3

// megabytebeat signal pads inside VDD_BB ring (27 signal pads total, 0 unused)
`define NUM_BB_UO_PADS 3
`define NUM_BB_ANALOG_PADS 5
`define NUM_NC_BB_PADS 0

// Unused pad positions in GNSS VDD ring (asig_5p0, kept for fixed 73-pad 0p5x1 bonding frame)
`define NUM_NC_PADS 13

`endif

`ifdef SLOT_1X0P5

// Power/ground pads for core and I/O
`define NUM_DVDD_PADS 7
`define NUM_DVSS_PADS 7

`define NUM_VDD_PADS 1
`define NUM_VSS_PADS 1

// Signal pads
`define NUM_INPUT_PADS 4
`define NUM_BIDIR_PADS 46
`define NUM_ANALOG_PADS 4
`define NUM_NC_PADS 0

`endif

`ifdef SLOT_0P5X0P5

// Power/ground pads for core and I/O
`define NUM_DVDD_PADS 3
`define NUM_DVSS_PADS 3

`define NUM_VDD_PADS 1
`define NUM_VSS_PADS 1

// Signal pads
`define NUM_INPUT_PADS 2
`define NUM_BIDIR_PADS 6
`define NUM_ANALOG_PADS 3
// unused pad positions of the fixed 56-pad frame (kept for the default bonding setup)
`define NUM_NC_PADS 29

`endif
