# Methodology, Ethics, and Privacy

## Study goals

The platform supports longitudinal and cross-sectional analyses of:

- Conversation dynamics
- Engagement intensity
- Interaction network topology
- Topic and sentiment drift over time
- Cross-server replication fidelity based on direct source message transfer

## Data governance model

1. **Server opt-in required** before ingestion is accepted.
2. **User privacy controls** allow participant-level exclusion.
3. **Anonymized identifiers** prevent direct attribution.
4. **Redacted message excerpts** reduce exposure of sensitive free text.
5. **Retention settings** support GDPR/CCPA-aligned data lifecycle workflows.
6. **Replication safeguards** require preconfigured source/target server records and explicit channel mappings.
7. **Token minimization** stores masked token previews in APIs and excludes token values from activity logs.
8. **Channel mapping controls** require explicit source-to-target mapping to constrain replication scope.
9. **Queue and failure tracking** are retained for auditability and transparent recovery workflows.

## Recommended disclosure for publications

- Discord API usage scope and bot permissions
- Consent and opt-out procedures
- Anonymization and retention methods
- NLP model and prompt design (OpenRouter model name/version)
- Known limitations and bias controls
- Reproducibility notes for aggregate metric exports
- Replication protocol details, including transfer ordering and channel mapping scope
- Replication quality metrics (coverage, response-time, context-hit rates) instead of claiming perfect fidelity

## Administrator value

- Engagement trend tracking
- Channel activity timing patterns
- Interaction graph transparency
- Topic-level signal monitoring
- Operational workflows for safe and reliable cross-server replication

## Researcher value

- Exportable aggregate metrics for reports
- Transparent methodology endpoint for appendices
- Privacy-first defaults suitable for institutional review workflows
- Account-token health monitoring and rotation metadata for reproducible replication analysis

## Replication safeguards checklist

- Use only server-authorized environments with explicit channel mappings.
- Never run replication workloads against communities without documented approval.
- Keep all outputs anonymized in publications, demos, and data exports.
- Maintain transparent logs for token health checks, source-history fetches, and replication sessions.
- Preserve source message content and metadata (author hash, timestamps, and message IDs) during transfer.
