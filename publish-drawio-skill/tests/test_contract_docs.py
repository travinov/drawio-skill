import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class ContractDocumentationTests(unittest.TestCase):
    def read(self, path):
        return (ROOT / path).read_text(encoding="utf-8")

    def test_skill_documents_versioned_source_aware_quality_gate(self):
        skill = self.read("SKILL.md")
        for text in (
            "schema_version: 1",
            "contract.version.missing",
            "--profile roadmap",
            "--profile gitflow",
            "scripts/self_check.py --check-registry",
            "scripts/verify_determinism.py",
            "scripts/export_smoke.py",
            "separate\n`bpmn-architect` skill",
        ):
            self.assertIn(text, skill)

    def test_references_match_schema_scales_and_event_variants(self):
        roadmap = self.read("references/roadmap.md")
        gitflow = self.read("references/git-flow.md")
        roadmap_schema = json.loads(self.read("data/roadmap.v1.schema.json"))
        gitflow_schema = json.loads(self.read("data/gitflow.v1.schema.json"))
        for scale in roadmap_schema["properties"]["time_scale"]["enum"]:
            self.assertIn(f"`{scale}`", roadmap)
        for field in ("start_order", "end_order", "schema_version", "roadmap.v1.schema.json"):
            self.assertIn(field, roadmap)
        for field in ("schema_version", "gitflow.v1.schema.json", "branch use before creation", "original_index"):
            self.assertIn(field, gitflow)
        self.assertEqual(gitflow_schema["properties"]["schema_version"]["const"], 1)

    def test_readme_declares_exact_dependency_ranges_and_commands(self):
        readme = self.read("README.md")
        requirements = self.read("requirements.txt")
        for requirement in ("PyYAML>=6.0,<7", "jsonschema>=4.18,<5"):
            self.assertIn(requirement, requirements)
            self.assertIn(f"`{requirement}`", readme)
        for command in ("/drawio:create", "/drawio:improve", "/drawio:resume", "/drawio:trace"):
            self.assertIn(command, readme)
        self.assertIn("--profile roadmap --source", readme)
        self.assertIn("--profile gitflow --source", readme)

    def test_corporate_main_host_contract_is_explicit_and_fail_closed(self):
        skill = self.read("SKILL.md")
        routing = self.read("references/model-routing.md")
        workflow = self.read("references/diagram-supervisor.md")
        for text in (
            "main interactive session only",
            "presents the structured result",
            "host-preflight",
            "host-preflight.json",
            "run-manifest.jsonl",
        ):
            self.assertIn(text, skill)
        self.assertIn("The lifecycle command host invokes Supervisor itself in an isolated process", routing)
        self.assertIn("A successful native `agent` tool status does not provide that proof.", routing)
        self.assertIn("interactive session only invokes the command and presents its result.", workflow)
        self.assertIn("Stop before analysis if preflight fails", workflow)

    def test_review_slash_command_is_the_normal_corporate_entrypoint(self):
        skill = self.read("SKILL.md")
        readme = self.read("README.md")
        workflow = self.read("references/diagram-supervisor.md")
        for text in (skill, readme, workflow):
            self.assertIn("/drawio:review", text)
        self.assertIn("commands/drawio/review.md", skill)
        self.assertIn("scripts/diagram_host.py", skill)


if __name__ == "__main__":
    unittest.main()
