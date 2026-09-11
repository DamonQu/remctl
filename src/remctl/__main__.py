"""Allow remctl to run as ``python -m remctl``."""

from remctl.cli import main


raise SystemExit(main())
