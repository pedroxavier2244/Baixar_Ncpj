import csv
from pathlib import Path

p = Path('data/2026-02/transformed/empresas.csv')
if not p.exists():
    print('NO_FILE')
    raise SystemExit

bad = 0
samples = []
with p.open('r', encoding='utf-8', newline='') as f:
    r = csv.reader(f)
    header = next(r, None)
    for i, row in enumerate(r, start=2):
        if not row:
            continue
        if (row[0].strip() if len(row) > 0 else '') == '':
            bad += 1
            if len(samples) < 5:
                samples.append((i, row[:7]))
print('bad_rows', bad)
print('samples')
for s in samples:
    print(s)
