import unittest
from fractions import Fraction

from pmwp_baseline.parsing import arithmetic, parse_response


class ArithmeticTests(unittest.TestCase):
    def test_exact_arithmetic(self):
        for expression, expected in [
            ("(300 - 3 * 76.0) / 2", 36),
            ("-2*12.36+28.97", 4.25),
            ("۱۰۰ × ۵۱۴۵ ÷ (۲۵ + ۱۰۰)", 4116),
            ("0.1 + 0.2", Fraction(3, 10)),
            ("1e-3", Fraction(1, 1000)),
            ("+8", 8),
        ]:
            with self.subTest(expression=expression):
                self.assertEqual(arithmetic(expression), expected)

    def test_reject_unsafe_or_unbounded(self):
        expressions = [
            "__import__('os').system('id')",
            "x+1",
            "2**1000000",
            "1/0",
            "True",
            "1//2",
            "1%2",
            "(1).__class__",
            "[1]",
            "1e9999999",
            "",
            "1+" * 300,
            "(" * 300 + "1" + ")" * 300,
        ]
        for expression in expressions:
            with self.subTest(expression=expression):
                with self.assertRaises(ValueError):
                    arithmetic(expression)

    def test_modes_and_final_line(self):
        self.assertEqual(parse_response("Reasoning\nx = ۸۰", "numeric").value, 80)
        self.assertFalse(parse_response("x = 10*8", "numeric").valid)
        parsed = parse_response("Reasoning\nx = 8 * 10", "equation")
        self.assertTrue(parsed.valid)
        self.assertTrue(parsed.unsimplified)
        self.assertFalse(parse_response("x = 80", "equation").unsimplified)
        for text in ("80", "x = 80\nThanks", "x = nan", "x = 1 / 0", "x = 80\n```"):
            self.assertFalse(parse_response(text, "numeric").valid)
        self.assertTrue(parse_response("x = 80\n", "numeric").valid)
        self.assertFalse(parse_response("x = 80\n", "numeric").format_compliant)
        with self.assertRaises(ValueError):
            parse_response("x = 1", "unknown")
