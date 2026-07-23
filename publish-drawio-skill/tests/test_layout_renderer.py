import copy
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from lifecycle_contracts import canonical_json_sha256


SHA = "a" * 64


def semantic_plan():
    return {
        "schema_version": 2,
        "role": "semantic_analyst",
        "status": "ok",
        "run_id": "run-render",
        "source_bundle_sha256": SHA,
        "baseline_semantic_digest": SHA,
        "result": {
            "mode": "create",
            "diagram_type": "flowchart",
            "title": "Rendered",
            "direction": "LR",
            "pages": [
                {
                    "page_id": "page-b",
                    "name": "Second",
                    "nodes": [
                        {
                            "stable_identity": {"page_id": "page-b", "cell_id": "same"},
                            "label": "Other page",
                            "semantic_type": "process",
                            "parent": None,
                            "style_hint": None,
                        }
                    ],
                    "edges": [],
                },
                {
                    "page_id": "page-a",
                    "name": "First",
                    "nodes": [
                        {
                            "stable_identity": {"page_id": "page-a", "cell_id": "child"},
                            "label": "Child",
                            "semantic_type": "process",
                            "parent": {"page_id": "page-a", "cell_id": "group"},
                            "style_hint": "fillColor=#ffffff;",
                        },
                        {
                            "stable_identity": {"page_id": "page-a", "cell_id": "group"},
                            "label": "Group",
                            "semantic_type": "container",
                            "parent": None,
                            "style_hint": None,
                        },
                        {
                            "stable_identity": {"page_id": "page-a", "cell_id": "target"},
                            "label": "Target",
                            "semantic_type": "decision",
                            "parent": None,
                            "style_hint": None,
                        },
                    ],
                    "edges": [
                        {
                            "stable_identity": {"page_id": "page-a", "cell_id": "edge"},
                            "source": {"page_id": "page-a", "cell_id": "child"},
                            "target": {"page_id": "page-a", "cell_id": "target"},
                            "label": "Continue",
                            "relationship": "branch",
                            "parent": None,
                            "style_hint": None,
                        }
                    ],
                },
            ],
            "semantic_delta": {
                "schema_version": 2,
                "baseline_semantic_digest": SHA,
                "source_bundle_sha256": SHA,
                "operations": [],
            },
            "assumptions": [],
            "requires_human": False,
            "human_questions": [],
        },
    }


def layout_result(plan=None):
    plan = plan or semantic_plan()
    return {
        "schema_version": 1,
        "result_id": "layout-render",
        "request_sha256": canonical_json_sha256({"request": "render"}),
        "backend": "python-layered",
        "pages": [
            {
                "page_id": "page-b",
                "name": "Second",
                "nodes": [
                    {
                        "node_id": "same",
                        "x": 15,
                        "y": 25,
                        "width": 120,
                        "height": 60,
                        "locked": False,
                    }
                ],
                "edges": [],
                "channel_reservations": [],
            },
            {
                "page_id": "page-a",
                "name": "First",
                "nodes": [
                    {
                        "node_id": "child",
                        "x": 140,
                        "y": 160,
                        "width": 160,
                        "height": 70,
                        "locked": False,
                    },
                    {
                        "node_id": "group",
                        "x": 100,
                        "y": 100,
                        "width": 240,
                        "height": 180,
                        "locked": False,
                    },
                    {
                        "node_id": "target",
                        "x": 500,
                        "y": 150,
                        "width": 140,
                        "height": 90,
                        "locked": False,
                    },
                ],
                "edges": [
                    {
                        "edge_id": "edge",
                        "source": "child",
                        "target": "target",
                        "edge_class": "branch",
                        "route_group": "explicit-branch-trunk",
                        "source_port": "east",
                        "target_port": "west",
                        "source_pin": 0.25,
                        "target_pin": 0.75,
                        "waypoints": [
                            {"x": 300, "y": 177.5},
                            {"x": 300, "y": 177.5},
                            {"x": 380, "y": 177.5},
                            {"x": 440, "y": 177.5},
                            {"x": 440, "y": 217.5},
                            {"x": 500, "y": 217.5},
                        ],
                        "label_bounds": {"x": 390, "y": 185, "width": 70, "height": 20},
                    }
                ],
                "channel_reservations": [],
            },
        ],
        "metrics": {
            "crossings": 0,
            "overlaps": 0,
            "route_length": 240,
            "bend_count": 2,
            "shared_route_length": 0,
            "label_collisions": 0,
        },
    }


def cell_by_id(path, page_id, cell_id):
    root = ET.parse(path).getroot()
    diagram = next(item for item in root.findall("diagram") if item.get("id") == page_id)
    return next(item for item in diagram.findall(".//mxCell") if item.get("id") == cell_id)


class LayoutRendererTests(unittest.TestCase):
    def test_exact_geometry_pins_canonical_points_and_label_bounds_are_serialized(self):
        from layout_renderer import render_layout

        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "rendered.drawio"
            render_layout(semantic_plan(), layout_result(), output)
            child = cell_by_id(output, "page-a", "child")
            child_geometry = child.find("mxGeometry")
            self.assertEqual(
                {key: child_geometry.get(key) for key in ("x", "y", "width", "height")},
                {"x": "40", "y": "60", "width": "160", "height": "70"},
            )
            self.assertEqual(child.get("parent"), "group")
            self.assertEqual(
                {key: child.get(f"data-layout-{key}") for key in ("x", "y", "width", "height")},
                {"x": "140", "y": "160", "width": "160", "height": "70"},
            )

            edge = cell_by_id(output, "page-a", "edge")
            self.assertIn("exitX=1;exitY=0.25;", edge.get("style"))
            self.assertIn("entryX=0;entryY=0.75;", edge.get("style"))
            self.assertEqual(edge.get("data-edge-class"), "branch")
            self.assertEqual(edge.get("data-route-group"), "explicit-branch-trunk")
            geometry = edge.find("mxGeometry")
            points = [
                (point.get("x"), point.get("y"))
                for point in geometry.findall("Array[@as='points']/mxPoint")
            ]
            self.assertEqual(
                points,
                [
                    ("300", "177.5"),
                    ("440", "177.5"),
                    ("440", "217.5"),
                    ("500", "217.5"),
                ],
            )
            self.assertEqual(
                {key: edge.get(f"data-label-{key}") for key in ("x", "y", "width", "height")},
                {"x": "390", "y": "185", "width": "70", "height": "20"},
            )
            self.assertEqual(
                {
                    key: geometry.get(key)
                    for key in ("x", "y", "relative", "width", "height")
                },
                {
                    "x": "0.3125",
                    "y": "15",
                    "relative": "1",
                    "width": "70",
                    "height": "20",
                },
            )
            offset = geometry.find("mxPoint[@as='offset']")
            self.assertEqual((offset.get("x"), offset.get("y")), ("0", "0"))

            import diagram_supervisor

            specification = diagram_supervisor.make_spec(output)
            edge_spec = next(
                item
                for page in specification["pages"]
                for item in page["cells"]
                if page["id"] == "page-a" and item["id"] == "edge"
            )
            self.assertEqual(
                edge_spec["geometry"]["label_offset"],
                {"x": 0.3125, "y": 15.0, "offset": {"x": 0.0, "y": 0.0}},
            )
            self.assertEqual(
                edge_spec["geometry"]["waypoints"],
                [
                    {"x": 300.0, "y": 177.5},
                    {"x": 440.0, "y": 177.5},
                    {"x": 440.0, "y": 217.5},
                    {"x": 500.0, "y": 217.5},
                ],
            )

    def test_identical_inputs_produce_byte_identical_outputs(self):
        from layout_renderer import render_layout

        with tempfile.TemporaryDirectory() as temp:
            first = Path(temp) / "first.drawio"
            second = Path(temp) / "second.drawio"
            render_layout(semantic_plan(), layout_result(), first)
            render_layout(copy.deepcopy(semantic_plan()), copy.deepcopy(layout_result()), second)
            self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_label_projection_uses_stable_first_segment_tie_break(self):
        from layout_renderer import render_layout

        result = layout_result()
        result["pages"][1]["edges"][0]["label_bounds"] = {
            "x": 425,
            "y": 182.5,
            "width": 10,
            "height": 10,
        }
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "tie.drawio"
            render_layout(semantic_plan(), result, output)
            geometry = cell_by_id(output, "page-a", "edge").find("mxGeometry")
            # The center is equally distant from the horizontal and vertical
            # bend segments. Stable index order selects the horizontal one.
            self.assertAlmostEqual(float(geometry.get("x")), 1.0 / 12.0)
            self.assertEqual(geometry.get("y"), "10")
            offset = geometry.find("mxPoint[@as='offset']")
            self.assertEqual((offset.get("x"), offset.get("y")), ("0", "0"))

    def test_route_group_is_not_inferred_when_contract_omits_it(self):
        from layout_renderer import render_layout

        result = layout_result()
        del result["pages"][1]["edges"][0]["route_group"]
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "ungrouped.drawio"
            render_layout(semantic_plan(), result, output)
            edge = cell_by_id(output, "page-a", "edge")
            self.assertEqual(edge.get("data-edge-class"), "branch")
            self.assertNotIn("data-route-group", edge.attrib)

    def test_diagonal_result_is_refused_without_partial_output(self):
        from layout_renderer import render_layout

        result = layout_result()
        result["pages"][1]["edges"][0]["waypoints"][2] = {"x": 420, "y": 190}
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "invalid.drawio"
            with self.assertRaises(Exception):
                render_layout(semantic_plan(), result, output)
            self.assertFalse(output.exists())

    def test_zero_length_route_is_refused_without_partial_output(self):
        from layout_renderer import LayoutRenderError, render_layout

        plan = semantic_plan()
        edge = plan["result"]["pages"][1]["edges"][0]
        edge["target"] = {"page_id": "page-a", "cell_id": "child"}
        result = layout_result(plan)
        route = result["pages"][1]["edges"][0]
        route.update(
            {
                "target": "child",
                "edge_class": "self_loop",
                "target_port": "east",
                "target_pin": 0.25,
                "waypoints": [
                    {"x": 300, "y": 177.5},
                    {"x": 300, "y": 177.5},
                ],
            }
        )
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "zero.drawio"
            with self.assertRaisesRegex(LayoutRenderError, "distinct waypoints"):
                render_layout(plan, result, output)
            self.assertFalse(output.exists())

    def test_missing_extra_duplicate_and_cross_page_bindings_are_refused(self):
        from layout_renderer import LayoutRenderError, render_layout

        mutations = []
        missing = layout_result()
        missing["pages"][1]["nodes"].pop()
        mutations.append(missing)
        extra = layout_result()
        extra["pages"][1]["nodes"].append(
            {"node_id": "extra", "x": 0, "y": 0, "width": 10, "height": 10, "locked": False}
        )
        mutations.append(extra)
        duplicate = layout_result()
        duplicate["pages"][1]["nodes"].append(copy.deepcopy(duplicate["pages"][1]["nodes"][0]))
        mutations.append(duplicate)
        cross_page = layout_result()
        cross_page["pages"][1]["edges"][0]["target"] = "same"
        mutations.append(cross_page)
        for result in mutations:
            with self.subTest(result=result):
                with tempfile.TemporaryDirectory() as temp:
                    with self.assertRaises(LayoutRenderError):
                        render_layout(semantic_plan(), result, Path(temp) / "bad.drawio")

    def test_existing_output_is_not_clobbered(self):
        from layout_renderer import LayoutRenderError, render_layout

        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "existing.drawio"
            output.write_bytes(b"keep")
            with self.assertRaises(LayoutRenderError):
                render_layout(semantic_plan(), layout_result(), output)
            self.assertEqual(output.read_bytes(), b"keep")


if __name__ == "__main__":
    unittest.main()
