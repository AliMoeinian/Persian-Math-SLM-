"""Bounded arithmetic evaluation. No eval, calls, names, powers, or attributes."""

import ast
import math
import operator
import re
from dataclasses import dataclass
from fractions import Fraction

_TRANSLATION = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩٫−×÷", "01234567890123456789.-*/")
_BINARY = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}


def normalize(text: str) -> str:
    return text.translate(_TRANSLATION)


def arithmetic(expression: str) -> Fraction:
    expression = normalize(expression).strip()
    if not expression or len(expression) > 512:
        raise ValueError("Empty or oversized expression")
    if not re.fullmatch(r"[0-9eE.\s+*/()\-]+", expression):
        raise ValueError("Unsupported arithmetic characters")
    try:
        root = ast.parse(expression, mode="eval")
        if len(list(ast.walk(root))) > 128:
            raise ValueError("Expression too complex")

        def visit(node: ast.AST) -> Fraction:
            if isinstance(node, ast.Constant) and type(node.value) in (int, float):
                token = ast.get_source_segment(expression, node)
                if token is None or len(token) > 64:
                    raise ValueError("Oversized literal")
                if not math.isfinite(node.value):
                    raise ValueError("Nonfinite number")
                # Bound decimal exponents before Fraction allocation.
                if "e" in token.lower() and abs(int(token.lower().split("e")[1])) > 100:
                    raise ValueError("Exponent out of range")
                result = Fraction(token)
            elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
                result = visit(node.operand) * (-1 if isinstance(node.op, ast.USub) else 1)
            elif isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
                result = _BINARY[type(node.op)](visit(node.left), visit(node.right))
            else:
                raise ValueError("Unsupported arithmetic syntax")
            if max(result.numerator.bit_length(), result.denominator.bit_length()) > 4096:
                raise ValueError("Arithmetic magnitude limit exceeded")
            return result

        return visit(root.body)
    except (SyntaxError, ZeroDivisionError, OverflowError, RecursionError) as exc:
        raise ValueError("Invalid arithmetic expression") from exc


@dataclass(frozen=True)
class Parsed:
    expression: str | None
    value: float | None
    valid: bool
    format_compliant: bool
    unsimplified: bool | None
    error: str | None


def parse_response(content: str, mode: str) -> Parsed:
    if mode not in {"numeric", "equation"}:
        raise ValueError("Unsupported output mode")
    lines = content.strip().splitlines()
    final = lines[-1].strip() if lines else ""
    match = re.fullmatch(r"x\s*=\s*(.+)", final)
    if not match:
        return Parsed(None, None, False, False, None, "missing_final_line")
    expression = normalize(match[1]).strip()
    compliant = content.endswith(final) and final.startswith("x = ")
    try:
        numeric_literal = (
            re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", expression) is not None
        )
        if mode == "numeric" and not numeric_literal:
            raise ValueError("Numeric mode requires a number")
        value = float(arithmetic(expression))
        if not math.isfinite(value):
            raise ValueError("Nonfinite value")
        # A syntax-based diagnostic, not proof of faithful symbolic reasoning.
        unsimplified = not numeric_literal if mode == "equation" else None
        return Parsed(expression, value, True, compliant, unsimplified, None)
    except ValueError as exc:
        return Parsed(expression, None, False, compliant, None, str(exc))
