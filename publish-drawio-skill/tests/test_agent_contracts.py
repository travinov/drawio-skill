import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class AgentRoleContractTests(unittest.TestCase):
    def read(self, relative):
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_all_isolated_roles_have_no_tools_or_extension_context(self):
        for relative in (
            "agents/diagram-supervisor.md",
            "agents/diagram-semantic-analyst.md",
            "agents/diagram-reviewer.md",
            "agents/diagram-repair.md",
        ):
            with self.subTest(role=relative):
                text = self.read(relative)
                self.assertIn("tools: []", text)
                self.assertIn("no extension context", text.lower())
                self.assertIn("immutable JSON", text)

    def test_supervisor_is_a_non_coordinating_layout_strategy_advisor(self):
        text = self.read("agents/diagram-supervisor.md")
        for action in (
            "create_layout",
            "reroute_edges",
            "expand_local_scope",
            "retry_layout_strategy",
            "request_semantic_clarification",
            "finish_best_effort",
        ):
            self.assertIn(f"`{action}`", text)
        self.assertIn("never coordinate", text.lower())
        self.assertNotIn("required_roles", text)

    def test_semantic_analyst_omits_ordinary_layout_geometry(self):
        text = self.read("agents/diagram-semantic-analyst.md")
        self.assertIn("Do not return ordinary routes, coordinates, or geometry.", text)
        self.assertIn("intake/type/semantic completeness", text)

    def test_semantic_v2_contract_stays_topology_only_in_docs_and_schema(self):
        analysis_schema = json.loads(self.read("data/semantic-analysis.v2.schema.json"))
        plan_schema = json.loads(self.read("data/semantic-plan.v2.schema.json"))

        self.assertNotIn("route", analysis_schema["$defs"]["edge"]["properties"])
        self.assertIn("route", plan_schema["$defs"]["edge"]["properties"])

        for relative in ("README.md", "SKILL.md", "agents/diagram-semantic-analyst.md"):
            with self.subTest(document=relative):
                text = self.read(relative).lower()
                self.assertIn("do not return ordinary routes, coordinates, or geometry", text)

    def test_layout_repair_returns_bounded_intent_only(self):
        text = self.read("agents/diagram-repair.md")
        self.assertIn("bounded `layout-repair-intent`", text)
        self.assertIn("no coordinates, waypoints, XML", text)

    def test_reviewer_receives_immutable_layout_evidence_and_cannot_approve_blockers(self):
        text = self.read("agents/diagram-reviewer.md")
        lowered = text.lower()
        for field in (
            "immutable backend proof",
            "changed sets",
            "locked sets",
            "congestion metrics",
            "quality profile version",
        ):
            self.assertIn(field, lowered)
        self.assertIn("blocking deterministic validator finding", lowered)
        self.assertIn("cannot be approved", lowered)

    def test_roles_never_search_or_discover_openspec(self):
        for relative in (
            "agents/diagram-supervisor.md",
            "agents/diagram-semantic-analyst.md",
            "agents/diagram-reviewer.md",
            "agents/diagram-repair.md",
        ):
            with self.subTest(role=relative):
                text = self.read(relative).lower()
                self.assertIn("do not discover, search for, or select repository specifications", text)
                self.assertIn("openspec material is ordinary document content only", text)


if __name__ == "__main__":
    unittest.main()
