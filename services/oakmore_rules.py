"""The Oakmore decision-rules solver, generic part, taken verbatim from the production
engine (OakmoreLabsProcessor decision_rules_engine.py). The reconciliation tool runs its
cause attribution through this so the decision of WHICH cause applies, and its amount, is
a rules table a person can read and edit, not code.

solve(alias, record) loads services/cause_rules.json and returns every matching row.
"""
import json
import math
import os
import re

_RULES_PATH = os.environ.get(
    "CAUSE_RULES_PATH",
    os.path.join(os.path.dirname(__file__), "cause_rules.json"),
)
_RULES: dict = {}


def _get_rules():
    global _RULES
    if not _RULES:
        with open(_RULES_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        _RULES = {r["ruleAlias"]: r for r in data["export"]["data"]["rules"]}
        _RULES["__details__"] = data["export"]["data"].get("details", {})
        _RULES["__actions__"] = data["export"]["data"].get("actions", {})
    return _RULES


def details():
    """The detail templates that ship next to the rules, keyed by detail id."""
    return _get_rules().get("__details__", {})


def actions():
    """The action each rule prescribes, keyed by the same id as its detail."""
    return _get_rules().get("__actions__", {})


# ---------------------------------------------------------------------------

def _get_nested(obj, path: str, default=None):
    """Return obj['a']['b']['c'] for path='a.b.c'."""
    parts = path.split(".")
    cur = obj
    for p in parts:
        if isinstance(cur, dict):
            cur = cur.get(p, default)
        else:
            return default
    return cur


def _set_nested(obj: dict, path: str, value):
    """Set obj['a']['b']['c'] = value for path='a.b.c', creating dicts as needed."""
    parts = path.split(".")
    cur = obj
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = value


# ---------------------------------------------------------------------------
# Function-expression evaluator
# ---------------------------------------------------------------------------

def _eval_expr(expr, row_ctx: dict):
    """
    Evaluate a DecisionRules outputScalarValue expression.
    """
    if expr is None:
        return 0

    # plain number
    if isinstance(expr, (int, float)):
        return float(expr)

    # plain string – may be a reference, arithmetic expression, or literal
    if isinstance(expr, str):
        expr = expr.strip()
        # FIX: Only treat as a single reference if there is exactly one {…}
        # pair. Strings like "{a} - {b}" have multiple ref blocks and must
        # be evaluated as arithmetic formulas, not looked up as a single key.
        if expr.startswith("{") and expr.endswith("}") and expr.count("{") == 1:
            ref = expr[1:-1]
            val = _get_nested(row_ctx, ref)
            if val is None:
                val = row_ctx.get(ref, 0)
            try:
                return float(val)
            except (TypeError, ValueError):
                return val
        # try to parse as float
        try:
            return float(expr)
        except ValueError:
            pass
        # If it contains {refs}, evaluate as an arithmetic expression string
        if "{" in expr:
            return _eval_formula_string(expr, row_ctx)
        return expr  # string literal

    # function AST dict
    if isinstance(expr, dict) and "functionName" in expr:
        fn = expr["functionName"]
        params = expr.get("parameters", [])
        args = [_eval_expr(p, row_ctx) for p in params]
        return _call_fn(fn, args, row_ctx)

    # fallback
    return expr


def _eval_formula_string(expr: str, row_ctx: dict):
    """
    Evaluate a simple arithmetic formula string that contains {ref} placeholders.
    """
    def replacer(m):
        ref = m.group(1)
        val = _get_nested(row_ctx, ref)
        if val is None:
            val = row_ctx.get(ref, 0)
        try:
            return str(float(val))
        except (TypeError, ValueError):
            return "0"

    # Replace all {ref} with their numeric values
    expr_eval = re.sub(r"\{([^}]+)\}", replacer, expr)
    # Remove stray whitespace / newlines
    expr_eval = expr_eval.strip().replace("\r", "").replace("\n", "")
    # Balance parens (some formulas have missing closing parens in the rule JSON)
    open_p = expr_eval.count("(")
    close_p = expr_eval.count(")")
    if open_p > close_p:
        expr_eval += ")" * (open_p - close_p)
    # Safety: only allow numeric chars, operators, parens, spaces, dots
    if not re.match(r"^[\d\s\+\-\*\/\(\)\.eE]+$", expr_eval):
        return 0
    try:
        return float(eval(expr_eval, {"__builtins__": {}}))  # noqa: S307
    except Exception:
        return 0


def _call_fn(fn: str, args: list, ctx: dict):
    fn = fn.upper()

    def n(i): return float(args[i]) if i < len(args) else 0
    def a(i): return args[i] if i < len(args) else 0

    if fn in ("PLUS_CHAR", "SUM"):
        return sum(float(x) for x in args)
    if fn == "MINUS_CHAR":
        return n(0) - n(1)
    if fn == "TIMES_CHAR":
        return n(0) * n(1)
    if fn == "DIVIDED_CHAR":
        denom = n(1)
        return n(0) / denom if denom != 0 else 0
    if fn == "ROUND":
        decimals = int(n(1)) if len(args) > 1 else 2
        return round(n(0), decimals)
    if fn == "MAX":
        return max(n(i) for i in range(len(args)))
    if fn == "MIN":
        return min(n(i) for i in range(len(args)))
    if fn == "ABS":
        return abs(n(0))
    if fn == "FLOOR":
        return math.floor(n(0))
    if fn == "CEILING":
        return math.ceil(n(0))
    if fn == "BTW":
        # Between: BTW(value, lo, hi) → True if lo <= value <= hi
        return n(1) <= n(0) <= n(2)

    # Comparison / logical
    if fn in ("IF",):
        cond = a(0)
        if isinstance(cond, bool):
            return a(1) if cond else a(2)
        return a(1) if bool(cond) else a(2)
    if fn in ("EQUAL", "EQ"):
        return args[0] == args[1]
    if fn in ("NOT_EQUAL", "NEQ"):
        return args[0] != args[1]
    if fn in ("GREATER", "GT"):
        return n(0) > n(1)
    if fn in ("GREATER_EQUAL", "GTE"):
        return n(0) >= n(1)
    if fn in ("LESS", "LT"):
        return n(0) < n(1)
    if fn in ("LESS_EQUAL", "LTE"):
        return n(0) <= n(1)
    if fn in ("AND",):
        return all(bool(a(i)) for i in range(len(args)))
    if fn in ("OR",):
        return any(bool(a(i)) for i in range(len(args)))
    if fn in ("NOT",):
        return not bool(a(0))

    raise ValueError(f"Unknown function: {fn}")


# ---------------------------------------------------------------------------
# Condition checker  (FIX: added IN, function conditions)
# ---------------------------------------------------------------------------

def _check_condition(cond: dict, value, row_ctx: dict = None) -> bool:
    op = cond.get("operator", "anything")
    cval = cond.get("value")

    if op == "anything":
        return True

    # FIX: Handle function-type conditions (used in MD-exemption, OR-exemption, SC-exemption)
    if op == "function" or cond.get("type") == "function":
        if isinstance(cval, dict) and "functionName" in cval:
            ctx = row_ctx if row_ctx else {}
            result = _eval_expr(cval, ctx)
            return bool(result)
        return False

    # normalise both sides to comparable types
    def _num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    if op in ("=", "=="):
        if isinstance(cval, list):
            return str(value).strip().upper() in [str(v).strip().upper() for v in cval]
        return str(value).strip().upper() == str(cval).strip().upper()
    if op == "!=":
        return str(value).strip().upper() != str(cval).strip().upper()

    # FIX: Handle IN operator (used widely across CT, NY, KS, AL, fed, etc.)
    if op == "IN":
        vals = cval if isinstance(cval, list) else [cval]
        return str(value).strip().upper() in [str(v).strip().upper() for v in vals]

    if op == "ELSE":
        return True

    if op == "NOT IN":
        vals = cval if isinstance(cval, list) else [cval]
        return str(value).strip().upper() not in [str(v).strip().upper() for v in vals]

    vnum = _num(value)
    if op == ">" and vnum is not None:
        return vnum > float(cval)
    if op == ">=" and vnum is not None:
        return vnum >= float(cval)
    if op == "<" and vnum is not None:
        return vnum < float(cval)
    if op == "<=" and vnum is not None:
        return vnum <= float(cval)
    if op == "between" and vnum is not None and isinstance(cval, list):
        lo, hi = float(cval[0]), float(cval[1])
        return lo <= vnum <= hi

    return False


# ---------------------------------------------------------------------------
# Decision table solver  (FIX: inject userVariables, pass ctx to conditions)
# ---------------------------------------------------------------------------

def _solve_decision_table(rule: dict, inputs, strategy="FIRST_MATCH"):
    """
    inputs: a single dict OR a list of dicts.
    Returns: list of lists (outer = per record, inner = matched rows).
    """
    if isinstance(inputs, dict):
        inputs = [inputs]

    dt = rule.get("decisionTable", {})
    columns = dt.get("columns", [])
    rows = dt.get("rows", [])

    # FIX: Extract userVariables and make them available in the row context
    user_vars = {}
    for uv in rule.get("userVariables", []):
        if isinstance(uv, dict) and uv.get("name"):
            val = uv.get("value")
            try:
                val = float(val)
            except (TypeError, ValueError):
                pass
            user_vars[uv["name"]] = val

    # Build column maps
    input_cols = {}   # colId -> inputVariable path
    output_cols = {}  # colId -> outputVariable path (in order)
    output_order = []
    for col in columns:
        cid = col["columnId"]
        if col["type"] == "input":
            input_cols[cid] = col.get("condition", {}).get("inputVariable", "")
        else:
            out_var = col.get("columnOutput", {}).get("outputVariable", "")
            output_cols[cid] = out_var
            output_order.append(cid)

    results = []
    for record in inputs:
        # flatten record for easy lookup
        flat = _flatten(record)
        # FIX: Merge userVariables into the flat context
        flat_with_uv = {**user_vars, **flat}
        matched_rows = []

        for row in rows:
            if not row.get("active", True):
                continue

            cells = {c["column"]: c for c in row["cells"]}

            # Check all input conditions
            match = True
            for cid, var_path in input_cols.items():
                cell = cells.get(cid, {})
                cond = cell.get("scalarCondition", {"operator": "anything"})
                val = flat_with_uv.get(var_path, _get_nested(record, var_path))
                # FIX: pass row_ctx for function conditions
                if not _check_condition(cond, val, flat_with_uv):
                    match = False
                    break

            if match:
                # Compute outputs left-to-right so later outputs can reference earlier ones
                out_record = {}
                row_ctx = {**flat_with_uv, **out_record}  # start with flattened input + userVars

                for cid in output_order:
                    cell = cells.get(cid, {})
                    out_val_def = cell.get("outputScalarValue", {})
                    val_type = out_val_def.get("type", "common")
                    raw_val = out_val_def.get("value")

                    if val_type == "function":
                        computed = _eval_expr(raw_val, row_ctx)
                    elif val_type == "common":
                        if isinstance(raw_val, str) and "{" in raw_val:
                            computed = _eval_expr(raw_val, row_ctx)
                        elif isinstance(raw_val, str):
                            try:
                                computed = float(raw_val)
                            except ValueError:
                                computed = raw_val
                        elif raw_val is not None:
                            try:
                                computed = float(raw_val)
                            except (TypeError, ValueError):
                                computed = raw_val
                        else:
                            computed = None
                    else:
                        computed = _eval_expr(raw_val, row_ctx)

                    out_var = output_cols[cid]
                    _set_nested(out_record, out_var, computed)
                    # also make available as flat key for next outputs in same row
                    row_ctx[out_var] = computed
                    row_ctx.update(_flatten(out_record))

                matched_rows.append(out_record)
                if strategy == "FIRST_MATCH":
                    break

        results.append(matched_rows)

    return results


def _flatten(d: dict, prefix="") -> dict:
    """Flatten nested dict to dotted keys."""
    out = {}
    for k, v in d.items():
        full_key = f"{prefix}{k}" if not prefix else f"{prefix}.{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, full_key))
        else:
            out[full_key] = v
    return out




def solve(alias: str, record: dict, strategy: str = "ALL_MATCH"):
    """Run one decision table over one record; returns the matched rows in table order."""
    rule = _get_rules().get(alias)
    if rule is None:
        raise KeyError(f"Rule alias not found: {alias!r}")
    return _solve_decision_table(rule, [record], strategy)[0]
