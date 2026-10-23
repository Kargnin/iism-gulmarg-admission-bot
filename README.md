# IISM Gulmarg Admission Bot

Books IISM Gulmarg / Kargil snow courses on [admission.iismgulmarg.in](https://admission.iismgulmarg.in/Admission_instructions.aspx)
by talking to the site's ASP.NET pages directly. One Python file, no installs (Python 3.8+, which every Mac has).

Built for booking day (**12 Nov 2026, 10:00 IST** for the 2026-27 season), when the site is overloaded:

- **Your priorities, in order.** It keeps trying your 1st choice until the site shows it is full, then moves to the 2nd, and so on.
- **One booking per phone number.** The site allows one booking per number. Number 1 books your best choice that still
  has seats, number 2 the next one, and so on, so you end up with options. Numbers the site says are already used are skipped.
- **Never gives up.** Timeouts, 500/503 errors, ASP.NET error pages, dropped connections and lost sessions are retried
  until everything you asked for is booked or you press Ctrl+C. If the reply to a submit never arrives but the site then
  says the number is used, it counts that as a (probable) booking instead of booking the same batch again.
- **Resumable.** Progress is saved; run it again and it skips numbers and batches that are already booked.

## Setup (each person does this once)

```bash
git clone https://github.com/Kargnin/iism-gulmarg-admission-bot.git
cd iism-gulmarg-admission-bot
cp config.example.jsonc config.jsonc     # then fill in every FILL_ME
```

The `FILL_ME`s are exactly what the form marks as mandatory (*):

- **About you:** name, father's name, nationality, date of birth, email, education, vegetarian / non-vegetarian,
  blood group, emergency contact name and phone.
- **Address:** house no, street, district/city, state and PIN code.
- **Booking:** phone numbers and course priorities.
- **Documents:** photo, signature, date-of-birth proof and education proof.

Everything else is optional and can stay empty: domicile, medical category, height, weight, waist, shoe size, and the
domicile / employment / skiing-certificate attachments.

Several people on one laptop: give each person their own file and pass it with `--config`, e.g.
`python3 iism-bot.py --config configs/rahul.jsonc`. Each config keeps its own progress under `runs/<config name>/`.

## Choosing courses

`coursePriorities` is a list, best first. Each entry is `"<batch> <level or course>"`:

| Batch | Where | Dates (2026-27) | Seats |
|---|---|---|---|
| G1 | Gulmarg | 20 Dec – 02 Jan | 80 shared by Basic / Intermediate / Advance |
| G2 | Gulmarg | 03 Jan – 16 Jan | Basic 50, Intermediate 20, Advance 10 |
| G3 | Gulmarg | 17 Jan – 30 Jan | Basic 50, Intermediate 20, Advance 10 |
| G4 | Gulmarg | 31 Jan – 13 Feb | Basic 40, Intermediate 10, Advance 10, **Snowboarding 20** |
| G5 | Gulmarg | 14 Feb – 27 Feb | Basic 50, Intermediate 10, Advance 10, **MOI 10** |
| G6 | Gulmarg | 28 Feb – 13 Mar | Basic 50, Intermediate 20, Advance 10 |
| K1 | Kargil | 08 Feb – 21 Feb | 40 shared by Basic / Intermediate / Advance |
| K2 | Kargil | 22 Feb – 07 Mar | 40 shared by Basic / Intermediate / Advance |

Fees: Gulmarg Rs 10,000 (age up to 25) / Rs 20,000 (above 25), MOI Rs 20,000; Kargil Rs 5,000 (locals) / Rs 10,000.
Age 10–40.

```jsonc
"coursePriorities": ["G2 Basic", "G3 Basic", "K1 Basic", "G4 Snowboarding", "G5 MOI"]
```

The dates live in [schedule.json](schedule.json); next season, update that file (or add `"batches"` to a config).
The snow courses only appear on the site at 10:00 on booking day, so nobody can see their names in advance. The bot
doesn't need to: it finds your batch by its dates in whatever course lists it. It prefers a course named for your level
and never one named for a different level. If the site's names ever need pinning down, a priority can give the text
directly: `{"batch": "G2", "level": "Basic", "courseText": "Snow Skiing", "batchText": "03 Jan"}`.

## Phone numbers

```jsonc
"phoneNumbers": ["98xxxxxx01", "98xxxxxx02"],
"maxBookings": 0          // 0 = one booking per number; 1 = stop after the first booking
```

With `"parallel": true` (default) every number works at the same time, each on a different choice. A number keeps its
choice until it books it or the site shows it full, then takes the best choice nobody else is working on.
`"parallel": false` does one number after another instead.

## Documents

Point `documentsDir` at the folder with your files and list the file names. The site says JPG/PNG under 500 KB.
On a Mac, PDFs and big images are converted automatically (first page of a PDF, saved under `runs/<config>/upload-ready/`;
your originals are not touched). On Windows/Linux convert them yourself.

| Key | Needed |
|---|---|
| `photo`, `signature`, `dobProof`, `educationProof` | yes (mandatory on the form) |
| `parentSignature` | only if the participant is under 18 (the form's "PARENT/GUARDIAN (IF PARTICIPANT IS UNDER 18 YEARS OLD)") |
| `skiingCertificate` | for Intermediate / Advance (your previous course certificate) |
| `domicileProof`, `employmentProof` | no |

The form's own required check for the parent/guardian signature stays on even after an adult's date of birth is entered.
So adults' forms go out without it first, and if the site does refuse one for that reason, the bot resends it straight
away with the applicant's own signature in that slot. Set `"parentSignatureFallback": false` under `"bot"` to turn that off.

## Booking day

Nothing has to be run beforehand: the bot does everything itself when you start it.

1. Some time before 10:00, plug in the laptop and run `python3 iism-bot.py`. It checks your config and documents, keeps
   the Mac awake, and from 09:50 starts looking for the snow courses. Don't close the lid.
2. It books as soon as the courses are actually on the site, whether that's early, at 10:00 or late. It prints a status
   line every 30 s, and a banner plus a Mac notification for every booking.
3. Afterwards, watch the email for the payment link(s). The fee is non-refundable, the site allows one course per person
   per year, and unpaid bookings lapse, so pay only for the batch you will attend.

### How it copes with the site on the day

- **Opening time is just a hint.** The bot starts looking 10 minutes early (`startEarlySec`) and keeps looking every
  2 s (`pollSec`) until the snow courses appear, however late that is. Nothing is skipped as "full" before then.
- **The site only shows new courses to a fresh visit.** Reloading the form isn't enough: you have to enter the mobile
  number and accept the terms again. So every new attempt starts from scratch, with a new session, the mobile number,
  the terms, and then the current course list. Until the courses appear, only one of your numbers does this every 2 s;
  once they're listed, all numbers book in parallel.
- **Crashes don't throw work away.** If a request times out or the site answers 500/503 in the middle of an attempt, the
  bot retries that step on the same session instead of starting over. After 6 failures in a row it starts afresh.
- **Courses come one by one.** If your choice's batch isn't listed yet while other snow courses are, the bot keeps
  checking it for up to 5 minutes (`unlistedGraceSec`) before moving to your next choice. Even then it keeps looking, and
  goes straight back if a new course appears on the list.
- **Full courses disappear.** A batch that was listed and has been taken off the list counts as full right away. So
  does "0 seats" once other batches show seats, or 2 minutes after the courses appeared (`seatGraceSec`). So does the
  site saying "no seats" when you submit. Full choices are still rechecked every minute in case seats come back.

Optional extras (the run itself doesn't depend on them):

| Command | |
|---|---|
| `python3 iism-bot.py --check` | check config and documents and that the site answers, any time before (sends nothing) |
| `python3 iism-bot.py --discover [--phone N]` | walk the live form and list what it offers (never submits; useless before 10:00 since the snow courses aren't listed yet) |
| `python3 iism-bot.py --now` | book, starting immediately instead of waiting until 10 minutes before opening |
| `python3 iism-bot.py --dry-run` | full run that stops right before each final submit |
| `python3 iism-bot.py --reset-state` | forget saved progress for this config |

Everything is logged to `runs/<config>/bot.log`; each submit's reply page is saved in `runs/<config>/responses/`.

## How it works

Plain HTTP, like a browser without JavaScript:

1. `Admission_instructions.aspx`: posts the mobile number (`lnkbtnSubmit`) and gets redirected to the criteria page.
2. `Admission_criteria.aspx?mobile=N`: ticks `chkAccept`, posts `lnkbtnNext`, and gets redirected to the form.
3. `admission_form.aspx?mobile=N` (multipart): picks the course (the reload fills levels and batches), the level and batch
   (the reload fills seats and age limits), the state, then the date of birth (the reload fills the fee, or says
   "Age Allow X To Y" and that choice is skipped).
4. Submit: every field, the documents, `chkIAgree`, `btnSubmit`.

Every reload posts back exactly what the page contained, so hidden and validation fields stay correct. Dropdown
choices are matched against what the site actually lists. Each attempt starts with steps 1–2 on a new session (that's
the only way to see newly listed courses). A crash in the middle is retried from the step that failed. A batch only
counts as full after the site shows it full twice.

What is known about the site and what is assumed: the pages, fields, validators, reloads and messages up to the
submit were copied from the live site on 23 Oct 2026. The submit's success page and the wording for a used number or a
full batch can't be seen without filing a real application, so the bot accepts several variants: redirects, alert
popups and message labels.

## Tests and rehearsal

`tests/mock_site.py` is a copy of the real site's pages and behaviour that can also break on purpose (errors, dropped
connections, slow answers, lost sessions, app restarts, submits whose reply never arrives). The tests run the bot
against it in every scenario above:

```bash
python3 -m unittest discover -s tests -v
```

Rehearse a whole booking locally, crashes included:

```bash
python3 tests/mock_site.py --port 8765 --chaos 0.4 --opens-in 60 --stagger 20 --hide-full
```

(`--opens-in`: seconds until the snow courses appear; `--stagger`: they appear 20 s apart; `--hide-full`: full
batches drop off the list. Like the real site, it shows new courses only to a fresh visit.)

```bash
python3 iism-bot.py --base-url http://127.0.0.1:8765 --now
```

A rehearsal keeps its progress in `runs/<config>@127.0.0.1_8765/`, apart from the real run, so it never marks your real
numbers as used.
