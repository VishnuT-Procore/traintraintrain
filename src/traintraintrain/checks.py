"""`expect` rules: small, safe comparisons on the metrics a step returns.

    expect: ["error_rate < 1.0", "pending_migrations == 0", "status == 'ready'", "healthy == true"]

Each rule is `<metric> <op> <value>` with op one of < <= > >= == !=. The value is a number,
true/false/null, or a quoted string. Rules are parsed, never evaluated as code.
"""
from __future__ import annotations

import operator
import re
from dataclasses import dataclass

OPS = {"<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge,
       "==": operator.eq, "!=": operator.ne}
_RULE = re.compile(r"^\s*([A-Za-z_][\w.\-]*)\s*(<=|>=|==|!=|<|>)\s*(.+?)\s*$")


@dataclass(frozen=True)
class Rule:
    metric: str
    op: str
    value: object
    text: str


@dataclass
class CheckResult:
    rule: str
    metric: str
    actual: object
    ok: bool
    why: str = ""

    def as_dict(self) -> dict:
        return {"rule": self.rule, "metric": self.metric, "actual": self.actual, "ok": self.ok, "why": self.why}


def _literal(raw: str):
    low = raw.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "none"):
        return None
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
        return raw[1:-1]
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"{raw!r} isn't a number, true/false/null, or a quoted string") from None


def parse_rule(text: str) -> Rule:
    if not isinstance(text, str):
        raise ValueError(f"expect rules are strings like \"error_rate < 1\", not {text!r}")
    m = _RULE.match(text)
    if not m:
        raise ValueError(f"can't read the rule {text!r}; write it as <metric> <op> <value>, e.g. error_rate < 1")
    try:
        value = _literal(m.group(3))
    except ValueError as e:
        raise ValueError(f"rule {text!r}: {e}") from None
    op = m.group(2)
    if op not in ("==", "!=") and (isinstance(value, bool) or not isinstance(value, (int, float))):
        raise ValueError(f"rule {text!r}: {op} needs a number")
    return Rule(m.group(1), m.group(2), value, text.strip())


def parse_rules(rules) -> list[Rule]:
    if rules is None:
        return []
    if isinstance(rules, str):
        rules = [rules]
    if not isinstance(rules, list):
        raise ValueError("expect must be a list of rules")
    return [parse_rule(r) for r in rules]


def _as_number(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return None
    return None


def check(rule: Rule, metrics: dict) -> CheckResult:
    if rule.metric not in metrics:
        known = ", ".join(sorted(metrics)) or "none"
        return CheckResult(rule.text, rule.metric, None, False,
                           f"no metric {rule.metric!r} in the result (got: {known})")
    actual = metrics[rule.metric]
    expected = rule.value
    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        number = _as_number(actual)
        if number is None:
            return CheckResult(rule.text, rule.metric, actual, False, f"{rule.metric} is {actual!r}, not a number")
        ok = OPS[rule.op](number, expected)
    else:
        ok = OPS[rule.op](actual, expected)
    why = "" if ok else f"{rule.metric} is {actual!r}"
    return CheckResult(rule.text, rule.metric, actual, ok, why)


def evaluate(rules: list[Rule], metrics: dict) -> list[CheckResult]:
    return [check(r, metrics) for r in rules]


def stop_reason(rules: list[Rule], metrics: dict) -> str | None:
    """A step's `stop_if` rules: why it should stop now (the first rule that matches), or None.

    A missing metric never matches.
    """
    for r in rules:
        if r.metric in metrics and check(r, metrics).ok:
            return f"{r.metric} is {metrics[r.metric]!r} (stop_if: {r.text})"
    return None
