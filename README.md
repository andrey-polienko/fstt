# FSTT - Freshservice Time Tracking

## Start
```
python fstt.py
```
The first run walks you through setup:
1. Your name and email.
2. The API key. It is saved outside this folder; FSTT shows the safe command for your OS and never asks you to paste the key.
3. Your customers. Search by name or ID.
4. The field values Freshservice needs to resolve tickets.
5. The recurring ticket types.
6. Optionally, the CloudBlue Internal Company for internal meetings.
7. Optionally, the scheduled tasks.

After that, `python fstt.py` opens the menu. At any prompt you can type `b` (back), `q` (quit) or `?` (help).

If you had an older `config.toml`, it is converted automatically and the original is kept as `config.v1-backup.toml`.

## Scheduled-task commands
These are set up by Settings -> Scheduled tasks; you normally don't type them.

| Command | What it does |
|---|---|
| `python fstt.py create-week` | creates this week's set (customers marked manual-only, like Internal, are skipped) |
| `python fstt.py resolve-previous` | resolves earlier weeks that are still open |
| `python fstt.py remind` | opens the "no time logged yet" screen for this week |

Add `--dry-run` to see what would happen without changing anything.

## Files
| File | Contents |
|---|---|
| `config.toml` | settings (FSTT rewrites it) |
| `state.json` | ticket IDs per week |
| `fstt.log` | output of scheduled runs |
| `fstt-schedule-<os>` | the scheduled-task commands generated for you |
