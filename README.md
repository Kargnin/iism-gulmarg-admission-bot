# IISM Gulmarg Admission Bot

Prepare your details once, then run this bot locally on booking day. It uses Python 3.8+ and the standard library: no pip packages or Chrome are needed.

**Easiest setup:** copy [prompt.md](prompt.md) into a new chat with an AI coding agent that can use your computer. It will clone the project, help fill your private config and prepare documents, check readiness, and leave you the booking-day command. Setup does not submit a real application or make a payment.

## 1. Download the bot

Install Git and Python 3.8+ first if they are not available. The commands below use a macOS/Linux shell; on Windows use the installed Python command (`py -3` or `python`) in place of `python3`. The AI setup route can handle the OS-specific commands for you.

```bash
git clone https://github.com/Kargnin/iism-gulmarg-admission-bot.git
cd iism-gulmarg-admission-bot
mkdir -p configs
chmod 700 configs
cp config.example.jsonc configs/me.jsonc
chmod 600 configs/me.jsonc
```

Keep `iism-bot.py`, `schedule.json`, and `form-rules.json` together. `configs/`, `documents/`, and `runs/` are ignored by Git; keep personal files there or in your own local document folder. Never commit them. On macOS/Linux, keep each personal config at mode `600` and its containing directory at `700`; bot-owned run artifacts are created with the same owner-only policy.

## 2. Fill your details and choose batches

Open `configs/me.jsonc`. Replace every `FILL_ME`, confirm the supplied defaults, and choose your own `coursePriorities`. A simple choice looks like `"G2 Basic"`; you do not need to copy the website's exact course wording. Reasonable state, education, and diet variations are resolved to website choices; review the resolved values during checks.

Each preference gets the phone number at the same position in `phoneNumbers`. For two preferences, provide at least two different primary numbers. Extra numbers are reserves, used only when a primary is explicitly already used or persistently refused by mobile verification. All preferences run in parallel by default, and **every preference is attempted**. These are independent applications, not a first-choice/backup list.

Basic skiing is the supported path. Intermediate/Advance choices produce a warning; those paths are not fully supported or validated.

### Advanced preference objects

Use an object only when a batch string cannot express the choice, for example when you need explicit date/location evidence or optional website-text hints. Every value is text:

```json
{
  "batch": "G2",
  "course": "Snow Skiing",
  "level": "Basic",
  "from": "2027-01-03",
  "to": "2027-01-16",
  "location": "Gulmarg",
  "label": "G2 Basic",
  "courseText": "Snow Skiing",
  "levelText": "Basic SSC",
  "batchText": "03 Jan"
}
```

- `batch` names a code from the active schedule. Without one, `from` is required.
- `course` is `Snow Skiing`, `Snow Boarding`, or `MOI`; `level` is a skiing level such as `Basic` (Intermediate/Advance remain unsupported paths).
- `from`, optional `to`, and `location` supply or override the schedule evidence used for matching. Dates must be unambiguous and the end cannot precede the start.
- `label` is the name shown in logs and prompts.
- `courseText`, `levelText`, and `batchText` are optional live-label hints. They only break ties among semantically compatible choices and cannot override conflicting course, level, date, or location evidence.

The checked-in 2026–27 schedule opens **12 November 2026 at 10:00 IST**:

| Batch | Location | Course dates |
|---|---|---|
| G1 | Gulmarg | 20 Dec 2026–2 Jan 2027 |
| G2 | Gulmarg | 3–16 Jan 2027 |
| G3 | Gulmarg | 17–30 Jan 2027 |
| G4 | Gulmarg | 31 Jan–13 Feb 2027 |
| G5 | Gulmarg | 14–27 Feb 2027 |
| G6 | Gulmarg | 28 Feb–13 Mar 2027 |
| K1 | Kargil | 8–21 Feb 2027 |
| K2 | Kargil | 22 Feb–7 Mar 2027 |

Dates and opening time come from `schedule.json`. Missing, full, or disappearing slots keep being checked until that preference succeeds or you stop the bot.

## 3. Prepare documents and check

Set `documentsDir` to your local document folder and fill the filenames in `documents`. You need a photo, applicant signature, DOB proof, and education proof. A real guardian signature is required for applicants under 18; adults may supply one too.

Uploads must be nonempty JPG/JPEG/PNG files, **at most 500 KB each**. On macOS, the bot can convert PDFs and oversized images without changing originals. PDF conversion uses the first page only. Review the prepared files for readable text, the right person, and all necessary information. Windows/Linux users should supply ready images.

```bash
python3 iism-bot.py --config configs/me.jsonc --check
```

Fix any reported config or document errors and run the check again. With valid local files, `--check` also makes one read-only site request; it sends no application data. A reachable mobile-entry page does not prove that date slots are published or that an application will succeed.

For an optional supervised live rehearsal:

```bash
python3 iism-bot.py --config configs/me.jsonc --dry-run --now
```

This sends your mobile number, course choices, state, and DOB to the live form and validates the latest final-form controls, selections, seats/fee presence, and applicable prepared documents. It stops before durable submission state, final application transmission, and document uploads. It can wait indefinitely for unpublished slots. Press **Ctrl+C** after readiness is demonstrated or the expected missing-slot state appears. An AI-assisted setup can also rehearse against the local mock with fictional data; see [prompt.md](prompt.md).

## 4. Run on booking day

Run this command yourself in an interactive terminal and keep the computer awake and connected:

```bash
python3 iism-bot.py --config configs/me.jsonc
```

It prepares documents, waits until shortly before opening, then checks continuously. Add `--now` only when you intend to start checking immediately. Recoverable errors start a fresh session with the same phone and prepared documents; retry waits grow up to 10 seconds. Ctrl+C stops further work. Restart with the same config to preserve progress in `runs/`.

If a final reply is uncertain, that preference waits for you to check the actual application/email and answer **`booked`** or **`not booked`** in the terminal. Other workers continue. Do not guess or delete saved progress to retry.

The bot does not pay fees. Watch the configured email for the payment link and follow its deadline. IISM's one-course-per-person-per-year and non-refundable-fee rules still apply; separate phone numbers do not establish eligibility. Booking is not guaranteed.

The existing client may continue without certificate verification after a logged certificate error. If the site insists on a guardian upload from an adult, the default `bot.parentSignatureFallback` permits reusing the applicant's signature; minors always need a real guardian signature. See [booking behavior and evidence](docs/booking-decisions.md) for details and unverified live boundaries.
