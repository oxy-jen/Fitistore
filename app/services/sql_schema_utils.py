import re


def _boolean_words_at_top_level(expression: str) -> set[str]:
    words = set()
    depth = 0
    quoted = False
    index = 0
    while index < len(expression):
        char = expression[index]
        if char == "'":
            if quoted and index + 1 < len(expression) and expression[index + 1] == "'":
                index += 2
                continue
            quoted = not quoted
        elif not quoted and char == "(":
            depth += 1
        elif not quoted and char == ")":
            depth -= 1
        elif not quoted and depth == 0 and (char.isalpha() or char == "_"):
            end = index + 1
            while end < len(expression) and (expression[end].isalnum() or expression[end] == "_"):
                end += 1
            word = expression[index:end].lower()
            if word in {"and", "or"}:
                words.add(word)
            index = end
            continue
        index += 1
    return words


def _strip_redundant_conjunction_groups(expression: str) -> str:
    output = []
    index = 0
    while index < len(expression):
        if expression[index] != "(":
            output.append(expression[index])
            index += 1
            continue
        depth = 1
        end = index + 1
        quoted = False
        while end < len(expression) and depth:
            char = expression[end]
            if char == "'":
                if quoted and end + 1 < len(expression) and expression[end + 1] == "'":
                    end += 2
                    continue
                quoted = not quoted
            elif not quoted and char == "(":
                depth += 1
            elif not quoted and char == ")":
                depth -= 1
            end += 1
        if depth:
            output.append(expression[index:])
            break
        inner = _strip_redundant_conjunction_groups(expression[index + 1:end - 1])
        prefix = "".join(output).rstrip()
        previous_word = re.search(r"([a-z_][a-z0-9_]*)$", prefix, re.I)
        previous_word = previous_word.group(1).lower() if previous_word else ""
        is_function_or_clause = previous_word not in {
            "", "and", "or", "not", "check", "where", "when", "then", "else", "in", "any", "all",
        }
        boolean_words = _boolean_words_at_top_level(inner)
        if "and" in boolean_words and "or" not in boolean_words and previous_word != "not" and not is_function_or_clause:
            output.append(inner)
        else:
            output.append("(" + inner + ")")
        index = end
    return "".join(output)


def normalize_sql_definition(value: str) -> str:
    value = str(value or "").lower().replace('"', "")
    value = re.sub(r"::(?:character varying|varchar|text|boolean)(?:\[\])?", "", value)
    value = re.sub(r"\bbtrim\s*\(([^()]*)\)", r"trim(\1)", value)
    value = re.sub(r"\btrim\s*\(\s*both\s+from\s+([^()]*)\)", r"trim(\1)", value)
    value = re.sub(
        r"=\s*any\s*\(\s*(?:\(\s*array\s*\[(.*?)\]\s*\)|array\s*\[(.*?)\])\s*\)",
        lambda match: " in (" + (match.group(1) if match.group(1) is not None else match.group(2)) + ")",
        value,
    )
    value = re.sub(r"(?<![a-z0-9_])\(([a-z_][a-z0-9_.]*)\)", r"\1", value)
    while True:
        value, null_predicates = re.subn(
            r"\(\s*([a-z_][a-z0-9_.]*\s+is\s+(?:not\s+)?null)\s*\)",
            r"\1", value,
        )
        value, in_predicates = re.subn(
            r"\(\s*([a-z_][a-z0-9_.]*\s+in\s*\([^()]*\))\s*\)",
            r"\1", value,
        )
        value, comparison_predicates = re.subn(
            r"\(\s*([a-z_][a-z0-9_.]*\s*(?:=|<>|!=|<=|>=|<|>)\s*'[^']*')\s*\)",
            r"\1", value,
        )
        if not null_predicates and not in_predicates and not comparison_predicates:
            break
    value = re.sub(r"(?<![a-z0-9_])\(([a-z_][a-z0-9_.]*)\)", r"\1", value)
    value = _strip_redundant_conjunction_groups(value)
    value = re.sub(r"\s+", "", value)
    prefix = "check" if value.startswith("check") else ""
    expression = value[len(prefix):].lstrip()
    while expression.startswith("(") and expression.endswith(")"):
        depth = 0
        wraps_all = True
        quote = False
        for index, char in enumerate(expression):
            if char == "'":
                quote = not quote
            elif not quote and char == "(":
                depth += 1
            elif not quote and char == ")":
                depth -= 1
                if depth == 0 and index != len(expression) - 1:
                    wraps_all = False
                    break
        if not wraps_all:
            break
        expression = expression[1:-1]
    return prefix + expression
