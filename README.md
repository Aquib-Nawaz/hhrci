# Mail Desk

A local two-page dashboard for searching Gmail inboxes, downloading attachments,
and sending local attachment files through an editable CSV mapping.

## Setup

1. Create a Gmail app password for the one Gmail account you want to use. Enable IMAP
   access in Gmail if it is not already enabled.
2. Enter your Gmail address and app password in the `.env` file in the project
   directory:

   ```sh
   GMAIL_USER=you@gmail.com
   GMAIL_APP_PASSWORD=your-16-character-app-password
   ```

   The project includes a blank `.env` file for these values. The app loads it
   at startup; existing environment variables take precedence. If
   `GMAIL_ACCOUNTS` is set in the environment, that configuration takes
   precedence over these two values. Never share or commit the `.env` file.
3. Install dependencies and run the local server:

   ```sh
   python -m pip install -r requirements.txt
   uvicorn app:app --host 127.0.0.1 --port 8000
   ```

4. Open <http://127.0.0.1:8000>.

## Dashboard behavior

- **Download mail** searches the selected single inbox from the chosen date
  onward, optionally filtering by one or more comma-separated sender addresses.
  Multiple senders are combined as OR matches.
  Every use case gets a unique UUID test ID and a JSON record at
  `data/tests/<test-id>.json`. It stores the search fields, message results,
  operation status/progress/errors, and counts for found, downloaded messages,
  downloaded files, and updated (uploaded) messages. Removing a row only
  removes it from that saved run; it does not delete the Gmail message.
  After reviewing search results, choose an output folder and start downloading.
  Downloaded attachments are written beneath that folder, grouped by test run
  and account.
- **Upload attachments** scans the selected local folder recursively and
  generates mapping rows from standalone five-digit roll numbers in filenames.
  The CSV uses columns `roll_number,mail_account,sent`; enter a recipient email
  for each roll number. A unique matching local file is sent as a new email
  attachment from the selected Gmail account. If more than one file matches a
  roll number, that mapping is reported as ambiguous and is not sent. Successful
  sends are marked `sent`; clear that value to enable a resend. Scanning a folder
  preserves existing recipient mappings and sent status.

Searching senders, downloading messages, and sending mapped files run in
parallel worker threads. The dashboard polls the saved test record for progress
while each operation is running. Each test JSON also records its upload folder,
sender account, and detected file paths.

Closing or refreshing the browser tab does not stop a running job. When the
dashboard is reopened, it finds the latest active test, opens the relevant
download or upload page, and resumes polling its saved status. Keep the local
Uvicorn server running; stopping or restarting the server interrupts in-flight
jobs.

The server is intentionally bound to localhost by default because the dashboard
can access local folders and send email.

The folder buttons open the operating system's native folder picker. Run the
server on the same desktop computer where the folders are stored.
