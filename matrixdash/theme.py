"""Colors, terminal escape codes and glyph tables."""

from __future__ import annotations

RESET = "\x1b[0m"
HIDE_CURSOR = "\x1b[?25l"
SHOW_CURSOR = "\x1b[?25h"
ALT_SCREEN = "\x1b[?1049h"
MAIN_SCREEN = "\x1b[?1049l"
CLEAR = "\x1b[2J"
WRAP_OFF = "\x1b[?7l"
WRAP_ON = "\x1b[?7h"

# Style ids. G0..G6 is a green ramp from almost black to pale green.
NONE, G0, G1, G2, G3, G4, G5, G6, RED, AMBER, CYAN, TEXT, WHITE, KEYCAP, KEYCAP_RED = range(15)
GREENS = (G0, G1, G2, G3, G4, G5, G6)

SGR_256 = {
    NONE: "0", G0: "38;5;22", G1: "38;5;28", G2: "38;5;34", G3: "38;5;40",
    G4: "38;5;46", G5: "38;5;83", G6: "38;5;157",
    RED: "38;5;196", AMBER: "38;5;214", CYAN: "38;5;51", TEXT: "38;5;151", WHITE: "38;5;231",
    KEYCAP: "38;5;16;48;5;40", KEYCAP_RED: "38;5;231;48;5;160",
}

# The Linux console only has 16 colors, so its palette is reprogrammed
# (OSC P) with shades of green and the styles are mapped onto those slots.
# Every SGR starts with 0 because bright colors on the console set the bold
# attribute, which would otherwise leak into the next color.
LINUX_PALETTE = {
    0: "000000", 1: "ff3b3b", 2: "00c040", 3: "ffaf00",
    4: "005a1e", 5: "00902e", 6: "00e0ff", 7: "9ec99e",
    8: "06240f", 9: "ff5f5f", 10: "39ff6a", 11: "ffd75f",
    12: "c6ffd2", 13: "1fdc58", 14: "8cffff", 15: "ffffff",
}
SGR_LINUX = {
    NONE: "0", G0: "90", G1: "34", G2: "35", G3: "32", G4: "95", G5: "92", G6: "94",
    RED: "91", AMBER: "33", CYAN: "36", TEXT: "37", WHITE: "97",
    KEYCAP: "30;42", KEYCAP_RED: "97;41",
}

# Rain glyphs dimmed when they fall behind the portrait (indexed by style id).
DIM_LUT = [NONE, G0, G0, G0, G1, G1, G1, G2, G1, G1, G1, G1, G2, G1, G1]

MATRIX_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ$+-*/=%#&<>[]{}|:;~^¦§¤¥£ÆØÞßµ"
GLITCH_CHARS = "01#$%@&*<>/\\|=+"
SCRAMBLE_CHARS = "0123456789ABCDEF#$%&*+=<>"
BORDER_CHARS = set("─│╭╮╰╯═║╔╗╚╝┄")
SPINNER = "-\\|/"

DAYS_PT = ["SEG", "TER", "QUA", "QUI", "SEX", "SAB", "DOM"]
MONTHS_PT = ["JAN", "FEV", "MAR", "ABR", "MAI", "JUN", "JUL", "AGO", "SET", "OUT", "NOV", "DEZ"]

BIG_DIGITS = {
    "0": ("┏━┓", "┃ ┃", "┗━┛"),
    "1": ("╺┓ ", " ┃ ", "╺┻╸"),
    "2": ("╺━┓", "┏━┛", "┗━╸"),
    "3": ("╺━┓", " ━┫", "╺━┛"),
    "4": ("╻ ╻", "┗━┫", "  ╹"),
    "5": ("┏━╸", "┗━┓", "╺━┛"),
    "6": ("┏━╸", "┣━┓", "┗━┛"),
    "7": ("╺━┓", "  ┃", "  ╹"),
    "8": ("┏━┓", "┣━┫", "┗━┛"),
    "9": ("┏━┓", "┗━┫", "╺━┛"),
}
