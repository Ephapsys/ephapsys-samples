#!/usr/bin/env python3

import os
import numpy as np
from dataclasses import dataclass, field
from typing import Any, List, Optional


@dataclass
class RobotStateStore:
    startup_vision_label: str = "-"
    latest_world_summary: str = "-"
    latest_scene_summary: str = "-"
    latest_camera_frame: Optional[object] = None
    prev_world_embedding: Optional[object] = None
    latest_expression: str = "neutral"
    latest_gaze: str = "center"
    stored_responses: List[str] = field(default_factory=list)
    language_warm_done: bool = False
    awaiting_tts_done: bool = False
    # Rolling frame buffer for temporally-smoothed pixel-diff motion scoring
    frame_buffer: List = field(default_factory=list)
    # Rolling world-embedding buffer for temporally-smoothed V-JEPA delta
    embedding_buffer: List = field(default_factory=list)
    # Recent activity scores for adaptive threshold calibration
    activity_history: List[float] = field(default_factory=list)
    # FAISS semantic memory index and parallel response list
    faiss_index: Optional[Any] = None
    faiss_responses: List[str] = field(default_factory=list)

    def append_response(self, text: str, limit: int = 8) -> None:
        self.stored_responses.append(text)
        self.stored_responses = self.stored_responses[-limit:]

    def latest_memory_context(self) -> str:
        if not self.stored_responses:
            return ""
        return f" Previously I said: {self.stored_responses[-1]}"

    def add_frame(self, frame, max_buffer: int = 8) -> None:
        """Add a raw camera frame to the rolling buffer."""
        self.frame_buffer.append(frame)
        self.frame_buffer = self.frame_buffer[-max_buffer:]

    def add_embedding(self, embedding, max_buffer: int = 8) -> None:
        """Add a world model embedding to the rolling buffer."""
        self.embedding_buffer.append(embedding)
        self.embedding_buffer = self.embedding_buffer[-max_buffer:]

    def update_activity(self, score: float, max_history: int = 20) -> None:
        """Record an activity score and trim history."""
        self.activity_history.append(score)
        self.activity_history = self.activity_history[-max_history:]

    def save(self, path: str) -> None:
        """Persist session state (responses + FAISS index) to disk."""
        import json
        import faiss as _faiss

        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, "responses.json"), "w") as fh:
            json.dump(
                {
                    "stored_responses": self.stored_responses,
                    "faiss_responses": self.faiss_responses,
                },
                fh,
            )
        if self.faiss_index is not None and self.faiss_index.ntotal > 0:
            _faiss.write_index(self.faiss_index, os.path.join(path, "memory.faiss"))

    @classmethod
    def load(cls, path: str) -> "RobotStateStore":
        """Restore session state from disk."""
        import json
        import faiss as _faiss

        store = cls()
        responses_path = os.path.join(path, "responses.json")
        if os.path.exists(responses_path):
            with open(responses_path) as fh:
                data = json.load(fh)
            store.stored_responses = data.get("stored_responses", [])
            store.faiss_responses = data.get("faiss_responses", [])
        faiss_path = os.path.join(path, "memory.faiss")
        if os.path.exists(faiss_path) and store.faiss_responses:
            try:
                store.faiss_index = _faiss.read_index(faiss_path)
            except Exception:
                pass  # corrupt index: start fresh
        return store

    def adaptive_thresholds(self):
        """Return (low, high) activity thresholds calibrated to recent history.

        Falls back to conservative defaults until at least 5 samples are
        available, then derives thresholds from the running mean and std so
        the robot self-calibrates to its deployment environment.
        """
        if len(self.activity_history) < 5:
            return 0.05, 0.16
        arr = np.array(self.activity_history, dtype="float32")
        mean, std = float(arr.mean()), float(arr.std())
        low = max(0.01, mean + 0.5 * std)
        high = max(low + 0.02, mean + 1.5 * std)
        return low, high
