"""
Claim-extractor eval. The extractor reads a source and emits atomic claims with verbatim quotes.

Precision is graded by the verifier's own rules (quote verbatim in source, value in quote), so
it needs no labels. Recall is graded against EXPECTED facts: (metric options, year, value).
FORBIDDEN quotes must never be extracted as facts: they are analyst questions, not company facts.
"""

SOURCES = {
    "10k": {"file": "acme_10k_fy2025.txt", "kind": "10-K", "published_at": "2026-02-20",
            "uri": "sec://ACME/10-K/FY2025"},
    "q4call": {"file": "acme_q4_fy2025_call.txt", "kind": "transcript", "published_at": "2026-02-10",
               "uri": "call://ACME/Q4FY2025"},
}


def f(metrics, year, value):
    return {"metrics": set(metrics), "year": year, "value": value}


EXPECTED = {
    "10k": [
        f(["revenue"], "2025", 1000.0),
        f(["revenue"], "2024", 959.7),
        f(["organic_growth"], "2025", 0.031),
        f(["revenue"], "2025", 620.0) | {"segment": True},
        f(["gross_margin"], "2025", 0.300),
        f(["gross_margin"], "2024", 0.296),
        f(["opex_pct_sales"], "2025", 0.200),
        f(["ebit"], "2025", 100.0),
        f(["ebit_margin"], "2025", 0.100),
        f(["capex"], "2025", 20.0),
        f(["d_and_a"], "2025", 19.5),
        f(["tax_rate"], "2025", 0.25),
        f(["dio"], "2025", 60.0),
        f(["guidance_revenue"], "2026", 1040.0),
    ],
    "q4call": [
        f(["revenue"], "2025", 1000.0),
        f(["pricing"], "2025", 0.03),
        f(["customer_retention"], "2025", 0.94),
        f(["ebit_margin"], "2025", 0.104),
    ],
}

FORBIDDEN = {
    "q4call": ["Are you seeing pricing of 5%"],
}
