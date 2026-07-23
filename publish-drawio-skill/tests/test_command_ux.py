import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import command_ux


class IntakeCommandUXTests(unittest.TestCase):
    def test_intake_answers_parse_stable_question_bindings(self):
        answers = command_ux.parse_intake_answers([
            "question-aaaaaaaaaaaaaaaaaaaa=dependency",
            '{"question_id":"question-bbbbbbbbbbbbbbbbbbbb","text":"Свободный ответ"}',
        ])
        self.assertEqual(answers, [
            {
                "question_id": "question-aaaaaaaaaaaaaaaaaaaa",
                "text": "dependency",
            },
            {
                "question_id": "question-bbbbbbbbbbbbbbbbbbbb",
                "text": "Свободный ответ",
            },
        ])

    def test_awaiting_input_payload_carries_native_selection_and_replay(self):
        question = {
            "question_id": "question-aaaaaaaaaaaaaaaaaaaa",
            "kind": "classification",
            "prompt": "Какой тип схемы нужен?",
            "reason": "Есть два допустимых представления.",
            "recommended": {"value": "dependency", "label": "Dependency"},
            "choices": [
                {"value": "c4", "label": "C4"},
                {"value": "dependency", "label": "Dependency"},
            ],
            "allow_free_text": True,
        }
        payload = command_ux.intake_awaiting_input(
            intake_id="intake-123",
            question=question,
            command="create",
        )
        self.assertEqual(payload["status"], "awaiting_input")
        self.assertEqual(payload["selection_required"]["question"], question)
        replay = payload["selection_required"]["replay"]
        self.assertEqual(replay["intake_id"], "intake-123")
        self.assertIn("--intake-id", replay["command"])
        self.assertIn("--intake-answer", replay["command"])

    def test_qwen_transport_accepts_hidden_intake_flags(self):
        tokens = command_ux.qwen_command_tokens(
            '--intake-id intake-123 '
            '--intake-answer "question-aaaaaaaaaaaaaaaaaaaa=dependency" '
            '--accept-intake-assumptions "Покажи зависимости"'
        )
        self.assertEqual(tokens[0:2], ["--intake-id", "intake-123"])
        self.assertIn("--accept-intake-assumptions", tokens)


if __name__ == "__main__":
    unittest.main()
