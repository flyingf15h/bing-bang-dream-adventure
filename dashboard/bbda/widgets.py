"""Small reusable pieces: ring buffers, readouts and configured plots."""

from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QPoint, QRect, QSize, Qt
from PySide6.QtGui import QFont, QFontMetrics
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLayout,
    QVBoxLayout,
    QWidget,
)

from . import theme


class RingBuffer:
    """Fixed-capacity rolling store for one scalar channel."""

    def __init__(self, capacity: int = 6000) -> None:
        self.capacity = capacity
        self._data = np.zeros(capacity, dtype=float)
        self._count = 0
        self._head = 0

    def append(self, value: float) -> None:
        self._data[self._head] = value
        self._head = (self._head + 1) % self.capacity
        self._count = min(self._count + 1, self.capacity)

    def clear(self) -> None:
        self._count = 0
        self._head = 0

    def values(self) -> np.ndarray:
        if self._count < self.capacity:
            return self._data[: self._count]
        return np.concatenate((self._data[self._head :], self._data[: self._head]))

    def __len__(self) -> int:
        return self._count


class FlowLayout(QLayout):
    """Lays items left to right, wrapping to a new line when width runs out.

    Qt ships no wrapping layout. The alternative here was a fixed row of three
    readouts, which needs about 870 px and so either forces a horizontal
    scrollbar or clips the last one on a 1366-wide display. Wrapping degrades
    to two rows instead, at any width, with no magic breakpoint to maintain.
    """

    def __init__(self, parent=None, spacing: int = 14) -> None:
        super().__init__(parent)
        self._items: list = []
        self.setSpacing(spacing)

    def addItem(self, item) -> None:      # noqa: N802 - Qt override
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):         # noqa: N802 - Qt override
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int):         # noqa: N802 - Qt override
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):        # noqa: N802 - Qt override
        return Qt.Orientations(Qt.Orientation(0))

    def hasHeightForWidth(self) -> bool:  # noqa: N802 - Qt override
        return True

    def heightForWidth(self, width: int) -> int:   # noqa: N802 - Qt override
        return self._layout(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect: QRect) -> None:    # noqa: N802 - Qt override
        super().setGeometry(rect)
        self._layout(rect, apply=True)

    def sizeHint(self):                   # noqa: N802 - Qt override
        return self.minimumSize()

    def minimumSize(self):                # noqa: N802 - Qt override
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QSize(
            margins.left() + margins.right(), margins.top() + margins.bottom()
        )

    def _layout(self, rect: QRect, apply: bool) -> int:
        """Place every item, and return the total height the run needed."""
        margins = self.contentsMargins()
        x = rect.x() + margins.left()
        y = rect.y() + margins.top()
        right = rect.right() - margins.right()
        line_height = 0
        spacing = self.spacing()

        for item in self._items:
            hint = item.sizeHint()
            if line_height and x + hint.width() > right:
                x = rect.x() + margins.left()
                y += line_height + spacing
                line_height = 0
            if apply:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + spacing
            line_height = max(line_height, hint.height())

        return y + line_height - rect.y() + margins.bottom()


class StatTile(QWidget):
    """A label above a value, for readouts that are not worth a chart."""

    def __init__(self, caption: str, unit: str = "", parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(1)

        self._caption = QLabel(caption if not unit else f"{caption}  ({unit})")
        self._caption.setObjectName("hint")
        self._value = QLabel("--")
        self._value.setObjectName("value")

        layout.addWidget(self._caption)
        layout.addWidget(self._value)

    def set_caption(self, caption: str, unit: str = "") -> None:
        """Relabel a tile whose meaning changed, not just its value."""
        self._caption.setText(caption if not unit else f"{caption}  ({unit})")

    def set_value(self, text: str, color: str | None = None) -> None:
        self._value.setText(text)
        self._value.setStyleSheet(f"color: {color};" if color else "")


class VectorReadout(QWidget):
    """X / Y / Z numbers with a colour chip and a magnitude.

    The chip carries the same hue as that axis' plot curve, and the letter
    beside it names the axis, so identity never rests on colour alone.

    Each number sits immediately beside the letter that names it, and is only
    as wide as the widest string its format can produce -- measured, not
    guessed. Three of these sit side by side on the Live tab, so a generous
    per-cell floor here becomes a window that will not fit on a laptop.

    The magnitude rides on the title line rather than taking a fourth column
    beside X / Y / Z: it is a summary of those three, not a peer of them, and
    a fourth equal cell made the row a third wider for no reading benefit.
    """

    def __init__(self, title: str, unit: str, fmt: str = "{:+8.3f}", parent=None) -> None:
        super().__init__(parent)
        self._fmt = fmt

        layout = QGridLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setHorizontalSpacing(12)
        layout.setVerticalSpacing(3)

        value_width = self._value_width()

        header = QLabel(f"{title}  ({unit})")
        header.setObjectName("hint")

        magnitude_name = QLabel("norm")
        magnitude_name.setObjectName("hint")
        self._magnitude = QLabel("--")
        self._magnitude.setObjectName("readoutMagnitude")
        self._magnitude.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._magnitude.setFixedWidth(value_width)

        title_row = QWidget()
        title_layout = QHBoxLayout(title_row)
        title_layout.setContentsMargins(0, 0, 0, 0)
        title_layout.setSpacing(6)
        title_layout.addWidget(header)
        title_layout.addStretch(1)
        title_layout.addWidget(magnitude_name)
        title_layout.addWidget(self._magnitude)
        layout.addWidget(title_row, 0, 0, 1, 3)

        self._values: dict[str, QLabel] = {}
        for column, axis in enumerate(theme.AXIS_ORDER):
            cell, value = self._make_cell(
                QLabel(axis.upper()), "readout", value_width, theme.AXIS_COLORS[axis]
            )
            layout.addWidget(cell, 1, column)
            self._values[axis] = value

    @staticmethod
    def _make_cell(
        name: QLabel, value_role: str, value_width: int, chip_color: str | None
    ) -> tuple[QWidget, QLabel]:
        """One `chip name value` group, packed tight and left-aligned."""
        cell = QWidget()
        row = QHBoxLayout(cell)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(5)

        if chip_color is not None:
            chip = QFrame()
            chip.setFixedSize(9, 9)
            chip.setStyleSheet(f"background: {chip_color}; border-radius: 2px;")
            row.addWidget(chip)

        name.setObjectName("hint")
        value = QLabel("--")
        value.setObjectName(value_role)
        value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        value.setFixedWidth(value_width)

        row.addWidget(name)
        row.addWidget(value)
        # The slack goes here, after the pair, so the number never drifts away
        # from the letter that names it.
        row.addStretch(1)
        return cell, value

    def _value_width(self) -> int:
        """Width of the widest string this readout's format can produce.

        The labels are a fixed width, so anything wider than this would be
        clipped. Measured against the full-scale end of every range these
        readouts carry -- gyro runs to +-2000 dps, four digits and a sign --
        rather than a typical resting value.
        """
        font = QFont()
        font.setFamilies(["Consolas", "Cascadia Mono", "DejaVu Sans Mono", "monospace"])
        font.setWeight(QFont.DemiBold)
        metrics = QFontMetrics(font)
        widest = max(
            metrics.horizontalAdvance(self._fmt.format(value).strip())
            for value in (-8888.888, -888.888, -88.888, 0.0, 8888.888)
        )
        return widest + 4

    def set_vector(self, vector: np.ndarray) -> None:
        for index, axis in enumerate(theme.AXIS_ORDER):
            self._values[axis].setText(self._fmt.format(vector[index]).strip())
        self._magnitude.setText(
            self._fmt.format(float(np.linalg.norm(vector))).strip()
        )


def make_plot(title: str, unit: str, height: int = 150) -> tuple[pg.PlotWidget, dict]:
    """A three-series time plot styled to the dashboard's tokens.

    Returns the widget and the curve dict keyed by axis name. Every plot gets
    a legend because there is more than one series.
    """
    widget = pg.PlotWidget()
    widget.setMinimumHeight(height)
    widget.setBackground(theme.SURFACE)
    widget.setTitle(f"{title}  ({unit})", color=theme.INK_SECONDARY, size="9pt")
    widget.showGrid(x=False, y=True, alpha=0.18)
    widget.setMouseEnabled(x=False, y=True)
    widget.setMenuEnabled(False)

    for side in ("left", "bottom"):
        axis = widget.getAxis(side)
        axis.setPen(pg.mkPen(theme.BASELINE, width=1))
        axis.setTextPen(pg.mkPen(theme.INK_MUTED))
    widget.getAxis("bottom").setLabel("seconds", color=theme.INK_MUTED)

    legend = widget.addLegend(offset=(-8, 6), labelTextColor=theme.INK_SECONDARY)
    legend.setBrush(pg.mkBrush(0, 0, 0, 0))
    legend.setPen(pg.mkPen(None))

    curves = {}
    for axis in theme.AXIS_ORDER:
        curves[axis] = widget.plot(
            pen=pg.mkPen(theme.AXIS_COLORS[axis], width=2),
            name=axis.upper(),
            antialias=True,
        )
    return widget, curves
