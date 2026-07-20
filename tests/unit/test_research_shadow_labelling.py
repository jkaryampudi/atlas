"""P0.1 (ADR-0018) non-authoritative labelling — the surfaces that don't need a
DB: the shared validation_label, the monthly-report caveat (reports/exports must
never present shadow results as validated), and the console badge markup."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from atlas.dcp.portfolio.attribution import Attribution, ShortfallLine
from atlas.dcp.reporting.attribution import (
    MonthlyAttribution,
    SleeveMonth,
    render_monthly,
)
from atlas.dcp.strategy_lifecycle import validation_label

ROOT = Path(__file__).resolve().parents[2]


def _monthly() -> MonthlyAttribution:
    return MonthlyAttribution(
        period="2026-07",
        sleeves=(
            SleeveMonth("xsmom", 2, Decimal("2.84"), Decimal("0.00"),
                        Decimal("2.84"), Decimal("234.74"), Decimal("8505.00")),
            SleeveMonth("total", 2, Decimal("0.23"), Decimal("0.00"),
                        Decimal("0.23"), Decimal("234.74"), Decimal("101734.74")),
        ),
        nav_change_aud=Decimal("234.74"), satellite_alpha_pp=Decimal("2.84"),
        headline=("The active satellite added 2.84 pp vs simply holding the "
                  "index (SPY total return), cumulative since inception."))


def _shortfall() -> Attribution:
    z = ShortfallLine(fills=0, qty=0, avg_bps=None, cost_aud=Decimal("0"))
    return Attribution(period="2026-07", trades_buy=0, trades_sell=0,
                       entry_shortfall=z, exit_shortfall=z,
                       realised_pnl_aud=Decimal("0"), lots_closed=0,
                       nav_start_aud=None, nav_end_aud=None,
                       unrealised_swing_aud=None, llm_spend_usd=Decimal("0"))


def test_validation_label_is_shared_and_correct():
    assert validation_label("paper") == {"authoritative": True,
                                          "validation_status": "validated"}
    assert validation_label("live") == {"authoritative": True,
                                         "validation_status": "validated"}
    assert validation_label("research_shadow") == {
        "authoritative": False, "validation_status": "research_shadow"}


def test_render_monthly_default_has_no_shadow_caveat():
    body = render_monthly(_monthly(), _shortfall())
    assert "RESEARCH SHADOW" not in body
    assert "| xsmom (SPY TR) | 2 | +2.84% |" in body     # untagged row


def test_render_monthly_caveats_research_shadow_without_changing_figures():
    body = render_monthly(_monthly(), _shortfall(),
                          non_authoritative_sleeves={"xsmom"})
    assert "RESEARCH SHADOW — NOT VALIDATED (ADR-0018)" in body      # prominent
    assert "must NOT be read or exported as validated" in body
    assert "| xsmom (SPY TR) — RESEARCH SHADOW / NOT VALIDATED |" in body  # tag
    assert "+2.84%" in body and "234.74" in body         # figures unchanged


def test_monthly_report_separates_authoritative_and_shadow_sections():
    """Test 12: the report shows the AUTHORITATIVE satellite alpha (headline) and
    a SEPARATE 'Research shadow — NOT VALIDATED' section — never fused."""
    m = MonthlyAttribution(
        period="2026-07",
        sleeves=(SleeveMonth("xsmom", 2, Decimal("2.84"), Decimal("0.00"),
                             Decimal("2.84"), Decimal("234.74"), Decimal("8505.00")),
                 SleeveMonth("total", 2, Decimal("0.23"), Decimal("0.00"),
                             Decimal("0.23"), Decimal("234.74"), Decimal("101734.74"))),
        nav_change_aud=Decimal("234.74"), satellite_alpha_pp=Decimal("2.84"),
        headline="The active satellite added 2.84 pp vs simply holding the index.",
        research_shadow_alpha_pp=Decimal("-5.00"))
    body = render_monthly(m, _shortfall(), non_authoritative_sleeves={"xsmom"})
    # the authoritative headline is present as its own section
    assert "## The active satellite added 2.84 pp" in body
    # the shadow section is a DISTINCT, labelled block carrying the shadow number
    assert "## Research shadow — NOT VALIDATED (ADR-0018)" in body
    assert "Research-shadow satellite alpha: -5.00 pp" in body
    # the authoritative headline number is not the shadow number
    assert "2.84" in body.split("## Research shadow")[0]


def test_exported_report_never_labels_shadow_as_validated():
    """Test 13: an exported report must never present a shadow figure as
    validated — the shadow section is explicitly NON-authoritative and the row
    is tagged NOT VALIDATED."""
    m = MonthlyAttribution(
        period="2026-07",
        sleeves=(SleeveMonth("xsmom", 2, Decimal("9.99"), Decimal("0.00"),
                             Decimal("9.99"), Decimal("100.00"), Decimal("500.00")),
                 SleeveMonth("total", 2, Decimal("0.10"), Decimal("0.00"),
                             Decimal("0.10"), Decimal("100.00"), Decimal("1000.00"))),
        nav_change_aud=Decimal("100.00"), satellite_alpha_pp=None,   # no authoritative
        headline="The active satellite has no measurable history yet.",
        research_shadow_alpha_pp=Decimal("9.99"))
    body = render_monthly(m, _shortfall(), non_authoritative_sleeves={"xsmom"})
    # the shadow sleeve's strong return is tagged, never presented as validated
    assert "| xsmom (SPY TR) — RESEARCH SHADOW / NOT VALIDATED |" in body
    assert "must NOT be read or exported as validated" in body
    # the authoritative headline claims NO measurable history (shadow excluded)
    assert "no measurable history" in body


def test_console_html_renders_not_validated_badges():
    html = (ROOT / "atlas" / "dashboard" / "console.html").read_text()
    # STRATEGY card per-row badge, keyed on the API authoritative field
    assert "RESEARCH SHADOW — NOT VALIDATED" in html
    assert "s.authoritative===false" in html
    # sleeve attribution: the per-row shadow marker + the alpha-headline caveat
    assert "SHADOW · NOT VALIDATED" in html
    assert "d.satellite_alpha_authoritative===false" in html
