"""Colour and typography tokens for the dashboard.

The dashboard renders on a dark surface only, so these are the dark steps of
a categorical palette validated for colour-vision deficiency: the three
axis colours clear the all-pairs CVD gate (worst deuteranope dE 9.4) and the
normal-vision floor (worst dE 20.9) against the #1a1a19 surface.

Axis identity is never carried by colour alone -- every plot has a legend and
the numeric readouts are labelled X/Y/Z.
"""

from __future__ import annotations

# Surfaces and ink
SURFACE = "#1a1a19"      # chart surface
PAGE = "#0d0d0d"         # window background
PANEL = "#151514"        # panel background, one step off the page
INK_PRIMARY = "#ffffff"
INK_SECONDARY = "#c3c2b7"
INK_MUTED = "#898781"    # axis labels, ticks
GRIDLINE = "#2c2c2a"
BASELINE = "#383835"
BORDER = "rgba(255, 255, 255, 0.10)"
BORDER_SOLID = "#2c2c2a"

# Categorical slots 1-3, used for the X / Y / Z axes everywhere.
AXIS_COLORS = {
    "x": "#3987e5",  # blue
    "y": "#d95926",  # orange
    "z": "#199e70",  # aqua
}
AXIS_ORDER = ("x", "y", "z")

# A fourth series appears only as a magnitude trace, never beside X/Y/Z as a
# peer, so it takes the muted ink rather than a categorical slot.
MAGNITUDE_COLOR = INK_MUTED

# Status palette -- reserved, never reused as a series colour. Always shipped
# with a text label so state is not colour-alone.
STATUS = {
    "good": "#0ca30c",
    "warning": "#fab219",
    "serious": "#ec835a",
    "critical": "#d03b3b",
}

FONT_FAMILY = 'system-ui, -apple-system, "Segoe UI", sans-serif'

# Live numbers are set in a monospaced face so a digit changing width does not
# shove the rest of the row sideways. Qt stylesheets have no
# `font-variant-numeric`, so tabular figures have to come from the family.
MONO_FAMILY = 'Consolas, "Cascadia Mono", "DejaVu Sans Mono", monospace'


def stylesheet() -> str:
    """Qt stylesheet for the whole application."""
    return f"""
    QWidget {{
        background: {PAGE};
        color: {INK_PRIMARY};
        font-family: {FONT_FAMILY};
        font-size: 12px;
    }}
    QGroupBox {{
        background: {PANEL};
        border: 1px solid {BORDER_SOLID};
        border-radius: 6px;
        margin-top: 14px;
        padding: 10px 8px 8px 8px;
        font-weight: 600;
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        left: 10px;
        padding: 0 4px;
        color: {INK_SECONDARY};
    }}
    QLabel#hint {{ color: {INK_MUTED}; }}
    QLabel#value {{
        font-family: {MONO_FAMILY};
        font-size: 14px;
        font-weight: 600;
    }}
    QLabel#readout {{
        font-family: {MONO_FAMILY};
        font-weight: 600;
    }}
    QLabel#readoutMagnitude {{
        font-family: {MONO_FAMILY};
        color: {INK_SECONDARY};
    }}
    QPushButton {{
        background: {SURFACE};
        border: 1px solid {BASELINE};
        border-radius: 5px;
        padding: 6px 12px;
        color: {INK_PRIMARY};
    }}
    QPushButton:hover {{ border-color: {INK_MUTED}; }}
    QPushButton:pressed {{ background: {PANEL}; }}
    QPushButton:disabled {{ color: {INK_MUTED}; border-color: {GRIDLINE}; }}
    QPushButton#primary {{
        background: {AXIS_COLORS['x']};
        border-color: {AXIS_COLORS['x']};
        font-weight: 600;
    }}
    QPushButton#primary:disabled {{ background: {GRIDLINE}; border-color: {GRIDLINE}; }}
    QComboBox, QLineEdit, QSpinBox, QDoubleSpinBox {{
        background: {SURFACE};
        border: 1px solid {BASELINE};
        border-radius: 5px;
        padding: 4px 8px;
        selection-background-color: {AXIS_COLORS['x']};
    }}
    QComboBox QAbstractItemView {{
        background: {SURFACE};
        border: 1px solid {BASELINE};
        selection-background-color: {AXIS_COLORS['x']};
    }}
    QTabWidget::pane {{
        border: 1px solid {BORDER_SOLID};
        border-radius: 6px;
        top: -1px;
    }}
    QTabBar::tab {{
        background: transparent;
        color: {INK_MUTED};
        padding: 7px 16px;
        border-bottom: 2px solid transparent;
    }}
    QTabBar::tab:selected {{
        color: {INK_PRIMARY};
        border-bottom: 2px solid {AXIS_COLORS['x']};
    }}
    QPlainTextEdit, QTextEdit {{
        background: {SURFACE};
        border: 1px solid {BORDER_SOLID};
        border-radius: 6px;
        font-family: Consolas, "Cascadia Mono", monospace;
        font-size: 11px;
    }}
    QProgressBar {{
        background: {SURFACE};
        border: 1px solid {BASELINE};
        border-radius: 5px;
        height: 14px;
        text-align: center;
        color: {INK_SECONDARY};
    }}
    QProgressBar::chunk {{ background: {AXIS_COLORS['x']}; border-radius: 4px; }}
    QSlider::groove:horizontal {{
        height: 4px; background: {BASELINE}; border-radius: 2px;
    }}
    QSlider::handle:horizontal {{
        width: 14px; margin: -6px 0; border-radius: 7px;
        background: {AXIS_COLORS['x']};
    }}
    QCheckBox {{ spacing: 7px; }}
    QHeaderView::section {{
        background: {PANEL};
        color: {INK_SECONDARY};
        border: none;
        border-bottom: 1px solid {BORDER_SOLID};
        padding: 5px;
    }}
    QTableWidget {{
        background: {SURFACE};
        gridline-color: {GRIDLINE};
        border: 1px solid {BORDER_SOLID};
        border-radius: 6px;
    }}
    """
