# TITO offline training mode

No network downloads. Uses pre-staged precip for the Guatemala training window:

```bash
./tito-run.sh hindcast "2023-06-21 07:00" "2023-06-21 08:00" --regions Guatemala --offline
```

Windows CMD:

```bat
tito-run.cmd hindcast "2023-06-21 07:00" "2023-06-21 08:00" --regions Guatemala --offline
```

## How it works (outside the core pipeline)

1. `offline/run_offline_hindcast.py` monkey-patches `prepare_cycle_precip` for **this process only**.
2. Offline hook stages precip from `offline_precips/` (or live `EF5_conf/precip` if the archive is empty).
3. If `stream_sat_ensemble_size` / `stormlab_ensemble_size` is smaller than the archive, the **wettest** members (highest mean precip) are mapped to `ensP1..N` / `ensQ1..N`.
4. Normal EF5 + FIM then run unchanged.

## Populate `offline_precips/`

After one good online run that filled `EF5_conf/precip` and `qpf_store`:

```bash
bash offline/materialize_offline_precips.sh
```

Expected tree:

```text
offline_precips/
  stream_sat/caribbean/ensP1..10/
  stormlab/guatemala/ensQ1..5/
  imerg/*.tif
  qpf_store/_shared/<cycle>/gfs_data/*.tif
```

## Knobs

| Env / config | Meaning |
|--------------|---------|
| `TITO_OFFLINE=1` | set automatically by `--offline` |
| `TITO_OFFLINE_PRECIP` | archive path (default `offline_precips/`) |
| `stream_sat_ensemble_size` | how many SS members to stage (wettest first) |
| `stormlab_ensemble_size` | how many SL members to stage (wettest first) |
| `warmup_enabled` | forced **False** in offline runner |

## Notes

- Core `orchestrator.py` / `prepare_precip.py` are **not** modified for normal online runs.
- Offline only intercepts precip prep via a process-local monkey-patch.
