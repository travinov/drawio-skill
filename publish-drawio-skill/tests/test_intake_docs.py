import os
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read_text(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


class DiagramIntakeDocumentationTests(unittest.TestCase):
    def test_skill_routes_non_trivial_requests_through_intake_agent(self):
        skill = read_text("SKILL.md")
        self.assertIn("Step -1", skill)
        self.assertIn("Diagram Intake Agent", skill)
        self.assertIn("references/diagram-intake.md", skill)
        self.assertIn("confirmed diagram brief", skill)

    def test_intake_reference_defines_bounded_semantic_questions(self):
        intake = read_text("references", "diagram-intake.md")
        self.assertIn("question matrix", intake)
        self.assertIn("not a fixed questionnaire", intake)
        self.assertIn("maximum three blocking semantic questions", intake)
        self.assertIn("one question per turn", intake)
        self.assertIn("non-blocking visual", intake)
        self.assertIn("assumptions", intake)
        self.assertNotIn("at most 5 questions", intake)
        self.assertNotIn("Always end non-trivial intake", intake)

    def test_intake_reference_defines_type_and_run_gates(self):
        intake = read_text("references", "diagram-intake.md")
        self.assertIn("explicit type skips confirmation", intake)
        self.assertIn("ambiguous type", intake)
        self.assertIn("native", intake)
        self.assertIn("full run starts only after", intake)

    def test_intake_reference_covers_major_diagram_routes(self):
        intake = read_text("references", "diagram-intake.md")
        for token in (
            "sequence",
            "architecture",
            "C4",
            "ERD",
            "git-flow",
            "process",
            "swimlanes",
        ):
            self.assertIn(token, intake)


if __name__ == "__main__":
    unittest.main()
