#!/usr/bin/env python3
"""Send one email through the shared SMTP config. Used by the shell scripts.

  uoa-sendmail.py --subject S --text FILE [--html FILE] [--dry-run]
"""
import argparse, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import uoa_common as U

p = argparse.ArgumentParser()
p.add_argument("--subject", required=True)
p.add_argument("--text", required=True, help="path to plain-text body")
p.add_argument("--html", help="path to HTML body")
p.add_argument("--to", help="override recipient")
p.add_argument("--tool", default="sendmail")
p.add_argument("--dry-run", action="store_true")
a = p.parse_args()

def read(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError as exc:
        U.log(a.tool, "error", f"cannot read body {path}: {exc}")
        sys.exit(1)

cfg = U.load_config()
user, _, smtp_pw = U.read_credentials(a.tool)
ok = U.send_mail(cfg, user, smtp_pw, a.subject, read(a.text),
                 read(a.html) if a.html else None,
                 dry_run=a.dry_run, tool=a.tool, recipient=a.to)
print("sent" if ok else "FAILED")
sys.exit(0 if ok else 4)
