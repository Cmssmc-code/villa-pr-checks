# villa-pr-checks

Public CI runs used as proof in pull requests to [ScrollPrize/villa](https://github.com/ScrollPrize/villa).

Each workflow checks out the upstream `main` commit and a pull-request commit of the fork
[Cmssmc-code/villa](https://github.com/Cmssmc-code/villa), runs the same commands on both,
writes a summary on the run page and keeps the logs as run artifacts.

| Workflow | Branch | What it runs |
|---|---|---|
| [spiral-slab-workers](.github/workflows/spiral-slab-workers.yml) | `spiral-slab-workers-windows` | On Ubuntu and macOS: the `spiral-fitting` tests of `extract_surface_tracks.py`, and `extract_surface_tracks.py --backend cpu` with the default options, in one process and with `--slab-workers 2` on a crop of the public PHerc0332 surface predictions; the outputs of main and of the branch are compared key by key ([scripts](spiral-slab-workers/)). |

A run for another commit of the branch can be started from the Actions tab
(Run workflow, `villa_sha`).
