# Fixtures

`sample_times_response.json` is **synthetic**. Its field names and value
shapes were read off the courses' own booking page (the inline
`template_time` / `template_book_time` JS templates render `time`, `holes`,
`available_spots`, `green_fee`, `green_fee_tax`, `cart_fee`, `cart_fee_tax`,
`has_special`, `special_discount_percentage`, `course_name`, `schedule_name`,
`minimum_players`), but the rows themselves are hand-written: the sandbox
this was built in has no network route to `foreupsoftware.com`.

It deliberately mixes cases the parser and matcher have to get right: a full
foursome slot, a single-spot slot, a 9-hole slot, an afternoon slot, and one
row belonging to a *different* schedule (Hendricks, 11075) to prove that
per-course filtering works — the request sends the facility-wide
`schedule_ids[]`, so other courses' schedules can come back in the response.

To capture a real response and diff it against this, run from any machine
with normal internet access:

```
python3 scripts/capture_sample.py weequahic 2026-09-05
```

It writes `live_<course>_<date>.json` here and prints the field names present
on the first entry. If those differ from what the parser expects, update
`app/foreup_client.py:parse_response` and replace this fixture with the real
capture.
