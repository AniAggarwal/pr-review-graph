# Config reference

One JSON file describes the graph. See `examples/example.config.json` for a complete example.

## Top level

| Key | Required | Meaning |
|---|---|---|
| `title` | yes | Page name (browser tab, gallery, `<h1>`). Two to four words, e.g. "Rocket 2.0 Review Graph". |
| `subtitle`, `caption` | no | Text under the title and under the graph. Wrap code in `backticks`. |
| `trunk` | yes | The branch every PR merges into: `{id, repo, branch, name, label, notes}`. Shown as a box beside the root PR. |
| `target` | yes | Where the trunk lands at the end (usually `main`): `{id, repo, branch, name}`. Shown above the trunk. |
| `final_label` | no | Label on the trunk → target arrow. `\|` breaks the line. Default "one PR once\|everything is in". |
| `bar` | no | The dashed "waits for" bar: a base-only branch that combines every PR above it, `{id, repo, branch, name, label}`. In `label`, `{sha}` becomes the branch's short SHA. |
| `rows` | no | Row labels: `chain1`, `chain2`, `siblings`, `last`, `other`. |
| `steps` | no | Names for review steps, e.g. `{"1": "Core loader"}`. Unnamed steps show as "Step N". |
| `statuses` | no | Extra or renamed statuses: `{"blocked": {"label": "Blocked", "color": "fixups"}}`. The colors are `needs`, `fixups`, `approved`, `merged` and `branch`. The built-ins are needs_review, rereview, fixups, changes, decision, approved, merge_now, merged, closed, hold and branch. |
| `verdicts`, `decision_verdicts` | no | The verdict buttons, as `[[value, label], …]`. The defaults are approved, merge_now, changes and hold. |
| `legend` | no | Legend wording overrides: `needs`, `fixups`, `approved`, `merged`, `step`, `stacked`, `waits`. |
| `edges` | no | `"infer"` (default) or an explicit list of `{from, to, kind}`. |
| `extra_edges` | no | Edges added on top of the inferred ones. |
| `waits_for` | no | `"auto"` (default) draws dashed lines into the bar from the last box of each chain and from every sibling. Anything else turns that off. |
| `artifact_url` | no | The published page this config refreshes, so later runs republish to the same URL. The scripts ignore it; it is a note for whoever runs the loop. |
| `nodes` | yes | The boxes (below). |

## Nodes

| Key | Meaning |
|---|---|
| `repo`, `number` | GitHub `owner/name` and the PR or issue number. `fetch` reads the rest from GitHub. |
| `kind` | `pr` (default), `issue`, or `decision` (no GitHub item; give `id` and `title`). |
| `id` | Stable, path-safe row id. Allowed characters: letters, digits and `_ - . ~ : @ +`. Default `<repo name>-<number>`. Never change it once reviews exist, because the user's verdicts and feedback point at it. |
| `group`, `order` | Where the box goes: `root` (the PR everything stacks on), `chain1`/`chain2` (stacked chains; `chain2` shares the root's row), `siblings` (each directly on the root; they hang off a bus), `last` (after the bar), `other` (other repos and decisions). `order` runs left to right. |
| `label` | Short name under the number, e.g. "core loader". |
| `top` | Override for the bold line, e.g. "docs #7" for a PR in another repo. |
| `title` | Override for the GitHub title. |
| `review_step` | The circled number. A PR may not have a lower step than the PR it stacks on. |
| `status` | One of the status vocabulary. If omitted, it's needs_review, or merged/closed from GitHub. A user's verdict at the current head overrides it live on the page. |
| `notes` | Shown in the panel. |
| `owner` | Who fixes feedback on this node (an agent or a person). `inbox` uses it. |
| `reviewed_head` | The SHA the user last reviewed, if they reviewed before the graph existed. The page links `compare/<reviewed_head>..<head>`. |
| `reviewed_patch_id` | The PR's patch-id at `reviewed_head` (12 hex chars), if it isn't already in the patch-id history. |
| `compare_url`, `compare_label` | An explicit "diff since your last review" link, e.g. from a review-only branch on a rebased stack. Takes precedence over everything else. |

## Edge inference

For every PR, `build` matches its base branch, in the same repo, against:
1. another node's head branch: a stacked edge from that PR;
2. the trunk branch: an edge from the trunk;
3. the bar branch: an edge from the bar.

PRs whose base is anything else (e.g. `main` in another repo) get no edge. Edge ids are `<from>__<to>`.

As PRs merge into the trunk, GitHub retargets the PRs above them to the trunk. The picture stays the same: a chain member whose base is now the trunk still hangs off the previous box in its chain, and a chain head or sibling still hangs off the root.

A PR that is merged or closed on GitHub always shows as merged or closed. That state overrides both the config's `status` and any verdict.

## Rows written to the artifact db

`build` writes `<page>.rows.json` with `nodes` and `edges` arrays. These are the documents `seed` writes to the `nodes` and `edges` collections. The page itself writes `reviews/<node_id>` `{node_id, verdict, head, by, at, sent_at?}` and `feedback/<auto>` `{node_id, text, head, author, at, resolved, reply, sent_at?, sent_thread?}`.

## Re-check: approvals on an older head

`fetch` records each PR's current patch-id (`gh pr diff` piped to `git patch-id --stable`). It keeps every head it has seen in a patch-id history file (`--history`, default `patch-history.json` next to `--out`). Keep that file beside the config so the history survives between runs.

An approval at an older head (the config's `status: approved` with `reviewed_head`, or a `reviews` row from the page) then works like this:
- **Same patch-id** at the old and new head: only the parent moved (a re-parent). The approval stands, and the box stays green.
- **Different or unknown patch-id:** the status becomes `recheck`. The box gets an amber border and a "re-check" tag, and the panel shows both patch-ids and the delta link.

The page stores the current `patch_id` in every verdict it writes, so later approvals carry their own patch-id. To backfill an approval made before patch-ids were recorded, run `review_graph.py patchid --repo R --base <base sha then> --head <head sha then> --record patch-history.json --key R#N`.
