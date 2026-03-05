"""
Incident Classifier — two-stage routing decision engine.

Stage 1: Keyword matching against routing_rules.yaml
Stage 2: scikit-learn TF-IDF + Logistic Regression fallback

The keyword stage is always tried first. If a confident keyword match is found,
it is returned immediately. Otherwise the ML model is used.
"""

import logging
import re
from typing import Optional

import joblib
import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline

logger = logging.getLogger(__name__)


class ClassificationResult:
    """Holds the routing decision for a single incident."""

    __slots__ = ("group_key", "confidence", "method", "matched_keywords")

    def __init__(
        self,
        group_key: str,
        confidence: float,
        method: str,  # "keyword" | "ml" | "fallback"
        matched_keywords: Optional[list[str]] = None,
    ) -> None:
        self.group_key = group_key
        self.confidence = round(confidence, 4)
        self.method = method
        self.matched_keywords = matched_keywords or []

    def __repr__(self) -> str:
        return (
            f"ClassificationResult(group={self.group_key!r}, "
            f"conf={self.confidence}, method={self.method!r})"
        )


def _normalize(text: str) -> str:
    """Lowercase and strip punctuation for keyword comparison."""
    return re.sub(r"[^a-z0-9 ]", " ", text.lower())


class IncidentClassifier:
    """
    Two-stage incident text classifier.

    Parameters
    ----------
    routing_rules : dict
        Parsed content of config/routing_rules.yaml
    model_path : str | None
        Path to a saved sklearn Pipeline (.joblib). If None, the classifier
        trains a lightweight model from the keyword corpus at startup.
    """

    def __init__(
        self,
        routing_rules: dict,
        model_path: Optional[str] = None,
    ) -> None:
        self._rules = routing_rules
        self._ml_threshold = routing_rules.get("ml_confidence_threshold", 0.72)
        self._groups: dict[str, dict] = routing_rules.get("assignment_groups", {})
        self._critical_keywords: list[str] = (
            routing_rules.get("priority_escalation", {}).get("critical_keywords", [])
        )
        self._fallback_key: str = (
            routing_rules.get("fallback_group", {}).get("sys_id", "helpdesk-group-006")
        )

        self._pipeline: Optional[Pipeline] = None
        self._group_keys: list[str] = list(self._groups.keys())

        if model_path:
            self._load_model(model_path)
        else:
            self._train_from_keywords()

    # ─── Model lifecycle ──────────────────────────────────────────────────────

    def _train_from_keywords(self) -> None:
        """
        Bootstrap a TF-IDF + Logistic Regression model from the keyword lists
        defined in routing_rules.yaml.

        Each keyword phrase becomes a training document with its group as label.
        We also generate simple synthetic variants (e.g. "X issue", "X problem")
        to improve generalisation without any external data.
        """
        X_train: list[str] = []
        y_train: list[str] = []

        suffixes = ["", " issue", " problem", " error", " failure", " alert"]

        for group_key, group_cfg in self._groups.items():
            for kw in group_cfg.get("keywords", []):
                for suffix in suffixes:
                    X_train.append(kw + suffix)
                    y_train.append(group_key)

        self._pipeline = Pipeline(
            [
                (
                    "tfidf",
                    TfidfVectorizer(
                        ngram_range=(1, 3),
                        sublinear_tf=True,
                        min_df=1,
                        max_features=10_000,
                    ),
                ),
                (
                    "clf",
                    LogisticRegression(
                        C=5.0,
                        max_iter=1000,
                        class_weight="balanced",
                        random_state=42,
                    ),
                ),
            ]
        )
        self._pipeline.fit(X_train, y_train)
        self._group_keys = list(self._pipeline.classes_)
        logger.info(
            "Trained keyword-bootstrapped ML model on %d samples across %d classes",
            len(X_train),
            len(self._group_keys),
        )

    def save_model(self, path: str) -> None:
        """Persist the trained pipeline to disk."""
        if self._pipeline is None:
            raise RuntimeError("No model to save.")
        joblib.dump(self._pipeline, path)
        logger.info("Model saved to %s", path)

    def _load_model(self, path: str) -> None:
        """Load a previously saved pipeline."""
        self._pipeline = joblib.load(path)
        self._group_keys = list(self._pipeline.classes_)
        logger.info("Model loaded from %s (%d classes)", path, len(self._group_keys))

    # ─── Classification ───────────────────────────────────────────────────────

    def classify(self, short_description: str, description: str = "") -> ClassificationResult:
        """
        Classify an incident and return a routing decision.

        Priority order:
        1. Critical keyword escalation check (returns group + escalation flag)
        2. Keyword match scan across routing groups
        3. ML model prediction if no keyword match
        4. Fallback to service_desk if ML confidence < threshold
        """
        combined_text = f"{short_description} {description}"
        normalized = _normalize(combined_text)

        # ── Stage 1: keyword match ───────────────────────────────────────────
        keyword_result = self._keyword_classify(normalized)
        if keyword_result is not None:
            logger.debug(
                "Keyword match → %s (keywords=%s)",
                keyword_result.group_key,
                keyword_result.matched_keywords,
            )
            return keyword_result

        # ── Stage 2: ML predict ──────────────────────────────────────────────
        return self._ml_classify(normalized)

    def _keyword_classify(self, normalized_text: str) -> Optional[ClassificationResult]:
        """
        Scan all groups' keyword lists. The group with the most keyword hits wins.
        Returns None if no keywords match.
        """
        hit_counts: dict[str, list[str]] = {}

        for group_key, group_cfg in self._groups.items():
            hits = []
            for kw in group_cfg.get("keywords", []):
                if kw.lower() in normalized_text:
                    hits.append(kw)
            if hits:
                hit_counts[group_key] = hits

        if not hit_counts:
            return None

        best_group = max(hit_counts, key=lambda g: len(hit_counts[g]))
        total_keywords = sum(
            len(g.get("keywords", [])) for g in self._groups.values()
        )
        # Confidence proxy: normalised hit ratio, bounded [0.75, 0.99]
        hit_ratio = len(hit_counts[best_group]) / max(total_keywords, 1)
        confidence = min(0.99, max(0.75, 0.75 + hit_ratio * 20))

        return ClassificationResult(
            group_key=best_group,
            confidence=confidence,
            method="keyword",
            matched_keywords=hit_counts[best_group],
        )

    def _ml_classify(self, text: str) -> ClassificationResult:
        """Run the sklearn pipeline and return the top prediction."""
        if self._pipeline is None:
            logger.warning("ML pipeline not initialised — using fallback")
            return self._fallback()

        proba = self._pipeline.predict_proba([text])[0]
        best_idx = int(np.argmax(proba))
        best_group = self._group_keys[best_idx]
        confidence = float(proba[best_idx])

        if confidence < self._ml_threshold:
            logger.debug(
                "ML confidence %.3f < threshold %.3f → fallback",
                confidence,
                self._ml_threshold,
            )
            return self._fallback(confidence)

        logger.debug("ML predict → %s (conf=%.3f)", best_group, confidence)
        return ClassificationResult(
            group_key=best_group,
            confidence=confidence,
            method="ml",
        )

    def _fallback(self, ml_confidence: float = 0.0) -> ClassificationResult:
        """Return the configured fallback group."""
        fallback_key = next(
            (k for k, v in self._groups.items()
             if v.get("sys_id") == self._fallback_key),
            list(self._groups.keys())[-1],
        )
        return ClassificationResult(
            group_key=fallback_key,
            confidence=max(ml_confidence, 0.0),
            method="fallback",
        )

    def is_critical(self, short_description: str, description: str = "") -> bool:
        """
        Return True if any critical keyword appears in the incident text.
        Used by the router to escalate priority before assignment.
        """
        normalized = _normalize(f"{short_description} {description}")
        return any(kw.lower() in normalized for kw in self._critical_keywords)
