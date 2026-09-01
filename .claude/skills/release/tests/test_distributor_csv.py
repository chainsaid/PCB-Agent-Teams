import csv

from scripts.distributor_csv import (
    transform_to_digikey,
    transform_to_lcsc,
    transform_to_mouser,
)

SAMPLE_BOM = [
    {
        "Qty": "1",
        "Refs": "U1",
        "MPN": "AMC1311BDWVR",
        "Category": "ic",
        "Footprint": "SOIC-8",
        "Vendor_Status": "active",
        "Vendor_Stock": "12756",
        "Vendor_Url": "https://www.digikey.com/...",
        "Datasheet": "...pdf",
    },
    {
        "Qty": "2",
        "Refs": "C1,C2",
        "MPN": "C0805C104K5RACTU",
        "Category": "generic",
        "Footprint": "C_0805_2012Metric",
        "Vendor_Status": "active",
        "Vendor_Stock": "500000",
        "Vendor_Url": "",
        "Datasheet": "",
    },
]


def test_transform_to_digikey_columns_and_values():
    rows = transform_to_digikey(SAMPLE_BOM)
    assert rows[0] == {
        "Manufacturer Part Number": "AMC1311BDWVR",
        "Quantity": "1",
        "Customer Reference": "U1",
    }
    assert rows[1] == {
        "Manufacturer Part Number": "C0805C104K5RACTU",
        "Quantity": "2",
        "Customer Reference": "C1,C2",
    }


def test_transform_to_mouser_columns_and_values():
    rows = transform_to_mouser(SAMPLE_BOM)
    assert list(rows[0].keys()) == ["Mfr Part Number", "Quantity", "Description"]
    assert rows[0]["Mfr Part Number"] == "AMC1311BDWVR"
    assert rows[0]["Quantity"] == "1"
    assert rows[0]["Description"] == "ic / SOIC-8"


def test_transform_to_lcsc_columns_and_values():
    rows = transform_to_lcsc(SAMPLE_BOM)
    assert list(rows[0].keys()) == [
        "Comment",
        "Designator",
        "Footprint",
        "LCSC Part #",
        "Manufacture Part Number",
        "Quantity",
    ]
    assert rows[0]["Designator"] == "U1"
    assert rows[0]["Footprint"] == "SOIC-8"
    assert rows[0]["Manufacture Part Number"] == "AMC1311BDWVR"
    assert rows[0]["LCSC Part #"] == ""
    assert rows[0]["Quantity"] == "1"


def test_transform_to_lcsc_extracts_part_number_from_vendor_url():
    """Vendor_Url already carries the LCSC product ID (component-selecting/
    component-preparing evidence's vendor.url) — the column shouldn't be left
    blank when it's sitting right there, forcing the user to click through
    and read it off the URL themselves."""
    rows = transform_to_lcsc([{
        "Qty": "1", "Refs": "R1", "MPN": "FRC1206F8202TS", "Category": "generic",
        "Footprint": "R_1206_3216Metric", "Vendor_Status": "active",
        "Vendor_Stock": "22975",
        "Vendor_Url": "https://www.lcsc.com/product-detail/2960747.html",
        "Datasheet": "",
    }])
    assert rows[0]["LCSC Part #"] == "C2960747"


def test_round_trip_through_csv_writer(tmp_path):
    out = tmp_path / "digikey_bulk.csv"
    rows = transform_to_digikey(SAMPLE_BOM)
    with out.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    parsed = list(csv.DictReader(out.open()))
    assert len(parsed) == 2
    assert parsed[0]["Manufacturer Part Number"] == "AMC1311BDWVR"
