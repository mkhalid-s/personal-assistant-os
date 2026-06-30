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
