"""
Peer comparison with explicit fiscal-calendar alignment - PRD v2 Section 5
Stage 3, "vs. peer set", and the reason Section 6 asks for a single-sector
watchlist.

calculator.compute_peer_comparison() matches on `(fiscal_year,
fiscal_period)` labels. Across this watchlist that is wrong, and quietly so.
NVIDIA's Q2 FY2027 ended 2026-07-26; Qualcomm's fiscal year ends in late
September, Micron's in late August, Broadcom's in early November. Lining up
the label "Q2" across those companies compares periods that ended months
apart and presents the result as a peer comparison. So alignment here is by
actual period END DATE, the same discipline the XBRL parser already applies
to comparative periods.

Two facts about the parser make this tractable, and this module depends on
both:

- Every fact labelled Q1-Q4 is a DISCRETE quarter (75-105 days) and every
  FY fact is a full year (330-380 days); year-to-date observations are
  rejected upstream. So the fiscal period label already encodes duration,
  and a quarter can never be silently compared against a half-year.
- Facts carry the accession and filing date they came from, so a peer's
  figures can be restricted to what the peer had actually published when
  the subject filing was made.

MAX_PERIOD_END_OFFSET_DAYS is 45 rather than an arbitrary round number:
consecutive quarter ends are about 91 days apart, so a window wider than
half of that could admit two different quarters of the same peer and make
the choice ambiguous. At 45 days at most one candidate can fall inside, and
the nearest one is the only one.

Every row carries how far its period end sits from the subject's, so a
reader can see the alignment rather than trust it. A peer with no period
inside the window is reported with the reason and its nearest available
period, never dropped - a silently short peer set reads as a complete one.
That reason distinguishes the two ways a peer drops out, which look
identical in a table but mean different things: Broadcom's quarter ending
2026-08-02 sits only 7 days from NVIDIA's, but it was filed 2026-09-10, so
as of NVIDIA's 2026-08-26 filing nobody could have seen it; Micron simply
has no quarter near that date.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from src.schema.citations import fact_citation
from src.schema.financial_schema import (
    CompanyFinancials,
    FinancialConcept,
    FinancialFact,
    FiscalPeriod,
)

# See module docstring: half a quarter, so the nearest match is unambiguous.
MAX_PERIOD_END_OFFSET_DAYS = 45

# Beyond this the periods are still comparable but the gap is worth naming in
# the memo rather than leaving the reader to notice the dates.
WIDE_OFFSET_DAYS = 30

# Revenue and net income are the figures; the margins are what actually
# compares across companies of very different size.
PEER_CONCEPTS: tuple[FinancialConcept, ...] = (
    FinancialConcept.REVENUE,
    FinancialConcept.NET_INCOME,
    FinancialConcept.GROSS_PROFIT,
)


def _is_annual(period: FiscalPeriod) -> bool:
    return period == FiscalPeriod.FY


@dataclass(frozen=True)
class PeerMetric:
    concept: FinancialConcept
    value: float | None
    citation: str = ""
    provenance: str = "unknown"
    unavailable_reason: str | None = None


@dataclass(frozen=True)
class PeerRow:
    company_name: str
    company_cik: str
    company_ticker: str | None
    is_subject: bool = False
    period_end_date: date | None = None
    fiscal_year: int | None = None
    fiscal_period: FiscalPeriod | None = None
    end_offset_days: int | None = None
    metrics: dict[FinancialConcept, PeerMetric] = field(default_factory=dict)
    unavailable_reason: str | None = None

    def value(self, concept: FinancialConcept) -> float | None:
        metric = self.metrics.get(concept)
        return metric.value if metric else None

    def _ratio(self, numerator: FinancialConcept) -> float | None:
        top = self.value(numerator)
        revenue = self.value(FinancialConcept.REVENUE)
        if top is None or not revenue:
            return None
        return top / revenue * 100

    @property
    def net_margin_pct(self) -> float | None:
        return self._ratio(FinancialConcept.NET_INCOME)

    @property
    def gross_margin_pct(self) -> float | None:
        return self._ratio(FinancialConcept.GROSS_PROFIT)


@dataclass
class PeerComparison:
    subject: PeerRow
    peers: list[PeerRow]
    concepts: tuple[FinancialConcept, ...]
    period_end_date: date
    fiscal_year: int
    fiscal_period: FiscalPeriod
    max_offset_days: int = MAX_PERIOD_END_OFFSET_DAYS

    @property
    def aligned_peers(self) -> list[PeerRow]:
        return [p for p in self.peers if p.unavailable_reason is None]

    @property
    def widest_offset_days(self) -> int:
        offsets = [abs(p.end_offset_days) for p in self.aligned_peers if p.end_offset_days is not None]
        return max(offsets) if offsets else 0

    def alignment_note(self) -> str:
        """One sentence a reader can check the comparison against."""
        aligned = self.aligned_peers
        if not aligned:
            return ("No peer reported a comparable period within "
                    f"{self.max_offset_days} days of {self.period_end_date}, so no peer comparison is shown.")
        note = (f"Peers are aligned on period end date, not fiscal label: each row's period ends within "
                f"{self.max_offset_days} days of the subject's {self.period_end_date}. "
                f"{len(aligned)} of {len(self.peers)} peers aligned; widest gap {self.widest_offset_days} days.")
        wide = [p.company_ticker or p.company_cik for p in aligned
                if p.end_offset_days is not None and abs(p.end_offset_days) > WIDE_OFFSET_DAYS]
        if wide:
            note += (f" {', '.join(sorted(wide))} differ by more than {WIDE_OFFSET_DAYS} days, so those "
                     "figures cover materially different weeks of trading.")
        return note


def _candidate_periods(
    financials: CompanyFinancials, *, annual: bool
) -> dict[tuple[int, FiscalPeriod, date], list[FinancialFact]]:
    grouped: dict[tuple[int, FiscalPeriod, date], list[FinancialFact]] = {}
    for fact in financials.facts:
        if _is_annual(fact.fiscal_period) != annual:
            continue
        grouped.setdefault((fact.fiscal_year, fact.fiscal_period, fact.period_end_date), []).append(fact)
    return grouped


def nearest_available_period(
    peer: CompanyFinancials,
    *,
    target_end: date,
    target_period: FiscalPeriod,
    concepts: tuple[FinancialConcept, ...] = PEER_CONCEPTS,
) -> tuple[date, int] | None:
    """
    The peer's closest usable period end regardless of the window, and how far
    off it is. Used to explain a gap: "nearest available ends 2026-05-03
    (-84d)" tells a reader whether the peer simply has a different calendar or
    had not yet reported when this filing was made.
    """
    grouped = _candidate_periods(peer, annual=_is_annual(target_period))
    wanted = set(concepts)
    ends = [key[2] for key, facts in grouped.items() if any(f.concept in wanted for f in facts)]
    if not ends:
        return None
    nearest = min(ends, key=lambda end: (abs((end - target_end).days), end))
    return nearest, (nearest - target_end).days


def align_peer_period(
    peer: CompanyFinancials,
    *,
    target_end: date,
    target_period: FiscalPeriod,
    concepts: tuple[FinancialConcept, ...] = PEER_CONCEPTS,
    max_offset_days: int = MAX_PERIOD_END_OFFSET_DAYS,
) -> tuple[int, FiscalPeriod, date] | None:
    """
    The peer period of the same duration class whose end date is nearest the
    subject's, within the window. Ties break toward the earlier end date so
    the choice is deterministic.

    A period is only a candidate if it actually carries one of the requested
    concepts; a period present in the data but empty of them would otherwise
    win the window and then render as an all-N/A row.
    """
    grouped = _candidate_periods(peer, annual=_is_annual(target_period))
    wanted = set(concepts)
    candidates = [
        key for key, facts in grouped.items()
        if any(f.concept in wanted for f in facts)
        and abs((key[2] - target_end).days) <= max_offset_days
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda key: (abs((key[2] - target_end).days), key[2]))


def _metrics_for(
    financials: CompanyFinancials,
    *,
    fiscal_year: int,
    fiscal_period: FiscalPeriod,
    period_end_date: date,
    concepts: tuple[FinancialConcept, ...],
) -> dict[FinancialConcept, PeerMetric]:
    metrics: dict[FinancialConcept, PeerMetric] = {}
    for concept in concepts:
        fact = next(
            (f for f in financials.facts_for(concept)
             if f.fiscal_year == fiscal_year and f.fiscal_period == fiscal_period
             and f.period_end_date == period_end_date),
            None,
        )
        if fact is None:
            metrics[concept] = PeerMetric(concept, None, unavailable_reason="not reported for this period")
        else:
            metrics[concept] = PeerMetric(concept, fact.value, fact_citation(fact), fact.source.value)
    return metrics


def build_peer_comparison(
    subject: CompanyFinancials,
    peers: list[CompanyFinancials],
    *,
    fiscal_year: int,
    fiscal_period: FiscalPeriod,
    period_end_date: date,
    concepts: tuple[FinancialConcept, ...] = PEER_CONCEPTS,
    max_offset_days: int = MAX_PERIOD_END_OFFSET_DAYS,
    unavailable: dict[str, str] | None = None,
) -> PeerComparison:
    """
    Assemble the subject row plus one row per peer, aligned by period end.

    `unavailable` maps a peer CIK to why its data could not be retrieved at
    all, so a fetch failure appears in the table as a named gap rather than a
    missing company.
    """
    unavailable = unavailable or {}
    subject_row = PeerRow(
        company_name=subject.company_name or subject.company_cik,
        company_cik=subject.company_cik,
        company_ticker=subject.company_ticker,
        is_subject=True,
        period_end_date=period_end_date,
        fiscal_year=fiscal_year,
        fiscal_period=fiscal_period,
        end_offset_days=0,
        metrics=_metrics_for(subject, fiscal_year=fiscal_year, fiscal_period=fiscal_period,
                             period_end_date=period_end_date, concepts=concepts),
    )

    rows: list[PeerRow] = []
    for peer in peers:
        base = {
            "company_name": peer.company_name or peer.company_cik,
            "company_cik": peer.company_cik,
            "company_ticker": peer.company_ticker,
        }
        reason = unavailable.get(peer.company_cik)
        if reason:
            rows.append(PeerRow(**base, unavailable_reason=reason))
            continue
        aligned = align_peer_period(peer, target_end=period_end_date, target_period=fiscal_period,
                                    concepts=concepts, max_offset_days=max_offset_days)
        if aligned is None:
            reason = (f"no {'annual' if _is_annual(fiscal_period) else 'quarterly'} period ending within "
                      f"{max_offset_days} days of {period_end_date}")
            nearest = nearest_available_period(peer, target_end=period_end_date,
                                               target_period=fiscal_period, concepts=concepts)
            if nearest is not None:
                reason += f"; nearest available ends {nearest[0]} ({nearest[1]:+d}d)"
            else:
                reason += "; no comparable period reported at all"
            rows.append(PeerRow(**base, unavailable_reason=reason))
            continue
        peer_fy, peer_fp, peer_end = aligned
        rows.append(PeerRow(
            **base,
            period_end_date=peer_end,
            fiscal_year=peer_fy,
            fiscal_period=peer_fp,
            end_offset_days=(peer_end - period_end_date).days,
            metrics=_metrics_for(peer, fiscal_year=peer_fy, fiscal_period=peer_fp,
                                 period_end_date=peer_end, concepts=concepts),
        ))

    # Deterministic order: aligned peers nearest-first, then the ones that
    # could not be aligned, alphabetically so reruns match.
    rows.sort(key=lambda r: (r.unavailable_reason is not None,
                             abs(r.end_offset_days) if r.end_offset_days is not None else 0,
                             r.company_ticker or r.company_cik))
    return PeerComparison(subject=subject_row, peers=rows, concepts=concepts,
                          period_end_date=period_end_date, fiscal_year=fiscal_year,
                          fiscal_period=fiscal_period, max_offset_days=max_offset_days)
