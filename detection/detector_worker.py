"""Detection thread: latest frame in, annotated frame and telemetry out."""

from __future__ import annotations

import queue
import threading
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QImage

from capture.source_manager import FrameHub
from export.control_csv import ControlCsvLog
from recording.recorder import RecordingWorker
from tracking.engine import PipelineConfig, VisionEngine
from utils.logger import get_logger
from utils.paths import CONTROL_LOG_DIR, OUTPUT_DIR, SCREENSHOTS_DIR
from utils.timers import FpsMeter


log = get_logger()


def bgr_to_qimage(frame: np.ndarray) -> QImage:
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    rgb = np.ascontiguousarray(rgb)
    height, width, channels = rgb.shape
    image = QImage(rgb.data, width, height, channels * width, QImage.Format.Format_RGB888)
    return image.copy()


class DetectionWorker(QThread):
    frame_ready = Signal(object)
    state_ready = Signal(dict)
    model_ready = Signal(str, str)
    model_failed = Signal(str)
    screenshot_saved = Signal(str)
    csv_saved = Signal(str, int)
    export_progress = Signal(int, int)
    export_finished = Signal(str)
    export_failed = Signal(str)

    def __init__(self, hub: FrameHub, recorder: RecordingWorker) -> None:
        super().__init__()
        self.hub = hub
        self.recorder = recorder
        self.engine = VisionEngine()
        self.csv_log = ControlCsvLog()
        self._config = PipelineConfig()
        self._lock = threading.Lock()
        self._commands: queue.Queue = queue.Queue()
        self._stop = False
        self._last_seq = -1
        self._last_view = None
        self._prev_status = ""
        self._prev_target = None
        self._proc_fps = FpsMeter()
        self._display_fps = FpsMeter()
        self._source_fps = 0.0
        self._exporting = False
        self.playing = False
        self._exporting = False

    def stop(self) -> None:
        self._stop = True
        self._commands.put(("noop", None))

    def set_config(self, config: PipelineConfig) -> None:
        with self._lock:
            self._config = config

    def set_playing(self, playing: bool) -> None:
        self.playing = playing

    def request_click(self, x: float, y: float) -> None:
        self._commands.put(("click", (x, y)))

    def request_reset(self) -> None:
        self._commands.put(("reset", None))

    def request_cycle(self, step: int) -> None:
        self._commands.put(("cycle", step))

    def request_clear_target(self) -> None:
        self._commands.put(("clear", None))

    def request_screenshot(self) -> None:
        self._commands.put(("shot", None))

    def request_csv(self) -> None:
        self._commands.put(("csv", None))

    def request_export(self, video_path: str) -> None:
        self._commands.put(("export", video_path))

    def run(self) -> None:
        try:
            with self._lock:
                device = self._config.device
            label = self.engine.load(device)
            self.model_ready.emit(self.engine.detector.manager.status_text, label)
        except Exception as exc:
            log.info("Detector load failed: %s", exc)
            self.model_failed.emit(str(exc))
            return
        pending_click = None
        while not self._stop:
            pending_click = self._drain(pending_click)
            if self._exporting:
                continue
            seq, frame, meta = self.hub.latest()
            if frame is None or seq == self._last_seq:
                if self.recorder.recorder.active and self._last_view is not None:
                    self.recorder.submit_frame(self._last_view)
                self.msleep(8)
                continue
            self._last_seq = seq
            try:
                self._process(frame, meta, pending_click)
            except Exception as exc:
                log.info("Frame processing error: %s", exc)
            pending_click = None
            self.hub.ack(seq)
        log.info("Detection worker stopped")

    def __init_export_flag(self) -> None:
        self._exporting = False

    def _drain(self, pending_click):
        if not hasattr(self, "_exporting"):
            self._exporting = False
        while True:
            try:
                name, payload = self._commands.get_nowait()
            except queue.Empty:
                return pending_click
            if name == "click":
                pending_click = payload
            elif name == "reset":
                self.engine.reset()
                self._prev_status = ""
                self._prev_target = None
            elif name == "cycle":
                self.engine.selector.cycle(int(payload))
            elif name == "clear":
                self.engine.selector.clear_target()
                log.info("Target cleared")
            elif name == "shot":
                self._save_screenshot()
            elif name == "csv":
                self._save_csv()
            elif name == "export":
                self._export_video(str(payload))
            elif name == "noop":
                continue
        return pending_click

    def _config_now(self) -> PipelineConfig:
        with self._lock:
            return self._config

    def _process(self, frame, meta: dict, click) -> None:
        config = self._config_now()
        config.source_fps = float(meta.get("fps") or config.source_fps or 0.0)
        config.proc_fps = self._proc_fps.value
        self._source_fps = config.source_fps
        config.show_debug = bool((config.overlays or {}).get("debug", False))
        recording = self.recorder.recorder.active
        remaining = self.recorder.recorder.remaining if recording else 0.0
        rec_label = f"REC 00:{int(round(remaining)):02d}" if recording else ""
        view, info = self.engine.step(frame, config, click_xy=click, recording=recording, rec_label=rec_label)
        self._last_view = view
        proc = self._proc_fps.tick()
        disp = self._display_fps.tick()
        info["proc_fps"] = proc
        info["display_fps"] = disp
        info["source_fps"] = self._source_fps
        info["dropped"] = self.hub.dropped()
        info["filename"] = meta.get("filename", "")
        self._log_transitions(info)
        self._append_csv(info)
        self.state_ready.emit(info)
        self.frame_ready.emit(bgr_to_qimage(view))
        if recording or not self.recorder._pending.empty():
            self.recorder.submit_frame(view)

    def _log_transitions(self, info: dict) -> None:
        status = info.get("status") or ""
        target = info.get("target_id")
        if target != self._prev_target and target:
            log.info("Target selected | %s", info.get("target_label"))
        if status != self._prev_status:
            if status == "LOST":
                log.info("Target lost | %s", info.get("target_label"))
            elif status == "REACQUIRED":
                log.info("Target reacquired | %s", info.get("target_label"))
        self._prev_status = status
        self._prev_target = target

    def _append_csv(self, info: dict) -> None:
        raw = info.get("raw_xy") or (0.0, 0.0)
        filt = info.get("filtered_xy") or (0.0, 0.0)
        self.csv_log.add(
            {
                "timestamp": f"{time.time():.3f}",
                "frame": info.get("frame_id", ""),
                "target_id": info.get("target_label") or "",
                "target_x": f"{raw[0]:.1f}",
                "target_y": f"{raw[1]:.1f}",
                "x_offset": f"{info.get('x_offset', 0):.1f}",
                "y_offset": f"{info.get('y_offset', 0):.1f}",
                "filtered_x": f"{filt[0]:.1f}",
                "filtered_y": f"{filt[1]:.1f}",
                "target_size": f"{info.get('target_size', 0):.3f}",
                "direction_command": info.get("command", ""),
                "speed_command": info.get("speed", ""),
                "virtual_n": f"{info.get('virtual_n', 0):.3f}",
                "virtual_e": f"{info.get('virtual_e', 0):.3f}",
                "status": info.get("status", ""),
            }
        )

    def _save_screenshot(self) -> None:
        if self._last_view is None:
            return
        SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = SCREENSHOTS_DIR / f"vtol_tracking_{stamp}.png"
        cv2.imwrite(str(path), self._last_view)
        log.info("Screenshot saved | %s", path.name)
        self.screenshot_saved.emit(str(path))

    def _save_csv(self) -> None:
        CONTROL_LOG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = CONTROL_LOG_DIR / f"control_{stamp}.csv"
        count = self.csv_log.export(path)
        log.info("Control CSV exported | %s | %s rows", path.name, count)
        self.csv_saved.emit(str(path), count)

    def _export_video(self, video_path: str) -> None:
        self._exporting = True
        try:
            capture = cv2.VideoCapture(video_path)
            if not capture.isOpened():
                raise RuntimeError(f"Could not open {video_path}")
            fps = float(capture.get(cv2.CAP_PROP_FPS) or 30.0)
            if fps < 1 or fps > 240:
                fps = 30.0
            total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            out_path = OUTPUT_DIR / f"vtol_tracking_full_{stamp}.mp4"
            writer = None
            config = self._config_now()
            self.engine.reset()
            index = 0
            while not self._stop:
                ok, frame = capture.read()
                if not ok or frame is None:
                    break
                view, _info = self.engine.step(frame, config)
                if writer is None:
                    from recording.video_encoder import open_writer

                    writer, _codec, _size, _fps = open_writer(str(out_path), fps, (view.shape[1], view.shape[0]))
                fitted = view
                width, height = _size if writer else (view.shape[1], view.shape[0])
                if writer is not None and (view.shape[1] != width or view.shape[0] != height):
                    fitted = cv2.resize(view, (width, height))
                if writer is not None:
                    writer.write(fitted)
                index += 1
                if index % 5 == 0:
                    self.export_progress.emit(index, total)
            if writer is not None:
                writer.release()
            capture.release()
            self.engine.reset()
            log.info("Full video exported | %s", out_path.name)
            self.export_finished.emit(str(out_path))
        except Exception as exc:
            log.info("Full video export failed: %s", exc)
            self.export_failed.emit(str(exc))
        finally:
            self._exporting = False
