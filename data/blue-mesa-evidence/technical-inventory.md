# Technical inventory

The synthetic payroll flow receives partner payroll instructions through REST APIs and scheduled encrypted files. Blue Mesa validates identity and schema, creates idempotent payment instructions, publishes status events, and reconciles outcomes against its existing ledger.

Technical unknowns include payroll provider API limits, file cut-off times, duplicate-file behavior, correction flows, identity federation, ownership of failed records, and reconciliation exception queues. The initial design should use existing AWS services when they satisfy the requirement and document why each service is selected.
