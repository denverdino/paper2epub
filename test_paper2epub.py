"""Unit tests for paper2epub.py — pure-function coverage, no network/API calls."""

import pytest
from pathlib import Path

import paper2epub as p


# ── Brace matching ──────────────────────────────────────────────────────────


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


class TestUnwrapLatexCmd:
    def test_adjustbox(self):
        assert p._unwrap_latex_cmd(
            r"\adjustbox{width=5cm}{content}", r"\adjustbox", 2
        ) == "content"

    def test_resizebox(self):
        assert p._unwrap_latex_cmd(
            r"\resizebox{1cm}{!}{hello}", r"\resizebox", 3, star=True
        ) == "hello"

    def test_resizebox_star(self):
        assert p._unwrap_latex_cmd(
            r"\resizebox*{1cm}{!}{hello}", r"\resizebox", 3, star=True
        ) == "hello"

    def test_texorpdfstring_keep_first(self):
        assert p._unwrap_latex_cmd(
            r"\texorpdfstring{$\alpha$}{alpha}", r"\texorpdfstring", 2, keep=0
        ) == "$\\alpha$"

    def test_no_match_passes_through(self):
        text = "no commands here"
        assert p._unwrap_latex_cmd(text, r"\foo", 1) == text

    def test_partial_name_not_matched(self):
        text = r"\adjustboxes are cool"
        assert p._unwrap_latex_cmd(text, r"\adjustbox", 2) == text

    def test_multiple_occurrences(self):
        text = r"\adjustbox{a}{X} and \adjustbox{b}{Y}"
        assert p._unwrap_latex_cmd(text, r"\adjustbox", 2) == "X and Y"

    def test_surrounding_text_preserved(self):
        text = r"before \resizebox{w}{h}{inner} after"
        assert p._unwrap_latex_cmd(text, r"\resizebox", 3, star=True) == "before inner after"


# ── _parse_numbered_response ────────────────────────────────────────────────


class TestParseNumberedResponse:
    def test_basic(self):
        raw = "[0]\nhello\n\n[1]\nworld"
        assert p._parse_numbered_response(raw) == {0: "hello", 1: "world"}

    def test_with_postprocess(self):
        raw = "[0]\nhello}"
        result = p._parse_numbered_response(raw, postprocess=p._fix_braces)
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


# ── _fix_braces ─────────────────────────────────────────────────────────────


class TestFixBraces:
    def test_balanced(self):
        assert p._fix_braces("{ok}") == "{ok}"

    def test_extra_closing(self):
        assert p._fix_braces("text}extra}") == "textextra"

    def test_nested_balanced(self):
        assert p._fix_braces("{a{b}c}") == "{a{b}c}"

    def test_mixed_unbalanced(self):
        assert p._fix_braces("{a}b}c{d}") == "{a}bc{d}"


# ── Title & Author extraction ──────────────────────────────────────────────


class TestExtractTitle:
    def test_simple_title(self, tmp_path):
        tex = tmp_path / "main.tex"
        tex.write_text(r"\title{My Paper}")
        assert p.extract_title(tex, {}) == "My Paper"

    def test_title_with_short_title(self, tmp_path):
        tex = tmp_path / "main.tex"
        tex.write_text(r"\title[short]{Full Title Here}")
        assert p.extract_title(tex, {}) == "Full Title Here"

    def test_title_with_macro(self, tmp_path):
        tex = tmp_path / "main.tex"
        tex.write_text(r"\title{The \myabbr Method}")
        assert p.extract_title(tex, {"myabbr": "COOL"}) == "The COOLMethod"

    def test_no_title(self, tmp_path):
        tex = tmp_path / "main.tex"
        tex.write_text(r"\begin{document} hello \end{document}")
        assert p.extract_title(tex, {}) is None

    def test_title_with_linebreak(self, tmp_path):
        tex = tmp_path / "main.tex"
        tex.write_text(r"\title{Line One \\ Line Two}")
        assert p.extract_title(tex, {}) == "Line One Line Two"

    def test_title_via_content_param(self, tmp_path):
        tex = tmp_path / "main.tex"
        tex.write_text("unused")
        result = p.extract_title(tex, {}, _content=r"\title{Direct}")
        assert result == "Direct"

    def test_title_with_nested_braces(self, tmp_path):
        tex = tmp_path / "main.tex"
        tex.write_text(r"\title{Learning $\mathcal{F}$ Spaces}")
        # extract_title strips \cmd{arg} → arg, so \mathcal{F} → F
        assert p.extract_title(tex, {}) == "Learning $F$ Spaces"

    def test_comment_stripped(self, tmp_path):
        tex = tmp_path / "main.tex"
        tex.write_text("% \\title{Commented}\n\\title{Actual}")
        assert p.extract_title(tex, {}) == "Actual"


class TestExtractAuthors:
    def test_single_author(self, tmp_path):
        tex = tmp_path / "main.tex"
        tex.write_text(r"\author{John Smith}")
        assert p.extract_authors(tex, {}) == ["John Smith"]

    def test_multiple_authors(self, tmp_path):
        tex = tmp_path / "main.tex"
        tex.write_text(r"\author{Alice, Bob, Charlie}")
        assert p.extract_authors(tex, {}) == ["Alice", "Bob", "Charlie"]

    def test_no_author(self, tmp_path):
        tex = tmp_path / "main.tex"
        tex.write_text(r"\begin{document}")
        assert p.extract_authors(tex, {}) == []

    def test_affiliations_stripped(self, tmp_path):
        tex = tmp_path / "main.tex"
        tex.write_text(r"\author{Alice, Bob \\ University}")
        assert p.extract_authors(tex, {}) == ["Alice", "Bob"]


# ── Macro handling ──────────────────────────────────────────────────────────


class TestExpandMacros:
    def test_simple(self):
        assert p.expand_macros(r"\foo bar", {"foo": "FOO"}) == "FOObar"

    def test_recursive(self):
        macros = {"a": r"\b", "b": "DONE"}
        assert p.expand_macros(r"\a", macros) == "DONE"

    def test_no_match(self):
        assert p.expand_macros("no macros", {"x": "y"}) == "no macros"

    def test_depth_limit(self):
        macros = {"a": r"\a"}  # infinite recursion
        result = p.expand_macros(r"\a", macros, depth=3)
        assert r"\a" in result  # stops after depth


class TestCollectMacros:
    def test_newcommand(self, tmp_path):
        tex = tmp_path / "test.tex"
        tex.write_text(r"\newcommand{\foo}{bar}")
        macros = p.collect_macros(tmp_path)
        assert macros["foo"] == "bar"

    def test_renewcommand(self, tmp_path):
        tex = tmp_path / "test.tex"
        tex.write_text(r"\renewcommand{\baz}{qux}")
        macros = p.collect_macros(tmp_path)
        assert macros["baz"] == "qux"

    def test_declare_math_operator(self, tmp_path):
        tex = tmp_path / "test.tex"
        tex.write_text(r"\DeclareMathOperator{\argmax}{arg\,max}")
        macros = p.collect_macros(tmp_path)
        assert macros["argmax"] == r"\operatorname{arg\,max}"

    def test_xspace_stripped(self, tmp_path):
        tex = tmp_path / "test.tex"
        tex.write_text(r"\newcommand{\myname}{Cool\xspace}")
        macros = p.collect_macros(tmp_path)
        assert macros["myname"] == "Cool"


# ── TeX content transforms ─────────────────────────────────────────────────


class TestStripProblematicPackages:
    def test_strips_single_package(self):
        content = r"\usepackage{hyperref}"
        assert p._strip_problematic_packages_content(content).strip() == ""

    def test_keeps_unknown_package(self):
        content = r"\usepackage{amsmath}"
        assert "amsmath" in p._strip_problematic_packages_content(content)

    def test_splits_multi_package(self):
        content = r"\usepackage{amsmath, hyperref}"
        result = p._strip_problematic_packages_content(content)
        assert "amsmath" in result
        assert "hyperref" not in result

    def test_strips_config_command(self):
        content = r"\hypersetup{colorlinks=true}"
        result = p._strip_problematic_packages_content(content)
        assert "hypersetup" not in result

    def test_strips_makeatletter(self):
        content = "\\makeatletter\nsome code\n\\makeatother"
        result = p._strip_problematic_packages_content(content)
        assert "makeatletter" not in result
        assert "makeatother" not in result


class TestStripNoiseContent:
    def test_strips_noindent(self):
        assert "noindent" not in p._strip_noise_content(r"\noindent Hello")

    def test_strips_vspace(self):
        result = p._strip_noise_content(r"\vspace{1cm} text")
        assert "vspace" not in result
        assert "text" in result

    def test_strips_vspace_star(self):
        result = p._strip_noise_content(r"\vspace*{1cm} text")
        assert "vspace" not in result
        assert "text" in result

    def test_strips_setlength(self):
        result = p._strip_noise_content(r"\setlength{\parindent}{0pt} text")
        assert "setlength" not in result
        assert "text" in result

    def test_strips_tikzpicture(self):
        content = r"before \begin{tikzpicture}\draw (0,0) -- (1,1);\end{tikzpicture} after"
        result = p._strip_noise_content(content)
        assert "tikzpicture" not in result
        assert "before" in result
        assert "after" in result

    def test_keeps_normal_text(self):
        text = "Just normal text"
        assert p._strip_noise_content(text) == text


class TestNormalizeCitations:
    def test_citep(self):
        result = p._CITE_RE.sub(r"\\cite{\1}", r"\citep{smith2020}")
        assert result == r"\cite{smith2020}"

    def test_textcite(self):
        result = p._CITE_RE.sub(r"\\cite{\1}", r"\textcite{jones2021}")
        assert result == r"\cite{jones2021}"

    def test_citep_with_options(self):
        result = p._CITE_RE.sub(r"\\cite{\1}", r"\citep[see][p.~5]{ref}")
        assert result == r"\cite{ref}"

    def test_existing_cite_unchanged(self):
        original = r"\cite{ref}"
        result = p._CITE_RE.sub(r"\\cite{\1}", original)
        assert result == original


class TestPreprocessHyperref:
    def test_hyperref_unwrapped(self):
        result = p._preprocess_hyperref_content(r"\hyperref[sec:intro]{Introduction}")
        assert result == "Introduction"

    def test_texorpdfstring(self):
        result = p._preprocess_hyperref_content(r"\texorpdfstring{$\alpha$}{alpha}")
        assert result == "$\\alpha$"

    def test_combined(self):
        content = r"\hyperref[tab]{\texorpdfstring{$x$}{x} table}"
        result = p._preprocess_hyperref_content(content)
        assert "$x$ table" == result


class TestNormalizeTableEnvs:
    def test_tabu_to_tabular(self):
        content = r"\begin{tabu} ... \end{tabu}"
        result = p._normalize_table_envs_content(content)
        assert r"\begin{tabular}" in result
        assert r"\end{tabular}" in result

    def test_tabularx_strips_width(self):
        content = r"\begin{tabularx}{\textwidth}{lXr} ... \end{tabularx}"
        result = p._normalize_table_envs_content(content)
        assert r"\begin{tabular}" in result
        assert r"\end{tabular}" in result

    def test_nicetabular(self):
        content = r"\begin{NiceTabular} ... \end{NiceTabular}"
        result = p._normalize_table_envs_content(content)
        assert r"\begin{tabular}" in result

    def test_tblr(self):
        content = r"\begin{tblr}{colspec={ll}} ... \end{tblr}"
        result = p._normalize_table_envs_content(content)
        assert r"\begin{tabular}{l}" in result
        assert r"\end{tabular}" in result

    def test_no_change_for_tabular(self):
        content = r"\begin{tabular}{ll} ... \end{tabular}"
        assert p._normalize_table_envs_content(content) == content


class TestNormalizeCodeListings:
    def test_minted_to_verbatim(self):
        content = r"\begin{minted}{python}print()\end{minted}"
        result = p._normalize_code_listings_content(content)
        assert r"\begin{verbatim}" in result
        assert r"\end{verbatim}" in result

    def test_minted_with_options(self):
        content = r"\begin{minted}[linenos]{python}code\end{minted}"
        result = p._normalize_code_listings_content(content)
        assert r"\begin{verbatim}" in result

    def test_mintinline(self):
        content = r"\mintinline{python}{x = 1}"
        result = p._normalize_code_listings_content(content)
        assert result == r"\texttt{x = 1}"

    def test_lstlisting_options_stripped(self):
        content = r"\begin{lstlisting}[language=C] code \end{lstlisting}"
        result = p._normalize_code_listings_content(content)
        assert r"\begin{lstlisting}" in result
        assert "[language=C]" not in result


class TestNormalizeSiunitx:
    def test_s_column_replaced(self):
        content = r"\begin{tabular}{lSr}"
        result = p._normalize_siunitx_content(content)
        assert r"\begin{tabular}{lrr}" == result

    def test_s_with_options(self):
        content = r"\begin{tabular}{S[round-mode=places] l}"
        result = p._normalize_siunitx_content(content)
        assert r"\begin{tabular}{r l}" == result

    def test_no_s_columns_unchanged(self):
        content = r"\begin{tabular}{l c r}"
        assert p._normalize_siunitx_content(content) == content


class TestRemoveResizebox:
    def test_basic(self):
        assert p._remove_resizebox(r"\resizebox{1cm}{!}{content}") == "content"

    def test_star(self):
        assert p._remove_resizebox(r"\resizebox*{1cm}{!}{content}") == "content"

    def test_surrounding_text(self):
        result = p._remove_resizebox(r"before \resizebox{w}{h}{inner} after")
        assert result == "before inner after"


class TestRemoveAdjustboxCmd:
    def test_basic(self):
        assert p._remove_adjustbox_cmd(r"\adjustbox{width=5cm}{table content}") == "table content"

    def test_not_adjustboxes(self):
        text = r"\adjustboxes is a word"
        assert p._remove_adjustbox_cmd(text) == text


class TestReplaceCaptionof:
    def test_basic_conversion(self):
        content = (
            r"\begin{minipage}{\textwidth}"
            r"\includegraphics{fig}"
            r"\captionof{figure}{A figure}"
            r"\end{minipage}"
        )
        result = p._replace_captionof_blocks(content)
        assert r"\begin{figure}[H]" in result
        assert r"\end{figure}" in result
        assert r"\caption{A figure}" in result

    def test_no_captionof_unchanged(self):
        content = r"\begin{minipage}{\textwidth}text\end{minipage}"
        assert p._replace_captionof_blocks(content) == content


# ── Float/figure transforms ────────────────────────────────────────────────


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
    def test_basic(self):
        content = r"\begin{wrapfigure}{r}{0.5\textwidth}img\end{wrapfigure}"
        result = p._WRAPFIG_RE.sub(r"\\begin{figure}[H]", content).replace(
            "\\end{wrapfigure}", "\\end{figure}"
        )
        assert r"\begin{figure}[H]" in result
        assert r"\end{figure}" in result


class TestUnwrapSubfigures:
    def test_basic(self):
        content = r"\begin{subfigure}[b]{0.5\textwidth}img\end{subfigure}"
        result = p._SUBFIG_BEGIN_RE.sub("", content).replace("\\end{subfigure}", "")
        assert "subfigure" not in result
        assert "img" in result


# ── Ding commands ───────────────────────────────────────────────────────────


class TestDingMap:
    def test_checkmark(self):
        result = p._DING_RE.sub(
            lambda m: p.DING_MAP.get(m.group(1), m.group(0)),
            r"\ding{51}",
        )
        assert result == "✓"

    def test_unknown_code_preserved(self):
        result = p._DING_RE.sub(
            lambda m: p.DING_MAP.get(m.group(1), m.group(0)),
            r"\ding{999}",
        )
        assert result == r"\ding{999}"


# ── Translation helpers ────────────────────────────────────────────────────


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


class TestFindSkipRanges:
    def test_single_env(self):
        content = r"text \begin{figure}fig\end{figure} more"
        ranges = p._find_skip_ranges(content)
        assert len(ranges) == 1
        assert content[ranges[0][0]:ranges[0][1]] == r"\begin{figure}fig\end{figure}"

    def test_nested(self):
        content = r"\begin{table}\begin{tabular}x\end{tabular}\end{table}"
        ranges = p._find_skip_ranges(content)
        assert len(ranges) == 2

    def test_no_skip_envs(self):
        content = r"\begin{document}text\end{document}"
        assert p._find_skip_ranges(content) == []


class TestChunkInSkipRange:
    def test_fully_inside(self):
        assert p._chunk_in_skip_range(5, 10, [(0, 20)])

    def test_outside(self):
        assert not p._chunk_in_skip_range(25, 30, [(0, 20)])

    def test_overlapping(self):
        assert p._chunk_in_skip_range(15, 25, [(0, 20)])


class TestSplitIntoChunks:
    def test_basic(self):
        chunks = p._split_into_chunks("a\n\nb\n\nc")
        assert chunks == ["a", "b", "c"]

    def test_no_split(self):
        assert p._split_into_chunks("single paragraph") == ["single paragraph"]


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

    def test_preserves_non_heading(self):
        text = "Just a paragraph."
        assert p._strip_heading_lines(text) == text


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


class TestNormalizeTextsc:
    def test_replaces_textsc(self):
        assert p._TEXTSC_RE.sub(r"\\text", r"\textsc{Hello}") == r"\text{Hello}"

    def test_preserves_text(self):
        assert p._TEXTSC_RE.sub(r"\\text", r"\text{ok}") == r"\text{ok}"

    def test_in_math(self):
        src = r"$\textsc{Foo}(x)$"
        assert p._TEXTSC_RE.sub(r"\\text", src) == r"$\text{Foo}(x)$"

    def test_no_match(self):
        src = "no textsc here"
        assert p._TEXTSC_RE.sub(r"\\text", src) == src


class TestProcessAlgorithms:
    def test_full_algorithm(self):
        tex = (
            r"\begin{algorithm}" + "\n"
            r"\caption{Test}" + "\n"
            r"\begin{algorithmic}[1]" + "\n"
            r"\Require input" + "\n"
            r"\Return output" + "\n"
            r"\end{algorithmic}" + "\n"
            r"\end{algorithm}"
        )
        result = p.process_algorithms(tex)
        assert "algorithmdisplay" in result
        assert "Algorithm" in result
        assert "Require" in result

    def test_no_algorithm(self):
        text = "just text"
        assert p.process_algorithms(text) == text


# ── _transform_tex_files helper ─────────────────────────────────────────────


class TestTransformTexFiles:
    def test_transforms_file(self, tmp_path):
        tex = tmp_path / "test.tex"
        tex.write_text("hello world")
        p._transform_tex_files(
            tmp_path, lambda c: c.replace("hello", "goodbye"), "Test",
        )
        assert tex.read_text() == "goodbye world"

    def test_no_change_no_write(self, tmp_path):
        tex = tmp_path / "test.tex"
        tex.write_text("unchanged")
        mtime_before = tex.stat().st_mtime
        p._transform_tex_files(tmp_path, lambda c: c, "Test")
        # file should not be rewritten
        assert tex.stat().st_mtime == mtime_before

    def test_guard_skips(self, tmp_path):
        tex = tmp_path / "test.tex"
        tex.write_text("no match here")
        p._transform_tex_files(
            tmp_path, lambda c: "CHANGED", "Test", guard="TRIGGER",
        )
        assert tex.read_text() == "no match here"

    def test_guard_passes(self, tmp_path):
        tex = tmp_path / "test.tex"
        tex.write_text("has TRIGGER word")
        p._transform_tex_files(
            tmp_path, lambda c: c.replace("TRIGGER", "DONE"), "Test", guard="TRIGGER",
        )
        assert "DONE" in tex.read_text()

    def test_glob_filter(self, tmp_path):
        (tmp_path / "a.tex").write_text("change me")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "b.tex").write_text("change me")
        p._transform_tex_files(
            tmp_path, lambda c: c.replace("change", "done"), "Test", glob="*.tex",
        )
        assert (tmp_path / "a.tex").read_text() == "done me"
        assert (sub / "b.tex").read_text() == "change me"  # not matched by *.tex


# ── find_main_tex ───────────────────────────────────────────────────────────


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


# ── simplify_documentclass ──────────────────────────────────────────────────


class TestSimplifyDocumentclass:
    def test_replaces_complex_class(self, tmp_path):
        tex = tmp_path / "main.tex"
        tex.write_text(r"\documentclass[12pt,twocolumn]{IEEEtran}")
        p.simplify_documentclass(tex)
        assert tex.read_text() == r"\documentclass{article}"

    def test_strips_maketitle(self, tmp_path):
        tex = tmp_path / "main.tex"
        tex.write_text("\\documentclass{article}\n\\maketitle\n")
        p.simplify_documentclass(tex)
        assert "maketitle" not in tex.read_text()

    def test_already_simple(self, tmp_path):
        tex = tmp_path / "main.tex"
        original = r"\documentclass{article}"
        tex.write_text(original)
        p.simplify_documentclass(tex)
        assert tex.read_text() == original


# ── get_input_order ─────────────────────────────────────────────────────────


class TestGetInputOrder:
    def test_single_file(self, tmp_path):
        main = tmp_path / "main.tex"
        main.write_text("just content")
        result = p.get_input_order(main)
        assert result == [main]

    def test_follows_input(self, tmp_path):
        (tmp_path / "chap.tex").write_text("chapter content")
        main = tmp_path / "main.tex"
        main.write_text(r"\input{chap}")
        result = p.get_input_order(main)
        assert len(result) == 2
        assert result[0] == main
        assert result[1].name == "chap.tex"

    def test_follows_include(self, tmp_path):
        (tmp_path / "appendix.tex").write_text("appendix content")
        main = tmp_path / "main.tex"
        main.write_text(r"\include{appendix}")
        result = p.get_input_order(main)
        assert len(result) == 2

    def test_no_duplicates(self, tmp_path):
        (tmp_path / "shared.tex").write_text("shared")
        main = tmp_path / "main.tex"
        main.write_text(r"\input{shared}\input{shared}")
        result = p.get_input_order(main)
        assert len(result) == 2  # main + shared, no dup


# ── extract_abstract ────────────────────────────────────────────────────────


class TestExtractAbstract:
    def test_basic(self, tmp_path):
        tex = tmp_path / "main.tex"
        tex.write_text(r"\begin{abstract}This is abstract.\end{abstract}")
        assert p.extract_abstract(tex) == "This is abstract."

    def test_no_abstract(self, tmp_path):
        tex = tmp_path / "main.tex"
        tex.write_text(r"\begin{document}\end{document}")
        assert p.extract_abstract(tex) == ""


# ── Theorem preprocessing (integration) ────────────────────────────────────


class TestPreprocessTheorems:
    def test_basic_theorem(self, tmp_path):
        tex = tmp_path / "paper.tex"
        tex.write_text(r"\begin{theorem}Some statement.\end{theorem}")
        p.preprocess_theorems(tmp_path)
        result = tex.read_text()
        assert r"\begin{quote}" in result
        assert "Theorem 1" in result
        assert "Some statement." in result

    def test_theorem_with_name(self, tmp_path):
        tex = tmp_path / "paper.tex"
        tex.write_text(r"\begin{theorem}[Fermat] Statement.\end{theorem}")
        p.preprocess_theorems(tmp_path)
        result = tex.read_text()
        assert "Fermat" in result

    def test_proof(self, tmp_path):
        tex = tmp_path / "paper.tex"
        tex.write_text(r"\begin{proof}By induction.\end{proof}")
        p.preprocess_theorems(tmp_path)
        result = tex.read_text()
        assert "Proof." in result
        assert "square" in result

    def test_custom_newtheorem(self, tmp_path):
        tex = tmp_path / "paper.tex"
        tex.write_text(
            r"\newtheorem{mydef}{My Definition}" + "\n"
            r"\begin{mydef}Custom env.\end{mydef}"
        )
        p.preprocess_theorems(tmp_path)
        result = tex.read_text()
        assert "My Definition 1" in result


# ── End-to-end preprocessing (file-level) ──────────────────────────────────


class TestFilePreprocessing:
    def test_normalize_citations(self, tmp_path):
        tex = tmp_path / "paper.tex"
        tex.write_text(r"\citep{ref1} and \textcite{ref2}")
        p.normalize_citations(tmp_path)
        result = tex.read_text()
        assert r"\cite{ref1}" in result
        assert r"\cite{ref2}" in result

    def test_preprocess_hyperref(self, tmp_path):
        tex = tmp_path / "paper.tex"
        tex.write_text(r"\hyperref[sec]{Section} text")
        p.preprocess_hyperref(tmp_path)
        assert tex.read_text() == "Section text"

    def test_strip_resizebox(self, tmp_path):
        tex = tmp_path / "paper.tex"
        tex.write_text(r"\resizebox{\textwidth}{!}{TABLE}")
        p.strip_resizebox(tmp_path)
        assert tex.read_text() == "TABLE"

    def test_strip_adjustbox(self, tmp_path):
        tex = tmp_path / "paper.tex"
        tex.write_text(r"\adjustbox{width=5cm}{content here}")
        p.strip_adjustbox(tmp_path)
        assert tex.read_text() == "content here"

    def test_destar_floats(self, tmp_path):
        tex = tmp_path / "paper.tex"
        tex.write_text(r"\begin{figure*}img\end{figure*}")
        p.destar_floats(tmp_path)
        result = tex.read_text()
        assert r"\begin{figure}" in result
        assert r"\end{figure}" in result

    def test_replace_ding(self, tmp_path):
        tex = tmp_path / "paper.tex"
        tex.write_text(r"\ding{51}")
        p.replace_ding_commands(tmp_path)
        assert tex.read_text() == "✓"

    def test_replace_textcircled_math(self, tmp_path):
        tex = tmp_path / "paper.tex"
        tex.write_text(r"step $\textcircled{1}$ and $\textcircled{2}$")
        p.replace_textcircled(tmp_path)
        assert tex.read_text() == "step ① and ②"

    def test_replace_textcircled_bare(self, tmp_path):
        tex = tmp_path / "paper.tex"
        tex.write_text(r"\textcircled{3}")
        p.replace_textcircled(tmp_path)
        assert tex.read_text() == "③"

    def test_normalize_table_envs(self, tmp_path):
        tex = tmp_path / "paper.tex"
        tex.write_text(r"\begin{tabu}data\end{tabu}")
        p.normalize_table_envs(tmp_path)
        result = tex.read_text()
        assert r"\begin{tabular}" in result
        assert r"\end{tabular}" in result

    def test_unwrap_makecell(self, tmp_path):
        tex = tmp_path / "paper.tex"
        tex.write_text(r"\makecell{Line 1 \\ Line 2}")
        p.unwrap_makecell(tmp_path)
        assert tex.read_text() == "Line 1 Line 2"

    def test_unwrap_makecell_with_alignment(self, tmp_path):
        tex = tmp_path / "paper.tex"
        tex.write_text(r"\makecell[c]{A \\ B}")
        p.unwrap_makecell(tmp_path)
        assert tex.read_text() == "A B"

    def test_strip_minipage_in_tables(self, tmp_path):
        tex = tmp_path / "paper.tex"
        tex.write_text(
            r"\begin{tabular}{cc}"
            r"\begin{minipage}{2cm}\includegraphics{img}\end{minipage}"
            r" & text \end{tabular}"
        )
        p.strip_minipage_in_tables(tmp_path)
        result = tex.read_text()
        assert r"\begin{minipage}" not in result
        assert r"\includegraphics{img}" in result

    def test_normalize_code_listings(self, tmp_path):
        tex = tmp_path / "paper.tex"
        tex.write_text(r"\begin{minted}{python}code\end{minted}")
        p.normalize_code_listings(tmp_path)
        result = tex.read_text()
        assert r"\begin{verbatim}" in result
        assert r"\end{verbatim}" in result

    def test_convert_wrapfigure(self, tmp_path):
        tex = tmp_path / "paper.tex"
        tex.write_text(r"\begin{wrapfigure}{r}{0.5\textwidth}img\end{wrapfigure}")
        p.convert_wrapfigure(tmp_path)
        result = tex.read_text()
        assert r"\begin{figure}[H]" in result
        assert r"\end{figure}" in result

    def test_unwrap_subfigures(self, tmp_path):
        tex = tmp_path / "paper.tex"
        tex.write_text(r"\begin{subfigure}[b]{0.5\textwidth}img\end{subfigure}")
        p.unwrap_subfigures(tmp_path)
        result = tex.read_text()
        assert "subfigure" not in result
        assert "img" in result

    def test_strip_problematic_packages(self, tmp_path):
        tex = tmp_path / "paper.tex"
        tex.write_text(r"\usepackage{hyperref}" + "\n" + r"\usepackage{amsmath}")
        p.strip_problematic_packages(tmp_path)
        result = tex.read_text()
        assert "hyperref" not in result
        assert "amsmath" in result


class TestUnwrapMakecell:
    def test_basic(self):
        assert p._unwrap_makecell_content(r"\makecell{A \\ B}") == "A B"

    def test_with_alignment(self):
        assert p._unwrap_makecell_content(r"\makecell[c]{A \\ B}") == "A B"

    def test_star(self):
        assert p._unwrap_makecell_content(r"\makecell*{X \\ Y}") == "X Y"

    def test_nested_formatting(self):
        result = p._unwrap_makecell_content(r"\makecell{\textbf{Bold} \\ normal}")
        assert result == r"\textbf{Bold} normal"

    def test_three_lines(self):
        result = p._unwrap_makecell_content(r"\makecell{A \\ B \\ C}")
        assert result == "A B C"

    def test_backslash_with_optional_arg(self):
        result = p._unwrap_makecell_content(r"\makecell{A \\[2pt] B}")
        assert result == "A B"

    def test_no_makecell(self):
        text = r"plain text \\ more"
        assert p._unwrap_makecell_content(text) == text

    def test_preserves_surrounding(self):
        result = p._unwrap_makecell_content(r"before \makecell{A \\ B} after")
        assert result == "before A B after"


class TestStripMinipageInTables:
    def test_strips_inside_tabular(self):
        content = (
            r"\begin{tabular}{cc}"
            r"{col} "
            r"\begin{minipage}{2.5cm}"
            r"\includegraphics[width=\linewidth]{img.png}"
            r"\end{minipage}"
            r" & text"
            r"\end{tabular}"
        )
        result = p._strip_minipage_in_tables_content(content)
        assert r"\begin{minipage}" not in result
        assert r"\end{minipage}" not in result
        assert r"\includegraphics[width=\linewidth]{img.png}" in result

    def test_preserves_outside_tabular(self):
        content = (
            r"\begin{figure}"
            r"\begin{minipage}{0.5\textwidth}img\end{minipage}"
            r"\end{figure}"
        )
        result = p._strip_minipage_in_tables_content(content)
        assert r"\begin{minipage}" in result

    def test_minipage_with_optional_position(self):
        content = (
            r"\begin{tabular}{c}"
            r"{l} "
            r"\begin{minipage}[t]{2cm}content\end{minipage}"
            r"\end{tabular}"
        )
        result = p._strip_minipage_in_tables_content(content)
        assert r"\begin{minipage}" not in result
        assert "content" in result
