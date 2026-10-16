"""Phase machine: legal transitions, terminal states, and the delivery gate set."""

from __future__ import annotations

import unittest

from swarmkit import phases
from swarmkit.errors import TransitionError


class TestTransitions(unittest.TestCase):
    def test_happy_path_is_legal(self) -> None:
        path = [phases.INTAKE, phases.PLAN, phases.MESH, phases.INTEGRATE, phases.QC, phases.RETRO, phases.DELIVERED]
        for current, nxt in zip(path, path[1:]):
            with self.subTest(current=current, nxt=nxt):
                self.assertTrue(phases.can_transition(current, nxt))
                phases.assert_transition(current, nxt)

    def test_skipping_integrate_is_refused(self) -> None:
        """The bug this guards: an orchestrator jumping QC -> DELIVERED."""
        self.assertFalse(phases.can_transition(phases.QC, phases.DELIVERED))
        with self.assertRaises(TransitionError) as ctx:
            phases.assert_transition(phases.QC, phases.DELIVERED)
        self.assertEqual(ctx.exception.code, "ILLEGAL_TRANSITION")

    def test_cannot_go_backwards(self) -> None:
        for current, nxt in [
            (phases.PLAN, phases.INTAKE),
            (phases.INTEGRATE, phases.MESH),
            (phases.RETRO, phases.QC),
        ]:
            with self.subTest(current=current):
                self.assertFalse(phases.can_transition(current, nxt))

    def test_abort_is_legal_from_every_live_phase(self) -> None:
        for phase in phases.PHASES:
            if phase in phases.TERMINAL:
                continue
            with self.subTest(phase=phase):
                self.assertTrue(phases.can_transition(phase, phases.ABORTED))

    def test_terminal_phases_go_nowhere(self) -> None:
        for phase in phases.TERMINAL:
            for nxt in phases.PHASES + (phases.ABORTED,):
                with self.subTest(phase=phase, nxt=nxt):
                    self.assertFalse(phases.can_transition(phase, nxt))

    def test_unknown_phase_is_refused(self) -> None:
        self.assertFalse(phases.is_phase("SHIPPED"))
        self.assertFalse(phases.can_transition(phases.INTAKE, "SHIPPED"))
        self.assertFalse(phases.can_transition("NONSENSE", phases.PLAN))

    def test_fix_loop_is_the_only_cycle(self) -> None:
        self.assertTrue(phases.can_transition(phases.QC, phases.FIX))
        self.assertTrue(phases.can_transition(phases.FIX, phases.QC))
        self.assertFalse(phases.can_transition(phases.FIX, phases.RETRO))

    def test_unfinished_excludes_terminal(self) -> None:
        self.assertNotIn(phases.DELIVERED, phases.UNFINISHED)
        self.assertNotIn(phases.ABORTED, phases.UNFINISHED)
        self.assertIn(phases.INTEGRATE, phases.UNFINISHED)

    def test_only_delivery_is_gated(self) -> None:
        self.assertEqual(phases.DELIVERY_GATED, frozenset({phases.DELIVERED}))

    def test_describe_mentions_every_phase(self) -> None:
        text = phases.describe()
        for phase in phases.PHASES:
            self.assertIn(phase, text)


if __name__ == "__main__":
    unittest.main()