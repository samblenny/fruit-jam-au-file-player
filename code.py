# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: Copyright 2026 Sam Blenny
#
# See NOTES.md for documentation links
#
import array
from audiobusio import I2SOut
import audiocore
from board import (
    CKP, CKN, D0P, D0N, D1P, D1N, D2P, D2N,
    I2C, I2S_BCLK, I2S_DIN, I2S_MCLK, I2S_WS
)
import digitalio
import displayio
import framebufferio
import gc
from micropython import const
import picodvi
from pwmio import PWMOut
import struct
import supervisor
import time

from adafruit_tlv320 import TLV320DAC3100


# I2S MCLK clock frequency
MCLK_HZ = const(15_000_000)


def init_display(width, height, color_depth):
    # Initialize the picodvi display
    # Video mode compatibility:
    # | Video Mode     | Fruit Jam | Metro RP2350 No PSRAM    |
    # | -------------- | --------- | ------------------------ |
    # | (320, 240,  8) | Yes!      | Yes!                     |
    # | (320, 240, 16) | Yes!      | Yes!                     |
    # | (320, 240, 32) | Yes!      | MemoryError exception :( |
    # | (640, 480,  8) | Yes!      | MemoryError exception :( |
    displayio.release_displays()
    gc.collect()
    fb = picodvi.Framebuffer(width, height, clk_dp=CKP, clk_dn=CKN,
        red_dp=D0P, red_dn=D0N, green_dp=D1P, green_dn=D1N,
        blue_dp=D2P, blue_dn=D2N, color_depth=color_depth)
    display = framebufferio.FramebufferDisplay(fb)
    supervisor.runtime.display = display
    return display


def configure_dac(i2c, sample_rate, mclk_hz):
    # Configure TLV320DAC (this requires a separate 15 MHz PWMOut to MCLK)

    # 1. Initialize DAC (this includes a soft reset and sets minimum volumes)
    dac = TLV320DAC3100(i2c)

    # 2. Configure headphone/speaker routing and volumes (order matters here)
    dac.speaker_output = False
    dac.headphone_output = True
    #
    # dac_volume (digital gain) range is -63.5 dB (soft) to 24 dB (loud).
    # headphone_volume (analog amp) range is -78.3 dB (soft) to 0 dB (loud).
    # - For samples that are normalized to near full scale loudness, keep
    #   dac_volume below 0 to avoid DSP filter clipping
    # - Use dac_volume=-3, headphone_volume=0 for line level
    # - Try headphone_volume=-24 for headphones (adjust as needed)
    #
    dac.dac_volume = -3       # Keep this below 0 to avoid DSP filter clipping
    dac.headphone_volume = 0  # CAUTION! Line level. Too loud for headphones!
    # dac.headphone_volume = -24  # Use this for headphones

    # 3. Configure the right PLL and CODEC settings for our sample rate
    dac.configure_clocks(sample_rate=sample_rate, mclk_freq=MCLK_HZ)

    # 4. Wait for power-on volume ramp-up to finish
    time.sleep(0.35)
    return dac


def load_au_file(filename):
    with open(filename, 'rb') as f:
        # Read AU file header (6 big-endian 32-bit unsigned integers, 24 bytes)
        header = f.read(24)
        if len(header) != 24:
            raise ValueError("file is not a valid AU file")

        # Make sure samples are 8000 Hz, mono, µ-law encoded
        magic, offset, size, encoding, rate, channels = struct.unpack(
            ">6I", header)
        if magic != 0x2e736e64:
            raise ValueError("Not an AU file (magic bytes are wrong)")
        if encoding != 1:
            raise ValueError("AU file sample encoding is not u-law")
        if rate != 8000 or channels != 1:
            raise ValueError("AU file is not 8000 Hz mono")
        if size == 0xffffffff:
            raise ValueError("AU file with header.size=-1 is not supported")

        t0 = time.monotonic()
        print("t = 0.000")

        # Pre-allocate output buffer (16-bit LPCM)
        pcm = array.array("h", bytearray(size * 2))
        t1 = time.monotonic()
        print(f"delta-t = {t1-t0:.3f}: pre-allocated PCM buffer")

        # Generate µ-law lookup table
        lut = array.array("h", bytearray(512))  # 'h' = int16
        for i in range(256):
            u = (~i) & 0xFF
            sign = u & 0x80
            exp = (u >> 4) & 0x07
            mant = u & 0x0F
            s = ((mant << 3) + 0x84) << exp
            s -= 0x84
            lut[i] = -s if sign else s
        lut_mv = memoryview(lut)
        t2 = time.monotonic()
        print(f"delta-t = {t2-t1:.3f}: generated u-law LUT")

        # Seek to start of audio data
        f.seek(offset)

        # Decode audio sample data in 1 kB chunks
        i = 0
        while i < size:
            data = f.read(min(1024, size - i))
            if not data:
                raise ValueError("Truncated AU file data")
            data_mv = memoryview(data)
            for j in range(len(data_mv)):
                pcm[i + j] = lut_mv[data_mv[j]]
            i += len(data_mv)
        t3 = time.monotonic()
        print(f"delta-t = {t3-t2:.3f}: decoded u-law samples to PCM")

        return pcm


def run():
    # Ensure display is low-res to leave enough RAM for audio sample buffers
    # The delays here are to let my video capture card sync after a reset
    print("One moment please...")
    gc.collect()
    time.sleep(2)
    init_display(320, 240, 16)
    gc.collect()

    # Print startup banner
    print("""
Fruit Jam AU file player (8kHz 8-bit mono u-law)
- CAUTION: Default volume is LINE LEVEL
- For headphones, edit code.py to set
  `dac.headphone_volume = -24`
- To convert WAV to AU with sox:
  `sox demo.wav -r 8000 -t au -e mu-law demo.au`
""")

    # Set up I2C and I2S buses
    i2c = I2C()
    audio = I2SOut(bit_clock=I2S_BCLK, word_select=I2S_WS, data=I2S_DIN)

    # Set up 15 MHz MCLK PWM clock output for less hiss and distortion
    mclk_pwm = PWMOut(I2S_MCLK, frequency=MCLK_HZ, duty_cycle=2**15)

    # Initialize DAC for 8 kHz sample rate
    dac = configure_dac(i2c, 8000, MCLK_HZ)
    time.sleep(1)  # ensure volume has stabilized

    # Load 8-bit µ-law samples from .au file into a 16-bit LPCM buffer
    pcm = load_au_file("demo.au")
    au = audiocore.RawSample(pcm, channel_count=1, sample_rate=8000)
    play_time = len(pcm) / 8000  # length of audio clip in seconds

    # Load 16-bit WAV version
    wav = audiocore.WaveFile("demo_16bit.wav")

    # Play demo file on loop
    print()
    while True:
        print("\rPlaying 8-bit AU ...  ", end='')
        time.sleep(0.5)
        audio.play(au)
        time.sleep(play_time + 1)
        print("\rPlaying 16-bit WAV ...", end='')
        time.sleep(0.5)
        audio.play(wav)
        time.sleep(play_time + 1)


# Run the demo
run()
