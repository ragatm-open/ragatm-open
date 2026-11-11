#!/usr/bin/env python
import os
import sys
from pathlib import Path

def main():
    # Ajoute le dossier backend/ au PYTHONPATH
    # pour permettre `import config...` et `import apps...`
    backend_dir = Path(__file__).resolve().parent  # .../ragATM/backend
    sys.path.insert(0, str(backend_dir))

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

    from django.core.management import execute_from_command_line
    execute_from_command_line(sys.argv)

if __name__ == "__main__":
    main()
