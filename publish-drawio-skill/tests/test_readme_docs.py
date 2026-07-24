import os
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def readme():
    with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fh:
        return fh.read()


class ReadmeDocumentationTests(unittest.TestCase):
    def test_readme_describes_extension_and_intake(self):
        text = readme()
        self.assertIn("drawio-skill extension", text)
        self.assertIn("Diagram Intake Agent", text)
        self.assertIn("confirmed diagram brief", text)

    def test_readme_lists_supported_diagrams_and_examples(self):
        text = readme()
        for token in (
            "git-flow",
            "sequence",
            "C4",
            "ERD",
            "UML",
            "Terraform",
            "Kubernetes",
            "OpenSpec",
        ):
            self.assertIn(token, text)

    def test_readme_documents_install_validation_and_corporate_constraints(self):
        text = readme()
        for token in (
            "SberUserSoft",
            "DRAWIO_BIN",
            "gitflow_validate.py",
            "validate.py",
            "внешние CDN",
        ):
            self.assertIn(token, text)

    def test_readme_documents_deterministic_resume_selection(self):
        text = " ".join(readme().split())
        for token in (
            "`/drawio:resume` без `--run` выбирает последний pending run "
            "по детерминированной host policy",
            "`/drawio:resume --run <run-id>` выбирает указанный run",
            "Если pending run отсутствует, команда возвращает ошибку",
            "не повторяет уже пройденные intake-вопросы",
        ):
            self.assertIn(token, text)


if __name__ == "__main__":
    unittest.main()
