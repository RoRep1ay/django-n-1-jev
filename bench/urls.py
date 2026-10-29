"""Every benchmark case served at /cases/<id>/, so URLconf-driven tooling (harness/ci_check.py) can find them."""
import importlib
import json
from pathlib import Path

from django.urls import path

CASES = json.loads((Path(__file__).resolve().parent.parent / 'cases' / 'manifest.json').read_text())

urlpatterns = [path(f'cases/{case["id"]}/', importlib.import_module(f'cases.{case["id"]}').View.as_view())
               for case in CASES]
