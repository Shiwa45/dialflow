"""
ai_dialer/online_learning.py

Online learning system that continuously improves models
from real dialing outcomes without full retraining.
"""

import numpy as np
import logging
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple
import time

logger = logging.getLogger(__name__)


@dataclass
class Experience:
    """
    One episode of dialer experience.

    Records: what state we were in, what ratio we used, what happened.
    Used for training and online learning.
    """
    features: np.ndarray         # State at decision time
    dial_ratio_used: float        # Action taken
    answer_rate_observed: float   # What actually happened (label for model 1)
    abandon_rate_observed: float  # What actually happened
    agent_utilization: float      # How busy were agents
    abandon_spike: bool           # Did abandon exceed threshold? (label for model 2)
    outcome_quality: float        # 0-1 composite score
    timestamp: float = field(default_factory=time.time)
    campaign_id: int = 0
    weight: float = 1.0           # Importance weight for training


class ExperienceReplayBuffer:
    """
    Prioritized experience replay buffer.

    Stores historical dialing episodes.
    Prioritizes sampling of:
    1. Recent experiences (recency bias)
    2. High-quality outcomes (success emphasis)
    3. Rare/unusual events (coverage)

    Used to:
    - Retrain models periodically
    - Online gradient updates
    """

    def __init__(
        self,
        max_size: int = 50_000,
        recency_weight: float = 0.7,
        quality_weight: float = 0.3,
    ):
        self.max_size = max_size
        self.recency_weight = recency_weight
        self.quality_weight = quality_weight

        self._buffer: Deque[Experience] = deque(maxlen=max_size)
        self._lock = threading.Lock()

    def add(self, experience: Experience):
        """Add experience to buffer"""
        with self._lock:
            self._buffer.append(experience)

    def sample(
        self,
        n: int,
        strategy: str = "prioritized",
    ) -> List[Experience]:
        """
        Sample N experiences from buffer.

        Strategies:
        - "prioritized": Weight by recency + quality
        - "random": Uniform random
        - "recent": Last N experiences
        - "best": Highest quality outcomes
        """
        with self._lock:
            buf = list(self._buffer)

        if len(buf) == 0:
            return []

        n = min(n, len(buf))

        if strategy == "random":
            indices = np.random.choice(len(buf), n, replace=False)
            return [buf[i] for i in indices]

        elif strategy == "recent":
            return buf[-n:]

        elif strategy == "best":
            sorted_buf = sorted(
                buf, key=lambda e: e.outcome_quality, reverse=True
            )
            return sorted_buf[:n]

        elif strategy == "prioritized":
            # Compute sampling weights
            weights = self._compute_priorities(buf)
            weights = weights / weights.sum()
            indices = np.random.choice(len(buf), n, replace=False, p=weights)
            return [buf[i] for i in indices]

        return buf[-n:]

    def _compute_priorities(self, buf: List[Experience]) -> np.ndarray:
        """
        Compute sampling priority for each experience.

        Priority = recency_weight * recency + quality_weight * quality
        """
        now = time.time()

        # Recency score: exponential decay (recent = higher weight)
        ages = np.array([now - e.timestamp for e in buf])
        max_age = max(ages.max(), 1.0)
        recency_scores = np.exp(-ages / max_age * 3)  # Decay over time

        # Quality score: outcome quality
        quality_scores = np.array([e.outcome_quality for e in buf])

        # Combine
        priorities = (
            self.recency_weight * recency_scores
            + self.quality_weight * quality_scores
        )

        # Small epsilon to ensure non-zero probabilities
        priorities = priorities + 1e-6

        return priorities

    def get_training_arrays(
        self,
        n: int = 10_000,
        strategy: str = "prioritized",
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """
        Get training arrays for model retraining.

        Returns:
            X: Feature matrix
            y_answer_rate: Answer rate labels
            y_abandon_spike: Binary abandon spike labels
            y_optimal_ratio: Optimal ratio labels (NaN for bad outcomes)
            weights: Sample weights
        """
        experiences = self.sample(n, strategy)

        if not experiences:
            return (
                np.zeros((0, 0)),
                np.zeros(0),
                np.zeros(0),
                np.zeros(0),
                np.zeros(0),
            )

        X = np.vstack([e.features for e in experiences])
        y_answer = np.array([e.answer_rate_observed for e in experiences])
        y_abandon = np.array([1.0 if e.abandon_spike else 0.0 for e in experiences])

        # Optimal ratio: use the ratio used when outcome was good
        y_ratio = np.array([
            e.dial_ratio_used if e.outcome_quality > 0.7 else np.nan
            for e in experiences
        ])

        weights = np.array([e.weight for e in experiences])

        return X, y_answer, y_abandon, y_ratio, weights

    def __len__(self):
        return len(self._buffer)

    def stats(self) -> Dict:
        """Buffer statistics"""
        with self._lock:
            buf = list(self._buffer)
        if not buf:
            return {"size": 0}

        qualities = [e.outcome_quality for e in buf]
        return {
            "size": len(buf),
            "max_size": self.max_size,
            "avg_quality": float(np.mean(qualities)),
            "good_outcomes": int(sum(1 for q in qualities if q > 0.7)),
            "abandon_spikes": int(sum(1 for e in buf if e.abandon_spike)),
            "oldest_age_min": float(
                (time.time() - buf[0].timestamp) / 60
            ) if buf else 0,
        }


def compute_outcome_quality(
    abandon_rate: float,
    agent_utilization: float,
    answer_rate: float,
    target_abandon: float = 0.03,
    target_utilization: float = 0.85,
) -> float:
    """
    Compute composite outcome quality score [0, 1].

    Good outcome = low abandon + high utilization + good answer rate

    Used to weight training samples and prioritize replay.
    """
    # Abandon score: 1.0 if zero, drops to 0 at max limit
    if abandon_rate <= 0:
        abandon_score = 1.0
    elif abandon_rate <= target_abandon:
        abandon_score = 1.0 - (abandon_rate / target_abandon) * 0.3
    elif abandon_rate <= target_abandon * 2:
        abandon_score = 0.4
    else:
        abandon_score = 0.0

    # Utilization score: target is 85%
    util_score = 1.0 - abs(agent_utilization - target_utilization) / target_utilization
    util_score = max(0.0, util_score)

    # Answer rate score: higher is better (more productive)
    answer_score = min(1.0, answer_rate / 0.40)  # 40% = max expected

    # Weighted composite
    quality = (
        0.50 * abandon_score   # Compliance most important
        + 0.35 * util_score    # Productivity
        + 0.15 * answer_score  # Lead quality
    )

    return float(np.clip(quality, 0.0, 1.0))
