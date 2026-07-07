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

    def test_intake_reference_defines_matrix_and_free_form_question(self):
        intake = read_text("references", "diagram-intake.md")
        self.assertIn("question matrix", intake)
        self.assertIn("not a fixed questionnaire", intake)
        self.assertIn("at most 5 questions", intake)
        self.assertIn("optional free-form visual preference question", intake)
        self.assertIn("If the user does not answer", intake)

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
