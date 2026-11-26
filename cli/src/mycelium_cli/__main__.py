from __future__ import annotations

import sys

from mycelium_cli.main import main

# Through main(), not app(): main() is what renders a CLIError as a message
# instead of a traceback, and `python -m mycelium_cli` is how the test suite
# runs the CLI, so calling app() here tested a path no installed user takes.
if __name__ == "__main__":
    sys.exit(main())
