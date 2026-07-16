# packet.ai as a secondary remote GPU source (reference-only)

**Status: reference / keep-in-mind. Not planned, not scoped for a release.**
This note records what was found while evaluating [packet.ai](https://packet.ai/)
as a second remote-pod backend alongside RunPod, so the evaluation doesn't have to
be redone from scratch later. Nothing here is committed to.

## What packet.ai is

Not an independent cloud: it is a **reseller storefront running on
[hosted·ai](https://www.hosted.ai/)'s "neocloud" software stack** (their own site
says "powered by hosted·ai"). The open-source dashboard
[`hosted-ai/packet-oss`](https://github.com/hosted-ai/packet-oss) is a Next.js
frontend that proxies every GPU action to `HOSTEDAI_API_URL` with a
`HOSTEDAI_API_KEY`. So the real orchestration API is **hosted.ai's provider-side
REST API**; packet.ai wraps it in a CLI plus a `PACKET_API_KEY`. Practical
consequence: it is a younger, thinner layer than RunPod, and the deploy path is one
vendor deep (packet.ai on top of hosted.ai).

## Fit against our remote-pod contract

Our remote-pod cluster (`runpod_client.py` / `remote_run.py` / `ssh_setup.py`)
needs three things from any backend. packet.ai's status on each:

| Requirement | RunPod (today) | packet.ai (as advertised) |
|---|---|---|
| **Network / model volume** (write once, reattach to every pod, region-locked) | Region-locked network volume, GraphQL `volumeMountPath` | **"Shared Volumes: attach persistent volumes to any instance"** + block storage ($7.20/mo per 100 GB) + S3-compatible object storage. Matches the "models once, mount everywhere" pattern. **Region-locking + API-driven reattach unconfirmed.** |
| **Easy programmatic deploy** (own SSH key, create/terminate, JSON) | GraphQL `podFindAndDeployOnDemand`, `PUBLIC_KEY` injection | **SSH:** full root with *your own* key, auto-injected (`ssh root@…packet.ai`). **CLI:** `packet login \| gpus \| launch --gpu <t> --setup <preset> \| ps \| ssh <id> \| terminate <id>`, `--json` output, `PACKET_API_KEY` env, explicit CI support. Maps cleanly onto our `ssh_setup` key model. |
| **Live GPU catalog / stock / price query** (our `available_gpus`) | GraphQL live list, per-region stock + price | **`packet gpus` / `--json`** returns types + real-time pricing. **No GraphQL.** Per-region stock granularity unconfirmed. |

## The one real integration risk

Unlike RunPod, whose GraphQL schema is inspectable anonymously (how our whole
`runpod_client` was built), **packet.ai's API reference is gated**:
`dash.packet.ai/docs` returns 403 (Cloudflare / login), and the underlying spec is
hosted.ai's provider API, which may not be fully exposed to end customers. So the
endpoint shapes can't be reverse-engineered before signing up. A `packet_client.py`
sibling to `runpod_client.py` is plausible, but might have to shell out to the
`packet` CLI (a subprocess seam) rather than talk HTTP directly, depending on what
the customer REST surface actually exposes.

## GPU catalog

Narrower than RunPod, but covers what we care about:
B200, H200, A100, H100, L40S, **RTX 6000 Pro 96 GB (Blackwell)**, RTX 4090, RTX
5090. The RTX 6000 Pro is the card benchmarked for video (measured 1440p ceiling on the
7B model, 4K infeasible); the 4090/5090 (24/32 GB) are cheap tag-mode candidates. Sample pricing seen: RTX 4090 ~$0.39/h, L40S
~$0.92/h, A100 80 GB ~$1.43/h, B200 from ~$3.75/h.

## Before any real work, verify (none answerable from public pages)

1. Is there a documented **customer REST API** (create / terminate / list /
   attach-volume), or is programmatic use **CLI-only**?
2. Can a **network volume be created once and reattached to new pods via API**, and
   is it **region-locked** like RunPod's?
3. **Stock reliability** on the specific cards we need. It is a small provider;
   availability may be thinner than RunPod's Secure Cloud.

A free account + one `packet gpus --json` and one `packet launch` / `terminate`
cycle (~15 min) answers all three and is the cheapest way to de-risk.

## Sources

- packet.ai: main / features / cli pages, `dash.packet.ai/docs` (login-gated)
- hosted·ai platform page (the underlying stack)
- `hosted-ai/packet-oss` on GitHub (dashboard + CLI; shows the hosted.ai API dependency)
- getdeploying.com/packet-ai (pricing / storage tiers)

_Evaluated 2026-07-14 against RunPod as the primary backend._
