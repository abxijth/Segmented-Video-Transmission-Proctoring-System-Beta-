"""Camera Manager — owns the webcam device and yields frames.

Thin wrapper over OpenCV's VideoCapture so the rest of the client never talks
to OpenCV's device API directly.
"""

from __future__ import annotations

import cv2

from client.config import ClientConfig


class CameraManager:
    def __init__(self, config: ClientConfig):
        self._config = config
        self._capture: cv2.VideoCapture | None = None

    def open(self) -> None:
        cap = cv2.VideoCapture(self._config.camera_index)
        if not cap.isOpened():
            raise RuntimeError(
                f"could not open camera index {self._config.camera_index}")

        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self._config.frame_width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self._config.frame_height)
        cap.set(cv2.CAP_PROP_FPS, self._config.fps)
        self._capture = cap

    @property
    def actual_resolution(self) -> tuple[int, int]:
        """The size the camera actually gave us (may differ from requested)."""
        cap = self._require_open()
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        return width, height

    def read_frame(self):
        """Return the next BGR frame, or None if the camera hiccuped."""
        cap = self._require_open()
        ok, frame = cap.read()
        return frame if ok else None

    def close(self) -> None:
        if self._capture is not None:
            self._capture.release()
            self._capture = None

    def _require_open(self) -> cv2.VideoCapture:
        if self._capture is None:
            raise RuntimeError("camera not open; call open() first")
        return self._capture

    def __enter__(self) -> "CameraManager":
        self.open()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()
