# Mail Desk

A local two-page dashboard for searching Gmail inboxes, downloading attachments,
and sending local attachment files through an editable CSV mapping.

## Setup a Google account

Complete these steps once for the Google account that will authorize the app.
The Cloud project and OAuth client identify the application; they do not lock it
to a particular Gmail mailbox.

1. **Create a Cloud project.** Sign into the [Google Cloud Console](https://console.cloud.google.com/)
   with the Google account you use to manage app settings. Create a project or
   select one you already have. A billing account is generally not required for
   this local API usage.
2. **Enable APIs.** In the selected project, enable the [Gmail API](https://console.cloud.google.com/flows/enableapi?apiid=gmail.googleapis.com).
   Also enable the [Google Drive API](https://console.cloud.google.com/flows/enableapi?apiid=drive.googleapis.com)
   if you want to download Drive links from messages or send oversized files
   through Drive.
3. **Configure Google Auth Platform.** Open [Google Auth Platform](https://console.cloud.google.com/auth/overview)
   in the same project and complete the app branding/contact details. For a
   personal Gmail account, set the audience to **External**. **Internal** is
   only for accounts belonging to the same Google Workspace organization as the
   Cloud project.
4. **Add test users.** Under **Audience**, add every Gmail account that will
   sign into this local app. Leave the app in **Testing** for personal use.
   You do not need to publish it just to use it yourself or with listed test
   accounts. Publishing/verification is for making the OAuth app generally
   available; it is not required to enable the APIs for your own testing.
5. **Create the OAuth client file.** Go to **Clients → Create client → Desktop
   app**, create the client, and download its JSON file. Put it in the project
   root as `credentials.json`. Keep this file private; it is ignored by Git.
   Gmail and Drive use this same OAuth client file. If it is in another
   location, set `GOOGLE_DRIVE_CLIENT_SECRETS_FILE` in `.env` to its path.
6. **Install and start the app.** From the project folder:

   ```sh
   python -m pip install -r requirements.txt
   uvicorn app:app --host 127.0.0.1 --port 8000
   ```

7. **Authorize Gmail.** Open <http://127.0.0.1:8000>, click **Connect Gmail API**,
   and sign into the Gmail account you want the dashboard to use. Add that
   account as a test user first. The dashboard automatically reads the
   authorized mailbox address from the Gmail API profile; no `GMAIL_USER` or
   Gmail app password is needed. The token is saved locally in
   `data/gmail-token.json`.
8. **Authorize Drive if needed.** Click **Connect Google Drive** to sign in to
   the account whose Drive should be used for linked-file downloads and
   oversized-file uploads. This can be a different Google account from the
   sender mailbox. Its token is saved separately in `data/drive-token.json`.

When an External OAuth app remains in Testing, Google can expire refresh tokens
after seven days for Gmail scopes. If mail access stops, click **Connect Gmail
API** again. Recipients of large-file Drive links must have permission to open
the Drive file; the app grants reader access to the selected recipient.

### Sending from another Gmail account

The OAuth `credentials.json` identifies the app, not the mailbox. The dashboard
uses whichever Gmail account you select during **Connect Gmail API** sign-in.
To switch from account A to B:

1. Add account B as an OAuth test user if the app is External and in Testing.
2. Click **Reconnect Gmail API**.
3. In Google sign-in, explicitly choose account B and approve access.

The app uses **one active Gmail mailbox at a time**. Authorizing B replaces the
saved Gmail token for A in `data/gmail-token.json`; the app does not impersonate
other mailboxes or send from A and B simultaneously. Repeat the steps to switch
back. The Drive connection is separate: large files are uploaded to the
connected Drive account, then shared with the email recipient.

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
