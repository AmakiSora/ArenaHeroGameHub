"""Smoke check for the production progress bar in render_config_panel."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dashboard import render_config_panel

html = render_config_panel(7, 4, 4)
assert 'prodBarWorkers' in html and 'prodBarVanguards' in html and 'prodBarRangers' in html
assert 'production-bar-fill producing' in html  # workers 7/10 -> producing
m = re.search(r'prodBarWorkers[^>]*style="width:(\d+)%"', html)
assert m and m.group(1) == '70', m.group(1) if m else 'no match'
print('workers 7/10 ->', m.group(1) + '% producing OK')

html2 = render_config_panel(10, 4, 5)
m2 = re.search(r'prodBarWorkers[^>]*style="width:(\d+)%"', html2)
assert m2 and m2.group(1) == '100', m2.group(1) if m2 else 'no match'
assert 'production-bar-fill ok' in html2
print('workers 10/10 -> 100% ok OK')

html3 = render_config_panel(12, 4, 5)  # over target caps at 100
m3 = re.search(r'prodBarWorkers[^>]*style="width:(\d+)%"', html3)
assert m3 and m3.group(1) == '100'
assert 'aria-valuenow="10"' in html3  # valuenow capped at target
print('workers 12/10 -> capped 100% OK')

html4 = render_config_panel(3, 0, 5)  # 3/10 -> 30%
m4 = re.search(r'prodBarWorkers[^>]*style="width:(\d+)%"', html4)
assert m4 and m4.group(1) == '30', m4.group(1) if m4 else 'no match'
print('workers 3/10 -> 30% OK')

import dashboard
from tactic_config import default_config

zero = default_config()
zero['target_workers'] = 0
original = dashboard.load_config
dashboard.load_config = lambda path: zero
try:
    html5 = dashboard.render_config_panel(3, 0, 0)  # target 0 with existing units
finally:
    dashboard.load_config = original
m5 = re.search(r'prodBarWorkers[^>]*style="width:(\d+)%"', html5)
assert m5 and m5.group(1) == '100'
print('target 0 with units -> 100% OK')
print('ALL PASS')
