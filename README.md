# Mail Desk

A local two-page dashboard for searching Gmail inboxes, downloading attachments,
and sending local attachment files through an editable CSV mapping.

## Setup

1. Set the Gmail address the dashboard should use in the `.env` file:

   ```sh
   GMAIL_USER=you@gmail.com
   ```

   The app loads `.env` at startup; existing environment variables take
   precedence. `GMAIL_APP_PASSWORD` is no longer used. The app uses OAuth for
   Gmail API access instead of IMAP/SMTP.
2. In the same Google Cloud project, enable **Gmail API** and **Google Drive
   API**. Configure the Google Auth Platform consent screen, choose **External**
   for a personal Gmail account, and add your account as a test user. The app
   requests Gmail read and send access and Drive file access.
3. Create an OAuth client ID for a **Desktop app**.
   Download its JSON file to the project directory as `credentials.json`.
   If you store it elsewhere, set `GOOGLE_DRIVE_CLIENT_SECRETS_FILE` in `.env`
   to its path. Do not share or commit this client file.
4. Install dependencies and run the local server:

   ```sh
   python -m pip install -r requirements.txt
   uvicorn app:app --host 127.0.0.1 --port 8000
   ```

5. Open <http://127.0.0.1:8000>. Click **Connect Gmail API** and sign into the
   account set in `GMAIL_USER`. The Gmail OAuth token is saved at
   `data/gmail-token.json`. Click **Connect Google Drive** as well if you want
   to download Drive links from messages or send oversized files via Drive;
   Drive authorization is saved separately at `data/drive-token.json`.
   While the OAuth app is in testing mode, Google may expire Gmail refresh
   tokens after seven days; reconnect from the dashboard if Gmail access stops.

## Dashboard behavior

- **Download mail** uses the Gmail API to search the selected single inbox from the chosen date
  onward, optionally filtering by one or more comma-separated sender addresses.
  Multiple senders are combined as OR matches.
  Enter a test name to label each saved run. Every use case gets a unique UUID
  test ID and a JSON record at `data/tests/<test-id>.json`. It stores the test
  name, search fields, message results,
  operation status/progress/errors, and counts for found, downloaded messages,
  downloaded files, and updated (uploaded) messages. Removing a row only
  removes it from that saved run; it does not delete the Gmail message.
  After reviewing search results, choose an output folder and start downloading.
  Attachments are written directly into that folder. If a filename already
  exists, the Gmail message ID is added to avoid overwriting it. Existing saved
  searches created by the IMAP version use a different ID format; start a new
  search after switching to the Gmail API before downloading those messages.
  Google Drive file links in message bodies are downloaded alongside regular
  email attachments. The linked file must be accessible to the connected Drive
  account. Google Docs, Sheets, Slides, and Drawings are exported as PDF, XLSX,
  PDF, and PNG respectively.
- **Upload attachments** scans the selected local folder recursively and
  generates mapping rows from standalone five-digit roll numbers in filenames.
  Enter a test name for each upload run. The shared CSV uses columns
  `roll_number,mail_account`; these mappings are reused across tests. A unique
  matching local file is sent as a new email
  attachment from the selected Gmail account through the Gmail API. If more than one file matches a
  roll number, that mapping is reported as ambiguous and is not sent. Per-file
  sent status is stored in that upload run's JSON record, not in the shared CSV.
  Set the email subject template for each upload; `{roll_number}` is replaced
  with the matching five-digit value.
  The upload page shows a collapsible per-file status list for the selected run.
  Scanning a folder preserves existing recipient mappings.
  Select **Randomly distribute to evaluators** to save a separate evaluator
  name/email list in `data/evaluators.csv`. The roster autosaves after edits
  and is restored when the upload page is reopened; incomplete rows are not
  saved. The dashboard can also save the roster immediately. It randomly
  distributes uniquely identified student files as evenly as possible across
  evaluators. Each file is assigned to no more than one evaluator; each run
  records the exact file-to-evaluator assignments and send status.
  Generate and review the assignment preview before starting the send. The
  previewed assignment is validated and persisted unchanged in the upload run.
  Starting an upload creates a dedicated upload-run JSON record, independent of
  download/search test records. The upload history selector lists upload runs
  only. Files larger than 16 MiB are uploaded to the connected Google Drive
  account instead of attached to Gmail. Drive grants reader access only to the
  intended recipient; it does not create public links. The email contains the
  Drive link. Drive copies remain in the connected account's Drive after
  sending.

Set `GMAIL_ATTACHMENT_MAX_BYTES` in `.env` to change the attachment cutoff
(positive integer bytes; default `16777216`, or 16 MiB). Keep the limit below
Gmail's maximum message size to allow for MIME encoding and message overhead.

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
