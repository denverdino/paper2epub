#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pypdfium2",
#     "Pillow",
#     "openai",
#     "PySocks",
#     "httpx[socks]",
#     "pylatexenc>=2.10,<3",
# ]
# ///
"""Download an arXiv paper's LaTeX source and convert it to EPUB."""

import argparse
import datetime
from dataclasses import dataclass, replace
from enum import Enum, IntEnum
import hashlib
import os
import re
import shutil
import smtplib
import socket
import subprocess
import sys
import threading
from urllib.parse import urlparse
import tarfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.message import EmailMessage
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from functools import cache
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, TypeVar

from pylatexenc.latexwalker import (
    LatexCharsNode,
    LatexCommentNode,
    LatexEnvironmentNode,
    LatexGroupNode,
    LatexMacroNode,
    LatexMathNode,
    LatexWalker,
    LatexWalkerParseError,
    get_default_latex_context_db,
)
from pylatexenc.macrospec import EnvironmentSpec, MacroSpec, MacroStandardArgsParser

SCRIPT_DIR = Path(__file__).resolve().parent


class Safety(Enum):
    SAFE = "safe"
    LOSSY = "lossy"
    FALLBACK_ONLY = "fallback_only"


class Fact(Enum):
    SYNTAX = "syntax"
    DISCOVERY = "discovery"
    INCLUDE_GRAPH = "include_graph"
    MACROS = "macros"
    PACKAGES = "packages"
    RESOURCES = "resources"
    LABELS = "labels"


class Phase(IntEnum):
    DISCOVERY = 0
    SAFE_NORMALIZATION = 1
    COMPATIBILITY = 2


class Implementation(Enum):
    SYNTAX_AWARE = "syntax_aware"


@dataclass(frozen=True)
class SourceFile:
    path: Path
    content: str

    @classmethod
    def from_path(cls, path: Path) -> "SourceFile":
        with path.open(encoding="utf-8", errors="replace", newline="") as stream:
            return cls(path=path, content=stream.read())

    @property
    def newline(self) -> str:
        """Prefer CRLF when present; otherwise use LF, including no-newline input."""
        return "\r\n" if "\r\n" in self.content else "\n"


def _normalize_newlines(text: str, newline: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").replace(
        "\n", newline,
    )


@dataclass(frozen=True)
class Edit:
    file: Path
    start: int
    end: int
    replacement: str
    pass_name: str
    safety: Safety


@dataclass(frozen=True)
class Diagnostic:
    file: Path
    pass_name: str
    code: str
    message: str
    start: int | None = None
    end: int | None = None


@dataclass(frozen=True)
class ResourceResult:
    converted: frozenset[Path]
    diagnostics: tuple[Diagnostic, ...]


PassPlanner = Callable[["DocumentSnapshot"], "PlanOutcome | list[Edit]"]


@dataclass(frozen=True)
class PassSpec:
    name: str
    planner: PassPlanner
    phase: Phase
    safety: Safety
    requires: frozenset[Fact]
    invalidates: frozenset[Fact]
    after: frozenset[str] = frozenset()
    before: frozenset[str] = frozenset()
    implementation: Implementation = Implementation.SYNTAX_AWARE
    idempotent: bool = False
    report_label: str | None = None


_PARSER_MACRO_ARGS = {
    "addtocounter": "{{",
    "addtolength": "{{",
    "adjustbox": "{{",
    "author": "{",
    "caption": "{",
    "captionsetup": "*[{",
    "chapter": "*[{",
    "mintinline": "[{{",
    "ccsdesc": "[{",
    "captionof": "{{",
    "csgdef": "{{",
    "DeclareSIUnit": "*[{{",
    "DeclareMathOperator": "*{{",
    "DeclareRobustCommand": "*{[[{",
    "definecolor": "*[{{{",
    "documentclass": "[{",
    "enlargethispage": "*{",
    "fancyhf": "*[{",
    "fancyfoot": "*[{",
    "fancyhead": "*[{",
    "graphicspath": "{",
    "hphantom": "*{",
    "hspace": "*{",
    "hypersetup": "*{",
    "include": "{",
    "includegraphics": "[{",
    "input": "{",
    "hyperlink": "{{",
    "hyperref": "[{",
    "hypertarget": "{{",
    "Hy@raisedlink": "{",
    "HyZraisedlink": "{",
    "linespread": "*{",
    "lstdefinestyle": "*{{",
    "lstset": "*{",
    "maketitle": "",
    "marginpar": "*{",
    "makecell": "*[{",
    "multicolumn": "{{{",
    "newcommand": "*{[[{",
    "newcounter": "{",
    "newtheorem": "*{[{[",
    "pagestyle": "*{",
    "paragraph": "*[{",
    "pgfplotsset": "*{",
    "phantom": "*{",
    "providecommand": "*{[[{",
    "RequirePackage": "[{",
    "renewcommand": "*{[[{",
    "resizebox": "*{{{",
    "section": "*[{",
    "setcounter": "{{",
    "setlength": "{{",
    "sisetup": "*{",
    "stepcounter": "*{",
    "subsection": "*[{",
    "subsubsection": "*[{",
    "tcbset": "*{",
    "textsc": "{",
    "texorpdfstring": "{{",
    "ding": "{",
    "textcircled": "{",
    "title": "[{",
    "todo": "*{",
    "fixme": "*{",
    "usepackage": "[{",
    "usetikzlibrary": "*{",
    "vphantom": "*{",
    "vspace": "*{",
    "label": "{",
    "Call": "{{",
}

_CITE_ALIASES = (
    "parencite", "textcite", "autocite", "fullcite", "footcite",
    "citeauthor", "citetitle", "citep", "citet", "Citet", "Citep",
    "citealt", "citealp", "citenum", "citeyear",
)

for _cite_alias in _CITE_ALIASES:
    _PARSER_MACRO_ARGS[_cite_alias] = "*[[{"

_NON_PROSE_LITERAL_MACROS = {
    "chapter", "section", "subsection", "subsubsection", "paragraph",
    "label", "caption", "includegraphics", "input", "include",
    "bibliography", "bibliographystyle", "documentclass", "usepackage",
    "RequirePackage", "title", "author", "date", "keywords",
    "newcommand", "renewcommand", "providecommand", "DeclareRobustCommand",
    "DeclareMathOperator", "setlength", "setcounter", "addtocounter",
    "addtolength", "newcounter", "graphicspath", "definecolor",
    "captionsetup", "fancyhead", "fancyfoot", "pagestyle",
    "cite", *_CITE_ALIASES,
}

_PARSER_ENVIRONMENT_ARGS = {
    "adjustbox": "{",
    "algorithm": "[",
    "algorithm*": "[",
    "algorithmic": "[",
    "CCSXML": "",
    "comment": "",
    "lstlisting": "[",
    "ltablex": "[{",
    "longtable": "[{",
    "minipage": "[[[{",
    "minted": "[{",
    "NiceTabular": "[{[",
    "NiceTabular*": "[{[{[",
    "subfigure": "[{",
    "subequations": "",
    "tabular": "{",
    "tabular*": "{",
    "tabularx": "[{{",
    "tabulary": "[{{",
    "tabu": "[{",
    "tikzpicture": "[",
    "tblr": "[{",
    "wrapfigure": "[{{",
    "xltabular": "[{{",
}

_PARSER_ARGUMENT_INDEXES = {
    "chapter": (2,),
    "paragraph": (2,),
    "resizebox": (1, 2, 3),
    "section": (2,),
    "subsection": (2,),
    "subsubsection": (2,),
}

_DEFINITION_BODY_COUNTS = MappingProxyType({
    "newcommand": 1,
    "renewcommand": 1,
    "providecommand": 1,
    "DeclareRobustCommand": 1,
    "newenvironment": 2,
})
_DEFINITION_COMMAND_RE = re.compile(
    r"\\(newcommand|renewcommand|providecommand|DeclareRobustCommand|newenvironment)\*?"
)
_ENVIRONMENT_TOKEN_RE = re.compile(r"\\(?:begin|end)(?=\s|%|\{)")
_DEFINITION_MASK_CHARACTER: Final[str] = "."


def _is_escaped_character(content: str, pos: int) -> bool:
    backslashes = 0
    pos -= 1
    while pos >= 0 and content[pos] == "\\":
        backslashes += 1
        pos -= 1
    return backslashes % 2 == 1


def strip_tex_comments(content: str) -> str:
    characters: list[str] = []
    pos = 0
    while pos < len(content):
        if content[pos] == "%" and not _is_escaped_character(content, pos):
            while pos < len(content) and content[pos] not in "\r\n":
                pos += 1
            continue
        characters.append(content[pos])
        pos += 1
    return "".join(characters)


def _in_tex_comment(content: str, pos: int) -> bool:
    line_start = max(
        content.rfind("\n", 0, pos),
        content.rfind("\r", 0, pos),
    ) + 1
    percent = content.find("%", line_start, pos)
    while percent >= 0:
        if not _is_escaped_character(content, percent):
            return True
        percent = content.find("%", percent + 1, pos)
    return False


def _iter_environment_token_ranges(
    content: str,
    start: int = 0,
    end: int | None = None,
) -> Iterable[tuple[int, int]]:
    limit = len(content) if end is None else end
    for match in _ENVIRONMENT_TOKEN_RE.finditer(content, start, limit):
        token_start = match.start()
        if (
            _is_escaped_character(content, token_start)
            or _in_tex_comment(content, token_start)
        ):
            continue

        pos = match.end()
        while pos < limit:
            if content[pos].isspace():
                pos += 1
                continue
            if content[pos] != "%" or _is_escaped_character(content, pos):
                break
            newline = re.search(r"\r\n|\r|\n", content[pos:limit])
            if newline is None:
                pos = limit
                break
            pos += newline.end()

        if pos >= limit or content[pos] != "{":
            continue
        closing = content.find("}", pos + 1, limit)
        if closing < 0:
            continue
        name = content[pos + 1:closing]
        if not name or any(character in name for character in "{}\r\n"):
            continue
        yield token_start, closing + 1


def _complete_group_range(content: str, pos: int) -> tuple[int, int] | None:
    while pos < len(content) and content[pos].isspace():
        pos += 1
    if pos >= len(content) or content[pos] != "{":
        return None
    closing = find_matching_brace(content, pos)
    return None if closing is None else (pos, closing + 1)


def _definition_body_ranges(content: str) -> tuple[tuple[int, int], ...]:
    ranges: list[tuple[int, int]] = []
    for match in _DEFINITION_COMMAND_RE.finditer(content):
        if (
            _is_escaped_character(content, match.start())
            or _in_tex_comment(content, match.start())
        ):
            continue
        definition_name = match.group(1)
        pos = match.end()
        name_group = _complete_group_range(content, pos)
        if name_group is not None:
            name_text = content[name_group[0] + 1:name_group[1] - 1]
            if definition_name == "newenvironment":
                if (
                    not name_text.strip()
                    or any(character in name_text for character in "{}\r\n")
                ):
                    continue
            elif re.fullmatch(
                r"\s*\\(?:[A-Za-z@]+|.)\s*", name_text
            ) is None:
                continue
            pos = name_group[1]
        else:
            if definition_name == "newenvironment":
                continue
            while pos < len(content) and content[pos].isspace():
                pos += 1
            name = re.match(r"\\(?:[A-Za-z@]+|.)", content[pos:])
            if name is None:
                continue
            pos += name.end()
        valid = True
        while True:
            while pos < len(content) and content[pos].isspace():
                pos += 1
            if pos >= len(content) or content[pos] != "[":
                break
            closing = find_matching_bracket(content, pos)
            if closing is None:
                valid = False
                break
            pos = closing + 1
        if not valid:
            continue
        bodies: list[tuple[int, int]] = []
        for _ in range(_DEFINITION_BODY_COUNTS[definition_name]):
            group = _complete_group_range(content, pos)
            if group is None:
                bodies = []
                break
            bodies.append((group[0] + 1, group[1] - 1))
            pos = group[1]
        ranges.extend(bodies)
    return tuple(ranges)


def _mask_definition_environment_tokens(content: str) -> str:
    characters = list(content)
    for start, end in _definition_body_ranges(content):
        for token_start, token_end in _iter_environment_token_ranges(
            content, start, end,
        ):
            characters[token_start:token_end] = [
                character
                if character in "\r\n"
                else _DEFINITION_MASK_CHARACTER
                for character in content[token_start:token_end]
            ]
    return "".join(characters)


@cache
def _build_latex_context(
    extra_macro_args: tuple[tuple[str, str], ...] = (),
    extra_environment_args: tuple[tuple[str, str], ...] = (),
):
    context = get_default_latex_context_db()
    macro_args = dict(_PARSER_MACRO_ARGS)
    macro_args.update(extra_macro_args)
    environment_args = dict(_PARSER_ENVIRONMENT_ARGS)
    environment_args.update(extra_environment_args)
    context.add_context_category(
        "paper2epub",
        macros=[
            MacroSpec(name, MacroStandardArgsParser(argspec))
            for name, argspec in macro_args.items()
        ],
        environments=[
            EnvironmentSpec(name, MacroStandardArgsParser(argspec))
            for name, argspec in environment_args.items()
        ],
        prepend=True,
    )
    return context


@dataclass(frozen=True)
class LatexArgumentRef:
    start: int
    end: int
    text: str
    opening_delimiter: str | None
    closing_delimiter: str | None
    complete: bool
    opaque: bool


@dataclass(frozen=True)
class LatexNodeRef:
    file: Path
    kind: str
    name: str
    start: int
    end: int
    parent_environment: str | None
    arguments: tuple[LatexArgumentRef | None, ...]
    command_token_end: int
    command_post_space_end: int
    begin_token_end: int
    end_token_start: int
    body_start: int
    body_end: int
    complete: bool
    opaque: bool


@dataclass(frozen=True)
class LatexMathRef:
    start: int
    end: int
    display: bool
    opening_delimiter: str
    closing_delimiter: str
    complete: bool
    opaque: bool


def _public_argument(ref: LatexNodeRef, index: int) -> LatexArgumentRef | None:
    argument_indexes = _PARSER_ARGUMENT_INDEXES.get(ref.name)
    if argument_indexes is not None:
        if index < 0 or index >= len(argument_indexes):
            return None
        index = argument_indexes[index]
    if index < 0 or index >= len(ref.arguments):
        return None
    return ref.arguments[index]


class LatexDocument:
    def __init__(
        self,
        source: SourceFile,
        *,
        macro_args: Mapping[str, str] | None = None,
        environment_args: Mapping[str, str] | None = None,
    ):
        self.source = source
        self.parse_warnings: tuple[str, ...] = ()
        pending_diagnostics: list[Diagnostic] = []
        self._pending_diagnostics = pending_diagnostics
        try:
            parser_content = _mask_definition_environment_tokens(
                source.content
            ).replace(
                r"\Hy@raisedlink", r"\HyZraisedlink",
            )
            walker = LatexWalker(
                parser_content,
                latex_context=_build_latex_context(
                    tuple(sorted((macro_args or {}).items())),
                    tuple(sorted((environment_args or {}).items())),
                ),
                tolerant_parsing=True,
            )
            nodes, _, _ = walker.get_latex_nodes(pos=0)
        except LatexWalkerParseError as exc:
            nodes = []
            self.parse_warnings = (str(exc),)
        self.math = tuple(self._walk_math(nodes))
        self.comment_ranges = tuple(self._walk_comments(nodes))
        refs = tuple(self._walk(nodes, parent_environment=None))
        self._refs = self._propagate_opaque_ranges(refs)
        self.literal_alpha_count = self._count_literal_alpha(nodes)
        self.diagnostics = tuple(pending_diagnostics)
        del self._pending_diagnostics

    def _walk_math(self, nodes: Iterable[Any]) -> Iterable[LatexMathRef]:
        for node in nodes:
            if isinstance(node, LatexMathNode):
                opening, closing = node.delimiters
                start = node.pos
                end = node.pos + node.len
                complete = (
                    self.source.content.startswith(opening, start)
                    and self.source.content.endswith(closing, start, end)
                )
                yield LatexMathRef(
                    start=start,
                    end=end,
                    display=node.displaytype == "display",
                    opening_delimiter=opening,
                    closing_delimiter=closing,
                    complete=complete,
                    opaque=not complete,
                )
                continue

            nodeargd = getattr(node, "nodeargd", None)
            if nodeargd is not None:
                for argument in nodeargd.argnlist:
                    if argument is not None:
                        yield from self._walk_math((argument,))

            children = getattr(node, "nodelist", None)
            if children:
                yield from self._walk_math(children)

    def _walk_comments(
        self, nodes: Iterable[Any],
    ) -> Iterable[tuple[int, int]]:
        for node in nodes:
            if isinstance(node, LatexCommentNode):
                yield node.pos, node.pos + node.len
                continue
            if isinstance(node, LatexMathNode):
                continue

            nodeargd = getattr(node, "nodeargd", None)
            if nodeargd is not None:
                for argument in nodeargd.argnlist:
                    if argument is not None:
                        yield from self._walk_comments((argument,))

            children = getattr(node, "nodelist", None)
            if children:
                yield from self._walk_comments(children)

    @classmethod
    def _count_literal_alpha(cls, nodes: Iterable[Any]) -> int:
        count = 0
        for node in nodes:
            if isinstance(node, LatexCharsNode):
                count += sum(character.isalpha() for character in node.chars)
                continue
            if isinstance(node, (LatexCommentNode, LatexMathNode)):
                continue
            if isinstance(node, LatexMacroNode):
                if node.macroname in _NON_PROSE_LITERAL_MACROS:
                    continue
                nodeargd = getattr(node, "nodeargd", None)
                if nodeargd is not None:
                    count += cls._count_literal_alpha(
                        argument
                        for argument in nodeargd.argnlist
                        if argument is not None
                    )
                continue
            children = getattr(node, "nodelist", None)
            if children:
                count += cls._count_literal_alpha(children)
        return count

    @staticmethod
    def _propagate_opaque_ranges(
        refs: tuple[LatexNodeRef, ...],
    ) -> tuple[LatexNodeRef, ...]:
        opaque_ranges = tuple(
            (ref.start, ref.end) for ref in refs if ref.opaque
        )
        return tuple(
            replace(ref, opaque=True)
            if not ref.opaque and any(
                start <= ref.start and ref.end <= end
                for start, end in opaque_ranges
            )
            else ref
            for ref in refs
        )

    def _walk(
        self,
        nodes: Iterable[Any],
        parent_environment: str | None,
    ) -> Iterable[LatexNodeRef]:
        for node in nodes:
            child_parent = parent_environment
            if isinstance(node, LatexMacroNode):
                yield self._make_ref(node, "command", node.macroname,
                                     parent_environment)
            elif isinstance(node, LatexEnvironmentNode):
                child_parent = node.environmentname
                yield self._make_ref(node, "environment", node.environmentname,
                                     parent_environment)

            nodeargd = getattr(node, "nodeargd", None)
            if nodeargd is not None:
                for argument in nodeargd.argnlist:
                    if argument is not None:
                        yield from self._walk((argument,), child_parent)

            children = getattr(node, "nodelist", None)
            if children:
                yield from self._walk(children, child_parent)

    def _make_argument_ref(self, argument: Any) -> LatexArgumentRef:
        start = argument.pos
        end = argument.pos + argument.len
        opening = closing = None
        complete = True
        content_start = start
        content_end = end
        if isinstance(argument, LatexGroupNode):
            opening, closing = argument.delimiters
            if opening is not None:
                content_start += len(opening)
            if closing is not None:
                matching_end = None
                if opening == "{" and closing == "}":
                    matching_end = find_matching_brace(self.source.content, start)
                elif opening == "[" and closing == "]":
                    matching_end = find_matching_bracket(self.source.content, start)
                complete = matching_end == end - len(closing)
                if complete:
                    content_end -= len(closing)
        return LatexArgumentRef(
            start=start,
            end=end,
            text=self.source.content[content_start:content_end],
            opening_delimiter=opening,
            closing_delimiter=closing,
            complete=complete,
            opaque=not complete,
        )

    def _make_ref(
        self,
        node: Any,
        kind: str,
        name: str,
        parent_environment: str | None,
    ) -> LatexNodeRef:
        if name == "HyZraisedlink":
            name = "Hy@raisedlink"
        nodeargd = getattr(node, "nodeargd", None)
        argspec = (
            _PARSER_MACRO_ARGS.get(name, "")
            if kind == "command"
            else _PARSER_ENVIRONMENT_ARGS.get(name, "")
        )
        argument_nodes = list(nodeargd.argnlist) if nodeargd is not None else []
        if len(argument_nodes) < len(argspec):
            argument_nodes.extend([None] * (len(argspec) - len(argument_nodes)))
        arguments = [
            None if argument is None else self._make_argument_ref(argument)
            for argument in argument_nodes
        ]

        token_end = node.pos
        post_space_end = node.pos
        begin_token_end = node.pos
        if kind == "command":
            token_end = node.pos + 1 + len(name)
            post_space_end = token_end + len(getattr(node, "macro_post_space", ""))

        else:
            begin_token = f"\\begin{{{name}}}"
            begin_token_end = node.pos + len(begin_token)
            post_space_end = begin_token_end

        # Tolerant parsing leaves an unmatched optional group outside the
        # command or environment node. Represent it explicitly so callers
        # never infer completeness from parser-specific node behavior.
        for index, marker in enumerate(argspec):
            if marker != "[" or arguments[index] is not None:
                continue
            recovery_start = max(
                [post_space_end]
                + [
                    argument.end
                    for argument in arguments[:index]
                    if argument is not None
                ]
            )
            if self.source.content.startswith("[", recovery_start):
                arguments[index] = LatexArgumentRef(
                    start=recovery_start,
                    end=len(self.source.content),
                    text=self.source.content[recovery_start + 1:],
                    opening_delimiter="[",
                    closing_delimiter="]",
                    complete=False,
                    opaque=True,
                )
            break

        required_indexes = tuple(
            index for index, marker in enumerate(argspec) if marker == "{"
        )
        complete = all(argument is None or argument.complete for argument in arguments)
        complete = complete and all(
            index < len(arguments)
            and arguments[index] is not None
            and arguments[index].opening_delimiter == "{"
            and arguments[index].closing_delimiter == "}"
            for index in required_indexes
        )
        end = max(
            [node.pos + node.len]
            + [argument.end for argument in arguments if argument is not None]
        )
        end_token_start = end
        body_start = node.pos
        body_end = end
        if kind == "environment":
            body_start = max(
                [begin_token_end]
                + [argument.end for argument in arguments if argument is not None]
            )
            end_token = f"\\end{{{name}}}"
            has_end_token = self.source.content.endswith(
                end_token, node.pos, end
            )
            if has_end_token:
                end_token_start = end - len(end_token)
                body_end = end_token_start
            else:
                complete = False
                self._pending_diagnostics.append(Diagnostic(
                    file=self.source.path,
                    pass_name="parser",
                    code="incomplete-environment",
                    message=f"Environment {name!r} has no matching end token",
                    start=node.pos,
                    end=end,
                ))
        elif not complete:
            self._pending_diagnostics.append(Diagnostic(
                file=self.source.path,
                pass_name="parser",
                code="incomplete-command",
                message=f"Command {name!r} has an incomplete argument",
                start=node.pos,
                end=end,
            ))
        return LatexNodeRef(
            file=self.source.path,
            kind=kind,
            name=name,
            start=node.pos,
            end=end,
            parent_environment=parent_environment,
            arguments=tuple(arguments),
            command_token_end=token_end,
            command_post_space_end=post_space_end,
            begin_token_end=begin_token_end,
            end_token_start=end_token_start,
            body_start=body_start,
            body_end=body_end,
            complete=complete,
            opaque=not complete,
        )

    def commands(self, name: str) -> list[LatexNodeRef]:
        return [
            ref for ref in self._refs
            if ref.kind == "command" and ref.name == name
        ]

    def environments(self, name: str) -> list[LatexNodeRef]:
        return [
            ref for ref in self._refs
            if ref.kind == "environment" and ref.name == name
        ]

    def source_text(self, ref: LatexNodeRef) -> str:
        return self.source.content[ref.start:ref.end]

    def argument_text(self, ref: LatexNodeRef, index: int) -> str | None:
        argument = self.argument(ref, index)
        return None if argument is None else argument.text

    def argument(
        self,
        ref: LatexNodeRef,
        index: int,
    ) -> LatexArgumentRef | None:
        return _public_argument(ref, index)


_K = TypeVar("_K")
_V = TypeVar("_V")


def read_only_mapping(values: Mapping[_K, _V]) -> Mapping[_K, _V]:
    return MappingProxyType(dict(values))


@dataclass(frozen=True)
class MacroDefinition:
    body: str
    parameter_count: int = 0
    optional_default: str | None = None
    math_operator: bool = False


@dataclass(frozen=True)
class DiscoveryFacts:
    title: str | None
    authors: tuple[str, ...]
    abstract: str
    macros: Mapping[str, MacroDefinition]
    packages: tuple[str, ...]
    include_order: tuple[Path, ...]
    graphicspaths: tuple[str, ...]
    resource_refs: tuple[tuple[Path, str], ...]
    labels: Mapping[str, tuple[Path, int]]
    theorem_labels: Mapping[str, str]


@dataclass(frozen=True)
class DocumentSnapshot:
    root: Path
    main_tex: Path
    revision: int
    sources: Mapping[Path, SourceFile]
    documents: Mapping[Path, LatexDocument]
    current_facts: frozenset[Fact]
    discovery: DiscoveryFacts | None = None

    @classmethod
    def from_directory(
        cls,
        root: Path,
        main_tex: Path,
        revision: int = 0,
    ) -> "DocumentSnapshot":
        paths = sorted(root.rglob("*.tex"))
        sources = {path: SourceFile.from_path(path) for path in paths}
        documents = {
            path: LatexDocument(source) for path, source in sources.items()
        }
        return cls(
            root=root,
            main_tex=main_tex,
            revision=revision,
            sources=read_only_mapping(sources),
            documents=read_only_mapping(documents),
            current_facts=frozenset({Fact.SYNTAX}),
            discovery=None,
        )


class PassDependencyError(ValueError):
    pass


def resolve_pass_order(passes: Iterable[PassSpec]) -> tuple[PassSpec, ...]:
    specs = tuple(passes)
    by_name: dict[str, PassSpec] = {}
    for spec in specs:
        if spec.name in by_name:
            raise PassDependencyError(f"duplicate pass name: {spec.name}")
        by_name[spec.name] = spec

    edges = {name: set() for name in by_name}
    for spec in specs:
        for dependency in spec.after:
            if dependency not in by_name:
                raise PassDependencyError(
                    f"missing pass dependency for {spec.name}: {dependency}"
                )
            edges[dependency].add(spec.name)
        for dependent in spec.before:
            if dependent not in by_name:
                raise PassDependencyError(
                    f"missing pass dependency for {spec.name}: {dependent}"
                )
            edges[spec.name].add(dependent)

    for earlier in specs:
        for later in specs:
            if earlier.phase < later.phase:
                edges[earlier.name].add(later.name)

    indegree = {name: 0 for name in by_name}
    for dependents in edges.values():
        for dependent in dependents:
            indegree[dependent] += 1

    ready = sorted(name for name, degree in indegree.items() if degree == 0)
    ordered: list[PassSpec] = []
    while ready:
        name = ready.pop(0)
        ordered.append(by_name[name])
        for dependent in sorted(edges[name]):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
                ready.sort()

    if len(ordered) != len(specs):
        blocked = sorted(name for name, degree in indegree.items() if degree)
        raise PassDependencyError(
            f"pass dependency cycle involving: {', '.join(blocked)}"
        )
    return tuple(ordered)


class PipelineContractError(ValueError):
    pass


FactBuilder = Callable[[DocumentSnapshot], DocumentSnapshot]


@dataclass(frozen=True)
class PassExecution:
    pass_name: str
    revision_before: int
    revision_after: int
    changed_files: tuple[Path, ...]
    rebuilt_before: frozenset[Fact]


@dataclass(frozen=True)
class PipelineResult:
    snapshot: DocumentSnapshot
    executions: tuple[PassExecution, ...]
    diagnostics: tuple[Diagnostic, ...] = ()


_SAFETY_RANK = {
    Safety.SAFE: 0,
    Safety.LOSSY: 1,
    Safety.FALLBACK_ONLY: 2,
}


class PassPipeline:
    def __init__(
        self,
        passes: Iterable[PassSpec],
        fact_builders: Mapping[Fact, FactBuilder] | None = None,
        fallback_enabled: bool = False,
    ):
        self.passes = resolve_pass_order(passes)
        self.fallback_enabled = fallback_enabled
        builders = {
            Fact.SYNTAX: rebuild_syntax,
            Fact.DISCOVERY: build_discovery_facts,
        }
        if fact_builders is not None:
            builders.update(fact_builders)
        required = frozenset().union(*(spec.requires for spec in self.passes))
        missing = sorted(required - builders.keys(), key=lambda item: item.value)
        if missing:
            names = ", ".join(fact.value for fact in missing)
            raise PipelineContractError(f"no builder registered for facts: {names}")
        self._fact_builders = read_only_mapping(builders)

    def _ensure_facts(
        self,
        snapshot: DocumentSnapshot,
        required: frozenset[Fact],
    ) -> tuple[DocumentSnapshot, frozenset[Fact]]:
        rebuilt: set[Fact] = set()
        for fact in sorted(
            required - snapshot.current_facts,
            key=lambda item: (
                0 if item is Fact.SYNTAX else 1,
                item.value,
            ),
        ):
            builder = self._fact_builders.get(fact)
            if builder is None:
                raise PipelineContractError(
                    f"no builder registered for fact: {fact.value}"
                )
            snapshot = builder(snapshot)
            rebuilt.add(fact)
        return snapshot, frozenset(rebuilt)

    def run(self, snapshot: DocumentSnapshot) -> PipelineResult:
        executions: list[PassExecution] = []
        diagnostics: list[Diagnostic] = []
        for spec in self.passes:
            executed = _execute_pass(
                snapshot, spec, self._fact_builders, self.fallback_enabled,
            )
            if executed is None:
                continue
            snapshot, execution, pass_diagnostics = executed
            executions.append(execution)
            diagnostics.extend(pass_diagnostics)

        return PipelineResult(snapshot, tuple(executions), tuple(diagnostics))


def _execute_pass(
    snapshot: DocumentSnapshot,
    spec: PassSpec,
    fact_builders: Mapping[Fact, FactBuilder],
    fallback_enabled: bool = False,
) -> tuple[DocumentSnapshot, PassExecution, tuple[Diagnostic, ...]] | None:
    """Execute one pass; shared by partial pipelines and the complete plan."""
    if spec.safety is Safety.FALLBACK_ONLY and not fallback_enabled:
        return None
    rebuilt: set[Fact] = set()
    for fact in sorted(
        spec.requires - snapshot.current_facts,
        key=lambda item: (0 if item is Fact.SYNTAX else 1, item.value),
    ):
        builder = fact_builders.get(fact)
        if builder is None:
            raise PipelineContractError(
                f"no builder registered for fact: {fact.value}"
            )
        snapshot = builder(snapshot)
        rebuilt.add(fact)
    revision_before = snapshot.revision
    diagnostics: list[Diagnostic] = []
    if spec.safety is Safety.FALLBACK_ONLY:
        diagnostics.append(Diagnostic(
            snapshot.main_tex, spec.name, "fallback-executed",
            f"executed fallback pass {spec.name} for {snapshot.main_tex}",
        ))
    outcome = _coerce_plan_outcome(spec.planner(snapshot))
    diagnostics.extend(outcome.diagnostics)
    if outcome.edits and Fact.SYNTAX not in spec.invalidates:
        raise PipelineContractError(
            f"pass {spec.name} edits source but does not invalidate syntax"
        )
    grouped: dict[Path, list[Edit]] = {}
    for edit in outcome.edits:
        if _SAFETY_RANK[edit.safety] > _SAFETY_RANK[spec.safety]:
            raise PipelineContractError(
                f"edit safety {edit.safety.name} exceeds pass "
                f"{spec.name} declaration {spec.safety.name}"
            )
        if edit.file not in snapshot.sources:
            raise PipelineContractError(
                f"pass {spec.name} edits unknown source: {edit.file}"
            )
        grouped.setdefault(edit.file, []).append(edit)
    changed: dict[Path, SourceFile] = {}
    for path, edits in grouped.items():
        content = EditPlanner.apply(snapshot.sources[path], edits)
        if content != snapshot.sources[path].content:
            changed[path] = SourceFile(path, content)
    if changed:
        sources = dict(snapshot.sources)
        sources.update(changed)
        documents = snapshot.documents
        if Fact.SYNTAX in spec.invalidates:
            documents = read_only_mapping({})
        snapshot = replace(
            snapshot, revision=snapshot.revision + 1,
            sources=read_only_mapping(sources), documents=documents,
            current_facts=snapshot.current_facts - spec.invalidates,
        )
    execution = PassExecution(
        spec.name, revision_before, snapshot.revision,
        tuple(sorted(changed)), frozenset(rebuilt),
    )
    return snapshot, execution, tuple(diagnostics)


FileEditPlanner = Callable[
    [SourceFile, LatexDocument],
    "PlanOutcome | list[Edit]",
]
TextTransform = Callable[[str], str]


def make_syntax_file_pass(
    *,
    name: str,
    planner: FileEditPlanner,
    safety: Safety,
    phase: Phase = Phase.SAFE_NORMALIZATION,
    main_only: bool = False,
    after: frozenset[str] = frozenset(),
    idempotent: bool = False,
    report_label: str | None = None,
) -> PassSpec:
    def plan(snapshot: DocumentSnapshot) -> PlanOutcome:
        paths = (snapshot.main_tex,) if main_only else tuple(snapshot.sources)
        edits: list[Edit] = []
        diagnostics: list[Diagnostic] = []
        for path in paths:
            outcome = _coerce_plan_outcome(
                planner(snapshot.sources[path], snapshot.documents[path])
            )
            edits.extend(outcome.edits)
            diagnostics.extend(outcome.diagnostics)
        return PlanOutcome(tuple(edits), tuple(diagnostics))

    return PassSpec(
        name=name,
        planner=plan,
        phase=phase,
        safety=safety,
        requires=frozenset({Fact.SYNTAX, Fact.DISCOVERY}),
        invalidates=frozenset(set(Fact) - {Fact.DISCOVERY}),
        after=after,
        implementation=Implementation.SYNTAX_AWARE,
        idempotent=idempotent,
        report_label=report_label,
    )


def rebuild_syntax(snapshot: DocumentSnapshot) -> DocumentSnapshot:
    documents = {
        path: LatexDocument(source)
        for path, source in snapshot.sources.items()
    }
    return replace(
        snapshot,
        documents=read_only_mapping(documents),
        current_facts=snapshot.current_facts | {Fact.SYNTAX},
    )


class EditConflictError(ValueError):
    pass


class EditPlanner:
    @staticmethod
    def validate(source: SourceFile, edits: Iterable[Edit]) -> list[Edit]:
        ordered = sorted(edits, key=lambda edit: (edit.start, edit.end))
        previous: Edit | None = None
        for edit in ordered:
            if edit.file != source.path:
                raise ValueError(
                    f"edit for different source file: "
                    f"{edit.file} != {source.path}"
                )
            if (
                edit.start < 0
                or edit.end < edit.start
                or edit.end > len(source.content)
            ):
                raise ValueError(
                    f"invalid edit range {edit.start}:{edit.end} "
                    f"for {source.path}"
                )
            if previous is not None and (
                edit.start < previous.end
                or (
                    edit.start == edit.end
                    and previous.start == previous.end
                    and edit.start == previous.start
                )
            ):
                raise EditConflictError(
                    f"overlap between {previous.pass_name} "
                    f"and {edit.pass_name} in {source.path}"
                )
            previous = edit
        return ordered

    @classmethod
    def apply(cls, source: SourceFile, edits: Iterable[Edit]) -> str:
        ordered = cls.validate(source, edits)
        content = source.content
        for edit in reversed(ordered):
            content = (
                content[:edit.start]
                + edit.replacement
                + content[edit.end:]
            )
        return content


@dataclass(frozen=True)
class PlanOutcome:
    edits: tuple[Edit, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()


def _coerce_plan_outcome(
    result: PlanOutcome | list[Edit],
) -> PlanOutcome:
    if isinstance(result, PlanOutcome):
        return result
    return PlanOutcome(edits=tuple(result))


def _untrusted(ref: LatexNodeRef) -> bool:
    return not ref.complete or ref.opaque


def _opaque_structure_outcome(
    source: SourceFile,
    ref: LatexNodeRef,
    pass_name: str,
    message: str | None = None,
) -> PlanOutcome:
    return PlanOutcome(diagnostics=(Diagnostic(
        source.path,
        pass_name,
        "opaque-structure",
        message or f"preserved incomplete {ref.kind} {ref.name}",
        ref.start,
        ref.end,
    ),))


def _reference_issue(
    source: SourceFile,
    ref: LatexNodeRef,
    pass_name: str,
) -> PlanOutcome | None:
    if ref.file != source.path:
        return PlanOutcome(diagnostics=(Diagnostic(
            source.path,
            pass_name,
            "foreign-reference",
            f"preserved reference owned by {ref.file}",
            ref.start,
            ref.end,
        ),))
    if _untrusted(ref):
        return _opaque_structure_outcome(source, ref, pass_name)
    return None


def plan_remove_node(
    source: SourceFile,
    ref: LatexNodeRef,
    pass_name: str,
    safety: Safety,
) -> PlanOutcome:
    issue = _reference_issue(source, ref, pass_name)
    if issue is not None:
        return issue
    return PlanOutcome(edits=(Edit(
        source.path,
        ref.start,
        ref.end,
        "",
        pass_name,
        safety,
    ),))


def plan_unwrap_command(
    source: SourceFile,
    ref: LatexNodeRef,
    keep_argument: int,
    pass_name: str,
    safety: Safety,
) -> PlanOutcome:
    issue = _reference_issue(source, ref, pass_name)
    if issue is not None:
        return issue
    argument = _public_argument(ref, keep_argument)
    if argument is None or not argument.complete or argument.opaque:
        return _opaque_structure_outcome(
            source,
            ref,
            pass_name,
            f"preserved incomplete argument {keep_argument} of {ref.name}",
        )
    return PlanOutcome(edits=(Edit(
        source.path,
        ref.start,
        ref.end,
        argument.text,
        pass_name,
        safety,
    ),))


def plan_rename_environment(
    source: SourceFile,
    ref: LatexNodeRef,
    new_name: str,
    pass_name: str,
    safety: Safety,
) -> PlanOutcome:
    issue = _reference_issue(source, ref, pass_name)
    if issue is not None:
        return issue
    if new_name == ref.name:
        return PlanOutcome()
    begin_name_start = ref.start + len(r"\begin{")
    end_name_start = ref.end_token_start + len(r"\end{")
    return PlanOutcome(edits=(
        Edit(
            source.path,
            begin_name_start,
            begin_name_start + len(ref.name),
            new_name,
            pass_name,
            safety,
        ),
        Edit(
            source.path,
            end_name_start,
            end_name_start + len(ref.name),
            new_name,
            pass_name,
            safety,
        ),
    ))


def plan_unwrap_environment(
    source: SourceFile,
    ref: LatexNodeRef,
    pass_name: str,
    safety: Safety,
) -> PlanOutcome:
    issue = _reference_issue(source, ref, pass_name)
    if issue is not None:
        return issue
    return PlanOutcome(edits=(
        Edit(
            source.path,
            ref.start,
            ref.body_start,
            "",
            pass_name,
            safety,
        ),
        Edit(
            source.path,
            ref.body_end,
            ref.end,
            "",
            pass_name,
            safety,
        ),
    ))


def plan_transform_argument(
    source: SourceFile,
    ref: LatexNodeRef,
    argument_index: int,
    transform: Callable[[str], str],
    pass_name: str,
    safety: Safety,
) -> PlanOutcome:
    issue = _reference_issue(source, ref, pass_name)
    if issue is not None:
        return issue
    argument = _public_argument(ref, argument_index)
    if argument is None or not argument.complete or argument.opaque:
        return _opaque_structure_outcome(
            source,
            ref,
            pass_name,
            f"preserved incomplete argument {argument_index} of {ref.name}",
        )
    replacement = transform(argument.text)
    if replacement == argument.text:
        return PlanOutcome()
    if transform(replacement) != replacement:
        return PlanOutcome(diagnostics=(Diagnostic(
            source.path,
            pass_name,
            "non-idempotent-transform",
            f"preserved argument {argument_index} of {ref.name}",
            argument.start,
            argument.end,
        ),))
    content_start = argument.start + len(argument.opening_delimiter or "")
    content_end = argument.end - len(argument.closing_delimiter or "")
    return PlanOutcome(edits=(Edit(
        source.path,
        content_start,
        content_end,
        replacement,
        pass_name,
        safety,
    ),))


_ALGO_CMD_NAMES = (
    "Require", "Ensure", "State", "For", "EndFor", "ForAll",
    "If", "ElsIf", "Else", "EndIf",
    "While", "EndWhile", "Repeat", "Until",
    "Loop", "EndLoop", "Return", "Print",
)
_ALGO_CANONICAL = {name.lower(): name for name in _ALGO_CMD_NAMES}
NEEDS_BRACE_ARG = {"For", "ForAll", "If", "ElsIf", "While", "Until"}
for _algorithm_command in _ALGO_CMD_NAMES:
    _PARSER_MACRO_ARGS[_algorithm_command] = (
        "{" if _algorithm_command in NEEDS_BRACE_ARG else ""
    )
INDENT_OPEN = {"For", "ForAll", "If", "While", "Loop", "Repeat"}
INDENT_CLOSE_BEFORE = {
    "EndFor",
    "EndIf",
    "EndWhile",
    "EndLoop",
    "Else",
    "ElsIf",
    "Until",
}
INDENT_OPEN_AFTER = {"Else", "ElsIf"}


# ---------------------------------------------------------------------------
# Title extraction
# ---------------------------------------------------------------------------


def _macro_invocation_argspec(definition: MacroDefinition) -> str:
    if definition.optional_default is None:
        return "{" * definition.parameter_count
    return "[" + "{" * (definition.parameter_count - 1)


_TITLE_DROP_COMMANDS = frozenset({
    "blfootnote",
    "footnote",
    "footnotetext",
    "thanks",
    "Huge",
    "huge",
    "LARGE",
    "Large",
    "large",
    "normalsize",
    "small",
    "footnotesize",
    "scriptsize",
    "tiny",
    "bigskip",
    "medskip",
    "smallskip",
    "vspace",
    "hspace",
})
_TITLE_DROP_ARGUMENTS = MappingProxyType({
    "blfootnote": "{",
    "footnote": "{",
    "footnotetext": "{",
    "thanks": "{",
})


def _strip_title_display_commands(
    text: str,
    macros: Mapping[str, MacroDefinition],
) -> str:
    argspecs = {
        name: _macro_invocation_argspec(definition)
        for name, definition in macros.items()
    }
    argspecs.update(_TITLE_DROP_ARGUMENTS)
    source = SourceFile(Path("<title-display>"), text)
    document = LatexDocument(source, macro_args=argspecs)
    edits = [
        Edit(
            source.path,
            ref.start,
            ref.end,
            "",
            "strip_title_display_commands",
            Safety.SAFE,
        )
        for name in _TITLE_DROP_COMMANDS
        for ref in document.commands(name)
        if ref.complete and not ref.opaque
    ]
    return EditPlanner.apply(source, edits)


def _macro_replacement(
    document: LatexDocument,
    ref: LatexNodeRef,
    definition: MacroDefinition,
) -> str | None:
    values: list[str] = []
    for index in range(definition.parameter_count):
        argument = document.argument(ref, index)
        if (
            index == 0
            and definition.optional_default is not None
            and argument is None
        ):
            values.append(definition.optional_default)
            continue
        if argument is None or not argument.complete or argument.opaque:
            return None
        values.append(argument.text)

    sentinel = "\0paper2epub-hash\0"
    body = definition.body.replace("##", sentinel)

    def replace_parameter(match: re.Match[str]) -> str:
        index = int(match.group(1)) - 1
        if index < 0 or index >= len(values):
            raise IndexError
        return values[index]

    try:
        body = re.sub(r"#([1-9])", replace_parameter, body)
    except IndexError:
        return None
    body = body.replace(sentinel, "#")
    return f"\\operatorname{{{body}}}" if definition.math_operator else body


def expand_macros(
    text: str,
    macros: Mapping[str, MacroDefinition],
    depth: int = 5,
) -> str:
    argspecs = {
        name: _macro_invocation_argspec(definition)
        for name, definition in macros.items()
    }
    source = SourceFile(Path("<metadata>"), text)
    for _ in range(depth):
        document = LatexDocument(source, macro_args=argspecs)
        edits: list[Edit] = []
        candidates = [
            ref
            for name in macros
            for ref in document.commands(name)
            if ref.complete and not ref.opaque
        ]
        for ref in _outermost_refs(candidates):
            replacement = _macro_replacement(
                document, ref, macros[ref.name],
            )
            if replacement is None:
                continue
            edits.append(Edit(
                source.path,
                ref.start,
                ref.end,
                replacement,
                "expand_metadata_macros",
                Safety.SAFE,
            ))
        expanded = EditPlanner.apply(source, edits)
        if expanded == source.content:
            break
        source = SourceFile(source.path, expanded)
    return source.content


def _format_title(raw: str, macros: Mapping[str, MacroDefinition]) -> str:
    # Lexical display cleanup over an already parsed title argument.
    raw = _strip_title_display_commands(raw, macros)
    raw = re.sub(r"\\\\", " ", raw)
    raw = expand_macros(raw, dict(macros))
    raw = _strip_title_display_commands(raw, macros)
    for _ in range(5):
        unwrapped = re.sub(r"\\[a-zA-Z]+\{([^{}]*)\}", r"\1", raw)
        if unwrapped == raw:
            break
        raw = unwrapped
    raw = re.sub(r"[{}]", "", raw)
    return re.sub(r"\s+", " ", raw).strip()


def _format_authors(
    raw: str,
    macros: Mapping[str, MacroDefinition],
) -> tuple[str, ...]:
    # Lexical display cleanup over an already parsed author argument.
    raw = expand_macros(raw, dict(macros))
    # Take only the first line (before \\) — the rest are affiliations.
    first_line = re.split(r"\\\\", raw)[0]
    first_line = re.sub(r"\$\^?\{[^}]*\}\$", "", first_line)
    first_line = re.sub(r"\$\^\{[^}]*\}\$", "", first_line)
    first_line = re.sub(r"\\[a-zA-Z]+\{[^}]*\}", "", first_line)
    first_line = re.sub(r"\\[a-zA-Z]+\b", "", first_line)
    first_line = re.sub(r"\\;", "", first_line)
    first_line = re.sub(r"[{}$]", "", first_line)
    authors = []
    for name in re.split(r"\s*,\s*", first_line):
        name = re.sub(r"\s+", " ", name).strip()
        if name and re.search(r"[a-zA-Z]", name):
            authors.append(name)
    return tuple(authors)


def _complete_argument_text(
    document: LatexDocument,
    ref: LatexNodeRef,
    index: int,
) -> str | None:
    argument = document.argument(ref, index)
    if (
        not ref.complete
        or ref.opaque
        or argument is None
        or not argument.complete
        or argument.opaque
    ):
        return None
    return argument.text


def _parse_macro_definition(
    document: LatexDocument,
    ref: LatexNodeRef,
    *,
    math_operator: bool,
) -> tuple[str, MacroDefinition] | None:
    raw_name = _complete_argument_text(document, ref, 1)
    body = _complete_argument_text(document, ref, 2 if math_operator else 4)
    if raw_name is None or body is None:
        return None
    match = re.fullmatch(r"\\([A-Za-z@]+)", raw_name.strip())
    if match is None:
        return None
    body = re.sub(r"\\xspace\b", "", body).strip()
    if math_operator:
        return match.group(1), MacroDefinition(body, math_operator=True)

    raw_count = _complete_argument_text(document, ref, 2)
    default = _complete_argument_text(document, ref, 3)
    if raw_count is None:
        count = 0
    elif not re.fullmatch(r"[0-9]", raw_count.strip()):
        return None
    else:
        count = int(raw_count.strip())
    if default is not None and count == 0:
        return None
    return match.group(1), MacroDefinition(
        body=body,
        parameter_count=count,
        optional_default=default,
    )


def _resolve_snapshot_include(
    snapshot: DocumentSnapshot,
    current_tex: Path,
    name: str,
) -> Path | None:
    candidates = (name,) if name.endswith(".tex") else (name, f"{name}.tex")
    by_resolved_path = {path.resolve(): path for path in snapshot.sources}
    for candidate in candidates:
        resolved = (current_tex.parent / candidate).resolve()
        if resolved in by_resolved_path:
            return by_resolved_path[resolved]
    return None


def _snapshot_include_order(snapshot: DocumentSnapshot) -> tuple[Path, ...]:
    visited: set[Path] = set()
    ordered: list[Path] = []

    def walk(path: Path) -> None:
        resolved = path.resolve()
        if resolved in visited or path not in snapshot.documents:
            return
        visited.add(resolved)
        ordered.append(path)
        document = snapshot.documents[path]
        refs = sorted(
            document.commands("input") + document.commands("include"),
            key=lambda ref: ref.start,
        )
        for ref in refs:
            name = _complete_argument_text(document, ref, 0)
            if name is None:
                continue
            child = _resolve_snapshot_include(snapshot, path, name.strip())
            if child is not None:
                walk(child)

    walk(snapshot.main_tex)
    return tuple(ordered)


def _iter_selected_graphicspaths(argument: str) -> Iterable[str]:
    pos = 0
    while pos < len(argument):
        path, end = extract_brace_arg(argument, pos)
        if path is None:
            pos += 1
            continue
        pos = end
        path = path.strip().rstrip("/")
        if path:
            yield path


def build_discovery_facts(snapshot: DocumentSnapshot) -> DocumentSnapshot:
    if Fact.SYNTAX not in snapshot.current_facts:
        raise PipelineContractError("discovery requires syntax facts")

    include_order = _snapshot_include_order(snapshot)
    remaining = tuple(path for path in snapshot.sources if path not in include_order)
    paths = include_order + remaining
    macros: dict[str, MacroDefinition] = {}
    packages: list[str] = []
    graphicspaths: list[str] = []
    resource_refs: list[tuple[Path, str]] = []
    labels: dict[str, tuple[Path, int]] = {}
    theorem_labels: dict[str, str] = {}

    macro_signatures = {
        "newcommand": False,
        "renewcommand": False,
        "providecommand": False,
        "DeclareRobustCommand": False,
        "DeclareMathOperator": True,
    }
    for path in paths:
        document = snapshot.documents[path]
        macro_declarations = sorted(
            (
                (ref, math_operator)
                for command, math_operator in macro_signatures.items()
                for ref in document.commands(command)
            ),
            key=lambda declaration: declaration[0].start,
        )
        for ref, math_operator in macro_declarations:
            parsed = _parse_macro_definition(
                document,
                ref,
                math_operator=math_operator,
            )
            if parsed is None:
                continue
            name, definition = parsed
            if name in macros:
                continue
            macros[name] = definition

        for command in ("usepackage", "RequirePackage"):
            for ref in document.commands(command):
                raw_packages = _complete_argument_text(document, ref, 1)
                if raw_packages is None:
                    continue
                for package in raw_packages.split(","):
                    package = package.strip()
                    if package and package not in packages:
                        packages.append(package)

        for ref in document.commands("graphicspath"):
            argument = _complete_argument_text(document, ref, 0)
            if argument is None:
                continue
            for directory in _iter_selected_graphicspaths(argument):
                if directory not in graphicspaths:
                    graphicspaths.append(directory)

        for ref in document.commands("includegraphics"):
            resource = _complete_argument_text(document, ref, 1)
            if resource is not None:
                resource_refs.append((path, resource.strip()))

        for ref in document.commands("label"):
            label = _complete_argument_text(document, ref, 0)
            if label is not None and label.strip() not in labels:
                labels[label.strip()] = (path, ref.start)

        for ref in document.commands("newtheorem"):
            environment = _complete_argument_text(document, ref, 1)
            label = _complete_argument_text(document, ref, 3)
            if environment is not None and label is not None:
                theorem_labels.setdefault(environment.strip(), label.strip())

    main_document = snapshot.documents[snapshot.main_tex]
    title = None
    for ref in main_document.commands("title"):
        raw_title = _complete_argument_text(main_document, ref, 1)
        if raw_title is not None:
            title = _format_title(raw_title, macros)
            break
    authors: tuple[str, ...] = ()
    for ref in main_document.commands("author"):
        raw_authors = _complete_argument_text(main_document, ref, 0)
        if raw_authors is not None:
            authors = _format_authors(raw_authors, macros)
            break
    abstract = ""
    for ref in main_document.environments("abstract"):
        if ref.complete and not ref.opaque:
            abstract = snapshot.sources[snapshot.main_tex].content[
                ref.body_start:ref.body_end
            ].strip()
            break

    discovery = DiscoveryFacts(
        title=title,
        authors=authors,
        abstract=abstract,
        macros=read_only_mapping(macros),
        packages=tuple(packages),
        include_order=include_order,
        graphicspaths=tuple(graphicspaths),
        resource_refs=tuple(resource_refs),
        labels=read_only_mapping(labels),
        theorem_labels=read_only_mapping(theorem_labels),
    )
    return replace(
        snapshot,
        discovery=discovery,
        current_facts=snapshot.current_facts | {Fact.DISCOVERY},
    )


# ---------------------------------------------------------------------------
# Algorithm preprocessing
# ---------------------------------------------------------------------------


def find_matching_brace(text: str, start: int) -> int | None:
    if start >= len(text) or text[start] != "{":
        return None
    depth = 0
    i = start
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def extract_brace_arg(text, pos):
    while pos < len(text) and text[pos] in " \t\n\r":
        pos += 1
    if pos >= len(text) or text[pos] != "{":
        return None, pos
    end = find_matching_brace(text, pos)
    if end is None:
        return None, pos
    return text[pos + 1 : end], end + 1


def find_matching_bracket(text: str, start: int) -> int | None:
    if start >= len(text) or text[start] != "[":
        return None
    depth = 0
    i = start
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == "[":
            depth += 1
        elif text[i] == "]":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def plan_replace_calls(
    source: SourceFile,
    document: LatexDocument,
) -> PlanOutcome:
    outcomes: list[PlanOutcome] = []
    trusted: list[LatexNodeRef] = []
    for ref in document.commands("Call"):
        issue = _reference_issue(source, ref, "replace_call")
        if issue is not None:
            outcomes.append(issue)
            continue
        name = document.argument(ref, 0)
        arguments = document.argument(ref, 1)
        if any(
            argument is None or not argument.complete or argument.opaque
            for argument in (name, arguments)
        ):
            outcomes.append(_opaque_structure_outcome(
                source, ref, "replace_call",
                "preserved incomplete Call arguments",
            ))
            continue
        trusted.append(ref)

    for ref in _outermost_refs(trusted):
        name = document.argument(ref, 0)
        arguments = document.argument(ref, 1)
        assert name is not None and arguments is not None
        replacement = (
            f"\\operatorname{{{replace_call_in_text(name.text)}}}"
            f"({replace_call_in_text(arguments.text)})"
        )
        outcomes.append(PlanOutcome(edits=(Edit(
            source.path,
            ref.start,
            ref.end,
            replacement,
            "replace_call",
            Safety.LOSSY,
        ),)))
    return _combine_outcomes(outcomes)


def replace_call_in_text(text: str) -> str:
    source = SourceFile(Path("<call-fragment>"), text)
    outcome = plan_replace_calls(source, LatexDocument(source))
    return EditPlanner.apply(source, outcome.edits)


def format_command(cmd, arg, extra):
    if cmd == "Require":
        return f"\\textbf{{Require:}} {extra}"
    elif cmd == "Ensure":
        return f"\\textbf{{Ensure:}} {extra}"
    elif cmd == "State":
        return extra
    elif cmd == "Return":
        return f"\\textbf{{return}} {extra}"
    elif cmd == "Print":
        return f"\\textbf{{print}} {extra}"
    elif cmd in ("For", "ForAll"):
        kw = "for all" if cmd == "ForAll" else "for"
        return f"\\textbf{{{kw}}} {arg} \\textbf{{do}}"
    elif cmd == "EndFor":
        return "\\textbf{end for}"
    elif cmd == "If":
        return f"\\textbf{{if}} {arg} \\textbf{{then}}"
    elif cmd == "ElsIf":
        return f"\\textbf{{else if}} {arg} \\textbf{{then}}"
    elif cmd == "Else":
        return "\\textbf{else}"
    elif cmd == "EndIf":
        return "\\textbf{end if}"
    elif cmd == "While":
        return f"\\textbf{{while}} {arg} \\textbf{{do}}"
    elif cmd == "EndWhile":
        return "\\textbf{end while}"
    elif cmd == "Repeat":
        return "\\textbf{repeat}"
    elif cmd == "Until":
        return f"\\textbf{{until}} {arg}"
    elif cmd == "Loop":
        return "\\textbf{loop}"
    elif cmd == "EndLoop":
        return "\\textbf{end loop}"
    return extra


def _parse_algorithmic_ref(
    source: SourceFile,
    document: LatexDocument,
    algorithmic: LatexNodeRef,
) -> list[tuple[int, int, str]]:
    commands = sorted(
        (
            ref
            for name in _ALGO_CMD_NAMES
            for ref in document.commands(name)
            if algorithmic.body_start <= ref.start < algorithmic.body_end
        ),
        key=lambda ref: ref.start,
    )
    lines: list[tuple[int, int, str]] = []
    indent = 0
    line_num = 1
    for index, ref in enumerate(commands):
        command = _ALGO_CANONICAL[ref.name.lower()]
        rest_end = (
            commands[index + 1].start
            if index + 1 < len(commands)
            else algorithmic.body_end
        )
        argument = document.argument(ref, 0) if command in NEEDS_BRACE_ARG else None
        argument_text = argument.text if argument is not None else None
        extra_start = argument.end if argument is not None else ref.command_token_end
        extra = source.content[extra_start:rest_end].strip()

        if command in INDENT_CLOSE_BEFORE:
            indent = max(0, indent - 1)
        if command == "State" and not extra:
            continue

        text = format_command(command, argument_text, extra)
        lines.append((line_num, indent, text))
        line_num += 1
        if command in INDENT_OPEN or command in INDENT_OPEN_AFTER:
            indent += 1
    return lines


def parse_algorithmic(content):
    wrapped = f"\\begin{{algorithmic}}{content}\\end{{algorithmic}}"
    source = SourceFile(Path("<memory>"), wrapped)
    document = LatexDocument(source)
    refs = document.environments("algorithmic")
    if not refs or _untrusted(refs[0]):
        return []
    return _parse_algorithmic_ref(source, document, refs[0])


def build_algorithm_output(
    caption, label, lines, algorithm_number, newline: str = "\n",
):
    indent_unit = "~~~~"
    parts = []
    parts.append("\\begin{algorithmdisplay}\n")

    if caption:
        caption = replace_call_in_text(caption)
        if label:
            parts.append(f"\\hypertarget{{{label}}}{{}}%")
        parts.append(f"\\textbf{{Algorithm {algorithm_number}}} {caption}\n")

    rule = "\\begin{center}\\rule{0.8\\textwidth}{0.4pt}\\end{center}"
    parts.append(rule + "\n")

    for line_num, indent, text in lines:
        text = replace_call_in_text(text)
        indent_str = indent_unit * indent
        parts.append(f"{line_num}:{indent_str} {text}\n")

    parts.append(rule + "\n")
    parts.append("\\end{algorithmdisplay}\n")
    return _normalize_newlines("\n".join(parts), newline)


def _contained_refs(
    refs: Iterable[LatexNodeRef],
    outer: LatexNodeRef,
) -> list[LatexNodeRef]:
    return sorted(
        (
            ref for ref in refs
            if outer.body_start <= ref.start and ref.end <= outer.body_end
        ),
        key=lambda ref: ref.start,
    )


def _required_child_argument_issue(
    source: SourceFile,
    document: LatexDocument,
    ref: LatexNodeRef,
    pass_name: str,
    argument_indexes: tuple[int, ...] = (0,),
) -> PlanOutcome | None:
    for argument_index in argument_indexes:
        argument = document.argument(ref, argument_index)
        if argument is None or not argument.complete or argument.opaque:
            return _opaque_structure_outcome(
                source,
                ref,
                pass_name,
                f"preserved incomplete required argument of {ref.name}",
            )
    return None


def plan_algorithms(snapshot: DocumentSnapshot) -> PlanOutcome:
    ordered_paths = (
        snapshot.discovery.include_order if snapshot.discovery is not None else ()
    ) + tuple(
        path
        for path in snapshot.sources
        if snapshot.discovery is None or path not in snapshot.discovery.include_order
    )
    outcomes: list[PlanOutcome] = []
    algorithm_number = 0
    for path in ordered_paths:
        source = snapshot.sources[path]
        document = snapshot.documents[path]
        candidates = sorted(
            (
                ref
                for name in ("algorithm", "algorithm*")
                for ref in document.environments(name)
            ),
            key=lambda ref: (ref.start, -ref.end),
        )
        selected: list[LatexNodeRef] = []
        for ref in candidates:
            if any(outer.start <= ref.start and ref.end <= outer.end for outer in selected):
                continue
            selected.append(ref)

        for ref in selected:
            issue = _reference_issue(source, ref, "preprocess_algorithms")
            if issue is not None:
                outcomes.append(issue)
                continue
            algorithmic_refs = _contained_refs(
                document.environments("algorithmic"), ref,
            )
            if not algorithmic_refs:
                outcomes.append(_opaque_structure_outcome(
                    source,
                    ref,
                    "preprocess_algorithms",
                    "preserved algorithm without a complete algorithmic environment",
                ))
                continue
            algorithmic = algorithmic_refs[0]
            algorithmic_issue = _reference_issue(
                source, algorithmic, "preprocess_algorithms",
            )
            if algorithmic_issue is not None:
                outcomes.append(algorithmic_issue)
                continue

            captions = _contained_refs(document.commands("caption"), ref)
            labels = _contained_refs(document.commands("label"), ref)
            calls = _contained_refs(document.commands("Call"), ref)
            commands = sorted(
                (
                    command
                    for name in _ALGO_CMD_NAMES
                    for command in document.commands(name)
                    if (
                        algorithmic.body_start
                        <= command.start
                        < algorithmic.body_end
                    )
                ),
                key=lambda command: command.start,
            )
            child_issue = None
            for child in (*captions, *labels, *commands, *calls):
                child_issue = _reference_issue(
                    source, child, "preprocess_algorithms",
                )
                if child_issue is not None:
                    break
                if child.name in {"caption", "label", *NEEDS_BRACE_ARG, "Call"}:
                    child_issue = _required_child_argument_issue(
                        source,
                        document,
                        child,
                        "preprocess_algorithms",
                        (0, 1) if child.name == "Call" else (0,),
                    )
                    if child_issue is not None:
                        break
            if child_issue is not None:
                outcomes.append(child_issue)
                continue

            caption = document.argument_text(captions[0], 0) if captions else None
            label = document.argument_text(labels[0], 0) if labels else None
            algorithm_number += 1
            replacement = build_algorithm_output(
                caption,
                label,
                _parse_algorithmic_ref(source, document, algorithmic),
                algorithm_number,
                source.newline,
            )
            outcomes.append(PlanOutcome(edits=(Edit(
                source.path,
                ref.start,
                ref.end,
                replacement,
                "preprocess_algorithms",
                Safety.LOSSY,
            ),)))
    return _combine_outcomes(outcomes)


# ---------------------------------------------------------------------------
# Translation (Qwen3.6-Flash via Bailian)
# ---------------------------------------------------------------------------

DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
TRANSLATE_MODEL = "qwen3.6-flash"
TRANSLATION_BATCH_MAX_CHARS = 24_000
TRANSLATION_BATCH_WORKERS = 5
_HEADING_TRANSLATION_MARKER = "% paper2epub:heading-translation"
_TRANSLATION_BEGIN_PREFIX = "% paper2epub:translation-begin:"
_TRANSLATION_END_PREFIX = "% paper2epub:translation-end:"
_TRANSLATION_BEGIN_RE = re.compile(
    r"(?m)^% paper2epub:translation-begin:([0-9a-f]{64})(?=\r?$)"
)

SKIP_ENV_NAMES = {
    "figure",
    "figure*",
    "table",
    "table*",
    "equation",
    "equation*",
    "align",
    "align*",
    "gather",
    "gather*",
    "multline",
    "multline*",
    "eqnarray",
    "eqnarray*",
    "algorithmdisplay",
    "algorithm",
    "algorithm*",
    "algorithmic",
    "lstlisting",
    "verbatim",
    "minted",
    "listing",
    "thebibliography",
    "tabular",
    "tabular*",
    "tabularx",
    "longtable",
    "wrapfigure",
    "subfigure",
    "tabu",
    "tabulary",
    "tblr",
    "xltabular",
    "ltablex",
    "NiceTabular",
    "NiceTabular*",
    "adjustbox",
    "tikzpicture",
}

TRANSLATABLE_HEADING_NAMES = (
    "chapter",
    "section",
    "subsection",
    "subsubsection",
    "paragraph",
)

_TRANSLATION_STRIP_COMMAND_NAMES = (
    *TRANSLATABLE_HEADING_NAMES,
    "input",
    "include",
    "label",
    "item",
)


def create_openai_client():
    from openai import OpenAI

    api_key = os.environ.get("DASHSCOPE_API_KEY")
    if not api_key:
        print(
            "Error: DASHSCOPE_API_KEY environment variable is required for --translate",
            file=sys.stderr,
        )
        sys.exit(1)
    return OpenAI(api_key=api_key, base_url=DASHSCOPE_BASE_URL)


def _chat(client, system_prompt: str, user_prompt: str) -> str:
    resp = client.chat.completions.create(
        model=TRANSLATE_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )
    return resp.choices[0].message.content.strip()


def _parse_numbered_response(raw: str, postprocess=None) -> dict[int, str]:
    # Lexical parsing of the LLM's plain-text numbered response protocol.
    translations: dict[int, str] = {}
    segments = re.split(r"\[(\d+)\]\s*\n", raw)
    i = 1
    while i < len(segments) - 1:
        try:
            num = int(segments[i])
            text = segments[i + 1].strip()
            if postprocess:
                text = postprocess(text)
            if text:
                translations[num] = text
        except (ValueError, IndexError):
            pass
        i += 2
    return translations


def _extract_section_headings_from_documents(
    tex_files: Iterable[Path],
    documents: Mapping[Path, LatexDocument],
) -> list[str]:
    headings = []
    for tex in tex_files:
        document = documents[tex]
        skip_ranges = translation_skip_ranges(document)
        refs = sorted(
            (
                ref
                for name in TRANSLATABLE_HEADING_NAMES
                for ref in document.commands(name)
            ),
            key=lambda ref: ref.start,
        )
        for ref in refs:
            if not ref.complete or ref.opaque:
                continue
            if _ref_is_within_ranges(ref, skip_ranges):
                continue
            title = document.argument_text(ref, 0)
            if title is not None:
                headings.append(title)
    return headings


def extract_snapshot_section_headings(snapshot: DocumentSnapshot) -> list[str]:
    if Fact.SYNTAX not in snapshot.current_facts:
        raise PipelineContractError("heading extraction requires syntax facts")
    paths = (
        snapshot.discovery.include_order
        if snapshot.discovery is not None
        else tuple(snapshot.sources)
    )
    return _extract_section_headings_from_documents(paths, snapshot.documents)


def extract_glossary(
    client, title: str | None, abstract: str, headings: list[str]
) -> str:
    system_prompt = (
        "你是一位专业的科技论文翻译专家。请根据以下论文信息，提取关键术语并给出中英对照翻译表。\n\n"
        "要求：\n"
        "1. 提取所有重要的技术术语、方法名、概念\n"
        "2. 专有名词（模型名、数据集名、人名）标注为'保留英文'\n"
        "3. 输出格式为每行一个：English term | 中文翻译\n"
        "4. 只输出术语表，不要其他内容\n"
        "5. 最多提取 50 个最重要的术语"
    )
    user_prompt = (
        f"标题：{title or '未知'}\n摘要：{abstract}\n章节标题：{', '.join(headings)}"
    )
    print("Extracting glossary ...")
    glossary = _chat(client, system_prompt, user_prompt)
    print(f"Glossary extracted ({glossary.count(chr(10)) + 1} terms)")
    return glossary


def _adjacent_complete_label_end(
    content: str,
    document: LatexDocument,
    ref: LatexNodeRef,
) -> int:
    for label in document.commands("label"):
        if label.start < ref.end:
            continue
        if strip_tex_comments(content[ref.end:label.start]).strip():
            break
        if not _untrusted(label):
            return label.end
        break
    return ref.end


def _strip_heading_lines(text: str) -> str:
    source = SourceFile(Path("<heading-strip>"), text)
    document = LatexDocument(source)
    refs = sorted(
        (
            ref
            for name in TRANSLATABLE_HEADING_NAMES
            for ref in document.commands(name)
        ),
        key=lambda ref: ref.start,
    )
    edits = []
    selected_end = -1
    for ref in refs:
        if not ref.complete or ref.opaque or ref.start < selected_end:
            continue
        end = _adjacent_complete_label_end(text, document, ref)
        edits.append(
            Edit(
                file=source.path,
                start=ref.start,
                end=end,
                replacement="",
                pass_name="strip_heading_lines",
                safety=Safety.SAFE,
            )
        )
        selected_end = end

    return EditPlanner.apply(source, edits).strip()


def _strip_translation_structure(text: str) -> str:
    """Remove non-prose commands changed by later compatibility passes."""
    source = SourceFile(Path("<translation-structure-strip>"), text)
    document = LatexDocument(source)
    edits = [
        Edit(
            source.path, ref.start, ref.end, "",
            "strip_translation_structure", Safety.SAFE,
        )
        for name in ("input", "include")
        for ref in document.commands(name)
        if not _untrusted(ref)
    ]
    return EditPlanner.apply(source, edits).strip()


def _is_prose(chunk: str) -> bool:
    source = SourceFile(Path("<prose-check>"), chunk)
    return LatexDocument(source).literal_alpha_count >= 20


def _chunk_ranges(content: str) -> list[tuple[int, int]]:
    # Lexical paragraph boundaries over the same source used for skip ranges.
    ranges: list[tuple[int, int]] = []
    start = 0
    for separator in re.finditer(
        r"(?:\r\n|\n)[^\S\r\n]*(?:\r\n|\n)", content,
    ):
        ranges.append((start, separator.start()))
        start = separator.end()
    ranges.append((start, len(content)))
    return ranges


def _translation_chunk_ranges(
    content: str,
    document: LatexDocument,
) -> list[tuple[int, int]]:
    math_ranges = [
        (ref.start, ref.end)
        for ref in document.math
        if ref.display or not ref.complete
    ]
    candidate_boundaries = {0, len(content)}
    for start, end in _chunk_ranges(content):
        candidate_boundaries.update((start, end))
    for start, end in _iter_environment_token_ranges(content):
        candidate_boundaries.update((start, end))
    candidate_boundaries.update(
        ref.start
        for ref in document.commands("item")
        if ref.complete and not ref.opaque
    )
    boundaries = {
        boundary
        for boundary in candidate_boundaries
        if not any(start < boundary < end for start, end in math_ranges)
    }
    for start, end in math_ranges:
        boundaries.update((start, end))
    ordered = sorted(boundaries)
    return list(zip(ordered, ordered[1:]))


@dataclass(frozen=True)
class _TranslationBlock:
    digest: str
    start: int
    end: int


def _translation_digest(chunk: str) -> str:
    # A single terminal line ending is a file boundary detail, not paragraph
    # identity. Once a generated block follows it, lexical chunking treats it
    # as part of the separating blank line.
    if chunk.endswith("\r\n"):
        source_text = chunk[:-2]
    elif chunk.endswith(("\r", "\n")):
        source_text = chunk[:-1]
    else:
        source_text = chunk
    return hashlib.sha256(source_text.encode("utf-8")).hexdigest()


def _complete_translation_blocks(content: str) -> list[_TranslationBlock]:
    """Return only closed, non-nested generated marker ranges."""
    blocks: list[_TranslationBlock] = []
    for begin in _TRANSLATION_BEGIN_RE.finditer(content):
        digest = begin.group(1)
        end_re = re.compile(
            rf"(?m)^{re.escape(_TRANSLATION_END_PREFIX + digest)}(?=\r?$)"
        )
        end = end_re.search(content, begin.end())
        if end is None:
            continue
        nested = _TRANSLATION_BEGIN_RE.search(content, begin.end())
        if nested is not None and nested.start() < end.start():
            continue
        blocks.append(_TranslationBlock(digest, begin.start(), end.end()))
    return blocks


def _strip_env_wrappers(text: str) -> str:
    source = SourceFile(Path("<environment-strip>"), text)
    document = LatexDocument(source)
    edits: list[Edit] = []
    for ref in document._refs:
        if ref.kind != "environment" or _untrusted(ref):
            continue
        edits.extend((
            Edit(
                source.path, ref.start, ref.body_start, "",
                "strip_environment_wrappers", Safety.SAFE,
            ),
            Edit(
                source.path, ref.end_token_start, ref.end, "",
                "strip_environment_wrappers", Safety.SAFE,
            ),
        ))
    return EditPlanner.apply(source, edits).strip()


def translation_skip_ranges(document: LatexDocument) -> list[tuple[int, int]]:
    refs = _outermost_refs(
        ref
        for name in SKIP_ENV_NAMES
        for ref in document.environments(name)
        if not _untrusted(ref)
    )
    ranges = [(ref.start, ref.end) for ref in refs]
    ranges.extend(
        (ref.start, ref.end)
        for ref in document.math
        if ref.display or not ref.complete
    )
    return list(_merge_ranges(ranges))


def _ref_is_within_ranges(
    ref: LatexNodeRef,
    ranges: Iterable[tuple[int, int]],
) -> bool:
    return any(start <= ref.start and ref.end <= end for start, end in ranges)


def _find_skip_ranges(content: str) -> list[tuple[int, int]]:
    source = SourceFile(Path("<skip-ranges>"), content)
    return translation_skip_ranges(LatexDocument(source))


def _chunk_in_skip_range(
    chunk_start: int, chunk_end: int, skip_ranges: list[tuple[int, int]]
) -> bool:
    for rs, re_ in skip_ranges:
        if chunk_start >= rs and chunk_end <= re_:
            return True
        if chunk_start < re_ and chunk_end > rs:
            return True
    return False


def _has_balanced_braces(text: str) -> bool:
    depth = 0
    for i, ch in enumerate(text):
        if ch in "{}":
            backslashes = 0
            j = i - 1
            while j >= 0 and text[j] == "\\":
                backslashes += 1
                j -= 1
            if backslashes % 2:
                continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


_MATH_PLACEHOLDER_RE = re.compile(r"⟦P2E_MATH_[^⟧\s]+⟧")


@dataclass(frozen=True)
class _ProtectedTranslationText:
    prompt_text: str
    replacements: tuple[tuple[str, str], ...]


def _protect_translation_math(text: str) -> _ProtectedTranslationText:
    source = SourceFile(Path("<translation-math-protection>"), text)
    document = LatexDocument(source)
    replacements: list[tuple[str, str]] = []
    edits: list[Edit] = []
    for index, ref in enumerate(document.math):
        if not ref.complete:
            continue
        math = text[ref.start:ref.end]
        digest = hashlib.sha256(math.encode("utf-8")).hexdigest()[:12]
        token = f"⟦P2E_MATH_{index}_{digest}⟧"
        replacements.append((token, math))
        edits.append(Edit(
            source.path,
            ref.start,
            ref.end,
            token,
            "protect_translation_math",
            Safety.SAFE,
        ))
    return _ProtectedTranslationText(
        prompt_text=EditPlanner.apply(source, edits),
        replacements=tuple(replacements),
    )


def _restore_translation_math(
    protected: _ProtectedTranslationText,
    candidate: str | None,
) -> str | None:
    if _translation_candidate_issue(protected, candidate) is not None:
        return None

    assert candidate is not None
    restored = candidate
    for token, math in protected.replacements:
        restored = restored.replace(token, math)
    return restored


def _translation_candidate_issue(
    protected: _ProtectedTranslationText,
    candidate: str | None,
) -> str | None:
    if not candidate:
        return "missing response"
    if not _has_balanced_braces(candidate):
        return "unbalanced braces"

    expected = Counter(token for token, _ in protected.replacements)
    actual = Counter(_MATH_PLACEHOLDER_RE.findall(candidate))
    if actual != expected:
        missing = sum((expected - actual).values())
        unexpected = sum((actual - expected).values())
        return (
            "math placeholder mismatch "
            f"(missing={missing}, unexpected={unexpected})"
        )

    candidate_source = SourceFile(Path("<translation-candidate>"), candidate)
    if LatexDocument(candidate_source).math:
        return "unexpected raw math"
    return None


def _request_numbered_translation(
    client, system_prompt: str, numbered_paragraphs: dict[int, str],
    request_limiter=None,
) -> dict[int, str]:
    user_prompt = "\n\n".join(
        f"[{idx}]\n{numbered_paragraphs[idx]}"
        for idx in sorted(numbered_paragraphs)
    )

    for attempt in range(3):
        try:
            if request_limiter is None:
                raw = _chat(client, system_prompt, user_prompt)
            else:
                with request_limiter:
                    raw = _chat(client, system_prompt, user_prompt)
            return _parse_numbered_response(raw)
        except Exception as e:
            if attempt < 2:
                import time

                time.sleep(2**attempt)
                print(f"  Retry {attempt + 1} ...", file=sys.stderr)
            else:
                raise RuntimeError("batch translation request failed") from e

    raise AssertionError("unreachable")


def _partition_translation_batches(
    numbered_paragraphs: Mapping[int, str],
    *,
    max_chars: int = TRANSLATION_BATCH_MAX_CHARS,
) -> tuple[dict[int, str], ...]:
    if max_chars <= 0:
        raise ValueError("translation batch limit must be positive")

    batches: list[dict[int, str]] = []
    current: dict[int, str] = {}
    current_chars = 0
    for index, paragraph in numbered_paragraphs.items():
        if current and current_chars + len(paragraph) > max_chars:
            batches.append(current)
            current = {}
            current_chars = 0
        current[index] = paragraph
        current_chars += len(paragraph)
    if current:
        batches.append(current)
    return tuple(batches)


def _translate_numbered_batch(
    client, system_prompt: str, numbered_paragraphs: dict[int, str],
    request_limiter=None,
) -> dict[int, str]:
    protected = {
        idx: _protect_translation_math(text)
        for idx, text in numbered_paragraphs.items()
    }
    prompt_paragraphs = {
        idx: item.prompt_text for idx, item in protected.items()
    }
    raw_translations = _request_numbered_translation(
        client, system_prompt, prompt_paragraphs, request_limiter,
    )
    translations = {
        idx: restored
        for idx, candidate in raw_translations.items()
        if idx in protected
        if (restored := _restore_translation_math(protected[idx], candidate))
        is not None
    }
    retry_paragraphs = {
        idx: prompt_paragraphs[idx]
        for idx in numbered_paragraphs
        if idx not in translations
    }
    retried: dict[int, str] = {}
    if retry_paragraphs:
        try:
            retried = _request_numbered_translation(
                client, system_prompt, retry_paragraphs, request_limiter,
            )
        except Exception as e:
            joined = ", ".join(str(idx) for idx in retry_paragraphs)
            print(
                f"translation: retry failed; skipped paragraph IDs: {joined} "
                f"({e})",
                file=sys.stderr,
            )
            return {
                idx: translations[idx]
                for idx in numbered_paragraphs
                if idx in translations
            }
        for idx in retry_paragraphs:
            candidate = retried.get(idx)
            restored = _restore_translation_math(protected[idx], candidate)
            if restored is not None:
                translations[idx] = restored

    invalid_ids = [
        idx
        for idx in numbered_paragraphs
        if idx not in translations
    ]
    if invalid_ids:
        details = ", ".join(
            f"{idx} ({_translation_candidate_issue(protected[idx], retried.get(idx))})"
            for idx in invalid_ids
        )
        print(
            f"translation: skipped invalid paragraph IDs: {details}",
            file=sys.stderr,
        )

    return {
        idx: translations[idx]
        for idx in numbered_paragraphs
        if idx in translations
    }


def _batch_translate(
    client, glossary: str, numbered_paragraphs: dict[int, str],
    request_limiter=None,
) -> dict[int, str]:
    if not numbered_paragraphs:
        return {}

    system_prompt = (
        "你是一位专业的科技论文翻译专家。请将以下编号的英文学术论文段落逐段翻译为中文。\n\n"
        f"术语对照表（请严格遵守）：\n{glossary}\n\n"
        "要求：\n"
        "1. 保持学术论文的专业性和准确性\n"
        "2. 严格按照术语对照表翻译术语，标注'保留英文'的术语保留英文原文\n"
        "3. 最高优先级约束：输入中所有形如 ⟦P2E_MATH_...⟧ 的字符串都是不可编辑的"
        "数学公式占位符，不是待翻译文本。必须将每个占位符完整、逐字复制到对应"
        "编号的译文中，并保持其所在语义位置；禁止删除、增添、翻译、改写、转义、"
        "拆分占位符，禁止修改其中任何字符或插入空格、换行。每个占位符必须在对应"
        "译文中出现且仅出现一次，且不得移动到其他编号的段落。输出前逐段核对占位符的"
        "内容和数量；即使无法翻译某段，也必须原样保留其中的全部占位符。\n"
        "4. 引用标记保持不变\n"
        "5. 译文流畅自然，符合中文科技论文的表达习惯\n"
        "6. 每段译文前标注对应编号，格式为 [编号]，然后换行输出译文\n"
        "7. 只输出翻译结果，不要添加任何解释"
    )
    batches = _partition_translation_batches(
        numbered_paragraphs,
        max_chars=TRANSLATION_BATCH_MAX_CHARS,
    )
    if len(batches) == 1:
        if request_limiter is None:
            return _translate_numbered_batch(client, system_prompt, batches[0])
        return _translate_numbered_batch(
            client, system_prompt, batches[0], request_limiter,
        )

    translations: dict[int, str] = {}
    with ThreadPoolExecutor(
        max_workers=min(TRANSLATION_BATCH_WORKERS, len(batches))
    ) as pool:
        futures = []
        for batch in batches:
            args = (client, system_prompt, batch)
            if request_limiter is not None:
                args += (request_limiter,)
            futures.append(pool.submit(_translate_numbered_batch, *args))
        try:
            for future in as_completed(futures):
                translations.update(future.result())
        except Exception:
            for future in futures:
                future.cancel()
            raise
    return {
        index: translations[index]
        for index in numbered_paragraphs
        if index in translations
    }


def _translate_heading_texts(client, glossary: str, texts: list[str]) -> dict[int, str]:
    if not texts:
        return {}
    system_prompt = (
        "你是一位专业的科技论文翻译专家。请将以下编号的英文章节标题翻译为中文。\n\n"
        f"术语对照表（请严格遵守）：\n{glossary}\n\n"
        "要求：\n"
        "1. 标注'保留英文'的术语保留英文原文\n"
        "2. 每个译文前标注对应编号，格式为 [编号]，然后换行输出译文\n"
        "3. 只输出纯文本翻译，不要添加任何LaTeX命令\n"
        "4. 只输出翻译结果，不要添加任何解释"
    )
    parts = [f"[{i}]\n{text}" for i, text in enumerate(texts)]
    user_prompt = "\n\n".join(parts)
    raw = _chat(client, system_prompt, user_prompt)
    return _parse_numbered_response(raw)


def _build_heading_translations(
    client, glossary: str, headings: list[str]
) -> dict[str, str]:
    unique_titles = list(dict.fromkeys(headings))
    if not unique_titles:
        return {}

    print(f"Translating {len(unique_titles)} section headings ...")
    translations = _translate_heading_texts(client, glossary, unique_titles)
    return {
        title: translations[i]
        for i, title in enumerate(unique_titles)
        if i in translations
    }


def _heading_translation_output(zh: str, newline: str) -> str:
    return (
        newline + newline + _normalize_newlines(zh, newline) + newline
        + _HEADING_TRANSLATION_MARKER + newline
    )


def _generated_heading_ranges(
    heading_translations: Mapping[str, str],
    source: SourceFile,
    document: LatexDocument,
) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    skip_ranges = translation_skip_ranges(document)
    for name in TRANSLATABLE_HEADING_NAMES:
        for ref in document.commands(name):
            if _untrusted(ref) or _ref_is_within_ranges(ref, skip_ranges):
                continue
            title_text = document.argument_text(ref, 0)
            zh = heading_translations.get(title_text or "")
            if not zh:
                continue
            start = _adjacent_complete_label_end(source.content, document, ref)
            generated = _heading_translation_output(zh, source.newline)
            if source.content.startswith(generated, start):
                ranges.append((start, start + len(generated)))
    return ranges


def _slice_without_ranges(
    content: str,
    start: int,
    end: int,
    ranges: Iterable[tuple[int, int]],
) -> str:
    parts: list[str] = []
    cursor = start
    for range_start, range_end in sorted(ranges):
        if range_end <= cursor or range_start >= end:
            continue
        parts.append(content[cursor:max(cursor, range_start)])
        cursor = min(end, max(cursor, range_end))
    parts.append(content[cursor:end])
    return "".join(parts)


def _merge_ranges(
    ranges: Iterable[tuple[int, int]],
) -> tuple[tuple[int, int], ...]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if start >= end:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return tuple(merged)


def _translation_projection_ranges(
    content: str,
    document: LatexDocument,
    heading_ranges: Iterable[tuple[int, int]],
) -> tuple[tuple[int, int], ...]:
    ranges = list(heading_ranges)
    ranges.extend(document.comment_ranges)
    ranges.extend(_iter_environment_token_ranges(content))
    ranges.extend(
        (ref.start, ref.end)
        for ref in document.math
        if ref.display or not ref.complete
    )
    for name in _TRANSLATION_STRIP_COMMAND_NAMES:
        ranges.extend(
            (ref.start, ref.end)
            for ref in document.commands(name)
            if ref.complete and not ref.opaque
        )
    return _merge_ranges(ranges)


def _canonical_translation_prose(text: str) -> str:
    text = _strip_env_wrappers(text)
    text = _strip_heading_lines(text)
    return _strip_translation_structure(text)


def _translate_headings(heading_translations: dict[str, str], content: str) -> str:
    source = SourceFile(Path("<translated-content>"), content)
    newline = source.newline
    document = LatexDocument(source)
    skip_ranges = translation_skip_ranges(document)
    refs = sorted(
        (
            ref
            for name in TRANSLATABLE_HEADING_NAMES
            for ref in document.commands(name)
        ),
        key=lambda ref: ref.start,
    )
    headings = []
    for ref in refs:
        if not ref.complete or ref.opaque:
            continue
        if _ref_is_within_ranges(ref, skip_ranges):
            continue
        title_text = document.argument_text(ref, 0)
        if title_text is None:
            continue
        insert_pos = _adjacent_complete_label_end(content, document, ref)
        headings.append((insert_pos, title_text))

    for insert_pos, title_text in reversed(headings):
        zh = heading_translations.get(title_text)
        if not zh:
            continue
        generated = _heading_translation_output(zh, newline)
        if content.startswith(generated, insert_pos):
            continue
        content = content[:insert_pos] + generated + content[insert_pos:]

    return content


def translate_file_content(
    client, glossary: str, heading_translations: dict[str, str], content: str,
    request_limiter=None,
) -> str:
    source = SourceFile(Path("<translation-content>"), content)
    document = LatexDocument(source)
    skip_ranges = translation_skip_ranges(document)
    heading_ranges = _generated_heading_ranges(
        heading_translations, source, document,
    )
    projection_ranges = _translation_projection_ranges(
        content, document, heading_ranges,
    )
    chunk_positions = _translation_chunk_ranges(content, document)
    chunks = [content[start:end] for start, end in chunk_positions]
    prose_chunks = [
        _slice_without_ranges(
            content, start, end, projection_ranges,
        ).strip()
        for start, end in chunk_positions
    ]
    newline = source.newline
    blocks = _complete_translation_blocks(content)
    translated_chunks: set[int] = set()
    generated_ranges: list[tuple[int, int]] = []
    for i, ((_, end), prose) in enumerate(zip(chunk_positions, prose_chunks)):
        digest = _translation_digest(prose)
        for block in blocks:
            if (
                block.digest == digest
                and content[end:block.start] == newline + newline
            ):
                translated_chunks.add(i)
                generated_ranges.append((block.start, block.end))
                break

    numbered: dict[int, str] = {}
    for i, chunk in enumerate(chunks):
        if i in translated_chunks:
            continue
        if _chunk_in_skip_range(
            chunk_positions[i][0], chunk_positions[i][1], generated_ranges
        ):
            continue
        if _chunk_in_skip_range(
            chunk_positions[i][0], chunk_positions[i][1], skip_ranges
        ):
            continue
        stripped = prose_chunks[i]
        if stripped and _is_prose(stripped):
            numbered[i] = stripped

    if not numbered:
        return _translate_headings(heading_translations, content)

    if request_limiter is None:
        translations = _batch_translate(client, glossary, numbered)
    else:
        translations = _batch_translate(
            client, glossary, numbered, request_limiter,
        )

    edits = [
        Edit(
            source.path,
            chunk_positions[i][1],
            chunk_positions[i][1],
            (
                (newline if chunks[i].endswith(newline) else newline + newline)
                + _TRANSLATION_BEGIN_PREFIX
                + _translation_digest(prose_chunks[i])
                + newline
                + _normalize_newlines(translations[i], newline)
                + newline
                + _TRANSLATION_END_PREFIX
                + _translation_digest(prose_chunks[i])
                + (
                    newline
                    if (
                        chunk_positions[i][1] < len(content)
                        and content[chunk_positions[i][1]] not in "\r\n"
                    )
                    else ""
                )
            ),
            "translate_paragraph",
            Safety.LOSSY,
        )
        for i in translations
    ]
    assembled = EditPlanner.apply(source, edits)
    return _translate_headings(heading_translations, assembled)


@dataclass(frozen=True)
class BarrierResult:
    snapshot: DocumentSnapshot
    diagnostics: tuple[Diagnostic, ...]


SourceTranslator = Callable[[SourceFile, DiscoveryFacts], str]


def _unique_lexical_document_body_range(
    content: str,
) -> tuple[int, int] | None:
    begin_token = r"\begin{document}"
    end_token = r"\end{document}"
    if content.count(begin_token) != 1 or content.count(end_token) != 1:
        return None
    body_start = content.index(begin_token) + len(begin_token)
    body_end = content.index(end_token)
    if body_start > body_end:
        return None
    return body_start, body_end


class TranslationBarrier:
    name = "translate"

    def __init__(self, translator: SourceTranslator, max_workers: int = 5):
        self.translator = translator
        self.max_workers = max_workers

    def run(self, snapshot: DocumentSnapshot) -> BarrierResult:
        discovery = snapshot.discovery
        if discovery is None or Fact.DISCOVERY not in snapshot.current_facts:
            raise PipelineContractError(
                "translation requires current discovery facts"
            )
        if Fact.SYNTAX not in snapshot.current_facts:
            raise PipelineContractError("translation requires syntax facts")

        diagnostics: list[Diagnostic] = []
        jobs: dict[Path, tuple[SourceFile, tuple[int, int] | None]] = {}
        for path in discovery.include_order:
            source = snapshot.sources[path]
            body_range = None
            if path == snapshot.main_tex:
                document = snapshot.documents[path]
                document_refs = sorted(
                    document.environments("document"),
                    key=lambda ref: (ref.start, -ref.end),
                )
                if not document_refs:
                    diagnostics.append(Diagnostic(
                        source.path,
                        self.name,
                        "missing-document",
                        "preserved main source without a document environment",
                    ))
                    continue
                ref = document_refs[0]
                issue = _reference_issue(source, ref, self.name)
                if issue is not None:
                    if ref.file != source.path:
                        diagnostics.extend(issue.diagnostics)
                        continue
                    body_range = _unique_lexical_document_body_range(
                        source.content,
                    )
                    if body_range is None:
                        diagnostics.extend(issue.diagnostics)
                        continue
                    diagnostics.append(Diagnostic(
                        source.path,
                        self.name,
                        "lexical-document-boundary-fallback",
                        "translated main source using unique lexical document boundaries",
                        *body_range,
                    ))
                else:
                    body_range = (ref.body_start, ref.body_end)
                source = SourceFile(
                    path,
                    source.content[body_range[0]:body_range[1]],
                )
            jobs[path] = (source, body_range)

        translated: dict[Path, str] = {}
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {
                pool.submit(self.translator, source, discovery): path
                for path, (source, _) in jobs.items()
            }
            try:
                for future in as_completed(futures):
                    path = futures[future]
                    content = future.result()
                    body_range = jobs[path][1]
                    if body_range is not None:
                        start, end = body_range
                        original = snapshot.sources[path].content
                        content = original[:start] + content + original[end:]
                    translated[path] = content
            except Exception:
                for future in futures:
                    future.cancel()
                raise

        changed = {
            path: SourceFile(path, content)
            for path, content in translated.items()
            if content != snapshot.sources[path].content
        }
        if not changed:
            return BarrierResult(snapshot, tuple(diagnostics))

        sources = dict(snapshot.sources)
        sources.update(changed)
        next_snapshot = replace(
            snapshot,
            revision=snapshot.revision + 1,
            sources=read_only_mapping(sources),
            documents=read_only_mapping({}),
            current_facts=snapshot.current_facts & {Fact.DISCOVERY},
        )
        return BarrierResult(next_snapshot, tuple(diagnostics))


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


def find_main_tex(paper_dir: Path) -> Path:
    texfiles = sorted(paper_dir.rglob("*.tex"))
    for tex in texfiles:
        source = SourceFile.from_path(tex)
        document = LatexDocument(source)
        documentclasses = document.commands("documentclass")
        documents = document.environments("document")
        if any(ref.complete and not ref.opaque for ref in documentclasses):
            return tex
        if any(ref.complete and not ref.opaque for ref in documents):
            return tex

    if texfiles:
        return texfiles[0]

    print("Error: no .tex file found in paper/", file=sys.stderr)
    sys.exit(1)


def _whole_line_range_if_alone(
    content: str,
    start: int,
    end: int,
) -> tuple[int, int]:
    line_start = content.rfind("\n", 0, start) + 1
    next_newline = content.find("\n", end)
    line_end = len(content) if next_newline == -1 else next_newline + 1
    if (
        not content[line_start:start].strip()
        and not content[end:line_end].strip()
    ):
        return line_start, line_end
    return start, end


def plan_simplify_documentclass(
    source: SourceFile,
    document: LatexDocument,
) -> list[Edit]:
    edits: list[Edit] = []
    for ref in document.commands("documentclass"):
        class_argument = document.argument(ref, 1)
        if (
            not ref.complete
            or ref.opaque
            or class_argument is None
            or not class_argument.complete
            or class_argument.opaque
            or class_argument.opening_delimiter != "{"
            or class_argument.closing_delimiter != "}"
        ):
            continue
        if document.source_text(ref) != r"\documentclass{article}":
            edits.append(Edit(
                file=source.path,
                start=ref.start,
                end=ref.end,
                replacement=r"\documentclass{article}",
                pass_name="simplify_documentclass",
                safety=Safety.LOSSY,
            ))
    for ref in document.commands("maketitle"):
        if not ref.complete or ref.opaque:
            continue
        start, end = _whole_line_range_if_alone(
            source.content,
            ref.start,
            ref.command_token_end,
        )
        edits.append(Edit(
            file=source.path,
            start=start,
            end=end,
            replacement="",
            pass_name="remove_maketitle",
            safety=Safety.LOSSY,
        ))
    return edits


def plan_unnumber_paragraphs(
    source: SourceFile,
    document: LatexDocument,
) -> list[Edit]:
    edits = []
    for ref in document.commands("paragraph"):
        title = document.argument(ref, 0)
        star = ref.arguments[0] if ref.arguments else None
        if (
            not ref.complete
            or ref.opaque
            or title is None
            or not title.complete
            or title.opaque
            or star is not None
        ):
            continue
        edits.append(
            Edit(
                file=source.path,
                start=ref.command_token_end,
                end=ref.command_token_end,
                replacement="*",
                pass_name="unnumber_paragraphs",
                safety=Safety.LOSSY,
            )
        )
    return edits


_HEADING_LEVELS = {
    "chapter": 0,
    "section": 1,
    "subsection": 2,
    "subsubsection": 3,
}


def _appendix_letter(number: int) -> str:
    """Format a one-based appendix number like spreadsheet columns."""
    letters = ""
    while number > 0:
        number, remainder = divmod(number - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def plan_preserve_appendix_numbering(
    snapshot: DocumentSnapshot,
) -> PlanOutcome:
    """Make appendix numbers explicit before pandoc drops ``\\appendix``."""
    if snapshot.discovery is None:
        raise PipelineContractError("appendix numbering requires discovery facts")
    ordered_paths = list(snapshot.discovery.include_order)
    ordered_paths.extend(
        path for path in snapshot.sources if path not in ordered_paths
    )
    events: list[tuple[int, int, SourceFile, LatexNodeRef]] = []
    for path_index, path in enumerate(ordered_paths):
        source = snapshot.sources[path]
        document = snapshot.documents[path]
        refs = document.commands("appendix")
        for name in _HEADING_LEVELS:
            refs.extend(document.commands(name))
        events.extend(
            (path_index, ref.start, source, ref)
            for ref in refs
        )
    events.sort(key=lambda item: (item[0], item[1]))
    appendix_index = next(
        (index for index, event in enumerate(events) if event[3].name == "appendix"),
        None,
    )
    if appendix_index is None:
        return PlanOutcome()
    numbered_after = [
        ref for *_, ref in events[appendix_index + 1:]
        if ref.name in _HEADING_LEVELS
        and not _untrusted(ref)
        and (not ref.arguments or ref.arguments[0] is None)
    ]
    if not numbered_after:
        return PlanOutcome()
    top_level = min(_HEADING_LEVELS[ref.name] for ref in numbered_after)
    counters = [0] * len(_HEADING_LEVELS)
    outcomes: list[PlanOutcome] = []
    active = False
    for _, _, source, ref in events:
        if ref.name == "appendix":
            active = True
            issue = _reference_issue(
                source, ref, "preserve_appendix_numbering",
            )
            if issue is not None:
                outcomes.append(issue)
                continue
            start, end = _whole_line_range_if_alone(
                source.content, ref.start, ref.end,
            )
            outcomes.append(PlanOutcome(edits=(Edit(
                source.path,
                start,
                end,
                "",
                "preserve_appendix_numbering",
                Safety.SAFE,
            ),)))
            continue
        if not active or _untrusted(ref):
            continue
        level = _HEADING_LEVELS[ref.name]
        if level < top_level or (ref.arguments and ref.arguments[0] is not None):
            continue
        title = _public_argument(ref, 0)
        if title is None or not title.complete or title.opaque:
            continue
        counters[level] += 1
        for deeper in range(level + 1, len(counters)):
            counters[deeper] = 0
        parts = [_appendix_letter(counters[top_level])]
        parts.extend(
            str(counters[index])
            for index in range(top_level + 1, level + 1)
        )
        prefix = ".".join(parts) + " "
        edits = [Edit(
            source.path,
            ref.command_token_end,
            ref.command_token_end,
            "*",
            "preserve_appendix_numbering",
            Safety.SAFE,
        )]
        title_start = title.start + len(title.opening_delimiter or "")
        edits.append(Edit(
            source.path,
            title_start,
            title_start,
            prefix,
            "preserve_appendix_numbering",
            Safety.SAFE,
        ))
        optional = ref.arguments[1] if len(ref.arguments) > 1 else None
        if optional is not None and optional.complete and not optional.opaque:
            optional_start = optional.start + len(optional.opening_delimiter or "")
            edits.append(Edit(
                source.path,
                optional_start,
                optional_start,
                prefix,
                "preserve_appendix_numbering",
                Safety.SAFE,
            ))
        outcomes.append(PlanOutcome(edits=tuple(edits)))
    return _combine_outcomes(outcomes)


# ---------------------------------------------------------------------------
# Preamble & noise cleanup
# ---------------------------------------------------------------------------

STRIP_PACKAGES = {
    "geometry", "hyperref", "cleveref", "natbib", "biblatex",
    "xcolor", "color", "microtype", "fontspec", "inputenc", "fontenc",
    "graphicx", "float", "placeins", "stfloats",
    "titlesec", "fancyhdr", "setspace", "parskip",
    "caption", "subcaption", "lineno", "enumitem",
    "booktabs", "multirow", "makecell", "colortbl", "array",
    "diagbox", "rotating", "ulem", "soul",
    "tcolorbox", "forest", "tikz", "pgfplots",
    "axessibility", "savetrees", "comment",
    "wrapfig", "pifont", "fontawesome", "fontawesome5",
    "lipsum", "blindtext", "nicematrix",
    "babel", "etoolbox", "bm",
    "adjustbox", "changepage", "pdflscape", "afterpage",
    "fancyvrb", "minted",
}

_CONFIG_CMDS = [
    "hypersetup", "captionsetup", "definecolor",
    "lstset", "lstdefinestyle", "pagestyle", "fancyhf", "fancyhead",
    "fancyfoot", "tcbset", "usetikzlibrary", "pgfplotsset",
    "sisetup", "DeclareSIUnit", "linespread",
]

_DEFINITION_COMMANDS = (
    "newcommand", "renewcommand", "providecommand", "DeclareRobustCommand",
)


def _combine_outcomes(outcomes: Iterable[PlanOutcome]) -> PlanOutcome:
    edits: list[Edit] = []
    diagnostics: list[Diagnostic] = []
    for outcome in outcomes:
        edits.extend(outcome.edits)
        diagnostics.extend(outcome.diagnostics)
    return PlanOutcome(tuple(edits), tuple(diagnostics))


def _suppress_contained_edits(outcome: PlanOutcome) -> PlanOutcome:
    """Keep outermost edits while retaining all planner diagnostics.

    Crossing ranges are intentionally retained so ``EditPlanner`` continues
    to reject planner defects that are not strict parent/child containment.
    """
    selected: list[Edit] = []
    for edit in sorted(
        outcome.edits,
        key=lambda item: (str(item.file), item.start, -item.end),
    ):
        if any(
            outer.file == edit.file
            and outer.start <= edit.start
            and edit.end <= outer.end
            for outer in selected
        ):
            continue
        selected.append(edit)
    return PlanOutcome(tuple(selected), outcome.diagnostics)


def _remove_trusted_ref(
    source: SourceFile,
    ref: LatexNodeRef,
    pass_name: str,
    *,
    whole_line: bool = False,
) -> PlanOutcome:
    issue = _reference_issue(source, ref, pass_name)
    if issue is not None:
        return issue
    start, end = ref.start, ref.end
    if whole_line:
        start, end = _whole_line_range_if_alone(source.content, start, end)
    return PlanOutcome(edits=(Edit(
        source.path, start, end, "", pass_name, Safety.LOSSY,
    ),))


def _definition_name(document: LatexDocument, ref: LatexNodeRef) -> str | None:
    argument = document.argument(ref, 1)
    if argument is None or not argument.complete or argument.opaque:
        return None
    # Lexical command-name validation inside a parser-owned definition argument.
    match = re.fullmatch(r"\\([A-Za-z@]+)", argument.text.strip())
    return None if match is None else match.group(1)


def _internal_definition_refs(document: LatexDocument) -> list[LatexNodeRef]:
    refs: list[LatexNodeRef] = []
    for command in _DEFINITION_COMMANDS:
        refs.extend(
            ref for ref in document.commands(command)
            if "@" in (_definition_name(document, ref) or "")
        )
    return refs


def plan_filter_packages(
    source: SourceFile,
    document: LatexDocument,
) -> PlanOutcome:
    outcomes: list[PlanOutcome] = []
    for command in ("usepackage", "RequirePackage"):
        for ref in document.commands(command):
            issue = _reference_issue(
                source, ref, "strip_problematic_packages",
            )
            if issue is not None:
                outcomes.append(issue)
                continue
            argument = document.argument(ref, 1)
            if argument is None or not argument.complete or argument.opaque:
                outcomes.append(_opaque_structure_outcome(
                    source, ref, "strip_problematic_packages",
                    f"preserved incomplete package list of {command}",
                ))
                continue
            packages = [package.strip() for package in argument.text.split(",")]
            remaining = [
                package for package in packages
                if package and package not in STRIP_PACKAGES
            ]
            if not remaining:
                outcomes.append(_remove_trusted_ref(
                    source, ref, "strip_problematic_packages", whole_line=True,
                ))
            elif len(remaining) != len(packages):
                outcomes.append(plan_transform_argument(
                    source,
                    ref,
                    1,
                    lambda _text, value=", ".join(remaining): value,
                    "strip_problematic_packages",
                    Safety.LOSSY,
                ))
    return _combine_outcomes(outcomes)


def plan_strip_config(
    source: SourceFile,
    document: LatexDocument,
) -> PlanOutcome:
    candidates: list[LatexNodeRef] = []
    for command in _CONFIG_CMDS:
        candidates.extend(document.commands(command))
    candidates.extend(document.commands("makeatletter"))
    candidates.extend(document.commands("makeatother"))
    candidates.extend(_internal_definition_refs(document))

    # A configuration command can occur inside a definition that is itself
    # removed. Keep only the outermost trusted candidate to avoid edit overlap.
    candidates.sort(key=lambda ref: (ref.start, -ref.end))
    selected: list[LatexNodeRef] = []
    outcomes: list[PlanOutcome] = []
    for ref in candidates:
        if _untrusted(ref):
            outcomes.append(_opaque_structure_outcome(
                source, ref, "strip_problematic_packages",
            ))
            continue
        if any(
            parent.start <= ref.start and ref.end <= parent.end
            for parent in selected
        ):
            continue
        selected.append(ref)
        outcomes.append(_remove_trusted_ref(
            source, ref, "strip_problematic_packages", whole_line=True,
        ))
    return _combine_outcomes(outcomes)


def _plan_problematic_packages(
    source: SourceFile,
    document: LatexDocument,
) -> PlanOutcome:
    return _suppress_contained_edits(_combine_outcomes((
        plan_filter_packages(source, document),
        plan_strip_config(source, document),
    )))


_NOISE_NO_ARG = (
    "sloppy", "raggedright", "raggedbottom", "noindent",
    "smallskip", "medskip", "bigskip", "vfill", "hfill",
    "allowbreak", "linebreak", "pagebreak", "newpage", "clearpage",
    "cleardoublepage", "centering", "tableofcontents", "FloatBarrier",
    "maketitle", "notag",
)

_NOISE_ONE_ARG = [
    "vspace", "hspace", "enlargethispage",
    "phantom", "vphantom", "hphantom",
    "todo", "fixme", "marginpar",
    "stepcounter",
]

_NOISE_TWO_ARG = [
    "setlength", "addtolength", "setcounter", "addtocounter",
    "csgdef",
]

_STRIP_ENVS = {"tikzpicture", "comment", "CCSXML"}

_PANDOC_INCOMPATIBLE_FONT_DECLARATIONS = ("tiny",)


def plan_strip_noise(
    source: SourceFile,
    document: LatexDocument,
) -> PlanOutcome:
    environment_refs = sorted(
        (
            ref
            for name in _STRIP_ENVS
            for ref in document.environments(name)
        ),
        key=lambda ref: (ref.start, -ref.end),
    )
    removed_ranges: list[tuple[int, int]] = []
    outcomes: list[PlanOutcome] = []
    for ref in environment_refs:
        if _untrusted(ref):
            outcomes.append(_opaque_structure_outcome(
                source, ref, "strip_noise_commands",
            ))
            continue
        if any(start <= ref.start and ref.end <= end for start, end in removed_ranges):
            continue
        removed_ranges.append((ref.start, ref.end))
        outcomes.append(_remove_trusted_ref(
            source, ref, "strip_noise_commands", whole_line=True,
        ))

    command_names = (*_NOISE_NO_ARG, "ccsdesc", *_NOISE_ONE_ARG, *_NOISE_TWO_ARG)
    for command in command_names:
        for ref in document.commands(command):
            if any(
                start <= ref.start and ref.end <= end
                for start, end in removed_ranges
            ):
                continue
            outcomes.append(_remove_trusted_ref(
                source, ref, "strip_noise_commands",
            ))
    for ref in document.commands("today"):
        issue = _reference_issue(source, ref, "strip_noise_commands")
        if issue is not None:
            outcomes.append(issue)
            continue
        outcomes.append(PlanOutcome(edits=(Edit(
            source.path,
            ref.start,
            ref.command_token_end,
            datetime.date.today().strftime("%B %d, %Y"),
            "strip_noise_commands",
            Safety.LOSSY,
        ),)))
    # A definition such as
    # \newenvironment{x}{\begin{minipage}}{\end{minipage}} can make
    # pylatexenc conservatively mark the rest of the file opaque.  These
    # declarations are argument-free presentation tokens, so removing only
    # their parser-owned command token remains bounded even in that case and
    # prevents pandoc's MathML reader from rejecting them inside formulas.
    definition_name_ranges = tuple(
        (argument.start, argument.end)
        for definition_command in _DEFINITION_COMMANDS
        for definition in document.commands(definition_command)
        if (argument := document.argument(definition, 1)) is not None
    )
    protected_ranges = (*removed_ranges, *definition_name_ranges)
    for command in _PANDOC_INCOMPATIBLE_FONT_DECLARATIONS:
        for ref in document.commands(command):
            if any(
                start <= ref.start and ref.end <= end
                for start, end in protected_ranges
            ):
                continue
            outcomes.append(PlanOutcome(edits=(Edit(
                source.path,
                ref.start,
                ref.command_token_end,
                "",
                "strip_noise_commands",
                Safety.LOSSY,
            ),)))
    return _suppress_contained_edits(_combine_outcomes(outcomes))


# ---------------------------------------------------------------------------
# Annotation system stripping (e.g. \atran, \aeq, \annotate)
# ---------------------------------------------------------------------------

_ANNOTATION_HELPERS = {
    "annotate", "annotatehypertarget", "annotateinitused",
    "annotategetlabels", "annotateprintlabels",
}


def plan_strip_annotations(
    source: SourceFile,
    document: LatexDocument,
) -> PlanOutcome:
    outcomes: list[PlanOutcome] = []
    for command in ("newcommand", "renewcommand"):
        for ref in document.commands(command):
            name = _definition_name(document, ref)
            if name is None:
                # Only diagnose a definition whose parsed name identifies it
                # as annotation infrastructure despite an incomplete body.
                name_argument = document.argument(ref, 1)
                raw_name = name_argument.text.strip() if name_argument else ""
                if raw_name == r"\atran" or raw_name.lstrip("\\") in _ANNOTATION_HELPERS:
                    outcomes.append(_opaque_structure_outcome(
                        source, ref, "strip_annotation_system",
                    ))
                continue
            if name == "atran":
                outcomes.append(plan_transform_argument(
                    source,
                    ref,
                    4,
                    lambda _text: "#1",
                    "strip_annotation_system",
                    Safety.LOSSY,
                ))
            elif name in _ANNOTATION_HELPERS:
                outcomes.append(_remove_trusted_ref(
                    source, ref, "strip_annotation_system", whole_line=True,
                ))
    for ref in document.commands("newcounter"):
        argument = document.argument(ref, 0)
        if argument is None or not argument.text.strip().startswith("annotate"):
            continue
        outcomes.append(_remove_trusted_ref(
            source, ref, "strip_annotation_system", whole_line=True,
        ))
    return _suppress_contained_edits(_combine_outcomes(outcomes))


# ---------------------------------------------------------------------------
# Citation and link normalization
# ---------------------------------------------------------------------------


def plan_normalize_citations(
    source: SourceFile,
    document: LatexDocument,
) -> PlanOutcome:
    outcomes: list[PlanOutcome] = []
    for command in _CITE_ALIASES:
        for ref in document.commands(command):
            issue = _reference_issue(source, ref, "normalize_citations")
            if issue is not None:
                outcomes.append(issue)
                continue
            argument = ref.arguments[-1] if ref.arguments else None
            if argument is None or not argument.complete or argument.opaque:
                outcomes.append(_opaque_structure_outcome(
                    source, ref, "normalize_citations",
                    f"preserved incomplete citation key argument of {command}",
                ))
                continue
            outcomes.append(PlanOutcome(edits=(Edit(
                source.path,
                ref.start,
                ref.end,
                f"\\cite{{{argument.text}}}",
                "normalize_citations",
                Safety.SAFE,
            ),)))
    return _suppress_contained_edits(_combine_outcomes(outcomes))


_LINK_KEEP_ARGUMENT = {
    "hyperref": 1,
    "hyperlink": 1,
    "hypertarget": 1,
    "Hy@raisedlink": 0,
    "texorpdfstring": 0,
}


def plan_preprocess_links(
    source: SourceFile,
    document: LatexDocument,
) -> PlanOutcome:
    algorithm_displays = tuple(
        environment
        for environment in document.environments("algorithmdisplay")
        if environment.complete and not environment.opaque
    )

    def is_generated_algorithm_anchor(ref: LatexNodeRef) -> bool:
        if not ref.complete or ref.opaque or ref.file != source.path:
            return False
        target = document.argument(ref, 0)
        visible = document.argument(ref, 1)
        if (
            target is None
            or visible is None
            or not target.complete
            or target.opaque
            or not visible.complete
            or visible.opaque
            or not target.text.strip()
            or visible.text.strip()
        ):
            return False
        for environment in algorithm_displays:
            if not (
                environment.body_start <= ref.start
                and ref.end <= environment.body_end
            ):
                continue
            prefix = source.content[environment.body_start:ref.start]
            suffix = source.content[ref.end:environment.body_end]
            marker_tail = suffix[1:].lstrip() if suffix.startswith("%") else ""
            if not prefix.strip() and marker_tail.startswith(
                "\\textbf{Algorithm "
            ):
                return True
        return False

    refs = sorted(
        (
            (ref, keep_argument)
            for command, keep_argument in _LINK_KEEP_ARGUMENT.items()
            for ref in document.commands(command)
            if not (
                command == "hypertarget"
                and is_generated_algorithm_anchor(ref)
            )
        ),
        key=lambda item: (item[0].start, -item[0].end),
    )
    selected: list[LatexNodeRef] = []
    outcomes: list[PlanOutcome] = []
    for ref, keep_argument in refs:
        if any(
            outer.start <= ref.start and ref.end <= outer.end
            for outer in selected
        ):
            continue
        selected.append(ref)
        outcome = plan_unwrap_command(
            source,
            ref,
            keep_argument,
            "preprocess_hyperref",
            Safety.LOSSY,
        )
        if outcome.edits:
            edit = outcome.edits[0]
            fragment = SourceFile(source.path, edit.replacement)
            nested = plan_preprocess_links(fragment, LatexDocument(fragment))
            replacement = EditPlanner.apply(fragment, nested.edits)
            outcome = PlanOutcome(
                edits=(replace(edit, replacement=replacement),),
                diagnostics=outcome.diagnostics + nested.diagnostics,
            )
        outcomes.append(outcome)
    return _combine_outcomes(outcomes)


# ---------------------------------------------------------------------------
# Table environment normalization
# ---------------------------------------------------------------------------

_TABLE_SIMPLE_RENAME = {
    "tabu": "tabular", "ltablex": "longtable",
    "NiceTabular": "tabular", "NiceTabular*": "tabular",
}

_TABLE_ENVIRONMENT_SPECS = {
    "tabu": ("tabular", 1),
    "ltablex": ("longtable", 1),
    "NiceTabular": ("tabular", 1),
    "NiceTabular*": ("tabular", 3),
    "tabularx": ("tabular", 2),
    "tabulary": ("tabular", 2),
    "xltabular": ("longtable", 2),
}


def _normalize_parsed_column_spec(spec: str) -> str | None:
    """Normalize complete table column tokens without parsing their contents."""
    result: list[str] = []
    i = 0
    while i < len(spec):
        if spec.startswith("@{", i):
            _, end = extract_brace_arg(spec, i + 1)
            if end == i + 1:
                return None
            i = end
            continue
        if spec[i] == "{":
            nested, end = extract_brace_arg(spec, i)
            if nested is None or end == i:
                return None
            normalized = _normalize_parsed_column_spec(nested)
            if normalized is None:
                return None
            result.append("{" + normalized + "}")
            i = end
            continue
        if spec[i] == "\\":
            end = i + 1
            if end < len(spec) and spec[end].isalpha():
                while end < len(spec) and spec[end].isalpha():
                    end += 1
            elif end < len(spec):
                end += 1
            result.append(spec[i:end])
            i = end
            continue
        if spec[i] == "S":
            end = i + 1
            while end < len(spec) and spec[end].isspace():
                end += 1
            if end < len(spec) and spec[end] == "[":
                bracket_end = find_matching_bracket(spec, end)
                if bracket_end is None:
                    return None
                end = bracket_end + 1
            result.append("r")
            i = end
            continue
        result.append(spec[i])
        i += 1
    return "".join(result)


def _plan_column_argument(
    source: SourceFile,
    ref: LatexNodeRef,
    argument_index: int,
) -> PlanOutcome:
    argument = (
        ref.arguments[argument_index]
        if 0 <= argument_index < len(ref.arguments)
        else None
    )
    if argument is None or not argument.complete or argument.opaque:
        return _opaque_structure_outcome(
            source,
            ref,
            "normalize_tables",
            f"preserved incomplete column specification of {ref.name}",
        )
    replacement = _normalize_parsed_column_spec(argument.text)
    if replacement is None:
        return _opaque_structure_outcome(
            source,
            ref,
            "normalize_tables",
            f"preserved unbalanced column specification of {ref.name}",
        )
    return plan_transform_argument(
        source,
        ref,
        argument_index,
        lambda _: replacement,
        "normalize_tables",
        Safety.LOSSY,
    )


def _plan_table_environment(
    source: SourceFile,
    ref: LatexNodeRef,
    new_name: str,
    column_index: int,
) -> PlanOutcome:
    issue = _reference_issue(source, ref, "normalize_tables")
    if issue is not None:
        return issue
    column = ref.arguments[column_index]
    if column is None or not column.complete or column.opaque:
        return _opaque_structure_outcome(
            source,
            ref,
            "normalize_tables",
            f"preserved incomplete column specification of {ref.name}",
        )
    outcomes = [plan_rename_environment(
        source, ref, new_name, "normalize_tables", Safety.LOSSY,
    )]
    if ref.begin_token_end < column.start:
        outcomes.append(PlanOutcome(edits=(Edit(
            source.path,
            ref.begin_token_end,
            column.start,
            "",
            "normalize_tables",
            Safety.LOSSY,
        ),)))
    if column.end < ref.body_start:
        outcomes.append(PlanOutcome(edits=(Edit(
            source.path,
            column.end,
            ref.body_start,
            "",
            "normalize_tables",
            Safety.LOSSY,
        ),)))
    outcomes.append(_plan_column_argument(source, ref, column_index))
    return _combine_outcomes(outcomes)


def plan_normalize_tables(
    source: SourceFile,
    document: LatexDocument,
) -> PlanOutcome:
    outcomes: list[PlanOutcome] = []
    for name, (new_name, column_index) in _TABLE_ENVIRONMENT_SPECS.items():
        for ref in document.environments(name):
            outcomes.append(_plan_table_environment(
                source, ref, new_name, column_index,
            ))

    for ref in document.environments("tblr"):
        issue = _reference_issue(source, ref, "normalize_tables")
        if issue is not None:
            outcomes.append(issue)
            continue
        argument = ref.arguments[1]
        if argument is None or not argument.complete or argument.opaque:
            outcomes.append(_opaque_structure_outcome(
                source, ref, "normalize_tables",
                "preserved incomplete tblr options",
            ))
            continue
        outcomes.append(_combine_outcomes((
            plan_rename_environment(
                source, ref, "tabular", "normalize_tables", Safety.LOSSY,
            ),
            PlanOutcome(edits=(Edit(
                source.path,
                ref.begin_token_end,
                ref.body_start,
                "{l}",
                "normalize_tables",
                Safety.LOSSY,
            ),)),
        )))

    for name, column_index in (("tabular", 0), ("longtable", 1)):
        for ref in document.environments(name):
            issue = _reference_issue(source, ref, "normalize_tables")
            outcomes.append(
                issue if issue is not None
                else _plan_column_argument(source, ref, column_index)
            )
    for ref in document.commands("multicolumn"):
        issue = _reference_issue(source, ref, "normalize_tables")
        outcomes.append(
            issue if issue is not None
            else _plan_column_argument(source, ref, 1)
        )
    return _combine_outcomes(outcomes)


def _outermost_refs(refs: Iterable[LatexNodeRef]) -> list[LatexNodeRef]:
    selected: list[LatexNodeRef] = []
    for ref in sorted(refs, key=lambda item: (item.start, -item.end)):
        if any(
            outer.start <= ref.start and ref.end <= outer.end
            for outer in selected
        ):
            continue
        selected.append(ref)
    return selected


def _apply_fragment_planner(
    source: SourceFile,
    start: int,
    end: int,
    planner: FileEditPlanner,
    pass_name: str,
    safety: Safety,
) -> PlanOutcome:
    """Plan a parser-owned fragment and map its contract to parent offsets."""
    fragment = SourceFile(source.path, source.content[start:end])
    nested = _coerce_plan_outcome(
        planner(fragment, LatexDocument(fragment))
    )
    replacement = EditPlanner.apply(fragment, nested.edits)
    diagnostics = tuple(
        replace(
            diagnostic,
            file=source.path,
            start=(
                None if diagnostic.start is None
                else start + diagnostic.start
            ),
            end=(
                None if diagnostic.end is None
                else start + diagnostic.end
            ),
        )
        for diagnostic in nested.diagnostics
    )
    edits = ()
    if replacement != fragment.content:
        edits = (Edit(
            source.path,
            start,
            end,
            replacement,
            pass_name,
            safety,
        ),)
    return PlanOutcome(edits, diagnostics)


def _fragment_replacement(
    source: SourceFile,
    start: int,
    end: int,
    outcome: PlanOutcome,
) -> str:
    if not outcome.edits:
        return source.content[start:end]
    return outcome.edits[0].replacement


def _flatten_makecell_body(content: str) -> str:
    # Lexical row separators inside an already parser-owned makecell body.
    content = re.sub(r"\s*\\\\(?:\s*\[[^\]]*\])?\s*", " ", content)
    content = re.sub(r"\s*\\newline\s*", " ", content)
    return content.strip()


def _plan_unwrap_commands(
    source: SourceFile,
    document: LatexDocument,
    command: str,
    keep_argument: int,
    pass_name: str,
    transform: TextTransform,
    nested_planner: FileEditPlanner | None = None,
) -> PlanOutcome:
    outcomes: list[PlanOutcome] = []
    trusted: list[LatexNodeRef] = []
    for ref in document.commands(command):
        issue = _reference_issue(source, ref, pass_name)
        if issue is not None:
            outcomes.append(issue)
        else:
            trusted.append(ref)
    for ref in _outermost_refs(trusted):
        argument = document.argument(ref, keep_argument)
        if argument is None or not argument.complete or argument.opaque:
            outcomes.append(_opaque_structure_outcome(source, ref, pass_name))
            continue
        content_start = argument.start + len(argument.opening_delimiter or "")
        content_end = argument.end - len(argument.closing_delimiter or "")
        nested = PlanOutcome()
        argument_text = argument.text
        if nested_planner is not None:
            nested = _apply_fragment_planner(
                source,
                content_start,
                content_end,
                nested_planner,
                pass_name,
                Safety.LOSSY,
            )
            argument_text = _fragment_replacement(
                source, content_start, content_end, nested,
            )
        outcomes.append(PlanOutcome(edits=(Edit(
            source.path,
            ref.start,
            ref.end,
            transform(argument_text),
            pass_name,
            Safety.LOSSY,
        ),), diagnostics=nested.diagnostics))
    return _combine_outcomes(outcomes)


def plan_unwrap_makecells(
    source: SourceFile,
    document: LatexDocument,
) -> PlanOutcome:
    return _plan_unwrap_commands(
        source, document, "makecell", 2, "unwrap_makecell",
        _flatten_makecell_body, plan_unwrap_makecells,
    )


def plan_unwrap_resizeboxes(
    source: SourceFile,
    document: LatexDocument,
) -> PlanOutcome:
    outcomes: list[PlanOutcome] = []
    trusted: list[LatexNodeRef] = []
    for ref in document.commands("resizebox"):
        issue = _reference_issue(source, ref, "unwrap_resizebox")
        if issue is not None:
            outcomes.append(issue)
        else:
            trusted.append(ref)
    for ref in _outermost_refs(trusted):
        argument = document.argument(ref, 2)
        if argument is None or not argument.complete or argument.opaque:
            outcomes.append(_opaque_structure_outcome(
                source, ref, "unwrap_resizebox",
            ))
            continue
        content_start = argument.start + len(argument.opening_delimiter or "")
        content_end = argument.end - len(argument.closing_delimiter or "")
        nested = _apply_fragment_planner(
            source,
            content_start,
            content_end,
            plan_unwrap_resizeboxes,
            "unwrap_resizebox",
            Safety.LOSSY,
        )
        outcomes.append(PlanOutcome(edits=(Edit(
            source.path, ref.start, ref.end,
            _fragment_replacement(source, content_start, content_end, nested),
            "unwrap_resizebox", Safety.LOSSY,
        ),), diagnostics=nested.diagnostics))
    return _suppress_contained_edits(_combine_outcomes(outcomes))


def _unwrap_environment_text(content: str, name: str) -> str:
    while True:
        source = SourceFile(Path("<environment>"), content)
        document = LatexDocument(source)
        edits: list[Edit] = []
        for ref in document.environments(name):
            if ref.complete and not ref.opaque:
                edits.extend(plan_unwrap_environment(
                    source, ref, f"unwrap_{name}", Safety.LOSSY,
                ).edits)
        if not edits:
            return content
        updated = EditPlanner.apply(source, edits)
        if updated == content:
            return content
        content = updated


def _plan_unwrap_environments(
    source: SourceFile,
    document: LatexDocument,
    name: str,
    pass_name: str,
    transform: TextTransform = lambda text: text,
    nested_planner: FileEditPlanner | None = None,
) -> PlanOutcome:
    outcomes: list[PlanOutcome] = []
    trusted: list[LatexNodeRef] = []
    for ref in document.environments(name):
        issue = _reference_issue(source, ref, pass_name)
        if issue is not None:
            outcomes.append(issue)
        else:
            trusted.append(ref)
    for ref in _outermost_refs(trusted):
        nested = PlanOutcome()
        if nested_planner is None:
            body = _unwrap_environment_text(
                source.content[ref.body_start:ref.body_end], name,
            )
        else:
            nested = _apply_fragment_planner(
                source,
                ref.body_start,
                ref.body_end,
                nested_planner,
                pass_name,
                Safety.LOSSY,
            )
            body = _fragment_replacement(
                source, ref.body_start, ref.body_end, nested,
            )
        outcomes.append(PlanOutcome(edits=(Edit(
            source.path, ref.start, ref.end, transform(body),
            pass_name, Safety.LOSSY,
        ),), diagnostics=nested.diagnostics))
    return _combine_outcomes(outcomes)


def plan_unwrap_adjustboxes(
    source: SourceFile,
    document: LatexDocument,
) -> PlanOutcome:
    environment_outcome = _plan_unwrap_environments(
        source,
        document,
        "adjustbox",
        "unwrap_adjustbox",
        nested_planner=plan_unwrap_adjustboxes,
    )
    command_outcome = _plan_unwrap_commands(
        source, document, "adjustbox", 1, "unwrap_adjustbox",
        lambda text: text, plan_unwrap_adjustboxes,
    )
    return _suppress_contained_edits(_combine_outcomes((
        environment_outcome, command_outcome,
    )))


def plan_unwrap_minipages(
    source: SourceFile,
    document: LatexDocument,
) -> PlanOutcome:
    return _plan_unwrap_environments(
        source, document, "minipage", "unwrap_minipages",
    )


def plan_convert_wrapfigures(
    source: SourceFile,
    document: LatexDocument,
) -> PlanOutcome:
    outcomes: list[PlanOutcome] = []
    for ref in document.environments("wrapfigure"):
        issue = _reference_issue(source, ref, "convert_wrapfigures")
        if issue is not None:
            outcomes.append(issue)
            continue
        body = _convert_wrapfigure_text(
            source.content[ref.body_start:ref.body_end],
        )
        outcomes.append(PlanOutcome(edits=(Edit(
            source.path, ref.start, ref.end,
            r"\begin{figure}[H]" + body + r"\end{figure}",
            "convert_wrapfigures", Safety.LOSSY,
        ),)))
    return _suppress_contained_edits(_combine_outcomes(outcomes))


def _convert_wrapfigure_text(content: str) -> str:
    source = SourceFile(Path("<wrapfigure>"), content)
    document = LatexDocument(source)
    refs = _outermost_refs(
        ref for ref in document.environments("wrapfigure")
        if ref.complete and not ref.opaque
    )
    edits = [Edit(
        source.path, ref.start, ref.end,
        r"\begin{figure}[H]"
        + _convert_wrapfigure_text(source.content[ref.body_start:ref.body_end])
        + r"\end{figure}",
        "convert_wrapfigures", Safety.LOSSY,
    ) for ref in refs]
    return EditPlanner.apply(source, edits)


def plan_unwrap_subfigures(
    source: SourceFile,
    document: LatexDocument,
) -> PlanOutcome:
    return _plan_unwrap_environments(
        source, document, "subfigure", "unwrap_subfigures",
    )


def plan_destar_floats(
    source: SourceFile,
    document: LatexDocument,
) -> PlanOutcome:
    outcomes = [
        plan_rename_environment(
            source, ref, name.removesuffix("*"), "destar_floats", Safety.LOSSY,
        )
        for name in ("table*", "figure*")
        for ref in document.environments(name)
    ]
    return _combine_outcomes(outcomes)


def _resolve_nested_captionof_fragment(content: str, kind: str) -> str:
    """Resolve captionof only inside a parser-owned minipage body."""
    begin = f"\\begin{{{kind}}}"
    end = f"\\end{{{kind}}}"
    source = SourceFile(Path("<nested-captionof>"), begin + content + end)
    outcome = plan_resolve_captionof(source, LatexDocument(source))
    resolved = EditPlanner.apply(source, outcome.edits)
    return resolved[len(begin):-len(end)]


def plan_resolve_captionof(
    source: SourceFile,
    document: LatexDocument,
) -> PlanOutcome:
    outcomes: list[PlanOutcome] = []
    minipages = document.environments("minipage")
    floats = [
        ref
        for name in ("table", "table*", "figure", "figure*")
        for ref in document.environments(name)
    ]
    selected_minipages: set[tuple[int, int]] = set()
    for caption in document.commands("captionof"):
        issue = _reference_issue(source, caption, "resolve_captionof")
        if issue is not None:
            outcomes.append(issue)
            continue
        kind_arg = document.argument(caption, 0)
        text_arg = document.argument(caption, 1)
        if (
            kind_arg is None or text_arg is None
            or not kind_arg.complete or kind_arg.opaque
            or not text_arg.complete or text_arg.opaque
        ):
            outcomes.append(_opaque_structure_outcome(
                source, caption, "resolve_captionof",
            ))
            continue
        kind = kind_arg.text.strip()
        if kind not in {"table", "figure"}:
            continue
        containing = [
            ref for ref in minipages
            if ref.start <= caption.start and caption.end <= ref.end
        ]
        if not containing:
            continue
        minipage = min(containing, key=lambda ref: ref.end - ref.start)
        key = (minipage.start, minipage.end)
        if key in selected_minipages:
            continue
        selected_minipages.add(key)
        mp_issue = _reference_issue(source, minipage, "resolve_captionof")
        if mp_issue is not None:
            outcomes.append(mp_issue)
            continue
        body = source.content[minipage.body_start:minipage.body_end]
        relative_start = caption.start - minipage.body_start
        relative_end = caption.end - minipage.body_start
        caption_text = source.content[text_arg.start:text_arg.end]
        body = (
            body[:relative_start] + r"\caption" + caption_text
            + body[relative_end:]
        )
        body = _resolve_nested_captionof_fragment(body, kind)
        already_wrapped = any(
            ref.name.removesuffix("*") == kind
            and ref.start < minipage.start
            and minipage.end < ref.end
            for ref in floats
        )
        replacement = body if already_wrapped else (
            f"\\begin{{{kind}}}[H]" + body + f"\\end{{{kind}}}"
        )
        outcomes.append(PlanOutcome(edits=(Edit(
            source.path, minipage.start, minipage.end, replacement,
            "resolve_captionof", Safety.LOSSY,
        ),)))
    return _suppress_contained_edits(_combine_outcomes(outcomes))


def build_table_float_passes() -> tuple[PassSpec, ...]:
    return (
        make_syntax_file_pass(
            name="normalize_tables", planner=plan_normalize_tables,
            safety=Safety.LOSSY, phase=Phase.COMPATIBILITY, idempotent=True,
        ),
        make_syntax_file_pass(
            name="resolve_captionof", planner=plan_resolve_captionof,
            safety=Safety.LOSSY, phase=Phase.COMPATIBILITY,
            after=frozenset({"normalize_tables"}),
            idempotent=True,
        ),
        make_syntax_file_pass(
            name="unwrap_minipages", planner=plan_unwrap_minipages,
            safety=Safety.LOSSY, phase=Phase.COMPATIBILITY,
            after=frozenset({"resolve_captionof", "normalize_tables"}),
            idempotent=True,
        ),
        make_syntax_file_pass(
            name="unwrap_makecell", planner=plan_unwrap_makecells,
            safety=Safety.LOSSY, phase=Phase.COMPATIBILITY,
            after=frozenset({"normalize_tables"}),
            idempotent=True,
        ),
        make_syntax_file_pass(
            name="unwrap_resizebox", planner=plan_unwrap_resizeboxes,
            safety=Safety.LOSSY, phase=Phase.COMPATIBILITY,
            after=frozenset({"normalize_tables"}),
            idempotent=True,
        ),
        make_syntax_file_pass(
            name="unwrap_adjustbox", planner=plan_unwrap_adjustboxes,
            safety=Safety.LOSSY, phase=Phase.COMPATIBILITY,
            after=frozenset({"normalize_tables"}),
            idempotent=True,
        ),
        make_syntax_file_pass(
            name="convert_wrapfigures", planner=plan_convert_wrapfigures,
            safety=Safety.LOSSY, phase=Phase.COMPATIBILITY, idempotent=True,
        ),
        make_syntax_file_pass(
            name="unwrap_subfigures", planner=plan_unwrap_subfigures,
            safety=Safety.LOSSY, phase=Phase.COMPATIBILITY, idempotent=True,
        ),
        make_syntax_file_pass(
            name="destar_floats", planner=plan_destar_floats,
            safety=Safety.LOSSY, phase=Phase.COMPATIBILITY,
            after=frozenset({"resolve_captionof"}),
            idempotent=True,
        ),
    )


# ---------------------------------------------------------------------------
# Theorem preprocessing
# ---------------------------------------------------------------------------

_BUILTIN_THEOREMS = {
    "theorem": "Theorem",
    "lemma": "Lemma",
    "proposition": "Proposition",
    "corollary": "Corollary",
    "definition": "Definition",
    "example": "Example",
    "remark": "Remark",
    "assumption": "Assumption",
    "claim": "Claim",
    "conjecture": "Conjecture",
    "observation": "Observation",
    "note": "Note",
    "fact": "Fact",
    "property": "Property",
    "condition": "Condition",
    "hypothesis": "Hypothesis",
}

_GENERATED_THEOREM_MARKER = "% paper2epub:generated-theorem"


def _theorem_rendering(
    source: SourceFile,
    document: LatexDocument,
    ref: LatexNodeRef,
    label: str,
    number: int,
) -> str:
    body = source.content[ref.body_start:ref.body_end]
    heading = f"\\textbf{{{label} {number}}}"
    optional = document.argument(ref, 0)
    if optional is not None and optional.text.strip():
        heading += f" \\textit{{({optional.text.strip()})}}"
    heading += "\\textbf{.}"
    return _normalize_newlines((
        f"\n\\begin{{quote}}\n{_GENERATED_THEOREM_MARKER}\n"
        f"{heading} {body}\n\\end{{quote}}\n"
    ), source.newline)


def _proof_rendering(
    source: SourceFile,
    document: LatexDocument,
    ref: LatexNodeRef,
) -> str:
    body = source.content[ref.body_start:ref.body_end]
    optional = document.argument(ref, 0)
    heading = (
        optional.text.strip()
        if optional is not None and optional.text.strip()
        else "Proof"
    )
    return _normalize_newlines((
        f"\n\\begin{{quote}}\n{_GENERATED_THEOREM_MARKER}\n"
        f"\\textit{{{heading}.}} {body} "
        "\\hfill$\\square$\n\\end{quote}\n"
    ), source.newline)


def _generated_theorem_quote_ranges(
    source: SourceFile,
    document: LatexDocument,
) -> tuple[tuple[int, int], ...]:
    return tuple(
        (quote.start, quote.end)
        for quote in document.environments("quote")
        if source.content[quote.body_start:quote.body_end].lstrip().startswith(
            _GENERATED_THEOREM_MARKER
        )
    )


def plan_theorems(
    snapshot: DocumentSnapshot,
    facts: DiscoveryFacts,
) -> PlanOutcome:
    labels = dict(_BUILTIN_THEOREMS)
    labels.update(facts.theorem_labels)
    environment_args = {name: "[" for name in labels}
    environment_args["proof"] = "["
    ordered_paths = facts.include_order + tuple(
        path for path in snapshot.sources if path not in facts.include_order
    )
    counters: dict[str, int] = {}
    outcomes: list[PlanOutcome] = []

    for path in ordered_paths:
        source = snapshot.sources[path]
        document = LatexDocument(source, environment_args=environment_args)
        generated_ranges = _generated_theorem_quote_ranges(source, document)
        candidates = [
            ref for name in (*labels, "proof")
            for ref in document.environments(name)
            if not any(
                start < ref.start and ref.end < end
                for start, end in generated_ranges
            )
        ]
        for ref in _outermost_refs(candidates):
            issue = _reference_issue(source, ref, "preprocess_theorems")
            if issue is not None:
                outcomes.append(issue)
                continue
            if ref.name == "proof":
                replacement = _proof_rendering(source, document, ref)
            else:
                counters[ref.name] = counters.get(ref.name, 0) + 1
                replacement = _theorem_rendering(
                    source, document, ref, labels[ref.name], counters[ref.name],
                )
            outcomes.append(PlanOutcome(edits=(Edit(
                source.path,
                ref.start,
                ref.end,
                replacement,
                "preprocess_theorems",
                Safety.LOSSY,
            ),)))
    return _combine_outcomes(outcomes)


# ---------------------------------------------------------------------------
# Code listing normalization
# ---------------------------------------------------------------------------


def plan_code_listings(
    source: SourceFile,
    document: LatexDocument,
) -> PlanOutcome:
    outcomes: list[PlanOutcome] = []
    for ref in document.environments("minted"):
        issue = _reference_issue(source, ref, "normalize_code_listings")
        if issue is not None:
            outcomes.append(issue)
            continue
        begin_name_start = ref.start + len("\\begin{")
        end_name_start = ref.end_token_start + len("\\end{")
        edits = [
            Edit(
                source.path,
                begin_name_start,
                begin_name_start + len("minted"),
                "verbatim",
                "normalize_code_listings",
                Safety.LOSSY,
            ),
            Edit(
                source.path,
                end_name_start,
                end_name_start + len("minted"),
                "verbatim",
                "normalize_code_listings",
                Safety.LOSSY,
            ),
        ]
        edits.extend(
            Edit(
                source.path,
                argument.start,
                argument.end,
                "",
                "normalize_code_listings",
                Safety.LOSSY,
            )
            for argument in ref.arguments
            if argument is not None
        )
        outcomes.append(PlanOutcome(edits=tuple(edits)))

    for ref in document.environments("lstlisting"):
        issue = _reference_issue(source, ref, "normalize_code_listings")
        if issue is not None:
            outcomes.append(issue)
            continue
        optional = document.argument(ref, 0)
        if optional is not None:
            outcomes.append(PlanOutcome(edits=(Edit(
                source.path,
                optional.start,
                optional.end,
                "",
                "normalize_code_listings",
                Safety.LOSSY,
            ),)))

    for ref in document.commands("mintinline"):
        issue = _reference_issue(source, ref, "normalize_code_listings")
        if issue is not None:
            outcomes.append(issue)
            continue
        code = document.argument(ref, 2)
        if code is None or not code.complete or code.opaque:
            outcomes.append(_opaque_structure_outcome(
                source, ref, "normalize_code_listings",
                "preserved incomplete mintinline code argument",
            ))
            continue
        outcomes.append(PlanOutcome(edits=(Edit(
            source.path,
            ref.start,
            ref.end,
            f"\\texttt{{{code.text}}}",
            "normalize_code_listings",
            Safety.LOSSY,
        ),)))
    return _suppress_contained_edits(_combine_outcomes(outcomes))


def convert_pdf_resources(paper_dir: Path) -> ResourceResult:
    import pypdfium2 as pdfium

    converted: set[Path] = set()
    diagnostics: list[Diagnostic] = []
    for pdf_path in paper_dir.rglob("*.pdf"):
        png_path = pdf_path.with_suffix(".png")
        try:
            doc = pdfium.PdfDocument(str(pdf_path))
            page = doc[0]
            bitmap = page.render(scale=4)
            bitmap.to_pil().save(str(png_path))
            converted.add(pdf_path)
            print(f"Converted: {pdf_path} -> {png_path}")
        except Exception as e:
            diagnostics.append(Diagnostic(
                file=pdf_path,
                pass_name="convert_pdf_resources",
                code="pdf-conversion-failed",
                message=f"could not convert {pdf_path}: {e}",
            ))
    return ResourceResult(frozenset(converted), tuple(diagnostics))


def plan_replace_tikz_figure(
    source: SourceFile,
    document: LatexDocument,
    figure: LatexNodeRef,
    image_reference: str,
) -> PlanOutcome:
    """Replace a figure's TikZ body while preserving its outer caption/label."""
    pass_name = "convert_tikz_resources"
    issue = _reference_issue(source, figure, pass_name)
    if issue is not None:
        return issue
    tikz_refs = [
        ref for ref in document.environments("tikzpicture")
        if figure.body_start <= ref.start and ref.end <= figure.body_end
    ]
    if not tikz_refs:
        return PlanOutcome()
    captions = [
        ref for ref in document.commands("caption")
        if figure.body_start <= ref.start and ref.end <= figure.body_end
        and not _untrusted(ref)
    ]
    tail_start = captions[-1].start if captions else figure.body_end
    inner_labels = [
        label.strip()
        for ref in document.commands("label")
        if figure.body_start <= ref.start and ref.end <= tail_start
        and not _untrusted(ref)
        for label in [document.argument_text(ref, 0)]
        if label and label.strip()
    ]
    replacement = (
        source.newline
        + r"\centering"
        + source.newline
        + rf"\includegraphics[width=\linewidth]{{{image_reference}}}"
        + source.newline
        + "".join(
            rf"\label{{{label}}}" + source.newline
            for label in inner_labels
        )
    )
    return PlanOutcome(edits=(Edit(
        source.path,
        figure.body_start,
        tail_start,
        replacement,
        pass_name,
        Safety.LOSSY,
    ),))


def _ordered_figure_refs(
    snapshot: DocumentSnapshot,
) -> list[tuple[SourceFile, LatexDocument, LatexNodeRef, bool]]:
    if snapshot.discovery is None:
        raise PipelineContractError("TikZ conversion requires discovery facts")
    ordered_paths = list(snapshot.discovery.include_order)
    ordered_paths.extend(
        path for path in snapshot.sources if path not in ordered_paths
    )
    figures: list[tuple[SourceFile, LatexDocument, LatexNodeRef, bool]] = []
    for path in ordered_paths:
        source = snapshot.sources[path]
        document = snapshot.documents[path]
        tikz_refs = document.environments("tikzpicture")
        for name in ("figure", "figure*"):
            for figure in document.environments(name):
                has_caption = any(
                    figure.body_start <= ref.start and ref.end <= figure.body_end
                    for ref in document.commands("caption")
                )
                if not has_caption:
                    continue
                has_tikz = any(
                    figure.body_start <= ref.start and ref.end <= figure.body_end
                    for ref in tikz_refs
                )
                figures.append((source, document, figure, has_tikz))
    return sorted(figures, key=lambda item: (
        ordered_paths.index(item[0].path), item[2].start,
    ))


def _pdf_figure_captions(pdf) -> list[tuple[int, float]]:
    """Return ``(page index, caption top)`` for numbered PDF figures."""
    captions: list[tuple[int, float]] = []
    pattern = re.compile(r"Figure\s+(?:[A-Z]+|\d+)(?:\.\d+)*:")
    for page_index, page in enumerate(pdf):
        text_page = page.get_textpage()
        text = text_page.get_text_range()
        for match in pattern.finditer(text):
            boxes = [
                text_page.get_charbox(index)
                for index in range(match.start(), match.end())
                if not text[index].isspace()
            ]
            if boxes:
                captions.append((
                    page_index,
                    max(box[3] for box in boxes),
                ))
    return captions


def _crop_figure_above_caption(page, caption_top: float, output: Path) -> None:
    """Rasterize and crop the nearest ink band immediately above a caption."""
    scale = 4
    image = page.render(scale=scale).to_pil().convert("RGB")
    page_width, page_height = page.get_size()
    # TikZ is emitted as PDF path objects. Group the path bands nearest the
    # caption; this preserves multi-row panels even when whitespace separates
    # their rows, while avoiding unrelated prose above the float.
    objects: list[tuple[float, float, float, float]] = []
    for obj in page.get_objects():
        if type(obj).__name__ == "PdfTextObj":
            continue
        x0, y0, x1, y1 = obj.get_bounds()
        if y0 >= caption_top and x1 >= 45 and x0 <= page_width - 45:
            objects.append((x0, y0, x1, y1))
    objects.sort(key=lambda bounds: bounds[1])
    selected: list[tuple[float, float, float, float]] = []
    band_top = caption_top
    for bounds in objects:
        if not selected or bounds[1] - band_top <= 45:
            selected.append(bounds)
            band_top = max(band_top, bounds[3])
        else:
            break
    if selected:
        padding = 15
        y0 = max(caption_top, min(bounds[1] for bounds in selected) - padding)
        y1 = min(page_height, max(bounds[3] for bounds in selected) + padding)
        crop_top = round((page_height - y1) * scale)
        crop_bottom = round((page_height - y0) * scale)
        search_left = round(35 * scale)
        search_right = round((page_width - 35) * scale)
        gray_region = image.convert("L").crop((
            search_left, crop_top, search_right, crop_bottom,
        ))
        mask = gray_region.point(lambda value: 255 if value < 245 else 0)
        ink = mask.getbbox()
        if ink is None:
            raise ValueError("figure path band contains no rendered content")
        pixel_padding = 6 * scale
        crop = (
            max(search_left, search_left + ink[0] - pixel_padding),
            crop_top,
            min(search_right, search_left + ink[2] + pixel_padding),
            crop_bottom,
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        image.crop(crop).save(output)
        return

    # Fallback for PDFs that flatten paths into a bitmap or form object.
    gray = image.convert("L")
    left = max(0, round(45 * scale))
    right = min(image.width, round((page_width - 45) * scale))
    caption_y = max(0, round((page_height - caption_top) * scale))
    dark_threshold = 245
    minimum_dark = max(2, scale)

    def row_has_ink(y: int) -> bool:
        row = gray.crop((left, y, right, y + 1))
        return sum(row.histogram()[:dark_threshold]) >= minimum_dark

    y = min(caption_y - 1, image.height - 1)
    while y > 0 and not row_has_ink(y):
        y -= 1
    bottom = y + 1
    blank_needed = 8 * scale
    blank_run = 0
    minimum_height = 20 * scale
    while y > 0:
        y -= 1
        if row_has_ink(y):
            blank_run = 0
        else:
            blank_run += 1
            if blank_run >= blank_needed and bottom - y >= minimum_height:
                break
    top = y + blank_run
    if bottom - top < minimum_height:
        raise ValueError("could not identify figure content above caption")

    region = gray.crop((left, top, right, bottom))
    mask = region.point(lambda value: 255 if value < dark_threshold else 0)
    bbox = mask.getbbox()
    if bbox is None:
        raise ValueError("figure crop contains no rendered content")
    padding = 6 * scale
    crop_left = max(left, left + bbox[0] - padding)
    crop_top = max(0, top + bbox[1] - padding)
    crop_right = min(right, left + bbox[2] + padding)
    crop_bottom = min(caption_y, top + bbox[3] + padding)
    output.parent.mkdir(parents=True, exist_ok=True)
    image.crop((crop_left, crop_top, crop_right, crop_bottom)).save(output)


def convert_tikz_resources(
    snapshot: DocumentSnapshot,
    pdf_path: Path,
) -> tuple[PlanOutcome, tuple[Diagnostic, ...]]:
    """Extract rendered TikZ figures from arXiv's PDF and plan source rewrites."""
    import pypdfium2 as pdfium

    figures = _ordered_figure_refs(snapshot)
    if not any(has_tikz for *_, has_tikz in figures):
        return PlanOutcome(), ()
    pdf = pdfium.PdfDocument(str(pdf_path))
    captions = _pdf_figure_captions(pdf)
    if len(captions) != len(figures):
        diagnostic = Diagnostic(
            snapshot.main_tex,
            "convert_tikz_resources",
            "figure-count-mismatch",
            f"preserved TikZ because source has {len(figures)} numbered figures "
            f"but PDF has {len(captions)} figure captions",
        )
        return PlanOutcome(), (diagnostic,)

    outcomes: list[PlanOutcome] = []
    diagnostics: list[Diagnostic] = []
    image_dir = snapshot.root / "paper2epub-figures"
    converted_index = 0
    for figure_index, (source, document, figure, has_tikz) in enumerate(figures):
        if not has_tikz:
            continue
        converted_index += 1
        image_path = image_dir / f"tikz-{converted_index:03d}.png"
        page_index, caption_top = captions[figure_index]
        try:
            _crop_figure_above_caption(pdf[page_index], caption_top, image_path)
        except Exception as error:
            diagnostics.append(Diagnostic(
                source.path,
                "convert_tikz_resources",
                "tikz-extraction-failed",
                f"could not extract TikZ figure {converted_index}: {error}",
                figure.start,
                figure.end,
            ))
            continue
        reference = os.path.relpath(image_path, source.path.parent)
        outcomes.append(plan_replace_tikz_figure(
            source, document, figure, reference,
        ))
        print(f"Extracted TikZ: PDF page {page_index + 1} -> {image_path}")
    return _combine_outcomes(outcomes), tuple(diagnostics)


def _resolve_converted_pdf(
    source_path: Path,
    reference: str,
    converted: frozenset[Path],
) -> Path | None:
    resolved_converted = {path.resolve(): path for path in converted}
    direct = (source_path.parent / reference).resolve()
    if direct in resolved_converted:
        return resolved_converted[direct]
    suffix = Path(reference)
    matches = [
        path for path in converted
        if len(suffix.parts) > 1
        and path.as_posix().endswith(suffix.as_posix())
    ]
    if not matches and len(suffix.parts) == 1:
        matches = [path for path in converted if path.name == suffix.name]
    return matches[0] if len(matches) == 1 else None


def plan_rewrite_pdf_refs(
    source: SourceFile,
    document: LatexDocument,
    resources: ResourceResult,
) -> PlanOutcome:
    outcomes: list[PlanOutcome] = []
    for ref in document.commands("includegraphics"):
        issue = _reference_issue(source, ref, "rewrite_pdf_image_refs")
        if issue is not None:
            outcomes.append(issue)
            continue
        argument = document.argument(ref, 1)
        if argument is None or not argument.complete or argument.opaque:
            outcomes.append(_opaque_structure_outcome(
                source, ref, "rewrite_pdf_image_refs",
                "preserved incomplete includegraphics resource",
            ))
            continue
        resource = argument.text.strip()
        if not resource.lower().endswith(".pdf"):
            continue
        if _resolve_converted_pdf(source.path, resource, resources.converted) is None:
            continue
        content_start = argument.start + len(argument.opening_delimiter or "")
        content_end = argument.end - len(argument.closing_delimiter or "")
        outcomes.append(PlanOutcome(edits=(Edit(
            source.path,
            content_start,
            content_end,
            argument.text[:-4] + ".png",
            "rewrite_pdf_image_refs",
            Safety.SAFE,
        ),)))
    return _combine_outcomes(outcomes)


DING_MAP = {
    "33": "!",
    "34": '"',
    "35": "#",
    "36": "$",
    "37": "%",
    "38": "&",
    "39": "'",
    "40": "(",
    "41": "✉",  # envelope
    "42": "*",
    "43": "+",
    "44": ",",
    "45": "-",
    "46": ".",
    "47": "/",
    "51": "✓",  # check mark ✓
    "52": "✗",  # ballot x ✗
    "53": "✗",  # alternate x
    "54": "✔",  # heavy check
    "55": "✘",  # heavy ballot x ✘
    "56": "✠",  # Maltese cross
    "72": "★",  # black star ★
    "73": "☆",  # white star
    "108": "▶",  # right triangle
    "110": "▼",  # down triangle
    "115": "●",  # black circle ●
    "164": "♦",  # diamond
    "168": "♣",  # club
    "170": "♥",  # heart
    "171": "♠",  # spade
    "172": "←",  # left arrow
    "173": "↑",  # up arrow
    "174": "→",  # right arrow
    "175": "↓",  # down arrow
    "228": "✉",  # envelope
}


_TEXTCIRCLED_MAP = {str(i): chr(0x2460 + i - 1) for i in range(1, 21)}  # ①-⑳


def plan_inline_symbols(
    source: SourceFile,
    document: LatexDocument,
    commands: tuple[str, ...] = ("ding", "textcircled"),
) -> PlanOutcome:
    outcomes: list[PlanOutcome] = []
    for command, replacements in (
        ("ding", DING_MAP),
        ("textcircled", _TEXTCIRCLED_MAP),
    ):
        if command not in commands:
            continue
        for ref in document.commands(command):
            issue = _reference_issue(source, ref, "inline_symbols")
            if issue is not None:
                outcomes.append(issue)
                continue
            argument = document.argument(ref, 0)
            if argument is None or not argument.complete or argument.opaque:
                outcomes.append(_opaque_structure_outcome(
                    source, ref, "inline_symbols",
                    f"preserved incomplete argument of {command}",
                ))
                continue
            replacement = replacements.get(argument.text.strip())
            if replacement is None:
                continue
            start, end = ref.start, ref.end
            if (
                command == "textcircled"
                and start > 0
                and end < len(source.content)
                and source.content[start - 1] == "$"
                and source.content[end] == "$"
            ):
                start -= 1
                end += 1
            outcomes.append(PlanOutcome(edits=(Edit(
                source.path,
                start,
                end,
                replacement,
                "inline_symbols",
                Safety.SAFE,
            ),)))
    return _suppress_contained_edits(_combine_outcomes(outcomes))


def plan_normalize_textsc(
    source: SourceFile,
    document: LatexDocument,
) -> PlanOutcome:
    outcomes: list[PlanOutcome] = []
    for ref in document.commands("textsc"):
        issue = _reference_issue(source, ref, "normalize_textsc")
        if issue is not None:
            outcomes.append(issue)
            continue
        outcomes.append(PlanOutcome(edits=(Edit(
            source.path,
            ref.start + 1,
            ref.command_token_end,
            "text",
            "normalize_textsc",
            Safety.SAFE,
        ),)))
    return _combine_outcomes(outcomes)


def build_initial_preprocessing_passes() -> tuple[PassSpec, ...]:
    return (
        make_syntax_file_pass(
            name="simplify_documentclass",
            planner=plan_simplify_documentclass,
            safety=Safety.LOSSY, phase=Phase.COMPATIBILITY,
            main_only=True,
        ),
        make_syntax_file_pass(
            name="strip_problematic_packages",
            planner=_plan_problematic_packages,
            safety=Safety.LOSSY, phase=Phase.COMPATIBILITY,
            after=frozenset({"simplify_documentclass"}),
            report_label="Stripped packages/config",
        ),
        make_syntax_file_pass(
            name="strip_noise_commands",
            planner=plan_strip_noise,
            safety=Safety.LOSSY, phase=Phase.COMPATIBILITY,
            after=frozenset({"strip_problematic_packages"}),
            report_label="Stripped noise commands",
        ),
        make_syntax_file_pass(
            name="strip_annotation_system",
            planner=plan_strip_annotations,
            safety=Safety.LOSSY, phase=Phase.COMPATIBILITY,
            after=frozenset({"strip_noise_commands"}),
            report_label="Stripped annotation system",
        ),
        make_syntax_file_pass(
            name="normalize_textsc",
            planner=plan_normalize_textsc,
            safety=Safety.SAFE, phase=Phase.COMPATIBILITY,
            after=frozenset({"strip_annotation_system"}),
            report_label="Replaced \\textsc with \\text",
        ),
    )


def build_reference_passes(
    resources: ResourceResult,
) -> tuple[PassSpec, ...]:
    """Build Task 5 source passes for later complete-plan assembly.

    External marker steps named in ``after`` are supplied by Task 9's
    preprocessing plan and are intentionally not part of this partial tuple.
    """
    def plan_pdf_refs(snapshot: DocumentSnapshot) -> PlanOutcome:
        outcomes = [
            plan_rewrite_pdf_refs(
                snapshot.sources[path], snapshot.documents[path], resources,
            )
            for path in snapshot.sources
        ]
        return _combine_outcomes(outcomes)

    def plan_inputs(snapshot: DocumentSnapshot) -> PlanOutcome:
        if snapshot.discovery is None:
            raise PipelineContractError(
                "input normalization requires discovery facts"
            )
        outcomes = [
            plan_normalize_inputs(
                snapshot.sources[path],
                snapshot.documents[path],
                snapshot.discovery,
            )
            for path in snapshot.sources
        ]
        return _combine_outcomes(outcomes)

    common = {
        "phase": Phase.COMPATIBILITY,
        "requires": frozenset({Fact.SYNTAX, Fact.DISCOVERY}),
        "invalidates": frozenset(set(Fact) - {Fact.DISCOVERY}),
        "implementation": Implementation.SYNTAX_AWARE,
        "idempotent": True,
    }
    return (
        PassSpec(
            name="rewrite_pdf_image_refs",
            planner=plan_pdf_refs,
            safety=Safety.SAFE,
            after=frozenset({"convert_pdf_resources"}),
            **common,
        ),
        make_syntax_file_pass(
            name="normalize_citations",
            planner=plan_normalize_citations,
            safety=Safety.SAFE, phase=Phase.COMPATIBILITY,
            after=frozenset({"rewrite_pdf_image_refs"}),
            idempotent=True,
        ),
        make_syntax_file_pass(
            name="preprocess_hyperref",
            planner=plan_preprocess_links,
            safety=Safety.LOSSY,
            phase=Phase.COMPATIBILITY,
            after=frozenset({"normalize_citations"}),
            idempotent=True,
        ),
        make_syntax_file_pass(
            name="replace_ding_commands",
            planner=lambda source, document: plan_inline_symbols(
                source, document, ("ding",),
            ),
            safety=Safety.SAFE,
            phase=Phase.COMPATIBILITY,
            idempotent=True,
        ),
        make_syntax_file_pass(
            name="replace_textcircled",
            planner=lambda source, document: plan_inline_symbols(
                source, document, ("textcircled",),
            ),
            safety=Safety.SAFE,
            phase=Phase.COMPATIBILITY,
            after=frozenset({"replace_ding_commands"}),
            idempotent=True,
        ),
        PassSpec(
            name="normalize_input_extensions",
            planner=plan_inputs,
            safety=Safety.SAFE,
            after=frozenset({"discover", "translate"}),
            **common,
        ),
    )


@dataclass(frozen=True)
class PreprocessingState:
    snapshot: DocumentSnapshot
    discovery: DiscoveryFacts | None = None
    resources: ResourceResult = ResourceResult(frozenset(), ())
    diagnostics: tuple[Diagnostic, ...] = ()


StepRunner = Callable[[PreprocessingState], PreprocessingState]


@dataclass(frozen=True)
class PreprocessingStep:
    name: str
    run: StepRunner
    after: frozenset[str] = frozenset()
    phase: Phase = Phase.COMPATIBILITY
    safety: Safety = Safety.SAFE


@dataclass(frozen=True)
class PreprocessingPlan:
    steps: tuple[PreprocessingStep, ...]


@dataclass(frozen=True)
class PreprocessingResult:
    snapshot: DocumentSnapshot
    discovery: DiscoveryFacts
    resources: ResourceResult
    diagnostics: tuple[Diagnostic, ...]


def resolve_preprocessing_steps(
    steps: Iterable[PreprocessingStep],
) -> tuple[PreprocessingStep, ...]:
    items = tuple(steps)
    by_name: dict[str, PreprocessingStep] = {}
    for step in items:
        if step.name in by_name:
            raise PassDependencyError(f"duplicate pass name: {step.name}")
        by_name[step.name] = step
    edges = {name: set() for name in by_name}
    indegree = {name: 0 for name in by_name}
    for step in items:
        for dependency in step.after:
            if dependency not in by_name:
                raise PassDependencyError(
                    f"missing pass dependency for {step.name}: {dependency}"
                )
            edges[dependency].add(step.name)
            indegree[step.name] += 1
    position = {step.name: index for index, step in enumerate(items)}
    ready = sorted(
        (name for name, degree in indegree.items() if degree == 0),
        key=position.__getitem__,
    )
    ordered: list[PreprocessingStep] = []
    while ready:
        name = ready.pop(0)
        ordered.append(by_name[name])
        for dependent in sorted(edges[name], key=position.__getitem__):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
                ready.sort(key=position.__getitem__)
    if len(ordered) != len(items):
        blocked = sorted(name for name, degree in indegree.items() if degree)
        raise PassDependencyError(
            f"pass dependency cycle involving: {', '.join(blocked)}"
        )
    return tuple(ordered)


def _step_from_pass(spec: PassSpec) -> PreprocessingStep:
    def run(state: PreprocessingState) -> PreprocessingState:
        executed = _execute_pass(
            state.snapshot, spec,
            read_only_mapping({
                Fact.SYNTAX: rebuild_syntax,
                Fact.DISCOVERY: build_discovery_facts,
            }),
        )
        if executed is None:
            return state
        snapshot, _, diagnostics = executed
        return replace(
            state, snapshot=snapshot, discovery=snapshot.discovery,
            diagnostics=state.diagnostics + diagnostics,
        )
    return PreprocessingStep(
        spec.name, run, phase=spec.phase, safety=spec.safety,
    )


def _complete_pass(
    name: str,
    planner: PassPlanner,
    safety: Safety = Safety.SAFE,
    phase: Phase = Phase.SAFE_NORMALIZATION,
) -> PassSpec:
    return PassSpec(
        name=name, planner=planner, phase=phase,
        safety=safety, requires=frozenset({Fact.SYNTAX, Fact.DISCOVERY}),
        invalidates=frozenset(set(Fact) - {Fact.DISCOVERY}),
        implementation=Implementation.SYNTAX_AWARE, idempotent=True,
    )


def build_preprocessing_plan(
    translate: bool,
    *,
    translation_client=None,
    translator: SourceTranslator | None = None,
    resource_converter: Callable[[Path], ResourceResult] = convert_pdf_resources,
    tikz_pdf: Path | None = None,
) -> PreprocessingPlan:
    """Assemble the one authoritative, transactionally executed pipeline."""
    steps: list[PreprocessingStep] = []

    def discover(state: PreprocessingState) -> PreprocessingState:
        snapshot = build_discovery_facts(state.snapshot)
        return replace(state, snapshot=snapshot, discovery=snapshot.discovery)

    steps.append(PreprocessingStep(
        "discover", discover, phase=Phase.DISCOVERY,
    ))

    def convert_tikz(state: PreprocessingState) -> PreprocessingState:
        if tikz_pdf is None:
            return state
        snapshot = state.snapshot
        if Fact.SYNTAX not in snapshot.current_facts:
            snapshot = rebuild_syntax(snapshot)
        if snapshot.discovery is None:
            snapshot = build_discovery_facts(snapshot)
        outcome, diagnostics = convert_tikz_resources(snapshot, tikz_pdf)
        if not outcome.edits:
            return replace(
                state,
                snapshot=snapshot,
                diagnostics=state.diagnostics + diagnostics,
            )
        grouped: dict[Path, list[Edit]] = {}
        for edit in outcome.edits:
            grouped.setdefault(edit.file, []).append(edit)
        sources = dict(snapshot.sources)
        for path, edits in grouped.items():
            sources[path] = SourceFile(
                path, EditPlanner.apply(snapshot.sources[path], edits),
            )
        snapshot = replace(
            snapshot,
            revision=snapshot.revision + 1,
            sources=read_only_mapping(sources),
            documents=read_only_mapping({}),
            current_facts=snapshot.current_facts - {Fact.SYNTAX},
        )
        return replace(
            state,
            snapshot=snapshot,
            diagnostics=state.diagnostics + diagnostics + outcome.diagnostics,
        )

    steps.append(PreprocessingStep(
        "convert_tikz_resources", convert_tikz,
        phase=Phase.COMPATIBILITY, safety=Safety.LOSSY,
    ))
    steps.extend(_step_from_pass(replace(
        spec, after=frozenset(), before=frozenset(),
        phase=Phase.COMPATIBILITY,
    ))
                 for spec in build_initial_preprocessing_passes())

    def convert(state: PreprocessingState) -> PreprocessingState:
        resources = resource_converter(state.snapshot.root)
        return replace(
            state, resources=resources,
            diagnostics=state.diagnostics + resources.diagnostics,
        )

    steps.append(PreprocessingStep("convert_pdf_resources", convert))

    def dynamic_pdf(state: PreprocessingState) -> PreprocessingState:
        spec = replace(
            build_reference_passes(state.resources)[0],
            after=frozenset(), before=frozenset(), phase=Phase.COMPATIBILITY,
        )
        return _step_from_pass(spec).run(state)

    steps.append(PreprocessingStep("rewrite_pdf_image_refs", dynamic_pdf))
    reference_specs = build_reference_passes(ResourceResult(frozenset(), ()))
    for spec in reference_specs[1:3]:
        steps.append(_step_from_pass(replace(
            spec, after=frozenset(), phase=Phase.COMPATIBILITY,
        )))

    table_by_old_name = {spec.name: spec for spec in build_table_float_passes()}
    table_steps = (
        ("normalize_table_envs", "normalize_tables"),
        ("unwrap_makecell", "unwrap_makecell"),
        ("rewrite_captionof", "resolve_captionof"),
        ("strip_minipage_in_tables", "unwrap_minipages"),
    )
    for name, old_name in table_steps:
        steps.append(_step_from_pass(replace(
            table_by_old_name[old_name], name=name, after=frozenset(),
            phase=Phase.COMPATIBILITY,
        )))
    # These concerns are deliberately folded into plan_normalize_tables.
    for name in ("strip_at_col_specs", "normalize_siunitx_columns"):
        steps.append(PreprocessingStep(name, lambda state: state))
    for name, old_name in (
        ("strip_resizebox", "unwrap_resizebox"),
        ("strip_adjustbox", "unwrap_adjustbox"),
        ("convert_wrapfigure", "convert_wrapfigures"),
        ("unwrap_subfigures", "unwrap_subfigures"),
        ("destar_floats", "destar_floats"),
    ):
        steps.append(_step_from_pass(replace(
            table_by_old_name[old_name], name=name, after=frozenset(),
            phase=Phase.COMPATIBILITY,
        )))
    for spec in reference_specs[3:5]:
        steps.append(_step_from_pass(replace(
            spec, after=frozenset(), phase=Phase.COMPATIBILITY,
        )))

    def theorem_plan(snapshot: DocumentSnapshot) -> PlanOutcome:
        if snapshot.discovery is None:
            raise PipelineContractError("theorem preprocessing requires discovery")
        return plan_theorems(snapshot, snapshot.discovery)

    steps.append(_step_from_pass(_complete_pass(
        "preprocess_theorems", theorem_plan,
        safety=Safety.LOSSY, phase=Phase.COMPATIBILITY,
    )))
    steps.append(_step_from_pass(make_syntax_file_pass(
        name="normalize_code_listings", planner=plan_code_listings,
        safety=Safety.LOSSY, phase=Phase.COMPATIBILITY, idempotent=True,
    )))
    steps.append(_step_from_pass(_complete_pass(
        "preprocess_algorithms", plan_algorithms,
        safety=Safety.LOSSY, phase=Phase.COMPATIBILITY,
    )))

    if translate:
        def translate_step(state: PreprocessingState) -> PreprocessingState:
            snapshot = state.snapshot
            if Fact.SYNTAX not in snapshot.current_facts:
                snapshot = rebuild_syntax(snapshot)
            source_translator = translator
            if source_translator is None and translation_client is None:
                raise PipelineContractError(
                    "translation requires a client or source translator"
                )
            if source_translator is None:
                facts = snapshot.discovery
                if facts is None:
                    raise PipelineContractError("translation requires discovery")
                headings = extract_snapshot_section_headings(snapshot)
                glossary = extract_glossary(
                    translation_client, facts.title, facts.abstract, headings,
                )
                translated_headings = _build_heading_translations(
                    translation_client, glossary, headings,
                )
                request_limiter = threading.BoundedSemaphore(
                    TRANSLATION_BATCH_WORKERS,
                )
                source_translator = lambda source, ignored: translate_file_content(
                    translation_client, glossary, translated_headings,
                    source.content, request_limiter,
                )
            barrier = TranslationBarrier(source_translator).run(snapshot)
            return replace(
                state, snapshot=barrier.snapshot,
                discovery=barrier.snapshot.discovery,
                diagnostics=state.diagnostics + barrier.diagnostics,
            )
        steps.append(PreprocessingStep(
            "translate", translate_step,
            phase=Phase.COMPATIBILITY, safety=Safety.LOSSY,
        ))

    steps.append(_step_from_pass(make_syntax_file_pass(
        name="unnumber_paragraph_headings", planner=plan_unnumber_paragraphs,
        safety=Safety.LOSSY, phase=Phase.COMPATIBILITY, idempotent=True,
    )))
    steps.append(_step_from_pass(_complete_pass(
        "preserve_appendix_numbering",
        plan_preserve_appendix_numbering,
        phase=Phase.COMPATIBILITY,
    )))

    def inputs(snapshot: DocumentSnapshot) -> PlanOutcome:
        if snapshot.discovery is None:
            raise PipelineContractError("input normalization requires discovery")
        return _combine_outcomes(
            plan_normalize_inputs(
                snapshot.sources[path], snapshot.documents[path],
                snapshot.discovery,
            )
            for path in snapshot.sources
        )

    steps.append(_step_from_pass(_complete_pass(
        "normalize_input_extensions", inputs, phase=Phase.COMPATIBILITY,
    )))
    chained = tuple(
        replace(step, after=frozenset({steps[index - 1].name}) if index else frozenset())
        for index, step in enumerate(steps)
    )
    return PreprocessingPlan(chained)


def _diagnostic_key(diagnostic: Diagnostic) -> tuple:
    return (
        str(diagnostic.file), diagnostic.pass_name,
        -1 if diagnostic.start is None else diagnostic.start,
        -1 if diagnostic.end is None else diagnostic.end,
        diagnostic.code, diagnostic.message,
    )


def run_preprocessing(
    paper_dir: Path,
    main_tex: Path,
    plan: PreprocessingPlan,
) -> PreprocessingResult:
    steps = resolve_preprocessing_steps(plan.steps)
    initial = DocumentSnapshot.from_directory(paper_dir, main_tex)
    state = PreprocessingState(initial, None, ResourceResult(frozenset(), ()))
    for step in steps:
        state = step.run(state)
    snapshot = state.snapshot
    if snapshot.discovery is None or Fact.DISCOVERY not in snapshot.current_facts:
        snapshot = build_discovery_facts(snapshot)
    diagnostics = tuple(sorted(state.diagnostics, key=_diagnostic_key))
    for path in sorted(snapshot.sources):
        if snapshot.sources[path].content != initial.sources[path].content:
            _write_source_content(path, snapshot.sources[path].content)
    for diagnostic in diagnostics:
        print(
            f"{diagnostic.pass_name}: {diagnostic.message}",
            file=sys.stderr,
        )
    if snapshot.discovery is None:
        raise PipelineContractError("complete preprocessing requires discovery")
    return PreprocessingResult(
        snapshot, snapshot.discovery, state.resources, diagnostics,
    )


def _write_source_content(path: Path, content: str) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        stream.write(content)


def download(url: str, dest: Path) -> None:
    subprocess.run(["curl", "-L", "--fail", url, "-o", str(dest)], check=True)



def _discovery_root(discovery: DiscoveryFacts, source_path: Path) -> Path:
    if not discovery.include_order:
        return source_path.parent
    return Path(os.path.commonpath(
        [str(path.resolve()) for path in discovery.include_order]
    )).parent if len(discovery.include_order) == 1 else Path(os.path.commonpath(
        [str(path.resolve().parent) for path in discovery.include_order]
    ))


def _resolve_discovered_input(
    source_path: Path,
    argument: str,
    discovery: DiscoveryFacts,
) -> Path | None:
    target_name = argument if argument.endswith(".tex") else f"{argument}.tex"
    discovered = {path.resolve(): path for path in discovery.include_order}
    candidates = (
        (source_path.parent / target_name).resolve(),
        (_discovery_root(discovery, source_path) / target_name).resolve(),
    )
    for candidate in candidates:
        if candidate in discovered:
            return discovered[candidate]
    return None


def plan_normalize_inputs(
    source: SourceFile,
    document: LatexDocument,
    discovery: DiscoveryFacts,
) -> PlanOutcome:
    outcomes: list[PlanOutcome] = []
    for command in ("input", "include"):
        for ref in document.commands(command):
            issue = _reference_issue(source, ref, "normalize_input_extensions")
            if issue is not None:
                outcomes.append(issue)
                continue
            argument = document.argument(ref, 0)
            if argument is None or not argument.complete or argument.opaque:
                outcomes.append(_opaque_structure_outcome(
                    source, ref, "normalize_input_extensions",
                    f"preserved incomplete argument of {command}",
                ))
                continue
            target = argument.text.strip()
            if target.endswith(".tex"):
                continue
            if _resolve_discovered_input(source.path, target, discovery) is None:
                continue
            content_start = argument.start + len(argument.opening_delimiter or "")
            content_end = argument.end - len(argument.closing_delimiter or "")
            outcomes.append(PlanOutcome(edits=(Edit(
                source.path,
                content_start,
                content_end,
                f"{target}.tex",
                "normalize_input_extensions",
                Safety.SAFE,
            ),)))
    return _combine_outcomes(outcomes)


def _pandoc_resource_paths(
    cwd: Path,
    discovery: DiscoveryFacts | None = None,
) -> list[str]:
    paths = [".", "figures", "images"]
    if discovery is None:
        main_tex = find_main_tex(cwd)
        snapshot = build_discovery_facts(
            DocumentSnapshot.from_directory(cwd, main_tex)
        )
        discovery = snapshot.discovery
    if discovery is None:
        raise PipelineContractError("pandoc resources require discovery facts")
    discovered_paths = discovery.graphicspaths
    for path in discovered_paths:
        if path not in paths:
            paths.append(path)
    return paths


def _numbering_scope(source: SourceFile) -> str:
    """Return the LaTeX numbered-object scope retained by this source."""
    document = LatexDocument(source)
    for ref in document.commands("chapter"):
        star = ref.arguments[0] if ref.arguments else None
        if ref.complete and not ref.opaque and star is None:
            return "chapter"
    return "global"


_NUMBERED_MATH_ENVIRONMENT = re.compile(
    r"\\begin\s*\{(?:align|equation|eqnarray|gather|multline)\*?\}"
)


def _subequation_label_groups(source: SourceFile) -> tuple[tuple[str, ...], ...]:
    """Collect each subequations parent label followed by its child labels."""
    document = LatexDocument(source)
    groups: list[tuple[str, ...]] = []
    for environment in document.environments("subequations"):
        if not environment.complete or environment.opaque:
            continue
        labels = [
            (ref, document.argument_text(ref, 0))
            for ref in document.commands("label")
            if environment.body_start <= ref.start < ref.end <= environment.body_end
            and ref.complete
            and not ref.opaque
        ]
        labels = [(ref, label.strip()) for ref, label in labels if label]
        if not labels:
            continue
        body = source.content[environment.body_start:environment.body_end]
        nested_math = _NUMBERED_MATH_ENVIRONMENT.search(body)
        nested_start = (
            environment.body_start + nested_math.start()
            if nested_math is not None
            else environment.body_start
        )
        parent = labels[0][1] if labels[0][0].start < nested_start else ""
        children = tuple(
            label for ref, label in labels if ref.start >= nested_start
        )
        if children:
            groups.append((parent, *children))
    return tuple(groups)


def _pandoc_numbering_scope(
    main_tex: Path,
    discovery: DiscoveryFacts | None,
) -> str:
    paths = discovery.include_order if discovery is not None else (main_tex,)
    if main_tex not in paths:
        paths = (main_tex, *paths)
    return "chapter" if any(
        _numbering_scope(SourceFile.from_path(path)) == "chapter"
        for path in paths
    ) else "global"


def _pandoc_subequation_groups(
    main_tex: Path,
    discovery: DiscoveryFacts | None,
) -> str:
    paths = discovery.include_order if discovery is not None else (main_tex,)
    if main_tex not in paths:
        paths = (main_tex, *paths)
    groups = (
        group
        for path in paths
        for group in _subequation_label_groups(SourceFile.from_path(path))
    )
    return ";".join("|".join(group) for group in groups)

def run_pandoc(
    main_tex: Path,
    output: Path,
    title: str | None,
    authors: list[str] | None = None,
    *,
    workdir: Path | None = None,
    discovery: DiscoveryFacts | None = None,
) -> None:
    cwd = workdir or main_tex.parent
    input_path = os.path.relpath(main_tex, cwd)
    args = [
        "pandoc",
        input_path,
        "--mathml",
        "--from",
        "latex",
        "--to",
        "epub3",
        "--standalone",
        "--toc",
        "--number-sections",
        "--metadata",
        "paper2epub-numbering-scope="
        f"{_pandoc_numbering_scope(main_tex, discovery)}",
        "--metadata",
        f"paper2epub-subequations={_pandoc_subequation_groups(main_tex, discovery)}",
        f"--resource-path={':'.join(_pandoc_resource_paths(cwd, discovery))}",
        f"--css={SCRIPT_DIR / 'epub.css'}",
        f"--lua-filter={SCRIPT_DIR / 'filter.lua'}",
    ]
    if title:
        args += ["--metadata", f"title={title}"]
    if authors:
        for author in authors:
            args += ["--metadata", f"author={author}"]
    args += ["-o", str(output)]

    subprocess.run(args, cwd=cwd, check=True)


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------


def send_email(epub_path: Path, title: str | None, arxiv_id: str) -> None:
    missing = [
        v for v in ("EMAIL_FROM", "EMAIL_TO", "EMAIL_PASSWORD") if not os.environ.get(v)
    ]
    if missing:
        print(
            f"Error: missing environment variables for --email: {', '.join(missing)}",
            file=sys.stderr,
        )
        sys.exit(1)

    email_from = os.environ["EMAIL_FROM"]
    email_to = os.environ["EMAIL_TO"]
    email_password = os.environ["EMAIL_PASSWORD"]
    smtp_host = os.environ.get("SMTP_SSL_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_SSL_PORT", "465"))

    msg = EmailMessage()
    msg["Subject"] = f"[paper2epub] {title or arxiv_id} ({arxiv_id})"
    msg["From"] = email_from
    msg["To"] = email_to
    msg.set_content(f"EPUB for arXiv paper {arxiv_id} is attached.")
    msg.add_attachment(
        epub_path.read_bytes(),
        maintype="application",
        subtype="epub+zip",
        filename=epub_path.name,
    )

    proxy_url = os.environ.get("SMTP_PROXY")
    orig_socket = socket.socket
    if proxy_url:
        import socks

        parsed = urlparse(proxy_url)
        proxy_host = parsed.hostname
        proxy_port = parsed.port or 1080
        proxy_user = parsed.username
        proxy_pass = parsed.password
        socks.set_default_proxy(
            socks.SOCKS5, proxy_host, proxy_port,
            username=proxy_user, password=proxy_pass,
        )
        socket.socket = socks.socksocket
        print(f"Sending {epub_path.name} to {email_to} via {smtp_host}:{smtp_port} (proxy {proxy_host}:{proxy_port}) ...")
    else:
        print(f"Sending {epub_path.name} to {email_to} via {smtp_host}:{smtp_port} ...")

    try:
        with smtplib.SMTP_SSL(smtp_host, smtp_port) as server:
            server.login(email_from, email_password)
            server.send_message(msg)
    finally:
        if proxy_url:
            socket.socket = orig_socket
    print("Email sent.")


def main():
    parser = argparse.ArgumentParser(description="Convert an arXiv paper to EPUB")
    parser.add_argument("arxiv_id", help="arXiv paper ID (e.g. 2402.08954)")
    parser.add_argument(
        "--translate",
        action="store_true",
        help="Translate to Chinese using Qwen3.6-Flash (requires DASHSCOPE_API_KEY)",
    )
    parser.add_argument(
        "--email",
        action="store_true",
        help="Send the EPUB via email (requires EMAIL_PASSWORD, EMAIL_FROM, EMAIL_TO; optional SMTP_PROXY for SOCKS5)",
    )
    args = parser.parse_args()

    if args.translate and not os.environ.get("DASHSCOPE_API_KEY"):
        print(
            "Error: DASHSCOPE_API_KEY environment variable is required for --translate",
            file=sys.stderr,
        )
        sys.exit(1)

    arxiv_id = args.arxiv_id
    paper_dir = Path("paper")
    tarball = Path("paper.tar.gz")

    if paper_dir.exists():
        shutil.rmtree(paper_dir)
    tarball.unlink(missing_ok=True)

    url = f"https://arxiv.org/src/{arxiv_id}"
    print(f"Downloading {url} ...")
    download(url, tarball)

    paper_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tarball) as tar:
        tar.extractall(path=paper_dir, filter="data")

    main_tex = find_main_tex(paper_dir)
    print(f"Using TeX file: {main_tex}")

    tikz_pdf: Path | None = None
    if any(
        r"\begin{tikzpicture}" in SourceFile.from_path(path).content
        for path in paper_dir.rglob("*.tex")
    ):
        pdf_key = hashlib.sha256(arxiv_id.encode()).hexdigest()[:16]
        tikz_pdf = Path("/tmp") / f"paper2epub-{pdf_key}.pdf"
        pdf_url = f"https://arxiv.org/pdf/{arxiv_id}"
        print(f"Downloading {pdf_url} for TikZ figures ...")
        try:
            download(pdf_url, tikz_pdf)
        except subprocess.CalledProcessError as error:
            print(
                f"Warning: could not download PDF for TikZ figures: {error}",
                file=sys.stderr,
            )
            tikz_pdf = None

    client = create_openai_client() if args.translate else None
    preprocessing = run_preprocessing(
        paper_dir,
        main_tex,
        build_preprocessing_plan(
            args.translate,
            translation_client=client,
            tikz_pdf=tikz_pdf,
        ),
    )
    discovery = preprocessing.discovery

    title = discovery.title
    if title:
        print(f"Paper title: {title}")
    authors = list(discovery.authors)
    if authors:
        print(f"Authors: {', '.join(authors)}")

    suffix = "-zh" if args.translate else ""
    output = Path.cwd() / f"{arxiv_id}{suffix}.epub"
    run_pandoc(
        main_tex,
        output,
        title,
        authors,
        workdir=paper_dir,
        discovery=discovery,
    )
    print(f"Generated: {output}")

    if args.email:
        send_email(output, title, arxiv_id)


if __name__ == "__main__":
    main()
