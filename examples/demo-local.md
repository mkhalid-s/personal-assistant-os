# Local-Only Demo

This demo exercises the assistant without network access, connector credentials, hosted graph services, or external mutations.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
cp .env.example data/.env.myos
```

`data/.env.myos` is ignored by git. Leave connector tokens blank for this demo.

## Run The Demo

```bash
export MYOS_DB_PATH=./data/demo-assistant.db
myos doctor --strict
myos capture "Decision: rollout canary first for the billing service"
myos capture "Risk: launch depends on platform owner confirmation by Friday"
myos capture "Task: prepare executive summary for launch readiness"
myos triage
myos today --meeting-hours 2
myos context "billing launch canary risk"
myos at-risk
myos why --item 1
myos close-day --mode hybrid --note "Validated local-only assistant loop"
myos report --output-dir ./data/reports
myos sanity --strict --report-dir ./data/reports
```

## Expected Result

You should see:

- `doctor --strict` reporting core checks as passing.
- Captured inbox items becoming triaged work items.
- `context` returning locally indexed context.
- `at-risk` showing the launch risk.
- A generated daily report under `data/reports/`.

## Cleanup

```bash
rm -f ./data/demo-assistant.db ./data/demo-assistant.db-shm ./data/demo-assistant.db-wal
```
