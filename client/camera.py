"""Camera Manager — owns the webcam device and yields frames.

Thin wrapper over OpenCV's VideoCapture so the rest of the client never talks
to OpenCV's device API directly.
"""

from __future__ import annotations

import cv2

from client.config import ClientConfig


def _fit_within(width: int, height: int,
                max_width: int, max_height: int) -> tuple[int, int]:
    """Scale (w, h) down to fit inside the max box, preserving aspect ratio.

    Never upscales (a 480p camera stays 480p). Result dimensions are forced
    even, which H.264 / yuv420p requires.
    """
    if width <= max_width and height <= max_height:
        out_w, out_h = width, height
    else:
        scale = min(max_width / width, max_height / height)
        out_w = int(width * scale)
        out_h = int(height * scale)
    return out_w - (out_w % 2), out_h - (out_h % 2)


class CameraManager:
    def __init__(self, config: ClientConfig):
        self._config = config
        self._capture: cv2.VideoCapture | None = None
        # Output size, capped to the configured max (e.g. 720p). Set on open().
        self._out_size: tuple[int, int] = (config.frame_width,
                                           config.frame_height)

    def open(self) -> None:
        cap = cv2.VideoCapture(self._config.camera_index)
        if not cap.isOpened():
            raise RuntimeError(
                f"could not open camera index {self._config.camera_index}")

        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self._config.frame_width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self._config.frame_height)
        cap.set(cv2.CAP_PROP_FPS, self._config.fps)
        self._capture = cap

        # Some cameras ignore the requested size and hand back their native
        # (e.g. 1080p) resolution. Cap the output to the configured maximum so
        # we never record — or upload — above 720p, whatever the camera does.
        raw_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        raw_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self._out_size = _fit_within(raw_w, raw_h,
                                     self._config.frame_width,
                                     self._config.frame_height)

    @property
    def actual_resolution(self) -> tuple[int, int]:
        """The size frames are delivered at — capped to the configured max."""
        self._require_open()
        return self._out_size

    def read_frame(self):
        """Return the next BGR frame (capped to 720p), or None on a hiccup."""
        cap = self._require_open()
        ok, frame = cap.read()
        if not ok:
            return None
        # Downscale oversized cameras to the cap. INTER_AREA is the best filter
        # for shrinking. Frames already at/below the cap pass through untouched.
        if (frame.shape[1], frame.shape[0]) != self._out_size:
            frame = cv2.resize(frame, self._out_size,
                               interpolation=cv2.INTER_AREA)
        return frame

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
