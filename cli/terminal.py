"""Interactive command-line terminal.

    $ python -m cli.terminal
    2026 Jul 25 / $ equity
    2026 Jul 25 /equity/ $ price
    2026 Jul 25 /equity/price/ $ quote --symbol AAPL

Menus and commands come straight from the registry, so anything added under
``backend/extensions`` shows up here without touching this file. Only the
standard library is used for rendering — no curses, no rich, no colour library.
"""
from __future__ import annotations

import json
import os
import shlex
import sys
import textwrap
from datetime import date
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Make `python cli/terminal.py` work as well as `python -m cli.terminal`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backend.extensions  # noqa: E402,F401 - importing registers every command
from backend.core.errors import MFTError  # noqa: E402
from backend.core.models import MFTObject  # noqa: E402
from backend.core.registry import (  # noqa: E402
    REGISTRY,
    CommandSpec,
    children,
    coverage,
    execute,
    get_spec,
    search,
)

# --------------------------------------------------------------------------- #
# Terminal styling
# --------------------------------------------------------------------------- #
_COLOUR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
RESET, BOLD, DIM = ("\033[0m", "\033[1m", "\033[2m") if _COLOUR else ("", "", "")
AMBER, CYAN, GREEN, RED = (
    ("\033[38;5;214m", "\033[36m", "\033[32m", "\033[31m") if _COLOUR else ("", "", "", "")
)

BANNER = r"""
  __  __ _____ _____   MINI FINANCIAL TERMINAL
 |  \/  |  ___|_   _|  open-source market research
 | |\/| | |_    | |    {commands} commands / {menus} menus / {providers} free data providers
 |_|  |_|_|     |_|    type `help` for the menu, `search <text>` to find a command
"""


def _fmt(value: Any, width: int = 22) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        text = "{:,.6g}".format(value)
    elif isinstance(value, bool):
        text = "yes" if value else "no"
    else:
        text = str(value).replace("\n", " ").strip()
    return text if len(text) <= width else text[: width - 1] + "…"


def render_table(rows: Sequence[Dict[str, Any]], max_rows: int = 25,
                 max_cols: int = 9, col_width: int = 22) -> str:
    """Plain-text table with the first ``max_cols`` columns."""
    if not rows:
        return DIM + "(no rows)" + RESET
    columns: List[str] = []
    for row in rows[:50]:
        for key in row:
            if key not in columns:
                columns.append(key)
    hidden = max(len(columns) - max_cols, 0)
    columns = columns[:max_cols]

    body = [[_fmt(row.get(c), col_width) for c in columns] for row in rows[:max_rows]]
    widths = [max(len(c), *(len(r[i]) for r in body)) if body else len(c)
              for i, c in enumerate(columns)]

    sep = "  "
    out = [BOLD + sep.join(c.ljust(w) for c, w in zip(columns, widths)) + RESET,
           DIM + sep.join("-" * w for w in widths) + RESET]
    for row in body:
        out.append(sep.join(cell.ljust(w) for cell, w in zip(row, widths)))
    notes = []
    if len(rows) > max_rows:
        notes.append("{} of {} rows".format(max_rows, len(rows)))
    if hidden:
        notes.append("{} more columns".format(hidden))
    if notes:
        out.append(DIM + "… " + ", ".join(notes) + RESET)
    return "\n".join(out)


def render_object(obj: MFTObject) -> str:
    rows = obj.to_records()
    if len(rows) == 1 and isinstance(rows[0], dict) and len(rows[0]) > 9:
        # A single wide record reads better as key/value pairs.
        pairs = [{"field": k, "value": _fmt(v, 60)} for k, v in rows[0].items()]
        table = render_table(pairs, max_rows=60, col_width=60)
    else:
        table = render_table(rows)
    footer = "{}{} row(s) · provider: {}{}".format(DIM, len(obj), obj.provider or "-", RESET)
    if obj.warnings:
        footer += "\n{}! {}{}".format(RED, "; ".join(str(w) for w in obj.warnings[:3]), RESET)
    return table + "\n" + footer


# --------------------------------------------------------------------------- #
# Argument parsing
# --------------------------------------------------------------------------- #
def _coerce(text: str) -> Any:
    low = text.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    if low in ("none", "null"):
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def parse_args(spec: CommandSpec, tokens: Sequence[str]) -> Dict[str, Any]:
    """``--symbol AAPL --limit 10`` plus a positional value for the first param."""
    accepted = {p["name"]: p for p in spec.parameters}
    kwargs: Dict[str, Any] = {}
    positional: List[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token.startswith("--"):
            name = token[2:].replace("-", "_")
            if name not in accepted:
                raise ValueError(
                    "{} has no parameter --{}. Try: {}".format(
                        spec.name, name, ", ".join("--" + k for k in accepted)
                    )
                )
            if i + 1 < len(tokens) and not tokens[i + 1].startswith("--"):
                kwargs[name] = _coerce(tokens[i + 1])
                i += 2
            else:  # bare flag == true
                kwargs[name] = True
                i += 1
        else:
            positional.append(token)
            i += 1
    if positional:
        first = next((p["name"] for p in spec.parameters if p["required"]), None)
        first = first or (spec.parameters[0]["name"] if spec.parameters else None)
        if first and first not in kwargs:
            kwargs[first] = _coerce(" ".join(positional))
    return kwargs


def command_help(spec: CommandSpec) -> str:
    lines = ["{}{}{}  {}".format(BOLD, spec.path, RESET, spec.description)]
    if spec.providers:
        lines.append("{}providers:{} {}".format(DIM, RESET, ", ".join(spec.providers)))
    lines.append("{}parameters:{}".format(DIM, RESET))
    for p in spec.parameters:
        flag = "--{}".format(p["name"])
        default = "required" if p["required"] else "default {!r}".format(p["default"])
        lines.append("  {:<22} {:<16} {}".format(flag, p["type"], default))
    doc = spec.func.__doc__
    if doc:
        lines.append("")
        lines.append(textwrap.indent(textwrap.dedent(doc).strip(), "  "))
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# The shell
# --------------------------------------------------------------------------- #
class Terminal:
    def __init__(self) -> None:
        self.path: List[str] = []
        self.last: Optional[MFTObject] = None
        self._setup_readline()

    # -- prompt & navigation ---------------------------------------------
    @property
    def location(self) -> str:
        return "/" + "/".join(self.path) + ("/" if self.path else "")

    def prompt(self) -> str:
        return "{}{} {}{}{} $ ".format(
            DIM, date.today().strftime("%Y %b %d"), AMBER + BOLD, self.location, RESET
        )

    def _setup_readline(self) -> None:
        try:
            import readline
        except ImportError:
            return
        readline.parse_and_bind("tab: complete")
        readline.set_completer_delims(" \t\n")

        def completer(text: str, state: int) -> Optional[str]:
            submenus, cmds = children("/".join(self.path))
            options = submenus + [c.name for c in cmds] + [
                "help", "home", "back", "quit", "search", "providers", "coverage",
                "export", "record", "cd",
            ]
            matches = [o for o in options if o.startswith(text)]
            return matches[state] if state < len(matches) else None

        readline.set_completer(completer)

    # -- display ----------------------------------------------------------
    def show_menu(self) -> None:
        submenus, cmds = children("/".join(self.path))
        print()
        if submenus:
            print("{}Menus{}".format(BOLD, RESET))
            for name in submenus:
                sub, subcmds = children("/".join(self.path + [name]))
                count = len(subcmds) + sum(len(children("/".join(self.path + [name, s]))[1]) for s in sub)
                print("  {}{:<24}{} {}{} commands{}".format(CYAN, name, RESET, DIM, count, RESET))
            print()
        if cmds:
            print("{}Commands{}".format(BOLD, RESET))
            for spec in cmds:
                print("  {}{:<24}{} {}".format(GREEN, spec.name, RESET, spec.description[:80]))
            print()
        if not submenus and not cmds:
            print(DIM + "(empty menu)" + RESET + "\n")
        print(DIM + "help · back (..) · home (/) · search <text> · quit" + RESET)

    # -- execution --------------------------------------------------------
    def run_command(self, spec: CommandSpec, tokens: Sequence[str]) -> None:
        if "--help" in tokens or "-h" in tokens:
            print(command_help(spec))
            return
        kwargs = parse_args(spec, tokens)
        missing = [p["name"] for p in spec.parameters if p["required"] and p["name"] not in kwargs]
        if missing:
            print("{}Missing required parameter(s): {}{}".format(RED, ", ".join(missing), RESET))
            print(DIM + "Try `{} --help`".format(spec.name) + RESET)
            return
        print(DIM + "running {} …".format(spec.path) + RESET)
        obj = execute(spec.path, **kwargs)
        self.last = obj
        print(render_object(obj))

    def export(self, tokens: Sequence[str]) -> None:
        if self.last is None:
            print(RED + "Nothing to export — run a command first." + RESET)
            return
        target = tokens[0] if tokens else "export.csv"
        rows = self.last.to_records()
        if target.endswith(".json"):
            with open(target, "w") as fh:
                json.dump(self.last.to_dict(), fh, indent=2, default=str)
        else:
            import csv

            columns: List[str] = []
            for row in rows:
                for key in row:
                    if key not in columns:
                        columns.append(key)
            with open(target, "w", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=columns)
                writer.writeheader()
                writer.writerows(rows)
        print("{}wrote {} row(s) to {}{}".format(GREEN, len(rows), target, RESET))

    # -- the loop ---------------------------------------------------------
    def handle(self, line: str) -> bool:
        """Process one input line. Returns False to exit."""
        try:
            tokens = shlex.split(line)
        except ValueError as exc:
            print("{}{}{}".format(RED, exc, RESET))
            return True
        if not tokens:
            return True
        head, rest = tokens[0], tokens[1:]

        if head in ("quit", "exit", "q", "e"):
            return False
        if head in ("help", "h", "?"):
            self.show_menu()
            return True
        if head in ("home", "/", "root"):
            self.path = []
            self.show_menu()
            return True
        if head in ("back", "..", "cd.."):
            if self.path:
                self.path.pop()
            self.show_menu()
            return True
        if head == "cd" and rest:
            return self.handle(" ".join(rest))
        if head == "search":
            hits = search(" ".join(rest)) if rest else []
            if not hits:
                print(DIM + "no matching commands" + RESET)
            for spec in hits[:40]:
                print("  {}{:<46}{} {}".format(GREEN, spec.path, RESET, spec.description[:60]))
            return True
        if head == "coverage":
            print(json.dumps(coverage(), indent=2))
            return True
        if head == "providers":
            from backend.providers import provider_table

            print(render_table(provider_table(), max_rows=40, col_width=48))
            return True
        if head == "export":
            self.export(rest)
            return True
        if head == "clear":
            os.system("cls" if os.name == "nt" else "clear")
            return True

        # Absolute path: `/equity/price/quote --symbol AAPL`
        if head.startswith("/") and len(head) > 1:
            try:
                spec = get_spec(head)
            except MFTError:
                self.path = [p for p in head.split("/") if p]
                self.show_menu()
                return True
            self.run_command(spec, rest)
            return True

        submenus, cmds = children("/".join(self.path))
        if head in submenus:
            self.path.append(head)
            self.show_menu()
            return True
        match = next((c for c in cmds if c.name == head), None)
        if match:
            self.run_command(match, rest)
            return True

        print("{}Unknown input {!r}.{} Try `help`, or `search {}`.".format(RED, head, RESET, head))
        return True

    def loop(self) -> None:
        from backend.providers import PROVIDERS

        print(AMBER + BANNER.format(commands=len(REGISTRY), menus=len(set(s.tag for s in REGISTRY.values())),
                                    providers=len(PROVIDERS)) + RESET)
        self.show_menu()
        while True:
            try:
                line = input(self.prompt())
            except (EOFError, KeyboardInterrupt):
                print()
                break
            try:
                if not self.handle(line):
                    break
            except MFTError as exc:
                print("{}{}{}".format(RED, exc, RESET))
            except (ValueError, TypeError, KeyError) as exc:
                print("{}{}: {}{}".format(RED, type(exc).__name__, exc, RESET))
            except Exception as exc:  # noqa: BLE001 - never drop the user out of the shell
                print("{}unexpected error: {}{}".format(RED, exc, RESET))
        print(DIM + "bye." + RESET)


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    terminal = Terminal()
    if argv:
        # Non-interactive: `python -m cli.terminal "/equity/price/quote --symbol AAPL"`
        for line in argv:
            terminal.handle(line)
        return 0
    terminal.loop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
