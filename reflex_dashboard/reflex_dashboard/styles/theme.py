"""
Design tokens and styling definitions for the Dark OLED Quantitative Trading Terminal.
Follows the Claude Code Frontend Design Toolkit principles.
"""

# Color Palette Tokens
BG_DARK = "#080A10"
SURFACE_DARK = "#10141E"
SURFACE_HOVER = "#181E2E"
BORDER_COLOR = "#1F273D"

# Semantic Accents
CYAN = "#00D4B2"      # Active / Telemetry / Neutral Brand
GREEN = "#10B981"     # Positive P&L / Filled / In-the-money
RED = "#EF4444"       # Negative P&L / Risk Limit / Canceled / Stop
CRIMSON = "#F43F5E"   # Negative drawdown / Loss accent
AMBER = "#F59E0B"     # Warning / Near-threshold / Pending
INDIGO = "#6366F1"    # Neural Policy / Model Inference / Benchmark

# Typography & Contrast
TEXT_PRIMARY = "#F8FAFC"
TEXT_MUTED = "#94A3B8"
TEXT_DIM = "#64748B"

# Font families
FONT_SANS = "'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif"
FONT_MONO = "'JetBrains Mono', 'SF Mono', Consolas, Menlo, monospace"

# Card Style
CARD_STYLE = {
    "background_color": SURFACE_DARK,
    "border": f"1px solid {BORDER_COLOR}",
    "border_radius": "8px",
    "box_shadow": "0 4px 12px rgba(0, 0, 0, 0.4)",
    "padding": "16px",
}

KPI_CARD_STYLE = {
    "background_color": SURFACE_DARK,
    "border": f"1px solid {BORDER_COLOR}",
    "border_radius": "8px",
    "padding": "14px 18px",
    "width": "100%",
    "transition": "all 0.15s ease-in-out",
    "_hover": {
        "border_color": CYAN,
        "background_color": SURFACE_HOVER,
    }
}

TABLE_HEADER_STYLE = {
    "background_color": "#0C101A",
    "color": TEXT_MUTED,
    "font_size": "0.75rem",
    "font_weight": "600",
    "text_transform": "uppercase",
    "letter_spacing": "0.05em",
    "border_bottom": f"1px solid {BORDER_COLOR}",
    "padding": "8px 12px",
}

TABLE_ROW_STYLE = {
    "border_bottom": f"1px solid rgba(31, 39, 61, 0.5)",
    "font_size": "0.85rem",
    "_hover": {
        "background_color": SURFACE_HOVER,
    }
}
