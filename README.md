# Audio-Reactive DMX Lights

Turn one audio track into a full DMX lighting show. The track is analysed **offline**, converted into a table of DMX frames, and played back in sync with the music by an **ESP32 (ESP-WROOM-32)** that talks to the lights through a **TTL-to-RS-485 module**.

Beats and the flow of the music drive brightness. Different instruments drive different colours. On every beat exactly one colour takes priority, so red, green and blue never blend into white.

---

## Table of contents

1. [Project summary](#1-project-summary)
2. [Goals](#2-goals)
3. [Hardware](#3-hardware)
4. [DMX basics used in this project](#4-dmx-basics-used-in-this-project)
5. [Light configuration](#5-light-configuration)
6. [Wiring](#6-wiring)
7. [How it works](#7-how-it-works)
8. [Colour logic](#8-colour-logic)
9. [Repository structure](#9-repository-structure)
10. [Setup and usage](#10-setup-and-usage)
11. [Testing and calibration](#11-testing-and-calibration)
12. [Troubleshooting](#12-troubleshooting)
13. [Design decisions](#13-design-decisions)
14. [Limitations](#14-limitations)
15. [Roadmap](#15-roadmap)
16. [Author and licence](#16-author-and-licence)

---

## 1. Project summary

| Item | Detail |
|---|---|
| Purpose | Make LED lights follow the beats, instruments and flow of a specific audio track |
| Control protocol | DMX512 |
| Microcontroller | ESP-WROOM-32 (ESP32) |
| DMX line driver | TTL to RS-485 module |
| Reference desk | Next Vmix 2000, 12/24-channel DMX light desk |
| Fixtures | LED lights with 3-pin DMX input, address `d001` |
| Approach | Pre-analyse the track offline, then play back DMX frames in sync with the audio |
| Track count | One track (the show is designed around a single song) |

## 2. Goals

- Brightness follows the beats and the overall flow (energy) of the audio.
- Lights stay lit through the whole song. They are not dark between peaks.
- A mix of RGB colours and white light.
- Different colours are tied to different instruments or frequency bands.
- One colour wins each beat, so red, green and blue never mix into white by accident.
- Hardware stays simple: ESP32 plus a cheap TTL-to-RS-485 module, no commercial DMX interface.

## 3. Hardware

| Part | Notes |
|---|---|
| ESP-WROOM-32 board | Generates the DMX signal from a UART |
| TTL to RS-485 module | Converts the ESP32 UART (TTL) into the differential RS-485 signal DMX uses |
| LED light(s) | 3-pin DMX input, address `d001` |
| 3-pin DMX (XLR) cable | Between the module and the light |
| Next Vmix 2000 | 12/24-channel DMX desk, used to learn how the fixture responds |
| Audio source | Speaker or sound system playing the track, started in sync with the lights |
| 5 V / USB supply | Powers the ESP32 and RS-485 module |
| 120 ohm resistor (recommended) | DMX line termination at the last fixture |

## 4. DMX basics used in this project

- DMX512 sends up to 512 channels, each with a value from 0 to 255.
- Serial format: **250,000 baud, 8 data bits, no parity, 2 stop bits (8N2)**.
- Each frame starts with a **break** (line held low), then a **mark-after-break**, then a **start code of 0**, then the channel values.
- Frames are sent continuously. Fixtures hold the last values they received.
- Differential signalling over RS-485 is why a TTL-to-RS-485 module is needed. The ESP32 cannot drive DMX directly.
- The ESP32 hardware UART is used with a generated break, which keeps timing stable while WiFi and other tasks run.

## 5. Light configuration

| Setting | Value |
|---|---|
| DMX address | `d001` (start channel 1) |
| Connector | 3-pin DMX input |
| Channels 1, 2, 3 | Red, green, blue (same layout as the Vmix 2000 channels) |
| Channel 7 | Master dimmer |

Verification test: setting channels 1, 2 and 3 to 255 produced white light, and sweeping the master dimmer faded it in and out. This confirmed the RGB layout and the dimmer channel.

> Channel 7 is the master dimmer, so the beat brightness is written to channel 7 while the colour mix stays on channels 1 to 3. If a different fixture is used, re-run the sweep test and update the channel map in the configuration.

## 6. Wiring

```
ESP32 (ESP-WROOM-32)          TTL-to-RS485 module           DMX light (3-pin)
------------------            -------------------           -----------------
TX (UART TX pin)  ---------->  DI
3V3 / 5V          ---------->  VCC
GND               ---------->  GND  ------------------------ Pin 1  (shield / GND)
GPIO (DE/RE) or tied high --->  DE + RE (joined together)
                               B (Data+) ------------------- Pin 3  (Data +)
                               A (Data-) ------------------- Pin 2  (Data -)
```

Notes:

- **DE and RE** are joined and held high so the module is always transmitting. Many modules do this automatically; check the board.
- **A/B labelling varies** between modules. If the light does not respond, swap A and B.
- **RX is unused.** DMX here is send-only.
- Use a common ground between the ESP32, the module and the fixture.
- Add a 120 ohm termination resistor between pin 2 and pin 3 at the last fixture on long runs.
- Choose a TX pin that is safe at boot (avoid strapping pins) and note it in the configuration.

DMX XLR 3-pin pinout:

| Pin | Signal |
|---|---|
| 1 | Ground / shield |
| 2 | Data - |
| 3 | Data + |

## 7. How it works

The project is split into two stages.

### Stage 1: offline analysis

1. Load the audio track.
2. Split the audio into frequency bands that roughly match instruments (for example bass and kick, mid-range, high-range).
3. Detect beats and onsets.
4. Compute a smoothed energy envelope for the overall flow of the song.
5. Decide, for each time step, which colour wins and how bright the light should be.
6. Export the result as a table of DMX frames at a fixed frame rate.

### Stage 2: playback on the ESP32

1. The ESP32 holds the pre-computed frame table.
2. On start, a timer begins in sync with the audio.
3. For each time step the ESP32 builds a DMX frame and sends it through the RS-485 module.
4. Frames keep streaming for the whole song so the light never sees a gap.

### Why offline instead of live audio analysis?

There is only one track to play, so the show can be tuned until it looks right. Offline analysis is more accurate than real-time analysis, needs no microphone or audio input on the ESP32, and gives the same result every time.

## 8. Colour logic

The first versions mixed bands directly into red, green and blue. Because several bands were often active at once, all three channels stayed high and the lights looked **white all the time**. The current logic fixes that:

1. **Instrument or band to colour.** Each band or instrument is assigned a colour (red, green or blue), with white used deliberately for accents.
2. **One colour per beat.** On each beat, the strongest band is chosen and only its colour is sent at full level. The other colour channels are held low, so red, green and blue never combine into white.
3. **Always lit.** A baseline brightness is kept between beats, and the master dimmer follows the energy envelope, so the lights are never dark during the song.
4. **Brightness on the beat.** Channel 7 (master dimmer) rises sharply on the beat and decays smoothly, following the flow of the audio.
5. **White as a choice.** White is produced only when it is requested (all three colour channels at the same level), for example on big accents.

## 9. Repository structure

> Adjust this section to match the files actually in the repository.

```
audio-reactive-dmx-lights/
├── README.md              This file
├── docs/
│   └── index.html         Project home page (GitHub Pages)
├── audio/                 Source audio track
├── analysis/              Offline audio analysis scripts
├── firmware/              ESP32 DMX playback code
├── data/                  Generated DMX frame table
└── LICENSE
```

## 10. Setup and usage

### Requirements

- ESP-WROOM-32 board and a USB cable
- TTL-to-RS-485 module and a DMX light, wired as in section 6
- The ESP32 toolchain you use for the firmware (Arduino IDE or PlatformIO)
- A computer to run the analysis scripts

### Steps

1. **Wire the hardware** as shown in section 6.
2. **Set the light** to DMX address `d001` and confirm it is in DMX mode.
3. **Run the sweep test** (section 11) to confirm channels 1 to 3 and 7 behave as expected.
4. **Analyse the track** with the scripts in `analysis/` to generate the DMX frame table.
5. **Upload the frame table and firmware** to the ESP32.
6. **Start playback** and the audio together. Align the start once and note the offset.
7. **Fine-tune** brightness, colour thresholds and sync offset, then regenerate and re-upload.

## 11. Testing and calibration

| Test | Expected result |
|---|---|
| Channel 7 sweep | Light fades in and out smoothly |
| Channels 1, 2, 3 at 255 | White light |
| Channel 1 only | Red |
| Channel 2 only | Green |
| Channel 3 only | Blue |
| Single-beat test | Only one colour appears on each beat |
| Full song run | Lights stay lit from start to finish |

If you can, confirm the same behaviour on the Next Vmix 2000 desk first. It is useful as a reference for what the fixture should do.

## 12. Troubleshooting

| Problem | Likely cause and fix |
|---|---|
| Light does nothing | Swap RS-485 A and B; check DE/RE is high; check common ground |
| Light flickers | Check 250 kbaud 8N2 settings and the break timing; add termination |
| Wrong colours | Re-check the channel map (1 to 3 = RGB); confirm the fixture's channel mode |
| Always looks white | Colours are mixing: lower the non-winning bands or use the one-colour-per-beat logic |
| Dark between beats | Raise the baseline brightness; smooth the energy envelope |
| Out of sync with music | Adjust the start offset and regenerate; check the frame rate |
| ESP32 resets or glitches | Avoid strapping pins for TX; check the power supply |

## 13. Design decisions

- **Offline analysis over live analysis.** Only one track is needed, so accuracy and repeatability win.
- **ESP32 over a desk.** An ESP32 and an RS-485 module cost far less than a DMX interface and can be embedded.
- **RS-485 module required.** The ESP32 UART is TTL and single-ended, so it cannot drive DMX directly.
- **One colour per beat.** Prevents the lights collapsing into white.
- **Master dimmer on channel 7.** Brightness is handled by the fixture's own dimmer, keeping the colour channels clean.

## 14. Limitations

- Designed around a single track. A new song needs a fresh analysis run.
- Sync depends on starting the audio and the lights together.
- Colours-by-instrument is approximated using frequency bands, not true instrument separation.
- DMX is one-way here; the fixture cannot report back.

## 15. Roadmap

- [ ] Support multiple fixtures and addresses
- [ ] Add more colours using extra mixing and white accents
- [ ] Real-time mode using a microphone or line-in
- [ ] Better instrument separation
- [ ] Simple web UI for tuning colours and thresholds
- [ ] Wireless start/sync trigger

## 16. Author and licence

Created by **Saurabh Biswas**.

Licensed under the MIT License. See the `LICENSE` file.
