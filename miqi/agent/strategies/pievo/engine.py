import os
import json
import logging
import math
import warnings
import re

# pievo_log 已移除 — 全部改用标准 logger


import time

import numpy as np
import asyncio
from typing import Dict, List, Any, Optional, Sequence, Tuple
from datetime import datetime

from sklearn.random_projection import GaussianRandomProjection

from miqi.agent.strategies.pievo.bayesian import GaussianProcessModel
from miqi.agent.strategies.pievo.embedding import EmbeddingService
from miqi.agent.strategies.pievo.extraction import extract_json_blocks
from miqi.agent.strategies.pievo.instruction import (
    get_hypothesis_guidance_prompt,
    get_experiment_guidance_prompt,
    get_principle_guidance_prompt,
)
from miqi.agent.strategies.pievo.utils import sanitize, get_submission_hash
# FlowTracker 暂时不引入，保持 engine 最小化
# TODO: re-enable FlowTracker for metrics/visualization after core migration

os.environ["KMP_DUPLICATE_LIB_OK"] = "true"

# AutoGen imports removed — PiEvoEngine no longer depends on AutoGen

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


EXPERIMENT_SAVED_FNAME = "experiment.json"
SUBMISSION_AND_GUIDANCE_FNAME = "submission_and_guidance.json"
SUBMISSION_NONE_GUIDANCE_FNAME = "submission_none_guidance.json"
TOKEN_USAGE_FNAME = "token_usage.json"


class PiEvoEngine:
    def __init__(
        self,
        *,
        llm_call,          # async (system_prompt: str, user_message: str) -> str
        tool_executor,     # async (tool_name: str, params: dict) -> dict
        task: str,
        output_dir: str = "./pievo_output",
        sigma: float = 0.1,
        anomaly_threshold: float = 0.85,
        warm_up_rounds: int = 8,
        new_principle_prior_mass: float = 1e-3,
        monte_carlo_samples: int = 25,
        initial_absolute_sigma: float = 0.01,
        llm_judge: bool = False,
        on_progress=None,
        on_round_complete=None,
    ):
        self.submissions: List[Dict[str, Any]] = []
        self.output_dir = output_dir
        self.task = task
        self.llm_call = llm_call
        self.tool_executor = tool_executor
        self.on_progress = on_progress
        self.on_round_complete = on_round_complete
        self.available_tools: list = []

        # --- PiEvo Algorithm State ---
        self.principles: Dict[str, str] = {}  # P_t: principle_id -> principle_text
        self.principle_beliefs: Dict[
            str, float
        ] = {}  # p_t(P): posterior belief over principles
        self.principle_priors: Dict[str, float] = {}
        self.history: List[Tuple[Any, float]] = []
        self.principles_rationals: Dict[str, dict] = {}
        # e.g., [{'principle_helix_aspect_ratio_optimization': {'RATIONAL': {'major premise': 'The chiral strength of a nanohelix, as quantified by the g-factor, is fundamentally influenced by the geometric aspect ratio of the helix structure, particularly the ratio between helix_radius and fiber_radius.', 'minor premises': ['A larger helix_radius relative to fiber_radius increases the spatial separation of the helical path, enhancing the chiral asymmetry.', 'The fiber_radius determines the compactness of the helical wire; a smaller fiber_radius allows for a more pronounced helical curvature.', 'The chiral effect is maximized when the helix_radius is significantly larger than the fiber_radius, creating a more open and asymmetric helical structure.', 'This geometric configuration enhances the optical and electromagnetic interactions that are sensitive to chirality.', 'Such a configuration increases the handedness of the helix, which is directly related to the g-factor.']}, 'PRINCIPLE_SUBMISSION': 'Maximize the ratio of helix_radius to fiber_radius to enhance the chiral effect, as a higher aspect ratio increases the spatial asymmetry and curvature of the helix, thereby increasing the g-factor.'}}]

        self.is_llm_judge: bool = llm_judge

        # --- Algorithm Parameters ---
        self.sigma = sigma
        self.initial_absolute_sigma = (
            initial_absolute_sigma  # we use the absolute noise initially（e.g., 1.0）
        )
        self.monte_carlo_samples = (
            monte_carlo_samples  # Number of samples for BALD and other estimations
        )
        self.new_principle_prior_mass = new_principle_prior_mass
        self.anomaly_threshold = anomaly_threshold

        self.embedding_service = EmbeddingService()

        self.warm_up_rounds = warm_up_rounds
        self.is_exploitation_phase = False
        self.exploitation_candidates: List[dict[str, float]] = []

        self._y_mean = 0.0
        self._y_std = 1.0

        # --- Gaussian Process Models per Principle ---
        self.gp_models: Dict[str, "GaussianProcessModel"] = {}
        self.feature_cache: Dict[Tuple[str, str], np.ndarray] = {}

        # --- State Tracking ---
        self._processed_submission_hashes = set()  # For efficient deduplication

        # --- Caches for performance ---
        self.prediction_cache: Dict[Tuple[str, str], Optional[float]] = {}
        self.optimal_prediction_cache: Dict[
            str, Tuple[Optional[str], Optional[float]]
        ] = {}

        # Track which history items have been processed for incremental updates
        self._last_processed_history_index = 0
        self._last_belief_update_history_index = 0

        # --- Metrics tracking for theoretical analysis ---
        self._last_anomaly_count = 0  # Track anomalies for theoretical metrics
        self._previous_principle_count = len(self.principles)  # Track principle growth

        # --- Regret and reward tracking for theoretical framework implementation ---
        self.bayesian_regret_history = []
        self.reward_history = []  # Track actual rewards r(h_t, P*) per round
        self.anomaly_storage = []  # Store anomalies A_t for principle generation (Eq. 100)
        self.true_principle_id = None  # P* if known (for simulation/testing)

        # --- Information tracking for theoretical bounds ---
        self._cumulative_information_sum = (
            0.0  # Track cumulative information for theoretical bounds
        )
        self._last_information_gain = (
            None  # Track last information gain for theoretical metrics
        )

        self.round_by_PHE_order_before_P: int = 0

        self.status: str = "Initializing"
        self.hypothesis_form = {}

    pass  # register_client removed — AutoGen dependency
    pass  # _reset_iter_logs removed — FlowTracker dependency
    pass  # _commit_and_reset_logs removed — FlowTracker dependency

    pass  # _process_message removed — submissions managed inline in P-H-E phases

    pass  # save_to_file removed — P-H-E loop manages state in memory

    def clear_caches(self):
        self.prediction_cache.clear()
        self.optimal_prediction_cache.clear()
        self.feature_cache.clear()

    def _update_scaler(self):
        if len(self.history) < 2:
            self._y_mean = 0.0
            self._y_std = 1.0
            return
        outcomes = [
            y for h, y in self.history if y != 0.0
        ]  # Filter out failed experiments
        if not outcomes:
            self._y_mean = 0.0
            self._y_std = 1.0
            return
        self._y_mean = np.mean(outcomes)
        std = np.std(outcomes)
        self._y_std = std if std > 1e-9 else 1.0

        # We do not update sigma here is to:
        # Ensure that self.sigma = 0.01 means "The observation noise is 1% of the historical standard deviation."

    def _normalize_y(self, y: float) -> float:
        """Raw -> Standardized"""
        return (y - self._y_mean) / self._y_std

    def _denormalize_y(self, y_norm: float) -> float:
        """Standardized -> Raw"""
        return (y_norm * self._y_std) + self._y_mean

    async def _estimate_principle_optimal_value(self, principle_id: str) -> float:
        """
        Estimates the optimal value v*(P) for a principle using historical data only.
        No LLM calls - uses Bayesian model analysis of historical performance.
        """
        if principle_id not in self.gp_models:
            return self._y_mean

        model = self.gp_models[principle_id]
        historical_predictions = []
        for h, y in self.history:
            try:
                features = self._extract_features(h, principle_id)
                mean, _ = model.predict(features)
                historical_predictions.append(mean)
            except Exception:
                continue

        if not historical_predictions:
            return self._y_mean

        max_norm = max(historical_predictions)
        return sanitize(self._denormalize_y(max_norm))

    async def _estimate_candidate_value_from_gp_model(
        self, hypothesis: str, principle_id: str
    ) -> float:
        """
        Estimates the value of a candidate hypothesis using only Bayesian model and historical data.
        No LLM calls.
        """
        if principle_id not in self.gp_models:
            return self._y_mean

        try:
            # Use async feature extraction for consistency
            features = self._extract_features(hypothesis, principle_id)
            model = self.gp_models[principle_id]

            # Ensure the feature dim is correct.
            mean_norm, variance = model.predict(features)
            return sanitize(self._denormalize_y(mean_norm))

        except Exception as e:
            logger.warning(f"Error estimating candidate value for {principle_id}: {e}")
            return self._y_mean

    async def _calculate_bayesian_regret(self, hypothesis: str) -> float:
        """
        Computes regret using only historical data and Bayesian models.
        This replaces the LLM-based regret calculation with data-driven approach.
        """
        if not self.principle_beliefs:
            return 0.0

        estimated_optimal = 0.0
        for pid, belief in self.principle_beliefs.items():
            optimal_value = await self._estimate_principle_optimal_value(pid)
            estimated_optimal += belief * optimal_value

        estimated_candidate = 0.0
        for pid, belief in self.principle_beliefs.items():
            candidate_value = await self._estimate_candidate_value_from_gp_model(
                hypothesis, pid
            )
            estimated_candidate += belief * candidate_value

        regret = estimated_optimal - estimated_candidate
        return max(0.0, regret)

    pass  # gather_submission_from_message removed — AutoGen dependency

    @staticmethod
    def _candidate_key(candidate) -> str:
        """Convert a candidate (dict or str) to a hashable string key."""
        if isinstance(candidate, dict):
            return json.dumps(candidate, sort_keys=True)
        return str(candidate)

    def _extract_features(self, hypothesis, principle_id: str) -> np.ndarray:
        """
        1. Basic and easy-to-get features:
            a. Dot Product of h_emb and p_emb
            b. Euclidean Dist of h_emb and p_emb
        2. LLM-as-Judge Features:
            a. Alignment Score: Does the parameter setting of the Hypothesis strictly follow the qualitative description of the Principle?
            b. Derivation Logic: Is the logical chain in the rational part of the Hypothesis complete and consistent?
        """
        h_key = self._candidate_key(hypothesis)
        cache_key = (h_key, principle_id)
        if cache_key in self.feature_cache:
            return self.feature_cache[cache_key]

        embed_func = self.embedding_service.embed_with_local_model

        # Get embeddings of Hypothesis and Principle
        h_emb = np.array(
            embed_func(
                hypothesis
                if isinstance(hypothesis, str)
                else json.dumps(hypothesis, sort_keys=True)
            )
        )
        principle_text = self.principles.get(principle_id)
        p_emb = np.array(embed_func(principle_text))
        h_emb = h_emb / (np.linalg.norm(h_emb) + 1e-9)
        p_emb = p_emb / (np.linalg.norm(p_emb) + 1e-9)

        phi_1_projection = np.dot(h_emb, p_emb)
        phi_2_euclidean_dist = np.linalg.norm(h_emb - p_emb)
        _base_features = np.array([phi_1_projection, phi_2_euclidean_dist])

        features = np.concatenate(
            [
                _base_features,
            ],
            dtype=np.float32,
        )

        features = np.nan_to_num(features, nan=0.0, posinf=1e6, neginf=-1e6)
        self.feature_cache[cache_key] = features
        return features

    def _extract_principles(self) -> List[str]:
        """Extracts principles from submissions and identifies newly discovered ones."""
        new_principle_ids = []
        for submission in self.submissions:
            if submission["source_agent"] == "principle":
                json_data = submission["json_data"]
                if isinstance(json_data, dict):
                    for key, value in json_data.items():
                        if key.startswith("principle_"):
                            principle_text = (
                                value.get("PRINCIPLE_SUBMISSION", "")
                                if isinstance(value, dict)
                                else str(value)
                            )
                            if key not in self.principles:
                                self.principles[key] = principle_text
                                new_principle_ids.append(key)
        return new_principle_ids

    def _handle_new_principles(self, new_principle_ids: List[str]):
        """
        Implements Coherent Augmentation by assigning a prior p_0(P_new)
        to newly discovered principles.
        """
        if not new_principle_ids:
            return

        if not self.principle_priors:  # First principles
            prob = self.new_principle_prior_mass
            for pid in self.principles:
                self.principle_priors[pid] = prob
        else:
            for new_pid in new_principle_ids:
                if new_pid not in self.principle_priors:
                    # Assign the initial belief score.
                    self.principle_priors[new_pid] = self.new_principle_prior_mass

    pass  # _extract_history removed — history managed directly in _run_experiment_phase

    async def _get_prediction(
        self, hypothesis, principle_id: str, return_normalized: bool = False
    ) -> Optional[float]:
        """
        Gets f_P(h), the predicted outcome for a hypothesis h under principle P.
        Uses Bayesian linear model trained on historical data only.
        """
        cache_key = (self._candidate_key(hypothesis), principle_id)
        if cache_key in self.prediction_cache:
            return self.prediction_cache[cache_key]

        if principle_id not in self.gp_models:
            logger.error(
                f"Prediction failed: GP model for {principle_id} does not exist. "
            )
            self.prediction_cache[cache_key] = None
            return None

        try:
            model = self.gp_models[principle_id]
            features = self._extract_features(hypothesis, principle_id)

            if len(features) != model.feature_dim:
                logger.error(
                    f"Feature dimension mismatch for {principle_id}: "
                    f"Model expected {model.feature_dim}, got {len(features)}. "
                    f"This may indicate a state error."
                )
                self.prediction_cache[cache_key] = None

            # Use trained Bayesian model
            mean_norm, variance = model.predict(features)

            if return_normalized:
                return mean_norm

            prediction = self._denormalize_y(mean_norm)

        except Exception as e:
            logger.warning(f"Prediction failed for ({hypothesis}, {principle_id}): {e}")
            prediction = None

        self.prediction_cache[cache_key] = prediction
        return prediction

    async def _likelihood(
        self,
        outcome: float,
        hypothesis: str,
        principle_id: str,
        is_normalized_input: bool = False,
    ) -> float:
        """
        Computes the likelihood p(y|h,P) using a Gaussian model.
        Uses Bayesian model's predictive distribution when available.
        Now properly accounts for both model and observational uncertainty.
        """
        effective_noise_var = self.sigma**2

        if is_normalized_input:
            y_norm = outcome
        else:
            y_norm = self._normalize_y(outcome)

        if (
            principle_id in self.gp_models
            and self.gp_models[principle_id].n_observations > 0
        ):
            # Use GP model's predictive distribution
            model = self.gp_models[principle_id]

            features = self._extract_features(hypothesis, principle_id)
            mean, model_variance = model.predict(features)

            # Total variance = model uncertainty + observational noise
            total_variance = model_variance + effective_noise_var
            if total_variance <= 0:
                total_variance = effective_noise_var  # Fallback to default noise

            # Compute likelihood with proper total uncertainty
            exponent = -0.5 * ((y_norm - mean) ** 2) / total_variance
            factor = 1.0 / math.sqrt(2 * math.pi * total_variance)
            likelihood = factor * math.exp(exponent)
            return max(likelihood, 1e-12)  # Use more conservative epsilon

        # Fallback to point prediction + fixed noise
        prediction = await self._get_prediction(
            hypothesis, principle_id, return_normalized=True
        )
        if prediction is None:
            return 0.0

        variance = effective_noise_var
        exponent = -0.5 * ((y_norm - prediction) ** 2) / variance
        factor = 1.0 / math.sqrt(2 * math.pi * variance)
        likelihood = factor * math.exp(exponent)
        return max(likelihood, 1e-12)  # Use more conservative epsilon

    async def _update_beliefs_from_full_history(self) -> None:
        """
        Correctly computes the posterior p_t(P) ∝ p_0(P) Π p(y_s|h_s, P)
        over the *full history* for *all* principles in the working set.
        Handles the case of no history by setting beliefs to priors.
        """
        if not self.principles:
            self.principle_beliefs = {}
            return

        # If there is no history, beliefs are the priors.
        if not self.history:
            if not self.principle_priors:
                # Initialize uniform priors if they don't exist
                prob = 1e-2
                self.principle_priors = {pid: prob for pid in self.principles}

            # Set beliefs from priors and normalize
            self.principle_beliefs = self.principle_priors.copy()
            total_prior = sum(self.principle_beliefs.values())
            if total_prior > 0:
                for pid in self.principle_beliefs:
                    self.principle_beliefs[pid] /= total_prior
            else:  # Fallback to uniform if sum is zero
                prob = 1e-2
                self.principle_beliefs = {pid: prob for pid in self.principles}
            return

        # --- Emphasized Logic Change ---
        # 1. Start from the fixed prior p_0(P)
        log_posteriors = {
            pid: math.log(max(1e-12, self.principle_priors.get(pid, 1e-6)))
            for pid in self.principles.keys()
        }

        # 2. Apply likelihood from ALL history items
        for h, y in self.history:
            if y == 0.0:  # Skip failed experiments
                continue
            for pid in self.principles.keys():
                try:
                    likelihood = await self._likelihood(y, h, pid)
                    log_posteriors[pid] += math.log(max(likelihood, 1e-12))
                except Exception:
                    log_posteriors[pid] -= 50  # Penalize failure

        # 3. Normalize using log-sum-exp for stability
        log_sum_exp = float("-inf")
        for log_prob in log_posteriors.values():
            log_sum_exp = np.logaddexp(log_sum_exp, log_prob)

        if np.isinf(log_sum_exp):
            # All posteriors are zero, fallback to uniform
            logger.warning("All posteriors are zero, resetting to uniform.")
            prob = 1e-2
            self.principle_beliefs = {pid: prob for pid in self.principles}
            return

        # 4. Compute final posterior beliefs
        self.principle_beliefs = {
            pid: math.exp(log_post - log_sum_exp)
            for pid, log_post in log_posteriors.items()
        }

        await self._train_gp_models()  # Keep GP training incremental
        self._last_belief_update_history_index = len(self.history)

    async def _train_gp_models(self) -> None:
        """
        Incrementally updates GP models with new historical data.
        Ensures new principles are "back-filled" with all historical data.
        Ensures models are created even if history is empty (t=0 case).
        """
        actual_feature_dim = 4 if self.is_llm_judge else 2

        if not self.principles:
            return

        if self.history:
            sample_hypothesis, _ = self.history[0]
            sample_principle_id = next(iter(self.principles.keys()), None)
            if sample_principle_id:
                try:
                    sample_features = self._extract_features(
                        sample_hypothesis, sample_principle_id
                    )
                    actual_feature_dim = len(sample_features)
                    logger.debug(
                        f"GP Training: Determined feature dimension is {actual_feature_dim}"
                    )
                except Exception as e:
                    logger.warning(
                        f"Could not determine actual feature dim, falling back to default: {e}"
                    )

        # 1. Clear existing models to ensure we retrain with current Scaler (_y_mean/_y_std)
        self.gp_models.clear()

        # 2. Re-train all models with ALL history using CURRENT scaler, this will be safe.
        for principle_id in self.principles.keys():
            self.gp_models[principle_id] = GaussianProcessModel(
                principle_id=principle_id,
                log_dir=self.output_dir,
                feature_dim=actual_feature_dim,
            )

            # Batch update (more efficient than loop if supported, otherwise loop)
            # Assuming incremental update is fast enough:
            for h, y in self.history:
                if y != 0.0:
                    try:
                        features = self._extract_features(h, principle_id)
                        y_norm = self._normalize_y(y)  # Uses CURRENT scaler
                        self.gp_models[principle_id].update(features, y_norm)
                    except Exception as e:
                        logger.warning(f"GP Update failed: {e}")

        self._last_processed_history_index = len(self.history)

    pass  # update_pievo_state removed — replaced by _update_state

    def _is_warm_up_phase(self) -> bool:
        """Check if we're still in the warm-up phase"""
        return len(self.history) < self.warm_up_rounds

    async def _select_hypothesis_warm_up(self, candidate_hypotheses: List[str]) -> str:
        """
        Select hypothesis during warm-up phase using uncertainty sampling or random selection.
        This helps accumulate diverse initial data for GP models.
        """
        # Uncertainty sampling: select hypothesis with the highest prediction variance
        best_hypothesis = None
        best_uncertainty = -1.0

        for h in candidate_hypotheses:
            total_uncertainty = 0.0
            valid_principles = 0

            for pid in self.principles.keys():
                try:
                    if pid in self.gp_models and self.gp_models[pid].n_observations > 0:
                        # Use GP model uncertainty
                        features = self._extract_features(h, pid)
                        _, variance = self.gp_models[pid].predict(features)
                        uncertainty = np.sqrt(variance)
                    else:
                        # Use default high uncertainty for untrained models
                        uncertainty = 1.0

                    total_uncertainty += uncertainty
                    valid_principles += 1

                except Exception as e:
                    logger.debug(f"Error computing uncertainty for {pid}: {e}")
                    continue

            # Average uncertainty across principles
            avg_uncertainty = total_uncertainty / max(1, valid_principles)

            if avg_uncertainty > best_uncertainty:
                best_uncertainty = avg_uncertainty
                best_hypothesis = h

            if best_hypothesis:
                logger.debug(
                    f"Warm-up (uncertainty): Selected '{str(best_hypothesis)[:30]}...' (uncertainty: {best_uncertainty:.3f})"
                )
                return best_hypothesis
            else:
                # Fallback to random if uncertainty computation fails
                import random

                selected = random.choice(candidate_hypotheses)
                logger.debug(f"Warm-up (fallback): Selected '{str(selected)[:30]}...'")
                return selected

        else:
            # Fallback to first candidate
            return candidate_hypotheses[0]

    def _calculate_optimal_value_star(self) -> Optional[float]:
        """
        Calculate v* = max_h r(h, P*) - the optimal value under the true principle.
        Based on Eq. in theoretical_framework.tex

        Returns:
            Optimal value as the maximum observed reward from experiment outcomes
        """
        if self.reward_history:
            try:
                valid_rewards = [r for r in self.reward_history if r is not None]
                if valid_rewards:
                    max_reward = max(valid_rewards)
                    logger.debug(
                        f"Calculated optimal_value_star from reward_history: {max_reward}"
                    )
                    return float(max_reward)
            except (ValueError, TypeError, StopIteration):
                logger.error(
                    "Failed to calculate `optimal_value_star` from `reward_history`."
                )

        logger.debug(
            f"Could not calculate optimal_value_star. reward_history length: {len(self.reward_history) if self.reward_history else 0}"
        )
        return None

    def _calculate_optimal_hypothesis_for_principle(
        self, principle_id: str
    ) -> Optional[Tuple[str, float]]:
        """
        Calculate h*(P) = argmax_h r(h, P) - the optimal hypothesis for a given principle.
        Theory: For each principle P, find the hypothesis that maximizes reward under that principle.

        Args:
            principle_id: ID of the principle P

        Returns:
            Tuple of (optimal_hypothesis, optimal_reward) or None if not available
        """
        if principle_id not in self.gp_models:
            return None

        model = self.gp_models[principle_id]
        if model.n_observations == 0:
            return None

        # Find the hypothesis in history that performed best under this principle
        best_hypothesis = None
        best_predicted_reward = -float("inf")

        # Check cache first
        if principle_id in self.optimal_prediction_cache:
            cached_hypothesis, cached_reward = self.optimal_prediction_cache[
                principle_id
            ]
            if cached_hypothesis is not None and cached_reward is not None:
                return cached_hypothesis, cached_reward

        # Search through historical data to find best performing hypothesis under this principle
        for hypothesis, actual_reward in self.history:
            try:
                # Get predicted reward for this hypothesis under this principle
                features = self._extract_features(hypothesis, principle_id)
                predicted_mean, predicted_variance = model.predict(features)

                # Use predicted mean as estimate of r(h, P)
                if predicted_mean > best_predicted_reward:
                    best_predicted_reward = predicted_mean
                    best_hypothesis = hypothesis
            except Exception as e:
                logger.debug(
                    f"Failed to evaluate hypothesis {hypothesis} under principle {principle_id}: {e}"
                )
                continue

        if best_hypothesis is not None:
            # Cache the result, but transform into raw first.
            best_predicted_reward_raw = self._denormalize_y(best_predicted_reward)
            self.optimal_prediction_cache[principle_id] = (
                best_hypothesis,
                best_predicted_reward_raw,
            )
            return best_hypothesis, best_predicted_reward_raw
        return None

    def _calculate_instantaneous_regret(self, reward: float) -> Optional[float]:
        """
        Calculate instantaneous regret: Δ_t = v* - r(h_t, P*)
        Based on Eq. 74 in theoretical_framework.tex

        Args:
            reward: The actual reward r(h_t, P*) observed

        Returns:
            Instantaneous regret if v* is available, None otherwise
        """
        if self.optimal_value_star is None:
            self.optimal_value_star = self._calculate_optimal_value_star()

        logger.debug(
            f"Calculating regret: optimal_value_star={self.optimal_value_star}, reward={reward}"
        )

        if self.optimal_value_star is None:
            return None

        regret = self.optimal_value_star - reward
        final_regret = max(0.0, regret)  # Regret is non-negative
        return final_regret

    def _calculate_regret_decomposition(
        self, hypothesis: str, selected_principle_id: str
    ) -> Tuple[Optional[float], Optional[float]]:
        """
        Calculate regret decomposition: Δ_t = Δ_t^(ID) + Δ_t^(PH)
        Based on Lemma 1 (Eq. 149-157) in theoretical_framework.tex

        Args:
            hypothesis: The chosen hypothesis h_t
            selected_principle_id: The principle P_t used to generate h_t

        Returns:
            Tuple of (identification_regret, ph_regret) or (None, None) if insufficient data
        """
        if self.true_principle_id is None or self.optimal_value_star is None:
            return None, None

        try:
            # Δ_t^(ID) = E[r(h*(P*), P*) - r(h*(P_t), P*)]

            # Estimate r(h*(P_t), P*) - optimal hypothesis under chosen principle, evaluated under true principle
            if selected_principle_id == self.true_principle_id:
                # If we chose the true principle, ID regret should be small
                identification_regret = 0.0
            else:
                # Estimate how much worse the chosen principle's best hypothesis is under true principle
                identification_regret = (
                    0.1 * self.optimal_value_star
                )  # Conservative estimate

            # Δ_t^(PH) = E[r(h*(P_t), P*) - r(h_t, P*)]
            # This measures regret from imperfect hypothesis generation given P_t

            # Estimate r(h*(P_t), P*) as best possible under chosen principle
            optimal_under_chosen_principle = self.optimal_value_star
            if selected_principle_id != self.true_principle_id:
                optimal_under_chosen_principle *= 0.9  # Discount for wrong principle

            # Current hypothesis performance
            current_reward = self.reward_history[-1] if self.reward_history else 0.0

            ph_regret = max(0.0, optimal_under_chosen_principle - current_reward)

            return identification_regret, ph_regret

        except Exception as e:
            logger.warning(f"Error calculating regret decomposition: {e}")
            return None, None

    def _calculate_regret_decomposition_proper(
        self, hypothesis: str, selected_principle_id: str, actual_reward: float
    ) -> Tuple[Optional[float], Optional[float]]:
        """
        Calculate proper regret decomposition: Δ_t = Δ_t^(ID) + Δ_t^(PH)
        Based on Lemma 1 (Eq. 149-157) in theoretical_framework.tex

        Theory:
        Δ_t^(ID) = E[r(h*(P*), P*) - r(h*(P_t), P*) | H_{t-1}]
        Δ_t^(PH) = E[r(h*(P_t), P*) - r(h_t, P*) | H_{t-1}]

        Args:
            hypothesis: The chosen hypothesis h_t
            selected_principle_id: The principle P_t used to generate h_t
            actual_reward: The actual reward r(h_t, P*) observed

        Returns:
            Tuple of (identification_regret, ph_regret) or (None, None) if insufficient data
        """
        if self.true_principle_id is None or self.optimal_value_star is None:
            return None, None

        try:
            # Get oracle hypothesis for true principle h*(P*)
            oracle_true_result = self._calculate_optimal_hypothesis_for_principle(
                self.true_principle_id
            )

            # Get oracle hypothesis for selected principle h*(P_t)
            oracle_selected_result = self._calculate_optimal_hypothesis_for_principle(
                selected_principle_id
            )

            # Δ_t^(ID) = E[r(h*(P*), P*) - r(h*(P_t), P*)]
            # This measures regret from choosing wrong principle P_t instead of P*
            identification_regret = 0.0
            if oracle_true_result is not None and oracle_selected_result is not None:
                _, oracle_true_reward = oracle_true_result
                _, oracle_selected_reward = oracle_selected_result

                # Evaluate both oracle hypotheses under true principle
                true_principle_model = self.gp_models.get(self.true_principle_id)
                if true_principle_model is not None:
                    try:
                        # Evaluate h*(P*) under P*
                        if oracle_true_result[0] is not None:
                            features_true = self._extract_features(
                                oracle_true_result[0], self.true_principle_id
                            )
                            norm_pred, _ = true_principle_model.predict(features_true)
                            reward_true_under_true = self._denormalize_y(
                                norm_pred
                            )  # go back to raw
                        else:
                            reward_true_under_true = self.optimal_value_star or 0.0

                        # Evaluate h*(P_t) under P*
                        if oracle_selected_result[0] is not None:
                            features_selected = self._extract_features(
                                oracle_selected_result[0], self.true_principle_id
                            )
                            norm_pred, _ = true_principle_model.predict(
                                features_selected
                            )
                            reward_selected_under_true = self._denormalize_y(
                                norm_pred
                            )  # go back to raw
                        else:
                            reward_selected_under_true = 0.0

                        identification_regret = max(
                            0.0, reward_true_under_true - reward_selected_under_true
                        )
                    except Exception as e:
                        logger.debug(
                            f"Error evaluating oracle hypotheses under true principle: {e}"
                        )
                        # Fallback to heuristic
                        if selected_principle_id != self.true_principle_id:
                            identification_regret = 0.1 * (
                                self.optimal_value_star or 1.0
                            )

            # Δ_t^(PH) = E[r(h*(P_t), P*) - r(h_t, P*)]
            # This measures regret from imperfect hypothesis generation given P_t
            ph_regret = 0.0
            if oracle_selected_result is not None:
                _, oracle_selected_reward = (
                    oracle_selected_result  # now this is raw data, not norm
                )

                # Evaluate h*(P_t) under true principle
                true_principle_model = self.gp_models.get(self.true_principle_id)
                if true_principle_model is not None:
                    try:
                        oracle_hypothesis, _ = oracle_selected_result
                        features = self._extract_features(
                            oracle_hypothesis, self.true_principle_id
                        )
                        norm_optimal_reward_under_true, _ = (
                            true_principle_model.predict(features)
                        )
                        optimal_reward_under_true = self._denormalize_y(
                            norm_optimal_reward_under_true
                        )  # go back to raw

                        # Current hypothesis performance under true principle
                        current_features = self._extract_features(
                            hypothesis, self.true_principle_id
                        )
                        norm_current_reward_under_true, _ = (
                            true_principle_model.predict(current_features)
                        )
                        current_reward_under_true = self._denormalize_y(
                            norm_current_reward_under_true
                        )  # go back to raw

                        ph_regret = max(
                            0.0, optimal_reward_under_true - current_reward_under_true
                        )
                    except Exception as e:
                        logger.debug(f"Error evaluating PH regret: {e}")
                        # Fallback to simpler calculation
                        ph_regret = max(
                            0.0, (oracle_selected_reward or 0.0) - actual_reward
                        )
                else:
                    # Fallback when we can't evaluate under true principle
                    ph_regret = max(
                        0.0, (oracle_selected_reward or 0.0) - actual_reward
                    )
            else:
                # No oracle for selected principle, use heuristic
                ph_regret = max(0.0, (self.optimal_value_star or 0.5) - actual_reward)

            return identification_regret, ph_regret

        except Exception as e:
            logger.warning(f"Error calculating proper regret decomposition: {e}")
            return None, None

    def _calculate_adaptive_anomaly_threshold(self) -> float:
        """
        Calculate adaptive anomaly threshold based on current principle beliefs.
        If self.anomaly_threshold is manually set, use that as override.
        Theory: Anomalies are defined as observations with surprisal exceeding threshold θ_t
        under the current MAP principle.

        Returns:
            Adaptive threshold value in [0, 1] range for normalized surprisal values
        """
        # Check if user has manually overridden the adaptive behavior
        if hasattr(self, "anomaly_threshold") and self.anomaly_threshold is not None:
            # Use manual override if set to a valid value in [0,1] range
            manual_threshold = float(self.anomaly_threshold)
            if 0.0 <= manual_threshold <= 1.0:
                return manual_threshold

        # Default threshold mapped to [0,1] range for normalized surprisal values
        # Original values were in range that needed to be scaled to [0,1]
        base_threshold = 0.7

        # If we have principle beliefs, we adapt based on confidence
        if self.principle_beliefs and len(self.principle_beliefs) > 1:
            # Calculate the entropy of the current principle beliefs (uncertainty measure)
            belief_entropy = self._calculate_principle_uncertainty()
            max_entropy = math.log(
                len(self.principle_beliefs)
            )  # Maximum possible entropy

            # Calculate normalized uncertainty (0 = certain, 1 = uniform)
            normalized_uncertainty = (
                belief_entropy / max_entropy if max_entropy > 0 else 0
            )

            # Adjust threshold based on belief uncertainty
            # More uncertain beliefs should have lower threshold to detect anomalies
            # More certain beliefs should have higher threshold to avoid frequent false positives
            if normalized_uncertainty > 0.7:  # High uncertainty
                base_threshold = 0.4  # Lower threshold (easier to trigger anomalies)
            elif normalized_uncertainty < 0.3:  # Low uncertainty
                base_threshold = 0.8  # Higher threshold (harder to trigger anomalies)
            else:  # Medium uncertainty
                base_threshold = 0.65

        return base_threshold

    async def _update_regret_and_rewards(self) -> None:
        """
        Updates the regret and reward history based on the full history of experiments.
        This method traverses the entire history to ensure regret is calculated correctly.
        It also checks for anomalies in the history.
        """
        self.reward_history = [outcome for _, outcome in self.history]
        self.optimal_value_star = self._calculate_optimal_value_star()

        if self.optimal_value_star is None:
            return

        self.regret_history = []
        self.cumulative_regret_sum = 0.0

        for hypothesis, reward in self.history:
            if reward == 0.0:  # Skip failed experiments
                continue
            regret = self._calculate_instantaneous_regret(reward)
            if regret is not None:
                self.regret_history.append(regret)
                self.cumulative_regret_sum += regret

        # Check for anomalies in the history
        if self.principle_beliefs:
            map_principle_id = max(
                self.principle_beliefs, key=self.principle_beliefs.get
            )
            if (
                map_principle_id in self.gp_models
                and self.gp_models[map_principle_id].n_observations > 0
            ):
                effective_noise_var = self.sigma**2

                self.anomaly_storage = []  # Clear previous anomalies
                for hypothesis, reward in self.history:
                    if reward == 0.0:  # Skip failed experiments
                        continue
                    try:
                        # Get expected reward under MAP principle
                        features = self._extract_features(hypothesis, map_principle_id)
                        expected_mean, expected_var = self.gp_models[
                            map_principle_id
                        ].predict(features)

                        # Calculate surprisal (normalized prediction error) - Theory Eq. 100
                        # Using squared error normalized by total uncertainty for stable anomaly detection
                        total_variance = expected_var + effective_noise_var
                        if total_variance <= 0:
                            total_variance = (
                                effective_noise_var  # Fallback to noise-only variance
                            )

                        # Calculate normalized squared error: (y-μ)²/σ²_total
                        # Apply sigmoid transformation to map [0,∞) to [0,1] for easier threshold control
                        # If the raw rewards are around 1000 and your normalized mean is 0, the error is $1000^2$, which will result in a massive surprisal value for every single observation, flooding the system with anomalies.
                        reward_norm = self._normalize_y(reward)
                        normalized_error_sq = (
                            (reward_norm - expected_mean) ** 2
                        ) / total_variance

                        # Use: x -> 1 - exp(-sqrt(x)) which maps [0,∞) to [0,1) with good sensitivity
                        surprisal = 1 - math.exp(-math.sqrt(normalized_error_sq))

                        # Store as anomaly if surprisal exceeds threshold
                        anomaly_threshold = self._calculate_adaptive_anomaly_threshold()

                        logger.warning(
                            f"Surprisal for h={hypothesis} and y={reward}: {surprisal}"
                        )

                        if surprisal > anomaly_threshold and reward > 0.0:
                            self._store_anomaly(
                                hypothesis, expected_mean, reward, surprisal
                            )
                            logger.debug(
                                f"🚨 Anomaly detected: expected {expected_mean:.3f}, got {reward:.3f}, surprisal {surprisal:.3f}"
                            )
                    except Exception as e:
                        logger.debug(f"Error in anomaly detection: {e}")

    def _store_anomaly(
        self,
        hypothesis: str,
        expected_reward: float,
        actual_reward: float,
        surprisal: float,
    ) -> None:
        """
        Store anomaly for principle generation based on Eq. 99-100 in theoretical_framework.tex

        Args:
            hypothesis: The hypothesis that caused the anomaly
            expected_reward: Expected reward under current best principle
            actual_reward: Actual observed reward
            surprisal: Surprisal value -log p(y|h, P_MAP)
        """
        anomaly_entry = {
            "hypothesis": hypothesis,
            "expected_reward": expected_reward,
            "actual_reward": actual_reward,
            "surprisal": surprisal,
            "timestamp": datetime.now().isoformat(),
        }
        if not anomaly_entry["actual_reward"] == 0.0:
            self.anomaly_storage.append(anomaly_entry)

        # Keep only recent anomalies (last 50 for memory efficiency)
        if len(self.anomaly_storage) > 50:
            self.anomaly_storage = self.anomaly_storage[-50:]

    def _calculate_principle_uncertainty(self) -> float:
        """
        Calculates the epistemic uncertainty over principles.
        $U^{\\mathrm{EP}}_t = H(p_t(P)) = -\\sum_{P \\in \\mathcal{P}_t} p_t(P) \\log p_t(P)$
        """
        if not self.principle_beliefs:
            return 0.0
        eps = 1e-12
        entropy = -sum(
            p * math.log(max(eps, p))
            for p in self.principle_beliefs.values()
            if p > eps
        )
        return entropy

    async def _estimate_information_gain_bald(self, hypothesis: str) -> float:
        r"""
        Estimates the expected information gain I(h) using the BALD approximation.
        This quantity measures how much testing a hypothesis h is expected to reduce our
        uncertainty about the true principle P.

        - **Theory**: $I(h) = I(P; Y_t \mid H_{t-1}, h_t) = H(P \mid H_{t-1}) - \mathbb{E}_{p(y|h, H_{t-1})}[H(P \mid H_{t-1}, h, y)]$
          % 1. The information gain is the reduction in entropy over principles.

        - **Approximation (BALD)**: The expectation $\mathbb{E}_{p(y|h, H_{t-1})}$ is intractable. We approximate it
          with Monte Carlo sampling.
          $p(y|h, H_{t-1}) = \sum_{P \in \mathcal{P}_t} p(y|h,P) p(P|H_{t-1})$
          % 2. We approximate the evidence p(y|h) by sampling from the predictive posterior.

        - **Engineering**: We simulate `M` possible future outcomes `y_s` based on our current beliefs,
          and for each, we calculate what our new uncertainty `H_new` would be. The average reduction
          in uncertainty is our estimated information gain.
        """
        current_entropy = self._calculate_principle_uncertainty()
        expected_posterior_entropy = 0.0

        # Use more samples if we have few principles to get more reliable estimates
        effective_samples = self.monte_carlo_samples
        if len(self.principles) < 5:
            effective_samples = max(
                self.monte_carlo_samples, 50
            )  # Use more samples for fewer principles

        # Parallelize Monte Carlo simulation
        try:
            # Generate all samples in parallel
            sample_tasks = [
                self._sample_outcome_marginalized(hypothesis)
                for _ in range(effective_samples)
            ]
            sampled_outcomes = await asyncio.gather(*sample_tasks)

            # Compute posterior entropies in parallel
            entropy_tasks = [
                self._calculate_hypothetical_posterior_entropy(hypothesis, outcome)
                for outcome in sampled_outcomes
            ]
            posterior_entropies = await asyncio.gather(*entropy_tasks)

            # Compute expected posterior entropy
            expected_posterior_entropy = sum(posterior_entropies)

            logger.debug(f"MC samples for {hypothesis}...: {sampled_outcomes[:3]}...")

        except Exception as e:
            # Fallback to sequential if parallel fails
            sample_count = 0
            for i in range(effective_samples):
                try:
                    sampled_outcome = await self._sample_outcome_marginalized(
                        hypothesis
                    )
                    posterior_entropy_if_observed = (
                        await self._calculate_hypothetical_posterior_entropy(
                            hypothesis, sampled_outcome
                        )
                    )
                    expected_posterior_entropy += posterior_entropy_if_observed
                    sample_count += 1
                except Exception as sample_e:
                    logger.debug(f"Failed to sample outcome {i}: {sample_e}")
                    continue

            # Adjust if we got fewer samples than expected
            if sample_count > 0:
                expected_posterior_entropy = (
                    expected_posterior_entropy * effective_samples / sample_count
                )

        if effective_samples > 0:
            expected_posterior_entropy /= effective_samples
        else:
            expected_posterior_entropy = current_entropy  # No reduction if no samples

        # Ensure we don't get negative information gain due to numerical issues
        information_gain = max(0.0, current_entropy - expected_posterior_entropy)

        # Track information gain for theoretical metrics
        self._last_information_gain = information_gain  # Remove redundant max
        if hasattr(self, "_cumulative_information_sum"):
            self._cumulative_information_sum += self._last_information_gain

        return information_gain  # Remove double max operation

    def _select_principle_for_round(self) -> Optional[str]:
        """Selects a principle P_t for the current round (using MAP)."""
        return max(self.principle_beliefs, key=self.principle_beliefs.get)

    def _select_principle_thompson_sampling(self) -> Optional[str]:
        """
        Selects a principle P_t using Thompson Sampling for better exploration-exploitation balance.
        This addresses the exploitation bias in pure MAP selection.
        """
        if not self.principle_beliefs:
            return None

        pids = list(self.principle_beliefs.keys())
        beliefs = np.array(list(self.principle_beliefs.values()))

        # Ensure beliefs are normalized and positive
        if np.sum(beliefs) <= 1e-12:
            return np.random.choice(pids)

        beliefs = beliefs / np.sum(beliefs)

        # Sample a principle according to the current belief distribution
        chosen_idx = np.random.choice(len(pids), p=beliefs)
        chosen_pid = pids[chosen_idx]
        return chosen_pid

    async def _sample_outcome_marginalized(self, hypothesis: str) -> float:
        """
        Correctly samples an outcome from the marginal distribution p(y|h)
        by first sampling a principle P ~ p_t(P), then sampling y ~ p(y|h,P).
        """
        noise_std = self.sigma

        if not self.principle_beliefs:
            return np.random.normal(0.0, noise_std)

        # --- Emphasized Logic Change ---
        # 1. Get principles and their posterior probabilities (beliefs)
        pids = list(self.principle_beliefs.keys())
        beliefs = np.array(list(self.principle_beliefs.values()))

        # Ensure normalization
        if beliefs.sum() <= 1e-9:
            chosen_pid = np.random.choice(pids)
        else:
            beliefs = beliefs / beliefs.sum()
            # 2. Sample ONE principle P_i according to the posterior
            chosen_pid = np.random.choice(pids, p=beliefs)

        # 3. Sample ONE outcome y from that principle's predictive distribution
        try:
            if (
                chosen_pid in self.gp_models
                and self.gp_models[chosen_pid].n_observations > 0
            ):
                model = self.gp_models[chosen_pid]
                features = self._extract_features(hypothesis, chosen_pid)
                # Use the GP model's ability to sample from its posterior
                sample_norm = model.sample_prediction(features, n_samples=1)[0]
                final_sample = sample_norm
            else:
                # Fallback: sample from a Gaussian centered at the point prediction
                prediction = await self._get_prediction(
                    hypothesis, chosen_pid, return_normalized=True
                )
                prediction = prediction if prediction is not None else 0.0
                final_sample = np.random.normal(prediction, noise_std)
            return float(final_sample)

        except Exception as e:
            logger.warning(f"Failed to sample from chosen principle {chosen_pid}: {e}")
            return np.random.normal(0.0, noise_std)

    async def _calculate_hypothetical_posterior_entropy(
        self, hypothesis: str, outcome_norm: float
    ) -> float:
        """Calculates what the posterior entropy would be if we observed (hypothesis, outcome)."""
        log_posteriors = {}
        likelihoods_debug = []

        for pid, belief in self.principle_beliefs.items():
            log_prior = math.log(max(belief, 1e-12))  # Avoid log(0)
            likelihood = await self._likelihood(
                outcome_norm, hypothesis, pid, is_normalized_input=True
            )
            log_posteriors[pid] = log_prior + math.log(
                max(likelihood, 1e-12)
            )  # Avoid log(0)
            likelihoods_debug.append(f"{pid}:{likelihood:.2e}")

        if not log_posteriors:
            return 0.0

        # Debug logging for likelihood differences
        # logger.debug(f"Likelihoods for y={outcome:.3f}: {', '.join(likelihoods_debug)}")

        # Numerical stability: log-sum-exp trick
        max_log = max(log_posteriors.values())
        posteriors = {
            pid: math.exp(log_post - max_log)
            for pid, log_post in log_posteriors.items()
        }
        total = sum(posteriors.values())

        if total <= 1e-12:
            logger.warning(f"Very small total posterior mass: {total}")
            return 0.0

        # Normalize and compute entropy
        normalized_posteriors = {pid: p / total for pid, p in posteriors.items()}

        entropy = -sum(
            p * math.log(max(p, 1e-12))
            for p in normalized_posteriors.values()
            if p > 1e-12
        )

        return entropy

    async def _detect_anomalies(self) -> List[Tuple[str, float, float, float]]:
        """
        Detects historical observations poorly explained by the current MAP principle.
        This is the correct implementation based on the PiEvo framework (Eq. 100).

        Returns:
            List of tuples (hypothesis, actual_outcome, expected_outcome, surprisal)
        """
        effective_noise_var = self.sigma**2

        anomalies = []
        if not self.history or not self.principles or not self.principle_beliefs:
            return anomalies

        # 1. Find the principle with MAP (P_t^MAP)
        try:
            map_principle_id = max(
                self.principle_beliefs, key=self.principle_beliefs.get
            )
        except ValueError:
            return anomalies

        # 2. Check if we have the GP model of this MAP principle
        if (
            map_principle_id not in self.gp_models
            or self.gp_models[map_principle_id].n_observations == 0
        ):
            return anomalies

        # 3. Get the GP model of this Principle
        model = self.gp_models[map_principle_id]
        adaptive_threshold = self._calculate_adaptive_anomaly_threshold()
        logger.debug(
            f"Anomaly Detection: Using MAP principle '{map_principle_id}' with threshold {adaptive_threshold:.3f}"
        )

        # 4. travel all the history, compute the surprisal
        for h, y in self.history:
            if y == 0.0:  # Skip failed experiments
                continue
            try:
                # Get the distribution of p(y|h, P_MAP)
                features = self._extract_features(h, map_principle_id)
                expected_mean_norm, expected_var = model.predict(features)

                # Compute all Var, (models, and sigma-controlled noise var)
                total_variance = expected_var + effective_noise_var
                if total_variance <= 1e-6:
                    total_variance = effective_noise_var

                y_norm = self._normalize_y(y)
                normalized_error_sq = (
                    (y_norm - expected_mean_norm) ** 2
                ) / total_variance

                # Surprisal will be mapped into 0~1 (Sigmoid -> [0,1])
                # Follow x -> 1 - exp(-sqrt(x))
                surprisal = 1 - math.exp(-math.sqrt(normalized_error_sq))

                expected_mean_raw = self._denormalize_y(expected_mean_norm)

                # Collect the anomalies.
                if surprisal > adaptive_threshold:
                    logger.warning(
                        f"🚨 Anomaly Detected: h='{str(h)[:20]}...', y={y:.2f}, MAP_pred={expected_mean_raw:.2f}, surprisal={surprisal:.3f}"
                    )
                    anomalies.append((h, y, expected_mean_raw, surprisal))

            except Exception as e:
                logger.debug(f"Error during anomaly detection for (h,y)=({h},{y}): {e}")
                continue

        return anomalies

    def _calculate_principle_discovery_potential(self, principle_id: str) -> float:
        """
        Calculate the discovery potential of a principle based on unexplored regions
        and model uncertainty.
        """
        if principle_id not in self.gp_models:
            return 0.5  # Default prior value

        model = self.gp_models[principle_id]
        if model.n_observations == 0:
            # New principles have high exploration potential
            return 0.9  # High potential for new principles

        # Look at recent history to identify how well this principle has been explored
        if not self.history:
            return 0.5

        # Calculate average uncertainty across recent predictions under this principle
        total_uncertainty = 0.0
        valid_predictions = 0

        for h, y in self.history:
            try:
                features = self._extract_features(h, principle_id)
                _, variance = model.predict(features)
                uncertainty = np.sqrt(variance)
                total_uncertainty += uncertainty
                valid_predictions += 1
            except Exception:
                continue

        if valid_predictions > 0:
            avg_uncertainty = total_uncertainty / valid_predictions
            # Higher uncertainty indicates more unexplored potential
            # Use sigmoid to map to [0,1] range
            discovery_potential = 1.0 - np.exp(-2.0 * avg_uncertainty)
            return discovery_potential
        else:
            return 0.5

    # ══════════════════════════════════════════════════════════
    # 新增：P-H-E 主循环（替代原 AutoGen 三 Agent 轮转）
    # ══════════════════════════════════════════════════════════

    async def run(
        self,
        max_rounds: int = 20,
        available_tools: list | None = None,
    ) -> dict:
        """运行完整的 P-H-E 发现循环。"""
        self.available_tools = available_tools or []

        for round_num in range(max_rounds):
            self.round_by_PHE_order_before_P = round_num

            await self._notify("P", round_num, "Generating/evolving principles...")
            await self._run_principle_phase()

            await self._notify("H", round_num, "Generating hypotheses...")
            hypotheses = await self._run_hypothesis_phase()

            await self._notify("E", round_num, "Selecting and running experiment...")
            outcome = await self._run_experiment_phase(hypotheses)

            if self.on_round_complete:
                await self.on_round_complete(round_num, {
                    "principles": dict(self.principles),
                    "beliefs": dict(self.principle_beliefs),
                    "outcome": outcome,
                })

        return self._build_report()

    async def _notify(self, phase: str, round_num: int, message: str):
        if self.on_progress:
            await self.on_progress({
                "phase": phase, "round": round_num, "message": message,
                "principles_count": len(self.principles),
                "top_belief": max(self.principle_beliefs.values()) if self.principle_beliefs else 0.0,
            })

    async def _run_principle_phase(self):
        await self._update_state()
        anomalies = await self._detect_anomalies()
        guidance_type, guidance_message = get_principle_guidance_prompt(
            anomalies=anomalies,
            num_principles=len(self.principles),
            principle_beliefs=self.principle_beliefs,
            top_principle_text=self._get_top_principle_text(),
            principles=self.principles,
        )
        response = await self.llm_call(self._principle_system_prompt(), guidance_message)
        blocks = extract_json_blocks(response, "principle")
        for block in blocks:
            self._process_principle_submission(block)

    async def _run_hypothesis_phase(self) -> list:
        await self._update_state()
        sorted_p = sorted(self.principle_beliefs.items(), key=lambda x: x[1], reverse=True)
        top_principle_text = self.principles.get(sorted_p[0][0], "") if sorted_p else ""
        second_best = self.principles.get(sorted_p[1][0], "") if len(sorted_p) > 1 else ""
        candidates_done = [{"candidate": h, "outcome": y} for h, y in self.history]
        guidance_type, guidance_message = get_hypothesis_guidance_prompt(
            task=self.task,
            top_principle_text=top_principle_text,
            second_best_principle_text=second_best,
            principle_beliefs=self.principle_beliefs,
            num_principles=len(self.principles),
            candidates_done=candidates_done,
            exploitation_candidates=self.exploitation_candidates,
            principles=self.principles,
        )
        self.is_exploitation_phase = (guidance_type == "EXPLOITATION")
        response = await self.llm_call(self._hypothesis_system_prompt(), guidance_message)
        blocks = extract_json_blocks(response, "hypothesis")
        hypotheses = []
        for block in blocks:
            hypotheses.extend(self._extract_hypotheses_from_json(block))
        return hypotheses

    async def _run_experiment_phase(self, candidates: list) -> dict | None:
        await self._update_state()
        if not candidates:
            return None
        selected = await self._select_via_ids(candidates)
        tool_name, params = self._parse_to_tool_call(selected)
        try:
            result = await self.tool_executor(tool_name, params)
            outcome = float(result.get("outcome", 0.0))
        except Exception:
            outcome = 0.0
        # 同时更新 self.submissions（_extract_history 依赖此格式）
        self.submissions.append({
            "source_agent": "experiment",
            "json_data": [{"candidate": selected, "outcome": outcome}],
        })
        self.history.append((selected, outcome))
        await self._update_beliefs_from_full_history()
        return {"candidate": selected, "outcome": outcome, "tool": tool_name}

    async def _select_via_ids(self, candidates: list) -> str:
        """IDS 实验选择（warm-up / exploitation / exploration）"""
        predictions = {}
        for h in candidates:
            hk = self._candidate_key(h)
            for pid in self.principles:
                p = await self._get_prediction(h, pid, return_normalized=True)
                predictions[(hk, pid)] = p if p is not None else 0.0
        rewards = {}
        for h in candidates:
            hk = self._candidate_key(h)
            rewards[hk] = sum(
                self.principle_beliefs.get(pid, 0) * predictions.get((hk, pid), 0)
                for pid in self.principles
            )
        if len(self.history) < self.warm_up_rounds:
            return await self._select_hypothesis_warm_up(candidates)
        if self.is_exploitation_phase:
            best, best_r = None, float("inf")
            for h in candidates:
                r = await self._calculate_bayesian_regret(h)
                if r < best_r:
                    best_r, best = r, h
            return best
        expected_opt = max(rewards.values()) if rewards else 0.0
        best_r, best_h = float("inf"), candidates[0]
        for h in candidates:
            hk = self._candidate_key(h)
            regret = max(0.0, expected_opt - rewards.get(hk, 0.0))
            ig = await self._estimate_information_gain_bald(h)
            ratio = (regret ** 2) / max(ig, 1e-6)
            if ratio < best_r:
                best_r, best_h = ratio, h
        return best_h

    async def _update_state(self):
        """更新 PiEvo 状态：提取原则 → 分配先验 → 更新 scaler → 训练 GP → 更新信念。"""
        new_principle_ids = self._extract_principles()
        self._handle_new_principles(new_principle_ids)
        self._update_scaler()
        if new_principle_ids:
            self.clear_caches()
        if self.principles:
            await self._train_gp_models()
            await self._update_beliefs_from_full_history()
            if self.history:
                await self._update_regret_and_rewards()

    def _get_top_principle_text(self) -> str:
        if not self.principle_beliefs:
            return ""
        top_id = max(self.principle_beliefs, key=self.principle_beliefs.get)
        return self.principles.get(top_id, "")

    def _parse_to_tool_call(self, candidate: str) -> tuple:
        params = {}
        for part in candidate.split(","):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                k, v = k.strip(), v.strip()
                try:
                    params[k] = float(v)
                except ValueError:
                    params[k] = v
        tool = self.available_tools[0] if self.available_tools else "unknown"
        return tool, params

    def _build_report(self) -> dict:
        best, best_v = None, float("-inf")
        for h, y in self.history:
            if y > best_v:
                best_v, best = y, h
        return {
            "principles": dict(self.principles),
            "beliefs": dict(self.principle_beliefs),
            "best_result": {"candidate": str(best) if best else None, "outcome": best_v},
            "history": [{"candidate": str(h), "outcome": y} for h, y in self.history],
            "rounds_completed": len(self.history),
            "principles_count": len(self.principles),
        }

    @staticmethod
    def _principle_system_prompt() -> str:
        return """[Your Role]
You are the Principle Agent of a scientific discovery system.
Propose scientific principles (laws, mechanisms, patterns) explaining
how parameters affect outcomes.

[Format]
Output ONE JSON object:
```json
{
    "principle_<name>": {
        "RATIONAL": {
            "major premise": "...",
            "minor premises": ["...", "..."]
        },
        "PRINCIPLE_SUBMISSION": "..."
    }
}
```"""

    @staticmethod
    def _hypothesis_system_prompt() -> str:
        return """[Your Role]
You are the Hypothesis Agent. Generate testable hypotheses grounded in
scientific principles.

[Format]
Output ONE JSON with HYPOTHESIS_SUBMISSION_A/B/C:
```json
{
    "HYPOTHESIS_SUBMISSION_A": {
        "rational": {"major premise": "...", "minor premises": ["..."]},
        "candidate": "param1=value1, param2=value2"
    }
}
```"""

    # ── 以下两个辅助方法（如果原版不存在，需要新增）──

    def _extract_hypotheses_from_json(self, json_block: dict) -> list:
        """从 JSON 块中提取所有 HYPOTHESIS_SUBMISSION_* 的 candidate 字段。"""
        results = []
        for key, value in json_block.items():
            if key.startswith("HYPOTHESIS_SUBMISSION"):
                candidate = value.get("candidate", "") if isinstance(value, dict) else str(value)
                if candidate:
                    results.append(candidate)
        return results

    def _process_principle_submission(self, json_block: dict):
        """处理 PrincipleAgent 提交的 JSON 块，提取原则。
        同时更新 self.submissions（_extract_principles 依赖此格式）。"""
        for key, value in json_block.items():
            if key.startswith("principle_"):
                rational = value.get("RATIONAL", {}) if isinstance(value, dict) else {}
                submission_text = value.get("PRINCIPLE_SUBMISSION", "") if isinstance(value, dict) else str(value)
                if submission_text:
                    self.principles[key] = submission_text
                if rational:
                    self.principles_rationals[key] = rational
        # 追加到 submissions（_extract_principles 读取此列表）
        self.submissions.append({
            "source_agent": "principle",
            "json_data": json_block,
        })
