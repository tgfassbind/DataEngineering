# Dataset

Sample of the SBB actual data ("Ist-Daten v2", Open Transport Data Swiss), used as
test fixture.

| File | Content |
|---|---|
| `excerpt_21092026_istdaten_v2.csv` | operating day 21.09.2026, 450 rows of the lines IR75 and S1 |

The excerpt keeps the original format: same columns, same separator, header starting
with `BETRIEBSTAG`. It includes S1 trips from both networks (Basel and Luzern/Zug), so
it also exercises the line filter via stops.

A full daily file covers all of Switzerland and is about 650 MB. The fetcher downloads
those; this excerpt is only for tests.

## Use

The tests of the transform (`transform/tests/`) use this file: the unit tests read
it directly, the end-to-end tests upload it to a local Cloud Storage emulator as the
raw file for 2026-09-21 and check the Parquet output against values computed from the
file itself. See the [verification](../README.md#verification) in the root README.

Source: [Open Transport Data Swiss](https://opentransportdata.swiss), Ist-Daten v2.