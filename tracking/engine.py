"""One-frame vision pipeline used by the live worker and the full-video export."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from control.speed_estimator import SpeedEstimator
from control.virtual_flight_controller import CommandBuilder
from detection.yolo_v4_detector import YoloV4Detector
from tracking.target_selector import TargetSelector
from utils.timers import LapTimer
from visualization.renderer import render_debug, render_frame


@dataclass
class PipelineConfig:
    confidence: float = 0.2
    nms: float = 0.4
    device: str = "AUTO"
    target_mode: str = "AUTO_CENTER"
    algorithm: str = "ENHANCED"
    dead_zone: float = 0.20
    smoothing: str = "ORIGINAL_20"
    pixel_scale: float = 0.1
    trail_length: int = 60
    lost_timeout: float = 1.5
    auto_reacquire: bool = True
    detection_scale: float = 1.0
    control_display: str = "DIRECTIONAL"
    overlays: dict = field(default_factory=dict)
    source_fps: float = 0.0
    proc_fps: float = 0.0
    show_debug: bool = False


class VisionEngine:
    def __init__(self) -> None:
        self.detector = YoloV4Detector()
        self.selector = TargetSelector()
        self.commands = CommandBuilder()
        self.speed = SpeedEstimator()
        self.frame_id = 0
        self._device = ""
        self._last_target_key: tuple | None = None

    def load(self, device: str = "AUTO") -> str:
        self._device = device
        return self.detector.load(device)

    def reset(self) -> None:
        self.selector.reset()
        self.speed.reset()
        self.frame_id = 0
        self._last_target_key = None

    def step(
        self,
        frame,
        config: PipelineConfig,
        click_xy: tuple[float, float] | None = None,
        recording: bool = False,
        rec_label: str = "",
    ) -> tuple[object, dict]:
        timer = LapTimer()
        if config.device != self._device:
            self.detector.set_device(config.device)
            self._device = config.device
        detections, raw_count = self.detector.detect(
            frame,
            confidence=config.confidence,
            nms=config.nms,
            detection_scale=config.detection_scale,
            count_raw=config.show_debug,
        )
        det_ms = timer.lap_ms()
        self.frame_id += 1
        height, width = frame.shape[:2]
        state = self.selector.update(
            detections,
            width,
            height,
            self.frame_id,
            config.dead_zone,
            click_xy,
            config.target_mode,
            config.algorithm,
            config.smoothing,
            config.lost_timeout,
            config.auto_reacquire,
            config.trail_length,
        )
        state.raw_detection_count = raw_count
        active = state.target_id is not None and state.status not in ("LOST", "SEARCHING", "DETECTED")
        if state.status == "REACQUIRED":
            active = True
        packet = self.commands.build(state, config.pixel_scale, active)
        now = time.perf_counter()
        speed_cmd, trend = self.speed.update(
            now,
            state.raw_xy[1] if active and state.raw_xy else None,
            state.area_ratio if active else None,
            active,
        )
        state.scale_trend = trend
        packet["speed"] = speed_cmd
        track_ms = timer.lap_ms()
        view = render_frame(
            frame,
            state,
            packet,
            config,
            self.detector.backend_label,
            recording=recording,
            rec_label=rec_label,
        )
        render_ms = timer.lap_ms()
        info = self._info(state, packet, config, det_ms, track_ms, render_ms, raw_count, len(detections))
        if config.show_debug or (config.overlays or {}).get("debug", False):
            render_debug(view, info)
        self._last_target_key = (state.status, state.target_id)
        return view, info

    def _info(self, state, packet, config: PipelineConfig, det_ms, track_ms, render_ms, raw_count, nms_count) -> dict:
        return {
            "frame_id": state.frame_id,
            "frame_w": state.frame_w,
            "frame_h": state.frame_h,
            "detected_count": nms_count,
            "raw_detections": raw_count,
            "nms_detections": nms_count,
            "confidence_threshold": config.confidence,
            "nms_threshold": config.nms,
            "backend": self.detector.backend_label,
            "detector": "YOLOv4-tiny",
            "yolo_input": "416x416",
            "model_name": self.detector.manager.display_name,
            "model_status": self.detector.manager.status_text,
            "target_id": state.target_id,
            "target_label": state.target_label,
            "target_conf": state.confidence,
            "status": state.status,
            "command": packet["command"],
            "speed": packet["speed"],
            "x_offset": packet["raw_offset_x"],
            "y_offset": packet["raw_offset_y"],
            "filtered_x": packet["filtered_offset_x"],
            "filtered_y": packet["filtered_offset_y"],
            "x_norm": packet["x_norm"],
            "y_norm": packet["y_norm"],
            "virtual_n": packet["virtual_n"],
            "virtual_e": packet["virtual_e"],
            "target_size": state.area_ratio * 100.0,
            "scale_trend": state.scale_trend,
            "dead_zone": packet["dead_zone"],
            "control_state": packet["control_state"],
            "centering_error": packet["centering_error"],
            "magnitude": packet["magnitude"],
            "dx": packet["dx"],
            "dy": packet["dy"],
            "raw_xy": state.raw_xy,
            "filtered_xy": state.filtered_xy,
            "det_ms": det_ms,
            "track_ms": track_ms,
            "render_ms": render_ms,
            "total_ms": det_ms + track_ms + render_ms,
            "backend_name": "VIRTUAL",
            "pixel_scale": config.pixel_scale,
            "algorithm": config.algorithm,
            "target_mode": config.target_mode,
        }
