"""Human-readable fact provenance, including a link to its SEC filing."""
from src.schema.financial_schema import FinancialFact


def fact_citation(fact: FinancialFact) -> str:
    text = f"{fact.source_tag or fact.concept.value}; period ending {fact.period_end_date}; {fact.unit}"
    if fact.accession_number:
        accession = fact.accession_number
        url = f"https://www.sec.gov/Archives/edgar/data/{int(fact.company_cik)}/{accession.replace('-', '')}/{accession}-index.html"
        text += f"; [{accession}]({url})"
    return text
