"""Decode Philips stereo I2S from a sigrok binary capture (CH0/1/2)."""

from dataclasses import dataclass


@dataclass
class I2S:
    left: list[int]
    right: list[int]
    frame_hz: float
    slot_widths: set[int]


def decode_i2s(raw: bytes, sample_hz: int) -> I2S:
    # WS changes one bit ahead of the next MSB. The bit on the edge
    # where the new WS first appears belongs to the previous channel.
    slots = []
    value = bits = 0
    channel = None
    previous = 0
    for index, sample in enumerate(raw):
        rising = sample & 1 and not previous & 1
        previous = sample
        if not rising:
            continue
        ws = (sample >> 1) & 1
        value = (value << 1) | ((sample >> 2) & 1)
        bits += 1
        if ws != channel:
            if channel is not None:
                signed = value - (1 << bits) if value & (1 << (bits - 1)) else value
                slots.append((channel, bits, signed, index))
            channel = ws
            value = bits = 0
        elif bits > 64:
            raise ValueError("I2S word select stopped toggling")
    pairs = [(a, b) for a, b in zip(slots, slots[1:]) if a[0] == 0 and b[0] == 1]
    if len(pairs) < 2:
        raise ValueError("I2S capture has fewer than two complete stereo frames")
    return I2S(
        [a[2] for a, _ in pairs],
        [b[2] for _, b in pairs],
        sample_hz * (len(pairs) - 1) / (pairs[-1][0][3] - pairs[0][0][3]),
        {slot[1] for slot in slots},
    )
