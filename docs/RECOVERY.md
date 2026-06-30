# Recovery Checklist

1. Stop any long-running `myos pulse`, `myos autopilot`, or `myos worker` process.
2. Run `myos backup` if the current database is still readable.
3. Restore the last known-good backup with `myos restore --from <backup.db>`.
4. Run `myos doctor --strict` and `myos migrations verify --strict`.
5. Run `myos performance-baseline` to capture post-restore retrieval/readiness timing.
6. Re-run connector sync only after `myos setup-live --check` reports safe local readiness.
