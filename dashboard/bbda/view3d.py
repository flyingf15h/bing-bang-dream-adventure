"""Real-time 3D orientation view.

Renders the board as a solid PCB-shaped slab inside a fixed world frame, so
the viewer sees the board move against a stationary ground plane rather than
the more confusing inverse. On top of the board sit:

* a body-axis triad, coloured and labelled X / Y / Z;
* the measured gravity vector, which should point at the floor;
* the measured magnetic field vector, which should point at magnetic north
  and stay put while the board turns.

Those last two are the whole point of drawing this in 3D: a calibration
problem that is invisible in a line plot is obvious the moment the magnetic
vector wobbles as you rotate the board.
"""

from __future__ import annotations

import numpy as np
import pyqtgraph as pg
import pyqtgraph.opengl as gl

from . import theme

# Board outline in millimetres. One scene unit is 10 mm, so a 25 mm board is
# 2.5 units long and sits comfortably inside the 2.6-unit axis arms.
BOARD_MM = (25.0, 18.0, 1.6)
WORLD_SCALE = 0.1  # mm -> scene units


def _hex_to_rgba(color: str, alpha: float = 1.0) -> tuple[float, float, float, float]:
    color = color.lstrip("#")
    r, g, b = (int(color[i : i + 2], 16) / 255.0 for i in (0, 2, 4))
    return (r, g, b, alpha)


def _box_mesh(size: tuple[float, float, float]) -> gl.MeshData:
    """Solid axis-aligned box centred on the origin."""
    sx, sy, sz = (s / 2.0 for s in size)
    verts = np.array(
        [
            [-sx, -sy, -sz], [+sx, -sy, -sz], [+sx, +sy, -sz], [-sx, +sy, -sz],
            [-sx, -sy, +sz], [+sx, -sy, +sz], [+sx, +sy, +sz], [-sx, +sy, +sz],
        ]
    )
    faces = np.array(
        [
            [0, 1, 2], [0, 2, 3],  # bottom
            [4, 6, 5], [4, 7, 6],  # top
            [0, 5, 1], [0, 4, 5],  # -Y
            [2, 6, 7], [2, 7, 3],  # +Y
            [1, 6, 2], [1, 5, 6],  # +X
            [0, 3, 7], [0, 7, 4],  # -X
        ]
    )
    return gl.MeshData(vertexes=verts, faces=faces)


class OrientationView(gl.GLViewWidget):
    """3D board view driven by a rotation matrix."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setBackgroundColor(pg.mkColor(theme.SURFACE))
        self.setCameraPosition(distance=9.0, elevation=22, azimuth=35)

        self._build_world()
        self._build_board()
        self._build_vectors()

        self._rotation = np.eye(3)
        self._show_gravity = True
        self._show_mag = True
        self._show_trail = True

        # Breadcrumb of where the board's +X axis has pointed recently. It
        # makes slow yaw drift visible, which a static pose cannot show.
        self._trail: list[np.ndarray] = []
        self._trail_item = gl.GLLinePlotItem(
            pos=np.zeros((2, 3)),
            color=_hex_to_rgba(theme.INK_MUTED, 0.55),
            width=1.5,
            antialias=True,
            mode="line_strip",
        )
        self.addItem(self._trail_item)

    # ------------------------------------------------------------------
    # Scene construction
    # ------------------------------------------------------------------
    def _build_world(self) -> None:
        grid = gl.GLGridItem()
        grid.setSize(x=12, y=12)
        grid.setSpacing(x=1, y=1)
        grid.setColor(pg.mkColor(theme.GRIDLINE))
        grid.translate(0, 0, -2.2)
        self.addItem(grid)

        # World axes, drawn thin and muted so they never compete with the
        # board's own triad. North is +X, up is +Z.
        for direction, length in (((1, 0, 0), 5.0), ((0, 1, 0), 5.0), ((0, 0, 1), 3.0)):
            pts = np.array([[0, 0, -2.2], np.array(direction) * length + [0, 0, -2.2]])
            item = gl.GLLinePlotItem(
                pos=pts,
                color=_hex_to_rgba(theme.BASELINE, 0.9),
                width=1.0,
                antialias=True,
            )
            self.addItem(item)

    def _build_board(self) -> None:
        size = tuple(v * WORLD_SCALE for v in BOARD_MM)
        mesh = _box_mesh(size)

        self._board = gl.GLMeshItem(
            meshdata=mesh,
            smooth=False,
            color=_hex_to_rgba("#2f6f4f", 0.92),  # PCB solder mask green
            shader="shaded",
            glOptions="opaque",
        )
        self.addItem(self._board)

        # Wireframe edge so the slab keeps a readable silhouette against the
        # dark surface from every angle.
        self._board_edges = gl.GLMeshItem(
            meshdata=mesh,
            smooth=False,
            drawFaces=False,
            drawEdges=True,
            edgeColor=_hex_to_rgba(theme.INK_SECONDARY, 0.7),
        )
        self.addItem(self._board_edges)

        # A small marker block standing in for the IMU, offset from centre so
        # the board's orientation is unambiguous even when it is edge-on.
        chip = _box_mesh((0.45, 0.45, 0.25))
        self._chip = gl.GLMeshItem(
            meshdata=chip,
            smooth=False,
            color=_hex_to_rgba(theme.INK_PRIMARY, 0.85),
            shader="shaded",
            glOptions="opaque",
        )
        self.addItem(self._chip)

        # Body-axis triad.
        self._axes: dict[str, gl.GLLinePlotItem] = {}
        for axis in theme.AXIS_ORDER:
            item = gl.GLLinePlotItem(
                pos=np.zeros((2, 3)),
                color=_hex_to_rgba(theme.AXIS_COLORS[axis]),
                width=3.0,
                antialias=True,
            )
            self.addItem(item)
            self._axes[axis] = item

        self._axis_labels: dict[str, gl.GLTextItem] = {}
        for axis in theme.AXIS_ORDER:
            label = gl.GLTextItem(
                pos=np.zeros(3),
                text=axis.upper(),
                color=pg.mkColor(theme.AXIS_COLORS[axis]),
            )
            self.addItem(label)
            self._axis_labels[axis] = label

    def _build_vectors(self) -> None:
        self._gravity = gl.GLLinePlotItem(
            pos=np.zeros((2, 3)),
            color=_hex_to_rgba(theme.INK_SECONDARY, 0.9),
            width=2.0,
            antialias=True,
        )
        self.addItem(self._gravity)
        self._gravity_label = gl.GLTextItem(
            pos=np.zeros(3), text="g", color=pg.mkColor(theme.INK_SECONDARY)
        )
        self.addItem(self._gravity_label)

        self._mag = gl.GLLinePlotItem(
            pos=np.zeros((2, 3)),
            color=_hex_to_rgba(theme.STATUS["warning"], 0.9),
            width=2.0,
            antialias=True,
        )
        self.addItem(self._mag)
        self._mag_label = gl.GLTextItem(
            pos=np.zeros(3), text="B", color=pg.mkColor(theme.STATUS["warning"])
        )
        self.addItem(self._mag_label)

    # ------------------------------------------------------------------
    # Options
    # ------------------------------------------------------------------
    def set_show_gravity(self, on: bool) -> None:
        self._show_gravity = on
        self._gravity.setVisible(on)
        self._gravity_label.setVisible(on)

    def set_show_mag(self, on: bool) -> None:
        self._show_mag = on
        self._mag.setVisible(on)
        self._mag_label.setVisible(on)

    def set_show_trail(self, on: bool) -> None:
        self._show_trail = on
        self._trail_item.setVisible(on)
        if not on:
            self._trail.clear()

    def reset_view(self) -> None:
        self.setCameraPosition(distance=9.0, elevation=22, azimuth=35)

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------
    def update_orientation(
        self,
        rotation: np.ndarray,
        accel_body: np.ndarray | None = None,
        mag_body: np.ndarray | None = None,
    ) -> None:
        """Redraw for a new world-from-body rotation matrix.

        ``accel_body`` and ``mag_body`` are the raw sensor readings in board
        axes; they are rotated into the world here so that a correct
        calibration makes them hold still while the board moves.
        """
        self._rotation = np.asarray(rotation, dtype=float)

        matrix = np.eye(4)
        matrix[:3, :3] = self._rotation
        transform = pg.Transform3D(matrix)

        self._board.setTransform(transform)
        self._board_edges.setTransform(transform)

        chip_transform = pg.Transform3D(transform)
        chip_transform.translate(0.7, 0.35, 0.2)
        self._chip.setTransform(chip_transform)

        # Body axes, rotated into the world frame.
        for index, axis in enumerate(theme.AXIS_ORDER):
            direction = self._rotation[:, index]
            length = 2.6 if axis != "z" else 2.0
            end = direction * length
            self._axes[axis].setData(pos=np.array([[0, 0, 0], end]))
            self._axis_labels[axis].setData(pos=end * 1.12)

        if accel_body is not None and self._show_gravity:
            world = self._rotation @ np.asarray(accel_body, dtype=float)
            magnitude = np.linalg.norm(world)
            if magnitude > 1e-6:
                end = world / magnitude * 2.2
                self._gravity.setData(pos=np.array([[0, 0, 0], end]))
                self._gravity_label.setData(pos=end * 1.1)

        if mag_body is not None and self._show_mag:
            world = self._rotation @ np.asarray(mag_body, dtype=float)
            magnitude = np.linalg.norm(world)
            if magnitude > 1e-6:
                end = world / magnitude * 3.0
                self._mag.setData(pos=np.array([[0, 0, 0], end]))
                self._mag_label.setData(pos=end * 1.08)

        if self._show_trail:
            tip = self._rotation[:, 0] * 2.6
            if not self._trail or np.linalg.norm(tip - self._trail[-1]) > 0.05:
                self._trail.append(tip)
                if len(self._trail) > 400:
                    self._trail.pop(0)
                if len(self._trail) >= 2:
                    self._trail_item.setData(pos=np.array(self._trail))


class MagScatterView(gl.GLViewWidget):
    """Point cloud of magnetometer samples, used by the calibration tab.

    Raw samples form an off-centre, squashed shell; corrected samples form a
    sphere centred on the origin. Showing both at once is the clearest
    possible statement of what the calibration did.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setBackgroundColor(pg.mkColor(theme.SURFACE))
        self.setCameraPosition(distance=180, elevation=20, azimuth=45)

        grid = gl.GLGridItem()
        grid.setSize(x=200, y=200)
        grid.setSpacing(x=20, y=20)
        grid.setColor(pg.mkColor(theme.GRIDLINE))
        self.addItem(grid)

        self._raw = gl.GLScatterPlotItem(
            pos=np.zeros((0, 3)),
            color=_hex_to_rgba(theme.AXIS_COLORS["y"], 0.75),
            size=4.0,
        )
        self.addItem(self._raw)

        self._corrected = gl.GLScatterPlotItem(
            pos=np.zeros((0, 3)),
            color=_hex_to_rgba(theme.AXIS_COLORS["z"], 0.85),
            size=4.0,
        )
        self.addItem(self._corrected)

        self._origin = gl.GLScatterPlotItem(
            pos=np.zeros((1, 3)),
            color=_hex_to_rgba(theme.INK_PRIMARY, 1.0),
            size=9.0,
        )
        self.addItem(self._origin)

    def set_points(self, raw: np.ndarray, corrected: np.ndarray | None = None) -> None:
        raw = np.asarray(raw, dtype=float)
        self._raw.setData(pos=raw if raw.size else np.zeros((0, 3)))
        if corrected is not None and len(corrected):
            self._corrected.setData(pos=np.asarray(corrected, dtype=float))
        else:
            self._corrected.setData(pos=np.zeros((0, 3)))

        if raw.size:
            extent = float(np.abs(raw).max()) * 2.4
            self.setCameraPosition(distance=max(extent, 60.0))

    def clear_points(self) -> None:
        self._raw.setData(pos=np.zeros((0, 3)))
        self._corrected.setData(pos=np.zeros((0, 3)))


class TrajectoryView(gl.GLViewWidget):
    """3D path the board has travelled, with the board drawn at the tip.

    Scaled in metres. The grid squares are 10 cm, which gives an immediate
    sense of whether the drift is centimetres or metres.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setBackgroundColor(pg.mkColor(theme.SURFACE))
        self.setCameraPosition(distance=1.6, elevation=28, azimuth=45)

        grid = gl.GLGridItem()
        grid.setSize(x=2.0, y=2.0)
        grid.setSpacing(x=0.1, y=0.1)
        grid.setColor(pg.mkColor(theme.GRIDLINE))
        self.addItem(grid)

        self._path = gl.GLLinePlotItem(
            pos=np.zeros((2, 3)),
            color=_hex_to_rgba(theme.AXIS_COLORS["x"], 0.95),
            width=2.0,
            antialias=True,
            mode="line_strip",
        )
        self.addItem(self._path)

        self._origin = gl.GLScatterPlotItem(
            pos=np.zeros((1, 3)), color=_hex_to_rgba(theme.INK_MUTED, 1.0), size=8.0
        )
        self.addItem(self._origin)

        self._here = gl.GLScatterPlotItem(
            pos=np.zeros((1, 3)), color=_hex_to_rgba(theme.STATUS["good"], 1.0), size=12.0
        )
        self.addItem(self._here)

        # A small triad at the current position shows attitude along the path.
        self._axes: dict[str, gl.GLLinePlotItem] = {}
        for axis in theme.AXIS_ORDER:
            item = gl.GLLinePlotItem(
                pos=np.zeros((2, 3)),
                color=_hex_to_rgba(theme.AXIS_COLORS[axis]),
                width=2.5,
                antialias=True,
            )
            self.addItem(item)
            self._axes[axis] = item

    def update_path(self, path: np.ndarray, position: np.ndarray, rotation: np.ndarray) -> None:
        path = np.asarray(path, dtype=float)
        if len(path) >= 2:
            self._path.setData(pos=path)
        self._here.setData(pos=position.reshape(1, 3))

        arm = 0.05  # 5 cm triad
        for index, axis in enumerate(theme.AXIS_ORDER):
            end = position + rotation[:, index] * arm
            self._axes[axis].setData(pos=np.array([position, end]))

        # Keep the whole path in frame without fighting the user's zoom too
        # hard: only widen, never narrow.
        if len(path) >= 2:
            extent = float(np.abs(path).max()) * 3.0
            if extent > self.opts["distance"]:
                self.setCameraPosition(distance=extent)

    def clear_path(self) -> None:
        self._path.setData(pos=np.zeros((2, 3)))
        self._here.setData(pos=np.zeros((1, 3)))
