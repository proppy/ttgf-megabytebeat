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

| Track (`uio` sel) | PWM Pin | Track | Artist |
| ----------------- | ------- | ----- | ------ |
| `0` | `uo[0]` | the 42 melody | people on irc |
| `1` | `uo[1]` | fractal trees | danharaj |
| `2` | `uo[2]` | untitled | droid |
| `3` | `uo[3]` | a tune to share | Niklas_Roy |
| `4` | `uo[7]` | sierpinski harmony | miiro |

## How it works

The unified XLS core digitizes 4× analog CV inputs (`a = ua[0]`, `b = ua[1]`, `c = ua[2]`, `d = ua[3]`) via four on-chip **4-bit resistive-summing ramp ADCs** (`r2r_ladder_4bit_sum`, `16`-step ramp at `32 kHz` → `2 kHz` parameter refresh) and computes 5 concurrent 8-bit PCM bytebeat streams at `8 kHz` (`2.048 MHz / 256`):

| Track (`uio` sel) | PWM Pin | Formula | Original Params |
| ----------------- | ------- | ------- | --------------- |
| `0` | `uo[0]` | `t*({b,a}&t>>{d,c})` | `a=0xa,b=0x2,c=0xa,d=0x0` |
| `1` | `uo[1]` | `t\|t%{b,a}\|t%(2+{d,c})` | `a=0xf,b=0xf,c=0xf,d=0xf` |
| `2` | `uo[2]` | `t>>a&b?t>>c:-t>>d` | `a=0x6,b=0x1,c=0x5,d=0x4` |
| `3` | `uo[3]` | `t*(t>>a\|t>>b)&{d,c}` | `a=0x9,b=0xd,c=0x0,d=0x1` |
| `4` | `uo[7]` | `t*a&(t>>b)\|t*c&(t>>d)` | `a=0x5,b=0x7,c=0x3,d=0xa` |

Outputs are available simultaneously as:
- **Digital 8-bit PWM** on `uo[0..3]` and `uo[7]` (`256`-clock frames at `2.048 MHz`).
- **Dual analog outputs** on `ua[4]` (DAC0) and `ua[5]` (DAC1) via two on-chip **8-bit poly-resistor R-2R DACs** (`r2r_ladder_8bit`, `~20.7 kΩ` output impedance, `0..VDD` swing). Select any of the 5 tracks per DAC using `uio[2:0]` (`dac0_sel`, defaults to track `0` for `5..7`) and `uio[5:3]` (`dac1_sel`, defaults to track `1` for `5..7`).

Samples are registered inside the XLS core for glitch-free updates at frame boundaries, and holding reset (`rst_n = 0`) or deselecting the project (`ena = 0`) combinationally clamps all 4 ADC ladders and both DAC ladders to `0 V` for near-zero standby leakage.

## How to test

- **Analog CV inputs (`ua[0..3]`):** Drive `ua[0..3]` with `0..3.3 V` analog control voltages (e.g., potentiometers or buffered CV sources with low source impedance, `< 1 kΩ` recommended; the Tiny Tapeout analog mux adds `~300–500 Ω` in series with the `6.3 kΩ` summing resistor, shifting the bottom trip point down by ~0.5 LSB while remaining monotonic).
- **Analog R-2R DACs (`ua[4..5]`):** Connect `ua[4]` and `ua[5]` through a high-impedance buffer, op-amp, or line-in (`>= 100 kΩ` input impedance; do not drive a low-impedance speaker directly from the `~20.7 kΩ` passive R-2R ladder). Use `uio[2:0]` and `uio[5:3]` to choose which tracks are routed to `ua[4]` and `ua[5]`.
- **Digital PWM (`uo[0..3]`, `uo[7]`):** Connect a speaker or Tiny Tapeout Audio PMOD to the PWM pin you want to play.

## External hardware

- 4× potentiometers (e.g. `1–5 kΩ` wired between `GND` and `3.3 V`, or buffered CV sources) on `ua[0..3]` for parameters `a..d`.
- DIP switches or jumpers on `uio[5:0]` for `dac0_sel` / `dac1_sel`.
- Op-amp buffer / audio amplifier / line-in for `ua[4..5]` analog outputs.
- Speakers or [TinyTapeout Audio PMOD](https://github.com/MichaelBell/tt-audio-pmod) if you want to hear `uo[7]`.
