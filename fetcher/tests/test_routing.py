"""Pure routing tests. Stdlib only:  python -m unittest discover -s tests"""
import unittest
from datetime import date

from app.routing import Source, expand_range, group_by_month, route

TODAY = date(2026, 10, 3)


class RoutingTest(unittest.TestCase):
    def test_current_month_uses_permalink(self):
        self.assertIs(route(date(2026, 10, 1), TODAY), Source.PERMALINK)
        self.assertIs(route(date(2026, 10, 3), TODAY), Source.PERMALINK)

    def test_earlier_months_use_archive(self):
        self.assertIs(route(date(2026, 9, 30), TODAY), Source.ARCHIVE)
        self.assertIs(route(date(2025, 10, 15), TODAY), Source.ARCHIVE)  # same month, earlier year

    def test_year_boundary(self):
        self.assertIs(route(date(2026, 12, 31), date(2027, 1, 2)), Source.ARCHIVE)

    def test_first_of_month_routes_yesterday_to_permalink(self):
        # archive for October is not published yet on 1 November
        self.assertIs(route(date(2026, 10, 31), date(2026, 11, 1)), Source.PERMALINK)
        self.assertIs(route(date(2026, 12, 31), date(2027, 1, 1)), Source.PERMALINK)   # year boundary
        self.assertIs(route(date(2027, 2, 28), date(2027, 3, 1)), Source.PERMALINK)    # short month
        self.assertIs(route(date(2028, 2, 29), date(2028, 3, 1)), Source.PERMALINK)    # leap year

    def test_first_of_month_rule_is_narrow(self):
        # only yesterday, only on the 1st
        self.assertIs(route(date(2026, 10, 30), date(2026, 11, 1)), Source.ARCHIVE)
        self.assertIs(route(date(2026, 9, 30), date(2026, 11, 1)), Source.ARCHIVE)
        self.assertIs(route(date(2026, 10, 31), date(2026, 11, 2)), Source.ARCHIVE)
        self.assertIs(route(date(2026, 11, 1), date(2026, 11, 1)), Source.PERMALINK)  # today itself

    def test_future_month_rejected(self):
        with self.assertRaises(ValueError):
            route(date(2026, 11, 1), TODAY)

    def test_range_across_month_boundary_uses_both(self):
        days = expand_range(date(2026, 9, 29), date(2026, 10, 2))
        self.assertEqual([route(d, TODAY) for d in days],
                         [Source.ARCHIVE] * 2 + [Source.PERMALINK] * 2)

    def test_expand_range_inclusive_and_validated(self):
        self.assertEqual(len(expand_range(date(2024, 2, 1), date(2024, 2, 29))), 29)
        with self.assertRaises(ValueError):
            expand_range(date(2026, 10, 2), date(2026, 10, 1))

    def test_group_by_month(self):
        g = group_by_month([date(2026, 9, 2), date(2026, 8, 31), date(2026, 9, 1)])
        self.assertEqual(list(g), [(2026, 8), (2026, 9)])
        self.assertEqual(g[(2026, 9)], [date(2026, 9, 1), date(2026, 9, 2)])


if __name__ == "__main__":
    unittest.main()
