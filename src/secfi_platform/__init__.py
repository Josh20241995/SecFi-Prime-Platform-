"""
SecFi Prime Platform
=====================
Institutional securities lending, financing, repo, and prime brokerage
decision-support and optimization platform.

This package is organized by business capability, not by technical layer,
so that ownership boundaries match how a real desk/risk/tech org would
divide the codebase:

    common/            shared types, enums, config, logging, math utils
    ingestion/          source connectors (EquiLend, DataLend, internal, custodian)
    normalization/      canonical schema mapping + data quality gates
    risk/               counterparty risk, capital/RWA, rates & FX risk
    optimization/       book optimization engine (MILP/LP)
    pricing/            market pricing intelligence
    recall_buyin/       recall/return/buy-in/supply risk engine
    reconciliation/     desk-vs-custodian-vs-firm break detection
    corporate_actions/  60-day forward corporate action impact engine
    growth/             counterparty growth/contraction opportunity engine
    explainability/     shared explanation + confidence scoring framework
    reporting/           daily/intraday output generation
    alerting/            threshold + event-driven alert generation
    orchestration/       batch + intraday job scheduling
    api/                 FastAPI service layer (desk-facing + system-facing)

See /docs/architecture.md for the full system design.
"""

__version__ = "1.0.0"
