"""Sign-aware colors for existing formatted numeric state values."""
import reflex as rx
from .theme import GREEN, RED, TEXT_MUTED


def numeric_text(value) -> rx.Var[str]:
    text = rx.Var.create(value).to(str)
    for original, replacement in (("-$0.00", "$0.00"), ("$-0.00", "$0.00"),
                                  ("-0.00%", "0.00%"), ("-0.0%", "0.0%"), ("-0.00", "0.00")):
        text = rx.cond(text == original, replacement, text)
    return text


def numeric_color(value) -> rx.Var[str]:
    text = rx.Var.create(value).to(str).replace("$", "").replace(",", "").replace("%", "").replace("+", "").strip()
    neutral = ((text == "0") | (text == "0.0") | (text == "0.00") |
               (text == "-0.00") | (text == "N/A") | (text == "OPEN") |
               (text == "-") | (text == ""))
    return rx.cond(neutral, TEXT_MUTED, rx.cond(text.startswith("-"), RED, GREEN))
