#!/usr/bin/env python3
"""Deterministic, host-owned pre-run intake for Draw.io workflows."""
from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Mapping


DIAGRAM_TYPES = (
    "flowchart", "bpmn", "c4", "er", "dependency",
    "sequence", "roadmap", "git-flow", "generic",
)
MAX_BLOCKING_QUESTIONS = 3
HIGH_CONFIDENCE = 0.8


_TYPE_PATTERNS = (
    ("bpmn", r"(?iu)(?:\bbpmn\b|бизнес[- ]процесс)"),
    ("c4", r"(?iu)(?:\bc4\b|context\s+diagram|container\s+diagram|component\s+diagram)"),
    ("er", r"(?iu)(?:\berd?\b|entity.relationship|таблиц|первичн\w*\s+ключ|внешн\w*\s+ключ|\bpk\b|\bfk\b)"),
    ("sequence", r"(?iu)(?:\bsequence\b|диаграмм\w*\s+последовательност|сообщени\w*\s+между)"),
    ("roadmap", r"(?iu)(?:\broadmap\b|дорожн\w*\s+карт|milestone|вех\w*)"),
    ("git-flow", r"(?iu)(?:\bgit[- ]?flow\b|ветк\w*.*(?:merge|релиз|hotfix)|\bhotfix\b)"),
    ("dependency", r"(?iu)(?:\bdependency\b|диаграмм\w*\s+зависимост)"),
    ("flowchart", r"(?iu)(?:\bflowchart\b|блок[- ]схем|диаграмм\w*\s+процесс)"),
)


def _stable_id(prefix: str, value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(encoded).hexdigest()[:20]}"


def _ordered_types(values) -> list[str]:
    selected = set(values)
    return [item for item in DIAGRAM_TYPES if item in selected]


def explicit_type(request: str) -> str | None:
    """Return an explicitly named diagram type, never a low-confidence guess."""
    value = request or ""
    for diagram_type, pattern in _TYPE_PATTERNS:
        if re.search(pattern, value):
            return diagram_type
    if re.search(r"(?iu)(?:\bдиаграмм\w*\b|\bсхем\w*\b).*\bзависимост", value):
        return "dependency"
    return None


def infer_existing_type(
    diagram: Path, evidence: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Preserve an allowlisted type from host evidence or represented structure."""
    evidence = evidence or {}
    candidates = (
        evidence.get("diagram_type"),
        evidence.get("classification"),
        (evidence.get("classification") or {}).get("selected")
        if isinstance(evidence.get("classification"), Mapping) else None,
    )
    for candidate in candidates:
        if candidate in DIAGRAM_TYPES:
            return {
                "diagram_type": candidate,
                "confidence": 1.0,
                "source": str(evidence.get("source") or "existing_evidence"),
            }

    path = Path(diagram)
    if path.is_file():
        try:
            root = ET.parse(path).getroot()
            text = " ".join(
                str(value)
                for element in root.iter()
                for key, value in element.attrib.items()
                if key in {"style", "value", "data-semantic-type", "diagram_type"}
            )
        except (OSError, ET.ParseError):
            text = ""
        structural_patterns = (
            ("bpmn", r"(?iu)\bbpmn\b|swimlane"),
            ("c4", r"(?iu)\bc4\b|c4Type"),
            ("er", r"(?iu)entityRelation|primaryKey|foreignKey"),
            ("sequence", r"(?iu)lifeline|sequence"),
            ("roadmap", r"(?iu)roadmap|milestone"),
            ("git-flow", r"(?iu)git[- ]?flow|hotfix"),
        )
        for diagram_type, pattern in structural_patterns:
            if re.search(pattern, text):
                return {
                    "diagram_type": diagram_type,
                    "confidence": 0.95,
                    "source": "existing_structure",
                }
    return {"diagram_type": None, "confidence": 0.0, "source": "unknown"}


def classify_request(request: str) -> dict[str, Any]:
    """Classify only strong lexical signals; weak requests remain ambiguous."""
    selected = explicit_type(request)
    if selected:
        return {
            "diagram_type": selected,
            "confidence": 0.95,
            "alternatives": [],
        }
    value = request or ""
    if re.search(r"(?iu)сервис\w*|систем\w*|интеграц", value) and re.search(
        r"(?iu)зависимост|связ|взаимодейств", value
    ):
        return {
            "diagram_type": "dependency",
            "confidence": 0.65,
            "alternatives": ["c4"],
        }
    return {
        "diagram_type": "generic",
        "confidence": 0.4,
        "alternatives": ["flowchart"],
    }


def _proposal(
    *,
    prompt: str,
    reason: str,
    recommended: Mapping[str, Any],
    choices: list[Mapping[str, Any]],
    allow_free_text: bool = True,
    kind: str = "semantic",
) -> dict[str, Any]:
    canonical = {
        "kind": kind,
        "prompt": str(prompt).strip(),
        "reason": str(reason).strip(),
        "recommended": {
            "value": str(recommended["value"]),
            "label": str(recommended["label"]),
        },
        "choices": [
            {"value": str(item["value"]), "label": str(item["label"])}
            for item in choices
        ],
        "allow_free_text": bool(allow_free_text),
    }
    canonical["question_id"] = _stable_id("question", canonical)
    return canonical


def _normalize_question(value: Any, index: int) -> dict[str, Any]:
    if isinstance(value, str):
        return _proposal(
            prompt=value,
            reason="Ответ меняет семантику или топологию диаграммы.",
            recommended={"value": "уточнить", "label": "Уточнить"},
            choices=[{"value": "уточнить", "label": "Уточнить"}],
        )
    if not isinstance(value, Mapping):
        raise ValueError(f"blocking question {index} must be text or an object")
    if "question_id" in value:
        raise ValueError("Semantic Analyst must not assign question ids")
    required = ("prompt", "reason", "recommended", "choices")
    if any(key not in value for key in required):
        raise ValueError(f"blocking question {index} is incomplete")
    recommended = value["recommended"]
    choices = value["choices"]
    if (
        not isinstance(recommended, Mapping)
        or not isinstance(choices, list)
        or not choices
    ):
        raise ValueError(f"blocking question {index} has invalid choices")
    return _proposal(
        prompt=value["prompt"],
        reason=value["reason"],
        recommended=recommended,
        choices=choices,
        allow_free_text=value.get("allow_free_text", True),
        kind="semantic",
    )


def blocking_gaps(
    request: str, diagram_type: str, answers: list[dict]
) -> list[dict[str, Any]]:
    """Return conservative host fallbacks only for truly underspecified requests."""
    if answers or len((request or "").split()) >= 5:
        return []
    prompts = {
        "sequence": (
            "Какие участники и обязательные альтернативные или ошибочные ветки нужны?",
            "Участники и alt/error ветки определяют сообщения и топологию.",
        ),
        "dependency": (
            "Какие сервисы входят в границу схемы и какие зависимости обязательны?",
            "Граница и обязательные связи определяют вершины и ребра.",
        ),
        "c4": (
            "Какой уровень C4 и какие системные границы нужно показать?",
            "Уровень и границы определяют допустимые элементы C4.",
        ),
        "er": (
            "Какие таблицы и связи PK/FK обязательны?",
            "Список сущностей и ключей определяет структуру ER.",
        ),
        "flowchart": (
            "Какие обязательные решения, ошибочные ветки и завершения есть в процессе?",
            "Решения и возвратные ребра определяют топологию процесса.",
        ),
    }
    if diagram_type not in prompts:
        return []
    prompt, reason = prompts[diagram_type]
    return [_proposal(
        prompt=prompt,
        reason=reason,
        recommended={"value": "описать", "label": "Описать обязательные элементы"},
        choices=[{"value": "описать", "label": "Описать обязательные элементы"}],
    )]


def _answer_map(answers: list[dict]) -> dict[str, str]:
    result = {}
    for answer in answers or []:
        if not isinstance(answer, Mapping):
            raise ValueError("intake answer must be an object")
        question_id = answer.get("question_id")
        text = answer.get("text")
        if not isinstance(question_id, str) or not isinstance(text, str):
            raise ValueError("intake answer requires string question_id and text")
        if question_id in result and result[question_id] != text:
            raise ValueError(f"conflicting answers for {question_id}")
        result[question_id] = text
    return result


def _classification_question(candidates: list[str]) -> dict[str, Any]:
    choices = [
        {"value": item, "label": item}
        for item in candidates
    ]
    recommended = choices[-1]
    return _proposal(
        prompt="Какой тип диаграммы лучше отражает задачу?",
        reason="Несколько допустимых представлений по-разному задают семантику.",
        recommended=recommended,
        choices=choices,
        kind="classification",
    )


def _assumption(text: str, accepted: bool) -> dict[str, Any]:
    value = {"text": str(text).strip(), "accepted": bool(accepted)}
    return {"note_id": _stable_id("assumption", value["text"]), **value}


def advance(
    *,
    request,
    mode,
    existing_evidence,
    answers,
    analysis,
    accept_assumptions=False,
) -> dict[str, Any]:
    """Advance one deterministic intake turn from immutable analyst proposals."""
    if not isinstance(analysis, Mapping):
        raise ValueError("intake analysis must be an object")
    proposed_type = analysis.get("diagram_type")
    alternatives = analysis.get("alternatives", [])
    if proposed_type not in DIAGRAM_TYPES:
        raise ValueError(f"unsupported diagram type: {proposed_type!r}")
    if not isinstance(alternatives, list) or any(
        item not in DIAGRAM_TYPES for item in alternatives
    ):
        raise ValueError("analysis alternatives contain an unsupported diagram type")
    confidence = analysis.get("confidence", 0)
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise ValueError("analysis confidence must be numeric")

    bound_answers = _answer_map(answers or [])
    selected = explicit_type(request)
    source = "explicit" if selected else None
    existing = existing_evidence or {}
    existing_type = existing.get("diagram_type") if isinstance(existing, Mapping) else None
    if not selected and existing_type in DIAGRAM_TYPES:
        selected, source = existing_type, "existing"

    candidates = _ordered_types([*alternatives, proposed_type])
    classification_question = None
    if not selected and confidence >= HIGH_CONFIDENCE:
        selected, source = proposed_type, "analysis"
    elif not selected:
        classification_question = _classification_question(candidates)
        human_value = bound_answers.get(classification_question["question_id"])
        if human_value in candidates:
            selected, source = human_value, "human"

    classification = {
        "selected": selected,
        "source": source or "pending",
        "confidence": 1.0 if source in {"explicit", "existing", "human"} else float(confidence),
        "candidates": [selected] if selected and source in {"explicit", "existing"} else candidates,
    }
    if selected and selected not in classification["candidates"]:
        classification["candidates"] = _ordered_types(
            [*classification["candidates"], selected]
        )

    if not selected:
        unknown_answer_ids = sorted(
            set(bound_answers) - {classification_question["question_id"]}
        )
        if unknown_answer_ids:
            raise ValueError(
                "intake answers are not bound to host questions: "
                + ", ".join(unknown_answer_ids)
            )
        return {
            "schema_version": 1,
            "status": "awaiting_input",
            "mode": mode,
            "classification": classification,
            "questions": [classification_question],
            "answers": list(answers or []),
            "assumptions": [],
            "completeness": 0.0,
        }

    proposals = [
        _normalize_question(value, index)
        for index, value in enumerate(analysis.get("blocking_questions", []), 1)
    ]
    if not proposals and analysis.get("sufficient") is False:
        proposals = blocking_gaps(request, selected, list(answers or []))

    remaining = proposals[MAX_BLOCKING_QUESTIONS:]
    consolidated = None
    if remaining:
        consolidated = _proposal(
            prompt=(
                "Уточните одним ответом оставшиеся обязательные детали: "
                + "; ".join(question["prompt"] for question in remaining)
            ),
            reason="Лимит из трех последовательных блокирующих вопросов исчерпан.",
            recommended={
                "value": "accept_assumptions",
                "label": "Принять перечисленные допущения",
            },
            choices=[{
                "value": "accept_assumptions",
                "label": "Принять перечисленные допущения",
            }],
            kind="consolidated",
        )
    acceptance = None
    if analysis.get("assumptions"):
        acceptance = _proposal(
            prompt="Подтвердить перечисленные неблокирующие допущения?",
            reason="Хост не применяет предложенные моделью допущения без явного принятия.",
            recommended={"value": "accept", "label": "Принять допущения"},
            choices=[{"value": "accept", "label": "Принять допущения"}],
            kind="assumption_acceptance",
        )
    known_question_ids = {
        question["question_id"] for question in proposals
    }
    if classification_question is not None:
        known_question_ids.add(classification_question["question_id"])
    if consolidated is not None:
        known_question_ids.add(consolidated["question_id"])
    if acceptance is not None:
        known_question_ids.add(acceptance["question_id"])
    unknown_answer_ids = sorted(set(bound_answers) - known_question_ids)
    if unknown_answer_ids:
        raise ValueError(
            "intake answers are not bound to host questions: "
            + ", ".join(unknown_answer_ids)
        )

    normalized_answers = list(answers or [])
    for question in proposals[:MAX_BLOCKING_QUESTIONS]:
        if question["question_id"] not in bound_answers:
            return {
                "schema_version": 1,
                "status": "awaiting_input",
                "mode": mode,
                "classification": classification,
                "questions": [question],
                "answers": normalized_answers,
                "assumptions": [],
                "completeness": min(
                    0.95,
                    len(bound_answers) / max(1, len(proposals) + 1),
                ),
            }

    assumptions = [
        _assumption(str(value), False)
        for value in analysis.get("assumptions", [])
    ]
    if remaining:
        remaining_assumptions = [
            _assumption(
                f"Неуточненный блокирующий вопрос: {question['prompt']}",
                False,
            )
            for question in remaining
        ]
        consolidated_answer = bound_answers.get(consolidated["question_id"])
        accept_remaining = bool(
            accept_assumptions or consolidated_answer == "accept_assumptions"
        )
        if not accept_remaining and consolidated_answer is None:
            return {
                "schema_version": 1,
                "status": "awaiting_input",
                "mode": mode,
                "classification": classification,
                "questions": [consolidated],
                "answers": normalized_answers,
                "assumptions": [*assumptions, *remaining_assumptions],
                "completeness": 0.95,
            }
        if accept_remaining:
            assumptions = [
                {**item, "accepted": True}
                for item in [*assumptions, *remaining_assumptions]
            ]
        # Any other free-text reply is a bound clarification of the remaining
        # semantic gaps. It resolves those gaps; it is not assumption consent.

    if assumptions and not all(item["accepted"] for item in assumptions):
        acceptance_answer = bound_answers.get(acceptance["question_id"])
        if accept_assumptions or acceptance_answer == "accept":
            assumptions = [{**item, "accepted": True} for item in assumptions]
        elif acceptance_answer:
            assumptions = [
                _assumption(
                    f"Пользователь уточнил визуальные условия: {acceptance_answer}",
                    True,
                )
            ]
        else:
            return {
                "schema_version": 1,
                "status": "awaiting_input",
                "mode": mode,
                "classification": classification,
                "questions": [acceptance],
                "answers": normalized_answers,
                "assumptions": assumptions,
                "completeness": 0.95,
            }

    return {
        "schema_version": 1,
        "status": "complete",
        "mode": mode,
        "classification": classification,
        "questions": [],
        "answers": normalized_answers,
        "assumptions": assumptions,
        "completeness": 1.0,
    }
