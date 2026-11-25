# MR CARE Koya

Koya is a Malayalam-first receptionist backend. The root FastAPI package handles product, warranty and approved-price lookups, records confirmed pending service requests, and provides private CRM APIs for customers, cases and interactions in SQLite.

## Run locally

Run these commands from the repository root using Python 3.12, the CI target.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8001
```

Use [.env.example](.env.example) as the reference for local configuration. Set separate random secrets for the read and write tools. The Sarvam API key is needed only for the chat smoke test. Keep the main application on loopback and out of the public tunnel or reverse proxy. Where a trusted TLS proxy is used on a private staff network, set `MRCARE_STAFF_AUTH_REQUIRED=1` before starting the main app. Loopback mode trusts the socket peer, so it is intended for direct local access only.

| App | Port | Routes |
| --- | --- | --- |
| `app.main:app` | 8001 | `GET /health`, `GET /readyz`, `POST /api/test-sarvam`, `POST /api/products/lookup`, `POST /api/prices/lookup`, `POST /api/requests`, `GET /api/demo/requests`, legacy `POST /api/voice-tool`, and `/api/crm` routes |
| `app.public_api:app` | 8002 | `GET /healthz`, `GET /readyz`, and `POST /api/voice-lookup` with `X-MRCARE-Tool-Key` |
| `app.public_write_api:app` | 8003 | `GET /healthz`, `GET /readyz`, and `POST /api/voice-request` with `X-MRCARE-Write-Key` |
| `app.voice_events_api:app` | 8004 | `GET /healthz`, `GET /readyz`, and authenticated Sarvam events under `/api/sarvam/inbound` |
| `app.whatsapp_events_api:app` | 8005 | Meta webhook, public case verification, `GET /healthz`, and `GET /readyz` |

Start the main or write app first on a fresh demo database. Both initialize a new demo database, which includes a fictional air fryer. The read app opens the same database for lookups and its readiness endpoint returns HTTP 503 until the schema is available. The seeder and rename scripts remain separate. See the [demo catalog](DEMO_DATA_CATALOG.md) for fixtures and dry-run commands. The bundled patches and nested `aivoice/` are historical. Use the root package and avoid reapplying the patches.

## Upgrade an existing SQLite database

Stop all processes and scripts that use the configured file. First rehearse the command against a copy in a private directory. Migrations 1 and 2 track the legacy schema and create staff users and sessions. Migration 3 adds CRM identity, cases, history, conversations, tokens, and verification tables. It also indexes `products.customer_id`. Migration 4 creates the technician profile, session, coverage, skill, hours, and leave tables plus dispatch requirements, appointment preferences, assignments, appointments, job notes, audit, and notifications. Existing customer, product, price, request, staff, and CRM rows remain intact. Verify the source path and select a new private backup filename. Keep the services stopped until row counts, key historical records, and readiness have been checked.

```powershell
.\.venv\Scripts\python.exe -m app.admin_cli migrate --backup .\data\before-foundation.sqlite3
```

The command creates and integrity-checks a SQLite backup before applying transactional schema changes. To recover, stop all database users, restore the verified backup into a separate filename, point `MRCARE_DB_PATH` at that file, and run the pre-upgrade application. Preserve the affected database and its sidecar files for diagnosis.

## Staff access and tool contracts

The foundation supports short-lived, revocable owner bearer sessions provisioned by a trusted local operator. This is intended for a private API client. Browser sign-in and additional role scopes are future work.

```powershell
.\.venv\Scripts\python.exe -m app.admin_cli issue-owner-session owner
.\.venv\Scripts\python.exe -m app.admin_cli revoke-owner-sessions owner
```

Use the issued value as `Authorization: Bearer <token>` over trusted TLS. It expires in one hour. Store it securely and avoid recording the CLI output or authorization header in logs.

New writes across the three routes require customer confirmation, interaction ID, a valid Indian mobile, product and issue details, and a date or time preference for follow-ups. All outcomes are pending. The separate webhook continues to require a confirmation summary. Existing action-based fallback keys still support legacy clients. For more than one confirmed request in the same call, send a different stable `idempotency_key` per confirmed operation and reuse it on retry. A key reused for a different request returns HTTP 409.

Ambiguous product matches return `found: false`, `ambiguous: true`, and a request for the serial number. The unknown-product response remains “Product master will be added.” The voice greeting is **നമസ്കാരം, MR CARE-ലേക്ക് സ്വാഗതം. എങ്ങനെ സഹായിക്കാം?**

## Phase 2 CRM

The main app exposes its private CRM contract in local Swagger UI at `/docs`. Staff routes under `/api/crm` always require an owner bearer token. Directory endpoints use the last returned numeric ID as the next `after` cursor.

- Create a customer with a stable `operation_key` for retries. Enroll a mobile with a completed OTP challenge. Existing customer phone fields remain unverified. Resolve a known verified mobile and matching name for staff use. Mismatches require review. Asset attachment requires an explicit customer, asset ID, and matching serial and refuses reassignment.
- Create a case with documented customer confirmation and a stable operation key. A supplied asset must belong to that customer. Asia/Kolkata numbering uses `DDMMYYNNN` with up to 999 references per day. Case states in this phase are pending, accepted, rejected, and cancelled. Transitions require the current version and a reason.
- Store a structured interaction for a case using the source channel, provider, provider account, and external ID. A retry with the same facts returns the original interaction ID. An interaction linked to another case returns HTTP 409. Internal notes and facts remain visible to authorized staff.
- Issue a case token through the staff API. Tokens expire after 30 days. Issuing a replacement revokes the old token. Resuming a case requires the reference, its token, a verified contact, and a one-use OTP that expires after five minutes. The response contains only that case's reference, status, description, and update time.

Contact enrollment and case resumption need a separately configured `MRCARE_CRM_OTP_SECRET` and an approved delivery adapter. The default adapter is disabled and returns HTTP 503 on challenge initiation. The tests inject a fake adapter. Keep the main application private. Existing Sarvam webhook writes continue to use the demo request workflow.

## Technician portal and dispatch

Owners manage dispatch from the `/api/dispatch` routes in local Swagger UI at `/docs`. Technicians sign in at `/technician` and use `/api/technician`, which is scoped to their own account. Provision individual one-hour tokens with `POST /api/dispatch/technicians/{id}/session`. Use a trusted private connection and share each token only with its technician through an approved channel. Portal sign-out revokes the current token. Owners can revoke all sessions for a technician.

- Set each unique technician code, coverage PIN codes, skills, weekly Kolkata working hours, leave, and workload limit. Configure a case's PIN and required skill with the assign endpoint. If the case is pending, acceptance triggers matching. For an accepted case, the endpoint matches immediately.
- Matching requires an active account, exact PIN and skill coverage, current availability, and spare workload. It ranks by current workload, then technician code. The dispatch queue prioritizes urgent unassigned cases. When nobody qualifies, the case stays unassigned and dispatch receives an in-app notification. Owner selection requires a reason and checks the same eligibility rules.
- A technician can accept or decline a 30-minute offer. Declines trigger reassignment. An owner can release an assignment with a reason and then assign again. Run `POST /api/dispatch/process-timeouts` from approved local scheduling or manually to process expired offers and generate upcoming appointment reminders. No scheduler is installed automatically.
- Save a customer preference separately from the booking. After technician acceptance, browse available Kolkata slots and reserve a future interval. Booking and rescheduling check hours, leave, and concurrent bookings in the same SQLite write transaction. Cancellation and no-show are recorded separately.
- The portal supports acceptance, decline, visit status changes, inspection and parts entries, job notes, escalation, and in-app notifications. Customer visit contact appears only after booking. CRM acceptance and dispatch progress are tracked separately, with the dispatch detail endpoint showing the visit and audit history. Completion closes the confirmed appointment.

The portal supports in-app notifications, reminders, and estimate submission from the approved price book. Browser account recovery and photo uploads require later integrations.

## Call centre and Sarvam

Owners can review `/call-centre` using a staff session. The board follows committed events and polls every ten seconds when the stream is unavailable. A fresh start-only record is a **start observed**, and after 15 minutes it becomes stale. Missing end events, ring state, and queue depth cannot be inferred. The existing product, price, and callback tools stay on their current applications.

For an approved deployment, start the isolated event app with `python -m uvicorn app.voice_events_api:app --host 127.0.0.1 --port 8004`. Expose only the approved event paths over HTTPS. Keep the main app private. Use one configured inbound app and deployment per event process and separate event, media, and tool secrets.

| Purpose | Route and provider mapping |
| --- | --- |
| Conversation hooks | `POST /api/sarvam/inbound/hooks/start` and `/end`. Configure Sarvam's `on_start` and `on_end` HTTP tools with static `X-MRCARE-Voice-Hook-Key`. Map Interaction ID to `interaction_id`. Supply an offset-bearing full timestamp as optional `event_at`, or the backend records receipt time. |
| Signed completion | `POST /api/sarvam/inbound/complete`. Use a stored Sarvam webhook config and its signing secret. Configure the expected app and deployment IDs. The backend verifies the raw payload HMAC and timestamp, deduplicates by app and interaction, and prevents late hooks from reopening completed calls. |
| Human escalation | `POST /api/sarvam/inbound/escalations` with the hook key, `interaction_id`, and a predefined category. The staff queue records a request. Actual transfer uses Sarvam's core tool on supported telephony and requires separate verification. |

Migration 5 adds `voice_calls`, `voice_event_inbox`, `voice_audit`, `voice_recording_metadata`, `voice_permissions`, `voice_followups`, and `voice_escalations`. Apply the same backup-first upgrade procedure above. Existing business and staff rows remain intact.

Link calls with an owner-reviewed case association. Phone matches and agent outputs are insufficient for identity. The webhook keeps metadata and transcript availability. Documented media permission allows owners to fetch transcripts and filtered trace outcomes from the workspace. For WAV retrieval, request a 60-second grant at `POST /api/call-centre/calls/{id}/recording-grant`. Fetch the returned path with the same bearer session and send its fragment value in `X-MRCARE-Media-Grant`. This keeps the grant outside access URLs and logs. By default, Sarvam holds the recordings and the database stores metadata and a checksum. Phase 7 adds optional private recording storage. Review access and retention policy before production.

### Happy Calls

Record explicit outbound permission for a verified CRM contact and queue a Happy Call only for a confirmed visit. The owner-only brief reads the confirmed appointment and technician and includes a current approved estimate when available. Submission rechecks permission and appointment version. Authenticated Sarvam Analytics reconciliation requires an exact attempt ID and contact match. A timeout produces an uncertain state requiring human review. A connected attempt and a customer informed outcome are separate.

Real outbound is disabled by default. Enable it only after separate owner approval, a verified dedicated agent, authorized telephony, and an approved process for proving identity and giving the agent case-scoped context. The Instant Outbound adapter uses documented dial fields. This checkout does not transmit the private owner brief to the agent or verify live workspace context mapping. The optional Phase 7 worker supports read-only reconciliation. Outbound call submission remains an explicit owner action.

### Malayalam voice QA before provider rollout

Use synthetic fixtures. Record the tested agent revision, selected voice, and outcome. Start in the Sarvam playground and obtain separate approval for live telephony tests.

| Scenario | Evidence to capture |
| --- | --- |
| Greeting and voice choice | The greeting is **നമസ്കാരം, MR CARE-ലേക്ക് സ്വാഗതം. എങ്ങനെ സഹായിക്കാം?**. A Malayalam reviewer compares supported voices if the pronunciation sounds Tamil-influenced. |
| Model, serial, address, and PIN | The agent confirms unclear letters or digits, code-mixes naturally, and reuses details already supplied. |
| Mobile validation | Valid ten-digit and `+91` forms pass. An incomplete number gets a reprompt and no write. |
| Lookup and cost | Unknown and ambiguous products, optional warranty numbers, and unavailable prices produce the existing safe results. |
| Confirmation and interruption | The agent handles interruption without repeating known questions, avoids the default water-damage power-on question, and writes only after confirmation. Pending requests remain pending. |
| Escalation and Happy Call | Record transfer and fallback outcomes. Verify identity before sharing case details. Require evidence before marking the customer informed. |

[Sarvam hooks](https://docs.sarvam.ai/voice-agents/build/on-start-on-end-hooks) · [Inbound webhook](https://docs.sarvam.ai/voice-agents/api/deployments/webhooks/webhook-payload) · [Analytics API](https://docs.sarvam.ai/voice-agents/api/analytics/overview) · [Instant Outbound](https://docs.sarvam.ai/voice-agents/api/instant-outbound/create) · [Call transfer](https://docs.sarvam.ai/voice-agents/build/tools/call-transfer)

## WhatsApp and case continuity

Migration 6 adds the WhatsApp inbox, consent history, verification bindings, handoffs, outbound queue, receipts, rate counters, and a shared channel claim. It adds a channel field defaulting to `crm` to the existing OTP challenge table. Run the backup-first migration procedure above before upgrading an existing SQLite deployment.

For an approved test deployment, run `python -m uvicorn app.whatsapp_events_api:app --host 127.0.0.1 --port 8005 --no-access-log`. Terminate TLS at the approved ingress and omit webhook verification query strings, request bodies, and authorization headers from all logs. The Meta webhook path is `/api/whatsapp/webhook`. Use the expected WABA ID and business phone-number ID. Set separate webhook verification and Meta app secrets. Keep the private owner operations on the main app.

- **Incoming messages.** The webhook acknowledges after verifying the raw-body signature and storing new provider IDs and receipts. It does not call an external OTP, messaging, or AI provider. The local responder recognizes supported Malayalam and English commands and routes unsupported text and attachments to staff. Attachments are identified by type. No media is downloaded automatically.
- **Case verification.** In chat, send `START` to opt in and `RESUME <case reference>` to choose a case. Then open the approved `/whatsapp/verify` page and provide the case token and an OTP sent by a separate approved adapter to the existing verified contact. Keep both credentials out of chat. The verified scope lasts 15 minutes and ends if the case token is revoked, the contact opts out, or the chat switches cases. After verifying, send `STATUS` or `നില` to see the latest CRM status and any confirmed appointment. The test adapter delivers synthetic OTPs. Live delivery is disabled by default.
- **Shared ownership.** WhatsApp uses the existing CRM conversation for the verified case. An active linked voice call holds a case claim until its end is observed or its lease expires. A pending WhatsApp case reply stays queued while another channel holds that claim. The coordination applies to linked voice calls and submitted Happy Calls. Unlinked conversations cannot be correlated by phone number alone.
- **Handoff and staff updates.** Send `HUMAN` to request staff assistance or send an update for the verified case. An open handoff pauses automated case replies. Use the private `/api/whatsapp/threads`, `/handoffs`, and `/outbox` endpoints to review, reply, and close handoffs. Staff free text requires a verified case scope and an open customer-service window.
- **Outbound controls.** All sends require active WhatsApp opt-in. `STOP` or `നിർത്തുക` revokes the scope and cancels queued sends. Free text also requires a user message within the last 24 hours. Outside that window, submit only a Meta-approved static template and language listed as `name:language` in `MRCARE_WHATSAPP_APPROVED_TEMPLATES`. The system checks consent, scope, template allowance, and rate limits again at submission. A provider acknowledgement records submission. Signed receipts separately update sent, delivered, read, or failed. An ambiguous send requires staff review and is not automatically retried.

Set `MRCARE_WHATSAPP_VERIFY_URL` to the approved HTTPS verification page. Graph API version and access token are server settings. Keep `MRCARE_WHATSAPP_SEND_ENABLED=0` until the owner approves production sending and the business account, webhook, verification delivery, templates, rates, and opt-in records are verified. Process queued messages explicitly through the owner API in this phase. No background worker is installed. The local workflow uses deterministic intent routing and CRM data, including approved estimates within the verified case scope. Free-form AI replies and media retrieval require later integration.

[Meta WhatsApp webhooks](https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/overview) · [WhatsApp Cloud API](https://developers.facebook.com/documentation/business-messaging/whatsapp/overview) · [WhatsApp Business Messaging Policy](https://business.whatsapp.com/policy)

## Estimates, invoices, and sales

Migration 7 adds finance accounts and sessions, issuer and fee policies, approved price revisions, immutable estimate lines and events, fiscal-year document sequences, invoices, voids, credit notes, and cash events. The backup-first procedure applies. Existing catalog rows remain in place, and no fictional demo price is copied into the finance price book.

Owners provision one-hour finance sessions and can revoke or deactivate them. Finance users can operate the estimate and invoice ledger, while the owner publishes issuer settings, the inspection minimum, and approved price versions. Authorized staff use the private `/api/finance` routes in local Swagger UI. The `/finance` view shows date-filtered totals, trends, recent invoices, and a CSV export. Technicians use **Prepare estimate** inside their assigned job. Each submission is an estimate revision sent for staff review.

- **Amounts and approvals.** Amounts use integer INR paise. Staff configure net inspection minimums and catalog prices explicitly. The backend computes net line value after discount and rounds the configured line tax half-up to one paise. A positive tax rate requires reviewed issuer registration, classification, and a tax label. Current catalog items and inspection policy are rechecked at approval. Quote only the current, approved, unexpired estimate for the verified case.
- **Invoice issue.** Record the verified customer's decision with evidence. Issue an invoice only from a current accepted estimate and a completed visit. Invoices have Kolkata financial-year sequences and preserve the issuer, customer, product, case, and priced lines as issued. Unpaid, unadjusted invoices can be voided with evidence. A replacement uses a new accepted estimate and a new number.
- **Adjustments and cash.** Credit notes reference original invoice line quantities and prorate the original net and tax in paise, assigning any rounding remainder when the line is fully credited. Cash entries record confirmed external activity. A stable source and transaction reference deduplicate payments and refunds across requests. Refunds require available refundable value linked to the original payment and a credit note. This API does not move funds or receive payment-provider webhooks. Never enter full card numbers or credentials.
- **Reconciliation.** Billed amounts, discounts, tax, credits, voids, collections, and refunds are dated by their ledger events. Net sales exclude tax and reflect credits and voids. Outstanding balances and refunds due are calculated as of the report cutoff, including prior-period invoices. Reports include service, parts, inspection, completed visits, and complaint conversion. Estimates do not enter billed sales.

Review the approved issuer setup, applicable classifications and tax rules, and customer addresses before relying on invoices for statutory use. GST return filing, statutory e-invoicing, tax-jurisdiction automation, and payment gateways are outside this implementation. [CBIC invoice, credit, and debit note guidance](https://cbic-gst.gov.in/gst-invoice-rules.html)

## Phase 7 production operations

Install `requirements-production.txt`. Set `MRCARE_DATABASE_URL` to use PostgreSQL and `MRCARE_REDIS_URL` to enable shared rate limiting and event notifications. With no PostgreSQL URL, the local demo uses SQLite. Budget database connections across all API replicas and workers using `MRCARE_DB_POOL_SIZE`. Increase `MRCARE_DB_POOL_MIN_SIZE` within that budget when pool metrics show requests waiting for connection setup during bursts. Keep the staff API, database, Redis, metrics, and monitoring private. Terminate TLS at a trusted proxy and use separate credentials for each integration. [Psycopg pool configuration](https://www.psycopg.org/psycopg3/docs/advanced/pool.html)

### Cutover and recovery

1. Stop writers and take a verified SQLite backup using the existing backup-first procedure. Apply migration 8 to a working copy using the SQLite CLI with `MRCARE_DB_PATH` set to that copy and `MRCARE_DATABASE_URL` cleared. Keep the original backup for recovery.
2. Set `MRCARE_DATABASE_URL` to an empty PostgreSQL destination. Run `python -m app.pg_admin import-sqlite --verified-backup <migration-8-copy>`. The importer checks SQLite integrity, imports in one transaction, verifies table counts and content hashes, and resets sequences. For a fresh install use `python -m app.pg_admin migrate`.
3. Check `/readyz` for each API and verify case, call, dispatch, WhatsApp and finance records before allowing writes. If cutover must be reversed after new writes, freeze writes and reconcile the new PostgreSQL records before reopening SQLite.
4. Write the PostgreSQL backup to encrypted storage with `pg_dump -Fc -f /encrypted/backups/koya.dump "$MRCARE_DATABASE_URL"`. Rehearse restoration against a separate empty database with `pg_restore --exit-on-error -d "$MRCARE_RESTORE_DATABASE_URL" /encrypted/backups/koya.dump`. Verify case and call counts, invoice totals, and event replay before promoting a recovered database. CI also defines a synthetic logical-restore check.

Run `python -m app.worker` for leases, retries, reconciliation, and retention. Review dead jobs and provider acknowledgements before any manual replay. `MRCARE_WORKER_ALLOW_EXTERNAL` defaults to zero. Keep external activity disabled until the corresponding provider settings and permissions have been verified. Workers do not initiate Happy Calls.

Owner sessions can connect to `/api/operations/events` with `Last-Event-ID` to recover committed changes. The board refreshes after the replay window expires and polls if it cannot open a stream. A Redis outage leaves database replay available and switches rate limiting to per-process counters. Scrape `/metrics` with `MRCARE_METRICS_KEY`. The example Prometheus configuration under `deploy/` discovers main API replicas over the staging network and loads alerts for readiness, error rates, queue age, and dead jobs. Mount the metrics key as a secret file and connect your alert receiver before rollout. `compose.phase7.yml` publishes the gateway only on loopback. Start two staff and lookup replicas using `docker compose -f compose.phase7.yml up -d --scale main=2 --scale voice-lookup=2` after migrating the database. Nginx resolves each service over the internal network. Expose approved provider routes only through the configured TLS ingress.

Optional S3 recording storage requires documented media permission, a private encrypted bucket, lifecycle rules, and a service identity limited to that bucket and prefix. The staff API rechecks permission and streams only through an expiring media grant. Use synthetic WAVs to exercise retention and permission revocation before enabling storage for callers. [S3 Block Public Access](https://docs.aws.amazon.com/AmazonS3/latest/userguide/access-control-block-public-access.html) · [S3 server-side encryption](https://docs.aws.amazon.com/AmazonS3/latest/userguide/serv-side-encryption.html)

Run `python -m scripts.load_capacity --rounds 1 --hold-seconds 1800 --enforce-targets` with `MRCARE_TEST_DATABASE_URL` pointing to a disposable empty loopback database named `koya_load_<suffix>` and a dedicated test Redis instance. Confirm p95 ≤ 500 ms, p99 ≤ 1 second, unexpected errors ≤ 0.5%, and dashboard propagation ≤ 2 seconds. On Linux staging, monitor API CPU with `rate(process_cpu_seconds_total[1m])` and memory with `process_resident_memory_bytes`. Check PostgreSQL and Redis resource usage alongside `mrcare_database_pool{stat="requests_waiting"}`, `mrcare_queue_depth`, and `mrcare_queue_delay_seconds`. This harness uses synthetic users and mock telephony. For live capacity, confirm the enabled Sarvam account's concurrency entitlement and the selected telephony route's simultaneous-call limit. Record those limits and the selected app and deployment before authorizing a live load test. The owner can exercise Malayalam voice acceptance during Phase 8.

## Verify

Run from the repository root. Each script uses its own process and synthetic data.

```powershell
$env:PYTHONPATH = "."
foreach ($test in Get-ChildItem tests/test_*_local.py) {
    .\.venv\Scripts\python.exe $test.FullName
    if ($LASTEXITCODE) { throw "Local API check failed: $($test.Name)" }
}
.\.venv\Scripts\python.exe test_phone_validation.py
```

The top-level live API and tunnel scripts require an explicitly approved test deployment. They are excluded from CI.
