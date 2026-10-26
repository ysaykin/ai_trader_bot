"""Unit tests for the multi-head neural policy network."""

from __future__ import annotations

import math
import unittest

from ml.policy_net import (
    MAX_PROFIT_TARGET,
    MIN_PROFIT_TARGET,
    POLICY_ACTIONS,
    MultiHeadPolicyNetwork,
)


class MultiHeadPolicyNetworkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.network = MultiHeadPolicyNetwork(
            feature_count=3,
            regime_size=6,
            strategy_count=20,
            seed=17,
        )
        self.sequence = (
            (0.10, -0.20, 0.30),
            (0.15, -0.10, 0.25),
            (0.20, 0.05, 0.10),
        )

    def test_all_heads_have_valid_shapes_and_ranges(self) -> None:
        output = self.network.forward([self.sequence, self.sequence])

        self.assertEqual(len(output.regime_embeddings), 2)
        self.assertTrue(all(len(row) == 6 for row in output.regime_embeddings))
        self.assertTrue(all(len(row) == 4 for row in output.action_probabilities))
        self.assertTrue(all(len(row) == 20 for row in output.strategy_probabilities))
        self.assertTrue(
            all(math.isclose(sum(row), 1.0) for row in output.action_probabilities)
        )
        self.assertTrue(
            all(math.isclose(sum(row), 1.0) for row in output.strategy_probabilities)
        )
        self.assertTrue(
            all(
                MIN_PROFIT_TARGET <= target <= MAX_PROFIT_TARGET
                for target in output.profit_targets
            )
        )

    def test_temporal_encoder_is_deterministic_and_order_sensitive(self) -> None:
        first = self.network.predict(self.sequence)
        repeated = self.network.predict(self.sequence)
        reversed_sequence = self.network.predict(tuple(reversed(self.sequence)))

        self.assertEqual(first, repeated)
        self.assertNotEqual(first.regime_embedding, reversed_sequence.regime_embedding)

    def test_recommendation_matches_each_head_argmax(self) -> None:
        recommendation = self.network.predict(self.sequence)

        self.assertIn(recommendation.action, POLICY_ACTIONS)
        self.assertEqual(
            recommendation.action,
            max(
                recommendation.action_probabilities,
                key=recommendation.action_probabilities.__getitem__,
            ),
        )
        self.assertEqual(
            recommendation.strategy_index,
            recommendation.strategy_probabilities.index(
                max(recommendation.strategy_probabilities)
            )
            + 1,
        )
        self.assertEqual(
            recommendation.strategy_name,
            f"strategy_{recommendation.strategy_index:02d}",
        )

    def test_invalid_inputs_and_state_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.network.forward([])
        with self.assertRaises(ValueError):
            self.network.forward([[]])
        with self.assertRaises(ValueError):
            self.network.forward([[(1.0, 2.0)]])
        with self.assertRaises(ValueError):
            self.network.forward([[(1.0, 2.0, float("nan"))]])

        state = self.network.state_dict()
        state.pop("action_bias")
        with self.assertRaises(ValueError):
            self.network.load_state_dict(state)


if __name__ == "__main__":
    unittest.main()
