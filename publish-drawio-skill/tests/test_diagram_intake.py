import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import diagram_intake


def analysis(**overrides):
    value = {
        "diagram_type": "flowchart",
        "confidence": 0.95,
        "alternatives": [],
        "sufficient": True,
        "blocking_questions": [],
        "assumptions": [],
    }
    value.update(overrides)
    return value


def proposed_question(number):
    return {
        "prompt": f"Вопрос {number}?",
        "reason": f"Причина {number}.",
        "recommended": {"value": f"answer-{number}", "label": f"Ответ {number}"},
        "choices": [{"value": f"answer-{number}", "label": f"Ответ {number}"}],
        "allow_free_text": True,
    }


class DiagramIntakeTests(unittest.TestCase):
    def test_diagram_types_have_exact_stable_order(self):
        self.assertEqual(
            diagram_intake.DIAGRAM_TYPES,
            (
                "flowchart", "bpmn", "c4", "er", "dependency",
                "sequence", "roadmap", "git-flow", "generic",
            ),
        )

    def test_explicit_type_skips_classification_confirmation(self):
        result = diagram_intake.advance(
            request="Создай sequence диаграмму вызовов API",
            mode="create",
            existing_evidence=None,
            answers=[],
            analysis=analysis(diagram_type="dependency", confidence=0.4),
        )
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["classification"]["selected"], "sequence")
        self.assertEqual(result["classification"]["source"], "explicit")
        self.assertEqual(result["questions"], [])

    def test_high_confidence_request_classification_is_automatic(self):
        classified = diagram_intake.classify_request(
            "Покажи таблицы, первичные и внешние ключи"
        )
        self.assertEqual(classified["diagram_type"], "er")
        self.assertGreaterEqual(classified["confidence"], 0.8)

    def test_ambiguous_type_returns_one_native_selection(self):
        result = diagram_intake.advance(
            request="Покажи сервисы и их зависимости",
            mode="create",
            existing_evidence=None,
            answers=[],
            analysis=analysis(
                diagram_type="dependency",
                confidence=0.55,
                alternatives=["c4"],
            ),
        )
        self.assertEqual(result["status"], "awaiting_input")
        self.assertEqual(result["classification"]["candidates"], ["c4", "dependency"])
        self.assertLessEqual(len(result["questions"]), 1)
        question = result["questions"][0]
        self.assertTrue(question["question_id"].startswith("question-"))
        self.assertEqual(len(question["question_id"]), len("question-") + 20)
        for field in (
            "prompt", "reason", "recommended", "choices", "allow_free_text"
        ):
            self.assertIn(field, question)

    def test_existing_diagram_type_is_preserved_from_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            diagram = Path(temporary) / "existing.drawio"
            diagram.write_text("<mxfile/>", encoding="utf-8")
            inferred = diagram_intake.infer_existing_type(
                diagram, {"diagram_type": "bpmn", "source": "diagram-metadata"}
            )
        self.assertEqual(inferred["diagram_type"], "bpmn")
        result = diagram_intake.advance(
            request="Сделай схему понятнее",
            mode="improve",
            existing_evidence=inferred,
            answers=[],
            analysis=analysis(diagram_type="flowchart", confidence=0.99),
        )
        self.assertEqual(result["classification"]["selected"], "bpmn")
        self.assertEqual(result["classification"]["source"], "existing")

    def test_questions_are_sequential_and_fourth_is_consolidated(self):
        model_analysis = analysis(
            sufficient=False,
            blocking_questions=[proposed_question(index) for index in range(1, 6)],
        )
        answers = []
        asked = []
        for _ in range(3):
            result = diagram_intake.advance(
                request="Нарисуй flowchart оплаты",
                mode="create",
                existing_evidence=None,
                answers=answers,
                analysis=model_analysis,
            )
            self.assertEqual(result["status"], "awaiting_input")
            self.assertEqual(len(result["questions"]), 1)
            question = result["questions"][0]
            asked.append(question["question_id"])
            answers.append({
                "question_id": question["question_id"],
                "text": question["recommended"]["value"],
            })
        result = diagram_intake.advance(
            request="Нарисуй flowchart оплаты",
            mode="create",
            existing_evidence=None,
            answers=answers,
            analysis=model_analysis,
        )
        self.assertEqual(len(set(asked)), 3)
        self.assertEqual(result["status"], "awaiting_input")
        self.assertEqual(len(result["questions"]), 1)
        self.assertEqual(result["questions"][0]["kind"], "consolidated")
        self.assertTrue(result["questions"][0]["allow_free_text"])

    def test_accepting_remaining_host_assumptions_completes_after_limit(self):
        model_analysis = analysis(
            sufficient=False,
            blocking_questions=[proposed_question(index) for index in range(1, 5)],
        )
        answers = []
        for _ in range(3):
            pending = diagram_intake.advance(
                request="Нарисуй flowchart оплаты", mode="create",
                existing_evidence=None, answers=answers, analysis=model_analysis,
            )
            question = pending["questions"][0]
            answers.append({
                "question_id": question["question_id"],
                "text": question["recommended"]["value"],
            })
        completed = diagram_intake.advance(
            request="Нарисуй flowchart оплаты", mode="create",
            existing_evidence=None, answers=answers, analysis=model_analysis,
            accept_assumptions=True,
        )
        self.assertEqual(completed["status"], "complete")
        self.assertTrue(completed["assumptions"])
        self.assertTrue(all(item["accepted"] for item in completed["assumptions"]))

    def test_nonblocking_assumptions_require_explicit_acceptance(self):
        pending = diagram_intake.advance(
            request="Нарисуй flowchart оплаты",
            mode="create",
            existing_evidence=None,
            answers=[],
            analysis=analysis(assumptions=["Использовать встроенный стиль"]),
        )
        self.assertEqual(pending["status"], "awaiting_input")
        completed = diagram_intake.advance(
            request="Нарисуй flowchart оплаты",
            mode="create",
            existing_evidence=None,
            answers=[],
            analysis=analysis(assumptions=["Использовать встроенный стиль"]),
            accept_assumptions=True,
        )
        self.assertEqual(completed["status"], "complete")

    def test_assumption_acceptance_consumes_the_bound_accept_answer(self):
        pending = diagram_intake.advance(
            request="Нарисуй flowchart оплаты",
            mode="create",
            existing_evidence=None,
            answers=[],
            analysis=analysis(assumptions=["Использовать встроенный стиль"]),
        )
        question = pending["questions"][0]
        completed = diagram_intake.advance(
            request="Нарисуй flowchart оплаты",
            mode="create",
            existing_evidence=None,
            answers=[{
                "question_id": question["question_id"],
                "text": "accept",
            }],
            analysis=analysis(assumptions=["Использовать встроенный стиль"]),
        )
        self.assertEqual(completed["status"], "complete")
        self.assertTrue(all(item["accepted"] for item in completed["assumptions"]))

    def test_consolidated_free_text_resolves_gaps_without_accepting_assumptions(self):
        model_analysis = analysis(
            sufficient=False,
            blocking_questions=[proposed_question(index) for index in range(1, 5)],
            assumptions=["Использовать встроенный стиль"],
        )
        answers = []
        for _ in range(3):
            pending = diagram_intake.advance(
                request="Нарисуй flowchart оплаты", mode="create",
                existing_evidence=None, answers=answers, analysis=model_analysis,
            )
            question = pending["questions"][0]
            answers.append({
                "question_id": question["question_id"],
                "text": question["recommended"]["value"],
            })
        consolidated = diagram_intake.advance(
            request="Нарисуй flowchart оплаты", mode="create",
            existing_evidence=None, answers=answers, analysis=model_analysis,
        )
        answers.append({
            "question_id": consolidated["questions"][0]["question_id"],
            "text": "Возврат идет к повторной проверке оплаты",
        })
        pending_acceptance = diagram_intake.advance(
            request="Нарисуй flowchart оплаты", mode="create",
            existing_evidence=None, answers=answers, analysis=model_analysis,
        )
        self.assertEqual(pending_acceptance["status"], "awaiting_input")
        self.assertEqual(
            pending_acceptance["questions"][0]["kind"],
            "assumption_acceptance",
        )
        self.assertEqual(
            [item["text"] for item in pending_acceptance["assumptions"]],
            ["Использовать встроенный стиль"],
        )
        self.assertFalse(any(
            item["accepted"] for item in pending_acceptance["assumptions"]
        ))

    def test_host_rejects_model_owned_ids_and_unknown_types(self):
        invalid_question = proposed_question(1)
        invalid_question["question_id"] = "model-owned"
        with self.assertRaises(ValueError):
            diagram_intake.advance(
                request="Схема",
                mode="create",
                existing_evidence=None,
                answers=[],
                analysis=analysis(
                    diagram_type="mind-map",
                    blocking_questions=[invalid_question],
                ),
            )

    def test_host_rejects_answer_not_bound_to_a_host_question(self):
        with self.assertRaises(ValueError):
            diagram_intake.advance(
                request="Нарисуй flowchart оплаты",
                mode="create",
                existing_evidence=None,
                answers=[{
                    "question_id": "question-deadbeefdeadbeefdead",
                    "text": "accept",
                }],
                analysis=analysis(),
            )


if __name__ == "__main__":
    unittest.main()
