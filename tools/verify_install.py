"""Checks used by install.bat. CUDA is optional and must not fail the install."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import cv2
import numpy as np

from detection.yolo_v4_detector import YoloV4Detector
from recording.video_encoder import open_writer
from utils.paths import (
    CFG_PATH,
    CLASSES_PATH,
    OUTPUT_DIR,
    RECORDINGS_DIR,
    SAMPLES_DIR,
    SCREENSHOTS_DIR,
    WEIGHTS_PATH,
    ensure_dirs,
    model_status,
)


def main() -> int:
    ensure_dirs()
    print("Python", sys.version.split()[0])
    print("OpenCV", cv2.__version__)
    major = int(cv2.__version__.split(".")[0])
    if major != 4:
        print("OpenCV 4.x is required. Darknet weights do not load on OpenCV 5.")
        return 1

    ok, message = model_status()
    print("Model:", message)
    if not ok:
        print("Expected files:")
        print(" ", WEIGHTS_PATH)
        print(" ", CFG_PATH)
        print(" ", CLASSES_PATH)
        return 1

    detector = YoloV4Detector()
    backend = detector.load("AUTO")
    print("Inference backend:", backend)
    if not backend:
        print("The network did not load.")
        return 1

    images = sorted((SAMPLES_DIR / "images").glob("*.jpg"))
    if not images:
        print("No sample images in samples/images")
        return 1
    detections = 0
    sample = None
    for path in images:
        frame = cv2.imread(str(path))
        if frame is None:
            continue
        if sample is None:
            sample = frame
        found, _raw = detector.detect(frame, 0.2, 0.4)
        detections += len(found)
    print(f"Detections on sample images: {detections}")
    if detections < 1:
        print("The model loaded but did not return any car detections.")
        return 1

    video_path = SAMPLES_DIR / "sample_drone.mp4"
    if sample is None:
        print("Could not read a sample image.")
        return 1
    _write_sample_video(video_path, sample)
    cap = cv2.VideoCapture(str(video_path))
    opened = cap.isOpened()
    ok, frame = cap.read()
    cap.release()
    print("Sample video:", "opened" if opened and ok else "FAILED", video_path.name)
    if not opened or not ok or frame is None:
        return 1

    test_path = OUTPUT_DIR / "_writer_test.mp4"
    writer, codec, size, fps = open_writer(str(test_path), 20.0, (sample.shape[0], sample.shape[1]))
    for _ in range(10):
        writer.write(cv2.resize(sample, size))
    writer.release()
    check = cv2.VideoCapture(str(test_path))
    readable = check.isOpened() and check.read()[0]
    check.release()
    test_path.unlink(missing_ok=True)
    print(f"VideoWriter codec {codec} @ {fps:.1f} fps:", "ok" if readable else "FAILED")
    if not readable:
        return 1

    for folder in (RECORDINGS_DIR, SCREENSHOTS_DIR, OUTPUT_DIR, ROOT / "logs" / "control"):
        folder.mkdir(parents=True, exist_ok=True)
        print("Folder", folder.relative_to(ROOT))
    print("Install check passed.")
    return 0


def _write_sample_video(path: Path, frame: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    writer, _codec, size, _fps = open_writer(str(path), 20.0, (frame.shape[0], frame.shape[1]))
    height, width = frame.shape[:2]
    for index in range(80):
        shift = int(round((index % 40) - 20))
        moved = np.zeros_like(frame)
        x0 = max(0, shift)
        y0 = max(0, shift // 2)
        src_x = max(0, -shift)
        src_y = max(0, -(shift // 2))
        copy_w = width - abs(shift)
        copy_h = height - abs(shift // 2)
        if copy_w > 10 and copy_h > 10:
            moved[y0:y0 + copy_h, x0:x0 + copy_w] = frame[src_y:src_y + copy_h, src_x:src_x + copy_w]
        else:
            moved = frame
        fitted = cv2.resize(moved, size) if (moved.shape[1], moved.shape[0]) != size else moved
        writer.write(fitted)
    writer.release()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("Install check failed:", exc)
        raise
