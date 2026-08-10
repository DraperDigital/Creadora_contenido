"""Unit tests for ``shared/cut/surgical_patcher.py``.

Exercises the mapper-log Issue-inventory parser, the deletion-only ``patch``
application (context-anchored span location, right-to-left splicing,
not-found reporting) and the conservative grammar cleanup. No run dirs, no
subprocess.
"""
from contenido_bionico.shared.cut.surgical_patcher import (
    Issue,
    _cleanup_artifacts,
    parse_issue_inventory,
    patch,
)


def _issue(num, preview, before, after, start=0, end=0, length=1):
    return Issue(
        num=num,
        final_idx_start=start,
        final_idx_end=end,
        length=length,
        raw_preview=preview,
        normalized=preview.lower(),
        context_before=before,
        context_after=after,
    )


# ---------- parse_issue_inventory ----------

def test_parse_issue_inventory_reads_all_fields(tmp_path):
    log = tmp_path / "mapper_error.log"
    log.write_text(
        "\n".join(
            [
                "=== Mapper error ===",
                "Issue 1: final tokens [10..11], 2 token(s)",
                "  raw preview: de Atlassian,",
                "  normalized: de atlassian",
                "  context before: el equipo de",
                "  context after: y trabajamos",
                "Issue 2: final tokens [40..40], 1 token(s)",
                "  raw preview: extra",
                "  normalized: extra",
                "  context before: una palabra",
                "  context after: al final",
            ]
        ),
        encoding="utf-8",
    )
    issues = parse_issue_inventory(log)
    assert [i.num for i in issues] == [1, 2]
    first = issues[0]
    assert (first.final_idx_start, first.final_idx_end, first.length) == (10, 11, 2)
    assert first.raw_preview == "de Atlassian,"
    assert first.context_before == "el equipo de"
    assert first.context_after == "y trabajamos"


def test_parse_issue_inventory_missing_file_returns_empty(tmp_path):
    assert parse_issue_inventory(tmp_path / "nope.log") == []


def test_parse_issue_inventory_skips_unparsable_lines(tmp_path):
    log = tmp_path / "mapper_error.log"
    log.write_text("some prose\nnot an issue header\n", encoding="utf-8")
    assert parse_issue_inventory(log) == []


# ---------- patch ----------

def test_patch_deletes_located_phrase():
    text = "Hola somos el equipo de Atlassian, y trabajamos duro cada dia"
    issue = _issue(1, "de Atlassian,", "somos el equipo", "y trabajamos")
    new_text, rows = patch(text, [issue])
    assert rows == [
        {
            "issue": 1,
            "raw_preview": "de Atlassian,",
            "outcome": "applied",
            "removed_bytes": len("de Atlassian,"),
        }
    ]
    assert "Atlassian" not in new_text
    assert new_text.startswith("Hola somos el equipo")
    assert "y trabajamos duro cada dia" in new_text


def test_patch_reports_not_found_and_keeps_text():
    text = "Hola somos el equipo y trabajamos duro"
    issue = _issue(1, "de Atlassian,", "somos el equipo", "y trabajamos")
    new_text, rows = patch(text, [issue])
    assert rows[0]["outcome"] == "not_found"
    assert rows[0]["removed_bytes"] == 0
    assert new_text == text


def test_patch_applies_multiple_issues_right_to_left():
    text = "uno dos MALO tres cuatro cinco PEOR seis siete"
    issues = [
        _issue(1, "MALO", "uno dos", "tres cuatro"),
        _issue(2, "PEOR", "cuatro cinco", "seis siete"),
    ]
    new_text, rows = patch(text, issues)
    assert [r["outcome"] for r in rows] == ["applied", "applied"]
    assert "MALO" not in new_text and "PEOR" not in new_text
    # All surviving words remain, in order.
    assert new_text.split() == ["uno", "dos", "tres", "cuatro", "cinco", "seis", "siete"]


def test_patch_matches_despite_accent_and_case_differences():
    # The log records normalized contexts; final.txt has the accented forms.
    text = "Aquí está la razón EXTRA de todo esto"
    issue = _issue(1, "EXTRA", "aqui esta la razon", "de todo")
    new_text, rows = patch(text, [issue])
    assert rows[0]["outcome"] == "applied"
    assert "EXTRA" not in new_text


def test_patch_ambiguous_phrase_without_context_is_not_found():
    # Preview occurs twice and there is no disambiguating context: skip.
    text = "gracias gracias por ver gracias gracias por compartir"
    issue = _issue(1, "gracias gracias", "", "")
    new_text, rows = patch(text, [issue])
    assert rows[0]["outcome"] == "not_found"
    assert new_text == text


# ---------- _cleanup_artifacts ----------

def test_cleanup_collapses_duplicate_function_words():
    assert _cleanup_artifacts("vamos a a ver") == "vamos a ver"
    assert _cleanup_artifacts("el el problema") == "el problema"
    # Real rhetorical repeats of longer words are preserved.
    assert _cleanup_artifacts("muy muy bien") == "muy muy bien"


def test_cleanup_collapses_punctuation_noise():
    assert _cleanup_artifacts("hola, , mundo") == "hola, mundo"
    assert _cleanup_artifacts("fin.  . Ya") == "fin. Ya"
    assert _cleanup_artifacts("doble  espacio") == "doble espacio"


def test_cleanup_strips_orphan_punctuation_at_sentence_start():
    assert _cleanup_artifacts("Listo. , ahora si") == "Listo. ahora si"
