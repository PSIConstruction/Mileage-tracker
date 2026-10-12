# PSI Mileage Tracker

The goal is to measure how many miles a vehicle actually drove, accurately enough to use for reimbursement.

Commercial apps (QuickBooks mileage, MileIQ, TripLog Drive) over-count short trips and record trips that never happened. Usually the GPS isn't the main problem. The bigger problem is **adding up the distance between noisy GPS points**. Every fix is off by a few meters in a random direction, so the path zig-zags. A parked phone drifts, and each drift counts as miles. This project keeps the raw GPS and takes four steps before measuring anything:

```
raw GPS ──► sanity filter ──► map-match to roads ──► road geometry ──► miles
 (kept         (reject bad       (Google Snap to        (interpolated
 forever)       fixes, log why)   Roads, 100 pts/req)    along curves)
```

This repository is **the first prototype**: phone → GPS fix every 5 s → Google Snap to Roads → compare against the odometer. Login, Business/Personal classification, PDFs, and automatic trip detection come later, after 20–30 real drives show the mileage matches the odometer.

| Folder | What it is |
|---|---|
| `android/` | Minimal Android recorder app: Start/End trip, odometer entry, one high-accuracy GPS fix every 5 s from a foreground service, raw fixes saved on the phone, trips shared as JSON. |
| `processing/` | Python pipeline + CLI: filtering, Google Roads map matching, mileage, an odometer comparison report, and GPX export for checking a trip on a map. No third-party dependencies. |

## What the simulated drives show

The test suite drives a simulated 3.58 mi route (a straight, a 90° curve, and two more legs). It adds 8 m of GPS noise, 3 minutes parked at each end, three teleport spikes, and one low-accuracy fix:

| Method | Distance | Error |
|---|---|---|
| Summing raw GPS points | 5.4–5.6 mi | **+52–55%** |
| Filtered GPS | 3.60–3.72 mi | +0.5–4% |
| Road-matched (fake roads client) | 3.57–3.61 mi | **< 1%** |

The raw-sum failure looks like what you saw from the commercial apps. These numbers are from simulation; only real drives against the odometer prove anything.

## Running the validation drives

1. **Install the app.** Every push builds a debug APK in GitHub Actions (`CI` workflow → `psi-mileage-debug-apk` artifact). Or open `android/` in Android Studio and press Run. On first Start, grant **Precise** location and allow notifications.
2. **Phone settings.** In the app, tap *Battery optimization settings* and set PSI Mileage to *Unrestricted* / *Don't optimize*. Samsung, Xiaomi, and others kill background apps aggressively without this.
3. **Before driving:** type in the odometer reading and tap **Start trip**. A notification shows how many fixes have been recorded.
4. **After parking:** tap **End trip** and enter the ending odometer reading and a note (e.g. "Shop → Newman, highway").
5. **Share:** tap *Share* (or *Share all*) and send the JSON files to yourself by email or Drive. Put them in a folder such as `trips/`, which git ignores because it holds employee locations.
6. **Process:**

   ```sh
   cd processing
   pip install -e .                      # or run with: python -m mileage ...
   export GOOGLE_MAPS_API_KEY=...        # see "Google setup" below
   mileage validate ../trips --csv ../trips/report.csv
   ```

   Output looks like this (illustrative numbers):

   ```
   trip                            odo     raw    filt   match  m+ends    err   err%
   2026-10-14T08-14-03_ab12       13.6   15.02   13.81   13.58   13.63  -0.02   -0.1
   ...
   Mean absolute error vs odometer (trips with odometer readings):
     raw                1.12 mi  (worst 2.40 mi, n=24)
     matched            0.06 mi  (worst 0.14 mi, n=24)
   ```

   To look at one trip in detail:

   ```sh
   mileage process ../trips/2026-10-14T08-14-03_ab12.json -v   # lists every rejected fix and why
   mileage gpx ../trips/2026-10-14T08-14-03_ab12.json          # open the .gpx at gpx.studio
   ```

   The GPX file has the raw track, the filtered track, the road-matched track, and a waypoint for each rejected fix. Use it to see *why* a trip came out wrong.

**Drives to deliberately include:** normal city streets, highway, many turns, parking lots, a stop of several minutes mid-trip, closely parallel roads (frontage roads, highway next to a local road), poor reception, and very short (1–3 mi) trips.

**Pass/fail target:** 13.6 actual → 13.5–13.7 recorded is acceptable. Odometers read to 0.1 mi, so ±0.1 mi on any trip is measurement error. For short trips, look at absolute error in miles, not percent.

## Google setup

1. Create a Google Cloud project and enable the **Roads API**.
2. Create an API key and restrict it to the Roads API.
3. **Set a quota** (APIs & Services → Roads API → Quotas), e.g. 200 requests/day. A bug then can't run up a bill. The CLI also refuses to make more than `--max-requests` (default 500) requests per run.

Cost: each request snaps up to 100 points, and Google currently includes 5,000 free requests per month. At one fix every 5 s that's about 8 minutes of driving per request, so two employees should stay well inside the free tier. Processed results are saved in `processed/` next to the trip files, so re-running `validate` doesn't call Google again. Use `--force` to re-match, or `--offline` to skip Google entirely (raw and filtered only). Check Google Maps Platform's terms on how long snapped results may be kept. The raw trip and the computed mileage are what matter for audit, and the matched path can always be regenerated.

## How the pipeline works

**Raw data is evidence and is never modified.** The phone writes each fix to an append-only file the moment it arrives, so a crash loses at most one fix. Processing reads the trip file and writes results to a *separate* file. When the algorithm improves, every past trip can be re-processed.

Each fix records time, latitude/longitude, horizontal accuracy, Doppler speed (and its accuracy), bearing, altitude, provider, and a mock-location flag.

**Sanity filter** (`processing/mileage/filtering.py`). Every rejected fix gets a logged reason:

- *accuracy*: reported accuracy worse than 50 m.
- *implied speed / teleport*: a jump implying more than 134 mph. If the *previous* fix was the bad one (a spike that slipped just under the limit), the filter rejects that fix instead and keeps the good one.
- *stationary jitter*: the phone's Doppler speed says stopped, and the fix is still within ~3× its accuracy radius of the last kept fix. This is what stops a parked phone from adding miles.
- *spike*: an out-and-back detour A→B→C that is far longer than A→C at speeds where that isn't physically possible.

**Map matching** (`processing/mileage/roads.py`). Kept fixes are sent to Snap to Roads with `interpolate=true` in batches of 100. Consecutive batches overlap by 2 points and are stitched together by `originalIndex`, so seams are neither double-counted nor dropped.

**Gaps** (`processing/mileage/pipeline.py`). If fixes stop for more than 5 min or 1 km (lost signal, the app was killed), each side is matched separately, the hole is counted as a straight line, and a warning is attached. A straight line under-counts, so check flagged trips by hand.

**Two "matched" numbers** are reported while testing:

- `matched`: distance along the snapped road path.
- `matched_plus_ends`: also adds the off-road leg from the true start and end to the nearest road (driveways, parking lots, job sites).

The odometer comparison will show which one to use for reimbursement.

## Not built yet (on purpose)

In order, once accuracy is proven:

- Automatic trip detection (vehicle Bluetooth + speed above ~8–10 mph for 15–30 s; end after several minutes stopped). Manual Start/End stays as the fallback.
- Upload to a server instead of share-sheet export.
- Business/Personal classification, job/purpose, editing, merging and splitting trips.
- Monthly totals, the monthly odometer-photo cross-check, and the PDF/CSV reimbursement report.
- iPhone app.
- **V2:** a self-hosted OpenStreetMap + Valhalla/Meili backend. The pipeline talks to map matching through the small `SnapClient` interface in `roads.py`, so it can be swapped in and compared trip-for-trip on the same raw data.
- **V3:** a dedicated Bluetooth GNSS receiver (NMEA) in the vehicle, if phone background behavior stays unreliable. It would produce the same trip format.

## Development

```sh
cd processing && python -m pytest -q        # pipeline tests, simulated drives, no network
cd android && ./gradlew assembleDebug       # needs the Android SDK (or let CI build it)
```
