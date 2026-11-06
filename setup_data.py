"""Download one month of NYC taxi trips and load it into a local DuckDB file.

Source: NYC Taxi and Limousine Commission (TLC) trip record data,
https://www.nyc.gov/site/tlc/about/tlc-trip-record-data.page

Run once:  python setup_data.py
"""

from __future__ import annotations

import shutil
import ssl
import urllib.request
from pathlib import Path

import certifi
import duckdb

DATA_DIR = Path(__file__).parent / "data"
DB_PATH = DATA_DIR / "taxi.duckdb"

FILES = {
    "yellow_tripdata_2024-01.parquet": "https://d37ci6vzurychx.cloudfront.net/trip-data/yellow_tripdata_2024-01.parquet",
    "taxi_zone_lookup.csv": "https://d37ci6vzurychx.cloudfront.net/misc/taxi_zone_lookup.csv",
}


def download() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    # Python from python.org on a Mac ships without trusted certificates; certifi supplies them.
    context = ssl.create_default_context(cafile=certifi.where())
    for name, url in FILES.items():
        target = DATA_DIR / name
        if target.exists():
            print(f"Already have {name}")
            continue
        print(f"Downloading {name} ...")
        partial = target.with_suffix(target.suffix + ".part")
        with urllib.request.urlopen(url, context=context) as response, partial.open("wb") as out:
            shutil.copyfileobj(response, out)
        partial.rename(target)


def build_database() -> None:
    if DB_PATH.exists():
        DB_PATH.unlink()
    con = duckdb.connect(str(DB_PATH))
    trips_file = DATA_DIR / "yellow_tripdata_2024-01.parquet"
    zones_file = DATA_DIR / "taxi_zone_lookup.csv"

    # Plain snake_case names make the SQL easier for people and for the model.
    con.execute(f"""
        CREATE TABLE trips AS
        SELECT
            VendorID              AS vendor_id,
            tpep_pickup_datetime  AS pickup_at,
            tpep_dropoff_datetime AS dropoff_at,
            passenger_count,
            trip_distance,
            RatecodeID            AS rate_code_id,
            PULocationID          AS pickup_location_id,
            DOLocationID          AS dropoff_location_id,
            payment_type,
            fare_amount,
            extra,
            mta_tax,
            tip_amount,
            tolls_amount,
            improvement_surcharge,
            total_amount,
            congestion_surcharge,
            Airport_fee           AS airport_fee
        FROM read_parquet('{trips_file}')
        WHERE tpep_pickup_datetime >= '2024-01-01'
          AND tpep_pickup_datetime <  '2024-02-01'
    """)

    con.execute(f"""
        CREATE TABLE zones AS
        SELECT
            LocationID AS location_id,
            Borough    AS borough,
            Zone       AS zone,
            service_zone
        FROM read_csv_auto('{zones_file}')
    """)

    # Codes from the TLC yellow taxi data dictionary.
    con.execute("""
        CREATE TABLE payment_types AS
        SELECT * FROM (VALUES
            (0, 'Flex fare'),
            (1, 'Credit card'),
            (2, 'Cash'),
            (3, 'No charge'),
            (4, 'Dispute'),
            (5, 'Unknown'),
            (6, 'Voided trip')
        ) AS t(payment_type, payment_name)
    """)

    build_quality_checks(con)

    count = con.execute("SELECT count(*) FROM trips").fetchone()[0]
    print(f"Built {DB_PATH.name} with {count:,} trips")
    con.close()


# Records that are almost surely data-entry errors, refunds, or meter faults.
# Each rule: (name, columns it affects, SQL condition that flags a bad row).
QUALITY_RULES = [
    ("Zero or negative distance", "trip_distance", "trip_distance <= 0"),
    ("Distance over 100 miles", "trip_distance", "trip_distance > 100"),
    ("Negative fare", "fare_amount, total_amount", "fare_amount < 0"),
    ("Negative total", "total_amount", "total_amount < 0"),
    ("Fare over $500", "fare_amount, total_amount", "fare_amount > 500"),
    ("Drop-off at or before pickup", "pickup_at, dropoff_at", "dropoff_at <= pickup_at"),
    ("Trip longer than 6 hours", "pickup_at, dropoff_at", "date_diff('minute', pickup_at, dropoff_at) > 360"),
    ("Zero passengers", "passenger_count", "passenger_count = 0"),
]


def build_quality_checks(con: duckdb.DuckDBPyConnection) -> None:
    """Make quality_rules (each rule and how many trips it flags) and trips_clean (trips minus flagged rows)."""
    con.execute("CREATE TABLE quality_rules (rule VARCHAR, columns_affected VARCHAR, condition VARCHAR, rows_flagged BIGINT)")
    for rule, columns, condition in QUALITY_RULES:
        flagged = con.execute(f"SELECT count(*) FROM trips WHERE {condition}").fetchone()[0]
        con.execute("INSERT INTO quality_rules VALUES (?, ?, ?, ?)", [rule, columns, condition, flagged])

    any_rule = " OR ".join(f"({condition})" for _, _, condition in QUALITY_RULES)
    # coalesce keeps rows where a rule can't be checked (a blank passenger count) instead of dropping them.
    con.execute(f"CREATE VIEW trips_clean AS SELECT * FROM trips WHERE NOT coalesce({any_rule}, false)")
    removed = con.execute(f"SELECT count(*) FROM trips WHERE {any_rule}").fetchone()[0]
    print(f"Quality rules flag {removed:,} trips; trips_clean leaves them out")


if __name__ == "__main__":
    download()
    build_database()
