# IISM Gulmarg Admission Bot

Books configured course preferences through IISM's ASP.NET WebForms requests. Python 3.8+ and its standard library are sufficient. Keep `iism-bot.py`, `schedule.json`, and `form-rules.json` together.

The published 2026–27 opening is **12 November 2026 at 10:00 IST**. Courses and levels can be visible before date schedules appear. Each preference keeps checking until its application is accepted or you stop the process.

## Setup

```bash
git clone https://github.com/Kargnin/iism-gulmarg-admission-bot.git
cd iism-gulmarg-admission-bot
cp config.example.jsonc config.jsonc
# Fill every FILL_ME and choose your own preferences, numbers and documents.
python3 iism-bot.py --check
```

`--check` reports configuration/field errors, accepted state/education/diet choices, document problems, and one read-only site check. It validates saved progress against the same applicant/season/origin identity as booking without modifying the progress file; a local blocker skips the site probe and cannot print full readiness. Site downtime is reported separately from invalid local details.

The current schema uses `applicant.diet`, top-level `phoneNumbers`, and `coursePriorities`. Unknown/obsolete keys are reported; private configs are never silently migrated. Course kinds and skiing levels use whole-word semantics, and incompatible combinations such as Snow Boarding + Basic are rejected at startup. `maxBookings` is removed: every configured preference is attempted.

## Primary and reserve numbers

```jsonc
"coursePriorities": ["G2 Basic", "G3 Basic"],
"phoneNumbers": ["9000000011", "9000000012", "9000000013"]
```

The first preference owns the first number, the second owns the second number, and both run in parallel. The third number is a reserve. Provide one unique primary per preference; extra numbers are reserves.

A worker takes an exclusively assigned reserve only after its number is explicitly already registered or persistently refused by mobile verification. Network errors, full/missing slots, and uncertain final submissions do not consume reserves. An exhausted worker alerts you and waits while the others continue.

Missing, full, or disappearing slots remain pending and are rechecked on fresh sessions indefinitely. No publication grace period abandons a preference or transfers its primary number to another date.

Basic skiing is supported. Intermediate/Advance entries generate an unsupported-path warning; the warning does not silently change or remove a preference.

| Code | Location | Dates |
|---|---|---|
| G1 | Gulmarg | 20 Dec 2026–2 Jan 2027 |
| G2 | Gulmarg | 3–16 Jan 2027 |
| G3 | Gulmarg | 17–30 Jan 2027 |
| G4 | Gulmarg | 31 Jan–13 Feb 2027 |
| G5 | Gulmarg | 14–27 Feb 2027 |
| G6 | Gulmarg | 28 Feb–13 Mar 2027 |
| K1 | Kargil | 8–21 Feb 2027 |
| K2 | Kargil | 22 Feb–7 Mar 2027 |

Dates come from `schedule.json`. Matching accommodates different wording and date formats, explores actual dropdowns, enforces every explicitly written start/end pair, and rejects configured or persisted ranges whose end precedes their start. Live IDs are not hardcoded. Preference objects may provide `courseText`, `levelText`, and `batchText` hints; hints rank compatible choices but stale wording never blocks semantic kind, level, location and date matching. If a generic course/level has no contradictory evidence and only the configured dates align, the approved permissive attempt continues with a prominent runtime warning.

The repository `schedule.json` is the default. An explicit `scheduleFile` selects another complete schedule and must exist. Top-level `batches` override matching batch codes from that schedule, while top-level `bookingOpensAt` overrides its opening time. These are explicit current-schema inputs; no alias or implicit migration is applied.

## Documents

Published uploads are **JPG/JPEG/PNG, at most 500 KB each**. Some form validators mention 1 MB/PDF/GIF; ready uploads use the stricter published image limit. Photo, applicant signature, DOB proof and education proof are mandatory. A real guardian signature is required under 18; configured adult guardian signatures are optional and are not tied to a course.

Preflight checks existence, byte type and size. It cannot verify identity or legibility. On macOS, the existing conversion feature can prepare PDFs/oversized images as JPEGs; PDFs use the first page. Review converted documents before booking if additional pages matter. Windows/Linux users should supply ready images.

Preparation happens once at startup. Workers share in-memory upload snapshots, so later edits, stale conversions or concurrent requests cannot swap files. Original files are not overwritten.

If the site insists on a guardian upload for an adult who has none configured, the applicant signature can be resent in that slot. `bot.parentSignatureFallback` controls this retained behavior. Runtime classification uses the site's reported age or age-count date; a server-classified minor without a real guardian signature is parked before final submission.

## Running and uncertain outcomes

```bash
python3 iism-bot.py                    # prepare, then wait until shortly before opening
python3 iism-bot.py --now              # check immediately
python3 iism-bot.py --dry-run --now    # dynamic flow without final applications
python3 iism-bot.py --discover         # inspect the current dropdown flow
python3 iism-bot.py --config configs/friend.jsonc --check
```

Backoff grows exponentially across consecutive failed visits and is capped at **10 seconds**, including 429 responses. `Retry-After` is logged; waits remain capped. After every recoverable site/page-state error, the worker discards its cookies and starts again at mobile verification, then repeats criteria → course → level → schedule → state → DOB with fresh WebForms state. Prepared uploads, phone ownership and durable booking progress remain unchanged. There is no cumulative booking retry limit. Ctrl+C prevents new requests/submissions and lets in-flight workers unwind.

An explicit 429 response to the original final POST retries the same phone from a fresh mobile visit and does not ask for booking confirmation. A deterministic validation rejection caused by immutable startup data parks that preference once with an actionable error while other workers continue; pre-submit temporary server errors, missing/full slots and availability changes keep retrying.

If the original final POST has no definite rejection or confirmation, **only that preference is held**. This includes failures or 429 responses while loading a redirected receipt, blocked foreign receipt redirects, and any unverified error-labelled final form. Only an actual failed-validator state or a known used/full/closed/direct-429 response proves direct rejection. Check the actual application/email and answer `booked` to finish that preference or `not booked` to resume from a fresh mobile visit. Other workers continue. A used-number message or a URL containing payment/success is not automatic confirmation. Without interactive stdin, pending progress is retained; restart interactively to answer.

Progress lives in `runs/<config-name>-<identity>/`, separated by full config path/site/season. A process lock prevents concurrent booking against the same progress. Writes are atomic and flushed before final submissions. The current durable schema records an explicit target-key version plus canonical location/date/kind/level identity, canonical phone strings, typed summary fields and typed receipt metadata. Older, unversioned, malformed or identity-incompatible progress is preserved and rejected before requests; it is never normalized, migrated or guessed from display dates. Matching hints are not part of identity. Trusted accepted history remains visible and its numbers stay protected when a target is removed or its dates, kind or level change.

Restarting preserves accepted applications and pending questions. Adding preferences/reserves does not count an old date as a new booking. `--reset-state` refuses to discard existing or unresolved applications.

Requests and redirects stay on the configured origin. The existing certificate-verification fallback remains: after a logged certificate error, the client can continue without certificate validation. This reduces transport authentication and is separate from the origin guard.

The institute allows one course per person per year and states that fees are non-refundable. This tool can submit configured alternatives with separate numbers; use and payment must follow the institute's rules. An accepted application and completed payment are separate. The bot does not pay fees.

## Tests and rehearsal

```bash
python3 -m unittest discover -s tests -v
python3 tests/mock_site.py --port 8765 --chaos 0.3 --opens-in 20 --hide-full
python3 iism-bot.py --base-url http://127.0.0.1:8765 --now
```

Use fictional details/documents locally. The mock exercises dynamic postbacks, delayed/disappearing slots, overload, session/application restarts and lost replies. Tests cover reserves, confirmation feedback, cancellation, shared uploads, progress/disk failures, origin guards and CLI outcomes.

Evidence and unverified boundaries are in [docs/booking-decisions.md](docs/booking-decisions.md). Hosted CI is not configured; run the local suite before using changes. Retry robustness cannot guarantee availability or admission.
