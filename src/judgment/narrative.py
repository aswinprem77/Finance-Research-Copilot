"""
Narrative change detection - the half of PRD v2 Stage 4 that keyword
screening alone cannot satisfy.

Section 5 Stage 4 asks for "New litigation or regulatory language detected"
and "Debt covenant or liquidity language changes". Both are claims about a
CHANGE. Screening the current filing for keywords cannot support either: a
risk factor that has been copied forward unchanged for eight quarters
matches every keyword and tells an analyst nothing. That is the noise the
PRD's own risk table warns about ("Judgment agent flags too much noise").

So this module compares the current filing's screened passages against the
same company's previous filing and sorts each one into:

- `new`         little or none of it appears in the prior filing
- `revised`     most sentences appear in the prior filing, some do not;
                the flag quotes the ones that do not
- `unchanged`   every sentence already appears in the prior filing (after
                rolling dates forward); routine, and labeled routine
- `unestablished`
                no prior filing is on record, so novelty CANNOT be claimed
                either way. This is deliberately not "new".

Two decisions worth stating, because both could reasonably have gone the
other way:

1. Similarity is lexical (sentence-level difflib), not embedding cosine,
   even though this project now has a perfectly good bi-encoder. Filing prose
   is literally copy-pasted forward and then edited, so a sequence matcher is
   the right tool for it, and unlike a cosine score it yields the actual
   sentences that changed, to put in the citation. It also keeps the judgment
   path independent of the retrieval stack, so changing an embedding model
   cannot silently change what gets flagged. The cost: a passage genuinely
   reworded to mean the same thing reads as `new`. That is a documented
   false positive.

2. Dates are normalized away before comparing; other numbers are not. Every
   quarterly filing rolls its period references forward mechanically, and
   treating that as a language change would flag the entire risk-factors
   section every quarter. A covenant ratio moving from 3.5x to 4.0x is a
   different matter and stays significant.

Screening runs over every prose chunk in the filing, not over retrieved
passages. Retrieval decides what a memo shows; it must not decide what gets
screened, or a litigation disclosure outside the top-k would be invisible.

THRESHOLDS HERE ARE UNCALIBRATED DEFAULTS. No labeled sample of real
filing-to-filing edits has been measured against them yet, so the
false-positive and false-negative rates are unknown. See PROGRESS.md.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from datetime import date

from src.judgment.rubric import LITIGATION_KEYWORDS, Flag
from src.retrieval.chunking import Chunk

NARRATIVE_VERSION = "v1"

# A 10-Q's risk factors are the successor of the previous 10-Q's or 10-K's,
# not of an 8-K filed in between. An 8-K is a short event filing, so
# comparing a full periodic report against one finds almost nothing in
# common and reports the entire filing as new. Baselines are therefore
# matched within a class: periodic against periodic, event against event.
PERIODIC_FORMS = frozenset({"10-K", "10-Q"})


def form_class(form: str) -> str:
    """Which filings are comparable with which. Amendments share their base form's class."""
    return "periodic" if form.replace("/A", "").strip() in PERIODIC_FORMS else "event"

# Keyword sets, one per screened topic. Same "narrow and grow it" philosophy
# as LITIGATION_KEYWORDS in rubric.py: a miss is a documented gap, a false
# flag from an overly broad term erodes trust in every other flag.
LIQUIDITY_REGULATORY_KEYWORDS = [
    "covenant", "liquidity", "going concern", "default",
    "regulatory investigation", "material weakness",
]

SCREEN_TOPICS: dict[str, list[str]] = {
    "LITIGATION": LITIGATION_KEYWORDS,
    "LIQUIDITY_REGULATORY": LIQUIDITY_REGULATORY_KEYWORDS,
}

# Comparison is per sentence, not per passage. Chunk boundaries follow
# document structure and a size budget, so a single edit upstream shifts every
# boundary after it; whole-passage similarity then reads unchanged boilerplate
# as new. Measured on two consecutive NVIDIA 10-Qs, whole-passage matching
# classified all 16 screened passages as new or revised and none as unchanged,
# including standard covenant and liquidity boilerplate.
#
# So a passage is scored by COVERAGE: the fraction of its sentences that also
# appear in the prior filing. That is stable under re-chunking, and the
# sentences that do not appear are the actual change to cite.
SENTENCE_MATCH_SIMILARITY = 0.90
UNCHANGED_COVERAGE = 0.99
REVISED_COVERAGE = 0.50

# Short sentences ("Refer to Note 11.") match almost anything, so they are not
# evidence either way and are left out of the coverage fraction.
MIN_SENTENCE_WORDS = 6

# Cap per topic so a first filing, which has no baseline and therefore
# cannot resolve anything, does not bury a memo in unestablished flags.
MAX_FLAGS_PER_TOPIC = 12

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")

_MONTHS = (r"january|february|march|april|may|june|july|august|september|october|november|december")
_DATE_PATTERNS = (
    re.compile(rf"\b(?:{_MONTHS})\s+\d{{1,2}},?\s+\d{{4}}\b", re.IGNORECASE),
    re.compile(rf"\b\d{{1,2}}\s+(?:{_MONTHS})\s+\d{{4}}\b", re.IGNORECASE),
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
    re.compile(rf"\b(?:{_MONTHS})\s+\d{{4}}\b", re.IGNORECASE),
    re.compile(r"\bfiscal\s+(?:year\s+)?\d{4}\b", re.IGNORECASE),
)


def normalize_for_comparison(text: str) -> str:
    """
    Reduce a passage to what a filing-to-filing comparison should treat as
    its substance: case and whitespace folded, period references replaced by
    a placeholder. Monetary amounts, ratios and counts survive untouched -
    those are the numbers a change in them would matter for.
    """
    reduced = text
    for pattern in _DATE_PATTERNS:
        reduced = pattern.sub(" <date> ", reduced)
    return " ".join(reduced.lower().split())


def _words(text: str) -> list[str]:
    return normalize_for_comparison(text).split()


def split_sentences(text: str) -> list[str]:
    """Normalized sentences of a passage, in order. Over-splitting on
    abbreviations is harmless: both sides split the same way."""
    return [part for part in (p.strip() for p in _SENTENCE_SPLIT.split(normalize_for_comparison(text))) if part]


def _comparable(sentences: list[str]) -> list[str]:
    return [s for s in sentences if len(s.split()) >= MIN_SENTENCE_WORDS]


@dataclass(frozen=True)
class NarrativePassage:
    chunk_id: str
    section: str
    text: str
    topics: tuple[str, ...]

    def to_dict(self) -> dict:
        return {"chunk_id": self.chunk_id, "section": self.section,
                "text": self.text, "topics": list(self.topics)}

    @staticmethod
    def from_dict(value: dict) -> "NarrativePassage":
        return NarrativePassage(
            chunk_id=str(value["chunk_id"]), section=str(value["section"]),
            text=str(value["text"]), topics=tuple(value.get("topics", [])),
        )


@dataclass
class FilingNarrative:
    """The screened narrative of one filing, as persisted for the next one to compare against."""

    company_cik: str
    accession_number: str
    form: str
    filing_date: date
    report_date: date | None = None
    passages: list[NarrativePassage] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "version": NARRATIVE_VERSION,
            "company_cik": self.company_cik,
            "accession_number": self.accession_number,
            "form": self.form,
            "filing_date": self.filing_date.isoformat(),
            "report_date": self.report_date.isoformat() if self.report_date else None,
            "passages": [p.to_dict() for p in self.passages],
        }

    @staticmethod
    def from_dict(value: dict) -> "FilingNarrative":
        version = value.get("version")
        if version != NARRATIVE_VERSION:
            raise ValueError(
                f"Unsupported narrative record version {version!r}; expected {NARRATIVE_VERSION!r}"
            )
        report_date = value.get("report_date")
        return FilingNarrative(
            company_cik=str(value["company_cik"]),
            accession_number=str(value["accession_number"]),
            form=str(value["form"]),
            filing_date=date.fromisoformat(value["filing_date"]),
            report_date=date.fromisoformat(report_date) if report_date else None,
            passages=[NarrativePassage.from_dict(p) for p in value.get("passages", [])],
        )


@dataclass(frozen=True)
class PassageChange:
    passage: NarrativePassage
    status: str  # "new" | "revised" | "unchanged" | "unestablished"
    similarity: float | None
    prior_chunk_id: str | None
    diff_summary: str | None


def screen_filing_narrative(
    chunks: list[Chunk],
    *,
    company_cik: str,
    accession_number: str,
    form: str,
    filing_date: date,
    report_date: date | None = None,
    topics: dict[str, list[str]] | None = None,
) -> FilingNarrative:
    """
    Collect every prose chunk carrying a screened keyword. Tables are never
    screened: a keyword in a flattened table rendering is a column label, not
    a disclosure.
    """
    active = SCREEN_TOPICS if topics is None else topics
    passages = []
    for chunk in chunks:
        if chunk.kind != "prose":
            continue
        lowered = chunk.text.lower()
        matched = tuple(sorted(topic for topic, words in active.items()
                               if any(word in lowered for word in words)))
        if matched:
            passages.append(NarrativePassage(chunk.chunk_id, chunk.section, chunk.text, matched))
    return FilingNarrative(company_cik=company_cik, accession_number=accession_number,
                           form=form, filing_date=filing_date, report_date=report_date,
                           passages=passages)


def _best_sentence_match(sentence: str, prior_sentences: dict[str, str]) -> tuple[str | None, float]:
    """
    Closest prior sentence and its ratio. Keys are opaque ids mapping back to
    a prior passage.

    Stops at the first sentence that clears SENTENCE_MATCH_SIMILARITY: the
    caller only needs to know whether the sentence is present in the prior
    filing and where, not which of several near-identical repeats scored
    highest. Iteration order is the prior filing's own passage order, so the
    result stays deterministic.
    """
    best_key, best_ratio = None, 0.0
    for key, candidate in prior_sentences.items():
        matcher = difflib.SequenceMatcher(None, candidate, sentence, autojunk=False)
        if matcher.real_quick_ratio() <= best_ratio or matcher.quick_ratio() <= best_ratio:
            continue
        ratio = matcher.ratio()
        if ratio > best_ratio:
            best_key, best_ratio = key, ratio
        if best_ratio >= SENTENCE_MATCH_SIMILARITY:
            break
    return best_key, best_ratio


def _diff_summary(unmatched: list[str], limit: int = 3) -> str:
    """Quote the sentences that are not in the prior filing, so a REVISED flag
    cites the change instead of merely asserting one."""
    shown = [s for s in unmatched if s][:limit]
    if not shown:
        return "wording changed without a sentence-level difference"
    more = f" (+{len(unmatched) - len(shown)} more)" if len(unmatched) > len(shown) else ""
    return "not in the prior filing: " + "; ".join(f'"{s[:200]}"' for s in shown) + more


def compare_narrative(current: FilingNarrative, prior: FilingNarrative | None) -> list[PassageChange]:
    """
    Classify each current passage against the prior filing's passages by what
    fraction of its sentences already appear there.

    With no prior filing every passage is `unestablished`, never `new` -
    absence of a baseline is not evidence of novelty, and claiming otherwise
    would make a company's first processed filing look like a crisis.
    """
    if prior is None or not prior.passages:
        return [PassageChange(p, "unestablished", None, None, None) for p in current.passages]

    # Every sentence in the prior filing's screened passages, flattened, so a
    # match is found wherever the text moved to.
    prior_sentences: dict[str, str] = {}
    for passage in prior.passages:
        for i, sentence in enumerate(_comparable(split_sentences(passage.text))):
            prior_sentences[f"{passage.chunk_id}#{i}"] = sentence

    changes = []
    for passage in current.passages:
        sentences = _comparable(split_sentences(passage.text))
        if not sentences:
            # Nothing long enough to judge; fall back to the whole passage.
            sentences = [normalize_for_comparison(passage.text)]

        matched, unmatched, sources = 0, [], []
        for sentence in sentences:
            key, ratio = _best_sentence_match(sentence, prior_sentences)
            if ratio >= SENTENCE_MATCH_SIMILARITY:
                matched += 1
                if key:
                    sources.append(key.split("#", 1)[0])
            else:
                unmatched.append(sentence)

        coverage = matched / len(sentences)
        # Attribute to the prior passage that supplied the most matches.
        prior_chunk_id = max(set(sources), key=sources.count) if sources else None

        if coverage >= UNCHANGED_COVERAGE:
            status, summary = "unchanged", None
        elif coverage >= REVISED_COVERAGE:
            status, summary = "revised", _diff_summary(unmatched)
        else:
            status, summary, prior_chunk_id = "new", None, None
        changes.append(PassageChange(passage, status, round(coverage, 4), prior_chunk_id, summary))
    return changes


_STATUS_RULES = {
    "new": ("NEW_{topic}_LANGUAGE", "notable",
            "Passage has no close counterpart in the prior filing"),
    "revised": ("REVISED_{topic}_LANGUAGE", "notable",
                "Passage is a reworded version of prior-filing language"),
    "unchanged": ("UNCHANGED_{topic}_LANGUAGE", "routine",
                  "Passage repeats prior-filing language after rolling dates forward"),
    "unestablished": ("UNESTABLISHED_{topic}_LANGUAGE", "notable",
                      "Passage contains screened language; no prior filing on record to compare against"),
}

# Most-to-least interesting, so the per-topic cap drops routine repeats first.
_STATUS_ORDER = {"new": 0, "revised": 1, "unestablished": 2, "unchanged": 3}


def flag_narrative_changes(
    changes: list[PassageChange],
    prior: FilingNarrative | None,
    *,
    max_per_topic: int = MAX_FLAGS_PER_TOPIC,
    relevance_order: list[str] | None = None,
) -> list[Flag]:
    """
    Turn classified passages into rubric flags. Every flag names the rule,
    the passage, the similarity that produced the verdict, and the prior
    filing it was compared against - or says explicitly that there was none.

    `relevance_order` is Path B's ranking of the screened passages, used
    only to break ties within a status and therefore to decide which
    passages survive `max_per_topic`. Eligibility is never retrieval's
    call - a passage is screened because it carries a keyword, full stop -
    so a change of retrieval stack can reorder a capped list but can never
    hide a passage the rules would otherwise flag.
    """
    rank = {chunk_id: i for i, chunk_id in enumerate(relevance_order or [])}
    baseline = (f"compared against {prior.accession_number} filed {prior.filing_date}"
                if prior is not None and prior.passages else "no prior filing on record")

    ordered = sorted(
        changes,
        key=lambda c: (_STATUS_ORDER.get(c.status, 9),
                       rank.get(c.passage.chunk_id, len(rank)),
                       -(c.similarity or 0.0),
                       c.passage.chunk_id),
    )
    counts: dict[str, int] = {}
    flags: list[Flag] = []
    for change in ordered:
        for topic in change.passage.topics:
            if counts.get(topic, 0) >= max_per_topic:
                continue
            counts[topic] = counts.get(topic, 0) + 1
            template, severity, description = _STATUS_RULES[change.status]
            similarity = "none" if change.similarity is None else f"{change.similarity:.2f}"
            against = (f"; closest prior passage {change.prior_chunk_id}"
                       if change.prior_chunk_id else "")
            detail = change.passage.text[:400]
            if change.diff_summary:
                detail = f"{detail}\n\nChange: {change.diff_summary}"
            flags.append(Flag(
                rule_id=template.format(topic=topic),
                rule_description=f"{description} (similarity {similarity}, {baseline})",
                severity=severity,
                citation=f"{change.passage.section} ({change.passage.chunk_id}){against}",
                detail=detail,
            ))
    return flags
