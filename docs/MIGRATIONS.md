# Migration And Recovery Notes

## Verify Schema

Run:

```sh
myos migrations verify --strict
```

This checks the migration ledger, required tables, SQLite `quick_check`, and foreign-key integrity.

## List Applied Migrations

Run:

```sh
myos migrations list
```

This prints each applied migration version, migration name, and timestamp, followed by the current and expected schema version.

Current schema version: `42`.

Recent production-readiness migrations:

- `35 add_recommendation_feedback`: stores privacy-safe recommendation feedback metadata.
- `36 add_factory_executor_backend`: stores factory executor backend and bounded executor context for coding-agent handoffs.
- `37 add_approval_integrity_binding`: pins approved action payload hashes and approval timestamps.
- `38 add_receipt_compensating_action`: stores approval-gated rollback proposals on execution receipts.
- `39 add_reminders`: adds durable reminders and the scheduler-facing due-time index.
- `40 add_personas`: stores scoped persona instructions, retrieval scopes, backend preferences, and action allowlists.
- `41 scrub_connector_payloads`: applies privacy filters to connector rows and nested raw payloads created before connector redaction became a persistence invariant.
- `42 add_factory_persona`: records the active persona on factory runs so scoped retrieval, role reasoning, and action filtering remain auditable.

## Backup Before Risky Work

Run:

```sh
myos backup
```

Backups are SQLite database copies written with the SQLite backup API.

## Restore

Run:

```sh
myos restore --from path/to/assistant-backup.db
```

Restore first writes a pre-restore backup of the current database, copies the selected backup into place, then verifies migrations.
