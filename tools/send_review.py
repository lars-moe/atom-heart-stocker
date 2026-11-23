#!/usr/bin/env python3
"""E-mail one or more review files (Markdown) as HTML.

Settings come from environment variables (GitHub repository secrets):
  SMTP_HOST, SMTP_PORT (465 = SSL, 587 = STARTTLS), SMTP_USER, SMTP_PASSWORD, REPORT_TO
"""
import os
import smtplib
import sys
from email.message import EmailMessage
from pathlib import Path

import markdown

CSS = ("body{font-family:Georgia,serif;color:#191a18;max-width:760px;margin:auto;line-height:1.55}"
       "h1,h2,h3{font-weight:400}table{border-collapse:collapse}td,th{border-bottom:1px solid #e3dfd7;"
       "padding:6px 8px;text-align:left}code{background:#f4f2ee;padding:1px 4px}")


def main(files):
    env = {k: os.getenv(k, "") for k in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "REPORT_TO")}
    missing = [k for k, v in env.items() if not v]
    if missing:
        print(f"E-mail not configured (missing secrets: {', '.join(missing)}); skipping.")
        return 0
    port = int(env["SMTP_PORT"])
    dash = f"https://{os.getenv('GITHUB_REPOSITORY_OWNER', '')}.github.io/{os.getenv('GITHUB_REPOSITORY', '/').split('/')[1]}/"
    for f in files:
        p = Path(f)
        if not p.exists() or p.name.lower() == "readme.md":
            continue
        text = p.read_text(encoding="utf-8")
        kind = "Daily" if "daily" in p.parts else "Weekly"
        msg = EmailMessage()
        msg["Subject"] = f"Atom Heart Stocker - {kind} review {p.stem}"
        msg["From"] = env["SMTP_USER"]
        msg["To"] = env["REPORT_TO"]
        msg.set_content(text + f"\n\nDashboard: {dash}\n")
        body = markdown.markdown(text, extensions=["tables"])
        msg.add_alternative(f"<html><head><style>{CSS}</style></head><body>{body}"
                            f"<p><a href='{dash}'>Open the dashboard</a></p></body></html>", subtype="html")
        if port == 465:
            with smtplib.SMTP_SSL(env["SMTP_HOST"], port, timeout=60) as s:
                s.login(env["SMTP_USER"], env["SMTP_PASSWORD"])
                s.send_message(msg)
        else:
            with smtplib.SMTP(env["SMTP_HOST"], port, timeout=60) as s:
                s.starttls()
                s.login(env["SMTP_USER"], env["SMTP_PASSWORD"])
                s.send_message(msg)
        print(f"Sent {p} to {env['REPORT_TO'][:3]}***")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
