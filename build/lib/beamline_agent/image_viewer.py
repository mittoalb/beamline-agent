"""Lightweight image viewer for the AI agent to show PNGs, TIFFs,
plots, and other on-disk images inline.

**Not exposed on the pystream GUI toolbar.** The dialog exists only
when the agent's `view_image` tool creates it. Rationale: the human
already has plenty of ways to open an image (double-click in the file
manager, drag into a viewer, `xdg-open`); the agent doesn't. This
tool closes that gap without cluttering the toolbar.

Supported formats:
- Grayscale + colour PNG / JPG / BMP / GIF (via PIL)
- Single- and multi-page TIFF (via tifffile)
- NumPy .npy (2D or 3D arrays)
- Anything else PIL can decode (fallback)

Uses `pyqtgraph.ImageView` internally — inherits zoom, pan,
histogram-based contrast control, and multi-slice sliders for
stacks (multi-page TIFF, 3D NPY) for free. No new deps beyond what
pystream already needs.

Sized to be visible but not overwhelming (720 × 560). Non-modal, so
the user can keep working in pystream while an agent-produced plot
sits open on a second monitor.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import numpy as np
from PyQt5 import QtCore, QtGui, QtWidgets

LOGGER = logging.getLogger(__name__)


# ── dark theme (matches Agents / Console dialogs) ────────────────────
_BG_WINDOW = "#000000"
_BORDER    = "#2a2a2a"
_TEXT      = "#e5e5e5"
_TEXT_DIM  = "#8a8a8a"

_QSS = f"""
QDialog {{ background: {_BG_WINDOW}; color: {_TEXT}; }}
QLabel {{ color: {_TEXT}; }}
QLabel#footer {{ color: {_TEXT_DIM}; font-size: 10px; }}
QToolButton, QPushButton {{
    background: #111;
    color: {_TEXT};
    border: 1px solid {_BORDER};
    border-radius: 3px;
    padding: 3px 8px;
}}
QToolButton:hover, QPushButton:hover {{ background: #1a1a1a; border-color: #444; }}
QScrollBar:vertical {{ background: {_BG_WINDOW}; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{
    background: {_BORDER}; border-radius: 3px; min-height: 20px;
}}
QScrollBar::handle:vertical:hover {{ background: #3a3a3a; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
"""


def _load_image_array(path: str) -> np.ndarray:
    """Read an image file into a numpy array. Returns 2D (H,W) grayscale,
    3D (H,W,3) or (H,W,4) colour, or 3D stack (N,H,W). Raises IOError
    on decode failure."""
    ext = os.path.splitext(path)[1].lower()

    # Multi-page TIFF stacks + 16-bit tiffs are best handled by tifffile.
    if ext in (".tif", ".tiff"):
        try:
            import tifffile  # optional dep — used elsewhere in pystream too
            return np.asarray(tifffile.imread(path))
        except ImportError:
            pass  # fall through to PIL

    # NumPy arrays saved as .npy — common for agent-produced plot data
    if ext == ".npy":
        return np.asarray(np.load(path))

    # PNG / JPG / BMP / GIF / anything PIL knows about
    from PIL import Image
    img = Image.open(path)
    # Ensure the image is loaded (PIL is lazy)
    img.load()
    arr = np.asarray(img)
    # PIL returns (H,W) grayscale or (H,W,3|4) colour — pyqtgraph accepts both.
    return arr


class ImageViewerDialog(QtWidgets.QDialog):
    """Standalone non-modal window rendering an image on disk. Built
    on top of pyqtgraph.ImageView so zoom / pan / histogram / stack-
    slider come for free.

    Constructor loads the file synchronously — expected to be fast
    (agent-produced plots are typically < 2 MB PNGs). For large
    volumes, prefer `view_hdf5_file` instead (which memory-maps)."""

    def __init__(self, path: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Image — {os.path.basename(path)}")
        self.setWindowFlags(
            QtCore.Qt.Window
            | QtCore.Qt.WindowMinimizeButtonHint
            | QtCore.Qt.WindowMaximizeButtonHint
            | QtCore.Qt.WindowCloseButtonHint
        )
        self.setModal(False)
        self.resize(720, 560)
        self.setStyleSheet(_QSS)
        self._path = path

        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(6, 6, 6, 6)
        v.setSpacing(4)

        # Header row: path + shape/dtype badge + close
        hdr = QtWidgets.QHBoxLayout()
        self._path_lbl = QtWidgets.QLabel(f"<b>{os.path.basename(path)}</b>")
        self._path_lbl.setToolTip(path)
        hdr.addWidget(self._path_lbl)
        self._info_lbl = QtWidgets.QLabel("")
        self._info_lbl.setStyleSheet(f"color: {_TEXT_DIM}; font-size: 11px;")
        hdr.addWidget(self._info_lbl)
        hdr.addStretch(1)
        close_btn = QtWidgets.QToolButton()
        close_btn.setText("close")
        close_btn.clicked.connect(self.close)
        hdr.addWidget(close_btn)
        v.addLayout(hdr)

        # Load + render
        try:
            arr = _load_image_array(path)
        except Exception as e:
            err = QtWidgets.QLabel(
                f"<b>Failed to load {path}:</b><br>{type(e).__name__}: {e}")
            err.setStyleSheet("color: #f87171; padding: 20px;")
            err.setWordWrap(True)
            v.addWidget(err, 1)
            LOGGER.warning("ImageViewerDialog: load failed for %s: %s",
                           path, e)
            self._info_lbl.setText("(load failed)")
            self._add_footer(v, path)
            return

        # pyqtgraph.ImageView handles zoom/pan/histogram/slider naturally.
        # Import here rather than at module top so this file can still
        # be imported in envs where pyqtgraph is not installed — it
        # errors at open time only.
        import pyqtgraph as pg
        pg.setConfigOptions(imageAxisOrder="row-major")

        iv = pg.ImageView(parent=self)
        iv.ui.roiBtn.setVisible(False)
        iv.ui.menuBtn.setVisible(False)
        # setImage semantics:
        #   2D (H,W)          → single image
        #   3D (N,H,W)        → stack, slider on axis 0
        #   3D (H,W,3|4)      → colour image
        #   pyqtgraph auto-detects colour vs stack for 3D.
        try:
            iv.setImage(arr, autoRange=True, autoLevels=True,
                        autoHistogramRange=True)
        except Exception as e:
            LOGGER.warning("ImageViewerDialog: setImage failed: %s", e)
        v.addWidget(iv, 1)
        self._iv = iv

        # Shape / dtype badge
        shape_str = " × ".join(str(d) for d in arr.shape)
        info = f"shape={shape_str} · dtype={arr.dtype}"
        try:
            info += (f" · range=[{np.nanmin(arr):.4g}, "
                     f"{np.nanmax(arr):.4g}]")
        except Exception:
            pass
        self._info_lbl.setText(info)

        self._add_footer(v, path)

    def _add_footer(self, layout: QtWidgets.QVBoxLayout, path: str) -> None:
        ftr = QtWidgets.QLabel(path)
        ftr.setObjectName("footer")
        ftr.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        layout.addWidget(ftr)


def open_image_viewer(path: str, parent=None) -> Optional[ImageViewerDialog]:
    """Factory that instantiates + shows the dialog. Returns the
    dialog (kept alive as long as `parent` — the caller doesn't
    have to store the reference)."""
    dlg = ImageViewerDialog(path, parent=parent)
    dlg.show()
    dlg.raise_()
    dlg.activateWindow()
    return dlg
