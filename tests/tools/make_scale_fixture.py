"""Generate the table-keys scale-acceptance fixture (4096 x 24 by default).

Each of the 12 binary vector positions is emitted twice as a pair of identical
columns bXXa/bXXb; row rXXXX carries the bits of integer XXXX, with b00 the
least-significant bit and b11 the most-significant.  Standard library only.
"""
import argparse
import json
from pathlib import Path


def build(row_count):
    if row_count & (row_count - 1):
        raise SystemExit('--rows must be a power of two')
    bits = row_count.bit_length() - 1
    columns = ['b%02d%s' % (position, suffix)
               for position in range(bits) for suffix in ('a', 'b')]
    rows = []
    for value in range(row_count):
        cells = []
        for position in range(bits):
            bit = (value >> position) & 1
            cells.extend((bit, bit))
        rows.append({'row_id': 'r%04d' % value, 'values': cells})
    return {'dataset_id': 'scale-%d' % row_count, 'columns': columns,
            'rows': rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rows', type=int, default=4096)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.rows <= 20000:
        raise SystemExit('--rows must be within 1..20000')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('w', encoding='utf-8') as stream:
        json.dump(build(args.rows), stream, ensure_ascii=False)
        stream.write('\n')


if __name__ == '__main__':
    main()
