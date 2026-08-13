Offline precip archive for Guatemala training (2023-06-21 07:00–08:00).

Populate this folder once (from a machine with the training precip already in EF5_conf):

  bash offline/materialize_offline_precips.sh

Expected layout after materialize:

  offline_precips/
    stream_sat/caribbean/ensP1..N/*.tif
    stormlab/guatemala/ensQ1..M/*.tif
    imerg/*.tif
    qpf_store/_shared/<YYYYMMDDHHMM>/gfs_data/*.tif

If this folder is empty, --offline still works by re-ranking the live
EF5_conf/precip ensembles (wettest members first) and skipping downloads.

Run (ONLY these times — other timestamps are refused in --offline):

  ./tito-run.sh hindcast "2023-06-21 07:00" "2023-06-21 07:00" --regions Guatemala --offline
  ./tito-run.sh hindcast "2023-06-21 07:00" "2023-06-21 08:00" --regions Guatemala --offline

Allowed cycle keys: 202306210700, 202306210800
(override with env TITO_OFFLINE_ALLOWED_CYCLES=... if you extend the archive)

For any other date/time, drop --offline (online downloads).
