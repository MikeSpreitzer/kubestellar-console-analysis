<!--
Copyright 2026 Mike Spreitzer
SPDX-License-Identifier: Apache-2.0
Authored by Mike Spreitzer with assistance from Claude (Anthropic, Opus 4.7).
-->

# Volume calibration: recorded query outputs

These tables show the output of the two volume-calibration SQL
queries in [README.md](README.md#volume-calibration-via-direct-sql)
at the time `git log -1 CALIBRATION.md` reports. They go stale as
new weeks accumulate; the recency drop-off in the trailing rows
(small `c_reopened` and small `merged` totals at the end of each
table) is itself a marker of how recent the snapshot is.

## Issues per week

```
week_start  reopens  c_reopened  c_quiet  c_human  n_humans
----------  -------  ----------  -------  -------  --------
2026-01-19  5        3           14       17       1
2026-01-26  0        0           60       47       2
2026-02-02  0        0           65       65       1
2026-02-09  0        0           42       42       5
2026-02-16  0        1           65       58       6
2026-02-23  1        1           50       49       6
2026-03-02  2        4           193      180      7
2026-03-09  3        1           198      149      15
2026-03-16  13       13          220      189      12
2026-03-23  13       17          298      280      15
2026-03-30  37       42          427      451      15
2026-04-06  93       85          1477     1325     18
2026-04-13  65       61          1243     1090     20
2026-04-20  28       24          596      527      15
2026-04-27  26       9           629      468      22
2026-05-04  4        4           602      529      32
2026-05-11  9        6           670      335      28
2026-05-18  7        4           447      210      17
2026-05-25  1        0           125      92       8
```

## Merged PRs per week

```
week_start  merged  n0   n1   n2  n3  n4  n5+  mean_isc
----------  ------  ---  ---  --  --  --  ---  --------
2026-01-12  44      42   2    0   0   0   0    1.0
2026-01-19  36      33   3    0   0   0   0    1.0
2026-01-26  191     181  10   0   0   0   0    1.0
2026-02-02  255     227  26   2   0   0   0    1.07
2026-02-09  226     207  19   0   0   0   0    1.0
2026-02-16  223     192  29   1   1   0   0    1.1
2026-02-23  109     69   40   0   0   0   0    1.0
2026-03-02  315     272  42   0   0   0   1    1.19
2026-03-09  309     225  73   6   4   1   0    1.2
2026-03-16  245     152  84   7   1   1   0    1.13
2026-03-23  312     106  186  15  2   0   3    1.17
2026-03-30  354     262  73   7   7   1   4    1.45
2026-04-06  525     223  169  43  23  13  54   2.78
2026-04-13  663     364  225  30  14  7   23   1.95
2026-04-20  629     322  262  29  4   6   6    1.28
2026-04-27  700     358  270  42  17  4   9    1.4
2026-05-04  594     172  362  25  19  11  5    1.3
2026-05-11  734     228  466  33  2   3   2    1.11
2026-05-18  476     135  313  23  1   2   2    1.13
2026-05-25  134     42   81   7   3   1   0    1.17
```
