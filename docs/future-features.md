# Future Features

Candidate features that are **not yet implemented**, sorted by difficulty
(easiest first), with a feasibility assessment for each. See "Sequencing &
dependencies" for the threads that drive ordering. Ideas investigated and
**dropped**, and the standing constraints (AMD/ROCm, provider choice), live in
`docs/dropped-ideas.md`.

One open milestone has a **date on it** and is listed first for that reason alone (#25, the
RunPod API v2 migration: REST v1 returns 410 Gone on **2026-11-15** and GraphQL in **early
2027**, and the app uses both). One is small (#24, enriching what a bug report auto-fills). The
rest are medium or larger: one measurement-gated processing capability (#21 denoising, deferred
behind #25 and gated on a measurement that has not been run), a Video Upscaler feature (#12 mixed
local+remote queue) and a remote-side one blocked on funds rather than design (#15 a second GPU
provider). Two lower-priority ones each introduce a new process model, networking, or packaging
(HTTP interface #3, Unraid #4). The **shipped** milestones are kept below as a numbering legend,
after the open work.

---

## Contents

- [25. RunPod API v2 migration](#25-runpod-api-v2-migration-medium-dated-money-adjacent)
- [24. Make a bug report actionable without asking](#24-make-a-bug-report-actionable-without-asking-small-medium)
- [21. Denoising before upscaling](#21-denoising-before-upscaling-medium-gated-on-a-measurement-deferred)
- [12. Local+remote mixed queue](#12-localremote-mixed-queue-medium)
- [15. Second remote GPU provider (packet.ai)](#15-second-remote-gpu-provider-packetai-medium)
- [3. HTTP interface](#3-http-interface-hard-low-priority)
- [4. Unraid Community Apps integration](#4-unraid-community-apps-integration-hardest-low-priority)
- [Sequencing & dependencies](#sequencing--dependencies)
- [Shipped milestones (numbering legend)](#shipped-milestones-numbering-legend)
- [Decided against / constraints](#decided-against--constraints)

---

## 25. RunPod API v2 migration: Medium (dated, money-adjacent)

Move the RunPod integration off **two** transports RunPod has now dated for shutdown, onto the
single `https://api.runpod.io/v2`. This entry sits first despite the file's easiest-first order,
because it is the only milestone here with a **date on it**: everything else can slip, this one
turns into a broken shipped feature on a calendar day. It is also what #21 is deferred behind
(2026-08-19).

**The announcement, as it applies here:** REST v1 stops serving traffic and returns **410 Gone on
2026-11-15**; the GraphQL API returns 410 in **early 2027**. Both are supposed to carry a `Sunset`
header from now on. Measured 2026-08-19 against this account: **neither actually sends one**
(`rest.runpod.io/v1/pods` and `api.runpod.io/graphql` both answer 200 with no `Sunset`,
`Deprecation` or `Link` header). So the dates belong in the code as constants, and the real
detection signal is a **410**, not a header that may never arrive.

### The two deadlines, and what each one takes with it

The app is unusual here in that it uses **both** doomed transports at once, for different calls,
and the halves die five months apart.

| Transport | What the app does on it | Dies |
|---|---|---|
| REST v1 (`rest.runpod.io/v1`) | `list_pods`, `get_pod`, `create_pod`, `start_pod`, `stop_pod`, `terminate_pod`, `pods_using_volume`, and all four network-volume calls | **2026-11-15** |
| GraphQL (`api.runpod.io/graphql`) | `deploy_pod` (every pod the app actually creates), `available_gpus` (all three live GPU pickers), `data_centers` (the Settings region picker), `list_pods_detailed` (the RunPod tab), `account_balance` (the funds guard) | **early 2027** |

Losing REST v1 breaks teardown, volume management and status polling. Losing GraphQL breaks **pod
creation**, which is the remote feature entirely. Neither half is optional.

**The good news is structural and was decided years ago:** `runpod_client.py` is a real seam.
Outside it the app touches RunPod through named functions plus exactly **10 raw field reads**,
and those 10 are the whole leak list:

| Where | Reads | v2 name |
|---|---|---|
| `remote_run.py:326`, `batch_video_upscale.py:2590`, `runpod_provision.py:242,312`, plus `pod_status` / `wait_until_running` inside the client | `desiredStatus` | `status` |
| `remote_run.py:417,427` | `costPerHr` | `cost` |
| `runpod_client.ssh_endpoint` | `publicIp` + `portMappings["22"]` | `ssh.direct{host,port}`, or `runtime.ports[]` where `private == 22` |
| `gui/tab_runpod.py:770,782,796` | a volume's `dataCenterId` | `dataCenter` |

### What v2 gives back, measured rather than assumed

Priced by calling v2 with the app's own key: read-only GETs, plus two create calls made
deliberately invalid so they could not deploy anything.

- **The GPU picker is a drop-in, and parity is confirmed.**
  `GET /v2/catalog/gpus?include=AVAILABILITY&product=POD&cloud=SECURE` against EU-RO-1 returned
  **the same 9 cards, at the same prices, at the same stock levels** as today's GraphQL
  `gpuTypes { lowestPrice }` query (RTX 2000 Ada $0.24 LOW, A4500 $0.25 LOW, L4 $0.49 LOW, A6000
  $0.53 LOW, PRO 4000 Blackwell $0.57 MEDIUM, PRO 4500 Blackwell $0.72 HIGH, 4090 $0.74 MEDIUM,
  5090 $0.99 LOW, A100-SXM4-80GB $1.59 LOW), plus the MI300X that `is_amd_gpu` already drops. The
  stock enum is the same three levels in upper case (`LOW/MEDIUM/HIGH`, plus an explicit `NONE`),
  so `available_gpus`' returned shape survives unchanged.
- **One call now covers every data center, but only if you ask for it.** Each GPU carries a
  `dataCenters[]` array of per-DC availability, so the per-region call the pickers make today
  collapses to one, and the UI can finally answer the question a sold-out card raises: *where is it
  in stock?* **Correction, measured while building P2:** none of that is in the catalog by
  default. A plain `GET /v2/catalog/gpus` is a price list, with no `availability`, no
  `dataCenters`, no `cudaVersions`, and `GET /v2/catalog/gpus/{id}` returns exactly the same
  fields. They appear only under `include=AVAILABILITY`, which additionally **requires**
  `product=POD` (no default, because a card can be scarce for pods and plentiful for serverless)
  and takes `cloud`, defaulting to SECURE upstream. This entry originally stated the fields as
  though they were always present, because it was read off the migration guide rather than off a
  response. That is the same mistake the entry itself is about, committed inside the entry.
- **`cudaVersions` per GPU, each flagged available or full** (under the same expansion). This
  retires the app's most
  embarrassing workaround: `KNOWN_CUDA_VERSIONS` enumerates every CUDA version by hand because
  `allowedCudaVersions` is exact-match set membership, and `deploy_pod` then applies the floor
  **only to consumer cards** as a heuristic. v2 has `gpu.minCudaVersion` (a real numeric floor,
  compared numerically so 12.11 is above 12.2) and the catalog says which versions have free
  capacity, so "the card is in stock but every machine on a new enough driver is full" becomes a
  **pre-flight check** instead of a burned deploy attempt.
- **A richer pod status enum**: `PROVISIONING / STARTING / RUNNING / EXITED / ERROR / TERMINATED`.
  `wait_until_running` currently waits out the full `deploy_timeout` on a pod that is never coming
  up, because it only knows to fail on `EXITED`/`TERMINATED`. `ERROR` lets it fail fast.
- **`runtime`** (per-GPU util, memory, CPU, uptime, live port mappings) **without SSH**, and
  **`GET /v2/pods/{id}/logs`**. Both are diagnostics the app currently gets only through its own
  tunnel and worker, which is exactly what is unavailable when something has gone wrong.
- **Typed 422s.** A bad create answers `{"title","status","detail","errors":[...]}` with the
  offending field named. Measured: `{"detail":"Unknown GPU type: __invalid__"}` and
  `{"errors":["$: additional properties 'supportPublicIp' not allowed"]}`. That is a status-line
  message worth showing verbatim.
- **Published rate limits**, in `RateLimit` / `RateLimit-Policy` headers on every response:
  measured 180/minute, 7200/hour, 86400/day. The 5-8 s status polling is nowhere near it, so
  nothing has to change; it is recorded so a future poll loop is not designed blind.
- **The v1 create enum problem is gone.** v1 REST `create_pod` 400s on cards the catalog lists
  (Blackwell PRO 4000/4500), which is the entire reason `deploy_pod` exists on GraphQL. v2
  validates against the catalog itself ("Unknown GPU type: …" for a bogus id), so `deploy_pod`'s
  reason to exist disappears and pod creation returns to being one ordinary POST. **Unconfirmed
  until a live deploy** (see Verification): a rejected invalid id proves the validator is not a
  curated enum, not that a Blackwell card deploys.

### The one thing v2 does not have: the account balance

**Settled in P3 below, 2026-08-20:** it is genuinely not there, it is not coming, the GraphQL
island stays until it 410s, and the code now SAYS when a configured floor is not being enforced.
The analysis that got there is kept as written.

There is **no account or balance endpoint in v2**. The full OpenAPI document
(`https://api.runpod.io/v2/openapi.json`, read 2026-08-19) has exactly one `/v2/account/*` path
and it is SSH keys; the only match for "balance" anywhere in the spec is "load balancer". The
balance lives solely in GraphQL's `myself { clientBalance currentSpendPerHr }`, which is the half
that dies in early 2027.

That matters because it is not a readout, it is a **safety net**: `funds_guard`'s start floor
refuses a run whose estimate would push the account below a configured floor, and the poller's
floor half stops a live run when the balance falls to it.

**What survives and what does not**, if nothing replaces it:

- **The session cap survives intact.** `session_cost(cost_per_hr, elapsed)` needs no balance, and
  v2 reports `cost` on the pod itself, which is the *real* billed rate rather than the picker's
  list price. The cap is arguably the better half of the guard anyway: it bounds *this run*.
- **The start floor and the balance floor die**, and they die *quietly and correctly*:
  `account_balance` returns None on any failure by contract, and `start_blocked` / `evaluate` both
  fail open on a None balance, so a 410 degrades to "the floor checks are skipped" rather than a
  crash or a blocked run. That is the right behaviour, but it is silent, so it has to become
  **visible**: the Settings floor field and the GUI funds readout must say the balance is no
  longer available, or a user keeps a floor configured that is not being enforced.
- **`GET /v2/billing?lastN=…` is spend, not balance**, so it cannot answer "will this run take me
  below $X". It can answer "what did the last N days cost", which is a more useful thing to show
  than a bare balance, and is worth adding regardless.

**Asked upstream, 2026-08-19:** [runpod/docs#807, "APIv2 and GraphQL account
balance"](https://github.com/runpod/docs/issues/807) puts the question to RunPod directly, naming
the overspending guard as the use case. **Check it before starting P3**, and do not wait on it:
an unanswered issue is itself an answer by the time GraphQL stops serving. What each outcome
means here:

| Answer | What P3 becomes |
|---|---|
| An account endpoint is coming | Keep the floor, repoint `account_balance` at it, done |
| No endpoint, ever | The floor is retired deliberately: remove the setting, say so in the release notes, and lean on the session cap plus `/v2/billing` spend |
| Silence | Same as "no endpoint", but the code keeps the GraphQL island until it 410s, since it costs nothing to leave working |

**Recommendation:** keep the GraphQL balance call alive as an isolated, already-fail-safe island
until it 410s (it is one query and one function), and design the UI now for the case where the
balance is unknown. Do **not** hold the rest of the migration on this question: it is the only
piece with no v2 answer, and it is already the piece built to work without one.

### v2's own maturity is contradictory, so build the seam and keep the escape hatch

**RunPod says both things, and the difference is not resolvable from outside.** The deprecation
e-mail of 2026-08-19 opens with "The new REST API v2 is **generally available** today". The launch
blog post, published 2026-07-29 and last edited 2026-08-14, still reads "now in **public beta**"
and carries "one honest caveat: v2 is in beta, which means endpoints and behavior may still change
before general availability" (both verified against the live page, 2026-08-19). The
`api-reference-v2` docs pages claim neither, and the OpenAPI document says only
`version: 2.0.0`. The likeliest reading is that v2 went GA with that e-mail and the blog post was
never updated, but a stale marketing page is a guess, not evidence.

**It does not change the decision, which is the useful part.** GA or beta, the API is **three
weeks old** and is being introduced alongside the retirement of the two things it replaces, so the
shape of the work is the same either way: assume it will move under us, and make that survivable
rather than betting on which label is current. The app has three months of overlap on the v1 half
and five on the GraphQL half: enough to migrate calmly, not enough to migrate twice. So **one
transport module, one config switch** (`runpod.api_version`, defaulting to v2, with v1/GraphQL
retained until the deadlines pass: see the switch direction below), and every response normalised
into the shapes the app already consumes, so the rest of the codebase changes in ten places
rather than everywhere.

Worth re-checking when the migration actually starts: if the blog still says beta then, that is a
second data point about how current RunPod's own documentation is, which matters more here than
the label does, because this plan is built on their spec.

**Which way the switch points: v2 is the default and v1 is the escape hatch, not the reverse.**
The tempting shape is "keep running v1, fall back to v2 when v1 stops answering", and it is wrong
for four reasons, the first of which is decisive on its own.

- **Installs do not update in lockstep.** A version shipped today with v1 as its default is still
  running on someone's machine on 2026-11-15, and that is the day it breaks, with no one at the
  keyboard. Ship v2 as the default now and those same installs pass the date untouched. The
  in-app updater helps but does not settle it: "Skip this version" exists.
- **The fallback path is the untested path.** Whichever transport is the default is the one every
  real run exercises; the other one runs for the first time on the day it is needed. v1-default
  means v2's first real execution is in November, unattended, on every install at once, spending
  money. v2-default inverts that: beta churn is discovered now, while v1 still exists and there
  are three months of runway. This is the same lesson as D1 and the ffmpeg pin: code that has
  never run is not a fallback, it is a hope.
- **A fallback should point at the more durable transport, and v1 dies first.** v1 also cannot
  stand alone: `deploy_pod` lives on GraphQL precisely because v1's create enum rejects the
  Blackwell cards the picker offers. So "default to v1" really means keeping v1 **and** GraphQL,
  and the app carries three transports instead of one plus a balance island.
- **Automatic cross-transport retry is unsafe exactly where the money is.** "v1 failed, try v2"
  around pod creation can deploy **two billed pods** when the first call succeeded and only its
  response was lost. Deploy is not idempotent, so it must never auto-fall-back. The same money
  argument runs the other way for **teardown**, where failing to stop a pod bills until the
  dead-man's switch fires: there, trying the other transport is right. One rule for both is what
  would be wrong.

So: `runpod.api_version` defaults to `v2`, `v1` is a documented escape hatch a user can set when
beta churn bites, a capability probe (one `GET /v2/pods`) **tells the user to flip the switch**
rather than flipping it silently, and automatic retry on the other transport is allowed only for
idempotent calls. v1 and the GraphQL island are then deleted on their own deadlines, not before:
the v1 half after 2026-11-15, the balance query when it 410s.

### The failure mode this migration actually has

Not the loud one. A wrong **request body** is a hard 422 that names the field, and unknown body
properties are rejected outright (`unevaluatedProperties: false`), so the create path fails
noisily and legibly. Two other things fail silently, both toward spending money:

1. **A renamed response field reads as None**, and the app's checks treat None as a decision
   rather than as an absence. Three worked examples straight off the leak table:
   - `remote_run._find_existing_pod` compares `desiredStatus` to `RUNNING`. Under v2 that is
     always None, so the app **never recognises its own running pod** and deploys a second one
     beside it. Two pods, both billing.
   - `runner_common.remote_pod_stopped` returns True for `status in (None, EXITED, TERMINATED)`.
     Under v2 it returns **True unconditionally**, so every transient network blip reads as "the
     pod is gone" and the auto-resume supervisor (#6) ends exactly the runs it exists to rescue.
   - `remote_run` reads `costPerHr` into `funds_guard`'s `cost_per_hr`. Under v2 that is None, so
     accrued session cost computes as zero and **the session cap never trips**: the one guard
     that was going to survive losing the balance.
2. **An unknown query parameter is ignored, not rejected.** Measured:
   `GET /v2/pods?desiredStatus=RUNNING` answers **200 with the full list**. A filter that stopped
   being applied looks exactly like a filter that matched everything.

This is the same shape the project has hit four times already (`known-defects.md` D1, the ffmpeg
pin, NVENC, the BtbN month-end URL): **present is not working, and accepted is not applied.** The
countermeasure is the one that would have caught those: assert on **behaviour**, not on the call
returning 200.

### Plan, in priority order

**P0. The normalisation seam and its tests. DONE, 2026-08-20.** There was no
`tests/test_runpod_client.py` at all, so nothing anywhere pinned the response shapes. What
shipped: a `# response normalisation` section in `runpod_client.py` holding ten pure accessors
(`unwrap_list`, `pod_state`, `pod_cost`, `pod_data_center`, `pod_gpu`, `pod_gpu_count`, `pod_ssh`,
`pod_volume_id`, `pod_record`, `volume_data_center`) plus `error_detail`, and all **ten raw field
reads outside the client routed through them** (`remote_run` x3, `batch_video_upscale` x1,
`runpod_provision` x2, `gui/tab_runpod` x3, and the client's own internals). The accessors read
**every shape at once** rather than switching on a configured version: a tolerant reader needs no
switch to get right, so the switch, when it lands, only decides which URL to call. Two
consequences worth recording. `_normalize_gql_pod` and `_normalize_rest_pod` collapsed into one
`pod_record`, and `_http_error_message` now reads the RFC 9457 body as well as the v1 flat one,
which was listed under P1 but belongs here: it is a read whose field moved, exactly like the
others. `tests/test_runpod_client.py` pins it with **recorded** payloads from the deploy below (a
spec-derived fixture would assert what the spec says, which is the mistake this project has made
four times), and names its money tests after the consequence rather than the field: a running pod
must be recognised so no second pod is deployed, the billed rate must be readable so the session
cap can trip, a running pod must never read as stopped. 27 tests, offline, 1455 in the suite still
green.

The last of those tests sweeps every module for `x.get("desiredStatus")`-shaped reads outside the
client, and **it was itself broken on the first attempt**, which is worth writing down because it
is this project's recurring failure in miniature. The first version was a regex over source text
with comments stripped; the comment-stripper returns TOKENS joined by newlines, so `.get(` was
never contiguous, the regex matched nothing, and the test passed. It passed with a violation
deliberately planted in `runner_common.py`. It is now a token scanner that reports `file:line`,
and it has its own test that plants a read and requires the scanner to find it. **A guard that has
never failed is not evidence that there is nothing to find.**

**P1. REST v1 to v2. DONE, 2026-08-20 (deadline was 2026-11-15).** `runpod_client` now speaks
both, and **v2 is what runs**. The switch is `runpod.api_version` ("v2" default, "v1" the escape
hatch), applied once per process by the two config loaders that every path already goes through
(`runner_common.load_config` for the runner subprocesses, `gui.common` for the GUI) rather than at
each call site, because a call site that forgot would talk to the wrong transport and the symptom
would be a renamed field silently reading None. It is **config-only, with no Settings control**,
like `ntfy_token` and `watchdog_min_samples`: it exists for the day beta churn bites, and
`probe_api_version` names the exact key and value to set when the configured version stops
answering, which is better discoverability than a line in a template nobody reads.

What moved: the base URL; `/networkvolumes` to `/network-volumes`; the volume create body's
`dataCenterId` to `dataCenter` (**the only renamed field in a REQUEST, which is why it cannot go
through the read seam**); the volume size bounds, checked against whichever version is live (v1
1-4000, v2 10-4096) so a refusal quotes a limit the user can act on; the three lifecycle verbs
collapsing into `POST /pods/{id}/action`; and `create_pod` gaining `v2_pod_body`, which **rebuilds**
the body key by key rather than patching a copy, because `unevaluatedProperties: false` turns a
single leftover v1 key into a 422. A **410 now says the actual thing to do** ("RunPod has retired
the API this version of Image Toolbox uses. Update the app.") instead of reporting a status code,
which matters because the promised `Sunset` headers are still not served and a 410 is the only
warning anyone gets. `test_connection` names the transport it used, so a user reporting a problem
says which one they were on without being asked.

Two things were deleted rather than ported. `list_pods`' `**filters` is gone: v2 takes no status
filter and **ignores** unknown query parameters (measured, 200 with the full list), so a filter
that quietly stopped applying would be indistinguishable from one that matched everything. No
caller ever passed one. And the pass-through spec dict is gone with it, for the same reason in the
other direction.

**Verified live on a second pod**, since the shape of a request is not evidence that it works.
Through the app's own `create_pod`: a pod deployed on v2, **stopped through the action endpoint**,
read back, and terminated. Two findings, one of which was a real open question. A pod stopped on
v2 reports **`EXITED`**, the same value v1 uses, so `ensure_stopped`'s idempotence and
`runner_common.remote_pod_stopped` are both correct on v2 (v2's `PodStatus` enum is richer, so
this was worth measuring rather than assuming), and it keeps a populated `cost` while stopped,
which is right: a stopped pod still bills its disk. `ensure_stopped` correctly answered "already
stopped" without calling out again.

**`start_pod` is the one call P1 could not prove.** RunPod answered `400 Bad Request: Failed to
resume pod.` The request SHAPE was accepted (a malformed one is a 422 naming the field, measured),
so this is RunPod declining to resume that particular stopped pod, which it does when the machine
no longer has room, not a defect in the call. It is recorded rather than chased because **the app
never calls it**: runs deploy fresh disposable pods and only ever stop or terminate them. Worth
knowing before someone builds a feature on resume. Incidentally that error is itself the new error
reader working: the reason came through at all only because `_http_error_message` now reads the
RFC 9457 body, where before it would have printed a bare "HTTP 400 Bad Request".

What is deliberately still on the old transports after P1: pod **creation** (`deploy_pod`), every
live GPU and data-center picker, and the account balance. Those are GraphQL, they have until early
2027, and they are P2 and P3.

**P2. GraphQL to v2. DONE, 2026-08-20 (deadline was early 2027).** All four GraphQL calls now
have a v2 path, selected by the same `runpod.api_version` switch, so the setting picks a whole
stack (v2 REST + v2 catalog, or v1 REST + GraphQL) rather than just a base URL.

**`deploy_pod` was the load-bearing one** and it turned into a wrapper. The GraphQL mutation
exists only because v1's REST create enum 400s on newer cards; v2 has no such limit, so on v2
`deploy_pod` applies the CUDA policy and calls `create_pod`. It returns the whole pod object
instead of the mutation's three fields, a superset, so `create_pod_resilient` is untouched. v2
also needs neither of the two things GraphQL had to be told: `mounts.network[].path` carries what
`volumeMountPath` did, and `ports: ["22/tcp"]` alone publishes the endpoint, while sending
`supportPublicIp` is a 422. The CUDA floor was **hoisted** into `deploy_cuda_versions` so both
paths apply the identical policy: a transport swap must not quietly change which HOSTS a run can
land on. v2 offers `gpu.minCudaVersion`, a real numeric floor and exactly what
`KNOWN_CUDA_VERSIONS` enumerates around, and it was deliberately NOT adopted here. Changing the
transport and the host-selection policy in one step would make a bad landing impossible to
attribute. That swap stays P4.

**`available_gpus` and `data_centers` moved onto `/v2/catalog/*`**, and both were checked by
running the two implementations side by side against the live account rather than by reading the
schema. The GPU lists came back **byte-identical**: the same 7 NVIDIA cards for EU-RO-1, same
prices, same stock levels, same VRAM, same display names. The data-center lists came back
identical too, 18 storage-capable on both, same order. The only real difference anywhere was the
spelling of the stock level (`HIGH` against `High`), which the picker prints straight into a
combobox label, so `_stock_label` settles it in one place and maps `NONE` to None so the existing
`if not stock` filters keep working untouched.

One caveat on how to reproduce that, learned by tripping over it: **live stock moves fast enough
to fake a discrepancy.** A later run of the same comparison returned 7 cards from one transport
and 6 from the other, purely because a card sold out in the seconds between two sequential HTTP
calls; a third run agreed again at 7. Over about twenty minutes the EU-RO-1 list turned over
almost completely (the RTX 2000 Ada and B200 left, a 5090, an A4500 and a PRO 6000 SE arrived).
So compare the two within seconds, expect churn, and treat the recorded-payload tests rather than
a live run as the thing that actually pins parity.

**`list_pods_detailed` collapsed to a single GET on v2.** `GET /v2/pods` carries `gpu.id`,
`dataCenterId` and `cost`, so there is nothing to enrich and no second source to fall back to. The
GraphQL ladder with its memoised `_PODS_MACHINE_SELECTIONS` probing (which exists only because
GraphQL 400s the whole query on one unknown field) survives on the v1 branch and dies with it.

**Verified live on the production path**, with a **consumer** card on purpose: `is_consumer_gpu`
is what gates the CUDA floor, and the app's shipped default `gpu_type_id` is a GeForce, so that is
the path a default install takes. An RTX 4090 went through `create_pod_resilient` to `deploy_pod`
to `create_pod` to `POST /v2/pods` carrying `gpu.allowedCudaVersions`, landed, published its SSH
endpoint after about 84 s of reporting RUNNING (the early-RUNNING finding again, and again the
reason `wait_until_running` must require an endpoint), and answered correctly through
`pod_record`, `list_pods_detailed` and `pods_using_volume`, the last of which reads v2's
`mounts.network[].volumeId` and had until then only been unit-tested. Then terminated, verified
gone.

**The deletions this milestone "earns" are deliberately NOT taken yet**, and the module got
LONGER rather than shorter. `_DEPLOY_MUTATION`, `_GPU_AVAIL_QUERY`, `_DC_QUERY`,
`_PODS_MACHINE_SELECTIONS` and `CREATABLE_GPU_IDS` are all still reachable on the v1 branch, which
is the escape hatch, and an escape hatch that has had half its code removed is not one. They go on
their own deadlines, as the switch section says: the v1 half after 2026-11-15, `_graphql` itself
when `_BALANCE_QUERY` stops answering. P3 settled that one below: the balance has no v2 successor
at all, so the island stays until it 410s and takes `_graphql` with it.

**P3. The balance question. DECIDED, 2026-08-20** (GraphQL's deadline is early 2027). The answer
is that it is **not coming back, the floor is kept anyway, and its silence is what got fixed**.

**Re-verified rather than assumed**, against the live OpenAPI document and the live account on the
day of the decision. The spec has 34 paths and exactly one `/v2/account/*`, which is `ssh-keys`.
`/v2/account`, `/v2/account/balance`, `/v2/account/credits`, `/v2/user` and `/v2/me` all **404** on
a real account, so this is absent rather than undocumented. The words "credit", "funds" and
"wallet" do not appear anywhere in the spec, and all eight matches for "balance" are "load
balancer". [runpod/docs#807](https://github.com/runpod/docs/issues/807) is still open with zero
comments, which is the "Silence" row of the table above: it resolves the same way as "no
endpoint", while costing nothing to leave working.

**`/v2/billing` is real, and it is spend, not balance.** Measured over 7 days on this account:
$0.7632 total, of which $0.7389 is `storageStandardAmount` (the standing 50 GB model volume) and
$0.0243 is `podGpuAmount`, which is the **entire** live-verification bill for P0 to P2. Buckets are
`hour` or `day`, `lastN` resolves to a window, and a **zero-spend bucket is omitted rather than
padded** (measured: `lastN=3&bucketSize=hour` returned 2 records), so anything plotting it must not
assume N records for `lastN=N`. It answers "what did I spend", never "what is left", so it cannot
replace the floor. Worth showing anyway, and still P4.

**`spend_per_hr` has no naive successor either, and this is the measurement that kills the obvious
idea.** The plausible replacement is summing `pod_cost()` over the running pods. Measured with
**zero** pods running, GraphQL reported `currentSpendPerHr: 0.005`: the network volume's standing
storage charge, which no pod query can see. The pod sum would have reported $0.00/h for an account
that is genuinely being drained, which is the wrong direction to be wrong in. (Noticed while
checking: `spend_per_hr` is fetched today and read by nothing. `hours_until_depleted` is called
with the pod's own `cost_per_hr`.)

**So the decision is to keep the island and remove the silence**, and the silence is the whole of
P3's code. `funds_guard` is fail-**open** by contract: an unknown balance skips the start floor and
the in-run balance floor rather than blocking a run. That is right, and it is invisible. A user who
set a floor keeps a floor that is no longer applied, with nothing on screen and nothing in the log
to say so, which is the same family as every other bug this migration produced: **it fails toward
spending money and looks like a normal run**. What shipped:

- **`account_balance_detail()`** classifies a lookup as `BALANCE_OK` / `NO_KEY` / `RETIRED` /
  `ERROR` and never raises. `account_balance()` stays exactly what it was, a wrapper returning the
  plain pair or None, so every existing caller keeps its fail-safe contract byte for byte.
- **`RunPodError.status`** carries the HTTP status, so permanence is readable without matching on
  message text.
- **`funds_guard.floor_unenforced()`**, pure and unit-tested, is the one place that words it, and
  it words RETIRED and ERROR differently: a blip fixes itself and needs no action, while a retired
  balance never comes back and the user has to move to the per-run cap or lose the guard.
- **`on_warn` is finally fired.** It had been accepted, stored and called by nothing since the
  guard was written, so a run guarded by an unreadable floor looked exactly like a guarded one. It
  now fires once per run, edge-triggered like the trip.
- **The readout says which.** `Funds: Unknown` for a blip, `Funds: Not published` for a retirement,
  each with its own tooltip; `_preflight_funds` and the "Funds guard armed" line say it too, since
  the log is where a user looks afterwards.

**One assumption in this entry was wrong and was measured out of it.** It said GraphQL would answer
a removed field with a 200 carrying an `errors` array, "so there is no status code to read". Asking
the live endpoint for a field that does not exist answers **HTTP 400** with `Cannot query field "…"
on type "User".`, and the `errors` list holds **objects** where v2's RFC 9457 list holds plain
strings. Both matter: the classification reads the message rather than the code, and `error_detail`
had been stringifying those objects verbatim, putting a Python dict repr in front of the user with
the actual sentence buried inside it. Fixed, with the recorded body in the tests.

**A probing trap worth knowing:** `api.runpod.io` sits behind Cloudflare, which answers a plain
`urllib` User-Agent with **403 "error code: 1010"** on the v2 REST paths too, not just GraphQL. The
app is unaffected (it always sends its own `_USER_AGENT`), but a probe script written with bare
urllib reads that as "the endpoint rejects my key" and sends you chasing the wrong thing.

**What this leaves on a clock.** `_graphql` and `_BALANCE_QUERY` are now the ONLY GraphQL the v2
stack still uses, and they are also the only part of the escape hatch that is not shared with v1.
When they 410, the floor retires by itself: `floor_unenforced` starts saying the retired wording,
the readout starts saying "Not published", and nothing breaks. The one thing left to do on that day
is remove the floor field from Settings so it stops being offered.

**P4. The opportunities, opt-in and afterwards**, once the migration is proven: per-DC stock in
the picker, `minCudaVersion` plus catalog `cudaVersions` replacing `KNOWN_CUDA_VERSIONS` and the
consumer-card heuristic, fail-fast on `ERROR` in `wait_until_running`, `runtime` metrics as a
fallback for the pod telemetry row when the tunnel is down, `/logs` for diagnosing a pod that
never came up, and last-N-days spend from `/v2/billing`. Each is small on its own; none of them
belong in the same change as the transport swap.

**Deletions the migration earns**, worth counting since the module is 943 lines: `deploy_pod`'s
GraphQL translation and `_DEPLOY_MUTATION`, `_graphql` with its browser-User-Agent workaround and
three query constants, `CREATABLE_GPU_IDS` (48 hand-copied ids kept only to document a v1 bug),
`KNOWN_CUDA_VERSIONS` with `allowed_cuda_versions` and probably `is_consumer_gpu`, the
`list_pods_detailed` fallback ladder and both pod normalisers. The net line count should go
**down**.

### Verification: answered on real hardware, 2026-08-20

Everything else in this entry was verified read-only or with deliberately invalid writes. Four
questions needed a real deploy, and one pod answered all four: an **RTX PRO 4500 Blackwell** in
EU-RO-1, created through **`POST /v2/pods`**, read back through v1, GraphQL and v2 inside the same
minute, and terminated immediately. Two cents.

1. **`env.PUBLIC_KEY` still gets SSH into the pod. Yes.** The create body set `env.PUBLIC_KEY` to
   the app's managed key and omitted `startSsh` entirely; `ssh -i` with that key logged in as root
   and ran `nvidia-smi`. So the app's zero-config SSH survives untouched and **nothing needs to be
   registered on the account** (see Traps for why that matters).
2. **`ports: ["22/tcp"]` alone still publishes a direct-TCP endpoint. Yes**, with no
   `supportPublicIp` anywhere: `ssh.direct` filled in with `{host, port, username: "root",
   command}`, matching v1's `publicIp` + `portMappings["22"]` byte for byte on the same pod.
3. **A Blackwell card deploys through v2's POST. Yes**, which closes the question `deploy_pod` was
   created to answer: the v1 create enum's rejection of newer cards is a v1 problem, and v2 needs
   no GraphQL workaround for it.
4. **`mounts.network` mounts the model volume. Yes**, at the `path` given, with the provisioned
   tree (`venv`, `models`, `seedvr2`, `ollama`, `ffmpeg`) present. `path` is required, exactly as
   GraphQL's `volumeMountPath` was.

Four further findings came out of the same hour, and three of them change the plan:

- **v2 reports `status: "RUNNING"` at creation**, in the create response itself, eight polls (about
  50 s) before `ssh.direct` was anything but null and `runtime` anything but null. The richer
  `PodStatus` enum does NOT mean the app can trust the status alone: `wait_until_running` must keep
  requiring an SSH endpoint. A port that only checked `status == "RUNNING"` would hand the run a
  host of `None` and fail somewhere much less obvious.
- **The v1 pod object is worse than assumed**: on the measured pod `machine` came back `{}` and
  there was **no GPU field at all**, so v1 alone cannot even name the card it is billing for. This
  is why `list_pods_detailed` prefers GraphQL, and it is an argument for P2 rather than against it:
  v2 carries `gpu.id`, `dataCenterId` and `cost` in one plain GET.
- **The GPU label is transport-dependent.** The same card was `"RTX PRO 4500"` on GraphQL and
  `"NVIDIA RTX PRO 4500 Blackwell"` on v2, and only the latter is the string a deploy accepts. So
  the label is display-only and must never be matched on. The seam's tests pin this rather than
  pretending the records are identical.
- **RFC 9457 is live and its `errors[]` array is the valuable half.** Measured verbatim: a
  sold-out card is `{"title": "Bad Request", "status": 400, "detail": "There are no longer any
  instances available with the requested specifications. Please refresh and try again."}` and a
  bad enum value is `{"title": "Unprocessable Entity", "status": 422, "detail": "Request
  validation failed.", "errors": ["$.action: value must be one of 'start', 'stop', 'restart',
  'terminate'"]}`. Reading only `error`/`message`, as the client did, would have thrown both away
  and shown the user a bare "HTTP 400 Bad Request" for a card that is simply sold out.

### Traps

- **Never call `PUT /v2/account/ssh-keys`.** It **replaces** the account's registered public keys.
  The app owns a managed key and injects it per pod precisely so it never touches account-wide
  state; writing there would silently clobber the user's own keys and lock them out of every pod
  they own, including ones this app knows nothing about. It is the most destructive call in the
  new API and it is one line away from looking like the tidy solution to Verification #1.
- **Keep sending a `User-Agent`.** Measured: `api.runpod.io/v2` behind Cloudflare answers **403
  Error 1010** to urllib's default `Python-urllib/3.12`, and 200 to the app's own
  `ImageToolbox-RunPod`. The app already sets one on REST and a browser string on GraphQL; the
  point is that the v2 host **is** the GraphQL host, so its Cloudflare rules now apply to calls
  that used to go to `rest.runpod.io`, where they did not.
- **The pass-through spec dict dies.** `deploy_pod` takes a v1-REST-shaped dict and translates it,
  so callers never had to change. v2 rejects unknown properties outright, so that dict must be
  built for v2 or explicitly translated: there is no "extra keys are harmless" any more.
- **Catalog prices are list prices**, and the spec states negotiated account discounts are not
  reflected. Today's `lowestPrice.uninterruptablePrice` behaves the same way, and the pod's own
  `cost` is the authoritative billed rate, so prefer it wherever a real number matters (estimates,
  the funds cap) and treat the catalog as the shopping view.
- **Data-center display names get worse.** GraphQL returns a human `location`; v2 has no such
  field at all and returns `name == id`. The Settings picker labels come from that, so the curated
  `DATACENTERS` labels became the display layer and the API answers membership and capability only
  (`curated_location`). Done in P2, and it turned out to be an IMPROVEMENT rather than a
  workaround: GraphQL called EU-RO-1, EU-NL-1 and EUR-IS-1 all "Europe", where the curated list
  says Romania, Netherlands and Iceland. `storageSupport` is gone too, succeeded by
  `networkVolumeTypes` being non-empty. **The `listed` half of this trap was wrong**: there is
  indeed no `listed` field, but v2 does not need one. Measured 2026-08-20: v2 returns 32 data
  centers to GraphQL's 50, the storage-capable sets are **identical, 18 for 18**, and not one of
  GraphQL's 18 unlisted data centers appears in v2 as storage-capable. v2's catalog is already
  effectively the listed view.
- **`reset` has no v2 equivalent.** The app does not use it. Recorded so nobody goes looking.
- **Sunset headers are announced but not served** (measured, above). Hard-code the dates, treat a
  410 as the signal, and make that 410 reach the user, because it is the one failure a user cannot
  diagnose and the app can name exactly: "RunPod retired the API this version of Image Toolbox
  uses; update the app."

<div align="right"><a href="#future-features">↑ Back to top</a></div>

---

## 24. Make a bug report actionable without asking: Small-Medium

Enrich what `gui.common._issue_url` auto-fills, so a terse report still carries enough to
diagnose. There is already an in-app **Report an issue** path that opens a pre-filled GitHub
new-issue page: it fills the app version, the OS, the Python version, the GPU **name**, and a
"please attach" line pointing at the newest crash log.

**The trigger.** A real report, in full: title "not working", body "the output folders seem
empty", plus the auto-filled `GPU: NVIDIA GeForce RTX 2060`. Nothing there is enough to
answer it, yet **the app knew the answer at the time and threw it away**: an empty output
folder is almost always a completed run that skipped everything as already near the target, or
a tree that has been conciliated (both correct behaviour), and the run summary said so. The
user is not going to write that down. The app can.

The premise: **a user who writes two words is the normal case, not a failure of the user.**
The lever is the automated half, and everything below already exists somewhere in the process.

### What to add, in order of what it would have settled

| # | Field | Settles |
|---|---|---|
| 1 | **The last run's summary per tool**: which tool, when, and its counts (processed / skipped / failed / duration), from the same dict already published to MQTT as `last_run` | "The output folder is empty" in one line, without a round trip. The single highest-value item here |
| 2 | **VRAM total, not just the GPU name** (`sample_gpu` already returns it, and a card name does not imply its memory: the RTX 2060 shipped in 6 GB and 12 GB) | Whether the card is under the 8 GB minimum, i.e. `known-defects.md` D4 |
| 3 | **Install mode** (Local / Remote / Both, from `install_mode.txt`) | Which half of the app is even in play. A Remote-only install has no local GPU stack at all |
| 4 | **The relevant settings for the tool that was last used**: Resolution Target, skip-cutoff, model, "Run on" mode | The most common "not working" is a correct run the settings explain |
| 5 | **The ffmpeg build stamp** (`ffmpeg/build.txt`) and whether `.venv` looks healthy | D1 and the vidstab pin, both of which are invisible from the outside and both of which we have now hit |
| 6 | **The tail of the newest run log, inline** rather than "please attach" | Users do not attach files. Bounded, see the cap below |

### Constraints that shape it

- **The URL cap is the real limit.** A pre-filled new-issue URL must stay under ~8 KB
  (`_MAX_ISSUE_URL = 7800` already encodes this for benchmark contributions). So the body gets
  a **budget**, filled in the order above, and the log tail is what shrinks. The benchmark path
  already has the escape hatch to copy: over the cap, fall back to pointing at a file the app
  wrote to disk.
- **A "Copy diagnostics" button is the other half**, and probably the better one: it writes
  the full block to the clipboard with no cap at all, and it is also what a user can paste into
  a forum, a chat, or an email that never becomes a GitHub issue.
- **Nothing may be sent anywhere.** This is a pre-filled browser form the user reads and can
  edit before submitting, which is the whole reason it is safe to put more in it. That
  property must not be traded away for convenience.
- **Paths carry the user's name and their folder structure.** Include what is needed
  (`ffmpeg/build.txt`'s contents, yes; the full path of every photo folder, no), and never any
  value from `config.local.json` (`config_store.SECRET_FIELDS` is the list: API key, MQTT
  password, notification tokens and webhook URLs, where a webhook id IS the credential).
  Redaction has to be a rule about what is collected, not a filter applied afterwards.
- **Where the last-run summaries come from is a design choice**, not obvious. They are
  currently published and forgotten. Persisting a small ring of them (a few rows keyed by tool)
  is one option; scraping the newest per-tool log file is another and stores nothing new.

### The human half, and the trap in it

A GitHub **issue template** would improve the other side, and the repo has none today
(`.github/` holds only workflows). The trap: GitHub's structured **issue forms** (YAML) and a
`?body=` pre-filled URL do not compose the way you would expect, and the app's whole
auto-filled body depends on `?body=`. Check how a form's fields are pre-filled by query
parameter before committing to one, or the enrichment above quietly stops arriving the day a
template lands.

<div align="right"><a href="#future-features">↑ Back to top</a></div>

---

## 21. Denoising before upscaling: Medium (gated on a measurement, deferred)

Optionally denoise a source before it reaches the model, as a **checkbox** in the Batch
Upscaler (images) and the Video Upscaler (videos).

> **Deferred, 2026-08-19.** The RunPod API v2 migration (#25) takes priority: it has a deadline
> and it affects a shipped, money-spending feature, while this one is still unproven. Deferred, not dropped, and
> nothing here expires.
>
> **Do not build this before the A/B harness reports.** Unlike everything else on this list,
> the *value* here is unknown rather than the cost. SeedVR2 is already a restoration model
> trained on degraded inputs, so denoising first may add nothing, or may remove detail the
> model would have used as evidence. If the answer is "no visible benefit", this milestone
> moves to `dropped-ideas.md` and nothing is built. **That is a successful outcome, not a
> wasted afternoon.**
>
> The harness is written out in full below, in this tracked file, so the procedure survives
> whatever happens to the untracked scratch notes it used to live in.

### The measurement that gates this: the A/B harness

Nothing here can be done from code. It is downloading sample files, producing comparison
sets, and looking at results. It needs no development time and does **not** have to wait for
the work this milestone is deferred behind.

**What does not exist yet is the third leg.** Originals and their upscaled results already
exist; **denoised-then-upscaled** does not, because the denoise stage is not built. The
script below writes the denoised copies as ordinary files, so the shipped Batch Upscaler can
be pointed at them with no code change at all.

#### Read this first: the seed confound

`upscale_engine.upscale()` draws a **fresh random seed for every image**
(`self.args.seed = random.randint(0, 2**31 - 1)`), and there is no setting to pin it. Two
upscales of the same file therefore differ from each other. Consequences:

- **Do not reuse upscaled files from an earlier run** as the "original" leg. They carry a
  different seed lottery than the denoised leg, and the comparison would measure seed
  variation as much as denoising. Re-upscale the originals in the same session, same
  Resolution Target, same model.
- **Judge across the whole set, not per image.** With 20 images a systematic effect separates
  from per-image seed noise; with 3 it does not. If mild denoising helps, it should help on
  *most* of the set, not spectacularly on one.
- Video does not have this problem: the Video Upscaler uses one fixed seed per source video.

#### Step 1: build the test set

About **20 images** in one folder, chosen deliberately across the three degradation types,
because they are unrelated problems and will not have one answer:

| Pick roughly | Type | What it looks like |
|---|---|---|
| 7 | **Sensor noise** | old digicam or high-ISO shots: random speckle, worst in shadows and flat sky |
| 7 | **JPEG artifacts** | heavily compressed or resaved files: blocking in gradients, ringing around edges |
| 6 | **Scan defects** | scanned prints: dust, scratches, paper grain |

The third group is the **control**. No denoiser touches dust and scratches, so if those come
out looking identical across all three legs, the test is working. If they come out
*different*, something else changed and the run is suspect.

Keep the originals outside any folder the app scans, so nothing is picked up by accident.

#### Step 2: produce the denoised copies

Run with the app's venv: `\.venv\Scripts\python.exe make_denoised.py <originals folder>`.
It writes sibling folders `<src>_denoised-mild` and `<src>_denoised-strong` and never
modifies the sources.

```python
"""Denoised copies of a folder of images at two strengths, for the #21 A/B harness."""
import os, sys, time
import cv2
import numpy as np
from PIL import Image, ImageOps

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".tif"}

# (label, h_luma, h_colour) for cv2.fastNlMeansDenoisingColored.
# MILD is what a shipped feature would plausibly default to: enough to lift speckle, not
# enough to erase fine texture. STRONG exists to show the failure mode, so the amount of
# detail at stake is visible if the default were ever set too high.
STRENGTHS = [("mild", 3, 3), ("strong", 10, 10)]

def main(src_root):
    src_root = os.path.abspath(src_root)
    files = [f for f in sorted(os.listdir(src_root))
             if os.path.splitext(f)[1].lower() in IMAGE_EXTS]
    if not files:
        print(f"No images found in {src_root}")
        return
    print(f"{len(files)} image(s) in {src_root}\n")
    for label, h, hc in STRENGTHS:
        out_root = f"{src_root}_denoised-{label}"
        os.makedirs(out_root, exist_ok=True)
        t0 = time.time()
        for i, name in enumerate(files, 1):
            src = os.path.join(src_root, name)
            # Match the upscaler's own loader (EXIF orientation applied, RGB) so the only
            # difference between the legs is the denoising itself.
            with Image.open(src) as im:
                im = ImageOps.exif_transpose(im).convert("RGB")
                arr = np.asarray(im)
            den = cv2.fastNlMeansDenoisingColored(arr, None, h, hc, 7, 21)
            dst = os.path.join(out_root, os.path.splitext(name)[0] + ".jpg")
            Image.fromarray(den).save(dst, "jpeg", quality=98, subsampling=0)
            print(f"  [{label}] {i}/{len(files)}  {name}")
        print(f"  -> {out_root}   ({time.time() - t0:.1f}s)\n")

if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".")
```

**One caveat to carry into the judging:** the script writes JPEGs, so the denoised leg takes
one extra encode the original leg does not. At q=98 with no chroma subsampling that is far
below what denoising changes, but it is not *nothing*, and the real implementation would keep
the image in memory and never write it (decision 2, the #19 prepare pipeline). If a
difference looks marginal, this is one reason to distrust it.

#### Step 3: upscale all three folders

Three Batch Upscaler runs, **in the same session with identical settings** (same Resolution
Target, same SeedVR2 model, same skip-cutoff, auto-straighten in the same state): the
originals, the mild copies, the strong copies.

#### Step 4: judge, and write it down

Open each pair in the app's own comparison window, so the images are seen the way a user sees
them. Four questions:

- **Does the denoised leg lose fine texture** the original leg kept (fabric, hair, foliage,
  skin)? That is the cost.
- **Does the original leg show invented texture** where there was only noise? Flat sky, walls,
  shadow. That is what denoising is supposed to prevent, and the reason for decision 1.
- **Is strong distinguishable from mild?** If not, the effect is small and the feature is
  probably not worth building.
- **Do the scanned prints look the same across all three?** If not, the test is broken.

The deliverable is a CSV in the style of `docs/tag-rename-benchmarks.csv`, e.g.
`docs/denoise-benchmarks.csv`:
`file, degradation_type, best_leg(original|mild|strong), texture_lost(0-3),
invented_noise_texture(0-3), notes`.

That file decides this milestone either way, and if the answer is "no", it is also what stops
the idea coming back in six months.

#### The video half

Same shape, cheaper to judge because the video seed is fixed per source. Two or three
genuinely noisy old clips, one mild `hqdn3d` copy of each:

```
ffmpeg -i "in.avi" -vf "hqdn3d=2:1.5:3:2.25" -c:v hevc_nvenc -preset p5 -rc vbr -cq 16 -pix_fmt p010le -c:a copy "in_denoised.mkv"
```

Then run the Video Upscaler on the originals and on the denoised copies, same target and
engine, and compare in the playback window. Do the post-upscale temporal experiment described
at the end of this milestone in the same sitting: it uses the same clips.

### Settled decisions (conditional on the measurement)

| # | Decision | Why |
|---|---|---|
| 1 | **Denoise BEFORE the model, not after** | After the model, the noise is no longer noise: SeedVR2 reads it as evidence of texture and reconstructs **plausible structure** from it at 4x scale, correlated and edge-consistent. A denoiser then has nothing to key on and can only blur everything uniformly. Cost also scales with output pixels (4-16x more), and the pre-split `-vf` seam already exists |
| 2 | **A checkbox in both upscalers, not a tab** | The seams already exist: for images it is a stage in the prepare pipeline #19 built (decode -> straighten -> **denoise** -> upscale, all on one in-memory array), for video it is a `denoise` flag on `SplitPlan` appending to the same `-vf` chain that already carries `bwdif`. A tab is a whole new surface for an unproven feature |
| 3 | **One implementation, at most two entry points** | Two independently-tuned filter chains spelled the same way will drift. A shared module with a checkbox calling into it is fine |
| 4 | **Fixed conservative `hqdn3d`, no strength UI** | Over-denoising an old tape removes the grain **and** the detail, and the model then invents something else entirely. A conservative default is the honest v1; expose a knob only if the measurement shows people need to tune it |
| 5 | **`nlmeans` is refused outright** | Measured at **0.06x realtime** (79 s for 125 frames of 1080p), i.e. 16x the clip duration, to feed a model that will re-invent the detail anyway |

### Why stabilization (#20) gets a tab and this does not

The distinction is technical, not aesthetic:

| | **Stabilise (#20)** | **Denoise (this)** |
|---|---|---|
| Temporal scope | **Global.** Needs the whole file; per-segment jolts at every boundary | **Local.** A few frames of window, so segment boundaries are a non-issue |
| Fits as a pipeline stage? | **No.** That is the whole finding | **Yes**, into a re-encode that already runs |
| Destructive side effect | **Yes**, ~10-21% of the frame, invisible in the output | None: a filter, reversible by re-running without it |
| Needs per-item review? | **Yes**, hence the per-video lever | No, a conservative default is honest |

### Measured filter costs (1080p, 125 frames, decode + filter + null sink)

| Filter | fps | vs realtime |
|---|---:|---:|
| `removegrain=1` | 266 | 10.7x |
| `atadenoise` (temporal) | 224 | 9.0x |
| **`hqdn3d`** (spatial + temporal) | 199 | **8.0x** |
| `fftdnoiz` | 106 | 4.3x |
| `bm3d` (basic) | 12 | 0.5x |
| `vaguedenoiser` | 22 | 0.9x |
| `nlmeans` | **1.6** | **0.06x** |

Images, CPU, `cv2.fastNlMeansDenoisingColored`: 0.34 s at 0.8 MP, 0.51 s at 3.9 MP, 2.09 s at
12 MP. Negligible against a SeedVR2 upscale either way.

### Things that must be decided as part of building it

- **Turning denoise on forces a re-encode of a video that would otherwise stream-copy**,
  converting a free lossless split into a full transcode whose intermediate is
  `yuv420p` 8-bit. Irrelevant for a noisy VHS capture, but it should be stated rather than
  discovered.
- **Remote-only installs have no `cv2`** (the Remote bootstrap installs pillow, piexif,
  paho-mqtt, python-vlc, matplotlib, certifi). **Decision: serve `cv2` from the RunPod
  network volume**, the same way the volume already caches the Ollama runtime and the SeedVR2
  weights, rather than adding ~40 MB to the Remote bootstrap for a feature most remote users
  may not enable. `provision.sh` is the place; it already does incremental,
  self-pruning provisioning, so this is an addition to an existing mechanism.
- **Three unrelated problems hide under one word**, and they will not have one answer:

  | Problem | What it actually is | Right tool |
  |---|---|---|
  | Sensor noise (old digicam, high ISO) | random per-pixel noise | a denoiser. SeedVR2 may already handle it |
  | JPEG compression artifacts | structured, not random | a deblocker, or nothing |
  | Scan defects: dust, scratches, mould | sparse localised damage | **inpainting**, not denoising |

  The third is what people actually complain about with old photo collections, and no
  denoiser touches it. The A/B set is deliberately built to separate these three.

### The separate experiment worth running at the same time

A mild **temporal** filter applied **after** upscaling would act on the model's *own*
instability rather than on the source's noise. SeedVR2's documented temporal jitter of fine
detail on slow pans (the 4x causal temporal VAE, `docs/video-upscaler.md`) is exactly what a
filter like `atadenoise` is built to suppress, and no pre-pass can touch it because it does
not exist yet at that point. Different feature, different target, same test clips.

<div align="right"><a href="#future-features">↑ Back to top</a></div>

---

## 12. Local+remote mixed queue: Medium
Let a single Video Upscaler queue run some jobs on local GPU(s) AND others on
rented RunPod pods in one Start, instead of the whole run being local **or**
remote.

- **Today's constraint:** the "Run on" switch is one mode for the entire run
  (`_start` branches to `_start_local` for the whole queue, or the remote
  single-/multi-pod path). Per-item GPU binding only distinguishes among
  **remote** cards; a local job stores no GPU (there is one implicit local card).
  As of 0.5.7 the selector is **locked while the queue is non-empty**, so a queue
  can't be half-built in one mode and switched, which is the correct interim
  behaviour until mixing exists.
- **Foundation already in place:** the `(engine, gpu)` queue grouping
  (`job_group_key` / `group_queue_order` / `distinct_group_keys`), the multi-pod
  orchestrator `_start_grouped` (one runner per group), the GPU picker combobox,
  and the per-item GPU column (which now renders the local card as
  "Local <name>", 0.5.7).
- **Work needed:** (a) a local GPU **identity** scheme so a job can bind a
  specific local card (e.g. `local:0` / `local:1` from `nvidia-smi -L`), not just
  an implicit single GPU; (b) let the GPU picker offer local card(s) as bindable
  options alongside live remote cards; (c) a launcher that dispatches **local
  groups to the in-process/subprocess local engine and remote groups to pods,
  concurrently** (the current grouped path is remote-only and serial); (d)
  per-source telemetry rows + estimates that already exist, wired per group; (e)
  scope the funds guard / confirm-before-rent to the **remote** groups only.
- **Clean stepping stone:** **multiple local GPUs within Local mode** alone
  (bind + run local groups on several local cards) is a smaller, self-contained
  first step that exercises (a)+(b)+(c-local) without any remote concurrency.
  Rare on consumer hardware but real (e.g. a multi-card workstation).
- **Risks:** concurrent orchestration of heterogeneous runners (a local
  in-process engine holding the GPU + N remote pods) is more moving parts than
  the current pendulum; a degrading local card (the watchdog) must not stall the
  remote groups; VRAM feasibility is per-card.

<div align="right"><a href="#future-features">↑ Back to top</a></div>

---

## 15. Second remote GPU provider (packet.ai): Medium
Let a remote run rent its GPU from a provider other than RunPod, starting with
[packet.ai](https://packet.ai/), behind a thin provider interface.

> **Blocked on funds, not on design.** The three unknowns below can only be
> answered by signing up and running one real deploy/terminate cycle, and vetting
> the cards costs billed GPU time. See `docs/packet-ai-secondary-gpu.md` for the
> full evaluation (2026-07-14).

- **Why a second provider:** price, stock and region coverage. The app already
  refuses to substitute a GPU type the user did not pick (0.4.0), so when a card
  is sold out in the chosen region the run simply fails and the user re-picks.
  A second catalog is the honest fix for that, and packet.ai's sample pricing
  (RTX 4090 ~$0.39/h, L40S ~$0.92/h, A100 80 GB ~$1.43/h) undercuts RunPod on
  several cards. Its catalog includes the **RTX 6000 Pro 96 GB** already
  benchmarked for video.
- **Why packet.ai and not vast.ai:** vast.ai was investigated 2026-06-23 and
  rejected on billing shape, not on principle: metered bandwidth **both ways**
  (~$40/TB) directly taxes the stream-every-image design, storage is ~5x RunPod's,
  and it has no region-wide network volume. See `docs/dropped-ideas.md`. That
  entry's vetting checklist is the standard packet.ai has to clear: (a) free or
  cheap ingress+egress, (b) cheap region-wide persistent storage that mounts on
  disposable instances, (c) reliable SSH with key injection. On advertised
  behaviour packet.ai clears all three; none is confirmed.
- **Gate before any code (from the evaluation note):** (1) is there a documented
  customer REST API, or is programmatic use CLI-only? (2) can a volume be created
  once and reattached to new pods via API, and is it region-locked? (3) is stock
  on the needed cards reliable, given it is a much smaller provider? Each answer
  changes the interface shape, so the ~15-minute account + `packet gpus --json` +
  one launch/terminate cycle comes first.
- **The known integration risk:** RunPod's GraphQL schema is inspectable
  anonymously, which is how `runpod_client.py` was built at all. packet.ai's API
  reference is login-gated (`dash.packet.ai/docs` returns 403) and the real
  orchestration API underneath is hosted.ai's provider-side REST, which may not be
  fully exposed to customers. So `packet_client.py` may have to **shell out to the
  `packet` CLI** rather than talk HTTP, which is a different seam (subprocess,
  parsing `--json`, a binary to locate) than `runpod_client.py`'s.
- **Work needed:** (a) a provider interface covering what `remote_run` actually
  uses (list GPUs with live price/stock, deploy with an injected public key and a
  mounted volume, inspect, terminate, account balance); (b) `packet_client.py`
  behind it, HTTP or CLI-backed; (c) a provider selector in the GUI plus
  per-provider credentials in `config_store.SECRET_FIELDS`; (d) provisioning the
  model volume a second time on the new provider (`provision.sh` is portable, the
  volume lifecycle is not); (e) the funds guard reading a second balance API.
- **The largest lift is the GUI, not the client.** Provider choice touches the
  RunPod tab, the per-tab GPU pickers, the cost estimator's rate tables, the
  benchmark corpus keys (a card's rate is per provider once prices differ), and
  every "is this remote" branch. Scope it deliberately: a first version that
  supports packet.ai for **video only** (one tab, one flow) is far cheaper than
  making every remote path provider-aware at once.
- **Risks:** a second provider doubles the surface that can break silently at a
  distance (stock, pricing, API drift) on a vendor stack one layer deeper than
  RunPod's. The dead-man's switch, worker, streaming engine and resume logic are
  all provider-agnostic already, so the blast radius is the control plane only.

<div align="right"><a href="#future-features">↑ Back to top</a></div>

---

## 3. HTTP interface: Hard (low priority)
Spin up a small HTTP server with a UI that mirrors the application UI.

- **What "mirror" implies:** rebuilding the thumbnail wall, two-row live status,
  progress/ETA, pause/resume/stop, and Settings as a web app, plus a backend
  and live updates (WebSocket/SSE).
- **Reuse:** the subprocess + stdin/stdout protocol is a clean backend seam; a
  server can drive the same scripts the GUI does.
- **Work needed:** an HTTP server (stdlib `http.server` is too thin for this,
  so realistically a small framework), a streaming channel for live
  progress/thumbnails, and a full second UI to maintain alongside the tkinter
  one.
- **Risks:** large, ongoing surface area (two UIs to keep in sync); auth/binding
  concerns if exposed beyond localhost.
- **Scope note:** a minimal "status + start/stop" web panel is far cheaper than
  a true mirror and worth considering first.

<div align="right"><a href="#future-features">↑ Back to top</a></div>

## 4. Unraid Community Apps integration: Hardest (low priority)
The user installs and runs the application on their Unraid server.

> **Status: deferred.** The app stays Windows-only for now: there is no Linux
> GPU server to target. Revisit only if that changes.

- **Why it's the hardest:** this is a distribution/packaging effort on top of a
  Linux port, not a discrete code feature. The app is Windows-bound (tkinter
  GUI, PowerShell `bootstrap.ps1`, `%USERPROFILE%`/`.venv\Scripts` paths,
  `CREATE_NO_WINDOW`). Unraid runs headless Docker on Linux.
- **Requires:** the HTTP interface (#3) for any UI; a Linux build of the
  pipeline; the NVIDIA Container Toolkit for GPU passthrough; a Dockerfile
  replacing `bootstrap.ps1`; and a Community Apps template XML.
- **What helps:** the heavy lifting (PyTorch/CUDA, SeedVR2, Ollama-over-URL) is
  already cross-platform; only the shell/GUI/packaging layers are Windows-only.

<div align="right"><a href="#future-features">↑ Back to top</a></div>

---

## Sequencing & dependencies

- **#1, #2, #5, #6, #7, #8, #9, #10, #11, #13, #14, #16, #17, #18, #22 and #23 are complete**
  (remote upscaling + funds-floor; RunPod video; video conciliation; self-healing remote runs;
  local video; benchmark sharing; telemetry usage graphs; Home Assistant dashboard samples;
  Real-ESRGAN engine; metadata copy + backfill; the comparison lens; derived-directory
  pruning; skipping image variants the pipeline cannot round-trip; Conciliation Undo; browsing
  already-upscaled images; the Video Stabilization workflow), so the remaining sequencing is
  only among the open milestones below.
- **Open milestones: #25, #24, #21, #12, #15, #3, #4.**
- **#25 (RunPod API v2) is the only milestone with a deadline, and it comes in two.** REST v1
  returns 410 on **2026-11-15** and GraphQL in **early 2027**, and the app ran on both at once:
  pod lifecycle and volumes on v1, pod *creation* plus every live GPU/DC picker on GraphQL. Its
  **P0 landed on 2026-08-20**: a normalisation seam in `runpod_client` plus the first
  `tests/test_runpod_client.py`, pinned with payloads recorded off a real pod read through all
  three transports. It had no deadline and went first anyway, because the migration's real hazard
  is renamed response fields reading as None and failing **silently, toward spending money** (a
  second billed pod, an auto-resume supervisor that quits on every blip, a session cap that never
  trips). **P1 and P2 landed the same day**, well ahead of both dates: `runpod.api_version` now
  selects a whole stack (v2 REST + v2 catalog by default, or v1 REST + GraphQL as a config-only
  escape hatch), and the GPU and data-center lists came back byte-identical from both. The
  GraphQL code is still reachable on the v1 branch and is deleted on its own deadline, not now, an
  escape hatch missing half its code being no escape hatch. What is LEFT has no v2 answer at all:
  the **account balance** the funds-guard floor is built on, so decide its degradation
  deliberately rather than discovering it on the day GraphQL stops.
- **#24 (richer bug reports) is independent of everything else and is the cheapest
  item on this list.** It touches one function (`gui.common._issue_url`) plus wherever the
  last-run summaries end up coming from, and it pays off on the NEXT bad report rather
  than at some later milestone. It is also the only entry here whose value grows with the
  number of users, so it is worth doing before a release rather than after one.
- **#21 (denoise) inherits #19's prepare pipeline**, which is built and in use: a RAW is
  decoded into an in-memory image, straightened in memory, and written to **exactly one**
  lossless temp only when it is actually upscaled (`batch_upscale._write_upscale_input`,
  `orientation.analyse_image`). Denoise slots in as a stage on that array, before the temp.
  The rule that matters is already enforced there and must not be relaxed: **no JPEG temp**,
  because it would spend a generation of quality before SeedVR2 sees a pixel.
- **#21 (denoise) is gated on the A/B harness and may never be built at all**, and is
  **deferred** behind the RunPod API work (#25) either way (2026-08-19). It is the only open
  milestone whose *value* is unknown rather than its cost. Do not start it before the
  measurement; a "no visible benefit" result moves it to `dropped-ideas.md`, which is a
  successful outcome. The harness itself needs no development time and does not need to wait
  for the RunPod work: it is an afternoon at the keyboard, and running it early is what keeps
  the deferral from turning into a re-litigation later.
- **#20 (Video Stabilization) shipped in 0.6.0** and cost one thing nobody predicted: it
  forced the app-wide **ffmpeg pin off the 8.1 release branch onto master**, because every
  8.1.x corrupts memory in `vidstabtransform`. Anything else built on a less-travelled ffmpeg
  filter should assume the same risk and measure the filter's *determinism* early, not just
  whether it runs.
- **#23 (Video Stabilization workflow) shipped in 0.6.0** and settled the one question it
  had been holding open: a stabilised output is **not** recorded as lineage, so Conciliation
  can never act on it. That precedent generalises - **a new pairing is not automatically a
  lineage row**, and any future "record what came from what" should ask whether the app's
  one destructive tool should be allowed to see it before choosing where it lives.
- **#12 (mixed local+remote queue)** is a medium, self-contained Video Upscaler feature that
  builds on the shipped `(engine, gpu)` grouping; #3 and #4 are lower priority and larger,
  each introducing a new process model, networking, or packaging. With Home Assistant already
  done over MQTT, the old telemetry coupling no longer drives sequencing.
- **#15 is gated by spend, not by other features.** It needs a paid account and
  billed GPU time to answer three questions no public page answers, so its
  ordering is set by when that spend happens, not by #12. Nothing else
  depends on it, and it does not depend on anything else. Note the overlap with
  #12: both add a dimension to "where does this job run", so whichever lands
  second inherits the other's grouping/selector work (a job would then carry
  engine + provider + GPU).
- **#12 has a clean stepping stone** (multiple local GPUs within Local mode)
  that can land first without any remote-concurrency work.
- **#4 depends on #3** (headless Unraid needs a web UI).
- **Follow-on from the shipped #6/#7:** generalise the Auto-resume supervisor from
  video to the image runners (batch upscale / tag) (not yet scheduled).
- **Follow-on from the shipped #8 (not yet scheduled): extend benchmark sharing
  to the IMAGE tasks.** Today the crowdsourced corpus covers `db.video_bench`
  only; per-card image throughput (`db.gpu_perf` for batch upscale and tag) is
  still served solely by the author-maintained `docs/image-benchmarks.csv`, so a user
  picking a remote GPU for an image run gets the author's numbers or nothing.
  The transport, CSV format, local-precedence import and maintainer merge tool
  are all reusable as-is; the work is deciding the shared row's identity for a
  task whose unit is an image, not a (target x compile x tile) cell, and keeping
  it out of the accumulating `gpu_perf` store on import (see
  `docs/dropped-ideas.md`). See `docs/benchmark-sharing.md`.
- **Architectural watch-item:** the app is dependency-light and Windows-only. #3
  and #4 each push toward extra packages, a long-running server, and
  cross-platform support, so adopt those deliberately.

<div align="right"><a href="#future-features">↑ Back to top</a></div>

---

## Shipped milestones (numbering legend)

Roadmap **#1, #2, #5, #6, #7, #8, #9, #10, #11, #13, #14, #16, #17, #18, #19, #20, #22 and
#23** are done and live. **This section is a pointer list, not a record.** Each entry says what the
number meant and where the design of record actually lives; nothing is described in full
here. The numbers survive because code and other docs cite the roadmap by them (`remote
#1`, `Video Upscaler #2`, `local #7`), so deleting the entries outright would strand those
references.

When a milestone ships, its rationale moves to the document that owns the feature and the
entry here shrinks to one of these lines. That rule is the point of the section: a design
kept in two places drifts, and the stale copy is the one that gets read.

- **#1: Remote upscaling (RunPod).** Shipped 0.3.1-0.4.2. The Batch Upscaler and Tag &
  Rename on a rented pod: disposable pod, resident streaming worker, dead-man's switch.
  See `CLAUDE.md` (Remote upscaling) and `docs/runpod-notes.md`.
- **#2: Video upscaling (experimental).** Shipped 0.4.x. The Video Upscaler:
  probe / split / stream / reassemble on a rented pod, with segment-level resume.
  See `CLAUDE.md` (Video Upscaler) and `docs/video-upscaler.md`.
- **#5: Video conciliation.** Shipped 0.5.1. Conciliation matches and replaces VIDEO
  originals alongside images, by content-hash lineage only (no name fallback, so a partial
  clip can never be taken for a whole-video match). See `CLAUDE.md` (Conciliation) and
  `conciliate.py`.
- **#6: Self-healing remote runs.** Shipped 0.5.0, video only. An opt-in Auto-resume
  supervisor survives losing the pod mid-run: reconnect a blip, or wait for the identical
  card and redeploy. See `CLAUDE.md` (Video Upscaler) and `docs/video-upscaler.md`
  section 17.
- **#7: Local video upscaling.** Shipped 0.5.0. The same SeedVR2 video work in-process on
  the user's own GPU, with a predictive VRAM sizer, a per-card benchmark and optional
  `torch.compile`. See `docs/local-video-upscaler.md`.
- **#8: Benchmark sharing.** Shipped 0.5.1. The per-card video benchmark as a crowdsourced
  corpus: pulled from GitHub at launch, contributed back through a pre-filled issue, curated
  by a maintainer `--merge` tool. See `CLAUDE.md` (Benchmark sharing) and
  `docs/benchmark-sharing.md`.
- **#9: Telemetry usage graphs.** Shipped 0.5.3. A per-run usage-graph window behind each
  telemetry row, one shared instance per source. See `CLAUDE.md` (Telemetry usage graphs)
  and `docs/telemetry-design.md`.
- **#10: Home Assistant dashboard samples.** Shipped 0.5.3. Ready-made Lovelace dashboards
  over the MQTT topics the app already published; docs and samples only, no pipeline change.
  See `samples/home-assistant/` and `docs/mqtt-integration.md`.
- **#11: Real-ESRGAN engine.** Shipped 0.5.6. A second video engine (a fixed-ratio 2X/4X
  GAN) local and remote, plus the general queue change it rides on: per-item GPU binding +
  grouped multi-pod Start. See `CLAUDE.md` (Real-ESRGAN engine cluster),
  `docs/video-upscaler.md` section 18 and `docs/local-video-upscaler.md` section 23.
- **#13: Copy metadata from the original.** Shipped 0.5.9. 13a writes the source's metadata
  onto the upscaled file wherever it is written; 13b backfills the already-upscaled backlog
  inside Conciliation, at the last moment both files exist. See `CLAUDE.md` (Metadata
  carried across) and `tests/test_exif_copy.py`.
- **#14: Hover magnifier ("lens view").** Shipped 0.6.0. Both comparison windows magnify
  the patch under the pointer as original AND upscaled side by side, at the real upscale
  ratio, with a wheel-zoomed and pinnable lens. See `CLAUDE.md` (Comparison) and
  `tests/test_lens_view.py`.
- **#16: Derived directories must not be re-scanned as input.** Shipped 0.5.9. One shared
  name rule prunes the app's own output folders (`__upscaled__`, `__Archive__`,
  `.imgtbx_video`) from every input walk. See `CLAUDE.md` (Derived-directory pruning) and
  `tests/test_derived_dirs.py`.
- **#17: Skip image variants the pipeline cannot round-trip.** Shipped 0.5.9. Transparency,
  several pages and 16-bit depth are detected from the header and skipped with a named
  reason, in the Batch Upscaler and in Conciliation (which checks the ORIGINAL, so the
  protection is retroactive). See `CLAUDE.md` (Image variants left as-is) and
  `tests/test_image_variants.py`.
- **#18: Conciliation Undo.** Shipped 0.5.9. Every file action is journalled before it
  happens, and an archive run can be reversed from that journal; a delete run is refused
  rather than attempted. See `CLAUDE.md` (Conciliation Undo) and
  `tests/test_conciliate_undo.py`.
- **#19: RAW and DNG input.** Shipped 0.6.0. The Batch Upscaler accepts ten RAW formats and
  renders each to a viewable JPEG, from the camera's own embedded preview where there is one
  and a LibRaw demosaic where there is not. Two findings are worth knowing before touching it:
  a RAW is **never eligible for upscaling** at the shipped target (measured 0 of 24, which is
  why it is exempt from the size skip and renders regardless), and a RAW extension must
  **never reach Pillow**, which answers confidently and wrongly for a TIFF/EP container. So
  what shipped is in practice a **RAW renderer**, and the upscale half is scaffolding waiting
  for a target high enough to make a RAW small - see the 8K revisit trigger in
  `docs/dropped-ideas.md`. See `CLAUDE.md` (RAW and DNG input),
  `docs/raw-preview-survey.csv` (the measurement) and `tests/test_raw_input.py`.
- **#20: Video Stabilization (new tab).** Shipped 0.6.0. A tab after Conciliation that
  steadies ONE shaky video into one new file with two-pass `vidstab`: no GPU, no pod, no
  network. It defaults to `optzoom=0` + `crop=keep` rather than the `optzoom=1` every ffmpeg
  tutorial copies, because that default discards a measured ~17-21% of the picture and the
  amount is set by the single worst jolt in the clip. The thing to know before touching it:
  **every ffmpeg 8.1.x corrupts memory in `vidstabtransform`** (fixed upstream by
  `316531e61cf`, on master, not on `release/8.1`), usually with no crash at all - just
  different pixels on every run - which is why `bootstrap.ps1` pins a master build and why
  the tool runs a determinism self-test before it will process anything. See `CLAUDE.md`
  (Video Stabilization) and `tests/test_video_stabilize.py`.
- **#22: Browse already-upscaled images.** Shipped 0.6.0. A **Browse upscaled…** window
  pairs an output tree back to its originals long after the run ended, by inverting the
  upscaler's own mirror. See `CLAUDE.md` (Browse upscaled) and
  `tests/test_browse_upscaled.py`.
- **#23: Video Stabilization tab improvements.** Shipped 0.6.0, all six items. The workflow
  around #20's foundation: a folder loader and a queue of whole-file jobs, a hand-off from
  the Video Upscaler's scan list, playback-first comparison, "Save as Default" on both
  folder fields, and a pair record. Two decisions are worth knowing before touching it.
  **The queue does not reverse #20's "not a batch tool" finding** - that finding is about
  the ALGORITHM being whole-file, and N independent whole-file jobs preserve it exactly, so
  nobody should later "simplify" the queue into segmenting one video. And the pair record
  **is deliberately not a lineage row**: `db.lineage` is what Conciliation matches on, and
  video conciliation is lineage-only, so a row there would make the app's one destructive
  tool offer to replace originals with stabilised copies. It lives in `db.stab_pairs`,
  which no conciliation query reads. The item-5 sub-decision that was left open ("may
  Conciliation ACT on it") was answered **no**, explicitly and with a test. See `CLAUDE.md`
  (Video Stabilization) and `tests/test_video_stabilize.py`.

<div align="right"><a href="#future-features">↑ Back to top</a></div>

---

## Decided against / constraints

Moved to **`docs/dropped-ideas.md`**: the Video Upscaler pause, the region
pre-seed, the deferred local-engine install, parallel jobs (an image tool
alongside the Video Upscaler), the automatic-telemetry half of benchmark
sharing, UI localization, a light/dark theme, background removal, and the
standing constraints (AMD/ROCm, vast.ai as a second provider).

<div align="right"><a href="#future-features">↑ Back to top</a></div>
