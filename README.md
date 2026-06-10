# IISM Gulmarg Admission Bot

Automates submission of the [IISM Gulmarg](https://admission.iismgulmarg.in/) course admission form via direct HTTP API calls. Zero dependencies — Python 3 stdlib only. No browser automation needed.

## Quick Start

```bash
# 1. Set up your config
cp config.example.json config.json
# Edit config.json with your personal details

# 2. Place your documents in documents/
#    - photo.png          (Photo of applicant)
#    - signature.png      (Signature of applicant)
#    - dob_proof.png      (Date of birth proof)
#    - domicile_proof.png  (Domicile proof)
#    - education_proof.png (Education qualification proof)
#    - skiing_certificate.png (Skiing certificate, if applicable)
#    - parent_signature.png   (Parent/guardian signature)
#    All files must be <500KB, png/jpg/jpeg format.

# 3. Run
python3 iism-bot.py
```

## Configuration

Copy `config.example.json` to `config.json` and fill in:

| Section | What to set |
|---|---|
| `applicant` | Name, DOB (dd/MM/yyyy), email, blood group, physical stats, emergency contact |
| `applicant.mobileNumbers` | Multiple phone numbers — tried in order, already-registered ones skipped |
| `address` | Permanent address details |
| `course` | Select by position (`"1"` = first non-default option) or by exact display text |
| `documents` | Paths to your document files under `documents/` |
| `bot` | Retry behavior, pipeline attempts, target time scheduling |
| `serverDefaults` | Server-populated values — discovered automatically, change only if needed |

### Course Selection

Use position numbers (recommended) or exact text:
```json
"course": {
  "name": "1",       // 1 = first non-default course
  "level": "1",      // 1 = first non-default level
  "schedule": "1"    // 1 = first schedule, auto-falls back to 2,3... if 0 seats
}
```

When `schedule` has 0 seats, the bot automatically tries the next schedule.

### Multiple Phone Numbers

```json
"applicant": {
  "mobileNumbers": ["9876543210", "9876543211", "9826372819"]
}
```

Numbers that return "already registered" are skipped. The bot cycles through remaining numbers.

## CLI Options

```
python3 iism-bot.py --dry-run         # Fill everything but don't submit
python3 iism-bot.py --target=10:00    # Wait until ~5 min before target time
python3 iism-bot.py --max-retries=5   # Override max retries per step
python3 iism-bot.py --reset-cache     # Clear cached course options
```

## How It Works

Direct HTTP calls to the ASP.NET WebForms backend — no browser, no Selenium, no Playwright:

1. **Mobile verification** — POST to `Admission_instructions.aspx`
2. **Accept criteria** — POST to `Admission_criteria.aspx`
3. **Cascading dropdowns** — POST each `onchange`-enabled dropdown (course → schedule → state → DOB), extracting VIEWSTATE/EVENTVALIDATION from each response
4. **Submit** — multipart/form-data POST with all fields and file uploads

Handles ASP.NET quirks: VIEWSTATE, EVENTVALIDATION, cookie-based sessions, cascading dependent dropdowns, and position-based option selection with caching.
