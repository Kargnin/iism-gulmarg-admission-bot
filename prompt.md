# Set up the IISM booking bot on my computer

Please set up https://github.com/Kargnin/iism-gulmarg-admission-bot locally, from clone through a checked private configuration and a safe rehearsal. Do the local work you can, ask me for genuinely missing information, and leave me ready to run it myself on booking day.

I authorize cloning and inspecting this repository, preparing private local configuration and document copies, running local tests, running the read-only check, and performing a bounded rehearsal that never submits a real final application. This request does **not** authorize a live booking, payment, sending messages, publishing my data, or committing/pushing personal files. Do not launch or leave a real booking process running during setup.

If this chat cannot access my local terminal/filesystem, say so clearly and give the concrete commands or access needed; do not pretend to clone, inspect files, or run checks. When local tools are available, do the work rather than merely describing it.

## Inspect the current project first

1. Locate a suitable existing checkout, or clone the repository into a sensible local project folder. Tell me the path. Preserve existing work and private configuration. Do not overwrite another person's files or reset saved progress.
2. Read the applicable `AGENTS.md`, current `README.md`, `config.example.jsonc`, `schedule.json`, `form-rules.json`, `docs/booking-decisions.md`, and the relevant source. Run `python3 iism-bot.py --help` to confirm the actual commands. The field map and commands below describe this version; adapt to the actual cloned schema and source instead of adding stale keys or guessing flags. Report a material mismatch.
3. Check Git and Python 3.8+. This bot uses only Python's standard library. No pip packages, browser driver, or Chrome installation is needed for runtime. If a required basic tool is missing, help me install it using the normal process for my OS. Do not introduce compatibility layers, alternate implementations, or silent migrations.
4. Keep `iism-bot.py`, `schedule.json`, and `form-rules.json` together. Use the supplied JSONC template, preserving useful comments. Store my configuration at `configs/<person>.jsonc` inside the ignored `configs/` folder; use a simple non-sensitive filename. On macOS/Linux, create that directory with mode `700` and the config with mode `600`. Do not create arbitrary personal JSON files in the repository root. Keep documents in an ignored local folder or a folder I choose outside the repository. `runs/` contains private logs, saved responses, converted uploads, and progress; the bot makes its own generated artifacts owner-only on POSIX. Verify the actual ignore rules before creating private files. Never commit or push configs, documents, or runs.

## Gather only the missing information

Ask for missing essentials in one organized message after checking the template and any configuration I explicitly identify. Let me enter sensitive values directly into the local config if I prefer. Ask for local file paths rather than requiring document uploads to this chat. Do not search unrelated folders for personal data. Do not invent names, dates, phone numbers, qualifications, medical details, signatures, or eligibility statements. Do not silently truncate or change my answers to make validation pass.

Use this field map unless the inspected current template differs. All applicant/address/document values are strings; keep phone numbers as strings too.

| Information to obtain or confirm | Config field |
|---|---|
| Applicant's name, father's name | `applicant.name`, `applicant.fatherName` |
| DOB in ASCII `dd/mm/19xx` or `dd/mm/20xx`, nationality | `applicant.dateOfBirth`, `applicant.nationality` |
| Email that can receive the payment link | `applicant.email` |
| Education, diet, blood group | `applicant.education`, `applicant.diet`, `applicant.bloodGroup` |
| Emergency contact's name and 10-digit phone | `applicant.emergencyContactName`, `applicant.emergencyContactPhone` |
| House/apartment, street/village/colony, district/city | `address.houseNo`, `address.street`, `address.district` |
| State/UT and six-digit PIN | `address.state`, `address.pincode` |
| Chosen course/batch preferences, in primary-phone order | `coursePriorities` |
| One unique 10-digit Indian primary mobile per preference, then optional reserves | `phoneNumbers` |
| Local document folder | `documentsDir` |
| Photo and applicant signature filenames | `documents.photo`, `documents.signature` |
| DOB proof and education proof filenames | `documents.dobProof`, `documents.educationProof` |
| Real guardian signature, required when the website reports under 18 or requests it | `documents.parentSignature` |

Confirm template defaults such as nationality, education, and diet instead of treating them as my answers. Optional fields can remain blank unless I want to supply them: `applicant.domicile`, `applicant.medicalCategory`, `applicant.height` (cm), `applicant.weight` (kg), `applicant.waist` (inches), `applicant.shoeSize` (UK), `documents.domicileProof`, `documents.skiingCertificate`, and `documents.employmentProof`. Intermediate/Advance preferences may need a previous-course certificate, but those booking paths remain unsupported/unvalidated; Basic skiing is the supported path. Do not silently change a requested level.

Use the checked-in rules and, only when needed and permitted, legitimate non-submitting discovery to resolve website choices. State, education, and diet answers may use reasonable abbreviations, spelling variations, or everyday wording. Use the bot's current resolver, show me the resulting website labels, and ask for correction only when an essential answer is missing or cannot reasonably be resolved. Do not force me to transcribe exact dropdown labels or invent a new config toggle. If a proposed match would change a fact, ask instead. Course wording and hints are flexible too: use batch-code strings such as `"G2 Basic"` whenever they express my actual choice. There is no need to hardcode live dropdown IDs.

When a simple batch string is insufficient, use the current preference-object schema documented under **Advanced preference objects** in `README.md`: `batch` selects a scheduled code; `course` and `level` state the semantic choice; `from`, `to`, and `location` provide explicit matching evidence; `label` controls the displayed name; and `courseText`, `levelText`, and `batchText` are optional tie-breaking hints only. All object values are strings. Without `batch`, `from` is required. Do not use hints to override contradictory course, level, date, or location evidence, and do not invent additional fields or aliases.

Read the current dates and opening from `schedule.json` and show the available batch codes before asking me to choose. The checked-in 2026–27 opening is 12 November 2026 at 10:00 IST; verify the inspected schedule rather than assuming that date applies to every later season. Do not hardcode someone else's preferences, add a `maxBookings` field, or change schedule/tuning settings without a real need.

Explain the phone mapping before finalizing the config: preference 1 owns number 1, preference 2 owns number 2, and so on; additional numbers are reserves. All preferences run in parallel by default and every configured preference is attempted. These are independent applications, not a ranked list that stops at the first success. A reserve is used only after an explicit already-used number or persistent mobile-verification refusal. Missing/full/unpublished slots and recoverable network failures keep the same phone. Do not add extra preferences merely to use all supplied numbers.

Phone input must be wholly recognizable as a phone number. Keep values as strings in the private config; a leading `+91`, `91`, or `0` and common ASCII spaces, parentheses, or hyphens are accepted for `phoneNumbers` and the emergency phone. The bot normalizes valid input to exactly 10 ASCII digits for posting and saved state. Reject labels such as `Home:`, extensions, prose, non-ASCII digits, and values with extra meaningful digits instead of extracting a convenient-looking substring.

## Prepare and inspect the documents locally

Required uploads are a photo, applicant signature, DOB proof, and education proof. DOB is required, and the website—not a local course-date calculation—determines the age response after it is entered. A website-reported minor or explicit guardian demand requires a real parent/guardian signature. Adults may explicitly provide one. The configured filenames are resolved from `documentsDir`; use an absolute folder path or `~` to avoid confusion about paths relative to `configs/`.

Prepare nonempty JPG/JPEG/PNG files at most 500,000 bytes each, using the stricter published 500 KB rule even if a page script mentions PDF/GIF/1 MB. Preserve originals. Work locally; do not use cloud conversion/OCR services or upload private documents elsewhere by default. Never fabricate, redraw, or alter a signature, identity detail, or certificate content.

On macOS the bot can convert PDFs/oversized images with the built-in tools and place prepared copies in `runs/.../upload-ready/`. PDF conversion takes only the first page. Review which page contains the needed information, show me the prepared documents locally, and obtain my legibility/content review. Do not assume the first page is sufficient for a multi-page proof. On other systems help me make suitable local image copies with available tools, or explain exactly which files still need manual preparation. Do not rename a PDF to `.jpg` or report readiness based on size alone. Check file bytes/type, dimensions as helpful, size, and that each config path points to the intended document; automated checks cannot establish identity or legibility.

Report the two existing behaviors plainly: the client may continue without TLS certificate verification after a logged certificate error; and `bot.parentSignatureFallback` allows an applicant signature in the guardian slot only after the website positively reports adulthood and still insists. A website-reported minor must use a real guardian signature. Missing website age is unknown, never proof of adulthood, and must not enable substitution. Do not add a local age fallback or another compatibility behavior.

## Check readiness without making a live application

Use my actual chosen config path in all final commands. Start with:

```bash
python3 iism-bot.py --config configs/<person>.jsonc --check
```

This validates local fields/documents and saved progress; when local checks pass it makes one read-only GET of the site. It does not submit application data. Fix the authorized local config/document issues, ask only for facts you cannot supply, and repeat as needed. Distinguish local validation failures from site downtime. A successful GET is not proof that schedules are published, eligibility is established, or final submission will work.

If current dropdown inspection is necessary, inspect the actual `--discover` behavior first. It uses the first configured phone by default and permits incomplete applicant/document details, but the plan still needs one configured primary phone per valid preference. It uses form postbacks but never sends the final application. Persistent discovery downtime returns a controlled nonzero result with the final site/network reason; rerun the same invocation preserving `--config`, `--base-url`, and `--phone`. A live dry run sends the mobile number, course choices, state, and DOB to the site and validates the latest final-form readiness, including applicable prepared document controls; in this version it stops before durable submission state, the final application request, and document uploads. Verify that boundary in the cloned code. For either path, apply your own tool policy to binding terms, authentication, or CAPTCHA; obtain any required user review/confirmation and do not bypass them. Do not contact the site merely to create activity when local checks and a mock can establish the same setup readiness.

If a short supervised live rehearsal is useful and permitted, run only the confirmed safe command:

```bash
python3 iism-bot.py --config configs/<person>.jsonc --dry-run --now
```

Set a short observation deadline, normally 60–120 seconds, before starting it. Poll progress while it runs and stop it gracefully with Ctrl+C/SIGINT when it reaches the pre-submit success point, demonstrates the expected missing-slot state, or reaches the deadline. Allow in-flight requests to unwind and confirm that no process remains. This command can wait indefinitely when schedules are unpublished; do not wait for a success that the live site cannot yet provide. Report “schedules not published” as an availability limitation when supported by the output, not an invalid-config error. Do not silently change the chosen batch to force a rehearsal to pass.

Use the disposable local mock with a healthy service to exercise the full setup flow quickly when live slots are absent. Adversarial recovery is covered separately by the repository tests; do not make a beginner rehearsal wait through persistent random faults. First inspect its current help/source. Create a separate ignored rehearsal config and valid synthetic image files, using wholly fictional applicant/contact details and no private documents. Start the mock on localhost:

```bash
python3 tests/mock_site.py --port 8765 --chaos 0 --opens-in 2 --hide-full
```

Then, in another managed process, run the bot against it with the synthetic config:

```bash
python3 iism-bot.py --config configs/rehearsal.jsonc --base-url http://127.0.0.1:8765 --now
```

Final submissions to this localhost mock are allowed for rehearsal; never omit or substitute its `--base-url`. Verify the destination before starting. The separate config and origin keep mock progress separate from the real booking run. Give the mock rehearsal a sensible deadline, stop both processes afterward, and keep the real config/progress untouched. If the chosen port is occupied, use an available localhost port in both commands. Mock success demonstrates local behavior, not production success. Run the repository's local test suite when useful to verify the checkout; hosted CI is not required for this setup.

## Leave me ready for booking day

Finish the local work, then give a concise readiness report with:

- The checkout, private config, original/prepared document locations, and any manual facts or document reviews still needed. Mask phone numbers and summarize private data rather than reproducing it.
- My resolved website state/education/diet values, chosen batches/dates, primary/reserve assignments, and opening time with timezone.
- What actually passed: local validation, read-only reachability, any bounded live rehearsal, and/or synthetic mock rehearsal. State what remains unverified, especially unpublished schedules and current production final outcomes. Do not claim a booking was made.
- Exact copy/paste commands from the correct working directory for another `--check`, starting the real run at the scheduled opening, and an immediate start if I intentionally need it. Print those real-run commands; **do not execute them during setup**. The scheduled command is `python3 iism-bot.py --config configs/<person>.jsonc`; `--now` makes it start checking immediately.
- How to keep the computer awake/online with an interactive terminal, stop with Ctrl+C, and restart with the same config and saved progress. Do not delete/reset unresolved progress or launch concurrent real processes for the same config.

Explain that each missing/full/disappearing preference remains pending until it succeeds or I stop. Recoverable site/page-state errors start a fresh HTTP session from mobile entry and criteria, preserving that preference's phone and prepared documents; exponential backoff is capped at 10 seconds. If a final submission has an unknown outcome, only its preference waits while siblings continue. I must check the actual application/email and answer `booked` or `not booked` in the terminal; you must not guess the answer or rotate phones to evade uncertainty.

I must watch the configured email for a payment link, check its deadline, and complete payment myself. Accepted application, payment, and admission are separate. IISM's one-course-per-person-per-year and non-refundable-fee rules apply; the bot's ability to attempt alternatives with separate numbers does not establish permission or eligibility. Do not promise a seat, invent institution rules, or automatically act on a future booking date.

There is no reset command. Preserve the same config and progress files, and reconcile any uncertain application instead of deleting, renaming, or replacing state.
