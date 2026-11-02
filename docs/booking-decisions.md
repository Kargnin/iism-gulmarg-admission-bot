# Booking behavior and evidence

Each preference has a primary number and its own worker. Extra numbers are exclusively leased reserves for explicitly used/persistently refused primaries. Every preference is attempted. Missing/full/disappeared schedules remain pending until booked or cancelled.

Course names remain flexible. Configuration uses unambiguous whole-word course-kind/level semantics and rejects incompatible combinations before requests. The cascade is course → level → schedule → state → DOB, carrying each response's hidden fields forward. Optional text hints rank compatible candidates but neither block renamed options, override contradictory semantic/date evidence, nor define identity. Parsed date-token spans enforce every explicitly written start/end pair without counting two interpretations of one numeric token twice. When no known evidence conflicts but a generic course/level is supported only by aligned dates, the intentionally permissive request proceeds with a runtime warning. A visible course does not prove that its schedules are open. Every recoverable error discards the visit and restarts at mobile verification with a fresh cookie jar and fresh WebForms state.

Certainty is decided at the original final POST boundary. A direct 429, known used/full/closed response or actual failed-validator state proves rejection. Any final-boundary page-state loss or unknown label is ambiguous even on a direct 500: durable pending state remains and only that batch waits for feedback while other workers continue. Accepted records store canonical location/date/kind/level identity under an explicit key version. Trusted unmatched history stays visible and protects its phone while a changed kind/level may use a reserve; older or unversioned progress fails closed without migration or date-based guessing.

Inputs and files are prepared once before workers start; uploads are shared immutable snapshots. Progress uses process locking and durable atomic writes, rejects reversed ranges, stores canonical semantic identity and phone strings, and validates every persisted summary/receipt field it consumes. Read-only check and booking validate the same applicant/season/origin identity. Existing and unresolved applications are never silently discarded or normalized.

Applicant state, education and diet values resolve once at startup: exact website text and declared state aliases rank first, followed by a unique actual-option acronym or plausible spelling, spacing or abbreviation match. Ambiguous initials are reported with candidate labels. The resolved website labels are shown before booking and are matched again against the current live options. Missing or unresolvable essentials remain actionable errors. A final request is not persisted or sent unless the latest form still selects the intended course, level, batch and state, echoes the configured DOB, reports seats/fee, and contains its mandatory enabled controls and exact submit button. Optional controls follow browser submission rules: configured optional values are warned and omitted when unavailable, while a server-classified minor still requires the real guardian file/control. An absent, reset or empty mandatory control restarts from a fresh mobile session. Unexpected worker programming failures stop sibling polling and produce a failing process result even if a read-only confirmation prompt remains blocked; late feedback cannot alter saved progress after shutdown.

Persisted semantic dates use canonical `YYYY-MM-DD` text only. A target cannot simultaneously be recorded as accepted and assigned or pending submission; malformed current-version state is preserved and rejected before requests.

Certificate-verification fallback and optional adult applicant-signature reuse when the site insists on a guardian upload are intentional retained behaviors. Runtime guardian classification uses the server-reported age or age-count date; minors require a real guardian signature and are held before final submission when it is missing. Intermediate/Advance entries receive an unsupported-path warning; no full live support is claimed.

## Observed on 2 November 2026

- The [admission flow](https://admission.iismgulmarg.in/Admission_instructions.aspx) is mobile entry → criteria → multipart admission_form.aspx.
- It uses ASP.NET WebForms / .NET 4.8, not PHP. Course, course level, schedule, state and DOB invoke __doPostBack and reload the form at the same URL. VIEWSTATE, VIEWSTATEGENERATOR, EVENTVALIDATION and other hidden inputs must come from the current response.
- Gulmarg course: **Snow Skiing Courses at Gulmarg**; levels: **Basic SSC**, **Intermediate SSC**, **Advance SSC**, **Snow Boarding**. Kargil includes **Basic skiing course**; MOI is separate. These observations inform tests; IDs are not hardcoded.
- Course/level lists were populated while schedule lists remained empty, including after selecting Basic. Real schedule IDs, allocation values and calculated fees remain unknown until schedules appear.
- The form has 94 controls and 35 validators, including hdfTypeOfCapacitySheet and hdfCapacitySheetAllocationId. Live values are carried through rather than fabricated.
- Current state, education, diet and maxlength rules are in form-rules.json. The fuller sanitized snapshot in tests/fixtures/live-form-2026-11-02.json excludes applicant data, phone numbers, documents and session tokens.
- Published instructions and visible upload labels require JPEG/JPG/PNG up to 500 KB; some script validators mention 1 MB/PDF/GIF. Prepared uploads use the stricter published requirement.
- Normal Chrome interaction produced a viewstate-MAC failure during a state postback. Availability checks also saw IIS 500 (missing PleskModule). These are server/page-state errors, not evidence that a course is full.

The [official 2026–27 schedule](https://www.iismgulmarg.in/Courses_By_IISM.html#snowski2026) confirms the stored ranges and opening on 12 November 2026 at 10:00. A [January 2025 firsthand walkthrough](https://www.youtube.com/watch?v=vIy0eV-R94M) corroborates the general flow and 500 KB preparation advice; old values are not current-season evidence.

## Unverified boundaries

No real application or payment was submitted. A historical response supports accepting explicit successful-submission messaging without an application ID; exact current production success/duplicate/full responses remain unverified. Mock outcomes test possibilities and do not prove live behavior.

Preflight checks presence/type/size rather than identity or legibility. The institute's one-course-per-person-per-year and payment rules apply. Tests and retries cannot guarantee admission.
