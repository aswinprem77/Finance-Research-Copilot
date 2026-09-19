"""
Tests for peer alignment.

The whole point of this module is that fiscal LABELS lie across a watchlist:
NVIDIA's Q2 FY2027 ended 2026-07-26 while Qualcomm's fiscal year ends in
September and Broadcom's in November. These tests are built on that real
calendar spread rather than on tidy calendar quarters.
"""
from datetime import date, timedelta

from src.analyst.peers import (
    MAX_PERIOD_END_OFFSET_DAYS,
    PEER_CONCEPTS,
    align_peer_period,
    build_peer_comparison,
)
from src.schema.financial_schema import (
    CompanyFinancials,
    FactSource,
    FinancialConcept,
    FinancialFact,
    FiscalPeriod,
)

SUBJECT_END = date(2026, 7, 26)  # NVIDIA Q2 FY2027


def _fact(cik, concept, value, fy, fp, end, source=FactSource.XBRL):
    return FinancialFact(company_cik=cik, concept=concept, value=value, fiscal_year=fy,
                         fiscal_period=fp, period_end_date=end, source=source,
                         source_tag=f"us-gaap:{concept.value}")


def _company(cik, name, ticker, rows):
    """rows: (concept, value, fy, fp, end)"""
    return CompanyFinancials(
        company_cik=cik, company_name=name, company_ticker=ticker,
        facts=[_fact(cik, *row) for row in rows],
    )


def _quarter(cik, name, ticker, end, fy, fp, revenue, net_income, gross_profit=None):
    rows = [(FinancialConcept.REVENUE, revenue, fy, fp, end),
            (FinancialConcept.NET_INCOME, net_income, fy, fp, end)]
    if gross_profit is not None:
        rows.append((FinancialConcept.GROSS_PROFIT, gross_profit, fy, fp, end))
    return _company(cik, name, ticker, rows)


SUBJECT = _quarter("0001045810", "NVIDIA", "NVDA", SUBJECT_END, 2027, FiscalPeriod.Q2,
                   96_221_000_000, 59_688_000_000, 67_000_000_000)


# --- alignment ---------------------------------------------------------------

def test_matches_a_peer_whose_fiscal_label_differs():
    # Broadcom's quarter ends 2026-08-02 and it calls that Q3 FY2026. The label
    # differs from the subject's Q2 FY2027; the dates are seven days apart.
    avgo = _quarter("0001730168", "Broadcom", "AVGO", date(2026, 8, 2), 2026, FiscalPeriod.Q3,
                    16_000_000_000, 4_000_000_000)
    aligned = align_peer_period(avgo, target_end=SUBJECT_END, target_period=FiscalPeriod.Q2)
    assert aligned == (2026, FiscalPeriod.Q3, date(2026, 8, 2))


def test_picks_the_nearest_period_not_the_first():
    amd = _company("0000002488", "AMD", "AMD", [
        (FinancialConcept.REVENUE, 1.0, 2026, FiscalPeriod.Q1, date(2026, 3, 28)),
        (FinancialConcept.REVENUE, 2.0, 2026, FiscalPeriod.Q2, date(2026, 6, 27)),
        (FinancialConcept.REVENUE, 3.0, 2026, FiscalPeriod.Q3, date(2026, 9, 26)),
    ])
    # 2026-06-27 is 29 days before the subject; 2026-09-26 is 62 days after.
    assert align_peer_period(amd, target_end=SUBJECT_END, target_period=FiscalPeriod.Q2) == \
        (2026, FiscalPeriod.Q2, date(2026, 6, 27))


def test_a_peer_outside_the_window_does_not_align():
    far = _quarter("0000999999", "Far", "FAR", date(2026, 5, 31), 2026, FiscalPeriod.Q2, 1.0, 1.0)
    assert align_peer_period(far, target_end=SUBJECT_END, target_period=FiscalPeriod.Q2) is None


def test_quarters_never_align_against_annual_periods():
    # A full year and a quarter are different durations; comparing them would
    # overstate the peer four-fold.
    annual = _company("0000888888", "Annual", "ANN", [
        (FinancialConcept.REVENUE, 400.0, 2026, FiscalPeriod.FY, date(2026, 7, 31)),
    ])
    assert align_peer_period(annual, target_end=SUBJECT_END, target_period=FiscalPeriod.Q2) is None
    assert align_peer_period(annual, target_end=SUBJECT_END, target_period=FiscalPeriod.FY) == \
        (2026, FiscalPeriod.FY, date(2026, 7, 31))


def test_a_period_without_any_requested_concept_is_not_a_candidate():
    # Otherwise an empty period wins the window and renders as an all-N/A row
    # while a usable period sits just outside it.
    peer = _company("0000777777", "Debt only", "DBT", [
        (FinancialConcept.TOTAL_DEBT, 5.0, 2026, FiscalPeriod.Q2, date(2026, 7, 25)),
    ])
    assert align_peer_period(peer, target_end=SUBJECT_END, target_period=FiscalPeriod.Q2) is None


def test_ties_resolve_to_the_earlier_period_deterministically():
    peer = _company("0000666666", "Tie", "TIE", [
        (FinancialConcept.REVENUE, 1.0, 2026, FiscalPeriod.Q2, SUBJECT_END - timedelta(days=10)),
        (FinancialConcept.REVENUE, 2.0, 2026, FiscalPeriod.Q3, SUBJECT_END + timedelta(days=10)),
    ])
    aligned = align_peer_period(peer, target_end=SUBJECT_END, target_period=FiscalPeriod.Q2)
    assert aligned[2] == SUBJECT_END - timedelta(days=10)


def test_window_edge_is_inclusive():
    inside = _quarter("0000555555", "Edge", "EDG", SUBJECT_END + timedelta(days=MAX_PERIOD_END_OFFSET_DAYS),
                      2026, FiscalPeriod.Q2, 1.0, 1.0)
    outside = _quarter("0000555556", "Past", "PST", SUBJECT_END + timedelta(days=MAX_PERIOD_END_OFFSET_DAYS + 1),
                       2026, FiscalPeriod.Q2, 1.0, 1.0)
    assert align_peer_period(inside, target_end=SUBJECT_END, target_period=FiscalPeriod.Q2) is not None
    assert align_peer_period(outside, target_end=SUBJECT_END, target_period=FiscalPeriod.Q2) is None


# --- table assembly ----------------------------------------------------------

def _watchlist_peers():
    return [
        _quarter("0001730168", "Broadcom", "AVGO", date(2026, 8, 2), 2026, FiscalPeriod.Q3,
                 16_000_000_000, 4_000_000_000, 10_000_000_000),
        _quarter("0000002488", "AMD", "AMD", date(2026, 6, 27), 2026, FiscalPeriod.Q2,
                 7_000_000_000, 1_000_000_000, 3_500_000_000),
        _quarter("0000804328", "QUALCOMM", "QCOM", date(2026, 6, 28), 2026, FiscalPeriod.Q3,
                 10_000_000_000, 2_500_000_000),
    ]


def test_rows_report_their_own_period_and_offset():
    comparison = build_peer_comparison(SUBJECT, _watchlist_peers(), fiscal_year=2027,
                                       fiscal_period=FiscalPeriod.Q2, period_end_date=SUBJECT_END)
    by_ticker = {r.company_ticker: r for r in comparison.peers}
    assert by_ticker["AVGO"].end_offset_days == 7
    assert by_ticker["AMD"].end_offset_days == -29
    assert by_ticker["QCOM"].period_end_date == date(2026, 6, 28)
    assert comparison.subject.end_offset_days == 0


def test_nearest_peer_is_listed_first():
    comparison = build_peer_comparison(SUBJECT, _watchlist_peers(), fiscal_year=2027,
                                       fiscal_period=FiscalPeriod.Q2, period_end_date=SUBJECT_END)
    assert [r.company_ticker for r in comparison.peers] == ["AVGO", "QCOM", "AMD"]


def test_margins_are_derived_from_the_same_aligned_period():
    comparison = build_peer_comparison(SUBJECT, _watchlist_peers(), fiscal_year=2027,
                                       fiscal_period=FiscalPeriod.Q2, period_end_date=SUBJECT_END)
    amd = next(r for r in comparison.peers if r.company_ticker == "AMD")
    assert round(amd.net_margin_pct, 2) == round(1_000_000_000 / 7_000_000_000 * 100, 2)
    assert round(amd.gross_margin_pct, 2) == 50.0


def test_a_peer_missing_gross_profit_reports_na_not_zero():
    comparison = build_peer_comparison(SUBJECT, _watchlist_peers(), fiscal_year=2027,
                                       fiscal_period=FiscalPeriod.Q2, period_end_date=SUBJECT_END)
    qcom = next(r for r in comparison.peers if r.company_ticker == "QCOM")
    assert qcom.gross_margin_pct is None
    assert qcom.metrics[FinancialConcept.GROSS_PROFIT].unavailable_reason


def test_an_unalignable_peer_is_kept_with_its_reason():
    peers = _watchlist_peers() + [
        _quarter("0000999999", "Offbeat", "OFF", date(2026, 5, 1), 2026, FiscalPeriod.Q2, 1.0, 1.0)
    ]
    comparison = build_peer_comparison(SUBJECT, peers, fiscal_year=2027,
                                       fiscal_period=FiscalPeriod.Q2, period_end_date=SUBJECT_END)
    off = next(r for r in comparison.peers if r.company_ticker == "OFF")
    assert "no quarterly period ending within" in off.unavailable_reason
    assert off not in comparison.aligned_peers
    assert len(comparison.peers) == 4  # kept, so the peer set does not look complete


def test_a_failed_fetch_is_reported_rather_than_dropped():
    empty = CompanyFinancials(company_cik="0000123456", company_name="Broken", company_ticker="BRK")
    comparison = build_peer_comparison(SUBJECT, _watchlist_peers() + [empty], fiscal_year=2027,
                                       fiscal_period=FiscalPeriod.Q2, period_end_date=SUBJECT_END,
                                       unavailable={"0000123456": "peer data unavailable (timeout)"})
    broken = next(r for r in comparison.peers if r.company_ticker == "BRK")
    assert broken.unavailable_reason == "peer data unavailable (timeout)"


def test_alignment_note_states_the_spread_and_names_wide_peers():
    comparison = build_peer_comparison(SUBJECT, _watchlist_peers(), fiscal_year=2027,
                                       fiscal_period=FiscalPeriod.Q2, period_end_date=SUBJECT_END)
    note = comparison.alignment_note()
    assert "aligned on period end date, not fiscal label" in note
    assert "3 of 3 peers aligned" in note
    assert comparison.widest_offset_days == 29


def test_alignment_note_says_so_when_nothing_aligned():
    far = [_quarter("0000999999", "Offbeat", "OFF", date(2026, 1, 1), 2026, FiscalPeriod.Q2, 1.0, 1.0)]
    comparison = build_peer_comparison(SUBJECT, far, fiscal_year=2027,
                                       fiscal_period=FiscalPeriod.Q2, period_end_date=SUBJECT_END)
    assert "No peer reported a comparable period" in comparison.alignment_note()
    assert comparison.aligned_peers == []


def test_gap_reason_names_the_peer_nearest_available_period():
    # The two ways a peer drops out look identical in a table but mean
    # different things: a different calendar, or a period not yet filed when
    # the subject filed. The reason has to carry that.
    off = _quarter("0000999999", "Offbeat", "OFF", date(2026, 5, 3), 2026, FiscalPeriod.Q2, 1.0, 1.0)
    comparison = build_peer_comparison(SUBJECT, [off], fiscal_year=2027,
                                       fiscal_period=FiscalPeriod.Q2, period_end_date=SUBJECT_END)
    reason = comparison.peers[0].unavailable_reason
    assert "nearest available ends 2026-05-03 (-84d)" in reason


def test_gap_reason_distinguishes_a_peer_with_no_periods_at_all():
    empty = CompanyFinancials(company_cik="0000111111", company_name="Empty", company_ticker="EMP")
    comparison = build_peer_comparison(SUBJECT, [empty], fiscal_year=2027,
                                       fiscal_period=FiscalPeriod.Q2, period_end_date=SUBJECT_END)
    assert "no comparable period reported at all" in comparison.peers[0].unavailable_reason
