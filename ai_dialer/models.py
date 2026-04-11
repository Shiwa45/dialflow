"""
ai_dialer/models.py

Three XGBoost models that work together:

Model 1: AnswerRatePredictor
    - Predicts: answer rate in next N seconds
    - Type: Regression
    - Target: actual answer rate observed after prediction

Model 2: AbandonRiskClassifier
    - Predicts: will abandon rate exceed threshold?
    - Type: Binary classification
    - Target: 1 if abandon_rate > threshold in next window

Model 3: OptimalRatioRegressor
    - Predicts: optimal dial ratio directly
    - Type: Regression
    - Target: ratio that produced best outcome historically
    - Label: ratio used when outcome was good (abandon<3%, util>80%)
"""

import numpy as np
import pandas as pd
import xgboost as xgb
import joblib
import os
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from datetime import datetime

from .features import FEATURE_NAMES, NUM_FEATURES

logger = logging.getLogger(__name__)

MODEL_DIR = os.path.join(
    os.path.dirname(__file__), "trained_models"
)
os.makedirs(MODEL_DIR, exist_ok=True)


# ============================================================================
# Model Configurations
# ============================================================================

ANSWER_RATE_PARAMS = {
    # Regression: predict continuous answer rate
    "objective":         "reg:squarederror",
    "eval_metric":       ["rmse", "mae"],
    "max_depth":         6,
    "learning_rate":     0.05,
    "n_estimators":      500,
    "subsample":         0.8,
    "colsample_bytree":  0.7,
    "min_child_weight":  5,
    "reg_alpha":         0.1,      # L1 regularization
    "reg_lambda":        1.0,      # L2 regularization
    "gamma":             0.1,      # Min split loss
    "tree_method":       "hist",   # Fast histogram method
    "random_state":      42,
    "n_jobs":            -1,
    "early_stopping_rounds": 30,
    "verbosity":         0,
}

ABANDON_RISK_PARAMS = {
    # Binary classification: will abandon rate spike?
    "objective":         "binary:logistic",
    "eval_metric":       ["logloss", "auc"],
    "max_depth":         5,
    "learning_rate":     0.05,
    "n_estimators":      400,
    "subsample":         0.8,
    "colsample_bytree":  0.7,
    "min_child_weight":  10,      # Higher = less overfitting on rare events
    "scale_pos_weight":  5,       # Handle class imbalance (rare abandon spikes)
    "reg_alpha":         0.2,
    "reg_lambda":        1.5,
    "tree_method":       "hist",
    "random_state":      42,
    "n_jobs":            -1,
    "early_stopping_rounds": 30,
    "verbosity":         0,
}

OPTIMAL_RATIO_PARAMS = {
    # Regression: predict optimal dial ratio
    "objective":         "reg:squarederror",
    "eval_metric":       ["rmse"],
    "max_depth":         7,        # Deeper = capture complex ratio interactions
    "learning_rate":     0.03,     # Slower = more precise
    "n_estimators":      800,
    "subsample":         0.85,
    "colsample_bytree":  0.75,
    "min_child_weight":  3,
    "reg_alpha":         0.05,
    "reg_lambda":        0.5,
    "gamma":             0.05,
    "tree_method":       "hist",
    "random_state":      42,
    "n_jobs":            -1,
    "early_stopping_rounds": 40,
    "verbosity":         0,
}


# ============================================================================
# Individual Models
# ============================================================================

class AnswerRatePredictor:
    """
    XGBoost model that predicts answer rate N seconds ahead.

    Trained on: (features_at_time_T) → (answer_rate_at_time_T+N)
    This tells us: "given current state, what % of calls will be answered?"
    """

    MODEL_FILE = os.path.join(MODEL_DIR, "answer_rate_model.json")

    def __init__(self):
        self.model = xgb.XGBRegressor(**ANSWER_RATE_PARAMS)
        self._is_trained = False
        self._feature_importance: Optional[Dict] = None

    def train(
        self,
        X_train: np.ndarray,
        y_train: np.ndarray,
        X_val: np.ndarray,
        y_val: np.ndarray,
    ) -> Dict:
        """
        Train the model.

        Args:
            X_train: Feature matrix (n_samples, NUM_FEATURES)
            y_train: Answer rates (n_samples,) values in [0, 1]
            X_val:   Validation features
            y_val:   Validation answer rates

        Returns:
            dict: Training metrics
        """
        # Clip targets to valid range
        y_train = np.clip(y_train, 0.0, 1.0)
        y_val   = np.clip(y_val, 0.0, 1.0)

        self.model.fit(
            X_train, y_train,
            eval_set=[(X_val, y_val)],
            verbose=False,
        )

        self._is_trained = True
        self._cache_feature_importance()

        # Validation metrics
        y_pred = self.model.predict(X_val)
        mae = float(np.mean(np.abs(y_pred - y_val)))
        rmse = float(np.sqrt(np.mean((y_pred - y_val) ** 2)))

        best_iter = self.model.best_iteration
        logger.info(
            f"AnswerRatePredictor trained | "
            f"MAE={mae:.4f} RMSE={rmse:.4f} "
            f"best_iter={best_iter}"
        )
        return {"mae": mae, "rmse": rmse, "best_iteration": best_iter}

    def predict(self, features: np.ndarray) -> float:
        """
        Predict answer rate.

        Args:
            features: Feature vector (NUM_FEATURES,) or (N, NUM_FEATURES)

        Returns:
            float: Predicted answer rate [0, 1]
        """
        if not self._is_trained:
            return 0.30  # Default fallback

        if features.ndim == 1:
            features = features.reshape(1, -1)

        pred = self.model.predict(features)
        # Clip to valid range
        return float(np.clip(pred[0], 0.05, 1.0))

    def predict_batch(self, features: np.ndarray) -> np.ndarray:
        """Batch prediction"""
        if not self._is_trained:
            return np.full(len(features), 0.30)
        preds = self.model.predict(features)
        return np.clip(preds, 0.05, 1.0)

    def save(self):
        """Save model to disk"""
        self.model.save_model(self.MODEL_FILE)
        logger.info(f"AnswerRatePredictor saved to {self.MODEL_FILE}")

    def load(self) -> bool:
        """Load model from disk"""
        if not os.path.exists(self.MODEL_FILE):
            return False
        self.model.load_model(self.MODEL_FILE)
        self._is_trained = True
        self._cache_feature_importance()
        logger.info("AnswerRatePredictor loaded")
        return True

    def _cache_feature_importance(self):
        """Cache feature importance for monitoring"""
        importance = self.model.feature_importances_
        self._feature_importance = dict(
            sorted(
                zip(FEATURE_NAMES, importance),
                key=lambda x: x[1],
                reverse=True,
            )
        )

    def get_top_features(self, n: int = 10) -> Dict:
        """Get top N most important features"""
        if not self._feature_importance:
            return {}
        return dict(list(self._feature_importance.items())[:n])


class AbandonRiskClassifier:
    """
    XGBoost binary classifier.
    Predicts: will abandon rate exceed threshold in next window?

    Output: probability of abandon spike [0, 1]
    High probability → reduce dial ratio immediately
    """

    MODEL_FILE = os.path.join(MODEL_DIR, "abandon_risk_model.json")
    DEFAULT_THRESHOLD = 0.03  # 3% FCC limit

    def __init__(self, risk_threshold: float = DEFAULT_THRESHOLD):
        self.model = xgb.XGBClassifier(**ABANDON_RISK_PARAMS)
        self.risk_threshold = risk_threshold
        self._is_trained = False
        self._feature_importance: Optional[Dict] = None
        self._calibration_factor: float = 1.0

    def train(
        self,
        X_train: np.ndarray,
        y_train: np.ndarray,  # Binary: 1 = abandon spike happened
        X_val: np.ndarray,
        y_val: np.ndarray,
    ) -> Dict:
        """
        Train abandon risk classifier.

        y labels: 1 if abandon_rate exceeded threshold in next window
                  0 otherwise
        """
        self.model.fit(
            X_train, y_train,
            eval_set=[(X_val, y_val)],
            verbose=False,
        )

        self._is_trained = True
        self._cache_feature_importance()

        # Metrics
        probs = self.model.predict_proba(X_val)[:, 1]
        preds = (probs > 0.5).astype(int)

        tp = int(np.sum((preds == 1) & (y_val == 1)))
        fp = int(np.sum((preds == 1) & (y_val == 0)))
        fn = int(np.sum((preds == 0) & (y_val == 1)))
        tn = int(np.sum((preds == 0) & (y_val == 0)))

        precision = tp / (tp + fp) if (tp + fp) > 0 else 0
        recall    = tp / (tp + fn) if (tp + fn) > 0 else 0
        f1        = (
            2 * precision * recall / (precision + recall)
            if (precision + recall) > 0 else 0
        )
        auc = float(self.model.evals_result().get(
            'validation_0', {}
        ).get('auc', [0])[-1])

        logger.info(
            f"AbandonRiskClassifier trained | "
            f"Precision={precision:.3f} Recall={recall:.3f} "
            f"F1={f1:.3f} AUC={auc:.3f} "
            f"TP={tp} FP={fp} FN={fn} TN={tn}"
        )

        return {
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "auc": auc,
            "confusion": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
        }

    def predict_risk(self, features: np.ndarray) -> Tuple[float, bool]:
        """
        Predict abandon risk.

        Args:
            features: Feature vector

        Returns:
            Tuple[float, bool]: (risk_probability, is_high_risk)
        """
        if not self._is_trained:
            return 0.0, False

        if features.ndim == 1:
            features = features.reshape(1, -1)

        prob = float(self.model.predict_proba(features)[0, 1])
        # Apply calibration
        calibrated_prob = min(1.0, prob * self._calibration_factor)
        is_high_risk = calibrated_prob > 0.5

        return calibrated_prob, is_high_risk

    def calibrate(self, X_cal: np.ndarray, y_cal: np.ndarray):
        """
        Post-training calibration using held-out data.
        Adjusts predictions to match actual frequencies.
        """
        if not self._is_trained:
            return

        probs = self.model.predict_proba(X_cal)[:, 1]
        actual_rate = float(np.mean(y_cal))
        predicted_rate = float(np.mean(probs))

        if predicted_rate > 0:
            self._calibration_factor = actual_rate / predicted_rate

        logger.info(
            f"AbandonRisk calibrated: "
            f"actual={actual_rate:.3f} "
            f"predicted={predicted_rate:.3f} "
            f"factor={self._calibration_factor:.3f}"
        )

    def save(self):
        self.model.save_model(self.MODEL_FILE)

    def load(self) -> bool:
        if not os.path.exists(self.MODEL_FILE):
            return False
        self.model.load_model(self.MODEL_FILE)
        self._is_trained = True
        self._cache_feature_importance()
        return True

    def _cache_feature_importance(self):
        importance = self.model.feature_importances_
        self._feature_importance = dict(
            sorted(
                zip(FEATURE_NAMES, importance),
                key=lambda x: x[1],
                reverse=True,
            )
        )

    def get_top_features(self, n: int = 10) -> Dict:
        if not self._feature_importance:
            return {}
        return dict(list(self._feature_importance.items())[:n])


class OptimalRatioRegressor:
    """
    XGBoost regression model that directly predicts the optimal dial ratio.

    Training label = the dial ratio that was actually used when the system
    produced good outcomes:
        - abandon_rate < target
        - agent_utilization > 80%
        - no abandon spikes in next window

    This is an offline supervised learning approach trained on historical
    "good" episodes from the dialer's own operation.
    """

    MODEL_FILE = os.path.join(MODEL_DIR, "optimal_ratio_model.json")
    SCALER_FILE = os.path.join(MODEL_DIR, "ratio_scaler.pkl")

    def __init__(self, ratio_min: float = 1.0, ratio_max: float = 4.0):
        self.model = xgb.XGBRegressor(**OPTIMAL_RATIO_PARAMS)
        self.ratio_min = ratio_min
        self.ratio_max = ratio_max
        self._is_trained = False
        self._feature_importance: Optional[Dict] = None
        self._training_count: int = 0

    def train(
        self,
        X_train: np.ndarray,
        y_train: np.ndarray,  # Optimal ratios observed
        X_val: np.ndarray,
        y_val: np.ndarray,
        sample_weights: Optional[np.ndarray] = None,
    ) -> Dict:
        """
        Train optimal ratio predictor.

        sample_weights: Give higher weight to more recent/better outcomes
        """
        # Clip ratios to valid range
        y_train = np.clip(y_train, self.ratio_min, self.ratio_max)
        y_val   = np.clip(y_val, self.ratio_min, self.ratio_max)

        self.model.fit(
            X_train, y_train,
            eval_set=[(X_val, y_val)],
            sample_weight=sample_weights,
            verbose=False,
        )

        self._is_trained = True
        self._training_count += 1
        self._cache_feature_importance()

        y_pred = self.model.predict(X_val)
        mae  = float(np.mean(np.abs(y_pred - y_val)))
        rmse = float(np.sqrt(np.mean((y_pred - y_val) ** 2)))

        logger.info(
            f"OptimalRatioRegressor trained (#{self._training_count}) | "
            f"MAE={mae:.4f} RMSE={rmse:.4f}"
        )

        return {
            "mae": mae,
            "rmse": rmse,
            "training_count": self._training_count,
        }

    def predict(
        self,
        features: np.ndarray,
        uncertainty: bool = False,
    ) -> Tuple[float, float]:
        """
        Predict optimal dial ratio.

        Args:
            features: Feature vector
            uncertainty: If True, estimate prediction uncertainty

        Returns:
            Tuple[float, float]: (predicted_ratio, uncertainty_estimate)
        """
        if not self._is_trained:
            return 1.5, 1.0  # Conservative default

        if features.ndim == 1:
            features = features.reshape(1, -1)

        ratio = float(np.clip(
            self.model.predict(features)[0],
            self.ratio_min,
            self.ratio_max
        ))

        # Estimate uncertainty using tree leaf variance
        if uncertainty and self._is_trained:
            leaf_preds = self._get_leaf_predictions(features[0])
            std = float(np.std(leaf_preds)) if len(leaf_preds) > 1 else 0.3
        else:
            std = 0.3  # Default uncertainty

        return ratio, std

    def _get_leaf_predictions(self, features: np.ndarray) -> np.ndarray:
        """
        Get per-tree predictions for uncertainty estimation.
        XGBoost: each tree makes an independent prediction.
        """
        try:
            booster = self.model.get_booster()
            dmatrix = xgb.DMatrix(features.reshape(1, -1))
            pred_contribs = booster.predict(
                dmatrix,
                iteration_range=(0, self.model.best_iteration or 100),
                output_margin=True,
            )
            return np.array([pred_contribs[0]])
        except Exception:
            return np.array([(self.ratio_min + self.ratio_max) / 2])

    def save(self):
        self.model.save_model(self.MODEL_FILE)

    def load(self) -> bool:
        if not os.path.exists(self.MODEL_FILE):
            return False
        self.model.load_model(self.MODEL_FILE)
        self._is_trained = True
        self._cache_feature_importance()
        return True

    def _cache_feature_importance(self):
        importance = self.model.feature_importances_
        self._feature_importance = dict(
            sorted(
                zip(FEATURE_NAMES, importance),
                key=lambda x: x[1],
                reverse=True,
            )
        )

    def get_top_features(self, n: int = 10) -> Dict:
        if not self._feature_importance:
            return {}
        return dict(list(self._feature_importance.items())[:n])
