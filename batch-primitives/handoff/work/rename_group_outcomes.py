"""Rename iter_ready_groups/GroupReady to group_outcomes/GroupOutcome and apply the approved
docstring edits in the current tree. Run from the repo root once per commit, as the
--tree-filter of git filter-branch (see Task 6 in tasks.md). Running it twice changes nothing.
"""
import pathlib
import re
import sys

ROOTS = [
    "clients/python/agentic-sandbox-client/k8s_agent_sandbox",
    "clients/python/agentic-sandbox-client/README.md",
    "docs/python_sdk_reference.md",
    "test/e2e/clients/python",
]

RENAMES = [
    (re.compile(r"\biter_ready_groups\b"), "group_outcomes"),
    (re.compile(r"\bGroupReady\b"), "GroupOutcome"),
    # pydoc-markdown escapes underscores in the reference's headings.
    (re.compile(r"\biter\\_ready\\_groups\b"), r"group\\_outcomes"),
]

# Approved docstring edits. Whitespace between words is matched loosely so the same edit
# applies to the source docstrings and to the generated reference in docs/python_sdk_reference.md.
EVENTS_OLD = r"Returns an (?:async )?iterator over the batch's events\."
EVENTS_NEW = "Hands out each initial claim once, as it becomes Ready, fails, or is lost."

GROUP_OLD = (
    r"It(\s+)fails with ``QuorumUnreachableError`` once too few of its members can still become Ready,"
    r"(\s+)or with ``BatchTimeoutError`` if it hasn't reached ``min_ready`` within the quorum timeout\."
)
GROUP_NEW = (
    "A failed{0}group is yielded with ``error`` set, not raised. The error is ``QuorumUnreachableError``"
    "{1}once too few of its members can still become Ready, or ``BatchTimeoutError`` if it hasn't reached"
    "{1}``min_ready`` within the quorum timeout."
)


def files():
    for root in ROOTS:
        p = pathlib.Path(root)
        if p.is_file():
            yield p
        elif p.is_dir():
            yield from (f for f in p.rglob("*") if f.suffix in (".py", ".md") and f.is_file())


def main() -> int:
    for f in files():
        text = f.read_text()
        new = text
        for pat, repl in RENAMES:
            new = pat.sub(repl, new)
        new = re.sub(EVENTS_OLD, EVENTS_NEW, new)
        new = re.sub(GROUP_OLD, lambda m: GROUP_NEW.format(m.group(1), m.group(2)), new)
        if new != text:
            f.write_text(new)
    return 0


if __name__ == "__main__":
    sys.exit(main())
