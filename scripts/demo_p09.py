"""Compatibility entry point; prefer `after-sales demo`."""

import argparse

from after_sales.demo import run_demo


def main(port):
    run_demo(port=port)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    main(parser.parse_args().port)
