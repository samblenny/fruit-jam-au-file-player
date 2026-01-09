# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: Copyright 2026 Sam Blenny
#
# See NOTES.md for documentation links
#
from audiobusio import I2SOut
import audiocore
from board import I2C, I2S_BCLK, I2S_DIN, I2S_MCLK, I2S_WS
import digitalio
from micropython import const
from pwmio import PWMOut
import struct
import time
import ulab.numpy as np

from adafruit_tlv320 import TLV320DAC3100


# I2S MCLK clock frequency
MCLK_HZ = const(15_000_000)


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
            raise ValueError("AU file sample encoding is not µ-law")
        if rate != 8000 or channels != 1:
            raise ValueError("AU file is not 8000 Hz mono")
        if size == 0xffffffff:
            raise ValueError("AU file with header.size=-1 is not supported")

        # Pre-allocate output buffer (16-bit LPCM)
        pcm = np.zeros(size, dtype=np.int16)
        print(f"DEBUG: size:{size} len(pcm):{len(pcm)}")

        # Generate µ-law lookup table
        lut = np.zeros(256, dtype=np.int16)
        for i in range(256):
            u = (~i) & 0xFF
            sign = u & 0x80
            exp = (u >> 4) & 0x07
            mant = u & 0x0F
            s = ((mant << 4) + 0x08) << exp
            s -= 0x84
            lut[i] = -s if sign else s

        # Seek to start of audio data
        f.seek(offset)

        # Decode audio sample data in 1 kB chunks
        i = 0
        while i < size:
            data = f.read(min(1024, size - i))
            if not data:
                raise ValueError("Truncated AU file data")
            samples = np.frombuffer(data, dtype=np.uint8)
            for j, b in enumerate(samples):
                pcm[i + j] = lut[b]
            i += len(data)

        return pcm


def run():

    # Set up I2C and I2S buses
    i2c = I2C()
    audio = I2SOut(bit_clock=I2S_BCLK, word_select=I2S_WS, data=I2S_DIN)

    # Set up 15 MHz MCLK PWM clock output for less hiss and distortion
    mclk_pwm = PWMOut(I2S_MCLK, frequency=MCLK_HZ, duty_cycle=2**15)

    # Initialize DAC for 8 kHz sample rate
    dac = configure_dac(i2c, 8000, MCLK_HZ)

    # Load 8-bit µ-law samples from .au file into a 16-bit LPCM buffer
    pcm = load_au_file("demo.au")
    samples = audiocore.RawSample(pcm, channel_count=1, sample_rate=8000)
    play_time = len(pcm) / 8000  # length of audio clip in seconds

    # Play demo file on loop
    time.sleep(0.5)
    while True:
        audio.play(samples)
        time.sleep(play_time + 1)


# Print startup banner
print("""
Fruit Jam AU file player (8000 hz, 8-bit mono, µ-law companded samples)
- CAUTION: Default volume is LINE LEVEL
- For headphones, edit code.py to set `dac.headphone_volume = -24`
- To convert a 16-bit .wav file to an 8-bit µ-law .au file with sox on Linux:
  `sox demo.wav -r 8000 -t au -e mu-law demo.au`
""")

# Run the demo
run()
