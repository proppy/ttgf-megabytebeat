```
░█▀▄░█░█░▀█▀░█▀▀░█▀▄░█▀▀░█▀█░▀█▀░▀░█▀▀
░█▀▄░░█░░░█░░█▀▀░█▀▄░█▀▀░█▀█░░█░░░░▀▀█
░▀▀░░░▀░░░▀░░▀▀▀░▀▀░░▀▀▀░▀░▀░░▀░░░░▀▀▀
       • ▌ ▄ ·. ▄▄▄ . ▄▄ •  ▄▄▄· 
       ·██ ▐███▪▀▄.▀·▐█ ▀ ▪▐█ ▀█ 
       ▐█ ▌▐▌▐█·▐▀▀▪▄▄█ ▀█▄▄█▀▀█ 
       ██ ██▌▐█▌▐█▄▄▌▐█▄▪▐█▐█ ▪▐▌
       ▀▀  █▪▀▀▀ ▀▀▀ ·▀▀▀▀  ▀  ▀ 
 █████   █████ █████ ███████████  █████████ 
░░███   ░░███ ░░███ ░█░░░███░░░█ ███░░░░░███
 ░███    ░███  ░███ ░   ░███  ░ ░███    ░░░ 
 ░███████████  ░███     ░███    ░░█████████ 
 ░███░░░░░███  ░███     ░███     ░░░░░░░░███
 ░███    ░███  ░███     ░███     ███    ░███
 █████   █████ █████    █████   ░░█████████ 
░░░░░   ░░░░░ ░░░░░    ░░░░░     ░░░░░░░░░  
      ,----,     ,----..        ,---,     ,---, 
    .'   .' \   /   /   \    ,`--.' |  ,`--.' | 
  ,----,'    | /   .     :  /    /  : /    /  : 
  |    :  .  ;.   /   ;.  \:    |.' ':    |.' ' 
  ;    |.'  /.   ;   /  ` ;`----':  |`----':  | 
  `----'/  ; ;   |  ; \ ; |   '   ' ;   '   ' ; 
    /  ;  /  |   :  | ; | '   |   | |   |   | | 
   ;  /  /-, .   |  ' ' ' :   '   : ;   '   : ; 
  /  /  /.`| '   ;  \; /  |   |   | '   |   | ' 
./__;      :  \   \  ',  /    '   : |   '   : | 
|   :    .'    ;   :    /     ;   |.'   ;   |.' 
;   | .'        \   \ .'      '---'     '---'   
`---'            `---`                          
```

Extended Play of some of the 2011's BYTEBEAT greatest hits on-a-chip:

| Track | Formula | Artist | Original Params |
| ----- | ------- | ------ | --------------- |
| `0` | `t*({b,a}&t>>{d,c})` | people on irc (*the 42 melody*) | `a=0xa,b=0x2,c=0xa,d=0x0` |
| `1` | `t\|t%{b,a}\|t%(2+{d,c})` | danharaj (*fractal trees*) | `a=0xf,b=0xf,c=0xf,d=0xf` |
| `2` | `t>>a&b?t>>c:-t>>d` | droid (*untitled*) | `a=0x6,b=0x1,c=0x5,d=0x4` |
| `3` | `t*(t>>a\|t>>b)&{d,c}` | Niklas_Roy (*a tune to share*) | `a=0x9,b=0xd,c=0x0,d=0x1` |
| `4` | `t*a&(t>>b)\|t*c&(t>>d)` | miiro (*sierpinski harmony*) | `a=0x5,b=0x7,c=0x3,d=0xa` |

## How it works

This TTGF26c mixed-signal design (`2x2` analog slot, Digital-on-Top) compiles the entire mixed-signal control and synthesis pipeline from a single fused XLS DSLX proc (`src/megabytebeat.x`) running at `32.768 kHz` (`2.097152 MHz / 64`):

- **4× 4-bit resistive-summing ramp ADCs (`ua[3:0]`):** Sample analog CV/potentiometer voltages for parameters `a, b, c, d`. Each `sc_adc_ramp_1ch_4b` channel (pure-passive `r2r_ladder_4bit_sum` poly-resistor macro `28.00 × 15.12 µm`, `L_2R = 5.82 µm` + `clkinv_12` drivers + `aoi21_1` comparator) is instantiated directly from DSLX via `#[extern_verilog]`.
- **32.768 kHz Bresenham BPM / tap-tempo sync (`ui_in[0]`):** Synchronized and edge-stretched across `/64` ticks; locks playback rate to external sync pulses (default `120 BPM` = `8.192 kHz` sample rate).
- **5-track XLS bytebeat synthesizer:** Evaluates all 5 canonical formulas concurrently (`umod_u32_u9` restoring divider in `fractaltrees` for compact GF180 area), driving 5× 8-bit PWM outputs on `uo_out[{7,3,2,1,0}]`.
- **2× 8-bit poly R-2R DACs (`ua[4]`, `ua[5]`):** Pure-passive `r2r_ladder_8bit` poly-resistor macros (`70.00 × 26.32 µm`, `L_1R = 20.00 µm`, `L_2R = 19.80 µm` trimmed for 3.0–5.5 V PVT monotonic linearity) driven by external `clkbuf_20` stdcells. Select which two tracks feed `ua[4]` / `ua[5]` via `ui_in[3:1]` and `ui_in[6:4]`.
- **Zero-standby power-gating:** Holding `rst_n = 0` or deselecting the project (`ena = 0`) combinationally clamps all 4 ADC and 2 DAC resistor ladders to `0 V` (`< 10 µA` total core standby).

> **Note on characterization / test modes:** Legacy multi-mode muxes are omitted to keep the `2x2` tile lean and congestion-free. Lightweight bring-up/characterization modes can be reintroduced cleanly in a follow-up by tapping directly into the fused XLS core's generated signals (`_adc_step_s`, `_adc_params_s`, `_adc_params_s_vld`, `_pcm_s`, `_pcm_s_vld`).

## How to test

1. Drive `clk` at `2.097152 MHz` (`64 × 32.768 kHz`), hold `rst_n = 0` for at least 4 clocks, then release `rst_n = 1`.
2. Apply `0–3.3 V` analog CV or potentiometer voltages to `ua[3:0]` for parameters `a, b, c, d`.
3. Select the tracks routed to the two analog DAC outputs (`ua[4]`, `ua[5]`) with `ui_in[3:1]` (`dac0_sel`, `0..4`) and `ui_in[6:4]` (`dac1_sel`, `0..4`).
4. Optionally pulse `ui_in[0]` (`bpm_in`) to lock the bytebeat sample rate to an external BPM / tap-tempo clock (`1024` samples per measured beat period).
5. Listen to all 5 tracks simultaneously via PWM on `uo_out[0..3]` and `uo_out[7]`, or directly from the analog R-2R DACs on `ua[4]` and `ua[5]`.

## External hardware

- 4× potentiometers or CV sources (`0–3.3 V`) connected to `ua[3:0]`.
- High-impedance audio amplifier / scope probes on `ua[4]` and `ua[5]` (`~20.8 kΩ` output impedance).
- Optional RC low-pass filter on `uo_out[0..3,7]` for PWM audio playback.
