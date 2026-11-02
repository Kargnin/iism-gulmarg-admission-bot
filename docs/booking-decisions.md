# Booking behavior and evidence

Each preference has a primary number and its own worker. Extra numbers are exclusively leased reserves for explicitly used/persistently refused primaries. Every preference is attempted. Missing/full/disappeared schedules remain pending until booked or cancelled.

Course names remain flexible. The cascade is course → level → schedule → state → DOB, carrying each response's hidden fields forward. A visible course does not prove that its schedules are open. Booking retries are indefinite, cancellation-aware and capped at 10 seconds. Unknown final outcomes require user feedback for that batch while other workers continue.

Inputs and files are prepared once before workers start; uploads are shared immutable snapshots. Progress uses process locking and durable atomic writes and binds applications to their concrete date/course identity. Existing and unresolved applications are never silently discarded.

Certificate-verification fallback and optional adult applicant-signature reuse when the site insists on a guardian upload are intentional retained behaviors. Minors require a real guardian signature. Intermediate/Advance entries receive an unsupported-path warning; no full live support is claimed.

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
