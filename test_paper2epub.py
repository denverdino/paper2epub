"""Unit tests for paper2epub.py — pure-function coverage, no network/API calls."""

from dataclasses import FrozenInstanceError, replace
import re
import shutil
import subprocess
import sys
import threading

import pytest
from pathlib import Path

import paper2epub as p


class TestSourceAndEditTypes:
    def test_source_file_replaces_invalid_utf8(self, tmp_path):
        path = tmp_path / "main.tex"
        path.write_bytes(b"before " + bytes([0xFF]) + b" after")

        source = p.SourceFile.from_path(path)

        assert source.path == path
        assert source.content == "before \ufffd after"

    def test_edit_is_immutable(self, tmp_path):
        edit = p.Edit(
            file=tmp_path / "main.tex",
            start=0,
            end=3,
            replacement="new",
            pass_name="example",
            safety=p.Safety.SAFE,
        )

        with pytest.raises(AttributeError):
            edit.start = 1


class TestPassSpec:
    def test_pass_spec_records_declarative_contract(self):
        planner = lambda snapshot: []

        spec = p.PassSpec(
            name="normalize",
            planner=planner,
            phase=p.Phase.SAFE_NORMALIZATION,
            safety=p.Safety.SAFE,
            requires=frozenset({p.Fact.SYNTAX}),
            invalidates=frozenset({p.Fact.SYNTAX}),
            after=frozenset({"discover"}),
            report_label="Normalized",
        )

        assert spec.planner is planner
        assert spec.requires == frozenset({p.Fact.SYNTAX})
        assert spec.implementation is p.Implementation.SYNTAX_AWARE
        assert spec.idempotent is False

    def test_pass_spec_is_immutable(self):
        spec = p.PassSpec(
            name="normalize",
            planner=lambda snapshot: [],
            phase=p.Phase.SAFE_NORMALIZATION,
            safety=p.Safety.SAFE,
            requires=frozenset(),
            invalidates=frozenset(),
        )

        with pytest.raises(FrozenInstanceError):
            spec.name = "changed"

    def test_phase_order_is_explicit(self):
        assert p.Phase.DISCOVERY < p.Phase.SAFE_NORMALIZATION
        assert p.Phase.SAFE_NORMALIZATION < p.Phase.COMPATIBILITY


class TestLatexDocument:
    def test_exposes_complete_inline_and_display_math_ranges(self, tmp_path):
        content = r"Before $x^2$ and \[\begin{cases}a&b\end{cases}\] after"
        document = p.LatexDocument(
            p.SourceFile(tmp_path / "main.tex", content),
        )

        assert [content[ref.start:ref.end] for ref in document.math] == [
            r"$x^2$", r"\[\begin{cases}a&b\end{cases}\]",
        ]
        assert [ref.display for ref in document.math] == [False, True]
        assert all(ref.complete and not ref.opaque for ref in document.math)

    def test_incomplete_math_is_opaque(self, tmp_path):
        content = r"Before \[unfinished"
        document = p.LatexDocument(
            p.SourceFile(tmp_path / "main.tex", content),
        )

        assert len(document.math) == 1
        assert document.math[0].complete is False
        assert document.math[0].opaque is True

    def test_finds_nested_commands_with_source_ranges(self, tmp_path):
        content = r"Before \section{A \textbf{nested} title} after"
        source = p.SourceFile(tmp_path / "main.tex", content)

        document = p.LatexDocument(source)

        section = document.commands("section")[0]
        bold = document.commands("textbf")[0]
        assert document.source_text(section) == r"\section{A \textbf{nested} title}"
        assert document.argument_text(section, 0) == r"A \textbf{nested} title"
        assert document.argument_text(bold, 0) == "nested"
        assert section.start < bold.start < bold.end <= section.end

    def test_finds_environment_and_parent(self, tmp_path):
        content = (
            r"\begin{figure}"
            r"\includegraphics[width=.8\linewidth]{figs/a.pdf}"
            r"\end{figure}"
        )
        source = p.SourceFile(tmp_path / "main.tex", content)

        document = p.LatexDocument(source)

        figure = document.environments("figure")[0]
        image = document.commands("includegraphics")[0]
        assert document.source_text(figure) == content
        assert image.parent_environment == "figure"
        assert document.argument_text(image, 0) == r"width=.8\linewidth"
        assert document.argument_text(image, 1) == "figs/a.pdf"

    def test_missing_optional_argument_returns_none(self, tmp_path):
        source = p.SourceFile(
            tmp_path / "main.tex",
            r"\includegraphics{figs/a.pdf}",
        )

        document = p.LatexDocument(source)
        image = document.commands("includegraphics")[0]

        assert document.argument_text(image, 0) is None
        assert document.argument_text(image, 1) == "figs/a.pdf"

    def test_starred_resizebox_exposes_parser_owned_arguments(self, tmp_path):
        content = r"\resizebox*{1cm}{!}{nested {body}}"
        source = p.SourceFile(tmp_path / "main.tex", content)

        document = p.LatexDocument(source)
        ref = document.commands("resizebox")[0]

        assert document.source_text(ref) == content
        assert len(ref.arguments) == 4
        assert ref.arguments[0] is not None
        assert ref.arguments[0].text == "*"
        assert [document.argument_text(ref, index) for index in range(3)] == [
            "1cm", "!", "nested {body}",
        ]

    def test_complete_arguments_expose_stable_ranges_and_metadata(self, tmp_path):
        content = r"\includegraphics[width=.8\linewidth]{figs/a.pdf}"
        document = p.LatexDocument(
            p.SourceFile(tmp_path / "main.tex", content),
        )

        image = document.commands("includegraphics")[0]
        optional = document.argument(image, 0)
        required = document.argument(image, 1)

        assert optional is not None
        assert required is not None
        assert content[optional.start:optional.end] == r"[width=.8\linewidth]"
        assert content[required.start:required.end] == r"{figs/a.pdf}"
        assert optional.text == r"width=.8\linewidth"
        assert required.text == "figs/a.pdf"
        assert optional.complete is True
        assert required.complete is True
        assert optional.opaque is False
        assert required.opaque is False
        assert image.complete is True
        assert image.opaque is False
        assert image.command_token_end == len(r"\includegraphics")
        assert image.command_post_space_end == image.command_token_end
        assert not hasattr(image, "node")

    def test_incomplete_required_group_preserves_text_and_is_opaque(self, tmp_path):
        content = r"text \section{unfinished"
        source = p.SourceFile(tmp_path / "main.tex", content)

        document = p.LatexDocument(source)
        section = document.commands("section")[0]
        title = document.argument(section, 0)

        assert title is not None
        assert title.text == "unfinished"
        assert document.argument_text(section, 0) == "unfinished"
        assert title.complete is False
        assert title.opaque is True
        assert section.complete is False
        assert section.opaque is True
        assert document.source_text(section) == r"\section{unfinished"

    def test_incomplete_optional_group_preserves_text_and_is_opaque(self, tmp_path):
        content = r"\includegraphics[width=.8\linewidth"
        document = p.LatexDocument(
            p.SourceFile(tmp_path / "main.tex", content),
        )

        image = document.commands("includegraphics")[0]
        optional = document.argument(image, 0)

        assert optional is not None
        assert optional.text == r"width=.8\linewidth"
        assert document.argument_text(image, 0) == r"width=.8\linewidth"
        assert optional.complete is False
        assert optional.opaque is True
        assert image.complete is False
        assert image.opaque is True

    def test_nested_command_in_incomplete_group_inherits_opaque(self, tmp_path):
        content = r"\section{before \textbf{nested}"
        document = p.LatexDocument(
            p.SourceFile(tmp_path / "main.tex", content),
        )

        section = document.commands("section")[0]
        bold = document.commands("textbf")[0]

        assert section.opaque is True
        assert bold.complete is True
        assert bold.opaque is True

    def test_node_after_malformed_optional_group_inherits_opaque(self, tmp_path):
        content = "\\documentclass[12pt\n\\maketitle\nBody"
        document = p.LatexDocument(
            p.SourceFile(tmp_path / "main.tex", content),
        )

        documentclass = document.commands("documentclass")[0]
        maketitle = document.commands("maketitle")[0]

        assert documentclass.opaque is True
        assert maketitle.complete is True
        assert maketitle.opaque is True

    def test_starred_section_preserves_full_source_and_required_title(self, tmp_path):
        content = r"\section*{Long}"
        document = p.LatexDocument(p.SourceFile(tmp_path / "main.tex", content))

        section = document.commands("section")[0]

        assert document.source_text(section) == content
        assert document.argument_text(section, 0) == "Long"

    def test_optional_section_preserves_full_source_and_required_title(self, tmp_path):
        content = r"\section[Short]{Long}"
        document = p.LatexDocument(p.SourceFile(tmp_path / "main.tex", content))

        section = document.commands("section")[0]

        assert document.source_text(section) == content
        assert document.argument_text(section, 0) == "Long"


class TestLatexEnvironmentContract:
    def test_exposes_nested_environment_body_ranges(self, tmp_path):
        content = (
            r"A\begin{comment}outer "
            r"\begin{comment}inner\end{comment} tail\end{comment}Z"
        )
        source = p.SourceFile(tmp_path / "main.tex", content)
        document = p.LatexDocument(source)
        outer, inner = document.environments("comment")

        assert document.source_text(outer) == content[1:-1]
        assert source.content[outer.body_start:outer.body_end] == (
            r"outer \begin{comment}inner\end{comment} tail"
        )
        assert outer.start < inner.start < inner.end < outer.end
        assert source.content[outer.end_token_start:outer.end] == r"\end{comment}"

    def test_unclosed_environment_is_opaque_and_diagnostic(self, tmp_path):
        source = p.SourceFile(
            tmp_path / "main.tex", r"\begin{comment}unfinished"
        )
        document = p.LatexDocument(source)
        ref = document.environments("comment")[0]

        assert not ref.complete
        assert ref.opaque
        assert any(d.code == "incomplete-environment" for d in document.diagnostics)

    def test_unmatched_optional_environment_argument_propagates_opacity(
        self, tmp_path
    ):
        content = (
            r"\begin{minipage}[t{.5\linewidth}"
            r"before \textbf{inside}\end{minipage}"
        )
        document = p.LatexDocument(
            p.SourceFile(tmp_path / "main.tex", content),
        )

        minipage = document.environments("minipage")[0]
        bold = document.commands("textbf")[0]

        assert minipage.complete is False
        assert minipage.opaque is True
        assert bold.complete is True
        assert bold.opaque is True

    def test_valid_minipage_does_not_reuse_earlier_optional_argument(self, tmp_path):
        content = (
            r"\begin{minipage}[t]{.5\linewidth}"
            r"body\end{minipage}"
        )
        document = p.LatexDocument(
            p.SourceFile(tmp_path / "main.tex", content),
        )

        minipage = document.environments("minipage")[0]

        assert minipage.complete is True
        assert minipage.opaque is False
        assert (
            minipage.begin_token_end
            <= minipage.body_start
            <= minipage.body_end
            <= minipage.end_token_start
        )


class TestMaskDefinitionEnvironmentTokens:
    def test_masks_complete_cross_argument_environment_without_moving_offsets(self):
        content = (
            r"\newenvironment{dedication}"
            r"{before \begin{minipage}[t]{1in} after}"
            r"{before \end{minipage} after}"
            r"\begin{document}Body\end{document}"
        )
        masked = p._mask_definition_environment_tokens(content)
        assert len(masked) == len(content)
        assert masked.index(r"\begin{document}") == content.index(r"\begin{document}")
        assert r"\begin{minipage}" not in masked
        assert r"\end{minipage}" not in masked

    @pytest.mark.parametrize("newline", ["\n", "\r\n"])
    def test_masks_comment_separated_tokens_without_moving_offsets(self, newline):
        content = (
            r"\newenvironment{dedication}"
            + "{" + rf"\begin% begin note{newline}{{quote}}" + "}"
            + "{" + rf"\end% end note{newline}{{quote}}" + "}"
            + r"\begin{document}Body\end{document}"
        )

        masked = p._mask_definition_environment_tokens(content)

        assert len(masked) == len(content)
        assert masked.index(r"\begin{document}") == content.index(
            r"\begin{document}"
        )
        assert r"\begin% begin note" not in masked
        assert r"\end% end note" not in masked

    @pytest.mark.parametrize("newline", ["\n", "\r\n"])
    def test_comment_separated_mask_preserves_exact_newline_positions(self, newline):
        content = (
            r"\newenvironment{x}"
            + "{" + r"before \begin% note" + newline + "{quote} after}"
            + "{" + r"before \end% note" + newline + "{quote} after}"
        )

        masked = p._mask_definition_environment_tokens(content)

        assert tuple(
            index for index, character in enumerate(masked)
            if character in "\r\n"
        ) == tuple(
            index for index, character in enumerate(content)
            if character in "\r\n"
        )
        assert masked.count("\r") == content.count("\r")
        assert masked.count("\n") == content.count("\n")

    @pytest.mark.parametrize("newline", ["\n", "\r\n"])
    def test_mask_is_non_whitespace_and_preserves_newlines(self, newline):
        token = r"\begin% note" + newline + "{minipage}"
        content = (
            r"\newenvironment{x}{\hfill" + token + "}"
            r"{\end{minipage}}"
        )

        masked = p._mask_definition_environment_tokens(content)
        start = content.index(r"\begin")
        end = start + len(token)

        assert len(masked) == len(content)
        assert [
            index for index, character in enumerate(masked)
            if character in "\r\n"
        ] == [
            index for index, character in enumerate(content)
            if character in "\r\n"
        ]
        assert all(
            masked[index] == content[index]
            if content[index] in "\r\n"
            else masked[index] == p._DEFINITION_MASK_CHARACTER
            for index in range(start, end)
        )
        assert not p._DEFINITION_MASK_CHARACTER.isspace()

    @pytest.mark.parametrize(
        "content",
        [
            "% " + r"\newenvironment{x}{" + "\n" + r"\begin{quote}}{"
            + "\n" + r"\end{quote}}" + "\n"
            + r"\begin{document}Body\end{document}",
            "% " + r"\newcommand{\x}{" + "\r\n" + r"\begin{quote}}"
            + "\r\n"
            + r"\begin{document}Body\end{document}",
            r"\\newenvironment{x}{\begin{quote}}{\end{quote}}"
            r"\begin{document}Body\end{document}",
            r"\\newcommand{\x}{\begin{quote}}"
            r"\begin{document}Body\end{document}",
        ],
    )
    def test_commented_or_escaped_pseudo_definition_is_unchanged(self, content):
        assert p._mask_definition_environment_tokens(content) == content

    def test_incomplete_definition_is_not_partially_masked(self):
        content = r"\newenvironment{x}{\begin{quote}}{\end{quote}"
        assert p._mask_definition_environment_tokens(content) == content

    def test_malformed_braced_command_name_is_not_masked(self):
        content = r"\newcommand{not-a-command}{\begin{x}}"
        assert p._mask_definition_environment_tokens(content) == content

    def test_bare_newenvironment_name_is_not_masked(self):
        content = r"\newenvironment\x{\begin{x}}{\end{x}}"
        assert p._mask_definition_environment_tokens(content) == content

    def test_masked_definition_does_not_extend_hfill_source_range(
        self, tmp_path,
    ):
        content = (
            r"\newenvironment{x}{\hfill\begin{minipage}[t]{1in}}"
            r"{\end{minipage}}"
        )
        source = p.SourceFile(tmp_path / "main.tex", content)

        hfill = p.LatexDocument(source).commands("hfill")[0]

        assert hfill.end <= content.index(r"\begin{minipage}")

    def test_strip_noise_preserves_balanced_minipage_definition(self, tmp_path):
        content = (
            r"\newenvironment{dedication}"
            r"{\hfill\begin{minipage}[t]{1in}\raggedright}"
            r"{\end{minipage}\clearpage}"
        )
        source = p.SourceFile(tmp_path / "main.tex", content)

        outcome = p.plan_strip_noise(source, p.LatexDocument(source))
        result = p.EditPlanner.apply(source, outcome.edits)

        assert r"\hfill" not in result
        assert result.count(r"\begin{minipage}") == 1
        assert result.count(r"\end{minipage}") == 1


def test_definition_local_minipage_does_not_make_document_opaque(tmp_path):
    content = (
        r"\newenvironment{dedication}"
        r"{\begin{minipage}[t]{0.66\textwidth}}{\end{minipage}}"
        r"\title{Book}\author{Author}"
        r"\begin{document}\begin{proof}Proof prose.\end{proof}\end{document}"
    )
    source = p.SourceFile(tmp_path / "main.tex", content)
    document = p.LatexDocument(source)
    assert not document.environments("document")[0].opaque
    assert not document.environments("proof")[0].opaque
    assert not document.commands("title")[0].opaque
    assert not document.commands("author")[0].opaque


# ── Brace matching ──────────────────────────────────────────────────────────


class TestDocumentSnapshot:
    def test_from_directory_reads_and_parses_all_tex_files(self, tmp_path):
        main = tmp_path / "main.tex"
        section = tmp_path / "sections" / "intro.tex"
        section.parent.mkdir()
        main.write_text(r"\documentclass{article}\input{sections/intro}")
        section.write_text(r"\section{Intro}")

        snapshot = p.DocumentSnapshot.from_directory(tmp_path, main)

        assert snapshot.revision == 0
        assert tuple(snapshot.sources) == (main, section)
        assert snapshot.documents[main].commands("documentclass")
        assert snapshot.documents[section].commands("section")
        assert snapshot.current_facts == frozenset({p.Fact.SYNTAX})

    def test_snapshot_mappings_are_read_only(self, tmp_path):
        main = tmp_path / "main.tex"
        main.write_text(r"\documentclass{article}")
        snapshot = p.DocumentSnapshot.from_directory(tmp_path, main)

        with pytest.raises(TypeError):
            snapshot.sources[main] = p.SourceFile(main, "changed")

        with pytest.raises(TypeError):
            snapshot.documents[main] = p.LatexDocument(snapshot.sources[main])

    def test_rebuild_syntax_keeps_revision_and_refreshes_documents(self, tmp_path):
        main = tmp_path / "main.tex"
        source = p.SourceFile(main, r"\section{Fresh}")
        stale = p.DocumentSnapshot(
            root=tmp_path,
            main_tex=main,
            revision=3,
            sources=p.read_only_mapping({main: source}),
            documents=p.read_only_mapping({}),
            current_facts=frozenset(),
        )

        rebuilt = p.rebuild_syntax(stale)

        assert rebuilt.revision == 3
        assert rebuilt.documents[main].argument_text(
            rebuilt.documents[main].commands("section")[0], 0
        ) == "Fresh"
        assert rebuilt.current_facts == frozenset({p.Fact.SYNTAX})


class TestDiscoveryFacts:
    def test_captures_metadata_before_cleanup(self, tmp_path):
        main = tmp_path / "main.tex"
        main.write_text(
            r"\documentclass{article}"
            r"\newcommand{\paperword}{Robust}"
            r"\title{A \paperword{} Paper}"
            r"\author{Ada Lovelace}"
        )
        snapshot = p.DocumentSnapshot.from_directory(tmp_path, main)

        discovered = p.build_discovery_facts(snapshot)

        assert discovered.discovery.title == "A Robust Paper"
        assert discovered.discovery.authors == ("Ada Lovelace",)
        assert discovered.discovery.macros["paperword"] == p.MacroDefinition(
            "Robust"
        )

    def test_expands_macro_backed_authors_before_formatting(self, tmp_path):
        main = tmp_path / "main.tex"
        main.write_text(
            r"\newcommand{\paperauthors}{Ada Lovelace, Alan Turing}"
            r"\author{\paperauthors}"
        )

        facts = p.build_discovery_facts(
            p.DocumentSnapshot.from_directory(tmp_path, main)
        ).discovery

        assert facts.authors == ("Ada Lovelace", "Alan Turing")

    def test_formats_parser_owned_metadata_arguments(self, tmp_path):
        main = tmp_path / "main.tex"
        main.write_text(
            r"\title[Short]{Line One \\ Learning $\mathcal{F}$ Spaces}"
            r"\author{Alice, Bob \\ University}"
        )

        facts = p.build_discovery_facts(
            p.DocumentSnapshot.from_directory(tmp_path, main)
        ).discovery

        assert facts.title == "Line One Learning $F$ Spaces"
        assert facts.authors == ("Alice", "Bob")

    def test_title_drops_layout_declarations_and_publication_note(self, tmp_path):
        main = tmp_path / "main.tex"
        main.write_text(
            r"\newcommand{\blfootnote}[1]{#1}"
            r"\title{\Huge \textbf{Mathematical Foundations of Deep Learning}"
            r"\blfootnote{Draft version. Final version is published elsewhere.}"
            r"\\ {\LARGE Theory and Algorithms}}"
        )

        facts = p.build_discovery_facts(
            p.DocumentSnapshot.from_directory(tmp_path, main)
        ).discovery

        assert facts.title == (
            "Mathematical Foundations of Deep Learning Theory and Algorithms"
        )

    def test_title_drops_unregistered_blfootnote_argument(self, tmp_path):
        main = tmp_path / "main.tex"
        main.write_text(
            r"\title{Book\blfootnote{Publication note.}\\ Subtitle}"
        )

        facts = p.build_discovery_facts(
            p.DocumentSnapshot.from_directory(tmp_path, main)
        ).discovery

        assert facts.title == "Book Subtitle"

    def test_nested_macro_definition_is_not_truncated(self, tmp_path):
        main = tmp_path / "main.tex"
        main.write_text(r"\newcommand{\loss}{\textbf{L}_{\mathrm{nested}}}")

        facts = p.build_discovery_facts(
            p.DocumentSnapshot.from_directory(tmp_path, main)
        ).discovery

        assert facts.macros["loss"] == p.MacroDefinition(
            r"\textbf{L}_{\mathrm{nested}}"
        )

    def test_collects_supported_complete_macro_declarations(self, tmp_path):
        main = tmp_path / "main.tex"
        main.write_text(
            r"\providecommand{\provided}{P}"
            r"\renewcommand*{\renewed}[1]{R #1}"
            r"\DeclareRobustCommand{\robust}{Safe\xspace}"
            r"\DeclareMathOperator*{\argmin}{arg\,min}"
        )

        facts = p.build_discovery_facts(
            p.DocumentSnapshot.from_directory(tmp_path, main)
        ).discovery

        assert facts.macros == {
            "provided": p.MacroDefinition("P"),
            "renewed": p.MacroDefinition("R #1", parameter_count=1),
            "robust": p.MacroDefinition("Safe"),
            "argmin": p.MacroDefinition(r"arg\,min", math_operator=True),
        }

    def test_normalizes_math_operator_body_before_metadata_expansion(
        self, tmp_path,
    ):
        main = tmp_path / "main.tex"
        main.write_text(
            r"\DeclareMathOperator{\op}{ arg\xspace }"
            r"\title{The \op{} Result}"
        )

        facts = p.build_discovery_facts(
            p.DocumentSnapshot.from_directory(tmp_path, main)
        ).discovery

        assert facts.macros["op"] == p.MacroDefinition(
            "arg", math_operator=True
        )
        assert p.expand_macros(r"\op", facts.macros) == r"\operatorname{arg}"
        assert facts.title == "The arg Result"

    def test_collects_complete_dependency_and_metadata_references(self, tmp_path):
        sections = tmp_path / "sections"
        sections.mkdir()
        main = tmp_path / "main.tex"
        intro = sections / "intro.tex"
        detail = sections / "detail.tex"
        main.write_text(
            r"\usepackage{amsmath, graphicx}"
            r"\RequirePackage[final]{xcolor}"
            r"\graphicspath{{figures/}{images/generated/}}"
            r"\newtheorem{claim}{Special Claim}"
            r"\begin{abstract}An {important} abstract.\end{abstract}"
            r"\label{doc:start}"
            r"\includegraphics[width=.8\linewidth]{figures/plot.pdf}"
            r"\input{sections/intro}"
        )
        intro.write_text(r"\label{sec:intro}\include{detail}")
        detail.write_text(r"\includegraphics{nested/image.png}")

        facts = p.build_discovery_facts(
            p.DocumentSnapshot.from_directory(tmp_path, main)
        ).discovery

        assert facts.abstract == "An {important} abstract."
        assert facts.packages == ("amsmath", "graphicx", "xcolor")
        assert facts.include_order == (main, intro, detail)
        assert facts.graphicspaths == ("figures", "images/generated")
        assert facts.resource_refs == (
            (main, "figures/plot.pdf"),
            (detail, "nested/image.png"),
        )
        assert facts.labels == {
            "doc:start": (main, main.read_text().index(r"\label{doc:start}")),
            "sec:intro": (intro, 0),
        }
        assert facts.theorem_labels == {"claim": "Special Claim"}

    def test_ignores_commented_commands(self, tmp_path):
        main = tmp_path / "main.tex"
        main.write_text(
            "% \\newcommand{\\hidden}{Wrong}\n"
            "% \\title{Commented}\n"
            "% \\input{missing}\n"
            r"\title{Visible}"
        )

        facts = p.build_discovery_facts(
            p.DocumentSnapshot.from_directory(tmp_path, main)
        ).discovery

        assert facts.title == "Visible"
        assert "hidden" not in facts.macros
        assert facts.include_order == (main,)

    def test_incomplete_definition_is_diagnosed_and_ignored(self, tmp_path):
        main = tmp_path / "main.tex"
        content = r"\newcommand{\broken}{unfinished"
        main.write_text(content)
        snapshot = p.DocumentSnapshot.from_directory(tmp_path, main)

        facts = p.build_discovery_facts(snapshot).discovery

        assert "broken" not in facts.macros
        assert snapshot.sources[main].content == content
        assert any(
            diagnostic.code == "incomplete-command"
            and diagnostic.start == 0
            for diagnostic in snapshot.documents[main].diagnostics
        )

    def test_include_cycle_terminates_in_document_order(self, tmp_path):
        nested = tmp_path / "chapters"
        nested.mkdir()
        main = tmp_path / "main.tex"
        first = nested / "first.tex"
        second = nested / "second.tex"
        main.write_text(r"\input{chapters/first}")
        first.write_text(r"\input{second}")
        second.write_text(r"\include{../main}")

        facts = p.build_discovery_facts(
            p.DocumentSnapshot.from_directory(tmp_path, main)
        ).discovery

        assert facts.include_order == (main, first, second)

    def test_discovery_mappings_are_read_only(self, tmp_path):
        main = tmp_path / "main.tex"
        main.write_text(r"\newcommand{\word}{value}\label{one}")
        facts = p.build_discovery_facts(
            p.DocumentSnapshot.from_directory(tmp_path, main)
        ).discovery

        with pytest.raises(TypeError):
            facts.macros["other"] = "changed"
        with pytest.raises(TypeError):
            facts.labels["two"] = (main, 0)

    def test_pipeline_builds_discovery_once_and_preserves_it_after_cleanup(
        self, tmp_path,
    ):
        main = tmp_path / "main.tex"
        main.write_text(r"\title{Original}\documentclass{custom}")
        spec = p.make_syntax_file_pass(
            name="cleanup",
            planner=p.plan_simplify_documentclass,
            safety=p.Safety.LOSSY,
            main_only=True,
        )

        result = p.PassPipeline([spec]).run(
            p.DocumentSnapshot.from_directory(tmp_path, main)
        )

        assert result.executions[0].rebuilt_before == frozenset({p.Fact.DISCOVERY})
        assert result.snapshot.discovery.title == "Original"
        assert p.Fact.DISCOVERY in result.snapshot.current_facts

    def test_pipeline_rebuilds_syntax_before_discovery_when_both_are_stale(
        self, tmp_path,
    ):
        main = tmp_path / "main.tex"
        source = p.SourceFile(main, r"\title{Recovered}")
        stale = p.DocumentSnapshot(
            root=tmp_path,
            main_tex=main,
            revision=2,
            sources=p.read_only_mapping({main: source}),
            documents=p.read_only_mapping({}),
            current_facts=frozenset(),
        )
        spec = p.make_syntax_file_pass(
            name="observe",
            planner=lambda source, document: [],
            safety=p.Safety.SAFE,
        )

        result = p.PassPipeline([spec]).run(stale)

        assert result.executions[0].rebuilt_before == frozenset({
            p.Fact.SYNTAX, p.Fact.DISCOVERY,
        })
        assert result.snapshot.discovery.title == "Recovered"


class TestParameterizedMacroDiscovery:
    @pytest.mark.parametrize(
        "definitions",
        [
            r"\renewcommand{\choice}{FIRST}\newcommand{\choice}{SECOND}",
            r"\newcommand{\choice}{FIRST}\renewcommand{\choice}{SECOND}",
        ],
    )
    def test_first_definition_wins_across_kinds_in_source_order(
        self, tmp_path, definitions,
    ):
        main = tmp_path / "main.tex"
        main.write_text(definitions)

        facts = p.build_discovery_facts(
            p.DocumentSnapshot.from_directory(tmp_path, main)
        ).discovery

        assert facts.macros["choice"] == p.MacroDefinition("FIRST")

    def test_main_definition_wins_over_included_and_detached_files(
        self, tmp_path,
    ):
        main = tmp_path / "main.tex"
        included = tmp_path / "included.tex"
        detached = tmp_path / "detached.tex"
        main.write_text(
            r"\newcommand{\choice}{MAIN}\input{included}"
        )
        included.write_text(r"\newcommand{\choice}{INCLUDED}")
        detached.write_text(r"\newcommand{\choice}{DETACHED}")

        facts = p.build_discovery_facts(
            p.DocumentSnapshot.from_directory(tmp_path, main)
        ).discovery

        assert facts.include_order == (main, included)
        assert facts.macros["choice"] == p.MacroDefinition("MAIN")

    def test_included_definition_wins_over_detached_file_when_main_has_none(
        self, tmp_path,
    ):
        main = tmp_path / "main.tex"
        included = tmp_path / "included.tex"
        detached = tmp_path / "detached.tex"
        main.write_text(r"\input{included}")
        included.write_text(r"\newcommand{\choice}{INCLUDED}")
        detached.write_text(r"\newcommand{\choice}{DETACHED}")

        facts = p.build_discovery_facts(
            p.DocumentSnapshot.from_directory(tmp_path, main)
        ).discovery

        assert facts.include_order == (main, included)
        assert facts.macros["choice"] == p.MacroDefinition("INCLUDED")

    def test_records_parameter_count_default_and_body(self, tmp_path):
        main = tmp_path / "main.tex"
        main.write_text(
            r"\documentclass{article}"
            r"\newcommand{\update}[1]{#1}"
            r"\newcommand{\named}[2][Default]{#1/#2}"
            r"\begin{document}\end{document}"
        )
        snapshot = p.build_discovery_facts(
            p.DocumentSnapshot.from_directory(tmp_path, main)
        )

        assert snapshot.discovery.macros["update"] == p.MacroDefinition(
            body="#1", parameter_count=1
        )
        assert snapshot.discovery.macros["named"] == p.MacroDefinition(
            body="#1/#2", parameter_count=2, optional_default="Default"
        )

    def test_rejects_invalid_counts_and_default_without_parameter(self, tmp_path):
        main = tmp_path / "main.tex"
        main.write_text(
            r"\newcommand{\tooMany}[10]{#1}"
            r"\newcommand{\invalid}[0][x]{x}"
            r"\begin{document}\end{document}"
        )
        snapshot = p.build_discovery_facts(
            p.DocumentSnapshot.from_directory(tmp_path, main)
        )
        assert "tooMany" not in snapshot.discovery.macros
        assert "invalid" not in snapshot.discovery.macros


def _pass_spec(name, *, phase=p.Phase.SAFE_NORMALIZATION, after=(), before=()):
    return p.PassSpec(
        name=name,
        planner=lambda snapshot: [],
        phase=phase,
        safety=p.Safety.SAFE,
        requires=frozenset(),
        invalidates=frozenset(),
        after=frozenset(after),
        before=frozenset(before),
    )


class TestResolvePassOrder:
    def test_order_is_independent_of_registration_order(self):
        specs = [
            _pass_spec("third", after=("second",)),
            _pass_spec("first"),
            _pass_spec("second", after=("first",)),
        ]

        forward = p.resolve_pass_order(specs)
        reverse = p.resolve_pass_order(reversed(specs))

        assert [spec.name for spec in forward] == ["first", "second", "third"]
        assert [spec.name for spec in reverse] == ["first", "second", "third"]

    def test_earlier_phases_run_first_without_manual_edges(self):
        specs = [
            _pass_spec("compat", phase=p.Phase.COMPATIBILITY),
            _pass_spec("discover", phase=p.Phase.DISCOVERY),
            _pass_spec("safe", phase=p.Phase.SAFE_NORMALIZATION),
        ]

        assert [spec.name for spec in p.resolve_pass_order(specs)] == [
            "discover", "safe", "compat",
        ]

    def test_missing_named_dependency_is_rejected(self):
        with pytest.raises(p.PassDependencyError, match="missing.*unknown"):
            p.resolve_pass_order([_pass_spec("only", after=("unknown",))])

    def test_dependency_cycle_is_rejected(self):
        specs = [
            _pass_spec("left", after=("right",)),
            _pass_spec("right", after=("left",)),
        ]

        with pytest.raises(p.PassDependencyError, match="cycle.*left.*right"):
            p.resolve_pass_order(specs)

    def test_duplicate_pass_name_is_rejected(self):
        with pytest.raises(p.PassDependencyError, match="duplicate.*same"):
            p.resolve_pass_order([_pass_spec("same"), _pass_spec("same")])


class TestPassPipeline:
    def test_reparses_after_a_pass_invalidates_syntax(self, tmp_path):
        main = tmp_path / "main.tex"
        main.write_text(r"\foo{Old}")

        def introduce_section(snapshot):
            source = snapshot.sources[main]
            return [p.Edit(
                main, 0, len(source.content), r"\section{Fresh}",
                "introduce_section", p.Safety.SAFE,
            )]

        observed_revisions = []

        def read_fresh_section(snapshot):
            observed_revisions.append(snapshot.revision)
            document = snapshot.documents[main]
            section = document.commands("section")[0]
            title = document.argument_text(section, 0)
            return [p.Edit(
                main, section.start, section.end, title,
                "read_fresh_section", p.Safety.SAFE,
            )]

        specs = [
            p.PassSpec(
                "introduce_section", introduce_section,
                p.Phase.SAFE_NORMALIZATION, p.Safety.SAFE,
                frozenset({p.Fact.SYNTAX}),
                frozenset({p.Fact.SYNTAX}),
            ),
            p.PassSpec(
                "read_fresh_section", read_fresh_section,
                p.Phase.SAFE_NORMALIZATION, p.Safety.SAFE,
                frozenset({p.Fact.SYNTAX}),
                frozenset({p.Fact.SYNTAX}),
                after=frozenset({"introduce_section"}),
            ),
        ]

        result = p.PassPipeline(specs).run(
            p.DocumentSnapshot.from_directory(tmp_path, main)
        )

        assert result.snapshot.sources[main].content == "Fresh"
        assert result.snapshot.revision == 2
        assert observed_revisions == [1]
        assert result.executions[1].rebuilt_before == frozenset({p.Fact.SYNTAX})

    def test_source_edit_must_invalidate_syntax(self, tmp_path):
        main = tmp_path / "main.tex"
        main.write_text("old")

        spec = p.PassSpec(
            "bad", lambda snapshot: [p.Edit(
                main, 0, 3, "new", "bad", p.Safety.SAFE,
            )],
            p.Phase.SAFE_NORMALIZATION, p.Safety.SAFE,
            frozenset(), frozenset(),
        )

        with pytest.raises(p.PipelineContractError, match="invalidate syntax"):
            p.PassPipeline([spec]).run(
                p.DocumentSnapshot.from_directory(tmp_path, main)
            )

    def test_missing_fact_builder_is_rejected_at_construction(self):
        spec = p.PassSpec(
            "needs_macros", lambda snapshot: [],
            p.Phase.DISCOVERY, p.Safety.SAFE,
            frozenset({p.Fact.MACROS}), frozenset(),
        )

        with pytest.raises(p.PipelineContractError, match="no builder.*macros"):
            p.PassPipeline([spec])

    def test_overlapping_edits_fail_without_mutating_snapshot(self, tmp_path):
        main = tmp_path / "main.tex"
        main.write_text("abcdef")
        snapshot = p.DocumentSnapshot.from_directory(tmp_path, main)
        spec = p.PassSpec(
            "overlap", lambda current: [
                p.Edit(main, 0, 3, "x", "one", p.Safety.SAFE),
                p.Edit(main, 2, 5, "y", "two", p.Safety.SAFE),
            ],
            p.Phase.SAFE_NORMALIZATION, p.Safety.SAFE,
            frozenset(), frozenset({p.Fact.SYNTAX}),
        )

        with pytest.raises(p.EditConflictError):
            p.PassPipeline([spec]).run(snapshot)

        assert snapshot.sources[main].content == "abcdef"
        assert main.read_text() == "abcdef"

    def test_edit_cannot_be_less_safe_than_pass_declaration(self, tmp_path):
        main = tmp_path / "main.tex"
        main.write_text("old")
        spec = p.PassSpec(
            "unsafe", lambda snapshot: [p.Edit(
                main, 0, 3, "new", "unsafe", p.Safety.LOSSY,
            )],
            p.Phase.SAFE_NORMALIZATION, p.Safety.SAFE,
            frozenset(), frozenset({p.Fact.SYNTAX}),
        )

        with pytest.raises(p.PipelineContractError, match="LOSSY.*SAFE"):
            p.PassPipeline([spec]).run(
                p.DocumentSnapshot.from_directory(tmp_path, main)
            )

    def test_collects_skipped_edit_diagnostics_without_failing(self, tmp_path):
        main = tmp_path / "main.tex"
        main.write_text(r"\begin{adjustbox}{width=1cm}body")

        def preserve_incomplete(snapshot):
            source = snapshot.sources[main]
            document = snapshot.documents[main]
            return p.plan_unwrap_environment(
                source,
                document.environments("adjustbox")[0],
                "preserve_incomplete",
                p.Safety.LOSSY,
            )

        spec = p.PassSpec(
            "preserve_incomplete", preserve_incomplete,
            p.Phase.SAFE_NORMALIZATION, p.Safety.LOSSY,
            frozenset({p.Fact.SYNTAX}), frozenset({p.Fact.SYNTAX}),
        )

        result = p.PassPipeline([spec]).run(
            p.DocumentSnapshot.from_directory(tmp_path, main)
        )

        assert result.snapshot.sources[main].content == (
            r"\begin{adjustbox}{width=1cm}body"
        )
        assert [diagnostic.code for diagnostic in result.diagnostics] == [
            "opaque-structure",
        ]

    def test_fallback_only_pass_is_skipped_by_default(self, tmp_path):
        main = tmp_path / "main.tex"
        main.write_text("old")
        calls = []

        def fallback(snapshot):
            calls.append(snapshot.revision)
            return p.PlanOutcome(edits=(p.Edit(
                main, 0, 3, "new", "fallback", p.Safety.FALLBACK_ONLY,
            ),))

        spec = p.PassSpec(
            "fallback", fallback,
            p.Phase.COMPATIBILITY, p.Safety.FALLBACK_ONLY,
            frozenset(), frozenset({p.Fact.SYNTAX}),
        )

        result = p.PassPipeline([spec]).run(
            p.DocumentSnapshot.from_directory(tmp_path, main)
        )

        assert result.snapshot.sources[main].content == "old"
        assert calls == []
        assert result.diagnostics == ()

    def test_enabled_fallback_records_execution_diagnostic(self, tmp_path):
        main = tmp_path / "main.tex"
        main.write_text("old")
        spec = p.PassSpec(
            "fallback", lambda snapshot: p.PlanOutcome(edits=(p.Edit(
                main, 0, 3, "new", "fallback", p.Safety.FALLBACK_ONLY,
            ),)),
            p.Phase.COMPATIBILITY, p.Safety.FALLBACK_ONLY,
            frozenset(), frozenset({p.Fact.SYNTAX}),
        )

        result = p.PassPipeline([spec], fallback_enabled=True).run(
            p.DocumentSnapshot.from_directory(tmp_path, main)
        )

        assert result.snapshot.sources[main].content == "new"
        assert result.diagnostics[-1].code == "fallback-executed"
        assert result.diagnostics[-1].pass_name == "fallback"
        assert result.diagnostics[-1].file == main
        assert "fallback" in result.diagnostics[-1].message
        assert str(main) in result.diagnostics[-1].message


class TestPassAdapters:
    def test_syntax_adapter_can_target_only_main_file(self, tmp_path):
        main = tmp_path / "main.tex"
        child = tmp_path / "child.tex"
        main.write_text(r"\section{Main}")
        child.write_text(r"\section{Child}")

        def remove_sections(source, document):
            return [p.Edit(
                source.path, ref.start, ref.end, "",
                "remove_sections", p.Safety.SAFE,
            ) for ref in document.commands("section")]

        spec = p.make_syntax_file_pass(
            name="main_only",
            planner=remove_sections,
            safety=p.Safety.SAFE,
            main_only=True,
        )
        result = p.PassPipeline([spec]).run(
            p.DocumentSnapshot.from_directory(tmp_path, main)
        )

        assert result.snapshot.sources[main].content == ""
        assert result.snapshot.sources[child].content == r"\section{Child}"

class TestInitialPreprocessingPipeline:
    def test_declares_the_existing_initial_order(self):
        specs = p.build_initial_preprocessing_passes()

        assert [spec.name for spec in p.resolve_pass_order(specs)] == [
            "simplify_documentclass",
            "strip_problematic_packages",
            "strip_noise_commands",
            "strip_annotation_system",
            "normalize_textsc",
        ]

    def test_cleanup_passes_are_syntax_aware_with_declared_safety(self):
        specs = {
            spec.name: spec for spec in p.build_initial_preprocessing_passes()
        }

        for name in (
            "strip_problematic_packages",
            "strip_noise_commands",
            "strip_annotation_system",
            "normalize_textsc",
        ):
            assert specs[name].implementation is p.Implementation.SYNTAX_AWARE
        assert specs["strip_problematic_packages"].safety is p.Safety.LOSSY
        assert specs["strip_noise_commands"].safety is p.Safety.LOSSY
        assert specs["strip_annotation_system"].safety is p.Safety.LOSSY
        assert specs["normalize_textsc"].safety is p.Safety.SAFE

    def test_discovery_preserves_packages_before_cleanup(self, tmp_path):
        main = tmp_path / "main.tex"
        main.write_text(r"\usepackage{hyperref,amsmath}\begin{document}x")

        result = p.PassPipeline(p.build_initial_preprocessing_passes()).run(
            p.DocumentSnapshot.from_directory(tmp_path, main)
        )

        assert result.snapshot.discovery.packages == ("hyperref", "amsmath")
        assert "hyperref" not in result.snapshot.sources[main].content

class TestCompletePreprocessingPlan:
    expected = [
        "discover", "convert_tikz_resources", "simplify_documentclass", "strip_problematic_packages",
        "strip_noise_commands", "strip_annotation_system", "normalize_textsc",
        "convert_pdf_resources", "rewrite_pdf_image_refs", "normalize_citations",
        "preprocess_hyperref", "normalize_table_envs", "unwrap_makecell",
        "rewrite_captionof", "strip_minipage_in_tables", "strip_at_col_specs",
        "normalize_siunitx_columns", "strip_resizebox", "strip_adjustbox",
        "convert_wrapfigure", "unwrap_subfigures", "destar_floats",
        "replace_ding_commands", "replace_textcircled", "preprocess_theorems",
        "normalize_code_listings", "preprocess_algorithms", "translate",
        "unnumber_paragraph_headings", "preserve_appendix_numbering",
        "normalize_input_extensions",
    ]

    def test_declares_every_step_once_in_required_order(self):
        translated = p.build_preprocessing_plan(True)
        plain = p.build_preprocessing_plan(False)

        assert [step.name for step in translated.steps] == self.expected
        assert [step.name for step in plain.steps] == [
            name for name in self.expected if name != "translate"
        ]

    def test_declares_compatibility_phase_and_safety_inventory(self):
        by_name = {
            step.name: step for step in p.build_preprocessing_plan(False).steps
        }
        compatibility = {
            "convert_tikz_resources",
            "simplify_documentclass", "strip_problematic_packages",
            "strip_noise_commands", "strip_annotation_system",
            "normalize_textsc", "convert_pdf_resources",
            "rewrite_pdf_image_refs", "normalize_citations",
            "preprocess_hyperref", "normalize_table_envs",
            "unwrap_makecell", "rewrite_captionof",
            "strip_minipage_in_tables", "strip_at_col_specs",
            "normalize_siunitx_columns", "strip_resizebox",
            "strip_adjustbox", "convert_wrapfigure", "unwrap_subfigures",
            "destar_floats", "replace_ding_commands",
            "replace_textcircled", "preprocess_theorems",
            "normalize_code_listings", "preprocess_algorithms",
            "unnumber_paragraph_headings", "preserve_appendix_numbering",
            "normalize_input_extensions",
        }
        assert by_name["discover"].phase is p.Phase.DISCOVERY
        assert all(
            by_name[name].phase is p.Phase.COMPATIBILITY
            for name in compatibility
        )
        lossy = {
            "convert_tikz_resources",
            "simplify_documentclass", "strip_problematic_packages",
            "strip_noise_commands", "strip_annotation_system",
            "preprocess_hyperref", "normalize_table_envs",
            "unwrap_makecell", "rewrite_captionof",
            "strip_minipage_in_tables", "strip_resizebox",
            "strip_adjustbox", "convert_wrapfigure", "unwrap_subfigures",
            "destar_floats", "preprocess_theorems",
            "normalize_code_listings", "preprocess_algorithms",
            "unnumber_paragraph_headings",
        }
        assert all(by_name[name].safety is p.Safety.LOSSY for name in lossy)
        assert by_name["normalize_citations"].safety is p.Safety.SAFE
        assert by_name["rewrite_pdf_image_refs"].safety is p.Safety.SAFE

    def test_complete_pipeline_preserves_dedication_minipage_boundaries(
        self, tmp_path,
    ):
        main = tmp_path / "main.tex"
        main.write_text(
            r"\newenvironment{dedication}"
            r"{\hfill\begin{minipage}[t]{1in}\raggedright}"
            r"{\end{minipage}\clearpage}"
            r"\begin{document}"
            r"\begin{dedication}For family\end{dedication}"
            r"\end{document}"
        )
        plan = p.build_preprocessing_plan(
            False,
            resource_converter=lambda root: p.ResourceResult(frozenset(), ()),
        )

        result = p.run_preprocessing(tmp_path, main, plan)
        processed = result.snapshot.sources[main].content

        assert processed.count(r"\begin{minipage}") == 1
        assert processed.count(r"\end{minipage}") == 1

    def test_invalid_dependency_fails_before_any_step_runs(self, tmp_path):
        main = tmp_path / "main.tex"
        main.write_text("body")
        called = []
        step = p.PreprocessingStep(
            "bad", lambda state: called.append("bad") or state,
            after=frozenset({"missing"}),
        )

        with pytest.raises(p.PassDependencyError, match="missing"):
            p.run_preprocessing(tmp_path, main, p.PreprocessingPlan((step,)))
        assert called == []

    def test_cycle_fails_before_any_step_runs(self, tmp_path):
        main = tmp_path / "main.tex"
        main.write_text("body")
        called = []
        plan = p.PreprocessingPlan((
            p.PreprocessingStep(
                "a", lambda state: called.append("a") or state,
                after=frozenset({"b"}),
            ),
            p.PreprocessingStep(
                "b", lambda state: called.append("b") or state,
                after=frozenset({"a"}),
            ),
        ))

        with pytest.raises(p.PassDependencyError, match="cycle"):
            p.run_preprocessing(tmp_path, main, plan)
        assert called == []

    def test_failure_writes_no_tex_files(self, tmp_path, monkeypatch):
        main = tmp_path / "main.tex"
        main.write_text("body")
        writes = []
        monkeypatch.setattr(p, "_write_source_content", lambda *args: writes.append(args))

        def fail(state):
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            p.run_preprocessing(
                tmp_path, main,
                p.PreprocessingPlan((p.PreprocessingStep("fail", fail),)),
            )
        assert writes == []
        assert main.read_text() == "body"

    @pytest.mark.parametrize("failure", ["resource", "translation"])
    def test_barrier_failure_rolls_back_earlier_tex_edits(
        self, tmp_path, monkeypatch, failure,
    ):
        main = tmp_path / "main.tex"
        original = (
            r"\documentclass[12pt]{IEEEtran}"
            r"\begin{document}Body.\end{document}"
        )
        main.write_text(original)
        writes = []
        monkeypatch.setattr(p, "_write_source_content", lambda *args: writes.append(args))

        def fail_resource(root):
            raise RuntimeError("resource failed")

        def fail_translation(source, facts):
            raise RuntimeError("translation failed")

        plan = p.build_preprocessing_plan(
            True,
            resource_converter=(
                fail_resource if failure == "resource"
                else lambda root: p.ResourceResult(frozenset(), ())
            ),
            translator=(
                fail_translation if failure == "translation"
                else lambda source, facts: source.content
            ),
        )

        with pytest.raises(RuntimeError, match=failure):
            p.run_preprocessing(tmp_path, main, plan)
        assert writes == []
        assert main.read_text() == original

    def test_success_writes_changed_file_once_and_preserves_crlf(
        self, tmp_path, monkeypatch,
    ):
        main = tmp_path / "main.tex"
        child = tmp_path / "child.tex"
        main.write_bytes(b"one\r\ntwo\r\n")
        child.write_bytes(b"same\r\n")
        writes = []
        original = p._write_source_content

        def record(path, content):
            writes.append(path)
            original(path, content)

        monkeypatch.setattr(p, "_write_source_content", record)

        def change(state):
            sources = dict(state.snapshot.sources)
            source = sources[main]
            sources[main] = p.SourceFile(main, source.content.replace("one", "ONE"))
            return replace(
                state,
                snapshot=replace(
                    state.snapshot,
                    sources=p.read_only_mapping(sources),
                ),
            )

        p.run_preprocessing(
            tmp_path, main,
            p.PreprocessingPlan((p.PreprocessingStep("change", change),)),
        )

        assert writes == [main]
        assert main.read_bytes() == b"ONE\r\ntwo\r\n"
        assert child.read_bytes() == b"same\r\n"

    def test_diagnostics_are_sorted_and_printed_once(self, tmp_path, capsys):
        main = tmp_path / "main.tex"
        main.write_text("body")

        def diagnose(state):
            diagnostics = (
                p.Diagnostic(main, "z", "2", "last", 5, 6),
                p.Diagnostic(main, "a", "1", "first", 1, 2),
            )
            return replace(state, diagnostics=diagnostics)

        result = p.run_preprocessing(
            tmp_path, main,
            p.PreprocessingPlan((p.PreprocessingStep("diagnose", diagnose),)),
        )

        assert [item.message for item in result.diagnostics] == ["first", "last"]
        stderr = capsys.readouterr().err
        assert stderr.count("first") == 1
        assert stderr.count("last") == 1

    def test_multifile_complete_plan_is_idempotent(self, tmp_path, monkeypatch):
        main = tmp_path / "main.tex"
        sections = tmp_path / "sections"
        sections.mkdir()
        child = sections / "child.tex"
        theorem = sections / "theorem.tex"
        figure = tmp_path / "figure.pdf"
        figure.write_bytes(b"fake pdf")
        main.write_text(
            r"\documentclass[12pt]{IEEEtran}" "\n"
            r"\usepackage{hyperref}" "\n"
            r"\title{Pipeline Paper}" "\n"
            r"\author{Ada Author}" "\n"
            r"\newtheorem{theorem}{Theorem}" "\n"
            r"\begin{document}" "\n"
            r"\maketitle" "\n"
            r"\includegraphics{figure.pdf}" "\n"
            r"\input{sections/child}" "\n"
            r"\begin{minted}{python}print(1)\end{minted}" "\n"
            r"\begin{algorithm}\caption{Work}\label{alg:work}"
            r"\begin{algorithmic}\State{Go}\end{algorithmic}\end{algorithm}" "\n"
            r"\end{document}" "\n"
        )
        child.write_text(
            r"\begin{table*}\begin{tabular}{@{}lc@{}}"
            r"\makecell{A\\B} & C\end{tabular}\end{table*}" "\n"
            r"\input{theorem}"
        )
        theorem.write_text(
            r"\begin{theorem}\label{thm:x}True.\end{theorem}" "\n"
            r"\paragraph{Note} Text \ding{51}."
        )
        resources = lambda root: p.ResourceResult(frozenset({figure}), ())
        plan = p.build_preprocessing_plan(
            True,
            resource_converter=resources,
            translator=lambda source, facts: source.content,
        )

        first = p.run_preprocessing(tmp_path, main, plan)
        writes = []
        monkeypatch.setattr(
            p, "_write_source_content", lambda path, content: writes.append(path),
        )
        second = p.run_preprocessing(tmp_path, main, plan)

        assert first.discovery.title == "Pipeline Paper"
        assert first.discovery.authors == ("Ada Author",)
        assert first.snapshot.sources[main].content == second.snapshot.sources[main].content
        assert first.snapshot.sources[child].content == second.snapshot.sources[child].content
        assert first.snapshot.sources[theorem].content == second.snapshot.sources[theorem].content
        assert writes == []
        assert r"\includegraphics{figure.png}" in main.read_text()
        assert r"\input{sections/child.tex}" in main.read_text()
        assert r"\input{theorem.tex}" in child.read_text()
        assert "minted" not in main.read_text()
        assert "algorithmdisplay" in main.read_text()
        assert r"\begin{quote}" in theorem.read_text()
        assert main.read_text().count(r"\begin{document}") == 1
        assert main.read_text().count(r"\end{document}") == 1

    def test_real_translation_plan_is_idempotent_after_later_rewrites(
        self, tmp_path, monkeypatch
    ):
        main = tmp_path / "main.tex"
        child = tmp_path / "child.tex"
        main.write_text(
            r"\documentclass{article}\begin{document}"
            r"\paragraph{Contributions.}" "\n"
            "Body prose has enough English words for paragraph translation.\n"
            r"\input{child}"
            r"\end{document}"
        )
        child.write_text("Short child text.")
        calls = []
        monkeypatch.setattr(p, "extract_glossary", lambda *args: "")
        monkeypatch.setattr(
            p, "_build_heading_translations",
            lambda client, glossary, headings: {"Contributions.": "贡献。"},
        )
        monkeypatch.setattr(
            p, "_batch_translate",
            lambda client, glossary, numbered, request_limiter=None: (
                calls.append(dict(numbered))
                or {index: "Chinese body." for index in numbered}
            ),
        )
        plan = p.build_preprocessing_plan(
            True,
            translation_client=object(),
            resource_converter=lambda root: p.ResourceResult(frozenset(), ()),
        )

        first = p.run_preprocessing(tmp_path, main, plan)
        first_call_count = len(calls)
        writes = []
        monkeypatch.setattr(
            p, "_write_source_content", lambda path, content: writes.append(path),
        )
        second = p.run_preprocessing(tmp_path, main, plan)

        assert first_call_count == 1
        assert len(calls) == first_call_count
        assert second.snapshot.sources[main].content == first.snapshot.sources[main].content
        assert writes == []
        assert r"\paragraph*{Contributions.}" in main.read_text()
        assert r"\input{child.tex}" in main.read_text()
        assert main.read_text().count("% paper2epub:translation-begin:") == 1

    def test_real_translation_step_limits_requests_across_files_and_batches(
        self, tmp_path, monkeypatch
    ):
        paragraph = "This English paragraph contains enough words to require translation."
        main = tmp_path / "main.tex"
        children = [tmp_path / f"child{index}.tex" for index in range(4)]
        main.write_text(
            r"\documentclass{article}\begin{document}"
            + "\n\n".join([paragraph] * 6)
            + "".join(rf"\input{{{child.stem}}}" for child in children)
            + r"\end{document}"
        )
        for child in children:
            child.write_text("\n\n".join([paragraph] * 6))

        snapshot = p.build_discovery_facts(
            p.DocumentSnapshot.from_directory(tmp_path, main)
        )
        state = p.PreprocessingState(snapshot, snapshot.discovery)
        translate_step = next(
            step for step in p.build_preprocessing_plan(
                True, translation_client=object(),
            ).steps
            if step.name == "translate"
        )
        monkeypatch.setattr(p, "extract_glossary", lambda *args: "")
        monkeypatch.setattr(p, "_build_heading_translations", lambda *args: {})
        monkeypatch.setattr(p, "TRANSLATION_BATCH_MAX_CHARS", 1)

        lock = threading.Lock()
        five_active = threading.Event()
        overflow = threading.Event()
        release_requests = threading.Event()
        active = 0
        max_active = 0

        def fake_chat(client, system_prompt, user_prompt):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
                if active == p.TRANSLATION_BATCH_WORKERS:
                    five_active.set()
                if active > p.TRANSLATION_BATCH_WORKERS:
                    overflow.set()
            try:
                assert release_requests.wait(5)
                ids = [
                    line for line in user_prompt.splitlines()
                    if line.startswith("[") and line.endswith("]")
                ]
                return "\n\n".join(f"{item}\nChinese translation." for item in ids)
            finally:
                with lock:
                    active -= 1

        monkeypatch.setattr(p, "_chat", fake_chat)
        errors = []

        def run_translation():
            try:
                translate_step.run(state)
            except BaseException as error:
                errors.append(error)

        runner = threading.Thread(target=run_translation)
        runner.start()
        try:
            assert five_active.wait(5)
            assert not overflow.wait(1)
        finally:
            release_requests.set()
            runner.join(10)

        assert not runner.is_alive()
        assert errors == []
        assert max_active == p.TRANSLATION_BATCH_WORKERS

    def test_translation_preparation_uses_current_snapshot_sources(
        self, tmp_path, monkeypatch,
    ):
        main = tmp_path / "main.tex"
        original = (
            r"\documentclass{article}"
            r"\begin{document}"
            r"\section{About \textsc{Method}}"
            r"Body prose with enough words for translation."
            r"\end{document}"
        )
        main.write_text(original)
        current_heading = r"About \text{Method}"

        def fake_glossary(client, title, abstract, headings):
            assert headings == [current_heading]
            assert main.read_text() == original
            return "Method | 方法"

        def fake_heading_map(client, glossary, headings):
            assert headings == [current_heading]
            assert main.read_text() == original
            return {current_heading: "关于方法"}

        def fake_translate(
            client, glossary, heading_map, content, request_limiter=None,
        ):
            assert current_heading in content
            assert heading_map == {current_heading: "关于方法"}
            assert request_limiter is not None
            assert main.read_text() == original
            return p._translate_headings(heading_map, content)

        monkeypatch.setattr(p, "extract_glossary", fake_glossary)
        monkeypatch.setattr(p, "_build_heading_translations", fake_heading_map)
        monkeypatch.setattr(p, "translate_file_content", fake_translate)
        p.run_preprocessing(
            tmp_path,
            main,
            p.build_preprocessing_plan(
                True,
                translation_client=object(),
                resource_converter=lambda root: p.ResourceResult(frozenset(), ()),
            ),
        )

        assert current_heading in main.read_text()
        assert "关于方法" in main.read_text()


class TestFinalPreprocessingArchitecture:
    def test_legacy_structural_matchers_are_removed(self):
        source = Path(p.__file__).read_text()

        for name in (
            "_CITE_RE",
            "_STRIP_ENV_RE",
            "_TABLE_STRIP_WIDTH_RES",
            "_MINIPAGE_BEGIN_RE",
            "_WRAPFIG_RE",
            "_SUBFIG_BEGIN_RE",
            "_ADJUSTBOX_ENV_BEGIN_RE",
            "_NEWTHEOREM_RE",
            "make_legacy_text_pass",
            "_transform_tex_files",
        ):
            assert name not in source

    def test_migrated_file_wrappers_are_removed(self):
        source = Path(p.__file__).read_text()

        for definition in (
            "def simplify_documentclass(",
            "def strip_problematic_packages(",
            "def normalize_citations(",
            "def preprocess_hyperref(",
            "def normalize_table_envs(",
            "def preprocess_theorems(",
            "def normalize_code_listings(",
            "def preprocess_algorithms(",
            "def normalize_input_extensions(",
        ):
            assert definition not in source

    def test_only_transaction_boundary_writes_tex_sources(self):
        source = Path(p.__file__).read_text()

        assert ".write_text(" not in source
        assert source.count("_write_source_content(") == 2


class TestEditPlanner:
    def test_applies_edits_in_reverse_source_order(self, tmp_path):
        source = p.SourceFile(tmp_path / "main.tex", "alpha beta gamma")
        edits = [
            p.Edit(source.path, 0, 5, "A", "one", p.Safety.SAFE),
            p.Edit(source.path, 11, 16, "G", "two", p.Safety.SAFE),
        ]

        assert p.EditPlanner.apply(source, edits) == "A beta G"

    def test_rejects_overlapping_edits(self, tmp_path):
        source = p.SourceFile(tmp_path / "main.tex", "abcdef")
        edits = [
            p.Edit(source.path, 1, 4, "X", "one", p.Safety.SAFE),
            p.Edit(source.path, 3, 5, "Y", "two", p.Safety.SAFE),
        ]

        with pytest.raises(p.EditConflictError, match="overlap"):
            p.EditPlanner.apply(source, edits)

    def test_containment_filter_keeps_crossing_edits_for_validation(self, tmp_path):
        source = p.SourceFile(tmp_path / "main.tex", "abcdef")
        outcome = p.PlanOutcome(edits=(
            p.Edit(source.path, 0, 4, "", "outer", p.Safety.LOSSY),
            p.Edit(source.path, 2, 6, "", "crossing", p.Safety.LOSSY),
        ))

        filtered = p._suppress_contained_edits(outcome)

        with pytest.raises(p.EditConflictError, match="overlap"):
            p.EditPlanner.apply(source, filtered.edits)

    def test_containment_filter_preserves_diagnostics(self, tmp_path):
        path = tmp_path / "main.tex"
        diagnostic = p.Diagnostic(
            path, "cleanup", "opaque-structure", "preserved", 2, 3,
        )
        outcome = p.PlanOutcome(
            edits=(
                p.Edit(path, 0, 5, "", "outer", p.Safety.LOSSY),
                p.Edit(path, 2, 3, "", "inner", p.Safety.LOSSY),
            ),
            diagnostics=(diagnostic,),
        )

        filtered = p._suppress_contained_edits(outcome)

        assert len(filtered.edits) == 1
        assert filtered.diagnostics == (diagnostic,)

    @pytest.mark.parametrize("start,end", [(-1, 1), (3, 2), (0, 99)])
    def test_rejects_invalid_ranges(self, tmp_path, start, end):
        source = p.SourceFile(tmp_path / "main.tex", "abc")
        edit = p.Edit(
            source.path, start, end, "", "bad", p.Safety.SAFE,
        )

        with pytest.raises(ValueError, match="invalid edit range"):
            p.EditPlanner.apply(source, [edit])

    def test_rejects_edit_for_another_file(self, tmp_path):
        source = p.SourceFile(tmp_path / "main.tex", "abc")
        edit = p.Edit(
            tmp_path / "other.tex", 0, 1, "x", "bad", p.Safety.SAFE,
        )

        with pytest.raises(ValueError, match="different source file"):
            p.EditPlanner.apply(source, [edit])


class TestStructuralPlannerPrimitives:
    def test_unwrap_command_keeps_nested_argument(self, tmp_path):
        source = p.SourceFile(
            tmp_path / "main.tex", r"A \resizebox{1cm}{!}{x \textbf{y}} Z"
        )
        document = p.LatexDocument(source)
        outcome = p.plan_unwrap_command(
            source, document.commands("resizebox")[0], 2,
            "resizebox", p.Safety.LOSSY,
        )

        assert p.EditPlanner.apply(source, outcome.edits) == r"A x \textbf{y} Z"

    def test_incomplete_command_is_skipped_with_diagnostic(self, tmp_path):
        source = p.SourceFile(tmp_path / "main.tex", r"\resizebox{1cm}{!}{x")
        document = p.LatexDocument(source)
        outcome = p.plan_unwrap_command(
            source, document.commands("resizebox")[0], 2,
            "resizebox", p.Safety.LOSSY,
        )

        assert outcome.edits == ()
        assert outcome.diagnostics[0].code == "opaque-structure"

    def test_remove_comment_environment_emits_exact_edit(self, tmp_path):
        content = r"left\begin{comment}hidden \textbf{text}\end{comment}right"
        source = p.SourceFile(tmp_path / "main.tex", content)
        ref = p.LatexDocument(source).environments("comment")[0]

        outcome = p.plan_remove_node(
            source, ref, "remove_comment", p.Safety.LOSSY,
        )

        assert outcome.edits == (p.Edit(
            source.path,
            content.index(r"\begin{comment}"),
            content.index(r"\end{comment}") + len(r"\end{comment}"),
            "",
            "remove_comment",
            p.Safety.LOSSY,
        ),)
        assert p.EditPlanner.apply(source, outcome.edits) == "leftright"

    def test_unwrap_hyperlink_preserves_nested_label_and_adjacent_text(
        self, tmp_path,
    ):
        content = r"L\hyperlink{target}{click \textbf{here}}R"
        source = p.SourceFile(tmp_path / "main.tex", content)
        ref = p.LatexDocument(source).commands("hyperlink")[0]

        outcome = p.plan_unwrap_command(
            source, ref, 1, "unwrap_hyperlink", p.Safety.LOSSY,
        )

        assert outcome.edits == (p.Edit(
            source.path,
            1,
            len(content) - 1,
            r"click \textbf{here}",
            "unwrap_hyperlink",
            p.Safety.LOSSY,
        ),)
        assert p.EditPlanner.apply(source, outcome.edits) == (
            r"Lclick \textbf{here}R"
        )

    def test_rename_tabular_environment_changes_only_name_tokens(self, tmp_path):
        content = r"A\begin{tabular}{lc}x&y\end{tabular}Z"
        source = p.SourceFile(tmp_path / "main.tex", content)
        ref = p.LatexDocument(source).environments("tabular")[0]

        outcome = p.plan_rename_environment(
            source, ref, "tabularx", "rename_table", p.Safety.SAFE,
        )

        assert outcome.edits == (
            p.Edit(source.path, 8, 15, "tabularx", "rename_table", p.Safety.SAFE),
            p.Edit(
                source.path, len(content) - 9, len(content) - 2,
                "tabularx", "rename_table", p.Safety.SAFE,
            ),
        )
        assert p.EditPlanner.apply(source, outcome.edits) == (
            r"A\begin{tabularx}{lc}x&y\end{tabularx}Z"
        )

    def test_unwrap_adjustbox_removes_only_boundary_ranges(self, tmp_path):
        content = (
            r"before\begin{adjustbox}{width=1cm}"
            r"x \textbf{y}\end{adjustbox}after"
        )
        source = p.SourceFile(tmp_path / "main.tex", content)
        ref = p.LatexDocument(source).environments("adjustbox")[0]

        outcome = p.plan_unwrap_environment(
            source, ref, "unwrap_adjustbox", p.Safety.LOSSY,
        )

        assert outcome.edits == (
            p.Edit(
                source.path, ref.start, ref.body_start, "",
                "unwrap_adjustbox", p.Safety.LOSSY,
            ),
            p.Edit(
                source.path, ref.body_end, ref.end, "",
                "unwrap_adjustbox", p.Safety.LOSSY,
            ),
        )
        assert p.EditPlanner.apply(source, outcome.edits) == (
            r"beforex \textbf{y}after"
        )

    def test_transform_tabular_argument_replaces_only_delimited_text(
        self, tmp_path,
    ):
        content = r"A\begin{tabular}{>{\bfseries}lc}x&y\end{tabular}Z"
        source = p.SourceFile(tmp_path / "main.tex", content)
        ref = p.LatexDocument(source).environments("tabular")[0]
        argument = ref.arguments[0]
        assert argument is not None

        outcome = p.plan_transform_argument(
            source, ref, 0, lambda text: text.replace("l", "X"),
            "columns", p.Safety.SAFE,
        )

        assert outcome.edits == (p.Edit(
            source.path,
            argument.start + 1,
            argument.end - 1,
            r">{\bfseries}Xc",
            "columns",
            p.Safety.SAFE,
        ),)
        assert p.EditPlanner.apply(source, outcome.edits) == (
            r"A\begin{tabular}{>{\bfseries}Xc}x&y\end{tabular}Z"
        )

    def test_non_idempotent_argument_transform_is_skipped(self, tmp_path):
        source = p.SourceFile(
            tmp_path / "main.tex", r"\begin{tabular}{lc}x&y\end{tabular}",
        )
        ref = p.LatexDocument(source).environments("tabular")[0]

        outcome = p.plan_transform_argument(
            source, ref, 0, lambda text: text + "x",
            "columns", p.Safety.SAFE,
        )

        assert outcome.edits == ()
        assert outcome.diagnostics[0].code == "non-idempotent-transform"

    def test_foreign_references_are_rejected_by_all_primitives(self, tmp_path):
        source = p.SourceFile(tmp_path / "main.tex", "local")
        foreign = p.SourceFile(
            tmp_path / "foreign.tex",
            r"\hyperlink{target}{label}\begin{tabular}{lc}x&y\end{tabular}",
        )
        document = p.LatexDocument(foreign)
        command = document.commands("hyperlink")[0]
        environment = document.environments("tabular")[0]

        outcomes = (
            p.plan_remove_node(source, command, "remove", p.Safety.LOSSY),
            p.plan_unwrap_command(
                source, command, 1, "unwrap_command", p.Safety.LOSSY,
            ),
            p.plan_rename_environment(
                source, environment, "tabularx", "rename", p.Safety.SAFE,
            ),
            p.plan_unwrap_environment(
                source, environment, "unwrap_environment", p.Safety.LOSSY,
            ),
            p.plan_transform_argument(
                source, environment, 0, str.upper,
                "transform", p.Safety.SAFE,
            ),
        )

        assert all(outcome.edits == () for outcome in outcomes)
        assert all(
            outcome.diagnostics[0].code == "foreign-reference"
            for outcome in outcomes
        )

    def test_spaced_comment_environment_rename_preserves_opaque_form(
        self, tmp_path,
    ):
        content = r"A\begin {comment}hidden\end {comment}Z"
        source = p.SourceFile(tmp_path / "main.tex", content)
        ref = p.LatexDocument(source).environments("comment")[0]
        assert ref.opaque

        outcome = p.plan_rename_environment(
            source, ref, "discard", "rename_comment", p.Safety.SAFE,
        )

        assert outcome.edits == ()
        assert outcome.diagnostics[0].code == "opaque-structure"
        assert p.EditPlanner.apply(source, outcome.edits) == content

    def test_incomplete_environment_is_preserved_by_all_environment_planners(
        self, tmp_path,
    ):
        source = p.SourceFile(
            tmp_path / "main.tex", r"\begin{adjustbox}{width=1cm}body",
        )
        ref = p.LatexDocument(source).environments("adjustbox")[0]

        outcomes = (
            p.plan_remove_node(source, ref, "remove", p.Safety.LOSSY),
            p.plan_rename_environment(
                source, ref, "center", "rename", p.Safety.LOSSY,
            ),
            p.plan_unwrap_environment(
                source, ref, "unwrap", p.Safety.LOSSY,
            ),
            p.plan_transform_argument(
                source, ref, 0, str.upper, "transform", p.Safety.LOSSY,
            ),
        )

        assert all(outcome.edits == () for outcome in outcomes)
        assert all(
            outcome.diagnostics[0].code == "opaque-structure"
            for outcome in outcomes
        )

    def test_replanning_transformed_output_emits_no_edits(self, tmp_path):
        source = p.SourceFile(
            tmp_path / "main.tex", r"A\hyperlink{target}{label}Z",
        )

        def plan(current):
            document = p.LatexDocument(current)
            outcomes = [
                p.plan_unwrap_command(
                    current, ref, 1, "unwrap_hyperlink", p.Safety.LOSSY,
                )
                for ref in document.commands("hyperlink")
            ]
            return tuple(
                edit for outcome in outcomes for edit in outcome.edits
            )

        first_edits = plan(source)
        updated = p.EditPlanner.apply(source, first_edits)
        reparsed = p.SourceFile(source.path, updated)

        assert updated == "AlabelZ"
        assert plan(reparsed) == ()


class TestFindMatchingBrace:
    def test_simple(self):
        assert p.find_matching_brace("{abc}", 0) == 4

    def test_nested(self):
        assert p.find_matching_brace("{a{b}c}", 0) == 6

    def test_deeply_nested(self):
        assert p.find_matching_brace("{a{b{c}d}e}", 0) == 10

    def test_escaped_brace(self):
        assert p.find_matching_brace(r"{a\}b}", 0) == 5

    def test_escaped_backslash(self):
        assert p.find_matching_brace(r"{a\\}", 0) == 4

    def test_not_at_brace(self):
        assert p.find_matching_brace("abc", 0) is None

    def test_unmatched(self):
        assert p.find_matching_brace("{abc", 0) is None

    def test_empty_braces(self):
        assert p.find_matching_brace("{}", 0) == 1

    def test_offset(self):
        assert p.find_matching_brace("xx{yy}zz", 2) == 5

    def test_out_of_bounds(self):
        assert p.find_matching_brace("{}", 10) is None


class TestStripTexComments:
    def test_removes_comments_but_preserves_newlines_and_escaped_percent(self):
        content = "before \\% value % note\r\nafter% tail\nend"

        assert p.strip_tex_comments(content) == (
            "before \\% value \r\nafter\nend"
        )


class TestExtractBraceArg:
    def test_simple(self):
        arg, pos = p.extract_brace_arg("{hello} rest", 0)
        assert arg == "hello"
        assert pos == 7

    def test_skip_whitespace(self):
        arg, pos = p.extract_brace_arg("  {hi}", 0)
        assert arg == "hi"

    def test_no_brace(self):
        arg, pos = p.extract_brace_arg("no brace", 0)
        assert arg is None

    def test_nested(self):
        arg, pos = p.extract_brace_arg("{a{b}c}", 0)
        assert arg == "a{b}c"

    def test_consecutive(self):
        arg1, pos = p.extract_brace_arg("{first}{second}", 0)
        arg2, pos = p.extract_brace_arg("{first}{second}", pos)
        assert arg1 == "first"
        assert arg2 == "second"


# ── _unwrap_latex_cmd ───────────────────────────────────────────────────────


class TestParseNumberedResponse:
    def test_basic(self):
        raw = "[0]\nhello\n\n[1]\nworld"
        assert p._parse_numbered_response(raw) == {0: "hello", 1: "world"}

    def test_with_postprocess(self):
        raw = "[0]\nhello}"
        result = p._parse_numbered_response(raw, postprocess=lambda text: text.rstrip("}"))
        assert result == {0: "hello"}

    def test_non_sequential_ids(self):
        raw = "[3]\nfoo\n\n[7]\nbar"
        assert p._parse_numbered_response(raw) == {3: "foo", 7: "bar"}

    def test_empty_translation_skipped(self):
        raw = "[0]\n\n\n[1]\nactual"
        result = p._parse_numbered_response(raw)
        assert 0 not in result
        assert result[1] == "actual"

    def test_empty_input(self):
        assert p._parse_numbered_response("") == {}

    def test_multiline_segment(self):
        raw = "[0]\nline one\nline two"
        result = p._parse_numbered_response(raw)
        assert result[0] == "line one\nline two"


# ── Translation response validation ─────────────────────────────────────────


class TestHasBalancedBraces:
    def test_balanced(self):
        assert p._has_balanced_braces(r"中文 \textbf{术语}")

    def test_extra_closing_is_invalid(self):
        assert not p._has_balanced_braces("中文}")

    def test_missing_closing_is_invalid(self):
        assert not p._has_balanced_braces(r"中文 \textbf{术语")

    def test_escaped_brace_does_not_open_group(self):
        assert p._has_balanced_braces(r"$f(x)=\left\{x\right.$")


class TestPartitionTranslationBatches:
    def test_empty_input_has_no_batches(self):
        assert p._partition_translation_batches({}) == ()

    def test_paragraph_count_does_not_split_batch(self):
        paragraphs = {i: "x" for i in range(25)}

        assert p._partition_translation_batches(
            paragraphs,
            max_chars=100,
        ) == (paragraphs,)

    def test_default_batch_limit_is_24000_characters(self):
        paragraphs = {0: "a" * 12_000, 1: "b" * 12_000}

        assert p._partition_translation_batches(paragraphs) == (paragraphs,)

    def test_respects_character_limit(self):
        assert p._partition_translation_batches(
            {0: "aaaa", 1: "bbbb", 2: "cc", 3: "ddd"},
            max_chars=6,
        ) == ({0: "aaaa"}, {1: "bbbb", 2: "cc"}, {3: "ddd"})

    def test_oversized_paragraph_is_single_batch(self):
        assert p._partition_translation_batches(
            {0: "abcdefgh", 1: "x"},
            max_chars=4,
        ) == ({0: "abcdefgh"}, {1: "x"})

    @pytest.mark.parametrize("max_chars", [0, -1])
    def test_rejects_non_positive_limit(self, max_chars):
        with pytest.raises(ValueError):
            p._partition_translation_batches(
                {0: "text"},
                max_chars=max_chars,
            )


class TestBatchTranslate:
    def test_empty_input_needs_no_batch_request(self):
        assert p._batch_translate(object(), "", {}) == {}

    def test_retries_only_missing_ids(self, monkeypatch):
        calls = []
        responses = iter(["[0]\n译文零", "[1]\n译文一"])

        def fake_chat(client, system_prompt, user_prompt):
            calls.append(user_prompt)
            return next(responses)

        monkeypatch.setattr(p, "_chat", fake_chat)
        result = p._batch_translate(
            object(), "term | 术语", {0: "zero", 1: "one"}
        )

        assert result == {0: "译文零", 1: "译文一"}
        assert "[0]" in calls[0] and "[1]" in calls[0]
        assert "[0]" not in calls[1] and "[1]" in calls[1]

    def test_retries_invalid_braces(self, monkeypatch):
        responses = iter(["[0]\n错误}", "[0]\n正确"])
        monkeypatch.setattr(p, "_chat", lambda *args: next(responses))

        assert p._batch_translate(object(), "", {0: "source"}) == {0: "正确"}

    def test_inline_math_is_protected_and_restored_verbatim(self, monkeypatch):
        source_math = r"$V(p,t)=G(p)$"
        prompts = []

        def fake_chat(client, system_prompt, user_prompt):
            prompts.append(user_prompt)
            assert source_math not in user_prompt
            token = re.search(r"⟦P2E_MATH_[^\s]+⟧", user_prompt).group(0)
            return f"[0]\n对于 {token}，结论成立。"

        monkeypatch.setattr(p, "_chat", fake_chat)

        result = p._batch_translate(
            object(), "", {0: f"For {source_math}, the conclusion holds."}
        )

        assert result == {0: f"对于 {source_math}，结论成立。"}
        assert len(prompts) == 1

    def test_retries_when_math_placeholder_is_missing(self, monkeypatch):
        responses = []

        def fake_chat(client, system_prompt, user_prompt):
            token = re.search(r"⟦P2E_MATH_[^\s]+⟧", user_prompt).group(0)
            responses.append(token)
            if len(responses) == 1:
                return "[0]\n公式被丢弃。"
            return f"[0]\n保留公式 {token}。"

        monkeypatch.setattr(p, "_chat", fake_chat)

        result = p._batch_translate(
            object(), "", {0: r"An inline formula $f(x)=x^2$ is retained."}
        )

        assert result == {0: r"保留公式 $f(x)=x^2$。"}
        assert len(responses) == 2

    def test_skips_persistent_math_placeholder_loss(
        self, monkeypatch, capsys,
    ):
        monkeypatch.setattr(p, "_chat", lambda *args: "[0]\n公式被丢弃。")

        result = p._batch_translate(
            object(), "", {0: r"An inline formula $f(x)=x^2$ is retained."}
        )

        assert result == {}
        assert "skipped invalid paragraph IDs: 0" in capsys.readouterr().err

    def test_allows_safe_reordering_of_math_placeholders(self, monkeypatch):
        def fake_chat(client, system_prompt, user_prompt):
            tokens = re.findall(r"⟦P2E_MATH_[^\s]+⟧", user_prompt)
            return f"[0]\n{tokens[1]} 与 {tokens[0]}"

        monkeypatch.setattr(p, "_chat", fake_chat)

        result = p._batch_translate(
            object(), "", {0: r"Compare $f(x)$ with $g(x)$."}
        )

        assert result == {0: r"$g(x)$ 与 $f(x)$"}

    def test_skips_when_retry_is_still_incomplete(
        self, monkeypatch, capsys,
    ):
        monkeypatch.setattr(p, "_chat", lambda *args: "")

        assert p._batch_translate(object(), "", {0: "source"}) == {}
        assert "0 (missing response)" in capsys.readouterr().err

    def test_retry_transport_error_skips_affected_ids(
        self, monkeypatch, capsys,
    ):
        responses = iter([{0: "译文零"}, RuntimeError("request failed")])

        def fake_request(*args):
            response = next(responses)
            if isinstance(response, Exception):
                raise response
            return response

        monkeypatch.setattr(p, "_request_numbered_translation", fake_request)

        result = p._batch_translate(object(), "", {0: "zero", 1: "one"})

        assert result == {0: "译文零"}
        assert "skipped paragraph IDs: 1" in capsys.readouterr().err

    def test_overlapping_batches_merge_in_input_order(self, monkeypatch):
        zero_started = threading.Event()
        one_started = threading.Event()
        one_completed = threading.Event()
        completion_order = []

        def translate(client, prompt, batch):
            if 0 in batch:
                zero_started.set()
                assert one_started.wait(2)
                assert one_completed.wait(2)
            else:
                one_started.set()
                assert zero_started.wait(2)
                completion_order.append(1)
                one_completed.set()
                return {1: "zh-1"}
            completion_order.append(0)
            return {index: f"zh-{index}" for index in batch}

        monkeypatch.setattr(p, "_translate_numbered_batch", translate)
        monkeypatch.setattr(p, "TRANSLATION_BATCH_MAX_CHARS", 1)

        result = p._batch_translate(object(), "", {0: "a", 1: "b"})

        assert completion_order == [1, 0]
        assert result == {
            0: "zh-0",
            1: "zh-1",
        }
        assert list(result) == [0, 1]

    def test_partial_batch_fallback_preserves_successful_translations(
        self, monkeypatch,
    ):
        monkeypatch.setattr(p, "TRANSLATION_BATCH_MAX_CHARS", 1)
        monkeypatch.setattr(
            p,
            "_translate_numbered_batch",
            lambda client, prompt, batch: (
                {} if 1 in batch else {index: f"zh-{index}" for index in batch}
            ),
        )

        result = p._batch_translate(
            object(), "", {0: "zero", 1: "one", 2: "two"}
        )

        assert result == {0: "zh-0", 2: "zh-2"}

    def test_retries_missing_and_unbalanced_ids_within_each_batch(
        self, monkeypatch
    ):
        first_requests = threading.Barrier(2)
        calls = []
        calls_lock = threading.Lock()

        def fake_chat(client, system_prompt, user_prompt):
            ids = tuple(
                int(line[1:-1])
                for line in user_prompt.splitlines()
                if line.startswith("[") and line.endswith("]")
            )
            with calls_lock:
                calls.append(ids)
            if len(ids) == 2:
                first_requests.wait(timeout=2)
            return {
                (0, 1): "[0]\n译文零",
                (1,): "[1]\n译文一",
                (2, 3): "[2]\n错误}\n\n[3]\n译文三",
                (2,): "[2]\n译文二",
            }[ids]

        monkeypatch.setattr(p, "_chat", fake_chat)
        monkeypatch.setattr(p, "TRANSLATION_BATCH_MAX_CHARS", 8)

        result = p._batch_translate(
            object(), "", {0: "zero", 1: "one", 2: "two", 3: "three"}
        )

        assert result == {0: "译文零", 1: "译文一", 2: "译文二", 3: "译文三"}
        assert sorted(calls) == [(0, 1), (1,), (2,), (2, 3)]

    def test_cancels_batches_that_remain_queued_after_failure(
        self, monkeypatch
    ):
        real_executor = p.ThreadPoolExecutor
        active_workers = threading.Barrier(2)
        cancellation_complete = threading.Event()
        started = []
        cancelled = []
        observations_lock = threading.Lock()
        active = 0
        max_active = 0

        class TrackingExecutor(real_executor):
            def submit(self, fn, client, prompt, batch):
                batch_id = next(iter(batch))
                future = super().submit(fn, client, prompt, batch)
                original_cancel = future.cancel

                def track_cancel():
                    was_cancelled = original_cancel()
                    if was_cancelled:
                        with observations_lock:
                            cancelled.append(batch_id)
                    if batch_id == 4:
                        cancellation_complete.set()
                    return was_cancelled

                future.cancel = track_cancel
                return future

        def translate(client, prompt, batch):
            nonlocal active, max_active
            batch_id = next(iter(batch))
            with observations_lock:
                started.append(batch_id)
                active += 1
                max_active = max(max_active, active)
            try:
                if batch_id == 0:
                    active_workers.wait(timeout=2)
                    raise RuntimeError("broken batch")
                if batch_id == 1:
                    active_workers.wait(timeout=2)
                assert cancellation_complete.wait(2)
                return {batch_id: f"zh-{batch_id}"}
            finally:
                with observations_lock:
                    active -= 1

        monkeypatch.setattr(p, "ThreadPoolExecutor", TrackingExecutor)
        monkeypatch.setattr(p, "_translate_numbered_batch", translate)
        monkeypatch.setattr(p, "TRANSLATION_BATCH_MAX_CHARS", 1)
        monkeypatch.setattr(p, "TRANSLATION_BATCH_WORKERS", 2)

        with pytest.raises(RuntimeError, match="broken batch"):
            p._batch_translate(
                object(), "", {index: str(index) for index in range(5)}
            )

        assert cancelled
        assert set(cancelled).isdisjoint(started)
        assert set(cancelled) | set(started) == set(range(5))
        assert max_active == 2
        assert max_active <= p.TRANSLATION_BATCH_WORKERS

    def test_propagates_batch_failure(self, monkeypatch):
        def translate(client, prompt, batch):
            if 1 in batch:
                raise RuntimeError("broken batch")
            return {index: f"zh-{index}" for index in batch}

        monkeypatch.setattr(p, "_translate_numbered_batch", translate)
        monkeypatch.setattr(p, "TRANSLATION_BATCH_MAX_CHARS", 1)

        with pytest.raises(RuntimeError, match="broken batch"):
            p._batch_translate(object(), "", {0: "a", 1: "b"})


# ── Title & Author extraction ──────────────────────────────────────────────


class TestExpandMacros:
    def test_required_and_multiple_arguments(self):
        macros = {
            "one": p.MacroDefinition("<#1>", 1),
            "two": p.MacroDefinition("#2/#1", 2),
        }
        assert p.expand_macros(r"\one{A} \two{left}{right}", macros) == (
            "<A> right/left"
        )

    def test_optional_argument_uses_value_or_default(self):
        macros = {
            "named": p.MacroDefinition("#1/#2", 2, "Default"),
        }
        assert p.expand_macros(r"\named{X}", macros) == "Default/X"
        assert p.expand_macros(r"\named[Given]{X}", macros) == "Given/X"

    def test_nested_expansion(self):
        macros = {
            "outer": p.MacroDefinition(r"\inner{#1}", 1),
            "inner": p.MacroDefinition("[#1]", 1),
        }
        assert p.expand_macros(r"\outer{value}", macros) == "[value]"

    def test_nested_known_candidates_expand_without_overlapping_edits(self):
        macros = {
            "outer": p.MacroDefinition("<#1>", 1),
            "inner": p.MacroDefinition("[#1]", 1),
        }
        assert p.expand_macros(r"\outer{\inner{x}}", macros) == "<[x]>"

    def test_missing_or_malformed_arguments_are_preserved(self):
        macros = {"pair": p.MacroDefinition("#1/#2", 2)}
        assert p.expand_macros(r"\pair{only}", macros) == r"\pair{only}"
        assert p.expand_macros(r"\pair{one}{broken", macros) == (
            r"\pair{one}{broken"
        )

    def test_out_of_range_parameter_reference_preserves_invocation(self):
        macros = {"invalid": p.MacroDefinition("#2", 1)}
        assert p.expand_macros(r"\invalid{value}", macros) == (
            r"\invalid{value}"
        )

    def test_expands_trenv_parameterized_title_wrappers(self, tmp_path):
        main = tmp_path / "main.tex"
        main.write_text(
            r"\documentclass{article}"
            r"\title{\update{\systemname: Transparently Share Serverless "
            r"Execution Environments Across Different Functions and Nodes}}"
            r"\newcommand{\systemname}{\textsc{TrEnv-X}\xspace}"
            r"\newcommand{\update}[1]{#1}"
            r"\begin{document}\end{document}"
        )
        snapshot = p.build_discovery_facts(
            p.DocumentSnapshot.from_directory(tmp_path, main)
        )
        assert snapshot.discovery.title == (
            "TrEnv-X: Transparently Share Serverless Execution Environments "
            "Across Different Functions and Nodes"
        )
        assert "#1" not in snapshot.discovery.title

    def test_zero_argument_math_operator_and_literal_hash(self):
        macros = {
            "name": p.MacroDefinition("TrEnv-X"),
            "op": p.MacroDefinition("argmin", math_operator=True),
            "hash": p.MacroDefinition("##1", 1),
        }
        assert p.expand_macros(r"\name \op \hash{x}", macros) == (
            r"TrEnv-X\operatorname{argmin}#1"
        )

    def test_unknown_comment_prefix_and_self_recursion_are_conservative(self):
        macros = {
            "foo": p.MacroDefinition("FOO"),
            "foobar": p.MacroDefinition("BAR"),
            "loop": p.MacroDefinition(r"\loop"),
        }
        text = "% \\foo\n" + r"\unknown \foobar \loop"
        assert p.expand_macros(text, macros) == "% \\foo\n" + (
            r"\unknown BAR\loop"
        )

    def test_parameterized_author_macro_is_expanded_in_discovery(self, tmp_path):
        main = tmp_path / "main.tex"
        main.write_text(
            r"\newcommand{\person}[2]{#1 #2}"
            r"\author{\person{Ada}{Lovelace}}"
            r"\begin{document}\end{document}"
        )
        snapshot = p.build_discovery_facts(
            p.DocumentSnapshot.from_directory(tmp_path, main)
        )
        assert snapshot.discovery.authors == ("Ada Lovelace",)

    def test_acyclic_expansion_is_idempotent(self):
        macros = {
            "outer": p.MacroDefinition(r"\inner{#1}", 1),
            "inner": p.MacroDefinition("[#1]", 1),
        }
        once = p.expand_macros(r"\outer{x}", macros)
        assert p.expand_macros(once, macros) == once == "[x]"

    def test_non_fixed_point_cycle_is_strictly_depth_limited(self):
        macros = {
            "a": p.MacroDefinition(r"\b"),
            "b": p.MacroDefinition(r"\a"),
        }
        assert p.expand_macros(r"\a", macros, depth=1) == r"\b"
        assert p.expand_macros(r"\a", macros, depth=2) == r"\a"


class TestPreambleCleanupPlanners:
    def test_filters_only_the_package_list_argument(self, tmp_path):
        content = r"Lead \usepackage[final]{amsmath, hyperref} tail"
        source = p.SourceFile(tmp_path / "main.tex", content)

        outcome = p.plan_filter_packages(source, p.LatexDocument(source))

        assert p.EditPlanner.apply(source, outcome.edits) == (
            r"Lead \usepackage[final]{amsmath} tail"
        )

    def test_complete_config_and_internal_definition_are_removed(self, tmp_path):
        content = (
            r"A\hypersetup{colorlinks={true}}"
            r"\newcommand{\pkg@internal}[1]{outer{#1}}Z"
        )
        source = p.SourceFile(tmp_path / "main.tex", content)

        outcome = p.plan_strip_config(source, p.LatexDocument(source))

        assert p.EditPlanner.apply(source, outcome.edits) == "AZ"

    def test_incomplete_config_is_preserved_with_diagnostic(self, tmp_path):
        content = r"Before \hypersetup{colorlinks=true After"
        source = p.SourceFile(tmp_path / "main.tex", content)

        outcome = p.plan_strip_config(source, p.LatexDocument(source))

        assert p.EditPlanner.apply(source, outcome.edits) == content
        assert any(d.code == "opaque-structure" for d in outcome.diagnostics)

    def test_internal_definition_suppresses_nested_package_edit(self, tmp_path):
        content = r"A\newcommand{\pkg@internal}{\usepackage{hyperref}}Z"
        source = p.SourceFile(tmp_path / "main.tex", content)

        outcome = p._plan_problematic_packages(source, p.LatexDocument(source))

        assert p.EditPlanner.apply(source, outcome.edits) == "AZ"

    def test_package_and_config_cleanup_is_idempotent(self, tmp_path):
        source = p.SourceFile(
            tmp_path / "main.tex",
            r"\usepackage{amsmath,hyperref}\hypersetup{x={y}}Body",
        )
        first = p.EditPlanner.apply(
            source,
            p._plan_problematic_packages(source, p.LatexDocument(source)).edits,
        )
        rewritten = p.SourceFile(source.path, first)
        second = p._plan_problematic_packages(
            rewritten, p.LatexDocument(rewritten),
        )

        assert p.EditPlanner.apply(rewritten, second.edits) == first

    def test_config_cleanup_preserves_graphicspath(self, tmp_path):
        content = (
            r"\graphicspath{{figures/}}"
            r"\hypersetup{colorlinks=true}"
        )
        source = p.SourceFile(tmp_path / "main.tex", content)

        outcome = p._plan_problematic_packages(source, p.LatexDocument(source))

        result = p.EditPlanner.apply(source, outcome.edits)
        assert result == r"\graphicspath{{figures/}}"


class TestStripNoisePlanner:
    @pytest.mark.parametrize("content", [
        r"A\noindent B",
        r"A\notag B",
        r"A\stepcounter{equation}B",
        r"A\csgdef{key}{value}B",
    ])
    def test_removes_registered_complete_noise_commands(self, tmp_path, content):
        source = p.SourceFile(tmp_path / "main.tex", content)

        outcome = p.plan_strip_noise(source, p.LatexDocument(source))

        assert p.EditPlanner.apply(source, outcome.edits) == "AB"

    def test_nested_ccsdesc_is_removed_as_one_complete_command(self, tmp_path):
        content = r"Before \ccsdesc[500]{Computing~{Machine learning}} After"
        source = p.SourceFile(tmp_path / "main.tex", content)
        outcome = p.plan_strip_noise(source, p.LatexDocument(source))
        assert p.EditPlanner.apply(source, outcome.edits) == "Before  After"

    def test_removes_tiny_font_declaration_from_math(self, tmp_path):
        content = (
            r"\newenvironment{dedication}"
            r"{\begin{minipage}}{\end{minipage}}"
            r"\begin{document}$p_{\tiny{R}}(\cdot\mid x,u)$\end{document}"
        )
        source = p.SourceFile(tmp_path / "main.tex", content)
        document = p.LatexDocument(source)

        tiny = document.commands("tiny")[0]
        assert tiny.complete
        assert not tiny.opaque

        outcome = p.plan_strip_noise(source, document)

        result = p.EditPlanner.apply(source, outcome.edits)
        assert result == content.replace(r"\tiny", "", 1)

    def test_tiny_inside_removed_environment_does_not_overlap(self, tmp_path):
        content = r"A\begin{tikzpicture}\tiny x\end{tikzpicture}Z"
        source = p.SourceFile(tmp_path / "main.tex", content)

        outcome = p.plan_strip_noise(source, p.LatexDocument(source))

        assert p.EditPlanner.apply(source, outcome.edits) == "AZ"

    def test_tiny_inside_removed_command_does_not_overlap(self, tmp_path):
        content = r"A\marginpar{\tiny x}Z"
        source = p.SourceFile(tmp_path / "main.tex", content)

        outcome = p.plan_strip_noise(source, p.LatexDocument(source))

        assert p.EditPlanner.apply(source, outcome.edits) == "AZ"

    @pytest.mark.parametrize("command", ["newcommand", "renewcommand"])
    def test_preserves_tiny_as_macro_definition_name(
        self, tmp_path, command,
    ):
        content = rf"\{command}{{\tiny}}{{custom}}"
        source = p.SourceFile(tmp_path / "main.tex", content)

        outcome = p.plan_strip_noise(source, p.LatexDocument(source))

        assert p.EditPlanner.apply(source, outcome.edits) == content

    def test_incomplete_two_argument_noise_command_is_preserved(self, tmp_path):
        content = r"Before \setlength{\parindent} After"
        source = p.SourceFile(tmp_path / "main.tex", content)
        outcome = p.plan_strip_noise(source, p.LatexDocument(source))
        assert p.EditPlanner.apply(source, outcome.edits) == content
        assert any(d.code == "opaque-structure" for d in outcome.diagnostics)

    def test_nested_comment_environment_is_removed_as_outermost_node(self, tmp_path):
        content = (
            r"A\begin{comment}outer \begin{comment}inner\end{comment} "
            r"tail\end{comment}Z"
        )
        source = p.SourceFile(tmp_path / "main.tex", content)
        outcome = p.plan_strip_noise(source, p.LatexDocument(source))
        assert p.EditPlanner.apply(source, outcome.edits) == "AZ"

    def test_is_idempotent(self, tmp_path):
        source = p.SourceFile(
            tmp_path / "main.tex",
            r"A\vspace*{1em}\begin{CCSXML}x\end{CCSXML}Z",
        )
        first = p.EditPlanner.apply(
            source,
            p.plan_strip_noise(source, p.LatexDocument(source)).edits,
        )
        rewritten = p.SourceFile(source.path, first)
        second = p.plan_strip_noise(rewritten, p.LatexDocument(rewritten))
        assert p.EditPlanner.apply(rewritten, second.edits) == first


class TestNormalizeCitations:
    @staticmethod
    def apply(tmp_path, content):
        source = p.SourceFile(tmp_path / "main.tex", content)
        outcome = p.plan_normalize_citations(source, p.LatexDocument(source))
        return source, outcome, p.EditPlanner.apply(source, outcome.edits)

    def test_citep(self, tmp_path):
        _, _, result = self.apply(tmp_path, r"\citep{smith2020}")
        assert result == r"\cite{smith2020}"

    def test_textcite(self, tmp_path):
        _, _, result = self.apply(tmp_path, r"\textcite{jones2021}")
        assert result == r"\cite{jones2021}"

    def test_nested_citation_notes_are_consumed_as_arguments(self, tmp_path):
        content = r"See \citep[see {Appendix A}][pp.~{2--3}]{alpha,beta}."
        _, _, result = self.apply(tmp_path, content)
        assert result == r"See \cite{alpha,beta}."

    def test_incomplete_citation_is_preserved_with_diagnostic(self, tmp_path):
        content = r"See \citep[see]{alpha"
        _, outcome, result = self.apply(tmp_path, content)
        assert result == content
        assert any(d.code == "opaque-structure" for d in outcome.diagnostics)

    def test_existing_cite_unchanged(self, tmp_path):
        original = r"\cite{ref}"
        _, _, result = self.apply(tmp_path, original)
        assert result == original

    def test_apply_reparse_replan_is_idempotent(self, tmp_path):
        source, _, first = self.apply(
            tmp_path, r"\citep[see {A}][p.~2]{alpha}",
        )
        assert first == r"\cite{alpha}"
        rewritten = p.SourceFile(source.path, first)
        second = p.plan_normalize_citations(
            rewritten, p.LatexDocument(rewritten),
        )
        assert second.edits == ()


class TestReferencePlanners:
    @pytest.mark.parametrize(
        "command,args",
        [*(
            (name, "{key}") for name in p._CITE_ALIASES
        ),
        ("hyperref", "[target]{text}"),
        ("hyperlink", "{target}{text}"),
        ("hypertarget", "{target}{text}"),
        ("Hy@raisedlink", "{text}"),
        ("texorpdfstring", "{tex}{pdf}"),
        ("ding", "{51}"),
        ("textcircled", "{1}"),
        ],
    )
    def test_parser_registers_reference_command_signatures(
        self, tmp_path, command, args,
    ):
        source = p.SourceFile(tmp_path / "main.tex", f"\\{command}{args}")
        refs = p.LatexDocument(source).commands(command)
        assert len(refs) == 1
        assert refs[0].complete

    def test_link_commands_unwrap_nested_arguments(self, tmp_path):
        content = (
            r"\hyperref[sec:{one}]{See {Section One}} "
            r"\hyperlink{target}{link {text}} "
            r"\hypertarget{target}{anchor} "
            r"\Hy@raisedlink{raised} "
            r"\texorpdfstring{TeX {text}}{PDF text}"
        )
        source = p.SourceFile(tmp_path / "main.tex", content)
        outcome = p.plan_preprocess_links(source, p.LatexDocument(source))
        assert p.EditPlanner.apply(source, outcome.edits) == (
            "See {Section One} link {text} anchor raised TeX {text}"
        )

    def test_incomplete_link_is_preserved_with_diagnostic(self, tmp_path):
        content = r"before \hyperref[sec]{broken"
        source = p.SourceFile(tmp_path / "main.tex", content)
        outcome = p.plan_preprocess_links(source, p.LatexDocument(source))
        assert p.EditPlanner.apply(source, outcome.edits) == content
        assert any(d.code == "opaque-structure" for d in outcome.diagnostics)

    def test_inline_symbols_replace_known_and_preserve_unknown(self, tmp_path):
        content = r"\ding{51} \ding{999} $\textcircled{2}$ \textcircled{99}"
        source = p.SourceFile(tmp_path / "main.tex", content)
        outcome = p.plan_inline_symbols(source, p.LatexDocument(source))
        assert p.EditPlanner.apply(source, outcome.edits) == (
            r"✓ \ding{999} ② \textcircled{99}"
        )

    def test_incomplete_symbol_is_preserved_with_diagnostic(self, tmp_path):
        content = r"\ding{51"
        source = p.SourceFile(tmp_path / "main.tex", content)
        outcome = p.plan_inline_symbols(source, p.LatexDocument(source))
        assert p.EditPlanner.apply(source, outcome.edits) == content
        assert any(d.code == "opaque-structure" for d in outcome.diagnostics)

    def test_links_apply_reparse_replan_is_idempotent(self, tmp_path):
        source = p.SourceFile(
            tmp_path / "main.tex",
            r"\hyperref[sec]{See \texorpdfstring{TeX}{PDF}}",
        )
        first_outcome = p.plan_preprocess_links(source, p.LatexDocument(source))
        first = p.EditPlanner.apply(source, first_outcome.edits)
        assert first == "See TeX"
        rewritten = p.SourceFile(source.path, first)
        second = p.plan_preprocess_links(rewritten, p.LatexDocument(rewritten))
        assert second.edits == ()

    def test_symbols_apply_reparse_replan_is_idempotent(self, tmp_path):
        source = p.SourceFile(
            tmp_path / "main.tex", r"\ding{51} $\textcircled{2}$",
        )
        first_outcome = p.plan_inline_symbols(source, p.LatexDocument(source))
        first = p.EditPlanner.apply(source, first_outcome.edits)
        assert first == "✓ ②"
        rewritten = p.SourceFile(source.path, first)
        second = p.plan_inline_symbols(rewritten, p.LatexDocument(rewritten))
        assert second.edits == ()


class TestStripAnnotationPlanner:
    def test_rewrites_atran_body_and_removes_helpers_structurally(self, tmp_path):
        content = (
            r"\newcommand{\atran}[2]{\overset{#2}{#1}}"
            r"\newcommand{\annotateinitused}{\setcounter{x}{0}}"
            r"\newcounter{annotatecount}Body"
        )
        source = p.SourceFile(tmp_path / "main.tex", content)

        outcome = p.plan_strip_annotations(source, p.LatexDocument(source))

        assert p.EditPlanner.apply(source, outcome.edits) == (
            r"\newcommand{\atran}[2]{#1}Body"
        )

    def test_incomplete_atran_definition_is_preserved(self, tmp_path):
        content = r"\newcommand{\atran}[2]{unfinished"
        source = p.SourceFile(tmp_path / "main.tex", content)

        outcome = p.plan_strip_annotations(source, p.LatexDocument(source))

        assert p.EditPlanner.apply(source, outcome.edits) == content
        assert any(d.code == "opaque-structure" for d in outcome.diagnostics)

    def test_helper_definition_suppresses_nested_counter_edit(self, tmp_path):
        content = r"A\newcommand{\annotateinitused}{\newcounter{annotatecount}}Z"
        source = p.SourceFile(tmp_path / "main.tex", content)

        outcome = p.plan_strip_annotations(source, p.LatexDocument(source))

        assert p.EditPlanner.apply(source, outcome.edits) == "AZ"

    def test_atran_body_replacement_suppresses_nested_candidates(self, tmp_path):
        content = (
            r"\newcommand{\atran}[2]{"
            r"\newcommand{\annotateinitused}{x}"
            r"\newcounter{annotatecount}#1}"
        )
        source = p.SourceFile(tmp_path / "main.tex", content)

        outcome = p.plan_strip_annotations(source, p.LatexDocument(source))

        assert p.EditPlanner.apply(source, outcome.edits) == (
            r"\newcommand{\atran}[2]{#1}"
        )

    def test_annotation_cleanup_is_idempotent(self, tmp_path):
        source = p.SourceFile(
            tmp_path / "main.tex",
            r"\newcommand{\atran}[2]{x}\newcounter{annotatecount}Body",
        )
        first = p.EditPlanner.apply(
            source,
            p.plan_strip_annotations(source, p.LatexDocument(source)).edits,
        )
        rewritten = p.SourceFile(source.path, first)
        second = p.plan_strip_annotations(rewritten, p.LatexDocument(rewritten))

        assert p.EditPlanner.apply(rewritten, second.edits) == first


class TestPlanPreprocessLinks:
    @staticmethod
    def apply(content):
        source = p.SourceFile(Path("<test>"), content)
        outcome = p.plan_preprocess_links(source, p.LatexDocument(source))
        return p.EditPlanner.apply(source, outcome.edits)

    def test_hyperref_unwrapped(self):
        result = self.apply(r"\hyperref[sec:intro]{Introduction}")
        assert result == "Introduction"

    def test_hyperref_unwrapped_with_nested_visible_text(self):
        result = self.apply(r"\hyperref[sec:intro]{See \textbf{Introduction}}")
        assert result == r"See \textbf{Introduction}"

    def test_texorpdfstring(self):
        result = self.apply(r"\texorpdfstring{$\alpha$}{alpha}")
        assert result == "$\\alpha$"

    def test_combined(self):
        content = r"\hyperref[tab]{\texorpdfstring{$x$}{x} table}"
        result = self.apply(content)
        assert "$x$ table" == result

    def test_hy_raisedlink(self):
        result = self.apply(r"\Hy@raisedlink{\hypertarget{lbl}{}} text")
        assert "Hy@raisedlink" not in result

    def test_hypertarget(self):
        result = self.apply(r"\hypertarget{label}{visible text}")
        assert result == "visible text"

    def test_preserves_generated_algorithm_anchor(self):
        content = p.build_algorithm_output(
            "Work", "alg:work", [(1, 0, "Go")], 1,
        )

        assert self.apply(content) == content

    def test_unwraps_visible_hypertarget_inside_algorithm_display(self):
        content = (
            r"\begin{algorithmdisplay}"
            r"\hypertarget{user}{Visible text}"
            r"\end{algorithmdisplay}"
        )

        assert self.apply(content) == (
            r"\begin{algorithmdisplay}Visible text\end{algorithmdisplay}"
        )

    def test_unwraps_empty_user_anchor_away_from_generated_marker(self):
        content = (
            r"\begin{algorithmdisplay}User text "
            r"\hypertarget{user}{}"
            r"\end{algorithmdisplay}"
        )

        assert self.apply(content) == (
            r"\begin{algorithmdisplay}User text \end{algorithmdisplay}"
        )

    def test_incomplete_algorithm_anchor_keeps_reference_diagnostic(self, tmp_path):
        content = (
            r"\begin{algorithmdisplay}"
            r"\hypertarget{alg:broken}{unfinished"
            r"\end{algorithmdisplay}"
        )
        source = p.SourceFile(tmp_path / "main.tex", content)

        outcome = p.plan_preprocess_links(source, p.LatexDocument(source))

        assert p.EditPlanner.apply(source, outcome.edits) == content
        assert any(item.code == "opaque-structure" for item in outcome.diagnostics)

    def test_hyperlink(self):
        result = self.apply(r"\hyperlink{target}{click here}")
        assert result == "click here"


class TestPlanNormalizeTables:
    @pytest.mark.parametrize(("content", "expected"), [
        (
            r"\begin{tblr}{colspec={X[l]X[r]},rowsep=2pt}a&b\end{tblr}",
            r"\begin{tabular}{l}a&b\end{tabular}",
        ),
        (
            r"\begin{tabular}{l@{\hspace{1em}}r}a&b\end{tabular}",
            r"\begin{tabular}{lr}a&b\end{tabular}",
        ),
    ])
    def test_nested_table_arguments_remain_balanced(
        self, content, expected, tmp_path,
    ):
        source = p.SourceFile(tmp_path / "main.tex", content)
        outcome = p.plan_normalize_tables(source, p.LatexDocument(source))

        assert p.EditPlanner.apply(source, outcome.edits) == expected

    @pytest.mark.parametrize(("content", "expected"), [
        (
            r"\begin{tabularx}[t]{\textwidth}{lS[round-mode=places]r}x\end{tabularx}",
            r"\begin{tabular}{lrr}x\end{tabular}",
        ),
        (
            r"\begin{tabulary}{\linewidth}{@{\hspace{.5em}}lr}x\end{tabulary}",
            r"\begin{tabular}{lr}x\end{tabular}",
        ),
        (
            r"\begin{xltabular}{\linewidth}{lS[table-format=2.1]}x\end{xltabular}",
            r"\begin{longtable}{lr}x\end{longtable}",
        ),
        (
            r"\begin{NiceTabular}[small]{l@{x{y}}r}[hvlines]x\end{NiceTabular}",
            r"\begin{tabular}{lr}x\end{tabular}",
        ),
        (
            r"\begin{NiceTabular*}{\textwidth}[t]{lS[table-format=2.1]}[hvlines]x\end{NiceTabular*}",
            r"\begin{tabular}{lr}x\end{tabular}",
        ),
        (
            r"\begin{tabu}{lr}x\end{tabu}",
            r"\begin{tabular}{lr}x\end{tabular}",
        ),
        (
            r"\begin{ltablex}{lr}x\end{ltablex}",
            r"\begin{longtable}{lr}x\end{longtable}",
        ),
    ])
    def test_renames_environment_and_normalizes_parsed_column_spec(
        self, content, expected, tmp_path,
    ):
        source = p.SourceFile(tmp_path / "main.tex", content)
        outcome = p.plan_normalize_tables(source, p.LatexDocument(source))

        assert p.EditPlanner.apply(source, outcome.edits) == expected

    def test_multicolumn_column_spec_uses_balanced_groups(self, tmp_path):
        content = r"\multicolumn{2}{l@{\hspace{1em}}S[table-format=2.1]}{value}"
        source = p.SourceFile(tmp_path / "main.tex", content)
        outcome = p.plan_normalize_tables(source, p.LatexDocument(source))

        assert p.EditPlanner.apply(source, outcome.edits) == (
            r"\multicolumn{2}{lr}{value}"
        )

    def test_repeated_column_spec_is_normalized_recursively(self, tmp_path):
        content = (
            r"\begin{tabular}{*{2}{S[table-format=2.1]@{\hspace{1em}}}}"
            r"a&b\end{tabular}"
        )
        source = p.SourceFile(tmp_path / "main.tex", content)
        outcome = p.plan_normalize_tables(source, p.LatexDocument(source))

        assert p.EditPlanner.apply(source, outcome.edits) == (
            r"\begin{tabular}{*{2}{r}}a&b\end{tabular}"
        )

    def test_incomplete_column_spec_is_preserved(self, tmp_path):
        content = r"\begin{tabular}{l@{\hspace{1em}}r"
        source = p.SourceFile(tmp_path / "main.tex", content)
        outcome = p.plan_normalize_tables(source, p.LatexDocument(source))

        assert p.EditPlanner.apply(source, outcome.edits) == content
        assert any(
            diagnostic.code == "opaque-structure"
            for diagnostic in outcome.diagnostics
        )

    @pytest.mark.parametrize("content", [
        r"\begin{NiceTabular}{lr}[hvlines x\end{NiceTabular}",
        r"\begin{NiceTabular}{lr} [hvlines x\end{NiceTabular}",
    ])
    def test_incomplete_nicetabular_trailing_options_are_preserved(
        self, content, tmp_path,
    ):
        source = p.SourceFile(tmp_path / "main.tex", content)
        outcome = p.plan_normalize_tables(source, p.LatexDocument(source))

        assert p.EditPlanner.apply(source, outcome.edits) == content
        assert any(
            diagnostic.code == "opaque-structure"
            for diagnostic in outcome.diagnostics
        )

    @pytest.mark.parametrize("content", [
        r"\begin{tblr}{colspec={X[l]X[r]},rowsep=2pt}a&b\end{tblr}",
        r"\begin{tabularx}{\textwidth}{l@{x{y}}S[table-format=2.1]}a&b\end{tabularx}",
        r"\multicolumn{2}{l@{\hspace{1em}}S[table-format=2.1]}{value}",
    ])
    def test_apply_reparse_replan_is_idempotent(self, content, tmp_path):
        source = p.SourceFile(tmp_path / "main.tex", content)
        first = p.EditPlanner.apply(
            source,
            p.plan_normalize_tables(source, p.LatexDocument(source)).edits,
        )
        rewritten = p.SourceFile(source.path, first)
        second = p.plan_normalize_tables(
            rewritten, p.LatexDocument(rewritten),
        )

        assert p.EditPlanner.apply(rewritten, second.edits) == first


class TestPlanTableFloatWrappers:
    @staticmethod
    def apply(tmp_path, content, planner):
        source = p.SourceFile(tmp_path / "main.tex", content)
        outcome = planner(source, p.LatexDocument(source))
        return p.EditPlanner.apply(source, outcome.edits), outcome

    @pytest.mark.parametrize(("planner", "content", "expected"), [
        (
            p.plan_unwrap_makecells,
            r"\makecell[c]{A \\[2pt] \makecell{B \\ C}}",
            "A B C",
        ),
        (
            p.plan_unwrap_resizeboxes,
            r"\resizebox*{\textwidth}{!}{A \resizebox{1cm}{!}{B}}",
            "A B",
        ),
        (
            p.plan_unwrap_adjustboxes,
            r"\begin{adjustbox}{width=1cm}A \adjustbox{max width=1cm}{B}\end{adjustbox}",
            "A B",
        ),
        (
            p.plan_convert_wrapfigures,
            r"\begin{wrapfigure}[2]{r}{.5\textwidth}img\end{wrapfigure}",
            r"\begin{figure}[H]img\end{figure}",
        ),
        (
            p.plan_unwrap_subfigures,
            r"\begin{subfigure}[b]{.5\textwidth}img\end{subfigure}",
            "img",
        ),
        (
            p.plan_destar_floats,
            r"\begin{table*}a\end{table*}\begin{figure*}b\end{figure*}",
            r"\begin{table}a\end{table}\begin{figure}b\end{figure}",
        ),
    ])
    def test_planners_handle_nested_or_argument_bearing_syntax(
        self, tmp_path, planner, content, expected,
    ):
        actual, _ = self.apply(tmp_path, content, planner)
        assert actual == expected

    @pytest.mark.parametrize("planner,content", [
        (p.plan_unwrap_makecells, r"\makecell[c]{A"),
        (p.plan_unwrap_resizeboxes, r"\resizebox{1cm}{!}{A"),
        (p.plan_unwrap_adjustboxes, r"\begin{adjustbox}{width=1cm}A"),
        (p.plan_unwrap_minipages, r"\begin{minipage}{2cm}A"),
        (p.plan_convert_wrapfigures, r"\begin{wrapfigure}{r}{2cm}A"),
        (p.plan_unwrap_subfigures, r"\begin{subfigure}{2cm}A"),
    ])
    def test_malformed_wrappers_are_preserved_with_diagnostic(
        self, tmp_path, planner, content,
    ):
        actual, outcome = self.apply(tmp_path, content, planner)
        assert actual == content
        assert any(d.code == "opaque-structure" for d in outcome.diagnostics)

    @pytest.mark.parametrize("planner,content", [
        (p.plan_unwrap_makecells, r"\makecell{A \\ \makecell{B \\ C}}"),
        (p.plan_unwrap_resizeboxes, r"\resizebox{1cm}{!}{\resizebox{2cm}{!}{x}}"),
        (p.plan_unwrap_adjustboxes, r"\begin{adjustbox}{width=1cm}\adjustbox{x}{y}\end{adjustbox}"),
        (p.plan_unwrap_minipages, r"\begin{minipage}{2cm}\begin{minipage}{1cm}x\end{minipage}\end{minipage}"),
        (p.plan_convert_wrapfigures, r"\begin{wrapfigure}{r}{2cm}x\end{wrapfigure}"),
        (p.plan_unwrap_subfigures, r"\begin{subfigure}{2cm}x\end{subfigure}"),
        (p.plan_destar_floats, r"\begin{figure*}x\end{figure*}"),
    ])
    def test_apply_reparse_replan_is_idempotent(
        self, tmp_path, planner, content,
    ):
        first, _ = self.apply(tmp_path, content, planner)
        rewritten = p.SourceFile(tmp_path / "main.tex", first)
        second = planner(rewritten, p.LatexDocument(rewritten))
        assert second.edits == ()

    def test_starred_resizebox_inside_opaque_ancestor_is_preserved(
        self, tmp_path,
    ):
        content = r"\section{outer \resizebox*{1cm}{!}{inner}"

        actual, outcome = self.apply(
            tmp_path, content, p.plan_unwrap_resizeboxes,
        )

        assert actual == content
        assert any(d.code == "opaque-structure" for d in outcome.diagnostics)

    def test_starred_resizebox_with_foreign_reference_is_preserved(
        self, tmp_path,
    ):
        content = r"\resizebox*{1cm}{!}{inner}"
        source = p.SourceFile(tmp_path / "main.tex", content)
        foreign = p.SourceFile(tmp_path / "foreign.tex", content)

        outcome = p.plan_unwrap_resizeboxes(
            source, p.LatexDocument(foreign),
        )

        assert outcome.edits == ()
        assert any(d.code == "foreign-reference" for d in outcome.diagnostics)

    def test_fragment_planner_maps_diagnostic_to_parent_source(self, tmp_path):
        source = p.SourceFile(tmp_path / "main.tex", "prefix INNER suffix")
        start = source.content.index("INNER")
        end = start + len("INNER")

        def diagnose(fragment, document):
            return p.PlanOutcome(diagnostics=(p.Diagnostic(
                p.Path("<fragment>"),
                "inner_planner",
                "opaque-structure",
                "nested malformed wrapper",
                1,
                4,
            ),))

        outcome = p._apply_fragment_planner(
            source,
            start,
            end,
            diagnose,
            "outer_planner",
            p.Safety.LOSSY,
        )

        assert outcome.edits == ()
        assert outcome.diagnostics == (p.Diagnostic(
            source.path,
            "inner_planner",
            "opaque-structure",
            "nested malformed wrapper",
            start + 1,
            start + 4,
        ),)

    @pytest.mark.parametrize(("planner", "fragment"), [
        (p.plan_unwrap_makecells, r"\makecell"),
        (p.plan_unwrap_resizeboxes, r"\resizebox{1cm}"),
        (p.plan_unwrap_adjustboxes, r"\adjustbox{width=1cm}"),
    ], ids=("makecell", "resizebox", "adjustbox"))
    def test_nested_malformed_fragment_diagnostic_uses_parent_coordinates(
        self, tmp_path, planner, fragment,
    ):
        content = f"prefix {fragment} suffix"
        source = p.SourceFile(tmp_path / "main.tex", content)
        start = content.index(fragment)
        end = start + len(fragment)

        outcome = p._apply_fragment_planner(
            source, start, end, planner, "outer", p.Safety.LOSSY,
        )

        assert outcome.edits == ()
        assert outcome.diagnostics
        assert all(d.file == source.path for d in outcome.diagnostics)
        assert all(
            d.start is None or start <= d.start <= d.end <= end
            for d in outcome.diagnostics
        )

    @pytest.mark.parametrize(("planner", "content"), [
        (p.plan_unwrap_makecells, r"\makecell{outer}"),
        (p.plan_unwrap_resizeboxes, r"\resizebox{1cm}{!}{outer}"),
        (p.plan_unwrap_adjustboxes, r"\adjustbox{width=1cm}{outer}"),
    ], ids=("makecell", "resizebox", "adjustbox"))
    def test_outer_wrapper_preserves_nested_fragment_diagnostic(
        self, tmp_path, monkeypatch, planner, content,
    ):
        source = p.SourceFile(tmp_path / "main.tex", content)

        def malformed_fragment(
            parent, start, end, nested_planner, pass_name, safety,
        ):
            return p.PlanOutcome(
                edits=(p.Edit(
                    parent.path, start, end, "nested", pass_name, safety,
                ),),
                diagnostics=(p.Diagnostic(
                    parent.path,
                    nested_planner.__name__,
                    "opaque-structure",
                    "nested malformed wrapper",
                    start,
                    end,
                ),),
            )

        monkeypatch.setattr(p, "_apply_fragment_planner", malformed_fragment)

        outcome = planner(source, p.LatexDocument(source))

        assert p.EditPlanner.apply(source, outcome.edits) == "nested"
        assert len(outcome.edits) == 1
        assert outcome.edits[0].safety is p.Safety.LOSSY
        assert outcome.diagnostics[0].code == "opaque-structure"
        assert outcome.diagnostics[0].file == source.path


class TestMinipageCaptionInteractions:
    @staticmethod
    def run_ordered(tmp_path, content):
        main = tmp_path / "main.tex"
        main.write_text(content)
        specs = p.build_table_float_passes()
        result = p.PassPipeline(specs).run(
            p.DocumentSnapshot.from_directory(tmp_path, main)
        )
        return result.snapshot.sources[main].content

    def test_standalone_minipage_is_unwrapped(self, tmp_path):
        assert self.run_ordered(
            tmp_path, r"\begin{minipage}{2cm}body\end{minipage}",
        ) == "body"

    def test_nested_minipages_are_unwrapped_without_overlap(self, tmp_path):
        content = (
            r"\begin{minipage}{2cm}outer "
            r"\begin{minipage}{1cm}inner\end{minipage}"
            r"\end{minipage}"
        )
        assert self.run_ordered(tmp_path, content) == "outer inner"

    @pytest.mark.parametrize("outer", ["table", "tabular"])
    def test_minipage_inside_table_structure_is_unwrapped(
        self, tmp_path, outer,
    ):
        argument = "{c}" if outer == "tabular" else ""
        content = (
            f"\\begin{{{outer}}}{argument}"
            r"\begin{minipage}{2cm}cell\end{minipage}"
            f"\\end{{{outer}}}"
        )
        result = self.run_ordered(tmp_path, content)
        assert "minipage" not in result
        assert f"\\begin{{{outer}}}" in result

    def test_captionof_standalone_minipage_becomes_float(self, tmp_path):
        content = (
            r"\begin{minipage}{\textwidth}img"
            r"\captionof{figure}{Caption}\end{minipage}"
        )
        assert self.run_ordered(tmp_path, content) == (
            r"\begin{figure}[H]img\caption{Caption}\end{figure}"
        )

    def test_captionof_table_does_not_nest_same_type_float(self, tmp_path):
        content = (
            r"\begin{table}\begin{minipage}{\textwidth}x"
            r"\captionof{table}{Caption}\end{minipage}\end{table}"
        )
        result = self.run_ordered(tmp_path, content)
        assert result == r"\begin{table}x\caption{Caption}\end{table}"
        assert result.count(r"\begin{table}") == 1

    def test_captionof_table_inside_starred_table_does_not_nest(
        self, tmp_path,
    ):
        content = (
            r"\begin{table*}\begin{minipage}{\textwidth}x"
            r"\captionof{table}{Caption}\end{minipage}\end{table*}"
        )
        source = p.SourceFile(tmp_path / "main.tex", content)

        resolved = p.EditPlanner.apply(
            source,
            p.plan_resolve_captionof(source, p.LatexDocument(source)).edits,
        )
        resolved_source = p.SourceFile(source.path, resolved)
        result = p.EditPlanner.apply(
            resolved_source,
            p.plan_destar_floats(
                resolved_source, p.LatexDocument(resolved_source),
            ).edits,
        )

        assert result == r"\begin{table}x\caption{Caption}\end{table}"
        assert result.count(r"\begin{table}") == 1

    def test_additional_captionof_in_same_minipage_is_preserved(
        self, tmp_path,
    ):
        content = (
            r"\begin{minipage}{\textwidth}x"
            r"\captionof{figure}{First}\captionof{figure}{Second}"
            r"\end{minipage}"
        )
        source = p.SourceFile(tmp_path / "main.tex", content)

        result = p.EditPlanner.apply(
            source,
            p.plan_resolve_captionof(source, p.LatexDocument(source)).edits,
        )

        assert r"\caption{First}" in result
        assert r"\captionof{figure}{Second}" in result

    def test_pass_order_preserves_caption_wrapper_until_resolution(self):
        specs = p.build_table_float_passes()
        by_name = {spec.name: spec for spec in specs}
        assert "normalize_tables" in by_name["unwrap_makecell"].after
        assert "resolve_captionof" in by_name["unwrap_minipages"].after
        assert "normalize_tables" in by_name["unwrap_minipages"].after
        assert "resolve_captionof" in by_name["destar_floats"].after
        ordered = [spec.name for spec in p.resolve_pass_order(specs)]
        assert ordered.index("resolve_captionof") < ordered.index("unwrap_minipages")
        assert ordered.index("resolve_captionof") < ordered.index("destar_floats")
        assert ordered.index("normalize_tables") < ordered.index("unwrap_makecell")


class TestDestarFloats:
    def test_table_star(self):
        content = r"\begin{table*}x\end{table*}"
        result = content
        for env in ("table", "figure"):
            result = result.replace(f"\\begin{{{env}*}}", f"\\begin{{{env}}}")
            result = result.replace(f"\\end{{{env}*}}", f"\\end{{{env}}}")
        assert r"\begin{table}" in result
        assert r"\end{table}" in result

    def test_figure_star(self):
        content = r"\begin{figure*}x\end{figure*}"
        result = content
        for env in ("table", "figure"):
            result = result.replace(f"\\begin{{{env}*}}", f"\\begin{{{env}}}")
            result = result.replace(f"\\end{{{env}*}}", f"\\end{{{env}}}")
        assert r"\begin{figure}" in result


class TestConvertWrapfigure:
    def test_basic(self, tmp_path):
        content = r"\begin{wrapfigure}{r}{0.5\textwidth}img\end{wrapfigure}"
        source = p.SourceFile(tmp_path / "main.tex", content)
        outcome = p.plan_convert_wrapfigures(source, p.LatexDocument(source))
        result = p.EditPlanner.apply(source, outcome.edits)
        assert r"\begin{figure}[H]" in result
        assert r"\end{figure}" in result


class TestUnwrapSubfigures:
    def test_basic(self, tmp_path):
        content = r"\begin{subfigure}[b]{0.5\textwidth}img\end{subfigure}"
        source = p.SourceFile(tmp_path / "main.tex", content)
        outcome = p.plan_unwrap_subfigures(source, p.LatexDocument(source))
        result = p.EditPlanner.apply(source, outcome.edits)
        assert "subfigure" not in result
        assert "img" in result


# ── Ding commands ───────────────────────────────────────────────────────────


class TestDingMap:
    @staticmethod
    def apply(tmp_path, content):
        source = p.SourceFile(tmp_path / "main.tex", content)
        outcome = p.plan_inline_symbols(source, p.LatexDocument(source))
        return p.EditPlanner.apply(source, outcome.edits)

    def test_checkmark(self, tmp_path):
        assert self.apply(tmp_path, r"\ding{51}") == "✓"

    def test_unknown_code_preserved(self, tmp_path):
        assert self.apply(tmp_path, r"\ding{999}") == r"\ding{999}"


# ── Translation helpers ────────────────────────────────────────────────────


class TestExtractGlossary:
    def test_limits_glossary_to_fifty_terms(self, monkeypatch):
        prompts = []

        def fake_chat(client, system_prompt, user_prompt):
            prompts.append(system_prompt)
            return "term | 术语"

        monkeypatch.setattr(p, "_chat", fake_chat)

        assert p.extract_glossary(object(), "Title", "Abstract", ["Intro"])
        assert "最多提取 50 个" in prompts[0]


class TestExtractSectionHeadings:
    @staticmethod
    def extract(tmp_path, tex):
        snapshot = p.DocumentSnapshot.from_directory(tmp_path, tex)
        return p.extract_snapshot_section_headings(snapshot)

    def test_supports_optional_arguments_and_nested_commands(self, tmp_path):
        tex = tmp_path / "section.tex"
        tex.write_text(r"\section[Short]{About \textbf{Method}}")

        assert self.extract(tmp_path, tex) == [r"About \textbf{Method}"]

    def test_includes_paragraph_with_nested_title(self, tmp_path):
        tex = tmp_path / "section.tex"
        tex.write_text(r"\paragraph[Short]{About \textbf{Method}}")

        assert self.extract(tmp_path, tex) == [r"About \textbf{Method}"]

    def test_includes_chapter_title(self, tmp_path):
        tex = tmp_path / "chapter.tex"
        tex.write_text(r"\chapter{Deep Reinforcement Learning}")

        assert self.extract(tmp_path, tex) == ["Deep Reinforcement Learning"]

    def test_excludes_headings_inside_translation_skip_environment(self, tmp_path):
        tex = tmp_path / "section.tex"
        tex.write_text(
            r"\section{Visible}"
            r"\begin{figure}\section{Hidden}\end{figure}"
        )

        assert self.extract(tmp_path, tex) == ["Visible"]


class TestTranslateHeadings:
    def test_paragraph_translation_is_plain_text(self):
        content = r"\paragraph{Contributions.}\label{para:contrib}" + "\nBody."

        result = p._translate_headings({"Contributions.": "贡献。"}, content)

        assert result.count(r"\paragraph") == 1
        assert r"\paragraph{贡献。}" not in result
        assert r"\label{para:contrib}" + "\n\n贡献。\n" in result

    def test_skips_paragraph_inside_incomplete_outer_command(self):
        content = (
            r"\paragraph{Shared}" + "\n\n" + r"\section{Outer \paragraph{Shared}"
        )

        result = p._translate_headings({"Shared": "共享"}, content)

        assert result.count("共享") == 1

    def test_inserts_after_complete_nested_label_argument(self):
        content = r"\section{Intro}\label{sec:{nested}}" + "\nBody."

        result = p._translate_headings({"Intro": "引言"}, content)

        assert r"\label{sec:{nested}}" + "\n\n引言\n" in result

    def test_chapter_translation_is_plain_text_after_label(self):
        content = (
            r"\chapter{Deep Reinforcement Learning}\label{chpt:rl}"
            "\nBody text."
        )

        result = p._translate_headings(
            {"Deep Reinforcement Learning": "深度强化学习"}, content,
        )

        assert result.count(r"\chapter") == 1
        assert r"\chapter{深度强化学习}" not in result
        assert r"\label{chpt:rl}" + "\n\n深度强化学习\n" in result

    def test_comment_between_heading_and_label_keeps_label_attached(self):
        content = (
            r"\subsection{Euler--Lagrange Equations}"
            "\n% source note\n"
            r"\label{subsec:oc-el}"
            "\nBody text."
        )

        result = p._translate_headings(
            {"Euler--Lagrange Equations": "欧拉-拉格朗日方程"}, content,
        )

        assert result.index(r"\label{subsec:oc-el}") < result.index(
            "欧拉-拉格朗日方程"
        )

    def test_does_not_insert_inside_translation_skip_environment(self):
        content = (
            r"\section{Visible}"
            r"\begin{figure}\section{Hidden}\end{figure}"
        )

        result = p._translate_headings(
            {"Visible": "可见", "Hidden": "隐藏"}, content,
        )

        assert "可见" in result
        assert "隐藏" not in result

    def test_crlf_heading_translation_preserves_newline_convention(self):
        content = r"\section{Intro}" + "\r\nBody."

        first = p._translate_headings({"Intro": "引言"}, content)
        second = p._translate_headings({"Intro": "引言"}, first)

        assert b"\n" not in first.encode().replace(b"\r\n", b"")
        assert second == first


def test_crlf_paragraph_translation_preserves_separators_and_is_idempotent(
    monkeypatch,
):
    content = (
        "First English paragraph with enough words for translation.\r\n\r\n"
        "Second English paragraph with enough words for translation."
    )
    monkeypatch.setattr(
        p, "_batch_translate",
        lambda client, glossary, numbered: {
            index: f"中文段落{index}。" for index in numbered
        },
    )

    first = p.translate_file_content(object(), "", {}, content)
    second = p.translate_file_content(object(), "", {}, first)

    assert b"\n" not in first.encode().replace(b"\r\n", b"")
    assert "\r\n\r\n" in first
    assert second == first


class TestTranslationProjectionRanges:
    def test_removes_boundaries_label_and_keeps_prose(self):
        content = (
            r"\begin{proof}\label{proof:one}" + "\n"
            "Proof prose has enough English words for translation.\n"
            r"\end{proof}"
        )
        source = p.SourceFile(Path("main.tex"), content)

        ranges = p._translation_projection_ranges(
            content, p.LatexDocument(source), (),
        )
        projected = p._slice_without_ranges(
            content, 0, len(content), ranges,
        )

        assert r"\begin{proof}" not in projected
        assert r"\end{proof}" not in projected
        assert r"\label{proof:one}" not in projected
        assert "Proof prose" in projected

    @pytest.mark.parametrize("newline", ["\n", "\r\n"])
    def test_removes_comment_separated_begin_and_end_boundaries(self, newline):
        content = (
            rf"\begin% begin note{newline}{{proof}}" + newline
            + "Proof prose has enough English words for translation."
            + newline + rf"\end% end note{newline}{{proof}}"
        )
        source = p.SourceFile(Path("main.tex"), content)

        ranges = p._translation_projection_ranges(
            content, p.LatexDocument(source), (),
        )
        projected = p._slice_without_ranges(
            content, 0, len(content), ranges,
        )

        assert r"\begin% begin note" not in projected
        assert r"\end% end note" not in projected
        assert "Proof prose" in projected

    def test_removes_environment_like_token_inside_comment(self):
        content = (
            "% ignored " + r"\begin% note" + "\n"
            "{proof}\n"
            "Visible prose has enough English words for translation."
        )
        source = p.SourceFile(Path("main.tex"), content)

        ranges = p._translation_projection_ranges(
            content, p.LatexDocument(source), (),
        )
        projected = p._slice_without_ranges(
            content, 0, len(content), ranges,
        )

        assert r"\begin% note" not in projected
        assert "Visible prose has enough English words" in projected


class TestMergeRanges:
    def test_merges_touching_nested_and_overlapping_ranges(self):
        assert p._merge_ranges(
            [(8, 12), (1, 4), (3, 6), (9, 10), (6, 8)],
        ) == ((1, 12),)

    def test_discards_empty_and_preserves_disjoint_ranges(self):
        assert p._merge_ranges(
            [(5, 5), (9, 7), (6, 8), (1, 3)],
        ) == ((1, 3), (6, 8))


@pytest.mark.parametrize("environment", ["proof", "example", "theorem"])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_multparagraph_translation_never_duplicates_wrappers(
    monkeypatch, environment, newline,
):
    content = (
        f"\\begin{{{environment}}}{newline}"
        "First paragraph has enough English prose to translate."
        f"{newline}{newline}"
        "Second paragraph also has enough English prose to translate."
        f"{newline}\\end{{{environment}}}"
    )
    payloads = []
    monkeypatch.setattr(
        p,
        "_batch_translate",
        lambda client, glossary, numbered: (
            payloads.extend(numbered.values())
            or {index: f"Chinese {index}." for index in numbered}
        ),
    )

    result = p.translate_file_content(object(), "", {}, content)

    assert all(
        r"\begin{" not in text and r"\end{" not in text
        for text in payloads
    )
    assert result.count(f"\\begin{{{environment}}}") == 1
    assert result.count(f"\\end{{{environment}}}") == 1
    if newline == "\r\n":
        assert "\n" not in result.replace("\r\n", "")


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_comment_separated_boundaries_never_enter_translation_payload(
    monkeypatch, newline,
):
    content = (
        rf"\begin% begin note{newline}{{proof}}" + newline
        + "First paragraph has enough English prose to translate."
        + newline + newline
        + "Second paragraph also has enough English prose to translate."
        + newline + rf"\end% end note{newline}{{proof}}"
    )
    payloads = []
    monkeypatch.setattr(
        p,
        "_batch_translate",
        lambda client, glossary, numbered: (
            payloads.extend(numbered.values())
            or {index: f"Chinese {index}." for index in numbered}
        ),
    )

    result = p.translate_file_content(object(), "", {}, content)

    assert payloads
    assert all(
        r"\begin% begin note" not in text
        and r"\end% end note" not in text
        for text in payloads
    )
    assert result.count(r"\begin% begin note") == 1
    assert result.count(r"\end% end note") == 1
    if newline == "\r\n":
        assert "\n" not in result.replace("\r\n", "")


class TestTranslateFileContent:
    def test_skipped_invalid_translation_preserves_source_without_marker(
        self, monkeypatch,
    ):
        content = "English prose has enough words to require translation."
        monkeypatch.setattr(p, "_batch_translate", lambda *args: {})

        result = p.translate_file_content(object(), "", {}, content)

        assert result == content
        assert "% paper2epub:translation-begin:" not in result

    def test_tex_comment_noise_is_removed_from_translation_payload(
        self, monkeypatch,
    ):
        content = "%\nFurthermore, we define the following useful quantity.\n%"
        payloads = []
        monkeypatch.setattr(
            p,
            "_batch_translate",
            lambda client, glossary, numbered: (
                payloads.extend(numbered.values())
                or {index: "此外，我们定义如下有用的量。" for index in numbered}
            ),
        )

        result = p.translate_file_content(object(), "", {}, content)

        assert payloads == ["Furthermore, we define the following useful quantity."]
        assert "此外，我们定义" in result
        assert result.count("%") >= 3

    def test_incomplete_display_math_is_opaque_to_translation(self, monkeypatch):
        prose = "Introductory prose has enough English words for translation."
        content = prose + "\n" + r"\[unfinished x + y"
        payloads = []
        monkeypatch.setattr(
            p,
            "_batch_translate",
            lambda client, glossary, numbered: (
                payloads.extend(numbered.values())
                or {index: "中文引言。" for index in numbered}
            ),
        )

        result = p.translate_file_content(object(), "", {}, content)

        assert payloads == [prose]
        assert result.endswith(r"\[unfinished x + y")
        assert result.index("中文引言。") < result.index(r"\[")

    def test_display_math_is_shared_and_translation_stays_outside_math(
        self, monkeypatch,
    ):
        prose = (
            "For any $p \\in P$ and $t \\in [0,T)$, we consider the evolution "
            "given by"
        )
        display = (
            "\\[\n"
            "\\begin{cases}\n"
            "  \\partial_s \\rho_s + \\nabla \\cdot(w \\rho_s)=0, \\\\\n"
            "  \\rho_t=p.\n"
            "\\end{cases}\n"
            "\\]"
        )
        content = prose + "\n" + display + "\nFollowing explanatory prose."
        payloads = []

        def translate(client, glossary, numbered):
            payloads.extend(numbered.values())
            return {
                index: "对任意参数，我们考虑下列演化。"
                for index in numbered
            }

        monkeypatch.setattr(p, "_batch_translate", translate)

        result = p.translate_file_content(object(), "", {}, content)

        assert payloads == [prose, "Following explanatory prose."]
        assert all(
            r"\[" not in payload and r"\]" not in payload
            for payload in payloads
        )
        assert result.count(r"\[") == 1
        assert result.count(r"\]") == 1
        assert result.count(r"\begin{cases}") == 1
        assert result.index("对任意参数") < result.index(r"\[")
        begin = result.index("% paper2epub:translation-begin:")
        end = result.index("% paper2epub:translation-end:")
        assert end < result.index(r"\[")
        assert r"\[" not in result[begin:end]

    @pytest.mark.parametrize("newline", ["\n", "\r\n"])
    def test_list_intro_and_items_translate_at_their_own_boundaries(
        self, monkeypatch, newline,
    ):
        intro = "Introductory prose has enough English words for translation."
        first = "First list item has enough English words for translation."
        second = "Second list item has enough English words for translation."
        content = newline.join((
            intro,
            "% source note",
            r"\begin{enumerate}",
            "% source note",
            rf"\item {first}",
            "% source note",
            rf"\item {second}",
            r"\end{enumerate}",
        ))
        calls = []

        def translate(_client, _glossary, numbered, _limiter=None):
            calls.append(dict(numbered))
            translated = {}
            for index, paragraph in numbered.items():
                if intro in paragraph:
                    translated[index] = "中文引言。"
                elif first in paragraph:
                    translated[index] = "中文第一项。"
                elif second in paragraph:
                    translated[index] = "中文第二项。"
            return translated

        monkeypatch.setattr(p, "_batch_translate", translate)

        result = p.translate_file_content(object(), "", {}, content)
        repeated = p.translate_file_content(object(), "", {}, result)

        payloads = tuple(calls[0].values())
        assert len(payloads) == 3
        assert all(r"\item" not in payload for payload in payloads)
        assert all(r"\begin" not in payload for payload in payloads)
        assert all(r"\end" not in payload for payload in payloads)
        assert result.count(r"\item") == 2
        assert result.index("中文引言。") < result.index(r"\begin{enumerate}")
        assert (
            result.index(first)
            < result.index("中文第一项。")
            < result.index(r"\item " + second)
        )
        assert (
            result.index(second)
            < result.index("中文第二项。")
            < result.index(r"\end{enumerate}")
        )
        assert repeated == result
        assert len(calls) == 1

    def test_paragraph_heading_is_not_duplicated(self, monkeypatch):
        content = r"\paragraph{Contributions.}" + "\nBody text with enough prose."
        monkeypatch.setattr(
            p, "_batch_translate", lambda client, glossary, numbered: {0: "正文。"}
        )

        result = p.translate_file_content(
            object(), "", {"Contributions.": "贡献。"}, content
        )

        assert result.count(r"\paragraph") == 1
        assert r"\paragraph{贡献。}" not in result
        assert "贡献。" in result

    @pytest.mark.parametrize("newline", ["\n", "\r\n"])
    def test_multiline_translation_range_is_idempotent_without_api_recall(
        self, monkeypatch, newline
    ):
        content = (
            "English paragraph with enough words for translation."
            + (newline if newline == "\r\n" else "")
        )
        calls = []

        def translate(client, glossary, numbered):
            calls.append(dict(numbered))
            return {0: f"Chinese first part.{newline}{newline}Chinese second part."}

        monkeypatch.setattr(p, "_batch_translate", translate)
        first = p.translate_file_content(object(), "", {}, content)
        second = p.translate_file_content(object(), "", {}, first)

        assert len(calls) == 1
        assert second == first
        assert "% paper2epub:translation-begin:" in first
        assert "% paper2epub:translation-end:" in first
        if newline == "\r\n":
            assert "\n" not in first.replace("\r\n", "")

    def test_plain_legacy_marker_does_not_suppress_translation(self, monkeypatch):
        content = (
            "First English paragraph with enough words for translation.\n\n"
            "% paper2epub:translation\n\n"
            "Second English paragraph with enough words for translation."
        )
        seen = []
        monkeypatch.setattr(
            p, "_batch_translate",
            lambda client, glossary, numbered: (
                seen.append(dict(numbered))
                or {index: f"Chinese {index}." for index in numbered}
            ),
        )

        result = p.translate_file_content(object(), "", {}, content)

        assert len(seen[0]) == 2
        assert "% paper2epub:translation" in result

    @pytest.mark.parametrize("closed", [True, False])
    def test_unbound_or_unclosed_marker_does_not_swallow_user_text(
        self, monkeypatch, closed
    ):
        paragraph = "English paragraph with enough words for translation."
        wrong_digest = "0" * 64
        marker_text = (
            f"% paper2epub:translation-begin:{wrong_digest}\n"
            "User-authored text inside marker-like lines."
        )
        if closed:
            marker_text += f"\n% paper2epub:translation-end:{wrong_digest}"
        content = paragraph + "\n\n" + marker_text
        seen = []
        monkeypatch.setattr(
            p, "_batch_translate",
            lambda client, glossary, numbered: (
                seen.append(dict(numbered))
                or {index: f"Chinese {index}." for index in numbered}
            ),
        )

        result = p.translate_file_content(object(), "", {}, content)

        assert paragraph in seen[0].values()
        assert marker_text in result

    @pytest.mark.parametrize("newline", ["\n", "\r\n"])
    def test_heading_and_paragraph_share_stable_translation_identity(
        self, monkeypatch, newline
    ):
        content = (
            r"\paragraph{Contributions.}" + newline
            + "Body prose has enough English words for paragraph translation."
        )
        calls = []
        monkeypatch.setattr(
            p, "_batch_translate",
            lambda client, glossary, numbered: (
                calls.append(dict(numbered)) or {0: "Chinese body."}
            ),
        )

        first = p.translate_file_content(
            object(), "", {"Contributions.": "贡献。"}, content
        )
        second = p.translate_file_content(
            object(), "", {"Contributions.": "贡献。"}, first
        )

        assert len(calls) == 1
        assert second == first
        assert first.count("% paper2epub:translation-begin:") == 1
        assert first.count("% paper2epub:heading-translation") == 1
        if newline == "\r\n":
            assert "\n" not in first.replace("\r\n", "")


class TestIsProse:
    def test_normal_text(self):
        assert p._is_prose("This is a normal paragraph with enough text to pass.")

    def test_empty(self):
        assert not p._is_prose("")
        assert not p._is_prose("   ")

    def test_pure_comments(self):
        assert not p._is_prose("% this is a comment\n% another comment")

    def test_pure_commands(self):
        assert not p._is_prose(r"\begin{figure}")
        assert not p._is_prose(r"\label{sec:intro}")

    def test_too_short(self):
        assert not p._is_prose("short")

    def test_math_heavy_text(self):
        assert p._is_prose(
            r"We define the function $f(x) = x^2$ and the variable $y$ for this problem."
        )

    def test_nested_structural_command_is_not_literal_prose(self):
        assert not p._is_prose(
            r"\label{thisisaverylong{nested}structuralidentifier}"
        )

    def test_counts_literal_text_inside_formatting_command(self):
        assert p._is_prose(
            r"\textbf{This is a complete prose sentence inside formatting.}"
        )


class TestFindSkipRanges:
    def test_single_env(self):
        content = r"text \begin{figure}fig\end{figure} more"
        ranges = p._find_skip_ranges(content)
        assert len(ranges) == 1
        assert content[ranges[0][0]:ranges[0][1]] == r"\begin{figure}fig\end{figure}"

    def test_nested(self):
        content = r"\begin{table}\begin{tabular}x\end{tabular}\end{table}"
        ranges = p._find_skip_ranges(content)
        assert ranges == [(0, len(content))]

    def test_no_skip_envs(self):
        content = r"\begin{document}text\end{document}"
        assert p._find_skip_ranges(content) == []

    def test_nested_same_name_uses_outermost_trusted_range(self, tmp_path):
        content = (
            "Prose before.\n\n"
            r"\begin{figure}outer \begin{figure}inner\end{figure} tail\end{figure}"
            "\n\nProse after."
        )
        source = p.SourceFile(tmp_path / "main.tex", content)

        ranges = p.translation_skip_ranges(p.LatexDocument(source))

        assert len(ranges) == 1
        assert source.content[ranges[0][0]:ranges[0][1]].startswith(
            r"\begin{figure}"
        )


class TestChunkInSkipRange:
    def test_fully_inside(self):
        assert p._chunk_in_skip_range(5, 10, [(0, 20)])

    def test_outside(self):
        assert not p._chunk_in_skip_range(25, 30, [(0, 20)])

    def test_overlapping(self):
        assert p._chunk_in_skip_range(15, 25, [(0, 20)])


class TestStripEnvWrappers:
    def test_strips_begin_end(self):
        text = r"\begin{quote}" + "\nsome text\n" + r"\end{quote}"
        result = p._strip_env_wrappers(text)
        assert "some text" == result

    def test_preserves_content(self):
        assert p._strip_env_wrappers("no wrappers") == "no wrappers"


class TestStripHeadingLines:
    def test_strips_section(self):
        text = r"\section{Introduction}" + "\nSome paragraph text."
        result = p._strip_heading_lines(text)
        assert "Introduction" not in result
        assert "Some paragraph text." in result

    def test_strips_paragraph(self):
        text = r"\paragraph{Contributions.}" + "\nBody text."

        result = p._strip_heading_lines(text)

        assert "Contributions" not in result
        assert result == "Body text."

    def test_preserves_prose_after_paragraph_on_same_line(self):
        text = r"\paragraph{Contributions.} Body text."

        result = p._strip_heading_lines(text)

        assert result == "Body text."

    def test_strips_multiline_paragraph_as_one_command(self):
        text = "\\paragraph{\nContributions.\n}\nBody text."

        result = p._strip_heading_lines(text)

        assert result == "Body text."

    def test_preserves_non_heading(self):
        text = "Just a paragraph."
        assert p._strip_heading_lines(text) == text

    def test_strips_adjacent_complete_label_with_nested_argument(self):
        text = r"\section{Intro}\label{sec:{nested}} Body text."

        assert p._strip_heading_lines(text) == "Body text."


class TestUniqueLexicalDocumentBodyRange:
    @pytest.mark.parametrize(
        ("content", "expected"),
        [
            (r"PRE\begin{document}BODY\end{document}POST", (19, 23)),
            (r"\begin{document}BODY", None),
            (r"BODY\end{document}", None),
            (r"\end{document}BODY\begin{document}", None),
            (
                r"\begin{document}\begin{document}BODY\end{document}",
                None,
            ),
            (
                r"\begin{document}BODY\end{document}\end{document}",
                None,
            ),
            (
                r"\begin{document}ONE\end{document}"
                r"\begin{document}TWO\end{document}",
                None,
            ),
        ],
    )
    def test_requires_one_ordered_pair(self, content, expected):
        assert p._unique_lexical_document_body_range(content) == expected


class TestTranslationBarrier:
    @staticmethod
    def snapshot(tmp_path, main_content, child_content=None):
        main = tmp_path / "main.tex"
        main.write_text(main_content)
        if child_content is not None:
            child = tmp_path / "child.tex"
            child.write_text(child_content)
        snapshot = p.build_discovery_facts(
            p.DocumentSnapshot.from_directory(tmp_path, main)
        )
        return main, snapshot

    def test_failure_returns_no_partial_snapshot_or_disk_writes(self, tmp_path):
        main, snapshot = self.snapshot(
            tmp_path,
            r"\begin{document}Main prose.\input{child}\end{document}",
            "Child prose with enough English words for translation.",
        )
        child = tmp_path / "child.tex"

        def translate(source, discovery):
            if source.path == child:
                raise RuntimeError("translation failed")
            return source.content + " translated"

        with pytest.raises(RuntimeError, match="translation failed"):
            p.TranslationBarrier(translate, max_workers=1).run(snapshot)

        assert snapshot.sources[main].content.startswith(r"\begin{document}")
        assert snapshot.sources[child].content.startswith("Child prose")
        assert main.read_text().startswith(r"\begin{document}")
        assert child.read_text().startswith("Child prose")

    def test_success_splices_main_body_and_invalidates_syntax(self, tmp_path):
        main, snapshot = self.snapshot(
            tmp_path,
            r"\documentclass{article}\begin{document}Main prose.\input{child}\end{document}",
            "Child prose.",
        )
        seen = {}

        def translate(source, discovery):
            seen[source.path] = source.content
            return source.content + " translated"

        result = p.TranslationBarrier(translate, max_workers=1).run(snapshot)
        child = tmp_path / "child.tex"

        assert seen[main] == r"Main prose.\input{child}"
        assert seen[child] == "Child prose."
        assert result.snapshot.sources[main].content == (
            r"\documentclass{article}\begin{document}"
            r"Main prose.\input{child} translated\end{document}"
        )
        assert result.snapshot.sources[child].content == "Child prose. translated"
        assert result.snapshot.revision == snapshot.revision + 1
        assert result.snapshot.documents == {}
        assert p.Fact.SYNTAX not in result.snapshot.current_facts
        assert p.Fact.DISCOVERY in result.snapshot.current_facts
        assert main.read_text() == (
            r"\documentclass{article}\begin{document}"
            r"Main prose.\input{child}\end{document}"
        )

    def test_opaque_main_document_uses_unique_lexical_boundaries(self, tmp_path):
        content = (
            "\\documentclass[12pt\n"
            r"\newcommand\blfootnote[1]{#1}"
            r"\begin{document}Main prose.\end{document}"
        )
        main, snapshot = self.snapshot(tmp_path, content)
        document = snapshot.documents[main]
        document_ref = document.environments("document")[0]
        seen = []

        assert document_ref.opaque

        result = p.TranslationBarrier(
            lambda source, discovery: seen.append(source.content) or (
                source.content + " translated"
            ),
            max_workers=1,
        ).run(snapshot)

        assert seen == ["Main prose."]
        assert result.snapshot.sources[main].content == content.replace(
            "Main prose.", "Main prose. translated",
        )
        assert [diagnostic.code for diagnostic in result.diagnostics] == [
            "lexical-document-boundary-fallback",
        ]

    def test_foreign_main_document_does_not_use_lexical_fallback(self, tmp_path):
        content = r"\begin{document}Main prose.\end{document}"
        main, snapshot = self.snapshot(tmp_path, content)
        document = snapshot.documents[main]
        document_ref = document.environments("document")[0]
        document._refs = tuple(
            p.replace(ref, file=tmp_path / "other.tex")
            if ref is document_ref else ref
            for ref in document._refs
        )
        calls = []

        result = p.TranslationBarrier(
            lambda source, discovery: calls.append(source.path) or "changed",
            max_workers=1,
        ).run(snapshot)

        assert calls == []
        assert result.snapshot is snapshot
        assert [diagnostic.code for diagnostic in result.diagnostics] == [
            "foreign-reference",
        ]

    def test_incomplete_main_document_is_preserved_with_diagnostic(self, tmp_path):
        content = r"\begin{document}Unfinished main prose."
        main, snapshot = self.snapshot(tmp_path, content)
        calls = []

        result = p.TranslationBarrier(
            lambda source, discovery: calls.append(source.path) or "changed",
        ).run(snapshot)

        assert calls == []
        assert result.snapshot is snapshot
        assert main.read_text() == content
        assert any(d.code == "opaque-structure" for d in result.diagnostics)

    def test_missing_main_document_preserves_main_but_translates_child(
        self, tmp_path
    ):
        main_content = r"\documentclass{article}\input{child}"
        child_content = "Child prose with enough English words for translation."
        main, snapshot = self.snapshot(tmp_path, main_content, child_content)
        child = tmp_path / "child.tex"
        calls = []

        def translate(source, discovery):
            calls.append(source.path)
            return source.content + " translated"

        result = p.TranslationBarrier(translate, max_workers=1).run(snapshot)

        assert calls == [child]
        assert result.snapshot.sources[main].content == main_content
        assert result.snapshot.sources[child].content == child_content + " translated"
        assert [d.code for d in result.diagnostics] == ["missing-document"]
        assert result.diagnostics[0].file == main

    def test_multifile_translation_preserves_crlf_and_is_idempotent(
        self, tmp_path, monkeypatch
    ):
        main_content = (
            "\\documentclass{article}\r\n"
            "\\begin{document}\r\n"
            "Main prose has enough English words for translation.\r\n\r\n"
            "\\input{child}\r\n"
            "\\end{document}\r\n"
        )
        child_content = (
            "Child prose also has enough English words for translation.\r\n"
        )
        main = tmp_path / "main.tex"
        child = tmp_path / "child.tex"
        main.write_bytes(main_content.encode())
        child.write_bytes(child_content.encode())
        snapshot = p.build_discovery_facts(
            p.DocumentSnapshot.from_directory(tmp_path, main)
        )
        monkeypatch.setattr(
            p,
            "_batch_translate",
            lambda client, glossary, numbered: {
                i: f"Chinese translation {i}." for i in numbered
            },
        )

        def translate(source, discovery):
            return p.translate_file_content(object(), "", {}, source.content)

        first = p.TranslationBarrier(translate, max_workers=1).run(snapshot)
        rebuilt = p.rebuild_syntax(first.snapshot)
        second = p.TranslationBarrier(translate, max_workers=1).run(rebuilt)

        for path in (main, child):
            translated = first.snapshot.sources[path].content
            assert "\n" not in translated.replace("\r\n", "")
            assert second.snapshot.sources[path].content == translated

    @pytest.mark.parametrize("newline", ["\n", "\r\n"])
    def test_heading_paragraph_barrier_second_run_needs_no_api_call(
        self, tmp_path, monkeypatch, newline
    ):
        content = (
            r"\begin{document}"
            + r"\paragraph{Contributions.}" + newline
            + "Body prose has enough English words for paragraph translation."
            + r"\end{document}"
        )
        main = tmp_path / "main.tex"
        main.write_bytes(content.encode())
        snapshot = p.build_discovery_facts(
            p.DocumentSnapshot.from_directory(tmp_path, main)
        )
        calls = []
        monkeypatch.setattr(
            p, "_batch_translate",
            lambda client, glossary, numbered: (
                calls.append(dict(numbered)) or {0: "Chinese body."}
            ),
        )

        def translate(source, discovery):
            return p.translate_file_content(
                object(), "", {"Contributions.": "贡献。"}, source.content
            )

        first = p.TranslationBarrier(translate, max_workers=1).run(snapshot)
        second = p.TranslationBarrier(translate, max_workers=1).run(
            p.rebuild_syntax(first.snapshot)
        )

        assert len(calls) == 1
        assert second.snapshot.sources[main].content == first.snapshot.sources[main].content
        assert first.snapshot.sources[main].content.count(
            "% paper2epub:translation-begin:"
        ) == 1

    def test_rebuilds_syntax_before_post_translation_paragraph_plan(self, tmp_path):
        main, snapshot = self.snapshot(
            tmp_path, r"\paragraph{Title} Body text.",
        )
        barrier = p.TranslationBarrier(
            lambda source, discovery: source.content + " translated",
        ).run(snapshot)

        rebuilt = p.rebuild_syntax(barrier.snapshot)
        edits = p.plan_unnumber_paragraphs(
            rebuilt.sources[main], rebuilt.documents[main],
        )

        assert p.Fact.SYNTAX in rebuilt.current_facts
        assert p.EditPlanner.apply(rebuilt.sources[main], edits).startswith(
            r"\paragraph*{Title}"
        )

    def test_rejects_discovery_object_without_current_fact(self, tmp_path):
        _, snapshot = self.snapshot(tmp_path, "Current prose.")
        stale = p.replace(
            snapshot,
            current_facts=snapshot.current_facts - {p.Fact.DISCOVERY},
        )
        calls = []

        with pytest.raises(p.PipelineContractError, match="discovery"):
            p.TranslationBarrier(
                lambda source, discovery: calls.append(source.path) or source.content,
            ).run(stale)

        assert calls == []


# ── Algorithm preprocessing ────────────────────────────────────────────────


class TestFormatCommand:
    def test_require(self):
        assert p.format_command("Require", None, "input x") == r"\textbf{Require:} input x"

    def test_return(self):
        assert p.format_command("Return", None, "result") == r"\textbf{return} result"

    def test_for(self):
        result = p.format_command("For", "i=1 to n", "")
        assert r"\textbf{for}" in result
        assert r"\textbf{do}" in result

    def test_if(self):
        result = p.format_command("If", "x > 0", "")
        assert r"\textbf{if}" in result
        assert r"\textbf{then}" in result

    def test_endif(self):
        assert "end if" in p.format_command("EndIf", None, "")

    def test_while(self):
        result = p.format_command("While", "true", "")
        assert r"\textbf{while}" in result


class TestParseAlgorithmic:
    def test_simple_sequence(self):
        content = r"\Require x\State y = x + 1\Return y"
        lines = p.parse_algorithmic(content)
        assert len(lines) == 3
        assert lines[0][1] == 0  # indent

    def test_indentation(self):
        content = r"\If{x > 0}\State positive\EndIf"
        lines = p.parse_algorithmic(content)
        # If at indent 0, State at indent 1, EndIf at indent 0
        assert lines[0][1] == 0  # If
        assert lines[1][1] == 1  # State
        assert lines[2][1] == 0  # EndIf

    def test_empty(self):
        assert p.parse_algorithmic("no algorithm") == []


class TestReplaceCallInText:
    def test_outside_math(self):
        result = p.replace_call_in_text(r"\Call{Foo}{x, y}")
        assert r"\operatorname{Foo}(x, y)" == result

    def test_inside_math(self):
        result = p.replace_call_in_text(r"$\Call{Bar}{z}$")
        assert r"$\operatorname{Bar}(z)$" == result

    def test_no_call(self):
        text = "no calls here"
        assert p.replace_call_in_text(text) == text

    @pytest.mark.parametrize("text", [
        r"\\Call{Escaped}{x}",
        "% \\Call{Commented}{x}\ntext",
        r"\Caller{Partial}{x}",
        r"\Call{MissingSecond}",
    ])
    def test_non_command_or_malformed_call_is_preserved(self, text):
        assert p.replace_call_in_text(text) == text

    def test_nested_call_arguments_are_parser_owned(self):
        text = r"\Call{Foo {Bar}}{{x_{i}}, \textbf{y}}"
        assert p.replace_call_in_text(text) == (
            r"\operatorname{Foo {Bar}}({x_{i}}, \textbf{y})"
        )

    def test_malformed_call_planner_emits_diagnostic(self, tmp_path):
        content = r"before \Call{MissingSecond} after"
        source = p.SourceFile(tmp_path / "main.tex", content)

        outcome = p.plan_replace_calls(source, p.LatexDocument(source))

        assert outcome.edits == ()
        assert p.EditPlanner.apply(source, outcome.edits) == content
        assert any(d.code == "opaque-structure" for d in outcome.diagnostics)

    def test_call_rewrite_is_idempotent(self, tmp_path):
        source = p.SourceFile(tmp_path / "main.tex", r"\Call{Foo}{x}")
        first = p.EditPlanner.apply(
            source, p.plan_replace_calls(source, p.LatexDocument(source)).edits,
        )
        rewritten = p.SourceFile(source.path, first)

        second = p.plan_replace_calls(rewritten, p.LatexDocument(rewritten))

        assert second.edits == ()


class TestNormalizeTextsc:
    def test_replaces_textsc_command_token(self, tmp_path):
        content = r"\textsc{Hello}"
        source = p.SourceFile(tmp_path / "main.tex", content)
        outcome = p.plan_normalize_textsc(source, p.LatexDocument(source))
        assert p.EditPlanner.apply(source, outcome.edits) == r"\text{Hello}"

    def test_preserves_text(self, tmp_path):
        content = r"\text{ok}"
        source = p.SourceFile(tmp_path / "main.tex", content)
        outcome = p.plan_normalize_textsc(source, p.LatexDocument(source))
        assert p.EditPlanner.apply(source, outcome.edits) == content

    def test_in_math(self, tmp_path):
        src = r"$\textsc{Foo}(x)$"
        source = p.SourceFile(tmp_path / "main.tex", src)
        outcome = p.plan_normalize_textsc(source, p.LatexDocument(source))
        assert p.EditPlanner.apply(source, outcome.edits) == r"$\text{Foo}(x)$"

    def test_no_match(self, tmp_path):
        src = "no textsc here"
        source = p.SourceFile(tmp_path / "main.tex", src)
        outcome = p.plan_normalize_textsc(source, p.LatexDocument(source))
        assert p.EditPlanner.apply(source, outcome.edits) == src

    def test_incomplete_textsc_is_preserved(self, tmp_path):
        src = r"before \textsc{unfinished"
        source = p.SourceFile(tmp_path / "main.tex", src)
        outcome = p.plan_normalize_textsc(source, p.LatexDocument(source))
        assert p.EditPlanner.apply(source, outcome.edits) == src
        assert any(d.code == "opaque-structure" for d in outcome.diagnostics)

    def test_is_idempotent(self, tmp_path):
        source = p.SourceFile(tmp_path / "main.tex", r"\textsc{Word}")
        first = p.EditPlanner.apply(
            source,
            p.plan_normalize_textsc(source, p.LatexDocument(source)).edits,
        )
        rewritten = p.SourceFile(source.path, first)
        second = p.plan_normalize_textsc(rewritten, p.LatexDocument(rewritten))

        assert p.EditPlanner.apply(rewritten, second.edits) == first


class TestPlanAlgorithms:
    @staticmethod
    def apply_single(tmp_path, content):
        main = tmp_path / "main.tex"
        main.write_text(content)
        snapshot = p.DocumentSnapshot.from_directory(tmp_path, main)
        outcome = p.plan_algorithms(snapshot)
        return (
            p.EditPlanner.apply(snapshot.sources[main], outcome.edits),
            outcome,
        )

    def test_preserves_nested_command_arguments(self, tmp_path):
        content = (
            r"\begin{algorithm}"
            r"\caption{Nested \textbf{caption}}"
            r"\label{alg:nested}"
            r"\begin{algorithmic}"
            r"\If{\Call{Ready}{\textbf{x_{i}}}}"
            r"\State \Call{Update}{{x_{i}}, {y_j}}"
            r"\EndIf"
            r"\end{algorithmic}"
            r"\end{algorithm}"
        )

        result, _ = self.apply_single(tmp_path, content)

        assert r"\textbf{Algorithm 1} Nested \textbf{caption}" in result
        assert r"\operatorname{Ready}(\textbf{x_{i}})" in result
        assert r"\operatorname{Update}({x_{i}}, {y_j})" in result
        assert r"\hypertarget{alg:nested}" in result

    def test_numbers_in_snapshot_document_order_without_global_state(self, tmp_path):
        main = tmp_path / "main.tex"
        part = tmp_path / "part.tex"
        main.write_text(
            r"\documentclass{article}\input{part}"
            r"\begin{algorithm}\caption{First}"
            r"\begin{algorithmic}\State a\end{algorithmic}"
            r"\end{algorithm}"
        )
        part.write_text(
            r"\begin{algorithm}\caption{Second}"
            r"\begin{algorithmic}\State b\end{algorithmic}"
            r"\end{algorithm}"
        )
        snapshot = p.build_discovery_facts(
            p.DocumentSnapshot.from_directory(tmp_path, main)
        )

        first = p.plan_algorithms(snapshot)
        second = p.plan_algorithms(snapshot)
        main_result = p.EditPlanner.apply(
            snapshot.sources[main], [e for e in first.edits if e.file == main],
        )
        part_result = p.EditPlanner.apply(
            snapshot.sources[part], [e for e in first.edits if e.file == part],
        )

        assert r"\textbf{Algorithm 1} First" in main_result
        assert r"\textbf{Algorithm 2} Second" in part_result
        assert first == second

    def test_preserves_incomplete_algorithm_with_diagnostic(self, tmp_path):
        content = (
            r"before\begin{algorithm}\begin{algorithmic}"
            r"\If{unfinished\end{algorithmic}\end{algorithm}after"
        )

        result, outcome = self.apply_single(tmp_path, content)

        assert result == content
        assert any(d.code == "opaque-structure" for d in outcome.diagnostics)

    def test_is_idempotent(self, tmp_path):
        content = (
            r"\begin{algorithm}\caption{Once}"
            r"\begin{algorithmic}\State value\end{algorithmic}"
            r"\end{algorithm}"
        )
        first, _ = self.apply_single(tmp_path, content)
        rewritten = tmp_path / "rewritten"
        rewritten.mkdir()
        main = rewritten / "main.tex"
        main.write_text(first)
        snapshot = p.DocumentSnapshot.from_directory(rewritten, main)

        outcome = p.plan_algorithms(snapshot)

        assert outcome.edits == ()
        assert p.EditPlanner.apply(snapshot.sources[main], outcome.edits) == first

    def test_crlf_renderer_preserves_newlines_and_is_idempotent(self, tmp_path):
        content = (
            r"\begin{algorithm}" + "\r\n"
            r"\caption{Test}" + "\r\n"
            r"\begin{algorithmic}\State value\end{algorithmic}" + "\r\n"
            r"\end{algorithm}"
        )
        first, _ = self.apply_single(tmp_path, content)
        rewritten = tmp_path / "crlf"
        rewritten.mkdir()
        main = rewritten / "main.tex"
        main.write_bytes(first.encode())
        snapshot = p.DocumentSnapshot.from_directory(rewritten, main)

        second = p.plan_algorithms(snapshot)

        assert b"\n" not in first.encode().replace(b"\r\n", b"")
        assert second.edits == ()

    @pytest.mark.parametrize(
        ("child_name", "mutation"),
        [
            ("caption", "opaque-ref"),
            ("label", "foreign-ref"),
            ("State", "opaque-ref"),
            ("If", "opaque-argument"),
        ],
        ids=("caption", "label", "command", "required-argument"),
    )
    def test_preserves_algorithm_when_child_is_untrusted(
        self, tmp_path, child_name, mutation,
    ):
        content = (
            r"\begin{algorithm}\caption{Trusted}\label{alg:trusted}"
            r"\begin{algorithmic}\If{condition}\State value\EndIf"
            r"\end{algorithmic}\end{algorithm}"
        )
        main = tmp_path / "main.tex"
        main.write_text(content)
        snapshot = p.DocumentSnapshot.from_directory(tmp_path, main)
        document = snapshot.documents[main]
        changed = False
        refs = []
        for ref in document._refs:
            if not changed and ref.name == child_name:
                if mutation == "opaque-argument":
                    argument = document.argument(ref, 0)
                    assert argument is not None
                    arguments = list(ref.arguments)
                    arguments[0] = p.replace(
                        argument, complete=False, opaque=True,
                    )
                    ref = p.replace(ref, arguments=tuple(arguments))
                elif mutation == "foreign-ref":
                    ref = p.replace(ref, file=tmp_path / "foreign.tex")
                else:
                    ref = p.replace(ref, complete=False, opaque=True)
                changed = True
            refs.append(ref)
        assert changed
        document._refs = tuple(refs)

        outcome = p.plan_algorithms(snapshot)

        assert p.EditPlanner.apply(snapshot.sources[main], outcome.edits) == content
        expected_code = (
            "foreign-reference" if mutation == "foreign-ref"
            else "opaque-structure"
        )
        assert any(d.code == expected_code for d in outcome.diagnostics)

    @pytest.mark.parametrize(
        ("call_index", "mutation"),
        [
            (0, "opaque-ref"),
            (0, "first-argument"),
            (1, "foreign-ref"),
            (1, "second-argument"),
        ],
        ids=("caption-ref", "caption-arg", "body-ref", "body-second-arg"),
    )
    def test_preserves_algorithm_when_call_is_untrusted(
        self, tmp_path, call_index, mutation,
    ):
        content = (
            r"\begin{algorithm}\caption{Run \Call{Name}{x}}"
            r"\begin{algorithmic}\State \Call{Body}{y}\end{algorithmic}"
            r"\end{algorithm}"
        )
        main = tmp_path / "main.tex"
        main.write_text(content)
        snapshot = p.DocumentSnapshot.from_directory(tmp_path, main)
        document = snapshot.documents[main]
        calls = document.commands("Call")
        target = calls[call_index]
        if mutation == "opaque-ref":
            replacement = p.replace(target, complete=False, opaque=True)
        elif mutation == "foreign-ref":
            replacement = p.replace(target, file=tmp_path / "foreign.tex")
        else:
            argument_index = 0 if mutation == "first-argument" else 1
            argument = document.argument(target, argument_index)
            assert argument is not None
            arguments = list(target.arguments)
            arguments[argument_index] = p.replace(
                argument, complete=False, opaque=True,
            )
            replacement = p.replace(target, arguments=tuple(arguments))
        document._refs = tuple(
            replacement if ref is target else ref for ref in document._refs
        )

        outcome = p.plan_algorithms(snapshot)

        assert p.EditPlanner.apply(snapshot.sources[main], outcome.edits) == content
        expected_code = (
            "foreign-reference" if mutation == "foreign-ref"
            else "opaque-structure"
        )
        assert any(d.code == expected_code for d in outcome.diagnostics)

    def test_preserves_algorithm_with_incomplete_call(self, tmp_path):
        content = (
            r"\begin{algorithm}\caption{Bad \Call{Name}}"
            r"\begin{algorithmic}\State value\end{algorithmic}"
            r"\end{algorithm}"
        )

        result, outcome = self.apply_single(tmp_path, content)

        assert result == content
        assert any(d.code == "opaque-structure" for d in outcome.diagnostics)


# ── _transform_tex_files helper ─────────────────────────────────────────────


class TestFindMainTex:
    def test_finds_documentclass(self, tmp_path):
        (tmp_path / "other.tex").write_text("just macros")
        main = tmp_path / "main.tex"
        main.write_text(r"\documentclass{article}\begin{document}\end{document}")
        assert p.find_main_tex(tmp_path) == main

    def test_finds_begin_document(self, tmp_path):
        tex = tmp_path / "paper.tex"
        tex.write_text(r"\begin{document} hello \end{document}")
        assert p.find_main_tex(tmp_path) == tex

    def test_fallback_to_first(self, tmp_path):
        tex = tmp_path / "random.tex"
        tex.write_text("no documentclass here")
        assert p.find_main_tex(tmp_path) == tex

    def test_ignores_commented_document_markers(self, tmp_path):
        commented = tmp_path / "commented.tex"
        main = tmp_path / "main.tex"
        commented.write_text("% \\documentclass{article}\n")
        main.write_text(r"\begin{document}body\end{document}")

        assert p.find_main_tex(tmp_path) == main

    def test_ignores_incomplete_documentclass(self, tmp_path):
        incomplete = tmp_path / "incomplete.tex"
        main = tmp_path / "main.tex"
        incomplete.write_text(r"\documentclass{article")
        main.write_text(r"\documentclass{book}")

        assert p.find_main_tex(tmp_path) == main


# ── simplify_documentclass ──────────────────────────────────────────────────


class TestPlanSimplifyDocumentclass:
    def test_plans_documentclass_and_maketitle_edits(self, tmp_path):
        content = (
            "\\documentclass[12pt]{IEEEtran}\n"
            "\\begin{document}\n"
            "  \\maketitle  \n"
            "Body\n"
            "\\end{document}"
        )
        source = p.SourceFile(tmp_path / "main.tex", content)
        document = p.LatexDocument(source)

        edits = p.plan_simplify_documentclass(source, document)
        result = p.EditPlanner.apply(source, edits)

        assert result == (
            "\\documentclass{article}\n"
            "\\begin{document}\n"
            "Body\n"
            "\\end{document}"
        )
        assert all(edit.safety is p.Safety.LOSSY for edit in edits)

    def test_plan_is_idempotent(self, tmp_path):
        source = p.SourceFile(
            tmp_path / "main.tex",
            r"\documentclass{article}\begin{document}Body\end{document}",
        )
        first = p.EditPlanner.apply(
            source,
            p.plan_simplify_documentclass(source, p.LatexDocument(source)),
        )
        second_source = p.SourceFile(source.path, first)

        assert p.plan_simplify_documentclass(
            second_source,
            p.LatexDocument(second_source),
        ) == []

    @pytest.mark.parametrize(
        "content",
        [
            "\\documentclass{IEEEtran\n\\begin{document}Body\\end{document}",
            "\\documentclass[12pt\n\\begin{document}Body\\end{document}",
            "\\documentclass\n\\begin{document}Body\\end{document}",
        ],
        ids=["incomplete-required", "incomplete-optional", "missing-required"],
    )
    def test_malformed_documentclass_is_preserved(self, tmp_path, content):
        source = p.SourceFile(tmp_path / "main.tex", content)

        edits = p.plan_simplify_documentclass(source, p.LatexDocument(source))

        assert edits == []
        assert p.EditPlanner.apply(source, edits) == content

    @pytest.mark.parametrize(
        "content",
        [
            "\\documentclass{IEEEtran\n\\maketitle\nBody",
            "\\documentclass[12pt\n\\maketitle\nBody",
        ],
        ids=["required-contains-maketitle", "optional-precedes-maketitle"],
    )
    def test_maketitle_in_opaque_documentclass_is_preserved(
        self, tmp_path, content,
    ):
        source = p.SourceFile(tmp_path / "main.tex", content)

        edits = p.plan_simplify_documentclass(source, p.LatexDocument(source))

        assert edits == []
        assert p.EditPlanner.apply(source, edits) == content


class TestPlanUnnumberParagraphs:
    def plan(self, tmp_path, content):
        source = p.SourceFile(tmp_path / "main.tex", content)
        document = p.LatexDocument(source)
        return source, p.plan_unnumber_paragraphs(source, document)

    def test_inserts_star_after_command_token(self, tmp_path):
        source, edits = self.plan(tmp_path, r"\paragraph{Contributions.} Body")

        assert len(edits) == 1
        assert edits[0].start == len(r"\paragraph")
        assert edits[0].start == edits[0].end
        assert edits[0].replacement == "*"
        assert edits[0].safety is p.Safety.LOSSY
        assert (
            p.EditPlanner.apply(source, edits)
            == r"\paragraph*{Contributions.} Body"
        )

    def test_preserves_optional_nested_multiline_content(self, tmp_path):
        content = "\\paragraph[Short]{About \\textbf{Method}\nDetails}\\label{x} Body"
        source, edits = self.plan(tmp_path, content)

        assert p.EditPlanner.apply(source, edits) == content.replace(
            r"\paragraph", r"\paragraph*", 1
        )

    def test_already_starred_is_unchanged(self, tmp_path):
        _, edits = self.plan(tmp_path, r"\paragraph*{Existing}")

        assert edits == []

    def test_incomplete_title_is_unchanged(self, tmp_path):
        _, edits = self.plan(tmp_path, r"\paragraph{Incomplete")

        assert edits == []

    def test_opaque_paragraph_is_unchanged(self, tmp_path):
        _, edits = self.plan(tmp_path, r"\section{Outer \paragraph{Inner}")

        assert edits == []

    def test_idempotent(self, tmp_path):
        source, edits = self.plan(tmp_path, r"\paragraph{Title}")
        once = p.EditPlanner.apply(source, edits)
        second_source, second_edits = self.plan(tmp_path, once)

        assert p.EditPlanner.apply(second_source, second_edits) == once
        assert second_edits == []


class TestParagraphUnnumberingPipeline:
    @pytest.mark.skipif(shutil.which("pandoc") is None, reason="pandoc not installed")
    def test_pandoc_numbers_section_but_not_paragraph(self, tmp_path):
        content = r"\section{Numbered}" + "\n" + r"\paragraph{Unnumbered}" + "\nBody"
        source = p.SourceFile(tmp_path / "main.tex", content)
        transformed = p.EditPlanner.apply(
            source,
            p.plan_unnumber_paragraphs(source, p.LatexDocument(source)),
        )

        result = subprocess.run(
            ["pandoc", "--from=latex", "--to=html5", "--number-sections"],
            input=transformed,
            text=True,
            capture_output=True,
            check=True,
        )

        assert result.stdout.count("header-section-number") == 1
        assert '<h4 class="unnumbered"' in result.stdout


class TestPreserveAppendixNumbering:
    @staticmethod
    def plan(tmp_path, content):
        main = tmp_path / "main.tex"
        main.write_text(content)
        snapshot = p.build_discovery_facts(
            p.DocumentSnapshot.from_directory(tmp_path, main)
        )
        outcome = p.plan_preserve_appendix_numbering(snapshot)
        return snapshot.sources[main], outcome

    def test_preserves_book_appendix_letters_and_order(self, tmp_path):
        source, outcome = self.plan(
            tmp_path,
            r"\chapter{Main}\section{First}"
            r"\appendix"
            r"\chapter{Extra}\section{More}\subsection{Detail}"
            r"\chapter{Other}\section{Last}",
        )

        result = p.EditPlanner.apply(source, outcome.edits)

        assert result == (
            r"\chapter{Main}\section{First}"
            r"\chapter*{A Extra}\section*{A.1 More}"
            r"\subsection*{A.1.1 Detail}"
            r"\chapter*{B Other}\section*{B.1 Last}"
        )
        assert all(edit.safety is p.Safety.SAFE for edit in outcome.edits)

    def test_preserves_article_appendix_letters(self, tmp_path):
        source, outcome = self.plan(
            tmp_path,
            r"\section{Main}\appendix"
            r"\section[Extra short]{Extra}\subsection{More}",
        )

        result = p.EditPlanner.apply(source, outcome.edits)

        assert result == (
            r"\section{Main}"
            r"\section*[A Extra short]{A Extra}"
            r"\subsection*{A.1 More}"
        )

    def test_incomplete_appendix_heading_is_preserved(self, tmp_path):
        source, outcome = self.plan(
            tmp_path, r"\appendix\chapter{Incomplete",
        )

        assert p.EditPlanner.apply(source, outcome.edits) == source.content

    @pytest.mark.skipif(shutil.which("pandoc") is None, reason="pandoc not installed")
    def test_pandoc_keeps_main_numbers_and_explicit_appendix_letters(
        self, tmp_path,
    ):
        source, outcome = self.plan(
            tmp_path,
            r"\chapter{Main}\section{First}"
            r"See Appendix \ref{app:more}."
            r"\appendix\chapter{Extra}"
            r"\section{More}\label{app:more}",
        )
        transformed = p.EditPlanner.apply(source, outcome.edits)

        result = subprocess.run(
            [
                "pandoc", "--from=latex", "--to=html5", "--number-sections",
                f"--lua-filter={p.SCRIPT_DIR / 'filter.lua'}",
            ],
            input=transformed,
            text=True,
            capture_output=True,
            check=True,
        )

        assert 'class="header-section-number">1</span> Main' in result.stdout
        assert ">A Extra</h1>" in result.stdout
        assert ">A.1 More</h2>" in result.stdout
        assert '>A.1</a>' in result.stdout
        assert "[app:more]" not in result.stdout
        assert result.stdout.index("Main") < result.stdout.index("A Extra")


# ── get_input_order ─────────────────────────────────────────────────────────


class TestNormalizeInputsPlanner:
    @staticmethod
    def discovery(*paths):
        return p.DiscoveryFacts(
            title=None,
            authors=(),
            abstract="",
            macros=p.read_only_mapping({}),
            packages=(),
            include_order=tuple(paths),
            graphicspaths=(),
            resource_refs=(),
            labels=p.read_only_mapping({}),
            theorem_labels=p.read_only_mapping({}),
        )

    def test_normalizes_only_targets_in_discovered_include_graph(self, tmp_path):
        main = tmp_path / "main.tex"
        child = tmp_path / "sections" / "child.tex"
        content = r"\input{sections/child} \include{missing}"
        source = p.SourceFile(main, content)
        outcome = p.plan_normalize_inputs(
            source,
            p.LatexDocument(source),
            self.discovery(main, child),
        )
        assert p.EditPlanner.apply(source, outcome.edits) == (
            r"\input{sections/child.tex} \include{missing}"
        )

    def test_incomplete_input_is_preserved_with_diagnostic(self, tmp_path):
        main = tmp_path / "main.tex"
        source = p.SourceFile(main, r"\input{sections/child")
        outcome = p.plan_normalize_inputs(
            source,
            p.LatexDocument(source),
            self.discovery(main, tmp_path / "sections" / "child.tex"),
        )
        assert p.EditPlanner.apply(source, outcome.edits) == source.content
        assert any(d.code == "opaque-structure" for d in outcome.diagnostics)


class TestPdfResources:
    def test_failed_pdf_conversion_keeps_original_reference(self, tmp_path):
        source = p.SourceFile(tmp_path / "main.tex", r"\includegraphics{fig.pdf}")
        resources = p.ResourceResult(converted=frozenset(), diagnostics=())
        outcome = p.plan_rewrite_pdf_refs(
            source, p.LatexDocument(source), resources,
        )
        assert outcome.edits == ()

    def test_successful_pdf_conversion_rewrites_backed_reference(self, tmp_path):
        pdf = tmp_path / "fig.pdf"
        source = p.SourceFile(tmp_path / "main.tex", r"\includegraphics{fig.pdf}")
        resources = p.ResourceResult(converted=frozenset({pdf}), diagnostics=())
        outcome = p.plan_rewrite_pdf_refs(
            source, p.LatexDocument(source), resources,
        )
        assert p.EditPlanner.apply(source, outcome.edits) == (
            r"\includegraphics{fig.png}"
        )

    def test_incomplete_image_reference_is_preserved_with_diagnostic(self, tmp_path):
        source = p.SourceFile(tmp_path / "main.tex", r"\includegraphics{fig.pdf")
        resources = p.ResourceResult(
            converted=frozenset({tmp_path / "fig.pdf"}), diagnostics=(),
        )
        outcome = p.plan_rewrite_pdf_refs(
            source, p.LatexDocument(source), resources,
        )
        assert p.EditPlanner.apply(source, outcome.edits) == source.content
        assert any(d.code == "opaque-structure" for d in outcome.diagnostics)

    def test_pdf_refs_apply_reparse_replan_is_idempotent(self, tmp_path):
        source = p.SourceFile(tmp_path / "main.tex", r"\includegraphics{fig.pdf}")
        resources = p.ResourceResult(
            converted=frozenset({tmp_path / "fig.pdf"}), diagnostics=(),
        )
        first_outcome = p.plan_rewrite_pdf_refs(
            source, p.LatexDocument(source), resources,
        )
        first = p.EditPlanner.apply(source, first_outcome.edits)
        assert first == r"\includegraphics{fig.png}"
        rewritten = p.SourceFile(source.path, first)
        second = p.plan_rewrite_pdf_refs(
            rewritten, p.LatexDocument(rewritten), resources,
        )
        assert second.edits == ()

    def test_conversion_records_only_resources_saved_successfully(
        self, tmp_path, monkeypatch,
    ):
        good = tmp_path / "good.pdf"
        bad = tmp_path / "bad.pdf"
        good.write_bytes(b"good")
        bad.write_bytes(b"bad")

        class FakeImage:
            def __init__(self, fail):
                self.fail = fail

            def save(self, path):
                if self.fail:
                    raise OSError("save failed")
                Path(path).write_bytes(b"png")

        class FakePage:
            def __init__(self, fail):
                self.fail = fail

            def render(self, scale):
                assert scale == 4
                return type("Bitmap", (), {"to_pil": lambda _self: FakeImage(self.fail)})()

        class FakeDocument:
            def __init__(self, path):
                self.fail = Path(path).name == "bad.pdf"

            def __getitem__(self, index):
                assert index == 0
                return FakePage(self.fail)

        fake_pdfium = type("Pdfium", (), {"PdfDocument": FakeDocument})()
        monkeypatch.setitem(sys.modules, "pypdfium2", fake_pdfium)

        result = p.convert_pdf_resources(tmp_path)

        assert result.converted == frozenset({good})
        assert (tmp_path / "good.png").exists()
        assert not (tmp_path / "bad.png").exists()
        assert [d.code for d in result.diagnostics] == ["pdf-conversion-failed"]


class TestReferencePassDependencies:
    def test_reference_passes_declare_barrier_and_post_translation_order(self):
        specs = p.build_reference_passes(p.ResourceResult(frozenset(), ()))
        by_name = {spec.name: spec for spec in specs}
        assert "convert_pdf_resources" in by_name["rewrite_pdf_image_refs"].after
        assert "discover" in by_name["normalize_input_extensions"].after
        assert "translate" in by_name["normalize_input_extensions"].after
        assert all(spec.idempotent for spec in specs)


class TestReplaceTikzFigure:
    def test_replaces_tikz_body_and_preserves_outer_caption(self, tmp_path):
        content = (
            r"\begin{figure}"
            r"\begin{subfigure}{.4\textwidth}"
            r"\begin{tikzpicture}A\end{tikzpicture}"
            r"\caption{First panel}\label{fig:first}\end{subfigure}"
            r"\begin{subfigure}{.4\textwidth}"
            r"\begin{tikzpicture}B\end{tikzpicture}"
            r"\caption{Second panel}\label{fig:second}\end{subfigure}"
            r"\caption{Whole figure}\label{fig:whole}"
            r"\end{figure}"
        )
        source = p.SourceFile(tmp_path / "main.tex", content)
        document = p.LatexDocument(source)
        outcome = p.plan_replace_tikz_figure(
            source,
            document,
            document.environments("figure")[0],
            "paper2epub-figures/tikz-001.png",
        )

        assert len(outcome.edits) == 1
        assert outcome.edits[0].safety is p.Safety.LOSSY
        result = p.EditPlanner.apply(source, outcome.edits)
        assert "tikzpicture" not in result
        assert "First panel" not in result
        assert "Second panel" not in result
        assert (
            r"\includegraphics[width=\linewidth]"
            r"{paper2epub-figures/tikz-001.png}"
        ) in result
        image_end = result.index("tikz-001.png}") + len("tikz-001.png}")
        first_label = result.index(r"\label{fig:first}")
        second_label = result.index(r"\label{fig:second}")
        outer_caption = result.index(r"\caption{Whole figure}")
        assert image_end < first_label < second_label < outer_caption
        assert result.count(r"\label{fig:whole}") == 1
        assert r"\caption{Whole figure}\label{fig:whole}" in result

        rewritten = p.SourceFile(source.path, result)
        second = p.plan_replace_tikz_figure(
            rewritten,
            p.LatexDocument(rewritten),
            p.LatexDocument(rewritten).environments("figure")[0],
            "paper2epub-figures/tikz-001.png",
        )
        assert second.edits == ()

    def test_non_tikz_figure_is_unchanged(self, tmp_path):
        source = p.SourceFile(
            tmp_path / "main.tex",
            r"\begin{figure}\includegraphics{plot.png}\end{figure}",
        )
        document = p.LatexDocument(source)

        outcome = p.plan_replace_tikz_figure(
            source,
            document,
            document.environments("figure")[0],
            "unused.png",
        )

        assert outcome.edits == ()


# ── extract_abstract ────────────────────────────────────────────────────────


class TestPlanTheorems:
    @staticmethod
    def _snapshot(tmp_path, content):
        main = tmp_path / "main.tex"
        main.write_text(content)
        snapshot = p.DocumentSnapshot.from_directory(tmp_path, main)
        discovered = p.build_discovery_facts(snapshot)
        return discovered, discovered.discovery

    def test_theorem_body_may_contain_nested_environments(self, tmp_path):
        snapshot, facts = self._snapshot(
            tmp_path,
            r"\newtheorem{claim}{Claim}"
            r"\begin{claim}[Nested]"
            r"\begin{enumerate}\item See \cite{a}.\end{enumerate}"
            r"\end{claim}",
        )

        outcome = p.plan_theorems(snapshot, facts)
        source = snapshot.sources[snapshot.main_tex]
        result = p.EditPlanner.apply(source, outcome.edits)

        assert r"\begin{enumerate}" in result
        assert r"\textbf{Claim 1}" in result
        assert r"\textit{(Nested)}" in result

    def test_numbers_in_snapshot_include_order(self, tmp_path):
        main = tmp_path / "main.tex"
        child = tmp_path / "child.tex"
        main.write_text(
            r"\newtheorem{claim}{Claim}"
            r"\begin{claim}First.\end{claim}"
            r"\input{child}"
        )
        child.write_text(r"\begin{claim}Second.\end{claim}")
        snapshot = p.build_discovery_facts(
            p.DocumentSnapshot.from_directory(tmp_path, main)
        )

        outcome = p.plan_theorems(snapshot, snapshot.discovery)
        by_file = {}
        for edit in outcome.edits:
            by_file.setdefault(edit.file, []).append(edit)

        main_result = p.EditPlanner.apply(snapshot.sources[main], by_file[main])
        child_result = p.EditPlanner.apply(snapshot.sources[child], by_file[child])
        assert "Claim 1" in main_result
        assert "Claim 2" in child_result

    def test_optional_proof_heading_and_nested_body(self, tmp_path):
        snapshot, facts = self._snapshot(
            tmp_path,
            r"\begin{proof}[Proof of the {main} result]"
            r"\begin{enumerate}\item Done.\end{enumerate}"
            r"\end{proof}",
        )

        outcome = p.plan_theorems(snapshot, facts)
        source = snapshot.sources[snapshot.main_tex]
        result = p.EditPlanner.apply(source, outcome.edits)

        assert r"\textit{Proof of the {main} result.}" in result
        assert r"\begin{enumerate}" in result
        assert r"\hfill$\square$" in result

    def test_preserves_incomplete_custom_theorem_with_diagnostic(self, tmp_path):
        snapshot, facts = self._snapshot(
            tmp_path,
            r"\newtheorem{claim}{Claim}\begin{claim}unfinished",
        )

        outcome = p.plan_theorems(snapshot, facts)

        assert outcome.edits == ()
        assert any(d.code == "opaque-structure" for d in outcome.diagnostics)

    def test_is_idempotent(self, tmp_path):
        snapshot, facts = self._snapshot(
            tmp_path,
            r"\begin{theorem}Statement.\end{theorem}",
        )
        first = p.plan_theorems(snapshot, facts)
        source = snapshot.sources[snapshot.main_tex]
        rewritten = p.EditPlanner.apply(source, first.edits)
        source.path.write_text(rewritten)
        second_snapshot = p.build_discovery_facts(
            p.DocumentSnapshot.from_directory(tmp_path, source.path)
        )

        second = p.plan_theorems(second_snapshot, second_snapshot.discovery)

        assert second.edits == ()

    def test_crlf_renderer_preserves_newlines_and_is_idempotent(self, tmp_path):
        snapshot, facts = self._snapshot(
            tmp_path,
            r"\begin{theorem}" + "\r\nStatement.\r\n" + r"\end{theorem}",
        )
        source = snapshot.sources[snapshot.main_tex]
        first = p.EditPlanner.apply(
            source, p.plan_theorems(snapshot, facts).edits,
        )
        source.path.write_bytes(first.encode())
        second_snapshot = p.build_discovery_facts(
            p.DocumentSnapshot.from_directory(tmp_path, source.path)
        )

        second = p.plan_theorems(
            second_snapshot, second_snapshot.discovery,
        )

        assert b"\n" not in first.encode().replace(b"\r\n", b"")
        assert second.edits == ()

    def test_nested_target_is_preserved_and_does_not_consume_number(self, tmp_path):
        content = (
            r"\begin{theorem}Outer "
            r"\begin{theorem}Nested literal.\end{theorem}"
            r"\end{theorem}"
            r"\begin{theorem}Sibling.\end{theorem}"
        )
        snapshot, facts = self._snapshot(tmp_path, content)

        first_outcome = p.plan_theorems(snapshot, facts)
        source = snapshot.sources[snapshot.main_tex]
        first = p.EditPlanner.apply(source, first_outcome.edits)

        assert r"\textbf{Theorem 1}" in first
        assert r"\begin{theorem}Nested literal.\end{theorem}" in first
        assert r"\textbf{Theorem 2}" in first
        assert "Theorem 3" not in first

        source.path.write_text(first)
        second_snapshot = p.build_discovery_facts(
            p.DocumentSnapshot.from_directory(tmp_path, source.path)
        )
        second_outcome = p.plan_theorems(
            second_snapshot, second_snapshot.discovery,
        )

        assert second_outcome.edits == ()

    def test_user_quote_theorem_is_converted(self, tmp_path):
        snapshot, facts = self._snapshot(
            tmp_path,
            r"\begin{quote}\begin{theorem}User theorem.\end{theorem}"
            r"\end{quote}",
        )

        outcome = p.plan_theorems(snapshot, facts)
        source = snapshot.sources[snapshot.main_tex]
        result = p.EditPlanner.apply(source, outcome.edits)

        assert r"\textbf{Theorem 1}" in result
        assert "paper2epub:generated-theorem" in result

    def test_generated_quote_skips_nested_target_through_intermediate_env(
        self, tmp_path,
    ):
        content = (
            r"\begin{theorem}Outer "
            r"\begin{center}\begin{theorem}Nested.\end{theorem}\end{center}"
            r"\end{theorem}"
        )
        snapshot, facts = self._snapshot(tmp_path, content)
        source = snapshot.sources[snapshot.main_tex]
        first = p.EditPlanner.apply(
            source, p.plan_theorems(snapshot, facts).edits,
        )
        source.path.write_text(first)
        second_snapshot = p.build_discovery_facts(
            p.DocumentSnapshot.from_directory(tmp_path, source.path)
        )

        second = p.plan_theorems(
            second_snapshot, second_snapshot.discovery,
        )

        assert "paper2epub:generated-theorem" in first
        assert second.edits == ()


class TestPlanCodeListings:
    def test_minted_nested_options_and_language_preserve_body(self, tmp_path):
        content = (
            r"\begin{minted}[escapeinside={{(*@}{@*)}}]{python{3}}"
            r"print({\"key\": {1, 2}})"
            r"\end{minted}"
        )
        source = p.SourceFile(tmp_path / "main.tex", content)

        outcome = p.plan_code_listings(source, p.LatexDocument(source))
        result = p.EditPlanner.apply(source, outcome.edits)

        assert result == (
            r"\begin{verbatim}print({\"key\": {1, 2}})\end{verbatim}"
        )
        body_offset = content.index("print")
        assert all(
            not (edit.start <= body_offset < edit.end)
            for edit in outcome.edits
        )

    def test_mintinline_nested_arguments_becomes_texttt(self, tmp_path):
        content = r"\mintinline[escapeinside={{{}{}}}]{python{3}}{x_{i} = {a: b}}"
        source = p.SourceFile(tmp_path / "main.tex", content)

        outcome = p.plan_code_listings(source, p.LatexDocument(source))

        assert p.EditPlanner.apply(source, outcome.edits) == r"\texttt{x_{i} = {a: b}}"

    def test_strips_lstlisting_options_with_nested_groups(self, tmp_path):
        content = r"\begin{lstlisting}[language=C,caption={A {nested} title}]code\end{lstlisting}"
        source = p.SourceFile(tmp_path / "main.tex", content)

        outcome = p.plan_code_listings(source, p.LatexDocument(source))
        result = p.EditPlanner.apply(source, outcome.edits)

        assert result == r"\begin{lstlisting}code\end{lstlisting}"

    def test_preserves_incomplete_minted_with_diagnostic(self, tmp_path):
        content = r"\begin{minted}[linenos]{python}unfinished"
        source = p.SourceFile(tmp_path / "main.tex", content)

        outcome = p.plan_code_listings(source, p.LatexDocument(source))

        assert outcome.edits == ()
        assert any(d.code == "opaque-structure" for d in outcome.diagnostics)

    def test_is_idempotent(self, tmp_path):
        source = p.SourceFile(
            tmp_path / "main.tex",
            r"\begin{minted}{python}code\end{minted}",
        )
        first = p.EditPlanner.apply(
            source, p.plan_code_listings(source, p.LatexDocument(source)).edits,
        )
        rewritten = p.SourceFile(source.path, first)

        second = p.plan_code_listings(rewritten, p.LatexDocument(rewritten))

        assert second.edits == ()


# ── End-to-end preprocessing (file-level) ──────────────────────────────────


class TestEpubCss:
    def test_toc_rule_suppresses_ordered_list_markers(self):
        css = (Path(p.__file__).resolve().parent / "epub.css").read_text()
        toc_rule = css.split("\nol.toc {\n", 1)[1].split("\n}", 1)[0]

        assert "list-style-type: none !important;" in toc_rule

    def test_pre_rule_explicitly_left_aligns_block_code(self):
        css = (Path(p.__file__).resolve().parent / "epub.css").read_text()
        pre_rule = css.split("\npre {\n", 1)[1].split("\n}", 1)[0]

        assert "text-align: left;" in pre_rule

    def test_equation_number_is_positioned_at_the_right_edge(self):
        css = (Path(p.__file__).resolve().parent / "epub.css").read_text()
        wrapper_rule = css.split("\n.numbered-equation {\n", 1)[1].split(
            "\n}", 1,
        )[0]
        number_rule = css.split("\n.equation-number {\n", 1)[1].split(
            "\n}", 1,
        )[0]

        assert "position: relative;" in wrapper_rule
        assert "position: absolute;" in number_rule
        assert "right: 0;" in number_rule


@pytest.mark.skipif(shutil.which("pandoc") is None, reason="pandoc is required")
class TestPandocFilter:
    @staticmethod
    def render_html(
        tmp_path,
        content,
        *,
        numbering_scope="global",
        subequations="",
    ):
        tex = tmp_path / "equations.tex"
        tex.write_text(content)
        return subprocess.run(
            [
                "pandoc",
                str(tex),
                "--from",
                "latex",
                "--to",
                "html5",
                "--mathml",
                "--metadata",
                f"paper2epub-numbering-scope={numbering_scope}",
                "--metadata",
                f"paper2epub-subequations={subequations}",
                f"--lua-filter={p.SCRIPT_DIR / 'filter.lua'}",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    def test_numbers_and_links_book_equations_within_chapters(self, tmp_path):
        html = self.render_html(
            tmp_path,
            r"\documentclass{article}"
            r"\begin{document}"
            r"\chapter{First}"
            r"\begin{equation*}z=0\end{equation*}"
            r"\begin{equation}\label{eq:first}x=1\end{equation}"
            r"In \eqref{eq:first}."
            r"\chapter{Second}"
            r"\begin{equation}\label{eq:second}y=2\end{equation}"
            r"In \eqref{eq:second}."
            r"\end{document}",
            numbering_scope="chapter",
        )

        assert re.search(r'id="eq:first"\s+class="numbered-equation"', html)
        assert re.search(r'id="eq:second"\s+class="numbered-equation"', html)
        assert html.count('class="equation-number">(1.1)</span>') == 1
        assert html.count('class="equation-number">(2.1)</span>') == 1
        assert '<a href="#eq:first"' in html
        assert 'data-reference="eq:first">(1.1)</a>' in html
        assert 'data-reference="eq:second">(2.1)</a>' in html

    def test_keeps_article_equation_numbers_global(self, tmp_path):
        html = self.render_html(
            tmp_path,
            r"\documentclass{article}"
            r"\begin{document}"
            r"\section{First}"
            r"\begin{equation}\label{eq:first}x=1\end{equation}"
            r"\begin{figure}\includegraphics{first.png}"
            r"\caption{First figure}\label{fig:first}\end{figure}"
            r"\begin{table}\begin{tabular}{c}A\end{tabular}"
            r"\caption{First table}\label{tab:first}\end{table}"
            r"In \eqref{eq:first}."
            r"\section{Second}"
            r"\begin{equation}\label{eq:second}y=2\end{equation}"
            r"\begin{figure}\includegraphics{second.png}"
            r"\caption{Second figure}\label{fig:second}\end{figure}"
            r"\begin{table}\begin{tabular}{c}B\end{tabular}"
            r"\caption{Second table}\label{tab:second}\end{table}"
            r"In \eqref{eq:second}."
            r"\end{document}",
        )

        assert html.count('class="equation-number">(1)</span>') == 1
        assert html.count('class="equation-number">(2)</span>') == 1
        assert 'data-reference="eq:first">(1)</a>' in html
        assert 'data-reference="eq:second">(2)</a>' in html
        assert "Figure 1:" in html
        assert "Figure 2:" in html
        assert "Table 1:" in html
        assert "Table 2:" in html

    def test_subequations_share_one_parent_counter(self, tmp_path):
        html = self.render_html(
            tmp_path,
            r"\documentclass{article}"
            r"\begin{document}"
            r"\chapter{First}"
            r"\begin{equation}\label{eq:first}x=1\end{equation}"
            r"\begin{align}"
            r"a&=1\label{subeq:a}\\"
            r"b&=2\label{subeq:b}"
            r"\end{align}"
            r"See \eqref{eq:pair}, \eqref{subeq:a}, and \eqref{subeq:b}."
            r"\begin{equation}\label{eq:after}y=2\end{equation}"
            r"See \eqref{eq:after}."
            r"\end{document}",
            numbering_scope="chapter",
            subequations="eq:pair|subeq:a|subeq:b",
        )

        assert 'data-reference="eq:pair">(1.2)</a>' in html
        assert 'data-reference="subeq:a">(1.2a)</a>' in html
        assert 'data-reference="subeq:b">(1.2b)</a>' in html
        assert 'data-reference="eq:after">(1.3)</a>' in html
        assert 'id="eq:pair"' in html
        assert 'id="subeq:a"' in html
        assert 'id="subeq:b"' in html

    def test_numbers_figures_and_tables_within_chapters(self, tmp_path):
        html = self.render_html(
            tmp_path,
            r"\documentclass{article}"
            r"\begin{document}"
            r"\chapter{First}"
            r"\begin{equation}\label{eq:first}x=1\end{equation}"
            r"\begin{figure}\includegraphics{first.png}\label{fig:first-a}"
            r"\caption{First figure}\label{fig:first}\end{figure}"
            r"\begin{table}\begin{tabular}{c}A\end{tabular}"
            r"\caption{First table}\label{tab:first}\end{table}"
            r"See \eqref{eq:first}, Figure \ref{fig:first}, "
            r"subfigure \ref{fig:first-a}, and Table \ref{tab:first}."
            r"\chapter{Second}"
            r"\begin{equation}\label{eq:second}y=2\end{equation}"
            r"\begin{figure}\includegraphics{second.png}"
            r"\caption{Second figure}\label{fig:second}\end{figure}"
            r"\begin{table}\begin{tabular}{c}B\end{tabular}"
            r"\caption{Second table}\label{tab:second}\end{table}"
            r"See \eqref{eq:second}, Figure \ref{fig:second}, "
            r"and Table \ref{tab:second}."
            r"\end{document}",
            numbering_scope="chapter",
        )

        assert "Figure 1.1:" in html
        assert "Figure 2.1:" in html
        assert "Table 1.1:" in html
        assert "Table 2.1:" in html
        assert 'data-reference="eq:first">(1.1)</a>' in html
        assert 'data-reference="eq:second">(2.1)</a>' in html
        assert 'data-reference="fig:first">1.1</a>' in html
        assert 'data-reference="fig:second">2.1</a>' in html
        assert 'data-reference="tab:first">1.1</a>' in html
        assert 'data-reference="tab:second">2.1</a>' in html
        assert 'id="fig:first-a"' in html
        assert 'data-reference="fig:first-a">1.1a</a>' in html

    def test_numbers_appendix_figures_and_tables(self, tmp_path):
        html = self.render_html(
            tmp_path,
            r"\documentclass{article}"
            r"\begin{document}"
            r"\chapter{Main}"
            r"\begin{figure}\includegraphics{main.png}"
            r"\caption{Main figure}\label{fig:main}\end{figure}"
            r"\begin{table}\begin{tabular}{c}M\end{tabular}"
            r"\caption{Main table}\label{tab:main}\end{table}"
            r"\chapter*{A Supplementary Materials}"
            r"\begin{figure}\includegraphics{appendix.png}"
            r"\caption{Appendix figure}\label{fig:appendix}\end{figure}"
            r"\begin{table}\begin{tabular}{c}A\end{tabular}"
            r"\caption{Appendix table}\label{tab:appendix}\end{table}"
            r"See Figure \ref{fig:appendix} and Table \ref{tab:appendix}."
            r"\end{document}",
            numbering_scope="chapter",
        )

        assert "Figure 1.1:" in html
        assert "Table 1.1:" in html
        assert "Figure A.1:" in html
        assert "Table A.1:" in html
        assert 'data-reference="fig:appendix">A.1</a>' in html
        assert 'data-reference="tab:appendix">A.1</a>' in html

    def test_resolves_preserved_subfigure_labels(self, tmp_path):
        html = self.render_html(
            tmp_path,
            r"\documentclass{article}"
            r"\begin{document}"
            r"\begin{figure}"
            r"\includegraphics{plot.png}"
            r"\label{fig:first}\label{fig:second}"
            r"\caption{Whole figure}\label{fig:whole}"
            r"\end{figure}"
            r"See Figure \ref{fig:first}, Figure \ref{fig:second}, "
            r"and Figure \ref{fig:whole}."
            r"\end{document}",
        )

        assert 'id="fig:first"' in html
        assert 'id="fig:second"' in html
        assert 'data-reference="fig:first">1a</a>' in html
        assert 'data-reference="fig:second">1b</a>' in html
        assert 'data-reference="fig:whole">1</a>' in html
        assert "[fig:first]" not in html
        assert "[fig:second]" not in html

    def test_resolves_generated_theorem_ref_numbers(self, tmp_path):
        tex = tmp_path / "refs.tex"
        tex.write_text(
            r"\documentclass{article}"
            r"\begin{document}"
            r"\begin{quote}"
            r"\textbf{Example 1} \textit{(Regression)}\textbf{.}"
            r"\label{ex:regression}Statement."
            r"\end{quote}"
            r"\begin{quote}"
            r"\textbf{Example 2} \textit{(Classification)}\textbf{.}"
            r"\label{ex:classification}Statement."
            r"\end{quote}"
            r"Examples \ref{ex:regression} and \ref{ex:classification}. "
            r"See \autoref{ex:regression}."
            r"\begin{quote}"
            r"\textbf{Theorem 3}\textbf{.}"
            r"\label{thm:plain}Statement."
            r"\end{quote}"
            r"Theorem \ref{thm:plain}."
            + p.build_algorithm_output("Test method", "alg:test", [], 4)
            + r"Algorithm \ref{alg:test}."
            r"\end{document}"
        )

        completed = subprocess.run(
            [
                "pandoc",
                str(tex),
                "--from",
                "latex",
                "--to",
                "plain",
                f"--lua-filter={p.SCRIPT_DIR / 'filter.lua'}",
            ],
            check=True,
            capture_output=True,
            text=True,
        )

        assert "Examples 1 and 2." in completed.stdout
        assert "See Example 1." in completed.stdout.replace("\N{NO-BREAK SPACE}", " ")
        assert "Theorem 3." in completed.stdout
        assert "Algorithm 4." in completed.stdout
        assert "[ex:" not in completed.stdout
        assert "[thm:" not in completed.stdout
        assert "[alg:" not in completed.stdout


class TestNumberingScope:
    def test_uses_chapter_scope_for_numbered_chapters(self, tmp_path):
        source = p.SourceFile(
            tmp_path / "book.tex",
            r"\documentclass{article}\chapter{One}\chapter*{Notes}",
        )

        assert p._numbering_scope(source) == "chapter"

    def test_uses_global_scope_without_numbered_chapters(self, tmp_path):
        source = p.SourceFile(
            tmp_path / "article.tex",
            r"\documentclass{article}\section{One}\chapter*{Notes}",
        )

        assert p._numbering_scope(source) == "global"

    def test_run_pandoc_passes_detected_scope_to_filter(
        self, tmp_path, monkeypatch,
    ):
        main = tmp_path / "book.tex"
        main.write_text(
            r"\documentclass{article}\begin{document}\chapter{One}"
            r"\begin{subequations}\label{eq:pair}"
            r"\begin{align}a&=1\label{subeq:a}\end{align}"
            r"\end{subequations}\end{document}"
        )
        captured = {}
        monkeypatch.setattr(
            p, "_pandoc_resource_paths", lambda cwd, discovery=None: ["."],
        )
        monkeypatch.setattr(
            p.subprocess,
            "run",
            lambda args, **kwargs: captured.update(args=args, kwargs=kwargs),
        )

        p.run_pandoc(main, tmp_path / "book.epub", None, workdir=tmp_path)

        assert "paper2epub-numbering-scope=chapter" in captured["args"]
        assert "paper2epub-subequations=eq:pair|subeq:a" in captured["args"]

    def test_collects_parent_and_child_subequation_labels(self, tmp_path):
        source = p.SourceFile(
            tmp_path / "book.tex",
            r"\begin{subequations}"
            r"\label{eq:pair}"
            r"\begin{align}"
            r"a&=1\label{subeq:a}\\b&=2\label{subeq:b}"
            r"\end{align}"
            r"\end{subequations}"
            r"\begin{subequations}"
            r"\begin{align}c&=3\label{subeq:c}\end{align}"
            r"\end{subequations}",
        )

        assert p._subequation_label_groups(source) == (
            ("eq:pair", "subeq:a", "subeq:b"),
            ("", "subeq:c"),
        )
