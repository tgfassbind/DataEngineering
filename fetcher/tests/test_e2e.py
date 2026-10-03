"""End-to-end test of the real CLI (subprocess) without Docker or internet.

* Object storage: moto's S3 server (S3 API emulator) by default, or ANY real
  S3 endpoint (e.g. the SeaweedFS service) when E2E_S3_ENDPOINT is set:
      E2E_S3_ENDPOINT=http://127.0.0.1:8333 E2E_S3_ACCESS_KEY_ID=... \
      E2E_S3_SECRET_ACCESS_KEY=... python -m unittest tests.test_e2e -v
* Sources: a local HTTP server mimicking the permalink CSVs and monthly ZIPs
  (ZIP members at the root, named YYYY-MM-DD_istdaten.csv, like the real one).

Requires dev dependencies:  pip install -r requirements-dev.txt
Run:                        python -m unittest tests.test_e2e -v
"""
import io
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from collections import Counter
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import boto3
from botocore.config import Config as BotoConfig

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HEADER = "BETRIEBSTAG;FAHRT_BEZEICHNER;BETREIBER_ID;LINIEN_TEXT\n"
BUCKET = "travel-buddy-test"


def csv_for(day, version):
    return (HEADER + f"{day};85:11:1:000;85:11;S1;v{version}\n").encode()


class Source:
    version = 1
    hits = Counter()
    html_days = set()          # serve an HTML page (200) instead of CSV
    missing_in_zip = set()     # days left out of the monthly ZIP
    zip_prefix = ""            # e.g. "sub/" to simulate an upstream format change


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        Source.hits[self.path] += 1
        # /permalink/2026-10-01_istdaten.csv
        if self.path.startswith("/permalink/"):
            day = self.path.split("/")[-1][:10]
            body = (b"<html>challenge</html>" if day in Source.html_days
                    else csv_for(day, Source.version))
            return self._send(200, body)
        # /archive/2026/2026-09.zip
        if self.path.startswith("/archive/"):
            y, m = self.path.split("/")[-1][:-4].split("-")
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for d in range(1, 32):
                    try:
                        day = date(int(y), int(m), d).isoformat()
                    except ValueError:
                        break
                    if day not in Source.missing_in_zip:
                        z.writestr(f"{Source.zip_prefix}{day}_istdaten.csv",
                                   csv_for(day, Source.version))
            return self._send(200, buf.getvalue())
        self._send(404, b"not found")

    def _send(self, code, body):
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src_port = free_port()
        cls.moto = None
        cls.s3_endpoint = os.environ.get("E2E_S3_ENDPOINT")
        cls.s3_key = os.environ.get("E2E_S3_ACCESS_KEY_ID", "test")
        cls.s3_secret = os.environ.get("E2E_S3_SECRET_ACCESS_KEY", "test")
        if not cls.s3_endpoint:
            port = free_port()
            cls.s3_endpoint = f"http://127.0.0.1:{port}"
            cls.moto = subprocess.Popen(
                [sys.executable, "-m", "moto.server", "-p", str(port)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.http = ThreadingHTTPServer(("127.0.0.1", cls.src_port), Handler)
        threading.Thread(target=cls.http.serve_forever, daemon=True).start()
        cls.tmp = tempfile.mkdtemp(prefix="fetcher-scratch-")
        cls.s3 = boto3.client("s3", endpoint_url=cls.s3_endpoint,
                              aws_access_key_id=cls.s3_key,
                              aws_secret_access_key=cls.s3_secret,
                              region_name="us-east-1",
                              config=BotoConfig(s3={"addressing_style": "path"}))
        for _ in range(50):
            try:
                cls.s3.list_buckets(); break
            except Exception:
                time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        if cls.moto:
            cls.moto.terminate()
        cls.http.shutdown()

    def setUp(self):
        Source.version, Source.html_days, Source.missing_in_zip = 1, set(), set()
        Source.zip_prefix = ""
        Source.hits.clear()

    def run_cli(self, *args, **env_overrides):
        env = {
            "PATH": os.environ["PATH"],
            "S3_ENDPOINT_URL": self.s3_endpoint,
            "S3_ACCESS_KEY_ID": self.s3_key, "S3_SECRET_ACCESS_KEY": self.s3_secret,
            "S3_BUCKET": BUCKET, "S3_CREATE_BUCKET": "true",
            "PERMALINK_URL_TEMPLATE": f"http://127.0.0.1:{self.src_port}/permalink/{{day}}_istdaten.csv",
            "ARCHIVE_URL_TEMPLATE": f"http://127.0.0.1:{self.src_port}/archive/{{year}}/{{year}}-{{month}}.zip",
            "FETCHER_REFERENCE_DATE": "2026-10-03",
            "FETCHER_TMP_DIR": self.tmp,
        }
        env.update(env_overrides)
        p = subprocess.run([sys.executable, "-m", "app", *args], cwd=ROOT, env=env,
                           capture_output=True, text=True, timeout=60)
        return p.returncode, p.stdout + p.stderr

    def get(self, day):
        return self.s3.get_object(Bucket=BUCKET, Key=f"raw/{day}_istdaten.csv")["Body"].read()

    def keys(self):
        try:
            r = self.s3.list_objects_v2(Bucket=BUCKET)
        except self.s3.exceptions.NoSuchBucket:
            return []
        return sorted(o["Key"] for o in r.get("Contents", []))

    def clear_bucket(self):
        for k in self.keys():
            self.s3.delete_object(Bucket=BUCKET, Key=k)

    # ------------------------------------------------------------------ tests
    def test_01_range_across_month_boundary_then_idempotent_then_force(self):
        self.clear_bucket()
        code, out = self.run_cli("--start-date", "2026-09-29", "--end-date", "2026-10-02")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.keys(), [f"raw/{d}_istdaten.csv" for d in
                                       ["2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02"]])
        self.assertEqual(Source.hits["/archive/2026/2026-09.zip"], 1)   # one ZIP for 2 days
        self.assertEqual(Source.hits["/permalink/2026-10-01_istdaten.csv"], 1)
        self.assertEqual(out.count("source=archive status=FETCHED"), 2, out)
        self.assertEqual(out.count("source=permalink status=FETCHED"), 2, out)
        self.assertEqual(self.get("2026-09-29"), csv_for("2026-09-29", 1))
        self.assertEqual(os.listdir(self.tmp), [], "temp dir not cleaned up")

        # Rerun: everything skipped, sources not contacted at all.
        Source.hits.clear(); Source.version = 2
        code, out = self.run_cli("--start-date", "2026-09-29", "--end-date", "2026-10-02")
        self.assertEqual(code, 0, out)
        self.assertEqual(out.count("status=SKIPPED"), 4, out)
        self.assertEqual(sum(Source.hits.values()), 0)
        self.assertEqual(self.get("2026-09-29"), csv_for("2026-09-29", 1))

        # Force overwrite: archive day and permalink day both replaced.
        code, out = self.run_cli("--start-date", "2026-09-30", "--end-date", "2026-10-01",
                                 "--force-overwrite")
        self.assertEqual(code, 0, out)
        self.assertEqual(out.count("status=OVERWRITTEN"), 2, out)
        self.assertEqual(self.get("2026-09-30"), csv_for("2026-09-30", 2))
        self.assertEqual(self.get("2026-10-01"), csv_for("2026-10-01", 2))
        self.assertEqual(self.get("2026-09-29"), csv_for("2026-09-29", 1))  # untouched

    def test_02_partial_existence_only_fetches_missing(self):
        self.clear_bucket()
        self.run_cli("--date", "2026-10-01")
        Source.hits.clear()
        code, out = self.run_cli("--start-date", "2026-10-01", "--end-date", "2026-10-02")
        self.assertEqual(code, 0, out)
        self.assertIn("date=2026-10-01 source=permalink status=SKIPPED", out)
        self.assertIn("date=2026-10-02 source=permalink status=FETCHED", out)
        self.assertEqual(Source.hits["/permalink/2026-10-01_istdaten.csv"], 0)

    def test_03_failures_are_reported_and_isolated(self):
        self.clear_bucket()
        Source.html_days = {"2026-10-02"}
        Source.missing_in_zip = {"2026-09-15"}
        code, out = self.run_cli("--start-date", "2026-09-14", "--end-date", "2026-09-15")
        self.assertEqual(code, 1, out)
        self.assertIn("date=2026-09-14 source=archive status=FETCHED", out)
        self.assertIn("date=2026-09-15 source=archive status=FAILED", out)
        code, out = self.run_cli("--date", "2026-10-02")
        self.assertEqual(code, 1, out)
        self.assertIn("does not look like an Ist-Daten CSV", out)
        self.assertNotIn("raw/2026-10-02_istdaten.csv", self.keys())  # nothing uploaded
        self.assertEqual(os.listdir(self.tmp), [])

    def test_04_failed_force_overwrite_keeps_existing_object(self):
        self.clear_bucket()
        self.run_cli("--date", "2026-10-01")
        Source.html_days = {"2026-10-01"}
        code, out = self.run_cli("--date", "2026-10-01", "--force-overwrite")
        self.assertEqual(code, 1, out)
        self.assertEqual(self.get("2026-10-01"), csv_for("2026-10-01", 1))

    def test_05_unreachable_archive_fails_and_cleans_up(self):
        code, out = self.run_cli(
            "--date", "2026-08-01", "--force-overwrite",
            ARCHIVE_URL_TEMPLATE=f"http://127.0.0.1:{self.src_port}/nope/{{year}}-{{month}}.zip")
        self.assertEqual(code, 1, out)
        self.assertIn("status=FAILED", out)
        self.assertEqual(os.listdir(self.tmp), [])

    def test_06_argument_and_config_errors_exit_2(self):
        for args in (["--date", "2026-10-04"],                     # future
                     ["--start-date", "2026-10-01"],               # missing end
                     ["--date", "2026-10-01", "--end-date", "2026-10-02"],
                     ["--start-date", "2026-10-02", "--end-date", "2026-10-01"],
                     ["--date", "01.10.2026"],
                     []):
            code, out = self.run_cli(*args)
            self.assertEqual(code, 2, f"{args}: {out}")
        code, out = self.run_cli("--date", "2026-10-01", S3_BUCKET="")
        self.assertEqual(code, 2, out)
        code, out = self.run_cli("--date", "2026-10-01", FETCHER_REFERENCE_DATE="garbage")
        self.assertEqual(code, 2, out)

    def test_07_first_of_month_uses_permalink_for_yesterday(self):
        self.clear_bucket()
        # Daily run on 1 Nov fetching 31 Oct: permalink, no archive download.
        code, out = self.run_cli("--date", "2026-10-31", FETCHER_REFERENCE_DATE="2026-11-01")
        self.assertEqual(code, 0, out)
        self.assertIn("date=2026-10-31 source=permalink status=FETCHED", out)
        self.assertEqual(Source.hits["/permalink/2026-10-31_istdaten.csv"], 1)
        self.assertFalse([p for p in Source.hits if p.startswith("/archive/")])
        # Range on 1 Nov: 30 Oct from the archive, 31 Oct + 1 Nov from permalink.
        Source.hits.clear()
        code, out = self.run_cli("--start-date", "2026-10-30", "--end-date", "2026-11-01",
                                 "--force-overwrite", FETCHER_REFERENCE_DATE="2026-11-01")
        self.assertEqual(code, 0, out)
        self.assertIn("date=2026-10-30 source=archive status=FETCHED", out)
        self.assertIn("date=2026-10-31 source=permalink status=OVERWRITTEN", out)
        self.assertIn("date=2026-11-01 source=permalink status=FETCHED", out)
        # Same date one day later: back to the archive, same key.
        Source.hits.clear(); Source.version = 2
        code, out = self.run_cli("--date", "2026-10-31", "--force-overwrite",
                                 FETCHER_REFERENCE_DATE="2026-11-02")
        self.assertEqual(code, 0, out)
        self.assertIn("date=2026-10-31 source=archive status=OVERWRITTEN", out)
        self.assertEqual(self.get("2026-10-31"), csv_for("2026-10-31", 2))

    def test_08_zip_members_must_be_at_root(self):
        self.clear_bucket()
        Source.zip_prefix = "sub/"
        code, out = self.run_cli("--date", "2026-09-10")
        self.assertEqual(code, 1, out)
        self.assertIn("not found in archive", out)
        self.assertEqual(self.keys(), [])

    def test_09_bucket_must_exist_when_creation_disabled(self):
        code, out = self.run_cli("--date", "2026-10-01", S3_BUCKET="does-not-exist-xyz",
                                 S3_CREATE_BUCKET="false", S3_BUCKET_WAIT_SECONDS="3")
        self.assertEqual(code, 2, out)
        self.assertIn("does not exist", out)

    def test_10_inventory_lists_raw_objects(self):
        self.clear_bucket()
        self.run_cli("--start-date", "2026-10-01", "--end-date", "2026-10-02")
        env = {"PATH": os.environ["PATH"], "S3_ENDPOINT_URL": self.s3_endpoint,
               "S3_ACCESS_KEY_ID": self.s3_key, "S3_SECRET_ACCESS_KEY": self.s3_secret,
               "S3_BUCKET": BUCKET}
        p = subprocess.run([sys.executable, "-m", "app.inventory"], cwd=ROOT, env=env,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("raw/2026-10-01_istdaten.csv", p.stdout)
        self.assertIn("2 object(s)", p.stdout)

    def test_11_empty_template_env_falls_back_to_default(self):
        # Compose passes VAR= for unset vars; must not crash, must use defaults.
        from app.config import Config, DEFAULT_ARCHIVE_URL_TEMPLATE
        os.environ.update({"S3_ACCESS_KEY_ID": "x", "S3_SECRET_ACCESS_KEY": "x",
                           "S3_BUCKET": "b", "ARCHIVE_URL_TEMPLATE": ""})
        self.assertEqual(Config.from_env().archive_url_template, DEFAULT_ARCHIVE_URL_TEMPLATE)


if __name__ == "__main__":
    unittest.main()
