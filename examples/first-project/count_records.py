"""Synthetic line counter with an intentional README/implementation mismatch."""
import argparse
from pathlib import Path


def count_records(path):
    """Count every input line, including blank lines, without modifying the file."""
    with Path(path).open('r', encoding='utf-8') as source:
        return sum(1 for _ in source)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path', type=Path, help='Synthetic UTF-8 input file')
    args = parser.parse_args(argv)
    print(count_records(args.path))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
