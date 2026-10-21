"""Weekly Google Drive backup hook — STUB (Phase 2).

Why stubbed: uploading to the user's Drive from PythonAnywhere needs either
(a) an OAuth user-consent flow (interactive — can't run headless), or
(b) a Google service-account JSON key (the user never provided one, and we
do NOT invent credentials).

What works TODAY (v1):
- The app takes a timestamped SQLite backup on every /backup call and keeps
  the last 30 in backups/. The owner can download any backup from the
  Integrations page and it lands in his own Drive/phone manually.

Options for Phase 2 (user's call):
1. He creates a Google Cloud service account, shares ONE Drive folder with
   it, and gives Rio the JSON key via the Secure Vault. Then this module
   uploads backups/xxx.db to that folder weekly via a PythonAnywhere
   scheduled task (google-api-python-client; googleapis.com IS on the
   free-tier whitelist).
2. A cron on Rio's side pulls the newest backup via the Drive connector.

Function signature for Phase 2 (keep stable):
    upload_latest_backup(drive_folder_id: str, sa_json_path: str) -> str
    Returns the Drive file id on success, raises on failure.
"""


def upload_latest_backup(drive_folder_id: str, sa_json_path: str) -> str:
    raise NotImplementedError(
        "Drive backup is Phase 2: needs a service-account JSON key from the "
        "user (Secure Vault). See module docstring."
    )
