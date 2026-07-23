import os
import sys
import unittest


SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)

import layout_geometry


class LayoutGeometryTests(unittest.TestCase):
    def test_collinear_overlap_returns_shared_length(self):
        self.assertEqual(
            layout_geometry.collinear_overlap(
                ((0, 10), (100, 10)),
                ((40, 10), (140, 10)),
            ),
            60.0,
        )

    def test_endpoint_touch_is_not_shared_segment(self):
        self.assertEqual(
            layout_geometry.collinear_overlap(
                ((0, 0), (20, 0)),
                ((20, 0), (40, 0)),
            ),
            0.0,
        )

    def test_shared_route_length_counts_partial_collinear_segments(self):
        self.assertEqual(
            layout_geometry.shared_route_length(
                [(0, 0), (100, 0), (100, 50)],
                [(40, 0), (140, 0)],
            ),
            60.0,
        )

    def test_shared_route_length_does_not_double_count_retraced_overlap(self):
        self.assertEqual(
            layout_geometry.shared_route_length(
                [(0, 0), (100, 0), (0, 0)],
                [(20, 0), (80, 0)],
            ),
            60.0,
        )

    def test_canonical_segment_has_stable_grid_order(self):
        self.assertEqual(
            layout_geometry.canonical_segment((10.0, 5.0), (0.0, 5.0)),
            ((0.0, 5.0), (10.0, 5.0)),
        )

    def test_segment_hits_rect_and_clearance(self):
        self.assertTrue(layout_geometry.segment_hits_rect(((0, 5), (20, 5)), (8, 0, 4, 10)))
        self.assertFalse(layout_geometry.segment_hits_rect(((0, 5), (7, 5)), (8, 0, 4, 10)))
        self.assertTrue(layout_geometry.segment_hits_rect(((0, 5), (7, 5)), (8, 0, 4, 10), clearance=1.0))

    def test_rects_overlap_detects_label_collision(self):
        self.assertTrue(layout_geometry.rects_overlap((0, 0, 30, 20), (25, 15, 30, 20)))
        self.assertFalse(layout_geometry.rects_overlap((0, 0, 30, 20), (30, 0, 30, 20)))

    def test_route_metrics_count_bends_and_detour(self):
        points = [(0, 0), (0, 30), (50, 30), (50, 0)]
        self.assertEqual(layout_geometry.bend_count(points), 2)
        self.assertEqual(layout_geometry.manhattan_length(points), 110.0)
        self.assertEqual(layout_geometry.detour_ratio(points), 2.2)
        self.assertTrue(layout_geometry.is_manhattan(points))


if __name__ == "__main__":
    unittest.main()
